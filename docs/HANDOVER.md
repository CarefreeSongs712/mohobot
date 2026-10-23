# Mohobot 项目接手说明

> 核对日期：2026-10-12（Asia/Shanghai）｜**历史代码基点**：`b63ad48`｜当时分支：`main`。
> 本文记录该次本地检查，不代表当前 `main`、生产实时状态或发布授权。接手前工作区干净，HEAD 与当时本地缓存的 origin/main 相同；未执行 fetch/SSH，也没有部署、迁移或业务配置修改。
> **当前（2026-10-22）状态请看下面第 0 节**；第 1~8 节是 2026-10-12 的历史交接记录，其中的基线数字不要当作当前验收结果。

---

## 0. 最新交接状态（2026-10-22）

> 本节是本次开发收尾的交接结论；详细待办与决策项以 [TODO.md](../TODO.md) 为准。

### 0.1 仓库与远端

| 项 | 状态 |
|---|---|
| 本地 / 远端 `dev` | 本节定稿时 CI 为 **success**；具体提交请以 `git log -1 origin/dev` 为准（文档难免在本节提交后才定稿，故不硬编码自身 SHA） |
| 工作区 | 干净，无未推送提交、无未跟踪的受跟踪文件 |
| `origin/main` | `a361736`（**冻结**，仅作发布线；本轮未改动、未发版） |
| 领先量 | `dev` 比 `main` 多 **约 75 个提交**（以 `git rev-list --count origin/main..origin/dev` 为准） |
| 开放 PR | **0** |
| 开放 issue | `#2`（wontfix，SQLite 旧库 WAL 迁移，历史遗留） |

### 0.2 本轮交付（`#51`~`#75`）

- **文档**：TODO 重构、README/DEVELOPMENT/AGENTS 全量同步、识图两模式补记（`#51`/`#53`/`#68`）。
- **知识库**：RAG 虚拟歌手知识库合并（`#52`）；中V歌曲库多源同步与模糊识别（`#47`/`#49`）。
- **表情包插件**：去概览式偷图 + 审核门禁（`#61`）、`allowed_bots` 多 bot 分流（`#64`）、列表按事件作用域过滤 + `steal_meme` 图片来源边界 + `source_type` 规范化（`#72`，Closes `#6`）。
- **识图**：`llm.vision_mode: describe/direct`（`#55`/`#68`），本机真 Key 已验证 direct。
- **新闻/动态**：存储 + 审核门禁 + 注入 + LLM 工具 + WebUI 审核页 + B站采集器（`#62`/`#65`，默认关闭）。
- **OOC 治理**：无标签自查/推理块外泄治理 + 采样惩罚参数（`#60`）、中英混杂清理（`#66`）。
- **插件策略/关系/主动聊天**（`#67`/`#70`）、fake OneBot 契约测试环境（`#56`）。
- **review 面板**：热维护写入屏障 + auto 消息 ID（`#69`）；缓存 inode、半行 offset、待审计数求交、前端代际（`#71`，Closes `#8`）。
- **LLM 多提供商**：多套 `base_url + api_key + default_model` 并存、全局/每 bot 选择（`#73`）；`/api/config` Key 泄漏热修（`#74`）；复审 N13~N17 跟进（`#75`）。
- **测试基础设施**：`pytest-xdist` 并行（全量 ~80s → ~20~27s）、CI 改 `-n auto --dist loadfile`、修 `test_wifepicker` 顺序依赖。
- **收尾修复**：snitch 重复注册（`d826da7`）、面板创建 Bot 不接管在线连接（`a494a91`）、本地空歌曲库假失败（`9e26b90`）。

### 0.3 验证基线（2026-10-22 实测，隔离快照）

快照 `mkwork` 排除 `.git/.venv`，进程清空代理变量；`run_in.py` 记录 `guard_loaded=13 blocked_network=0`。

| 检查 | 结果 |
|---|---|
| 全量 pytest（`-n auto --dist loadfile`，排除 concurrency） | **810 passed** / 约 26s |
| 根 runner `tests/_run_all.py` | **91 passed / 0 failed** |
| `tests/test_ban_pipeline.py`（runner 漏执行） | OK |
| `tests/test_plugin_reload.py`（runner 漏执行） | OK |
| `tests/test_concurrency.py` | OK |
| `tests/smoke_startup.py` | 启动/关闭 OK |
| `compileall` | 退出 0 |
| Ruff 门禁（`E9,F63,F7`） | 退出 0 |
| 表情包插件 pytest | **503 passed / 2 skipped** |
| 主面板内联 JS `node --check` | OK |

已知既有债务（非本轮引入）：6 处 Ruff 告警（`message_handler` F401、`llm_service` F401/F841、`bot_manager` 3× F821、relationship F401）；代理环境变量容错未做（见 TODO §4）。

### 0.4 本机环境（这台 Linux 开发机）

- `.venv` 已装齐 `requirements.txt` + `requirements-dev.txt`（含 `pytest-xdist`）。
- `config/global.yaml`、`review/config.yaml`、`data/`、`logs/` **均被 .gitignore 排除**，只在本机，**从未提交**；里面含真实 Key 与面板密码哈希，移交/发布时不要打包。
- 已实测可跑：OneBot 客户端（本机用 SnowLuma，QQ 2774641554，主管理号 2038427197）→ `ws://127.0.0.1:8081/ws`；面板 9090；审核面板 9091（`review/config.yaml` 已建）。
- **代理坑（本机必看）**：`ALL_PROXY=socks://...` 会让 httpx 启动即崩；`no_proxy` 含 `[::1]` 会 `Invalid port`。启动前用：
  ```bash
  env -u ALL_PROXY -u all_proxy \
      NO_PROXY="localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16" \
      no_proxy="localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16" \
      .venv/bin/python main.py
  ```
- 可选依赖未装：`playwright`（关系图渲染降级文本）、`umap-learn`（表情语义图降级 PCA）。

### 0.5 待维护者决策（移交接手第一优先级）

1. **`dev → main` 发版**：按 AGENTS「只以 main 发布」，需维护者授权后做整体回归 + 合并 + release notes。（唯一未决项）

**2026-10-23 维护者已定取舍**（详细见 [TODO §2.9](../TODO.md)）：
- **REQ-9**：~~history 双写 flush 策略~~ → 已决（2026-10-23）：删除双写、群聊单写；flush 批量暂不启用；
- **history 双写已删除**（2026-10-23）：群聊只写合并布局，旧 per-bot 布局停止写入（review 读侧兼容存量）；flush 批量仍暂不启用；
- **CI legacy runner**：PR 阶段不再跑，仅 `push main` 时运行（pytest 为超集）；
- 发布/部署体验（Docker、Windows 安装器、对外部署文档）：**不做**（个人自用）；
- 安全基础加固（登录锁 DoS、失败限流、计时枚举、PBKDF2 等）：**可修可不修**，不阻塞发布，按需排期；
- 运维观测（healthz/readyz、成功率/延迟、告警）与知识库/新闻真实验收：**可做**，列入后续可选优化。

### 0.6 接手注意事项（易踩）

- 根 `main` 冻结：任何新改动走 `dev`，绝不直接推 `main`。
- 插件 `plugins/snitch.py` 是「内置工具 + 插件文件」双身份，注册已幂等；不要为了去掉日志里的历史报错而删插件文件。
- 面板「创建 Bot」现在会立即接管已在线连接（`create_bot` → `bind_qq`），修改这块时保留该行为。
- 本地测试优先用快照（`.venv/prep/mkwork.sh` + `run_in.py`），因为工作区已有 `config/`、`data/`。

---

## 1. 接手结论与范围

**当时的架构与开发约束已梳理；隔离开发环境在该快照上完成验证，并记录了 9 个 P1、13 个 P2 问题，详见 [缺陷审查与修复方案](AUDIT-2026-10-12.md)。这不是当前 main 的完整回归或发布结论；后续修复与 CI 状态以 TODO、PR 和 Actions 为准。**

已阅读主 README、开发指南、配置模型与示例、主装配入口、测试 runner、相关消息/LLM/文件/TTS实现，以及主动聊天、表情包插件的局部说明。本次新增守则、交接与审查文档、校正文档漂移，不修改业务源码或运行数据。

本机系统 Python 3.12.3（`/usr/bin/python`）缺多项依赖，不要直接用它运行项目；项目根已建 `.venv`（Git 忽略）并装齐依赖。根目录仍无本地业务配置和运行数据目录，没有启动真实服务。

## 2. 建议阅读顺序与文档职责

| 文档 | 解决的问题 |
|---|---|
| [AGENTS.md](../AGENTS.md) | 每次接手必须遵守的数据、并发、扩展、验证、安全与授权底线 |
| [README.md](../README.md) | 产品能力、配置、命令、部署、数据布局、生产快照 |
| [DEVELOPMENT.md](DEVELOPMENT.md) | 修改插件、配置、工具、数据库、面板时的联动清单与已知机制 |
| [global.example.yaml](../config/global.example.yaml) | 新安装配置起点；不是完整字段清单，也不是生产配置 |
| [config.py](../mohobot/models/config.py) | 配置字段、默认值、load/save/to_dict 的实际来源 |
| [mohobot.service](../deploy/mohobot.service) | 新部署模板；不能重复安装到已有生产环境 |
| [审核配置模板](../review/config.example.yaml) | 独立审核面板端口、只读数据来源与审核员哈希配置 |
| [主动聊天 README](../plugins/proactive_chat/README.md) | 调度窗口、免打扰、重启恢复、发送与记忆约束 |
| [表情包 README](../plugins/meme_stealer/README.md) / [局部 CLAUDE.md](../plugins/meme_stealer/CLAUDE.md) | 模型回退、工具、独立 WebUI、适配边界、测试与热重载约束 |
| [外部表情包来源](../plugins/meme_stealer/docs/external-sources.md) | 只读导入边界、ZIP/网络安全、来源许可与凭据保护 |

开发指南引用的外部“远程部署操作规范.txt”未包含在本次仓库中；需要远程操作时应向用户取得该规范，并重新核实现有环境。不得自行补造其内容。

**信息判读方式**：操作约束按项目规范执行；实现状态用代码与测试核实；配置用实际加载值而非例子；带日期的生产参数只作快照。冲突应说明并校正，不默默忽略。

## 3. 架构与关键入口

```text
OneBot v11 客户端（NapCat / LLOneBot / Lagrange）
    │ 反向 WebSocket
    ▼
WSServer ──事件任务──▶ MessageHandler ──插件/命令或 LLM──▶ WSServer 出站队列
                           │
                           ├─ contexts：工作记忆 / session generation
                           ├─ history：收到与发送事件的追加归档
                           ├─ SQLite：完成的对话轮次
                           └─ Persona / A/B / Emotion / Image / TTS / Music

main.py：创建、注入、启动、热同步、优雅关闭
主面板：FastAPI + SSE + 单文件 HTML
review：独立进程、配置和数据库，只读 history
表情包 WebUI：插件自管的可选端口与认证
```

| 入口 | 接手关注点 |
|---|---|
| [main.py](../main.py) | 同一 event loop、依赖装配顺序、共享实例、关闭/重启资源释放 |
| [ws_server.py](../mohobot/ws_server.py) / [outbound.py](../mohobot/services/outbound.py) | bot 连接、队列限速、echo、发送确认与出站归档 |
| [message_handler.py](../mohobot/message_handler.py) | 主消息链、封禁/群门控/去重、上下文写回、产物过滤 |
| [context_manager.py](../mohobot/context_manager.py) / [file_store.py](../mohobot/file_store.py) | 会话索引、generation、压缩、维护屏障、文件锁 |
| [persona_service.py](../mohobot/persona_service.py) | 共享预设、session 覆盖、引用检查、备份恢复一致性 |
| [persona_ab.py](../mohobot/services/persona_ab.py) / [repository](../mohobot/services/persona_ab_repository.py) | 单轮匿名评估状态机、发送确认、投票与切换、独立数据 |
| [llm_service.py](../mohobot/llm_service.py) / [llm_tools.py](../mohobot/services/llm_tools.py) | 请求级输入、流式与工具续轮、模型热同步、用量 |
| [plugin_system.py](../mohobot/interceptors/plugin_system.py) | 加载/注入、同步配置回调、观察/感知、启停与重载 |
| [app.py](../mohobot/web_panel/app.py) / [index.html](../mohobot/web_panel/static/index.html) | 认证、密钥掩码、配置白名单、管理 API 与前端 |
| [review loader](../review/loader.py) / [store](../review/store.py) | history 增量解析、message_id 审核身份、独立审核库 |

### 消息主链（不要随意换序）

1. 归档收到的消息；外部服务独占群随后退出，群通知/请求也受此门控。
2. 消息封禁预检查；`/ab` 提前消费；私聊自动回复过滤。
3. 记录群内 bot 与最近消息 → 感知 → 多 bot 合并回复 → 插件观察。
4. 群响应 gate（命令/ping/@本 bot/引用本 bot）→ 全局命令去重。
5. 图片限流/归一化 → 封禁、插件、内置命令、关键词拦截器链。
6. LLM 排除群门控 → ping/PONG → 捕获 session/generation/人设快照。
7. 准备上下文与引用 → 可选 A/B 或普通 LLM → 过滤、分段、发送。
8. 主链 full_reply.strip() 非空时写回捕获会话；generation 校验失败即停止 DB/情感回写。非空不等于模型调用/发送成功，现有错误文案也可能落盘。

封禁命令有既有旁路例外，不能简单把整个封禁拦截器前移。群的 LLM 排除与外部服务独占不是同一种过滤。

## 4. 数据边界与备份注意

- 原始 history 只追加，用于回溯与审核，**不作为 LLM 实时上下文**。
- contexts 是可变工作记忆；满轮总结失败可裁剪，周期总结失败保留待重试。
- 群历史全 bot 共享、私聊按 bot 隔离；群聊**只写**合并布局（`history/group/`），旧 per-bot 布局已停止写入（review 读侧仅兼容存量）。
- 人设正文、感知、群最近消息、情感、歌曲注解仅请求级注入。引用内容可以按现有用户文本渲染规则保存，但不能存原始 system 块、消息段 repr 或 base64。
- 通用文件读改写采用 file_store 的同一把路径锁；不等于跨进程事务或崩溃安全替换。人设/评估已有原子替换实现须保留。
- 人设库损坏要报错；删除/恢复必须强制检查真实引用，不能依赖显示缓存。锁序为 persona → maintenance → chat → file。
- A/B 只表示单轮、相同历史下的偏好，投票不改历史也不自动切换人设；只允许固定 bot 的私聊，默认关闭。
- 聊天 ZIP 备份不包含 A/B 数据，需独立 JSON 导出，首版无恢复导入；备份也不是完整 bot 配置/全部插件数据备份。
- review 的审核结论存独立数据库；复制 Git 仓库或 Git bundle 都不等于备份运行数据、未提交文件与密钥。

## 5. 本次已核实的文档漂移

| 原说明 | 当前代码/检查结果 | 本次处理 |
|---|---|---|
| 插件工具只能走 song_ 前缀 | `_execute_tool` 按 registry.contains 路由任意已注册名称，内置名优先 | 更新开发指南，不重复“修复”已支持的能力 |
| get_group_member_info 错误桩仍广告 | 兼容执行分支仍在，但 schema 列表排除该名称 | 更新工具状态说明 |
| 情感间隔门控已 revert/不存在 | EmotionConfig 与 manager 仍有 min_interval_sec、analysis_round_cooldown；关键词默认阈值 3，仅用户计分 | 更新已知坑条目 |
| TTS 只有 MiniMax、只朗读首个短标注 | 主服务支持 HTTP/MiniMax；主回复链 ReplyArtifactFilter 合并多段朗读、不作旧短句截断 | 更新 TTS 与环境变量说明 |
| 同 key 就复用 LLM client | 当前按 key + base_url 的完整二元组复用 | 更新客户端说明 |
| 情感 enabled 一律重启生效 | 有实例时 enabled 可热同步；启动未创建实例时开启需重装配；max_concurrent 容量仍需重启 | 明确装配与运行期开关的区别 |
| 全量基线 41/45 文件 | 当前根测试 63 文件，runner 候选 62，另排除 concurrency；还会漏掉无顶层 wrapper 的类测试；未完整实测 | 区分文件计数、通过基线与实际覆盖 |
| 消息队列未实现、只有冒烟测试 | 已有每 bot 出站队列和根回归 runner | 更新 README 开发状态 |
| 示例配置缺部分字段/旧注释 | 缺轮数压缩、最近群消息、tts 整段；歌曲“用户消息下方”注释也仍是旧行为 | 保留风险提醒；实际字段以配置模型、歌曲注入以当前实现为准，不改模板默认 |
| 歌曲注解属于用户消息下方 | 当前拼入主 system 的服务端歌曲参考资料，不算用户发言 | 校正 README 与开发指南，仍仅请求级注入 |

另核实：用量已采用增量读取及短缓存，旧“每次全量解析 + 60 秒缓存”的描述不再准确。章节行号与文件长度易漂移，定位时以函数名为主。本次不是全仓功能审计，未实际验收页面行为、机器人发送或外部服务。

## 6. 验证记录（2026-10-12）

| 检查 | 结果 | 边界 |
|---|---|---|
| Git 状态与 HEAD | **历史记录**：接手前干净，main / b63ad48，本地缓存 origin/main 同提交 | 当时未访问远程确认最新提交；当前状态不要沿用该快照，先查 `git status`、`origin/main` 与开放 PR |
| `python -m compileall -q main.py mohobot plugins review scripts tests` | 退出 0 | 仅 Python 语法，不执行业务；会产生忽略的 pycache |
| `python -m pytest -q tests/test_reply_filter.py` | **10 passed，退出 0** | 仅该纯逻辑测试文件，不是根全量验收 |
| 主面板内联 JS 提取后 `node --check` | 1 个脚本，退出 0 | 语法正确不代表 DOM/API/页面行为通过 |
| 系统 Python 下 `python tests/_run_all.py`（接手时） | 退出 1：loguru 等依赖缺失，导入中断 | 已由下方隔离环境基线取代 |
| 系统 Python `pip check` | 退出 1：pynacl 依赖 cffi 缺失 | 系统环境问题，不改全局包 |
| 文档本地链接/代码围栏/LF/空白与 `git diff --check` | 本地链接均存在，检查退出 0 | 未访问外部链接；未提交/推送 |

### 隔离环境基线（同日 20:50 实测）

在 `.venv/prep/workspace` 源码快照中运行：快照只含 388 个受跟踪文件，**不含** `config/global.yaml`、`review/config.yaml`、`data/`、`logs/`；子进程清空 `MOHOBOT_*` 等环境变量；审计钩子拦截外网与 DNS，只允许测试自建的本机临时端口。

| 检查 | 结果 |
|---|---|
| `pip check`（.venv） | 无冲突 |
| 网络防护自检 | 3 类越界操作均被拦截，自建本机端口放行 |
| `compileall` | 退出 0 |
| 根 runner `tests/_run_all.py` | **历史快照结果：62 passed, 0 failed** | 当前 CI/PR 结果见 TODO 与 GitHub Actions，不把该数字当作 main 当前结果 |
| `tests/test_concurrency.py`（runner 排除） | 全部通过 |
| `tests/test_ban_pipeline.py`（runner 漏执行） | **14 例 OK** |
| `tests/test_plugin_reload.py`（runner 漏执行） | 全部通过 |
| `tests/test_qzone.py` 直接运行（runner 未等待 4 例协程） | 全部通过 |
| `tests/smoke_startup.py` | 启动/关闭 OK |
| 表情包插件 pytest | **488 passed, 2 skipped**（需上游 git 历史 / 可选 umap） |
| 表情包插件 ruff | 通过 |
| 快照内产生的未跟踪业务文件 | 0 |

**基线全绿不等于没有缺陷**：审查确认的 22 项问题都不在现有测试覆盖内，见 [审查报告](AUDIT-2026-10-12.md)。

### 复现基线

```bash
# 依赖（已完成；重建时先确认 .venv 不存在）
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r plugins/meme_stealer/requirements-dev.txt

# 隔离快照中的全部阶段（快照与脚本在 .venv/prep/，Git 忽略）
.venv/bin/python .venv/prep/run_baseline.py
# 只跑部分阶段，例如：
.venv/bin/python .venv/prep/run_baseline.py root-runner ban-pipeline
```

快照是某一时刻的副本；修改源码后需重建快照，或在确认无业务配置/数据的工作区直接用 `.venv/bin/python` 运行测试。`requirements.txt` 只有版本下限，本次装到 openai 3.x、websockets 17、SQLAlchemy 2.1 等新大版本，生产环境实际版本需另行核对。

根 runner 只发现模块顶层 `test_*`，不调用 unittest 类或 `main()`，零用例文件也记 PASS；同步测试返回的协程不会被等待。因此封禁消息链、插件热重载、QQ 空间会话测试必须单独运行，62 文件通过不代表完整覆盖。表情包插件另有 pytest/ruff，在插件目录运行。

改主面板前端时可用此无中间文件的语法检查（当前 HTML 只有一个普通内联脚本；结构改为 module/src 后应调整）：

```bash
python - <<'PY'
import re, subprocess
from pathlib import Path
html = Path('mohobot/web_panel/static/index.html').read_text(encoding='utf-8')
blocks = re.findall(r'<script>([\s\S]*?)</script>', html)
assert blocks, '未找到待验证内联脚本'
for source in blocks:
    subprocess.run(['node', '--check'], input=source, text=True, check=True)
PY
```

## 7. 操作与发布边界

- 当前只整理文档与建立本地开发环境，不提交、不推送、不 SSH、不启动真实主服务、不生成本地业务配置、不操作业务数据。
- 现有生产目录/unit/端口见 README 的带日期快照；上线前重新检查工作区、main 分支、执行入口、MainPID、cgroup 与监听端口。模板 unit 只给新环境使用。
- 提交/推送/生产重启需明确授权；只以 main 发布，pull 用 ff-only；遇分叉/脏工作区暂停报告。不得直接用本地目录覆盖生产配置和数据。
- 管理面板进程内重启不加载新 Python 源码；代码发布需要对应 systemd 进程重启，不另开第二实例。
- review 通常独立运行，但可随 systemd cgroup 一同停止/重启；不能假设它总不受主服务操作影响。
- 主面板密钥掩码与认证不等于插件配置同样保密；表情包插件配置中的密钥对有面板权限的人可见。所有面板不宜公网裸露。
- 不对“所有脚本”做执行式巡检：[_sweep_once.py](../scripts/_sweep_once.py) 在模块顶层删除旧结论并把待审批量判为 normal，且写死历史生产路径；[sync_banlist.py](../scripts/sync_banlist.py) 整体覆盖全局名单。两者是历史专项工具，不是启动或验收步骤，不能随意 import/运行。
- [migrate_history_layout.py](../scripts/migrate_history_layout.py) 需要停机/备份，可 dry-run；歌曲迁移、清洗、重抓、同步脚本均可能写数据库/访问网络，应按任务逐一授权。
- 根 [MIT 许可](../LICENSE) 不覆盖所有移植模块；表情包与主动聊天各有 [AGPL-3.0](../plugins/meme_stealer/LICENSE)、[AGPL-3.0](../plugins/proactive_chat/LICENSE)，发布时检查各模块的上游声明和义务。

## 8. 后续工作优先级

1. ~~准备隔离依赖环境，建立完整实测回归基线~~（已完成，见第 6 节）。
2. 按 [审查报告](AUDIT-2026-10-12.md) 的建议顺序修复缺陷：先一行级修复和隐私越权，再记忆正确性与投递生命周期；每项先补能复现的失败测试。
3. 改进根测试 runner（收集 unittest 类、等待协程、零用例判失败），避免已知漏测再次被「全绿」掩盖。
4. 新功能按守则限定范围，补回归并同步文档，保留用户现有数据与配置。
5. 如需补全示例配置/版本锁定/CI，单列任务，不在交接时擅改运行默认值。
6. 如需生产发布，取得外部部署规范及授权，按实际环境预检和验收；数据维护另行确认。
