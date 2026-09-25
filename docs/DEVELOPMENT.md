# Mohobot 开发指南

> 面向要改这个仓库的人。目标：读完能独立加插件、加配置项、加 LLM 工具、加 DB 字段，并且知道每处改动该动哪些文件。
>
> 基于 `main` 分支当前工作区（`__version__ = "0.1.0"`，约 3.7 万行 Python）整理。
> 文末「已知坑与不一致」一章是实际踩点记录，**建议先读那一章**。

---

## 0. 项目速览

一个进程同时服务多个 QQ 机器人的**多 Bot AI 框架**，基于 asyncio + OneBot v11 **反向 WebSocket**（框架当服务端，NapCat / LLOneBot / Lagrange 当客户端连进来）。

```
QQ 协议端(NapCat 等) ──反向WS──▶ WSServer ──▶ MessageHandler ──▶ 拦截器链 ──▶ LLM/插件
                                     │
                                     ├──▶ WebPanel  http://127.0.0.1:9090  (主管理面板)
                                     └──▶ review/   http://127.0.0.1:9091  (审核面板, 独立进程)
```

技术栈：Python 3.10+ / asyncio / websockets / FastAPI + SSE / SQLAlchemy(SQLite) / OpenAI SDK / loguru / Pillow。

三条数据原则（README 明确写的，改代码时必须守住）：

1. **原始数据不可变**：`data/history/` 是只读 JSONL 归档，**绝不允许**作为 LLM 实时输入。
2. **工作数据可变**：`data/contexts/` 是 LLM 实时推理用的记忆（JSON 数组）。
3. **历史入库**：完成的对话轮次另写 SQLite `conversations` 表。

代码分层：

| 目录 | 职责 |
|---|---|
| `main.py` | 装配容器 `MohobotApplication`：创建并互连所有组件 |
| `mohobot/ws_server.py` | 反向 WS 服务端 + 出站队列 |
| `mohobot/bot_manager.py` | Bot 实例生命周期、QQ 绑定、群内 bot 感知 |
| `mohobot/message_handler.py` | **消息处理管线（核心，1596 行）** |
| `mohobot/context_manager.py` | 会话上下文读写 + AI 总结压缩 |
| `mohobot/llm_service.py` | LLM 流式/工具/视觉/用量 |
| `mohobot/interceptors/` | 拦截器链：封禁、插件、内置命令、关键词 |
| `mohobot/services/` | 横切服务：用量、出站队列、TTS、任务监管、审计、LLM 工具注册表 |
| `mohobot/emotion/` | 情感系统（好感度/亲密度/关系阶段/长期记忆） |
| `mohobot/ban/` | 全局封禁名单 |
| `mohobot/music_knowledge/` | 歌曲知识库（SQLite 事实库 + 匹配 + VCPedia 同步） |
| `mohobot/db/` | 独立 SQLite（`conversations` 表） |
| `mohobot/web_panel/` | 主管理面板（FastAPI + 单文件前端 `static/index.html`） |
| `plugins/` | 动态加载插件 |
| `review/` | 聊天记录审核面板（半独立进程） |
| `tests/` | 自研 runner 的回归测试（非 pytest） |

---

## 1. 开发环境与日常循环

```bash
# 依赖
pip install -r requirements.txt

# 配置（global.yaml 已被 .gitignore 排除）
cp config/global.example.yaml config/global.yaml
# 填 LLM Key，或用环境变量：
export MOHOBOT_LLM_API_KEY="sk-xxx"
export MOHOBOT_VISION_API_KEY="sk-xxx"     # 可选

# WebUI 首次必须初始化密码（没有默认 admin/admin）
export MOHOBOT_WEB_PASSWORD='your-strong-password'

# 启动
python main.py
#   WS  监听 config/global.yaml → server.port（示例 8081；dataclass 默认 8060）
#   面板 http://127.0.0.1:9090 ｜ 审核面板 http://127.0.0.1:9091
```

### 回归测试（唯一的验收手段）

```bash
python tests/_run_all.py          # 全量，从仓库根执行
python tests/test_context_summary.py   # 单文件（多数测试自带 __main__）
python tests/test_concurrency.py  # 被 _run_all 排除，必须单独跑
python tests/_js_check.py         # WebUI JS 括号平衡检查（改 index.html 后跑）
python tests/smoke_startup.py     # 真实启动+关闭冒烟，用 tests/test_config.yaml
```

**当前基线：`45 passed, 0 failed`**（2026-09-25 实测，含新增的 `test_emotion_webui.py`）。改动后以此为对照。

runner 机制（`tests/_run_all.py`）：

- `glob("tests/test_*.py")`，跳过文件名含 `concurrency` 的。
- 每个**文件**是一个 pass/fail 单元：`exec_module` 导入 + 依次调用所有 `test_*` 函数（同步/异步都支持）。
- 文件内第一个失败的测试会**中断该文件剩余测试**，且没有逐用例隔离。
- `exec_module` 在 `try` 之外 —— **导入错误会直接中断整轮**。
- 退出码非 0 表示有失败，可直接用于 CI。

写新测试的约定：

```python
"""新功能回归测试: 一句话覆盖点。"""
import asyncio, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # 必需

from mohobot.context_manager import ContextManager
from mohobot.file_store import json_read


class FakeLLM:                       # 桩：按被测代码实际调用的签名实现
    async def chat(self, **kw):
        return ("桩回复", None)


async def test_feature_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:      # 必须隔离磁盘
        mgr = ContextManager(data_dir=td)
        await mgr.append_context("bot_001", "group", "g1", [
            {"role": "111-昵称", "content": "你好", "timestamp": 1},
            {"role": "assistant", "content": "你好呀", "timestamp": 2},
        ])
        ctx = await json_read(Path(td) / "contexts/bot_001/group/g1/main.json")
        assert len(ctx) == 2
    print("[1] 覆盖点 OK")


async def main() -> None:
    await test_feature_roundtrip()

if __name__ == "__main__":
    asyncio.run(main())
```

桩的惯用写法：每个测试文件内联定义 `FakeWS` / `FakeLLM` / `FakePlugins`（不共享 fixture 库）。`FakeWS` 需要提供 `send_to_bot(bot_id, action, params=None, wait_response=False, timeout=10.0)`、`send_group_msg`、`send_private_msg`，并记录调用。要测 `MessageHandler` 的私有方法时，惯用 `MessageHandler.__new__(MessageHandler)` + 手工绑定属性，避免构造重组件。

---

## 2. 启动装配：`main.py` 的 `MohobotApplication`

`startup()` 的顺序是有依赖的，加组件时要插进正确位置：

| 步骤 | 内容 |
|---|---|
| 1 | `GlobalConfig.load(config_path)`（路径来自 `MOHOBOT_CONFIG`，默认 `./config/global.yaml`） |
| 2 | 建数据目录：`data/{bots,history,contexts,cache/images}` + `plugins_dir` |
| 3 | `BotManager` → `migrate_legacy_bots()`；`ContextManager`；`UsageRecorder` |
| 3b | 歌曲知识库 `SongInfoMatcher`/`SongInfoService`（失败降级为 None） |
| 4 | `LLMService`；把 `summarize_context` 注入 ContextManager + `set_trim_config(...)` |
| 4b | 情感系统 `EmotionManager`（`emotion.enabled` 为真才建，失败降级） |
| 4c | `ImageCache`；TTS `TTSService`（`tts.enabled` + `base_url`，失败降级） |
| 5 | `PluginSystem` + 注入点设置 → `load_plugins()` |
| 6 | `DatabaseManager`（`database.enabled`）→ `MessageHandler(...)` |
| 7 | 拦截器：`BanStore`/`BanInterceptor`、`CommandHandler`、`KeywordFilter` |
| 8 | `WSServer` → 回填循环引用（`message_handler._ws`、`command_handler._ws`） |
| 8b | TTS 注入 ws + `start()`；插件 `set_runtime_refs(ws_server=...)` + `apply_injections()` |
| 9 | `ws_server.set_event_callback(message_handler.handle_event)` → `ws_server.start()` |
| 10 | `WebPanel` 创建 + 作为受监管任务启动 |
| 11 | 上下文周期压缩任务 `context-sweep` |
| 12 | `_maybe_start_review_panel()` 拉起审核面板独立进程 |

**拦截器链顺序（`main.py:241`）**，顺序本身是语义：

```python
interceptors = [ban_filter, plugin_system, command_handler, keyword_filter]
#               封禁静默丢弃 → 插件(含 /jrlp 等别名) → 内置命令(/help /sess) → 关键词兜底
```

**事件回调**：`WSServer._dispatch` 把事件作为**独立 task** 派发（`owner="events"`），慢处理器不会阻塞 WS 连接。

**优雅关闭**：`shutdown()` 顺序为 WebPanel → 插件 `on_shutdown` + 取消 owner `plugins` → 取消 `context-sweep` → 情感 flush → TTS stop → WS stop → LLM close → ImageCache close → MessageHandler.close()（刷文件 writer）→ UsageRecorder close → supervisor shutdown。

> **关键约束（`main.py:551-557` 有注释警告）**：`startup()` 和 `run_forever()` 必须共享**同一个 event loop**。曾用两次 `asyncio.run()` 导致服务器全部死亡。

**重启**：`WebPanel` 的 `POST /api/settings/restart` → 1 秒后调 `MohobotApplication.restart()` → **进程内** `shutdown()` + `startup()`。所以 `shutdown` 必须真的释放端口（面板先 `stop()` 再 await 其任务，超时则取消，否则重启会 `Address already in use`）。

---

## 3. 消息处理管线（`mohobot/message_handler.py`）

这是全项目最核心的文件。`handle_event` 是入口，按事件类型分流；`_handle_message` 是消息主路径。

### 3.1 `_handle_message` 执行顺序

1. **记录群内 bot 存在**（`note_group_message`）→ 全局指令去重与合并回复的依据。
2. **群聊最近消息缓冲**（`_note_group_recent`，内存 `_group_recent_msgs`，单条截断 80 字）。
3. **环境感知收集**（`plugin_system.collect_perception` → 缓存 `_perception_text`）。
4. **群聊多 bot 合并回复**（`_try_merged_group_reply`）：命中 `_MERGED_GROUP_TRIGGERS` 且群内多 bot 时，由随机选中的 bot 收集全部 bot 回复，发一条合并转发，其余 bot 静默；返回 `True` 即消费掉。
5. **插件观察钩子**（`plugin_system.dispatch_observed`）：**所有消息**（含未 @bot 的群消息）在 gate 之前先过一遍插件。返回 `(True, reply)` 即消费。
6. **群 gate**（`_should_respond_to_group`）—— 只在以下情况放行：
   - 文本以 `/` 开头（命令）
   - 文本 strip 后 lower == `ping`（全局，无需斜杠）
   - **直接 @ 本 bot 的 QQ**（不是 @all）
   - 引用的消息 id 属于**本 bot 自己发过的**（`BotInstance.is_my_message` 追踪）
7. **全局指令去重**（`_should_defer_global_command`）：命中全局指令且群内多 bot 时，只有随机抽签选中的 bot 继续。
8. **私聊图片限流**（`_check_image_rate_limit`，10 秒冷却，超限剥掉 image 段只留文本）。
9. **图片引用归一化**（`_normalize_image_segments`）：NapCat 群图常只有 `file` 没有 `url`，经 `get_image` API 换 base64 → data URI。
10. **拦截器链**：`for interceptor in self._interceptors: handled, response = await interceptor.intercept(...)`；`handled=True` 则发回复并 return。
11. **ping/PONG**：剥空白后完全匹配 → 回 `PONG`。
12. **LLM 路径**：
    - 解析引用消息（`_resolve_quoted_display`，走 `get_msg` API，TTL 缓存）→ 作为 `system` 块注入
    - `_build_legacy_context`：加载 context + 追加**临时** `system` 块（群聊最近消息 / 环境感知 / 情感状态），**这些块不写回文件、不参与总结**
    - `_stream_llm_reply` → 流式接收 + 分段发送（见 3.3）
    - 成功且有内容 → `append_context`（写 `user` + `assistant` 两条）+ `_persist_legacy_turn`（写 SQLite 两行）+ `emotion.schedule_turn`

### 3.2 消息内容渲染规则

`_render_message_content(bot_id, message, describe_missing=...)` 把消息段渲染成给 LLM/上下文看的文本：

- 纯文本段：原文拼接。
- `reply` / `at` 段：**不进内容**（reply 只用于取 id，at 不入文）。
- `image` 段：有 `summary`（NapCat 表情包，如 `[动画表情]`）直接用；否则占位 `[图片]`，**第一张照片**会尝试替换为 `[图片]（概要：…）`。
- 其他段走 `_SEG_PLACEHOLDERS`：`[语音]` `[视频]` `[表情]` `[合并转发]` `[卡片消息]` `[文件]`。
- `describe_missing=True`（引用消息场景）会缓存未命中时**现调视觉**；`False`（上下文存储）只读缓存。

> 铁律：**绝不把段列表的 repr 存进上下文或送进 LLM**（归一化后含 base64 data URI，会爆 prompt）。

### 3.3 分段发送算法（`_flush_ready_segments`）

「标点符号 + 长度分隔法」，优先级从高到低：

0. **长引文保护**：`“…”` 内长度 ≥ 16（`_QUOTE_MIN_LEN`）视为完整语段（歌词/台词）整体单独成段；引文**未闭合**（流式中途）时绝不在引文中间切分。引文前的文字按 `_QUOTE_PREFIX_BOUNDARY`（含冒号/破折号）找边界。
1. **双换行 `\n\n`** —— 段落分隔，**无条件切**（无最小长度要求）。单个 `\n` 不是分隔。
2. **硬上限** `_seg_max_len`（默认 60）：超限强制切，尽量切在最后一个标点。
3. **软下限** `_seg_min_len`（默认 12）：达到最小长度后可在最后一个强标点（`。！？!?…`）或弱标点（`；;，,、`）处切。

发送时：首段可按 `reply_quote` 引用触发消息，后续段之间 `random.uniform(segment_delay_min, segment_delay_max)` 随机延迟。配置全在 `ReplyConfig`。

### 3.4 其他管线行为

- **合并转发**：群聊纯文本回复 ≥ `_forward_min_len`（600 字）自动改合并转发，每块 1000 字（`_forward_chunk_chars`），失败回退普通发送。
- **工具结果泄漏防御**：`_sanitize_tool_leak` —— 整条以 `[工具` 开头则丢弃；含 `"\n[工具调用: "` 则截断。
- **私聊自动回复过滤**（`ignore_auto_reply`，默认开）：`_handle_message` 最开头（拦截器链/插件观察钩子之前）对私聊消息判定，命中即静默丢弃（归档保留）。判定规则写死：① 文本以 `[自动回复]` 开头；② 同一 (bot_id, user_id) 连续 3 条相同文本且相邻间隔 < 5 分钟（内存 `_repeat_state`，超窗/换文本重置）。QQ 自动回复无统一结构化标记（有的带前缀、有的是 QQ 预设纯文案如"我在线的，马上回消息"），生产实测见 `docs/DEVELOPMENT.md` 本节备注。
- **戳一戳**：`notice_type=notify, sub_type=poke` 且 target 是本 bot → 从 `touch_replies` 随机取一条回复。优先级：bot 私有 > 全局 > 内置 `DEFAULT_TOUCH_REPLIES`。
- **request 事件**：交给插件 `on_request`；插件不接管则**静默不处理**（不自动同意好友/入群）。
- **上下文写的 role 是 `"QQ号-昵称"`**（`_speaker_role`），LLM 靠这个知道「谁说的」；`LLMService._build_messages` 把非 `user`/`assistant`/`system`/`summary` 的角色转成 `user` 并加 `[role]: ` 前缀。
- **`summary` 角色** → 转成 `system` 的「【较早对话总结】」块。

---

## 4. 数据层与持久化铁律

### 4.1 `file_store.py` —— 所有新 I/O 必须走这里

```python
from mohobot.file_store import json_read, json_write, json_update, JSONLWriter, ensure_dir

data = await json_read(path)                      # 缺失/空 → None
await json_write(path, data, pretty=True)         # 整体覆盖
new = await json_update(path, fn, default=[])     # 原子读-改-写（同一把锁内）
```

- 模块级 `_file_locks: {绝对路径: asyncio.Lock}`，按路径互斥。
- **`json_update` 是多人/多协程改同一文件时的唯一正确选择** —— 它把 read→mutate→write 全放在锁内，避免丢失更新（ContextManager 的 `append_context`、BanStore、插件配置都靠它）。
- `JSONLWriter` 是追加式写入器，每次 append 刷盘；MessageHandler 为每个 history 文件缓存一个 writer，关闭时统一 `close()`。
- ⚠️ **`json_write` 不是 temp+rename 原子写**：它是 `open("w")` 就地截断。进程在写到一半崩溃会留下半截文件（`json_update` 会把损坏文件当 `default` 处理）。

### 4.2 会话上下文布局与文档结构

```
data/contexts/{bot_id}/
├─ group/{group_id}/main.json                  # 群聊固定单会话
│                    session_index.json
└─ private/{user_id}/sess_main.json | sess_001.json | ...
                     session_index.json        # {"sessions":[{id,name,created}], "active":"..."}
```

群聊固定 `main.json`；私聊一个用户可有多个会话，由 `session_index.json` 索引。

context 文件是**扁平 JSON 数组**，元素形如：

```json
{"role": "3831097597-墨染荷韵", "content": "你好", "timestamp": 1730000000}
{"role": "assistant", "content": "你好呀", "timestamp": 1730000001}
{"role": "summary",   "content": "【总结块】…", "timestamp": 1730000002}
```

**轮数计算**（`_count_rounds`）：`summary` 记 **1.0** 轮，其余每条记 **0.5** 轮，求和后 `ceil`。所以「一轮」= 一条 user + 一条 assistant。

### 4.3 上下文压缩（三种触发）

配置项全在 `GlobalConfig` 顶层：

```yaml
context_summary_enabled: true                 # 总开关
context_trim_at_rounds: 40                    # 满多少轮触发
context_trim_remove_rounds: 15                # 每次最早期多少轮交给 AI 总结
context_summary_age_hours: 3                  # 「旧对话」判定年龄
context_summary_sweep_enabled: true           # 周期时间压缩
context_summary_sweep_interval_minutes: 30
context_summary_min_interval_hours: 24        # 已压缩会话再次周期压缩的最小间隔
```

1. **满轮压缩**：`append_context` 后重读，轮数 ≥ `trim_at_rounds` → `_compact`。把最早的 `remove_rounds` 轮交给 `LLMService.summarize_context`，总结作为**唯一一个** `role="summary"` 块插到最前。总结块视为 1 轮，**可被再次嵌套总结**。
2. **满轮时顺带时间压缩**：`_head_for_compaction` 取「最早 N 轮」与「超龄前缀」两者中**更长**的那个（`_old_prefix_len` 从头扫描 `timestamp < now - age_hours` 的连续前缀，最多包含开头那一个 summary 块；**必须至少含一条非 summary 旧消息**才会返回非 0，避免孤立 summary 被反复总结）。
3. **周期扫描**：`main.py:_context_sweep_loop` 每 `sweep_interval_minutes` 调 `sweep_all_sessions()`，遍历所有 `private/{活跃会话}` 与 `group/main`。已压缩且未超轮数的会话受 `min_interval_hours` 日闸限制。

**失败语义差异（重要）**：`trim_on_failure=True`（满轮路径）总结失败也照裁，防止无限增长；`trim_on_failure=False`（周期路径）失败**保留数据**待下轮重试。

**并发保护**：`_merge` 在锁内重读，只有当前头部仍等于捕获时的头部（`_same_entries`，JSON 排序键比对）才写回，否则放弃。所以压缩不会覆盖并发 append 的新消息。

### 4.4 SQLite `conversations` 表

`schema`（`mohobot/db/sql_database.py`）：

```python
uuid (PK)  user_id  character_id  timestamp  source  type  content
speaker_id  speaker_nickname  meta_data
```

- `character_id` = `bot_id`；群聊行 `user_id` = **群号**，真实发言人放在 `speaker_id`/`speaker_nickname`。
- 唯一生产写入点：`message_handler._persist_legacy_turn`，每完成一轮写 **2 行**（`source="user"` / `"agent"`）。
- 连接参数：WAL + `synchronous=NORMAL` + `busy_timeout=15000`。

**加一列的固定套路**（增量迁移，不重建表）：

1. 在 `sql_database.py` 的模型加 `Column`。
2. 在 `_migrate_sqlite_schema` 加守卫：

```python
cols = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(conversations)").fetchall()}
if "new_col" not in cols:
    connection.exec_driver_sql("ALTER TABLE conversations ADD COLUMN new_col VARCHAR")
connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_conversations_new_col ON conversations (new_col)")
```

歌曲库同理，走 `song_database.py` 的 `_SONG_MIGRATION_COLUMNS` + `migrate_song_schema`。

### 4.5 其他存储位置

| 位置 | 内容 |
|---|---|
| `data/history/group/{群号}.jsonl` | 群聊合并归档（跨 bot 共享一个文件，写入时按 message_id 近期窗口去重，行内 `bot_id` 标注接收/发送 bot） |
| `data/history/{bot_id}/private/{QQ}.jsonl` | 私聊原始事件归档（不同 bot 与同一用户的私聊是不同对话，不合并） |
| `data/bots/{bot_id}/config.json` | per-bot 配置 |
| `data/cache/images/` + `data/cache/image_cache_map.json` | 图片缓存 + phash 映射 |
| `data/ban/{ban_list,banall_list,pass_list,passall_list}.json` | 封禁名单 |
| `data/emotion/{bot_id}/{user_states,memory}.json` | 情感状态与长期记忆 |
| `data/stats/llm_usage.jsonl` | 用量流水 |
| `data/audit/web_admin.jsonl` | 管理面板变更审计 |
| `data/plugins_config/{plugin}.json` | 插件配置（全局一份） |
| `data/plugins_state.json` | 插件启停状态 |
| `data/plugins_data/{plugin}/…` | 插件私有数据 |
| `review/data/review.db` | 审核面板自己的库 |

> `data/` 与 `logs/` **完全未纳入版本控制**（`git ls-files data` 为空）。

### 4.6 history 群聊合并布局（双写过渡期）

群聊消息曾经每 bot 单独存一份（`history/{bot_id}/group/{群号}.jsonl`），同一条群消息多只 bot 都会收到 → N 份重复。现改为**合并存储 + 写入去重**：

- **写入**（两处）：`MessageHandler._archive_event`（收到的消息）写 `history/group/{群号}.jsonl`，行内注入 `bot_id`（接收 bot）；`WSServer._write_archive_line`（`message_sent`）写同一文件，行内 `bot_id`（发送 bot）。**私聊仍按 bot 分目录**，路径不变。
- **去重**：`MessageHandler._claim_group_mid` —— 每群一个 `OrderedDict` 窗口（`_GROUP_MID_WINDOW = 2048`），重复 mid 直接跳过合并写入。检查与登记之间无 await，天然互斥。窗口仅存内存：重启后理论上可能重复写一次，消费方（review loader 的 `seen_mids`）读侧兜底去重。
- **`history_dual_write`（默认 true）**：开启时群消息/发言**额外**按旧布局 `history/{bot_id}/group/` 归档一份（不去重、内容与旧代码完全一致，回滚保险）。确认新布局稳定后在 WebUI 关闭（热生效），下个版本删旧写入代码。
- **存量迁移**：`python scripts/migrate_history_layout.py`（**停机运行**）把旧 per-bot 群文件（含遗留 QQ 号目录）按 mid 去重合并进新布局、补 `bot_id`、按时间排序，旧文件移入 `data/history_legacy_{时间戳}/` 备份。幂等，可重复执行。
- **读侧适配**：review loader（群会话单文件 + 按行内 bot_id/@/引用做归属过滤，`_CACHE_VERSION` 已 bump 作废旧 sidecar）、chat_manager `history_path`（群路径不含 bot_id）、web_panel `_count_sync`（group 目录单层遍历）。备份/恢复/清理按 bot 筛选时不含群合并数据（共享，不拆分）。

---

## 5. 配置系统

### 5.1 结构

`GlobalConfig` 是纯 dataclass（**不是 pydantic**），`load()` 用 PyYAML + 逐字段 `raw.get(key, default)`。

| 顶层段 | 关键字段与默认 |
|---|---|
| `admins: list[int]` | 全局管理员 QQ（封禁系统与插件共用；回退旧 `ban.admins`） |
| `server` | `host="0.0.0.0"`, `port=8060`, `max_size=10MB`, `outbound_interval=0.5`, `outbound_maxsize=100`, `outbound_enqueue_timeout=2.0` |
| `llm` | chat/vision/emotion 三组 model+base_url+api_key+温度+max_tokens；`models: list[str]` 供 WebUI 下拉 |
| `web_panel` | `enabled/host/port=9090/username/password_hash` |
| `interceptor` | `keyword_file="./data/keywords.json"` |
| `reply` | `stream/segment_reply/segment_min_len=12/segment_max_len=60/segment_delay_min=0.2/segment_delay_max=0.5/reply_quote` |
| `database` | `enabled/folder="./data/database"/file="luotianyi.db"`（示例 yaml 写 `mohobot.db`） |
| `anysearch` | `enabled/api_key/base_url/timeout=30` |
| `ban` | `enabled` |
| `emotion` | `enabled=False/smart_update/force_update_interval=10/significance_threshold=5/favour_*/intimacy_*` |
| `tts` | `enabled=False/base_url/api_key/model/voice_id/speed/vol/pitch/sample_rate/bitrate/format/queue_maxsize=16/timeout/tts_prompt_template/cmd_max_chars=30/cmd_cooldown=120` |
| `music_knowledge: dict` | 无类型字典，不经 WebUI |
| 顶层标量 | `touch_replies`, `log_dir`, `data_dir`, `plugins_dir`, `context_summary_*` 系列, `group_recent_msgs_count`, `ignore_auto_reply` |

`BotConfig`（`data/bots/{bot_id}/config.json`）：`bot_id / qq / nickname / persona / enabled / touch_replies / chat_model_override / vision_model_override / tts_enabled / tts_voice_id / command_prefix / keyword_replies`。

**bot_id 与 QQ 分离**：`bot_id` 是自动编号内部标识（`bot_001`…，`next_bot_id` 取最大号 +1，零填充 3 位）；`qq=0` 表示未绑定；**QQ 唯一绑定**（`bind_qq` 会先从其他 bot 解绑）。新 QQ 连进来默认不分配 bot，需在面板创建/绑定。

### 5.2 加一个配置项的完整清单

以顶层标量（参照 `group_recent_msgs_count`）为例，**8 处**：

| # | 文件 | 改什么 |
|---|---|---|
| 1 | `mohobot/models/config.py` | dataclass 加字段：`group_recent_msgs_count: int = 10` |
| 2 | 同文件 `load()` | `group_recent_msgs_count=int(raw.get("group_recent_msgs_count", 10)),` |
| 3 | 同文件 `save()` | `"group_recent_msgs_count": self.group_recent_msgs_count,` |
| 4 | 同文件 `to_dict()` | 同上（与 `save()` 是两份重复字典，都要加） |
| 5 | `mohobot/web_panel/app.py` `PUT /api/config` | 顶层标量是**显式白名单**，必须把 key 加进那个 `for key in (...)` 元组 |
| 6 | `mohobot/web_panel/static/index.html` | `loadConfigPage()` 加 `formField(...)` 渲染；`saveGlobalConfig()` 加取值发送 |
| 7 | `config/global.example.yaml` | 加带注释的模板项（仅文档作用） |
| 8 | 热生效钩子（可选） | `update_config` 里 `cfg.save()` 之后调对应 `sync_config`/`set_*_config` |

**不同形态的差异**：

- **嵌套段字段**（如 `reply.stream`）：跳过第 5 步 —— `PUT /api/config` 对 `reply/ban/emotion/tts/interceptor/web_panel` 走通用 `hasattr` 循环。
- **`llm.*`**：不经 `/api/config`，走独立端点 `PUT /api/models`。
- **per-bot 字段**：改 `BotConfig` 的 dataclass / `load` / `to_dict` 三处即可，`update_bot_config` 是通用 `hasattr` 循环，**无需**白名单；前端加在 `loadBotConfig()` / `saveBotConfig()`。
- **`server` / `database` / `log_dir` / `data_dir` / `plugins_dir`**：故意不给 WebUI 编辑（服务端路径），`GET /api/config` 会 `pop("server")`。

### 5.3 环境变量

**没有通用的 env→config 合并**，全是在使用点读，且 **YAML 值优先**（`config_value or os.environ[...]`）。

| 变量 | 作用 |
|---|---|
| `MOHOBOT_CONFIG` | 指定 global.yaml 路径 |
| `MOHOBOT_LLM_API_KEY` | 回退 `llm.chat_api_key` |
| `MOHOBOT_VISION_API_KEY` | 回退 `llm.vision_api_key`（再回退 chat key） |
| `MOHOBOT_EMOTION_API_KEY` | 回退 `llm.emotion_api_key` |
| `MOHOBOT_MINIMAX_API_KEY` | 回退 `tts.api_key`（注意不是 `MOHOBOT_TTS_API_KEY`） |
| `MOHOBOT_WEB_PASSWORD` | **仅**在 `password_hash` 为空时初始化，随后哈希落盘 |
| `MOHOBOT_TEST_CONFIG` | 测试用 |

---

## 6. 插件开发

### 6.1 形态与加载

- **单文件插件**：`plugins/xxx.py` → 插件名 `xxx`。
- **目录插件**：`plugins/xxx/main.py` → 插件名 `xxx`（目录里**必须有 `main.py`**，否则整个目录被跳过）。
- 跳过规则：以 `_` 开头、`__pycache__`、没有 `main.py` 的目录。
  > 现状：`plugins/feed/` 是只剩 `__pycache__` 的空目录（已删插件的残留），加载器会静默跳过。
- 导入方式：`importlib.util.spec_from_file_location(f"mohobot_plugin_{name}", ...)`，**不注册进 `sys.modules`**；类必须是该模块内定义的、字面名为 `Plugin` 的类。
- 目录插件会把自己的目录插进 `sys.path`，所以 `main.py` 里可以 `import xxx_core`（wifepicker / relationship / qzone 都这样组织）。
- **热重载**：`reload_plugins()` = `shutdown_plugins()` + 清空 + 重新 `load_plugins()`（内含 `apply_injections` / `startup_plugins` / `start_tick_tasks`）。**没有文件监听 watchdog**，由面板按钮或 `set_enabled` 触发。
- **启停状态**：`data/plugins_state.json`，格式 `{plugin_name: {"enabled": bool}}`，缺省为启用。

### 6.2 钩子契约全表

| 钩子 | 签名 | 返回 | 派发点 |
|---|---|---|---|
| `on_message` | `async (self, bot_id, event, raw)` | `(handled: bool, reply: str\|list\|None)` | 拦截器链 |
| `on_message_observed` | `async (self, bot_id, event, raw)` | `(handled, reply)` | gate **之前**，所有消息 |
| `on_notice` | `async (self, bot_id, event, raw)` | 忽略 | notice 事件 |
| `on_meta` | `async (self, bot_id, event, raw)` | 忽略 | meta 事件 |
| `on_request` | `async (self, bot_id, event, raw)` | `bool`（True = 已接管） | 好友/入群申请 |
| `on_perception` | `async (self, bot_id, event, raw)` | `str`（多插件用 `\n` 拼） | 注入 LLM 请求，**不落盘** |
| `on_config_update` | `(self, config: dict) -> None` | 忽略 | 面板保存插件配置后 |
| `on_startup` | `(self)` 无参，sync/async 均可 | 忽略 | 插件加载完 |
| `on_shutdown` | `(self)` 无参，sync/async 均可 | 忽略 | 关闭时（逆序） |
| `on_tick` | `(self)` 无参，sync/async 均可 | 忽略 | 周期任务（需配合 `interval_sec`） |

`handled=True` 的含义：**管线停止**，`response` 由框架发送（经 `_send_reply`，带长文本自动合并转发与工具泄漏防御）。插件自己发图就返回 `(True, None)`。

⚠️ **`on_config_update` 是同步调用的**（`plugin_system.py:531-536` 直接 `updater(merged)`，不 await）。写成 `async def` 会导致协程被丢弃、代码根本不执行。

**周期任务**：类上同时提供 `on_tick` 和 `interval_sec` 才会启动；`interval_sec` 可以是 `@property`，每轮重读（配置热生效）。任务以 `owner="plugins"` 注册进 supervisor。需要自己的后台任务时用注入的 supervisor：

```python
self._daily_task = self._task_supervisor.create_task(
    coro, name="my-task", owner="plugins"
) if self._task_supervisor else asyncio.create_task(coro)
```

### 6.3 类属性（框架读取）

| 属性 | 作用 |
|---|---|
| `global_triggers: set/list/tuple` | **只控制群内多 bot 去重**，不注册命令。精确匹配或「命令 + 空格 + 参数」。 |
| `no_prefix_triggers: set` | 无需 `/` 前缀的精确整词触发（在 `dispatch_observed` 内处理）。若同时也在 `global_triggers`，群内同样去重。 |
| `bind_bots: list[str]` | 限定只在指定 `bot_id` 上派发（插件仍加载、`on_tick` 仍跑）。 |
| `info: dict` | `/help` 与面板的描述元数据，如 `{"commands":[{"name","desc","admin"}]}`。类 docstring 自动成为 `description`。 |
| `interval_sec` | 配合 `on_tick`。 |

**群内多 bot 去重机制**：`message_handler._should_defer_global_command` 收集内置 `{"/help","/tts"}` + 所有插件的 `global_triggers` + 前缀集 `("/ban","/pass","/dec-")`，命中后调 `BotManager.pick_bot_for_group(group_id, message_id)` 抽签 —— 抽签结果按 `(群号, message_id)` 缓存 60 秒，所以同一条消息触发的多个 bot 协程得到**同一个**结果，不会出现都回或都不回。

### 6.4 注入 API

必须在 `Plugin` 类上定义 **`@classmethod`**（框架查的是 `inst.__class__`，且只传一个位置参数）：

```python
@classmethod
def inject_ws_server(cls, ws_server) -> None: ...
@classmethod
def inject_bot_manager(cls, bot_manager) -> None: ...
@classmethod
def inject_data_dir(cls, data_dir) -> None: ...          # 总是调用
@classmethod
def inject_anysearch_client(cls, client) -> None: ...     # 配了 anysearch 才调
@classmethod
def inject_llm_service(cls, llm_service) -> None: ...
@classmethod
def inject_task_supervisor(cls, supervisor) -> None: ...
@classmethod
def inject_song_matcher(cls, matcher) -> None: ...
@classmethod
def inject_admin_ids(cls, admin_ids) -> None: ...          # list[str]，注意是字符串
```

**插件配置**注入方式不同：schema 有默认值时框架用 `object.__setattr__` 设实例属性 **`plugin_config`**（不是 `self.config`，也没有 `get_config()`）。

### 6.5 `_conf_schema.json`

放在**插件目录**下。⚠️ **单文件插件拿不到自己的 schema** —— schema 路径取 `entry_path.parent`，对单文件插件就是 `plugins/` 本身（所有单文件插件会共用 `plugins/_conf_schema.json`）。**要 per-plugin 配置就必须用目录形态。**

字段 spec：

```json
{
  "my_int": {
    "type": "int",                       // string|text|int|float|bool|list|object
    "default": 20,
    "description": "面板标签",
    "hint": "面板灰字说明",
    "slider": {"min": 1, "max": 100, "step": 1},   // 仅 UI
    "options": ["a", "b"],                          // 仅 UI（字符串枚举）
    "invisible": true                               // 仅 UI（隐藏）
  },
  "my_obj": {
    "type": "object",
    "items": { "child": {"type": "bool", "default": true} }   // object 走嵌套
  }
}
```

加载器行为：`object` 递归 `items`；`list → []`；`bool → False`；`int → 0`；`float → 0.0`；**其余（含 `text`）→ `""`**（除非给了 `default`）。保存时会做类型强转（`list` 接受 JSON 数组或逗号分隔字符串；`bool` 接受 `"true"/"1"/"yes"/"on"`），合并是 schema 默认值深合并提交值，写入 `data/plugins_config/{name}.json`（首次运行会把默认值落盘，便于面板编辑）。

### 6.6 发消息 / 调 OneBot

```python
# 通用出口：任何 OneBot action
resp = await ws.send_to_bot(bot_id, "get_group_info", {"group_id": gid},
                            wait_response=True, timeout=5.0)   # 返回 {"status":"ok","retcode":0,"data":{...}}

await ws.send_group_msg(bot_id, group_id, "文本或段列表")
await ws.send_private_msg(bot_id, user_id, "…")
await ws.send_image(bot_id, chat_type, chat_id, img_path)   # 内部转 base64://，NapCat 无需读本地路径
await ws.send_group_forward_msg(bot_id, group_id, nodes)
```

两种产出方式：**返回** `(True, "文本")` 让管线发（推荐，长文本自动合并转发）；或**自己发**（发图、延迟、主动推送）。

### 6.7 完整插件模板

```
plugins/myplugin/
├── main.py
├── _conf_schema.json          # 可选；目录形态才有意义
└── myplugin_core/__init__.py  # 可选子模块（main.py 里 sys.path 已含插件目录）
```

```python
"""一句话摘要（面板显示）。"""

from __future__ import annotations

import sys, os
from pathlib import Path
from typing import Any

from loguru import logger

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
# from myplugin_core.core import do_something


class Plugin:
    """类 docstring → 面板/help 描述。"""

    # ── 框架读取的类属性 ──
    global_triggers = {"/mycmd"}          # 群内多 bot 去重（不注册命令）
    # no_prefix_triggers = {"mykeyword"}
    # bind_bots = ["bot_001"]
    # interval_sec = 60                   # 配合 on_tick

    info = {"commands": [
        {"name": "mycmd", "desc": "做某事（/mycmd <参数>）"},
        {"name": "myadmin", "desc": "管理员命令", "admin": True},
    ]}

    # ── 注入（必须 classmethod）──
    _ws_server = None
    _data_dir = "./data"
    _admin_ids: list[str] = []
    _task_supervisor = None

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    @classmethod
    def inject_data_dir(cls, data_dir: str) -> None:
        cls._data_dir = data_dir

    @classmethod
    def inject_admin_ids(cls, admin_ids) -> None:
        cls._admin_ids = [str(a) for a in (admin_ids or [])]

    @classmethod
    def inject_task_supervisor(cls, supervisor) -> None:
        cls._task_supervisor = supervisor

    # ── 配置：schema 有默认值时框架注入 plugin_config ──
    _DEFAULTS = {"enabled": True, "limit": 1}

    def __init__(self):
        self.plugin_config: dict = dict(self._DEFAULTS)

    def _cfg(self, key: str, default):
        value = (getattr(self, "plugin_config", None) or {}).get(key, default)
        return value if value not in (None, "") else default

    def _is_admin(self, event) -> bool:
        return str(getattr(event, "user_id", "") or "") in set(self._admin_ids)

    @staticmethod
    def _text(event) -> str:
        msg = getattr(event, "message", "")
        if isinstance(msg, str):
            return msg.strip()
        return "".join(
            seg.get("data", {}).get("text", "")
            for seg in (msg or []) if isinstance(seg, dict) and seg.get("type") == "text"
        ).strip()

    # ── 钩子 ──

    async def on_message(self, bot_id: str, event: Any, raw_event: dict[str, Any]):
        """/ 前缀命令。返回 (True, 回复) 即终止管线。"""
        text = self._text(event)
        if not (text == "/mycmd" or text.startswith("/mycmd ")):
            return (False, None)
        if text.startswith("/myadmin") and not self._is_admin(event):
            return (True, "❌ 你没有权限。")
        return (True, f"echo: {text[len('/mycmd'):].strip()}")

    async def on_message_observed(self, bot_id, event, raw):
        """每条消息（含未 @bot 的群消息）在 gate 前调用。
        不要消费无关消息 —— 一律 return (False, None) 放行。"""
        return (False, None)

    async def on_notice(self, bot_id, event, raw) -> None: return None
    async def on_meta(self, bot_id, event, raw) -> None: return None
    async def on_request(self, bot_id, event, raw) -> bool: return False
    async def on_perception(self, bot_id, event, raw) -> str: return ""

    def on_config_update(self, config: dict) -> None:      # 同步！不要写 async
        self.plugin_config = config or {}

    async def on_shutdown(self) -> None:
        pass      # 释放资源写这里
```

### 6.8 插件侧边习惯

- **管理员判定**：`str(event.user_id) in set(self._admin_ids)`（无框架 helper）。
- **插件私有数据**：`data/plugins_data/{name}/…`，用 `file_store.json_update`。
- **异常是被吞掉的**：每个插件单独 `try/except` 并 log，一个插件抛错不影响其他插件 —— 所以出错只会看到日志，不会看到崩溃。

---

## 7. LLM 服务

### 7.1 客户端与模型

构造三个 `AsyncOpenAI`：chat / vision / emotion。key 回退链是 `yaml → env → chat key`；vision 与 emotion 若与 chat key 相同则**复用 chat client**。

`chat_model` 可被 `BotConfig.chat_model_override` 覆盖。图片**不再**让主模型切 vision 模型：`_build_messages` 先调 vision 把图描述成文本注入，主模型只看到文本。

`max_tokens` 会被 `min(cfg, 131072)` 夹紧（过大的值在某些网关返回空流）。

### 7.2 工具调用

内置工具三个（`llm_service.__init__` 里硬编码 schema）：

| 工具 | 状态 |
|---|---|
| `get_current_time` | 正常（UTC+8 格式化） |
| `get_group_member_info` | **永远是错误桩**，却仍向模型广告 |
| `anysearch_search` | 正常；未配 key 时从 schema 列表里**移除** |

插件工具经 `mohobot/services/llm_tools.py` 的**进程内 `registry`** 注册：

```python
from mohobot.services.llm_tools import LLMTool, registry, tool_schema

registry.register(LLMTool(
    tool_schema("song_search", "描述", {"query": {"type": "string"}}, ["query"]),
    song_search,          # sync/async 均可，参数按 schema 关键字传入
))
```

`registry` 在 `llm_tools.py` import 时通过 `load_plugin_tools()` 主动 import `plugins.song_tools` 来填充。`_current_tools_schemas()` 每次调用重读 registry（热重载插件工具可见）。

⚠️ **加工具的致命陷阱**：`_execute_tool` 只把**名字以 `song_` 开头**的调用路由到 registry：

```python
if func_name.startswith("song_"):
    return await registry.execute(func_name, args)
```

其他名字的插件工具会被**广告给模型但执行时返回 `未知工具`**。所以要加新工具，必须：

1. 在插件里 `registry.register(...)`；
2. 保证该模块在启动时被 import（`llm_tools.load_plugin_tools()` 目前只 import `plugins.song_tools`，需要在那里补 import）；
3. **修改 `_execute_tool`**：把前缀判断改成「先查 registry，未命中再走内置分支」，或者接受 `song_` 前缀这一临时约定。

（注意 `_execute_tool` 里 `startswith("song_")` 的分支**重复写了两次**，第二个是死代码 —— 历史遗留。）

工具循环上限 4 轮（`max_tool_rounds`），最后一轮不再提供工具以逼出文本；若仍空则非流式重试一次。

### 7.3 流式与降级

`chat_stream` 是 async generator，yield `(chunk, is_final)`。

- 首次调用 `stream=True, stream_options={"include_usage": True}`，`tool_choice="auto"`。
- 无重试：初始调用异常直接 `[LLM 调用失败: ...]`（`chat()` 同样，返回错误串作为回复内容）。
- 工具后续流没产出内容 → 非流式重试一次；仍空 → `[工具调用完成，但模型未返回文本]`。
- 视觉 `describe_image*` 失败降级为空串。
- `analyze_emotion` 失败返回 `None`；`EmotionExpert` 外面有超时 + 重试 + **连续 3 次失败熔断**。

### 7.4 用量 `UsageRecorder`

流水文件 `data/stats/llm_usage.jsonl`，一行一次请求：

```json
{"time":…, "request_id":…, "bot_id":…, "module":"chat|summary|emotion|vision|tts",
 "kind":"chat|stream|tool_follow_up|vision|summary|emotion|tts",
 "provider":"openai-compatible", "model":…, "chat_type":…, "chat_id":…, "user_id":…,
 "prompt_tokens":…, "completion_tokens":…, "total_tokens":…, "cached_tokens":…, "chars":…}
```

- `chars` 是 TTS 计费字符（此时 token 字段全 0）。
- 缓存命中：优先 OpenAI `prompt_tokens_details.cached_tokens`，否则 DeepSeek `prompt_cache_hit_tokens`。
- **没有金额成本模型**，只有 token / 字符计数。
- 聚合接口：`get_session_usage_stats`（按会话）、`get_user_usage_stats`（按用户）、`get_module_usage_stats`（按 bot×module）、`get_usage_stats`（全局）。区间语法 `today|7d|30d|Nd|Nh`（`\d{1,4}[dh]`）。整个 JSONL 全量解析 + 60 秒 TTL 缓存。

### 7.5 总结压缩接口

`LLMService.summarize_context(entries) -> str | None`：prompt 要求「2-4 句全局概要 + 最多 5 条重点轮次浓缩，≤800 字，不要 markdown」；失败返回 `None`，由调用方决定降级（见 4.3）。

---

## 8. 各子系统速查

### 8.1 `TaskSupervisor` —— 所有后台任务的注册处

```python
supervisor.create_task(coro, name="xxx", owner="group")   # 受监管
await supervisor.cancel_owner("group")
await supervisor.shutdown()
```

- `owning` 分组便于按模块取消（现有 owner：`application` / `web-panel` / `context-sweep` / `plugins` / `events` / `emotion` / `tts`）。
- 关闭中再 `create_task` 会抛 `TaskSupervisorClosed`（WS 派发处专门捕获）。
- 组件持有 supervisor 时惯用 `supervisor.create_task(...) if supervisor else asyncio.create_task(...)`。

### 8.2 出站队列 `services/outbound.py`

- 每个 bot 一条 `PriorityQueue` + 一个 worker，**串行发送**并强制最小间隔（`server.outbound_interval`，默认 0.5s）。
- 两个优先级：`CONTROL_PRIORITY=0`（**内联执行**，绕过队列与限速）、`MESSAGE_PRIORITY=10`。
- 队列满 → 立刻 `OutboundQueueFullError`（快速失败），入队超时也转成这个异常。
- **新消息一律走 `WSServer.send_group_msg/send_private_msg/send_image`**，这样才有限速 + `message_id` 追踪（引用回复识别依赖 `_pending_sent` 的 echo 记录）。

### 8.3 情感系统 `emotion/`

- 存储 `data/emotion/{bot_id}/{user_states,memory}.json`，内存缓存 + dirty 集，`emotion-autosave` 每 60 秒落盘。
- 模型：`favor(-100..100)` / `intimacy(0..100)` / 8 维情绪 / 交互统计 / 关系阶段。
- 阶段：正向 `INITIAL→DEEPENING→COMMITMENT→SYMBIOSIS`，负向 `COLD/DISLIKE/HOSTILE`；用**滞回**切换（上阈值 vs 阈值-5）防抖。
- 每轮 `schedule_turn` 起一个 `emotion-turn` 任务（fire-and-forget），`SmartUpdateManager.should_update` 决定是否真的调 LLM（情绪跨度 ≥8 / 关键词强度 / 陈旧 / 强制间隔 / 久别 7 天）。
- 命令：`好感度` `关系阶段` `好感排行 [N]` `负好感排行 [N]`；管理员 `设置好感/设置亲密/设置态度/重置好感/查看好感/情感重置`。
- 注入：`build_context_block` 作为临时 `system` 块（不落盘）。LLM 分析失败降级为 `_smart_fallback`。
- `emotion.enabled` **启动时读取，改后需重启**（与 TTS 保存即热生效不同）。

### 8.4 TTS `services/minimax_tts.py`

- MiniMax `t2a_v2` 同步合成。两条通路：LLM 自标 `<tts>` 句自动朗读、`/tts <文本>` 指令。
- 全局 FIFO 单飞行队列（`queue_maxsize` 默认 16），**队列满丢最新**。
- `<tts>` 标签剥离在 `message_handler` + `utils/tts_marker.py`：`TTSMarkerFilter` 会**扣留**半个标签防止跨 chunk 撕裂；标注句超长截到第一个句末标点、多标注取第一个、忘写闭标签容错。
- 音色：全局 `tts.voice_id`，per-bot `BotConfig.tts_voice_id` 覆盖（提交任务时就解析好）。
- 失败降级：LLM 来源静默（文本已发）；指令来源回错误提示。
- 面板保存后除 `queue_maxsize` 外全部热生效（`sync_config`）。

### 8.5 封禁 `ban/`

- 四个 JSON：`ban_list`（会话封）/ `banall_list`（全局封）/ `pass_list`（会话解禁）/ `passall_list`（全局解禁）。
- `session_key` = `group:{群号}` 或 `private:{QQ号}`。
- **优先级：会话解禁 > 会话封禁 > 全局解禁 > 全局封禁**。
- 语义是「bot 静默忽略被禁用户的消息」，不是 QQ 群管理封禁。
- 时间格式 `1d2h30m10s` 可组合，不带时间 = 永久。
- 拦截器**排在链首**，被禁用户的一切消息静默丢弃（`return (True, None)`）。
- 存储有 60 秒进程内缓存 + 一把全局锁串行化读写。

### 8.6 歌曲知识 `music_knowledge/`

- SQLite 事实库 `data/song_knowledge/knowledge_db.db`，`songs` 表字段：`name/safe_name/uploader/singers/lyricist/composer/arranger/mixer/tuner/mastering/pv/illustrator/year/introduction/lyrics`。
- 匹配三级：`《歌名》` 书名号高置信 → 裸歌名（长度 ≥3 且含语境词 唱/听/歌/歌词）→ 歌词片段（10 字滑窗）。
- 命中后 `build_annotation()` 生成 `【歌曲信息】…`，**仅拼进本轮 LLM 请求的用户消息下方，不写 context**。
- VCPedia 同步内置 Anubis PoW 反爬解题 + cookie 复用；入口 `/sync-songs`（管理员）或 `scripts/sync_vcpedia.py`。
- 用**独立的**轻量 engine/pool（`pool.py`），与 `mohobot/db` 无关。

### 8.7 AnySearch

Anysearch MCP JSON-RPC over httpx。`safe_search()` 失败返回 `""`（不阻塞回复）。只在 `anysearch.enabled` + `api_key` 都满足时构造并注入插件；未配置时 `anysearch_search` 工具 schema 被摘掉。

### 8.8 图片缓存 `image_cache.py`

- 300 MB LRU 上限，按 `cached_at` 淘汰最旧；`data/cache/image_cache_map.json` 存 `{url: {path, phash, description, cached_at, size}}`。
- **phash 去重**：Hamming 距离 < 5 视为同一张图，复用描述。
- `get_or_describe(url, vision_callback)` → `(local_path, description)`，同 URL **单飞行**（`_in_flight` Future + `asyncio.shield`），并发请求只调一次 VLM。
- **空描述不入缓存**（注释明确：临时失败不能被永久掩盖）。
- `peek_description(url)` 只读不下载不识别（占位符 `[图片]` / `[图片下载失败]` 返回 `None`）。

---

## 9. 两个 WebUI

### 9.1 主面板 `mohobot/web_panel/app.py`

- 单文件 FastAPI，全部路由是 `_setup_routes()` 里的闭包；前端是单文件 `static/index.html`。
- **认证**：PBKDF2-HMAC-SHA256（100000 轮，格式 `pbkdf2_sha256$salt$hex`）+ **Bearer token**（32 字节 hex，1 小时 TTL，进程内存）。**不用 cookie**。前端存 `localStorage['mohobot_token']`。登录防爆破（与 review/ 同款）：处理全局串行化（`_login_lock`）+ 每次尝试无条件 0.5s 硬延迟（耗时恒定防计时侧信道），失败写 loguru WARNING（用户名不存在/密码错误分列），成功写 INFO。
- 无默认密码：`__init__` 找不到 hash 也没有 `MOHOBOT_WEB_PASSWORD` 就 `raise ValueError`。
- SSE 日志流：因为 `EventSource` 不能带 header，先 `POST /api/logs/ticket` 拿一次性 30 秒票据，再 `GET /api/logs/stream?ticket=...&level=DEBUG,INFO`。日志 sink 是 loguru handler，`stop()` 时移除（防重启后重复 sink）。
- **密钥掩码 + 留空保留**：`GET` 时 `llm/anysearch/tts` 中含 `key|token|secret|password` 的字段返回 `********`；保存时提交空串或掩码则**保留原值**。
- **路径安全**：所有 `bot_id` 路径参数过 `_safe_id`（`^[A-Za-z0-9_-]{1,128}$`）。
- 备份/恢复/清理需再次输入账号密码；zip 恢复有 zip-slip 防护。
- 审计：除登录外所有 POST/PUT/PATCH/DELETE 写 `data/audit/web_admin.jsonl`，敏感 key 递归打码。
- 板块与端点（实际 11 个，docstring 里写「7 个」已过时）：

| 板块 | 端点 |
|---|---|
| 数据总览 | `GET /api/dashboard` `/api/usage/{sessions,users,modules}` `/api/health` |
| 配置文件 | `GET/PUT /api/config`；bot `GET/POST /api/bots`、`PUT /api/bots/{id}/bind`、`POST .../unbind`、`GET/PUT .../config` |
| 模型配置 | `GET/PUT /api/models` |
| TTS 语音 | `GET /api/tts/{status,config}`、`PUT /api/tts/config` |
| 插件管理 | `GET /api/plugins`、`POST /api/plugins/{toggle,reload}`、`GET/POST /api/plugins/{name}/config` |
| 对话数据 | `GET /api/contexts`、`.../{bot}/{type}/{id}`、session 增删切、message 编辑、reset |
| 实时日志 | `POST /api/logs/ticket`、`GET /api/logs/stream` |
| 系统设置 | `PUT /api/settings/password`、`POST /api/settings/restart` |
| 数据管理 | `POST /api/data/{backup,restore,cleanup}`（scope: cache/history/contexts/ban） |
| 封禁管理 | `GET /api/ban`、`POST /api/ban/operate` |
| 情感管理 | `GET /api/emotion/status`（队列/burst/熔断/存储快照）、`GET /api/emotion/states?bot_id=`（按好感降序、跳过零互动用户）、`POST /api/emotion/operate`（action=set_favor/set_intimacy/set_attitude/reset_user/clear_bot）、`GET /api/emotion/memory?bot_id=&user_id=`（只读记忆） |

> 情感板块用户身份只显示 QQ 号（不解析昵称）。`clear_bot` 清空该 bot 的情感状态与长期记忆（store.clear_bot 两者一起清）；态度文本在 manager 层先截断到 20 字再校验。回归测试 `tests/test_emotion_webui.py`。

### 9.2 审核面板 `review/`

半独立：**独立进程、独立端口（默认 9091）、独立配置（`review/config.yaml`）、独立数据库（`review/data/review.db`）**。主进程退出不影响它。

- 主进程 `main.py:_maybe_start_review_panel()` 负责拉起：缺 `config.yaml` / `enabled: false` / 端口已被监听 → 跳过；否则 detached `Popen` 起 `review/main.py`，日志写 `review/panel.log`。
- **数据源：`data/history` 消息事件流（唯一来源）**。history 只增不删，条目身份 = **message_id**（`mid:<id>`；无 id 时退回内容指纹 `hash:...`），审核结论永不因上下文压缩而失联（旧 contexts 指纹方案已废弃 —— 框架的 AI 总结压缩曾使生产上 97.5% 的已审条目失联）。
- **群聊审核范围（面板侧过滤，归档保持完整）**：只审 bot 发言（`message_sent`）与用户 @ 某只 bot（`at` 段 qq == 该 bot 的 self_id）或引用某只 bot 发言（`reply` 段 id ∈ 该 bot 已归档发言 mid 集合）的消息；私聊全部审（按 bot 独立会话）。
- **群聊单文件会话**：群聊归档本身已合并存储（`history/group/{群号}.jsonl`，见 §4.6），session_key 仍固定 `"_merged/group/{群号}"`（兼容 review.db 既有结论）。bot 发言按行内 `bot_id` 归属（旧数据无该字段时按 self_id 反查 bots 配置）；用户消息命中 @/引用 时归属到对应 bot（同时命中多只取 bot_id 排序最前者）。按 bot 的 mid 集合（`bot_mids_by_bot`）与 self_id（文件内 message_sent 行自带 ∪ bots 配置）做过滤。用户消息无 id 时按 time+uid+text 兜底去重；bot 发言不做内容级去重。
- `WSServer` 出站层把 bot 发送的消息以 `post_type: "message_sent"` 追加进同一个 history JSONL（`ws_server._write_archive_line`，echo 超时用 `local:` id 兜底；发送明确失败不归档；`base64://` 大字段净化为占位）。
- 加载器对 history 文件做**增量解析**（记录 offset，只读新增字节，mtime/size 失效；文件被截断时全量重读）。
- 会话明细接口分页（`?page=&page_size=`，默认锚定第一条待审所在页；判定后重开自动跳下一批）。
- 审核状态存自己的库（`reviewed_entries` / `abnormal_records` / `review_log`）。
- 多用户支持，改密码时**按行替换 `config.yaml` 的 `password_hash:` 行以保留注释**（`_write_user_password`，结构不符时兜底整体重写、注释会丢）。
- **登录防爆破**：登录处理全局串行化（`asyncio.Lock`）+ 每次尝试固定 0.5s 硬延迟（耗时恒定防计时侧信道）。

**生产切换（旧 contexts 指纹 → history message_id）部署步骤**：

1. 备份并清空旧审核库（旧指纹基于 contexts 条目，切到 message_id 后无法映射，按约定全部作废；如需留证据先从面板导出 CSV）：
   ```bash
   cd review/data && stamp=$(date +%Y%m%d-%H%M%S) \
     && for f in review.db review.db-wal review.db-shm; do [ -f "$f" ] && mv "$f" "$f.bak-$stamp"; done
   ```
2. `git pull` 后**重启 mohobot**（WSServer 归档逻辑与 review 面板都需要新代码）。
3. 重启前的 bot 历史发言没有 `message_sent` 归档，用户引用它们的旧消息不会被识别为「引用 bot」；新发言即时生效。

⚠️ 生产服务器上 `review/hash_password.py` 有**未提交的手工改动**（绕过导入错误的就地补丁），部署 `git pull` 前需先处理（还原或提交），否则 pull 会被本地修改卡住。

---

## 10. 已知坑与不一致（改动前必读）

**真实 bug / 设计缺陷**

1. **`_execute_tool` 只路由 `song_` 前缀**（`llm_service.py:1213-1218`）：非 `song_` 的插件工具会被广告给模型但执行返回 `未知工具`；且该分支**重复两次**，第二段是死代码。加工具必须改这里（见 7.2）。
2. **`get_group_member_info` 是永久错误桩**（`llm_service.py:1222-1224`），却仍在工具列表里，模型会浪费一轮调用。
3. **`on_config_update` 若写成 `async def` 则完全不执行**（同步调用不 await）。
4. **单文件插件无法拥有自己的 `_conf_schema.json`**（schema 路径取 `entry_path.parent` = `plugins/`）。
5. **`config.log_dir` 是死配置**：`main.py:547` 硬编码 `setup_logger(log_dir="./logs")`，配了没用。
6. **`json_write` 非原子**（就地截断，非 temp+rename），崩溃可能留半截文件。
7. **`app.py:265` 的 `hmac.compare_digest(token, token)` 是自我比较的空操作**，误导性代码（实际靠 dict 查找 + 过期判断）。
8. **`_HARDCODED_PERSONAS` 有拼写异常键** `38310975970`（多一位数字），几乎肯定是 10 位 QQ 的笔误。
9. **`plugins/feed/` 是空目录残留**（只有 `__pycache__`），加载器静默跳过。

**文档与代码漂移**

10. **默认值不一致**：`server.port` dataclass 默认 `8060` vs 示例 yaml `8081`；`database.file` 默认 `luotianyi.db` vs 示例 `mohobot.db`。**以 `config/global.yaml` 为准**，dataclass 默认只在没写 yaml 时生效。
11. **`config/global.example.yaml` 落后**：缺 `context_summary_enabled` / `context_trim_at_rounds` / `context_trim_remove_rounds` / `group_recent_msgs_count` / `tts.*` 整段。
12. **`app.py` docstring 说「7 板块」，实际 11 个**。

**会咬人的机制**

13. **`GlobalConfig.save()` 会丢注释**：PyYAML `yaml.dump` 重新生成，`global.example.yaml` 的注释在第一次 WebUI 保存后就没了。只有审核面板改密码那处做了保注释的按行替换。
14. **加顶层标量配置必须改 `PUT /api/config` 的白名单元组**，否则面板保存静默丢弃该字段（嵌套段不需要）。
15. **`save()` 和 `to_dict()` 是两份重复字典**，加字段两处都要改，漏一处会出现「存了但读不到」。
16. **情感 `min_interval_sec` / `analysis_round_cooldown` 门控已被 revert**（`bfacefb`、`5c7c534`）：现在 `EmotionConfig` 没有这两个字段，`smart.py` 是**宽松版**（`"好"` 在正向关键词里、bot 回复关键词 +1、阈值 `intensity >= 2`）。别按旧文档去改这两个字段。
17. **改 `MessageHandler` 时注意临时 `system` 块**（群聊最近消息 / 环境感知 / 情感 / 引用消息）**绝不能写回 context 文件**，否则会污染压缩与轮数统计。
18. **`data/` 与 `logs/` 完全不入库**，本地跑测试/开发会污染工作区数据；测试必须用 `tempfile`。
19. **`tests/_js_check.py` 自带词法误报且不拦人**：对当前 `index.html` 报 MISMATCH/UNCLOSED（对某处正则/字符串误判），且无论结果如何**退出码恒为 0**。改前端后请用 `node --check`（提取 `<script>` 内容）验证真实语法。

---

## 11. 常见任务 Cookbook

### 加一个配置项
见 §5.2 的 8 步清单（顶层标量）/ 差异说明（嵌套段、llm、per-bot）。

### 加一个插件
1. 建 `plugins/myplugin/main.py`（+ 可选 `_conf_schema.json`）。
2. 按 §6.7 模板写 `Plugin` 类。
3. 要群内多 bot 去重 → 声明 `global_triggers`。
4. 要无需 `/` 前缀 → 声明 `no_prefix_triggers` + `on_message_observed`。
5. 要周期任务 → `on_tick` + `interval_sec`。
6. 要配置 → 目录形态 + `_conf_schema.json` + 读 `self.plugin_config`。
7. 写 `tests/test_myplugin.py`（参考 `tests/test_wifepicker.py` 的桩法）。
8. `python tests/_run_all.py`；面板「插件管理」热重载验证。

### 加一个 LLM 工具
1. 在插件里 `registry.register(LLMTool(tool_schema(...), handler))`。
2. 确保模块启动时被 import（`mohobot/services/llm_tools.py:load_plugin_tools()` 的 import 列表）。
3. **改 `llm_service._execute_tool`** 让非 `song_` 前缀也能路由到 registry。
4. handler 返回 `str` 或可 JSON 序列化的对象；异常会被 registry 转成 `{"error": ...}`。

### 加 `conversations` 表字段
见 §4.4 的两步迁移套路。

### 调一个新的 OneBot API
```python
resp = await ws.send_to_bot(bot_id, "action_name", {...}, wait_response=True, timeout=5.0)
data = (resp or {}).get("data")
```
`wait_response=True` 走 control 优先级（内联、不限速）；不需要回包就用 `wait_response=False`。

### 让某功能「保存即热生效」
在 `web_panel/app.py` 的 `update_config` / `update_tts_config` 里，`cfg.save()` 之后调用对应组件的 `sync_config(...)` / `set_*_config(...)`，或在组件里提供 `@property` 每轮重读（如 ContextManager 的 `sweep_interval_minutes`）。

### 发一条消息
```python
await ws.send_group_msg(bot_id, group_id, "文本或段列表")
await ws.send_private_msg(bot_id, user_id, "…")
await ws.send_image(bot_id, chat_type, chat_id, path)
await ws.send_group_forward_msg(bot_id, group_id, nodes)
```
别绕过 `WSServer`（会丢限速与 reply 追踪）。

---

## 12. 提交前检查

```bash
python tests/_run_all.py            # 必须 41/41（或你的新基线）
python tests/test_concurrency.py    # 动了并发/合并回复/抽签时要跑
python tests/_js_check.py           # 动了 web_panel/static/index.html 要跑
python tests/smoke_startup.py       # 动了 main.py 装配要跑
```

另外自重这些：

- 新增文件 I/O 是否走了 `file_store`（多人改同一文件必须用 `json_update`）？
- 新增上下文相关逻辑是否避免了把临时块写回 context 文件？
- 新增后台任务是否注册进 `TaskSupervisor`（带 name/owner），并在 `shutdown` 路径能停？
- 新增配置项是否 8 处都改了（尤其 `PUT /api/config` 白名单）？
- 新增消息发送是否走了 `WSServer` helper？
