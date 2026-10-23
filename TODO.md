# TODO

项目待办清单。每一项都尽量链接到对应的 Issue / PR，并写明**如何验证已完成**。
实现但尚未合入时保留条目并注明 PR；维护者合入后更新状态并记录在对应章节中。

> 当前基线：2026-10-22，`main` = `a361736`（**冻结，不接收新改动**），`dev` 以 `git log -1 origin/dev` 为准（本节维护时 CI 四项通过）。
> 分支规则：功能/修复走「本地分支 → PR → `pr-review-report` 审查 → CI 绿 → 合并 `dev`」；**极为简单的文档/注释改动可由维护者授权后直接提交 `dev`**；**任何情况下不直接推 `main`**。
> 交接状态、验收基线、本机环境与待决策项见 [docs/HANDOVER.md 第 0 节](docs/HANDOVER.md)。

---

## 1. 用户提出的长期重点需求与演进任务（2026-10-18）

| 编号 | 需求项 | 核心目标与技术边界 | 前置条件 / 依赖 | 状态 / 对应 PR |
|---|---|---|---|---|
| **REQ-1** | **文档全面审阅与重写** | 参照最新实现与架构标准，对 `README.md`、`docs/DEVELOPMENT.md`、`AGENTS.md`、`TODO.md` 进行全量审阅、校准与重写，彻底消除过时提交 SHA、旧分支表述及不符实现。 | 无直接代码依赖 | **✅ 已完成** —— `#51`（TODO 重构）、`#53`（README/DEVELOPMENT/AGENTS 全量同步）、`#68`（补记 `vision_mode` 两种识图模式并修正过期注释） |
| **REQ-2** | **审查并推进知识库代码 (`feat/knowledge-base`)** | 审查位于分支 `feat/knowledge-base`（head `0d1eaa0`）的 RAG 虚拟歌手知识库实现（语义检索 + 可选 rerank），做完整代码审查与回归验证，确认无误后通过 PR 流程合并。 | 隔离环境测试、内存及依赖核验 | **✅ 已完成** —— `#52` 合并（含合并态验证与 3 条 Ruff 债务清理）；模块 `mohobot/knowledge/`，专项测试 20 passed；开关 `knowledge.enabled` 默认关 |
| **REQ-3** | **重构表情包管理插件 (`meme_stealer`)** | 1. 移除入站聊天全量概览式抓取，仅保留 LLM tool call 抓取与 WebUI 手动上传；<br>2. 引入管理员审核机制，LLM 抓取标注后须经 WebUI 审核批准方可入库；<br>3. 表情包元数据增加适用 `bot_id` 标签，支持按 bot 分流与展示。 | 需明确 WebUI 审核数据流及前端修改范围 | **✅ 三点全部完成** —— `#61`（去概览抓图 + 工具抓取默认待审）、`#64`（`allowed_bots` 白名单 + 检索过滤 + 工具 `bots` 参数）；WebUI 可视化编辑 `allowed_bots` 属可选后续 |
| **REQ-4** | **改造中V歌曲知识库** | 1. 改造定时同步机制（解决阻塞与重试问题）；<br>2. 数据来源除 VCPedia 外，增加萌娘百科（`moegirl`）、VocaWiki 等；<br>3. 歌曲上下文注入支持错别字、漏字、括号别名的容错与模糊识别。 | 多源同步需网络与真机验证 | **✅ 已完成（既有实现）** —— `#47` 多来源同步/字段合并/模糊识别，`#49` 修复并发竞态（锁内提交 + 原子 upsert）；三源 `vcpedia/moegirl/vocawiki` 已接线，模糊匹配 Damerau-Levenshtein + 歌词 6-gram；定时同步开关 `music_knowledge.sync.enabled` 默认关 |
| **REQ-5** | **优化识图逻辑（直传多模态支持）** | 在现有“独立 VLM 描述转述”之外，并列提供配置选项：当主模型具备原生视觉能力时，允许将图片原始数据直接与文本一并发送，减少一次额外模型请求。 | LLM 服务层多模态消息构造适配 | **✅ 已完成** —— `#55` 新增 `llm.vision_mode: describe(默认) / direct`，失败自动回退 describe；`#68` 补文档与注释。真实多模态已在本机实测（2026-10-22：`deepseek-chat` 读图成功，见 §2.6） |
| **REQ-6** | **新闻动态注入与主动话题功能** | 1. 采集并向 LLM 注入近期相关新闻（B站官号动态/视频、知名 UP 主动态，或由 LLM 在聊天中主动提取）；<br>2. 新闻作为主动聊天资料与对话话题；<br>3. 提供 function call / tool 供模型主动调取查询；<br>4. 新增或提取的新闻条目必须经管理员审核后方可生效。 | 引入审核数据表、B站API或采集器 | **✅ 主链完成（默认关闭）** —— `#62`（存储/审核门禁/注入/工具）、`#65`（WebUI 审核页 + B站采集器 + `news_submit` 提取工具）。**剩余**：qzone 说说话题池未接；`inject_window_days` 尚未参与过滤；B站接口风控需真机观察 |
| **REQ-7** | **Bot OOC 行为分析与缓解方案** | 针对未闭合/缺失 `<think>` 思考过程、中英混杂、复读死循环、长文本失控、过度模板化等问题，通过 Prompt 调优、输出过滤（`reply_filter.py`）、Repetition Penalty 与流式截断系统性治理。 | 需真实 bad case 与模型实测 | **🟡 部分完成** —— `#60`（无标签自查/推理块外泄治理，Closes `#57`；采样惩罚参数已暴露默认 0）、`#66`（中英混杂：保守清理句首英文填充词与 AI 套话）。**剩余**：惩罚参数推荐值需真实模型实测；复读/模板化尚无系统性 bad case 库 |
| **REQ-8** | **Fake OneBot 契约测试环境** | 搭建本地 Fake OneBot v11 反向 WebSocket 服务端/客户端测试框架，模拟网络延迟、断连、echo 丢包、非法字段、乱序推送等极端边界，脱离真实 QQ 进程依赖。 | 基于 `websockets` 编写 harness | **✅ 已完成** —— `#56`：`tests/fake_onebot.py` + 9 条真实 socket 契约测试（多账号/未绑定/乱序 echo/丢 echo/缺 echo/非法 JSON/强杀断连/双账号同 id）；3 个变异各精准命中 |
| **REQ-9** | **性能与 I/O 效率优化（history 双写）** | 基准定位真实瓶颈：序列化仅 ~1.4%，`flush` 是主成本；批量化可省 ~57% 但引入崩溃丢失窗口。 | 需维护者决定是否接受丢失窗口 | **🟡 调研完成，待决策** —— `#54` 记录实测数据与三个候选方案（A 保持现状 / B 时间窗批量 / C 默认关闭的批量路径），**未改持久化语义** |

### 2.1 REQ-9 实测结论：history 双写瓶颈不在 JSON 序列化

2026-10-19 在隔离快照（`mkwork` + `run_in`，`guard_loaded=1 blocked_network=0`）上用真实归档事件做基准，N=3000 条、群聊双写（合并文件 + 旧布局）两个目标：

| 方案 | 耗时 | 相对 |
|---|---:|---|
| 仅 `json.dumps`（3000 次） | 0.060s | 占整体 ~1.4% |
| 写文件、不 flush（两文件） | 2.533s | — |
| 每条写 + flush（当前实现） | **4.262s** | 基线 |
| 写 + 每 50 条 flush 一次 | **1.847s** | **省 ~57%** |

**据此撤回了一个看似合理的优化**：先前的「复用同一次 `json.dumps` 结果给两个 writer」端到端实测 `speedup=0.95x`（即无收益，落在噪声内），因此**没有合入**——序列化不是瓶颈，不值得为此增加 `JSONLWriter.append_raw_line` 这类新 API 表面积。

**真正的杠杆是 flush 频率**（每条 flush → 每 N 条一次，实测省 ~57%），代价是崩溃时可能丢失最近 N 条归档（当前语义是「每条落盘即可见」）。该取舍会改变 history 的持久化强度，且 review 面板按文件增量读取，**必须由维护者拍板**后再动，不擅自实施。

可选方案（供维护者选）：
- **A. 保持现状**：最简单、最安全，热路径开销可接受（3000 条 4.3s ≈ 1.4ms/条，实际群聊速率远低于此）。
- **B. 时间窗批量化**：如每 100ms 或每 50 条 flush 一次，配 `close()` 时排空；需评估 review 面板读侧的可见性延迟与崩溃丢失窗口。
- **C. 仅对新写入路径批量**：在压测证明是瓶颈后再启用，默认关闭。

---

## 2. 当前 PR 与分支状态

> 当前 `origin/main` = `a361736`（**冻结**）；`origin/dev` 以 `git log -1 origin/dev` 为准（不硬编码，避免每次提交后失同步）。main 不再接收日常改动；发布时再单独决定 `dev → main`。

| PR 编号 | 目标分支 | 简要内容 | 审查/合并状态 |
|---|---|---|---|
| `#44` | `main` | 群聊 `@` 定向指令分发 | 已合并（`9f95d2e`） |
| `#45` | `main` | Review worker 请求超时与重试逻辑加固 | 已合并（`821176b`） |
| `#46` | `main` | 空白/纯符号消息替换为语义标注 | 已合并（`148c264`） |
| `#47` | `main` | 多来源歌曲知识库同步、字段合并与模糊识别 | 已合并（`6d8a2da`） |
| `#49` | `main` | 修复多来源歌曲同步并发竞态（锁内提交与原子 upsert） | 已合并（`147651d`） |
| `#50` | `main` | 历史纯符号条目改为占位标注而非丢弃（`#46` 跟进修复） | 已合并（`a361736`） |
| `#43` | `main` | 历史消息跨账号去重实验分支 | 经生产数据验证不构成实际冲突，已关闭 |
| `#51` | `main` | TODO 全量重构（REQ-1~9 立项） | 已合并（`51b4389`） |
| `#52` | `main` | 知识库分支审查与集成（`mohobot/knowledge/`） | 已合并（`6ec669f`） |
| `#53` | `main` | README / DEVELOPMENT / AGENTS 全量同步 | 已合并（`2657cdc`） |
| `#54` | `main` | history 双写性能实测结论（瓶颈在 flush，非序列化） | 已合并（`0fcd536`）→ 后续随 `dev` 回退撤出 main |
| `#55` | `dev` | 识图 `direct` 模式（图片直传主模型，失败回退 describe） | 已合并（`81ef77f`） |
| `#56` | `dev` | 真实 socket OneBot 协议契约测试（9 条 + 3 变异） | 已合并（`94e7c7c`） |
| `#58` | `dev` | 冻结时钟修复 qzone 时段性失败（00:00-00:01 / 23:59） | 已合并（`9634aa6`） |
| `#59` | `dev` | REQ-3/6/7 系统技术设计规范（`docs/designs/`） | 已合并（`852c466`） |
| `#60` | `dev` | OOC：无标签自查/推理块外泄治理 + 采样惩罚参数 | 已合并（`601485e`），Closes `#57` |
| `#61` | `dev` | 表情包：移除概览式偷图 + 工具抓取默认待审 | 已合并（`e8f8b6a`） |
| `#62` | `dev` | 新闻知识库后端（审核门禁 + 注入 + 工具，默认关） | 已合并（`2b107df`） |
| `#63` | `dev` | TODO 状态同步（本轮 REQ-3/6/7） | 已合并（`f4d883f`） |
| `#64` | `dev` | 表情包 `allowed_bots` 适用 bot 白名单 + 检索过滤 | 已合并（`247fda8`） |
| `#65` | `dev` | 新闻 WebUI 审核页 + B站采集器 + LLM 主动提取 | 已合并（`7d91b45`） |
| `#66` | `dev` | 中英混杂治理（保守清理句首英文填充词与 AI 套话） | 已合并（`a53effe`） |
| `#67` | `dev` | relationship：审批卡片注入防护 + 批量任务生命周期 | 已合并（`66ecc87`），Closes `#7` |
| `#68` | `dev` | 补记 `vision_mode` 两种识图模式 + 修正过期注释 | 已合并（`90d9dda`） |
| `#69` | `dev` | 历史热维护写入屏障 + review auto 消息 ID 登记 | 已合并（`0d3d994`），`#8` 部分 |
| `#70` | `dev` | 插件策略消费：`bot.enabled`、离群剔除、主动聊天封禁、表情回执 | 已合并（`1eb2c06`），Closes `#9` |

### 2.2 开放 Issue 处理记录（2026-10-20）

| Issue | 级别 | 处理结果 | 对应 PR |
|---|---|---|---|
| `#7` relationship 拉黑生命周期 + 审批文本注入 | P1/P2 | **已关闭**：注入双层防护（渲染净化 + 解析首现优先）、批量任务纳入 `on_shutdown`；「踢人拉黑」经核对已是正确实现（既有测试覆盖） | `#67` |
| `#9` 插件策略（回执/上限/生命周期） | P2 | **已关闭**：消费 `bot_config.enabled`、`group_decrease` 剔除抽签缓存、主动聊天热更新签名 + 封禁检查、表情包离线回执；qzone 单帖上限经核对已修 | `#70` |
| `#8` review 面板缓存/计数/竞态 | P2 | **已关闭**：`#69`（热维护写入屏障 + auto 消息 ID 登记）+ `#71`（inode 缓存身份、半行 offset 边界、待审计数求交、前端会话代际） | `#69` `#71` |
| `#6` meme 作用域过滤与图片来源边界 | enhancement | **已关闭**：列表按事件作用域过滤（P1 跨群泄漏）、`steal_meme` 仅允许当前/引用消息内图片、`source_type` 统一 `strip().lower()` | `#72` |
| `#2` SQLite 旧库 WAL 迁移 | wontfix | 维护者此前表示暂缓，保持 wontfix | — |

> 截至 2026-10-22：开放 issue 仅剩 `#2`（wontfix），开放 PR 为 **0**；本轮 `#71`~`#75` 均已合并。

### 2.3 本轮新增能力与基础设施

| 项 | 状态 | 说明 | PR/提交 |
|---|---|---|---|
| LLM 多提供商 | ✅ 已合并 | 每套 `base_url+api_key+default_model` 可并存(snowluma/deepseek)；全局 `chat_provider/vision_provider/emotion_provider`，每 bot 可用 `*_provider_override`；不配 providers 时行为与旧版完全一致；WebUI「模型配置 → 多提供商」可增删改，Key 掩码且留空保留原值 | `#73`（`c88bc20`） |
| 测试提速 | ✅ 已合并 | `pytest-xdist` + CI `-n auto --dist loadfile`；本地全量 80s → 17s（`-n 8`）；修 `test_wifepicker` 顺序依赖；串行慢的根因是真实 sleep（qzone 防刷 5.07/4.21/2.96/2.70s、plugin tick 3.5s、login 0.5s 硬延迟等，前 30 项占 72%） | `ba0ec66` |
| 测试耗时调查 | ✅ 建档 | `.venv/prep/report-test-speed.md`：模式对比、慢用例归因、runner 覆盖缺口（707 vs 774 顶层函数，`test_ban_pipeline`/`test_plugin_reload` 记 PASS 但 0 用例）、后续建议 | 本地文件 |

**待维护者决策**：CI 的 legacy runner 是否改为只在 push main 时跑（PR 用 pytest 为准，预计 PR CI 再省 ~90s）。

### 2.4 多提供商安全热修（PR #74）

独立审查（`.venv/prep/reviews/pr73/INDEPENDENT.md`）在 #73 合并后发现 **P1**：`GET /api/config` 明文返回 provider `api_key`；另有 4 个 P2（无效 provider 跳过全局回退、provider client 不退休、bot `vision_provider_override` 未接线、`emotion_provider` 未接线）与 P3（空 key 跨端点继承、`models` 被清空、前端缓存）。

PR #74 已全部修复并合入 `dev`（`9bfc27b`）：新增 `tests/test_provider_hardening.py` 10 条，7/7 变异杀死；全量 798 passed、runner 89/0、ban/reload/concurrency 单独 OK、smoke OK。

**教训（已写入流程）**：PR 审查改为**独立子代理先审**再合并；自身测试容易漏"同一改动在其它出口的影响面"（本例是 `/api/config` 这个既有出口）。

### 2.5 多提供商跟进修复（PR #75 + 收尾）

独立复审 #74 后跟进（PR #75，已合并）：
- **N14（P2 安全，已修）**：`chat_base_url` 为空时空 key provider 会把主 Key 发往第三方端点 → 仅"未写 base_url"或"同端点"才继承；
- **N13（P2，已修）**：仅 per-bot `vision_provider_override` 时识图静默失效 → 可用性改为"任一 provider client 即可用"，不可用时打 warning；
- **N15（P3，已修）**：provider client 不再每次保存都重建（identity 缓存复用）；顺带修掉 `_provider_identities` 命名冲突导致的 `'dict' object is not callable`；
- **N16/N17（P3，已修）**：补 `close()` 关闭当前 provider client、`_vision_callback`/`MessageHandler` 传 bot 配置的承重测试；
- **N18~N28（P3，已修）**：loguru `%s/%r` 不插值、示例注释、前端 provider 删除/缓存为空时的选项、测试替身签名等。

遗留（P3，接受现状）：N21 `_provider_specs` 对缺字段 SimpleNamespace 的容错；N22 ImageCache 按图片共享描述，per-bot vision 只在首次识别生效（缓存层设计，需单独排期）。

交接前实测（`dev` 见上）：全量 **810 passed**、runner **91/0**、ban-pipeline / plugin-reload / concurrency 单独 OK、smoke OK、compileall 0、Ruff 门禁 0、表情包插件 **503 passed / 2 skipped**、面板内联 JS 语法 OK。

### 2.6 移交前收尾（2026-10-22）

| 项 | 内容 | 提交 |
|---|---|---|
| snitch 插件加载失败 | `llm_tools.py` 已把 `plugins/snitch.py` 作为内置工具导入注册，插件系统再加载同名文件 → `duplicate LLM tool`；注册改为幂等 | `d826da7` |
| 面板创建 Bot 不接管在线连接 | `create_bot(qq=...)` 只写配置，已连接的 QQ 仍是「未绑定」→ 消息被忽略；改为创建后立即 `bind_qq` 晋升连接 | `a494a91` |
| 本地空歌曲库假失败 | `test_real_db_dialogue_cases` 在库存在但为空时执行并失败 → 空库跳过 | `9e26b90` |
| 多提供商功能 | `#73` 多提供商 + `#74` Key 泄漏热修 + `#75` 跟进修复，均已合入 `dev` | `#73`~`#75` |
| 审核面板 | 本机已建 `review/config.yaml`（git 忽略，不入库），启动自动拉起 9091 | 本地文件 |
| 识图 direct | 本机 `vision_mode: direct`，真 Key 实测可用 | 本地配置 |

**本机真机验证（2026-10-22）**：
- SnowLuma（QQ 2774641554）→ `Bot bot_001 connected`；私聊消息触发 `LLM stream call: model=deepseek-chat` 并正常回复；
- 群消息未 @ 时正确跳过（`Skipping group message (not mentioned)`）；
- 真 Key 识图：`deepseek-chat` 读出图片文字 `HELLO 42`（`direct` 模式）；
- 启动/停机正常；审核面板配置可被 `verify_password` 校验通过。

### 2.7 文案与配置模板清理（2026-10-23）

- 移除已停用模型 `deepseek-flash` / `DeepSeek-V4-Flash` 在文档与示例配置中的残留（README、TODO、`config/global.example.yaml` 的 `chat_model` / `models` / `emotion.burst_model`）；
- **修复示例配置既有缩进 bug**：`news:` 顶格插在 `llm:` 块中间，导致其后 `emotion_*` 与 `models` 被 YAML 吸进 `news`（照抄模板会静默失效）。已把 `news:` 移到 `llm:` 之后，`GlobalConfig.load(example)` 验证 `llm.models/emotion_model` 与 `news.*` 均正确。
- **待决策（未改代码）**：`mohobot/models/config.py` 的默认值仍含 `DeepSeek-V4-Flash`（`chat_model`、`models` 列表、`emotion.burst_model`）。改默认值属行为变更，需维护者确认后单独 PR + 回归。

**遗留（P3，不阻断移交）**：`_provider_specs` 对缺字段测试替身的容错；ImageCache 按图共享描述导致 per-bot vision 仅首识别生效；`snitch` 内置注册与插件加载共用同一文件（现幂等，行为等价）。


### 2.8 维护者决策记录（2026-10-23，横向对比后的改进项取舍）

> 背景：与 AstrBot / LangBot / NoneBot 公开能力对比后，列出 4 类可补强方向；维护者按「个人自用」定位给出取舍。

| # | 改进方向 | 维护者决策 | 处理方式 |
|---|---|---|---|
| 1 | **发布/部署体验**：Docker Compose、Windows 一键安装、正式 systemd/healthcheck、对外部署文档 | **不做** | 自用为主，无对外分发/部署需求；仅保留现有 README 的 Ubuntu 部署说明，不再投入 |
| 2 | **安全基础加固**：登录锁 DoS、失败锁定/IP 限流、用户名计时枚举、PBKDF2 升参、登录失败 IP 审计、安全响应头 | **可修可不修**（不阻塞发布） | 审计证据保留在 `.venv/prep/security-audit-login-2026-10-22.md`；面板仅 localhost/SSH 隧道使用时风险有限，按需再排期 |
| 3 | **运维观测**：`/healthz`、`/readyz`、LLM 成功率/延迟、provider 失败统计、告警 | **可做** | 列入后续可选优化；不阻塞发布 |
| 4 | **知识库/新闻真实验收**：RAG embedding 实调、B站采集器风控观察、`inject_window_days` 过滤生效验证、真实 bad-case 库 | **可做** | 列入后续可选优化；真 Key/真环境就绪后逐项验证 |

**结论**：发布阻塞项只剩「REQ-9 决策 / CI runner 口径 / dev→main 发版授权」三项；第 1 类明确关闭，第 2 类降级为按需，第 3/4 类保留为可选优化。

## 3. 分支模型与规范

| 分支 | 职能 | 说明 |
|---|---|---|
| `main` | 生产发布分支 | 唯一稳定发布线，CI 自动门禁校验，生产环境拉取基线。 |
| `dev` | 团队集成分支 | 多特性或多修复先行联调验证分支，经审查通过后通过 PR 汇总至 `main`。 |
| `feat/<topic>` | 特性分支 | 新功能开发分支，禁止直接推送到远程，通过 PR 提出。 |
| `bugfix/<topic>` | 缺陷修复分支 | 缺陷修复专用分支，附带 RED/GREEN 回归测试。 |
| `docs/<topic>` | 文档及待办分支 | 文档重写、规范梳理及待办更新分支。 |

**工作铁律**：
- 严格遵循 **本地修改 -> 本地验证/变异测试 -> 提交功能分支 -> PR 审查（pr-review-report） -> CI 全绿 -> 合并至 main -> 同步 dev**。
- 绝不直接向 `origin/main` 或 `origin/dev` 进行未经审查的直接 push。

---

## 4. 测试与质量基础设施进展

- **CI 门禁规范**：
  - Python 3.12 官方基准：逐用例测试、`tests/_run_all.py` 完整 runner、独立并发测试 `test_concurrency.py`。
  - 本地快速口径（已装 `pytest-xdist`）：`pytest -q -n auto --dist loadfile tests --ignore=tests/test_concurrency.py`，全量约 **20~27s**；`--dist loadfile` 用于规避同文件顺序/模块状态依赖。
  - 代码质量：Ruff 基础阻断门禁（`E9, F63, F7`），增量债务报告（`E4, E7, E9, F`）。
  - 插件回归：`plugins/meme_stealer/` 专用轻量测试套件。
- **环境缺陷与待修复项**：
  - ~~`test_qzone.py::test_auto_publish_tick_end_to_end` 时钟硬编码~~ —— **已修（`#58`）**：整段测试冻结在固定时刻，8 个时刻矩阵（含 00:00/00:01/23:59）全部通过；生产代码未改。
  - **代理环境变量容错（未修，本机实测）**：两个独立坑 ——
    ① `ALL_PROXY=socks://127.0.0.1:7890/`：新版 httpx/httpx2 不支持 socks scheme，启动在初始化 LLM 客户端时直接 `Fatal error: Unknown scheme for proxy URL`；测试里表现为 `test_anysearch` / `test_webpanel_stop` 失败。
    ② `no_proxy` 含 `[::1]`：httpx2 把它当 `host:port` 解析 → `Invalid port: ':1]'`。
    绕过（本机已验证）：
    ```bash
    env -u ALL_PROXY -u all_proxy \
        NO_PROXY="localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16" \
        no_proxy="localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16" \
        .venv/bin/python main.py
    ```
    根治方向：启动器/测试引导里净化这两类变量（未实施，属 P3）。
  - **既有 Ruff 债务（未清理）**：`mohobot/message_handler.py` 的 `extract_plain_text` F401、`mohobot/llm_service.py` 的 `MessageSegment` F401 与 `since` F841、`mohobot/bot_manager.py` 的 3 条 `websockets` F821、`plugins/relationship/relationship_core/request/model.py` 的 `field` F401 —— 均为基线既有，按「最小改动面」原则未在本轮清理。
