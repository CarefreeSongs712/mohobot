# 歌曲索引：持久化缓存 + 按需重建 + 后台构建

> 状态：**历史实施记录**（PR #98，2026-10-30；首次部署当时仍需全量构建一次）
> 基线代码：`dac2e782`（历史代码基点；不表示当前部署一致）
> 编写日期：2026-10-30
> 本文混合了实施前方案、当时生产快照和验收记录；生产路径、数据量、耗时、容量与备份数字均为 2026-10-30 历史观测，不代表当前生产状态。实施状态以当前源码为准。

---

## 1. 背景与目标（实施前背景）

实施前，mohobot 每次启动会在 `SongInfoMatcher.__init__` 里**同步重建**歌曲歌词 6-gram 倒排索引，导致当时启动耗时较长、服务监听和 bot 重连延后。以上是设计稿记录的历史问题，不是当前代码路径或当前性能观测；当前实现状态以本文开头所述源码为准。

目标：

1. **持久化**：索引序列化到磁盘，启动时直接加载。
2. **更新才重建**：用数据库指纹判断是否失效，未变化不重建。
3. **后台构建**：缓存未命中时不阻塞启动，先让 WS/面板起来，索引在后台构建。
4. 匹配结果、阈值、置信度**完全不变**；缓存不可用时自动降级为当前行为。

---

## 2. 实施前核对与历史实测数据

### 2.1 代码路径（2026-10-30 实施前核对；仅作历史背景）

| 位置 | 作用 |
|---|---|
| `main.py` `startup()` L104-121 | 构造 `SongInfoMatcher(...)`，这是阻塞点 |
| `mohobot/music_knowledge/matcher.py` L247-249 | `__init__` 末尾调用 `self._ensure_index()` |
| 同上 L253-280 | `reload_index()`：查全库 → 建 `SongNameIndex` + `LyricGramIndex` |
| 同上 L282-284 | `_ensure_index()`：未加载就同步重建 |
| 同上 L470-485 `_find_by_lyrics()` | 开头调用 `_ensure_index()`，**未就绪时会同步重建** |
| 同上 L288-320 `match()` | 读取 `_name_index` / `_lyric_index`，为 `None` 时直接返回 `None` |
| `mohobot/music_knowledge/fuzzy.py` L447-560 | `LyricGramIndex`，结构为 `dict[str, list[int]]` |
| `mohobot/music_knowledge/fuzzy.py` L205-300 | `SongNameIndex`，重建只需约 2s，不需要持久化 |
| `plugins/song_sync.py` L253-260 | 同步完成后 `matcher.reload_index()`（同步阻塞） |
| `mohobot/music_knowledge/pool.py` | `ensure_init()` / `get_session()`，全局 SQLAlchemy 会话池 |
| `config/global.yaml` L158-168 | `music_knowledge.song_database` 配置 |

### 2.2 生产库数据（2026-10-30 历史快照；非当前状态）

- 当时检查的是部署数据目录下的歌曲知识库 SQLite 文件（WAL 模式）；具体路径、容量、歌曲数、歌词字数及 gram 数值均不在本文保留。
- 主库时间戳和当时 `song_sync` 插件的启用状态属于该次部署快照，不作为当前状态基准。
- 当时备份配置排除缓存目录；当前备份范围需按实际部署配置核实。

### 2.3 实测耗时（2026-10-30 历史环境观测；不可作为当前性能或容量基准）

| 阶段 | 耗时 |
|---|---|
| `import main` 全量导入 | **24.15s**（fastapi/openapi 3.2s、openai 3.2s、sqlalchemy 1.6s 等） |
| 查 16k 行歌词 | 1.4s |
| `SongNameIndex` 构建 | 2.0s |
| 歌词归一化 | 12.4s |
| **`LyricGramIndex.build()`（GC 开）** | **约 150s** |
| **`LyricGramIndex.build()`（GC 关）** | **102s** |
| 情感/TTS/知识库/14 插件/DB/WS/面板 | 约 5s |
| 10 个 bot 重连 | 约 2s |
| 峰值内存（单独构建进程） | 约 1.0GB RSS |

### 2.4 为什么朴素持久化不划算（2026-10-30 历史实验记录）

| 方式 | 写入 | 读回 | 体积 |
|---|---|---|---|
| pickle(protocol=5) | 96s | **106s** | 165MB |
| marshal | 38s | **74s** | 198MB |

原因：543 万个 gram，每个对应一个**单元素 list**。反序列化要重新分配 543 万个 list，时间接近重建。
**结论：必须换成紧凑扁平格式，否则不要做持久化。**

---

## 3. 方案总览（实施前设计）

```
启动
 ├─ 计算 DB 指纹
 ├─ 命中缓存? ──是──> 加载紧凑索引(<10s) ──> 就绪
 └─ 否 ─> 立即返回(服务照常启动) ─> 后台构建 ─> 写缓存 ─> 原子替换 ─> 就绪
```

- 就绪前 `match()` 返回 `None`（走现有的"歌曲识别降级"分支，不影响正常聊天）。
- 同步（`song_sync`）完成后：只标记失效并触发后台重建，**不再同步 reload**。
- 同一份缓存同时可供下一次启动复用。

---

## 4. 详细设计（实施前方案；行为以当前源码为准）

### 4.1 指纹（失效判据）

新建 `mohobot/music_knowledge/index_cache.py`：

```python
INDEX_FORMAT_VERSION = 1      # 序列化格式变更时 +1
NORMALIZE_VERSION = 1         # normalize_name 规则变更时 +1

def compute_fingerprint(db_path: str, *, gram_size: int, fuzzy: bool) -> str:
    st = os.stat(db_path)
    wal = db_path + "-wal"
    wst = os.stat(wal) if os.path.exists(wal) else None
    payload = {
        "v": INDEX_FORMAT_VERSION,
        "norm": NORMALIZE_VERSION,
        "gram": gram_size,
        "fuzzy": bool(fuzzy),
        "db": [st.st_mtime_ns, st.st_size],
        "wal": [wst.st_mtime_ns, wst.st_size] if wst else None,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()[:16]
```

要求：

- **WAL 必须纳入**：SQLite WAL 模式下写入主要改 `-wal`，主库 mtime 可能到 checkpoint 才变。
- 指纹命中 `SongStats.updated_at` 可作为兜底（可选，不作硬依赖）。
- 指纹写进文件头，加载前先比对；不匹配就忽略缓存。

### 4.2 缓存位置与文件

```
data/cache/song_index/
  <fingerprint>.bin          # 索引本体
  <fingerprint>.meta.json    # 可选：构建耗时、条数、创建时间
  .lock                      # fcrtl.flock，避免多进程同时写
```

- 默认 `keep=2`，加载成功后清理更旧的 `<fp>.bin`。
- `data/cache` 已被备份排除，**不会撑大 p0/p1**。
- 整个目录可随时删除，等同于"强制重建"。

### 4.3 紧凑序列化格式（无第三方依赖）

不建议直接 pickle/marshal 整个对象。建议结构：

```
Header:
  magic        b"MBIDX1\0"        (7 bytes)
  version      uint32             (INDEX_FORMAT_VERSION)
  header_len   uint32
  header_json  {fingerprint, docs, grams, postings, gram_size, sha256(body)}
Body:
  doc_uuid     UTF-8 换行拼接      (16157 行)
  doc_name     UTF-8 换行拼接
  doc_lyrics   UTF-8 换行拼接      (已归一化，约 6.85MB)
  doc_len      array('i')          (16157)
  grams_blob   UTF-8 拼接          (按字典序排序)
  gram_off     array('Q')          (grams+1)
  post_ptr     array('Q')          (grams+1, CSR indptr)
  post_doc     array('i')          (总 posting 数)
```

- 加载时**不要**为每个 gram 建 `list[int]`。二选一，实现方先 benchmark：
  - **方案 A（推荐）**：`postings = {gram: memoryview(post_doc)[ptr[i]:ptr[i+1]]}`。5.4M 次 dict 插入，预计 3～6s；`match()` 改动最小（把 `ids` 当序列遍历即可）。
  - **方案 B（更快但改动大）**：不建 dict，保持有序数组，查询时 `bisect` 定位 gram 的 `[lo,hi)`，再遍历 `post_doc`。预计加载 1～3s，但 `LyricGramIndex.match()` 要改查询方式。
- `_index`（`[(name, lyrics)]`）和 `_doc_id`（`uuid -> int`）**不需要落盘**，加载后从 `doc_uuid/doc_name/doc_lyrics` 重建（16k 条，毫秒级）。
- body 用 `sha256` 或 `crc32` 校验，截断/损坏直接判为 miss。
- 写入必须**先写临时文件再 `os.replace()`**，避免半截文件。

### 4.4 加载路径

`SongInfoMatcher.__init__` 改为非阻塞：

```python
self._snapshot = None          # 持有 index / name_index / lyric_index
self._ready = threading.Event()
fp = compute_fingerprint(...)
snap = load_cache(cache_dir, fp)          # 命中则 <10s
if snap is not None:
    self._install(snap)
else:
    self._schedule_build(reason="startup")
```

- `match()`：`snap = self._snapshot`，为 `None` 直接返回 `None`。
- **`_find_by_lyrics()` 里删除 `self._ensure_index()`**，改为 `if self._snapshot is None: return None`。这是必须的，否则第一条带歌词的消息会触发同步重建、卡死事件循环。
- 保留 `reload_index()` 作为**同步**方法（测试/工具用），另新增 `request_rebuild(reason)` 非阻塞入口。

### 4.5 后台构建

接口：

```python
def request_rebuild(self, *, reason: str) -> None
def is_ready(self) -> bool
```

- 已就绪时收到失效请求：置 `_dirty = True`；若正在构建则等结束后再跑一轮。
- 构建产出完整 `Snapshot`，**在 `self._lock` 内原子替换** `self._snapshot`，旧对象由引用计数释放，读路径不需要额外加锁。
- 构建成功后**先写缓存**再替换（下次启动直接命中）。

线程 vs 子进程（实施前的历史方案比较）：

以下为实施前的方案比较，保留作历史决策记录，不是实现说明：

| 方式 | 当时评估 |
|---|---|
| `threading.Thread` | 可直接访问对象；纯 Python CPU 构建仍受 GIL 影响主事件循环。 |
| 子进程 | 避免占用主进程 GIL；通过缓存文件交付结果。 |

当前源码已实现 `thread`、`subprocess`、`sync` 三种模式；应用入口默认 `thread`。线程构建使用 `gc_control=True`，构建期间暂时关闭并在结束后恢复循环 GC；同时按 `yield_every` 调用 `time.sleep(0)` 让出执行，并通过进度回调记录进度。子进程模式也可用，失败时回退线程构建。此处只描述当前源码行为；本文其余性能数字和实施前建议仍是历史记录，不作当前性能保证或部署建议。

### 4.6 同步集成

`plugins/song_sync.py::_reload_matcher()`：

```python
matcher.request_rebuild(reason="song_sync")   # 不再同步 reload_index()
```

- 同步本身在 worker 线程写库，写完后 `-wal` mtime 变化，下一次启动自然失效。
- 实施前记录称当时插件处于禁用状态；这不是当前启用状态。当前 `_reload_matcher()` 会优先调用 `request_rebuild(reason="song_sync")`，兼容旧对象时才回退到 `reload_index()`。

---

## 5. 实施前代码改动清单（历史记录，非当前待办）

| 文件 | 改动 |
|---|---|
| `mohobot/music_knowledge/index_cache.py` | **新增**：指纹、紧凑格式 save/load、文件锁、清理旧缓存 |
| `mohobot/music_knowledge/fuzzy.py` | `LyricGramIndex` 增加 `to_compact()` / `from_compact()`（或模块级函数）；`match()` 若选方案 B 则改查询方式；更新过时 docstring（"5580 首 2.6s"） |
| `mohobot/music_knowledge/matcher.py` | `__init__` 非阻塞；`match()` / `_find_by_lyrics()` 改造；新增 `request_rebuild()` / `is_ready()`；快照原子替换 |
| `mohobot/models/config.py` | `music_knowledge` 是 `dict`，无需强类型；若要强校验可加子模型（可选） |
| `main.py` | 构造 `SongInfoMatcher` 时传入 `index_cache` / `build` 配置；启动日志区分"缓存命中/后台构建中" |
| `plugins/song_sync.py` | `_reload_matcher()` 改为 `request_rebuild()` |
| `config/global.yaml`（示例） | 增加 `music_knowledge.index_cache` 与 `build` 默认段 |
| `tests/test_song_index_cache.py` | **新增**，见第 8 节 |
| `docs/DEVELOPMENT.md` | 补充索引缓存与启动行为说明 |

### 5.1 配置示例

```yaml
music_knowledge:
  enabled: true
  song_database:
    db_folder: ./data/song_knowledge
    db_file: knowledge_db.db
  index_cache:
    enabled: true
    dir: ./data/cache/song_index
    keep: 2
  build:
    mode: thread          # thread | subprocess | sync
    yield_every: 500      # 仅 thread 模式
```

- 缺省值必须兼容：没有 `index_cache` 段时默认开启；`build.mode` 默认 `thread`。
- `enabled: false` → 完全回退到旧的同步构建。

---

## 6. 实施前边界情况与风险（历史方案，部署前需按当前实现复核）

1. **GIL/GC（实施前风险与当前实现）**：纯 Python thread 构建受 GIL 影响；当前源码会在构建期间用 `gc_control` 暂停循环 GC，并按 `yield_every` 让出执行。影响程度取决于数据、主机与配置；当时提出的性能结论不能替代当前环境实测。若需隔离 CPU 负载，可评估 `subprocess` 模式。
2. **峰值内存**：当时估算新旧快照短暂共存会增加内存压力；具体主机可用内存、swap、峰值和备份时间均为部署环境数据，需在获准环境重新核验。
3. **WAL 指纹**：只比对主库会漏掉同步写入；必须纳入 `-wal`。
4. **缓存损坏**：截断/校验失败 → 删除该文件并走重建，不能抛异常。
5. **多进程**：审核面板是独立进程但不使用 matcher；仍建议用 `.lock` 防止两个 mohobot 同时写缓存。
6. **格式/算法变更**：改序列化或归一化规则时递增 `INDEX_FORMAT_VERSION` / `NORMALIZE_VERSION`，否则旧缓存会把错误结果带进来。
7. **测试隔离**：`tests/` 用临时目录建库，缓存目录默认也要落到 tmp，不能污染仓库/生产。
8. **备份**：当时建议将缓存放在 `data/cache/song_index`，以匹配当时的备份排除规则；当前备份范围与传输量需重新核验。
9. **首次部署**：首次部署时无缓存可能触发全量构建；具体部署状态须按当前缓存检查。

---

## 7. 不在本次范围

- `_find_by_lyrics()` 对每条消息做 16k 首全表扫描的性能问题（独立优化项）。
- 24s 的模块导入优化（openai/fastapi 延迟导入）。
- 匹配算法、阈值、置信度的任何调整（本次要求结果不变）。

---

## 8. 实施前测试计划（历史记录；非当前验证结果）

新增 `tests/test_song_index_cache.py`：

1. **round-trip**：小库构建 → 保存 → 新 `SongInfoMatcher` 从缓存加载，对固定消息集（含书名号歌名、歌词片段、短歌词、错字）结果与直接构建**逐条一致**。
2. **指纹失效**：`os.utime` 改主库 → 指纹变化；只改 `-wal` → 指纹也要变；都不变 → 命中。
3. **损坏缓存**：截断/改字节 → 判 miss、自动重建、不抛异常。
4. **未就绪语义**：cache miss + 延迟构建时，`match()` 立即返回 `None`；`_find_by_lyrics()` 不得触发同步构建（用 mock 断言 `reload_index` 未被调用）。
5. **原子替换**：构建完成后旧引用仍可读、新引用生效（多线程循环 match 压测可选）。
6. **格式版本**：写一个旧 `INDEX_FORMAT_VERSION` 的文件 → 判 miss。
7. **兼容性**：`index_cache.enabled=false` 时行为与改动前一致。

实施前计划要求 `tests/test_song_knowledge.py`、`tests/test_song_sync.py` 通过；本文不提供这些测试在当前基点的执行结果。

---

## 9. 验收标准（2026-10-30 实施前目标记录）

以下为当时拟定的验收表，不表示这些检查在当前环境已执行或仍满足；生产数据规模和资源指标应在获准的隔离/部署环境重新测量。

- [ ] 有缓存冷启动：`SongInfoMatcher` 初始化目标 **≤ 10s**，服务重启总耗时目标 **≤ 40s**（当时对比的旧启动耗时仅作历史参考）。
- [ ] 无缓存首次启动：WS/面板在 **40s 内**起来，索引后台完成；期间 bot 可正常聊天，歌曲识别暂时为空。
- [ ] 缓存命中时 `match()` 抽样 200 条消息，结果与改动前**完全一致**。
- [ ] 缓存文件位于 `data/cache/song_index/`，p0 包体积无增长（约 67M 量级不变）。
- [ ] 常驻 RSS 不高于实施前基线（基线数值为当时观测，当前需重新测量；紧凑格式预期更低）。
- [ ] 启动/重建期间无 >1s 的事件循环阻塞日志（thread 模式下用 `yield_every` 观察）。
- [ ] `song_sync` 手动触发一次后：不阻塞主循环，完成后索引自动更新。

---

## 10. 实施时的回滚设想（历史记录；执行前须按当前配置与部署规范核验）

- 配置回退：`music_knowledge.index_cache.enabled: false` + `build.mode: sync`，行为等同当前版本。
- 缓存目录可直接删除（`rm -rf data/cache/song_index`），无副作用。
- 当时方案记录的代码回退基点为 `dac2e782`；当前回退须按实际发布历史与部署规范确认。

---

## 11. 实测基准参考（2026-10-30 历史记录；仅供理解当时测量方法）

以下示例曾指向特定部署目录。复用时应在获准的隔离环境替换为本地仓库根目录、虚拟环境与自制数据库，不要据此访问生产：

```bash
cd <仓库根目录>
# 1) 构建耗时
time .venv/bin/python -c "
import sqlite3; from mohobot.music_knowledge.fuzzy import LyricGramIndex
rows=sqlite3.connect('data/song_knowledge/knowledge_db.db').execute('select uuid,name,lyrics from songs')
g=LyricGramIndex(); g.build(rows.fetchall()); print(len(g._postings))
"
# 2) 缓存加载耗时（实现后）
time .venv/bin/python -c "
from mohobot.music_knowledge.index_cache import load_cache
load_cache('data/cache/song_index', '<fingerprint>')
"
# 3) 部署启动耗时：仅在获得授权、按部署规范完成预检后操作
# 不要从此历史示例直接重启服务。
```

参考基线：导入 24s / 歌名索引 2s / 歌词归一化 12s / 6-gram 构建 102s(GC off)～150s(GC on) / 其余 5s / bot 重连 2s。

---

## 12. 实施前执行顺序（历史记录）

以下是实施前的工作清单，仅保留为历史决策依据，不是当前待办。路径已泛化；基线 SHA 仅作历史参考。

1. 在本地仓库根目录（基线 `dac2e782`）建分支。
2. 先写 `index_cache.py` + 单元测试，用**小库**跑通 round-trip 与指纹逻辑。
3. 改 `fuzzy.py` 的紧凑序列化，benchmark 方案 A/B 的加载耗时，选定一个。
4. 改 `matcher.py` 为非阻塞 + 快照原子替换，修 `_find_by_lyrics`。
5. 改 `main.py` / `song_sync.py` 的接线与配置。
6. 全量测试；在生产库副本上跑一次真实构建 + 加载，记录数字。
7. 部署：先 `index_cache.enabled: false` 上线代码（行为不变），再打开开关重启，观察一次全量构建；第二次重启确认命中缓存。
8. 更新 `docs/DEVELOPMENT.md` 与 `fuzzy.py` 过时注释。
