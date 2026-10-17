# TODO

项目待办清单。每一项都尽量链接到对应的 Issue / PR，并写明**如何验证已完成**。
实现但尚未合入时保留条目并注明 PR；维护者合入后删除已完成条目，历史留在 PR 中。

> 历史基线：2026-10-14 `main` = `b4548cd`；当前 `main` = `bd9fbd2`。所有历史数字都绑定其基线提交，不作为当前验收结论。

## 1. 当前仍开放的 PR

> 快照：2026-10-17。`main` = `0e87ea2`，`dev` = `24f62e2`。这里只列 GitHub 当前 OPEN 且尚未进入 main 的 PR；已关闭或只存在于 `dev` 的 PR 归入 §1.3 历史表，不再出现在待合入队列。

| PR | 目标 | 内容 | 状态 |
|---|---|---|---|
| #17 | `main` | 删除 `RequestConfig._clean()` 后残留的 4 行不可达代码 | 审查 PASS；内容已在 `dev`；PR 的 base 仍是旧 main，合入前需按当前 main 复核 |
| #20 | `main` | 本 TODO 清单 | 本轮修正后 head `ff6352b`，待复审 |
| #33 | `main` | 第三批低风险 Ruff 告警清理 | 审查 PASS；内容已在 `dev`；PR 的 base 仍是旧 main，合入前需按当前 main 复核 |
| #38 | `dev` | 分支模型（master/dev/feat/bugfix/gh-pages）与 CI 迁移期双分支监听 | PASS；P2 已修，待维护者审查 |
| #37 | `main` | dev 集成 PR | draft，待 #39/#38 合入 dev 后更新 |
| #39 | `dev` | 非流式工具轮无文本兜底改为 LLMDiagnostic | PASS，待合入 dev 后刷新 #37 |

### 1.1 分支模型（目标态，带前置条件）
| 分支 | 用途 | 生效条件 |
|---|---|---|
| `master` | 目标发布分支 | 仅当管理员把默认分支 `main` 重命名为 `master`、并配置好分支保护后才作为发布线；在此之前生产仍按 `main` 操作 |
| `main` | 迁移期兼容分支 | 与 `master` 同步；切换完成后可保留只读或删除 |
| `dev` | 集成分支 | 汇聚已审查 PR 做联调与 CI 预验收，不对应生产 |
| `feat/<topic>` | 功能分支 | 例 `feat/ctx-restore`；走 PR |
| `bugfix/<topic>` | 缺陷修复分支 | 例 `bugfix/outbound-cancel`；走 PR |
| `gh-pages` | Pages 静态站点 | 仅在确有 Pages 产物时创建 |

约束：

1. `main → master` 是仓库引用操作（重命名默认分支、改分支保护），**文件 PR 无法完成**，需管理员在维护窗口执行；PR #38 只改文档与 CI 触发配置。
2. CI 迁移期双监听：`ci.yml` 与 `compat.yml` 的 `push` 同时监听 `main` 与 `master`，切换前后都不会出现门禁空窗；切换完成后可移除 `main`。
3. Git **不允许** `feat` 与 `feat/<topic>` 并存（`refs/heads/feat` 与 `refs/heads/feat/*` 冲突）；若要用字面 `feat`/`bugfix`，必须先把 `feat/knowledge-base` 等嵌套分支改名。
4. 分支名不带前导 `/`：写 `feat/login`，不是 `/feat/login`。

### 1.2 工作约定
- 所有改动走 **分支 → PR → 审查 → 合并**；即使有 push 权限也不直接推 `main`/`master`，也不在未授权时直接创建/重命名远程分支。
- 每个修复 PR 附带：先失败后通过的回归用例（RED/GREEN）、全量根回归结果、触发条件的如实说明。
- 改动消息发送 / 上下文 / 存储语义时，补跑 `tests/test_concurrency.py`（根 runner 不包含它）。

### 1.3 已关闭/已整合 PR（历史，不要重复合入）
#18 #19 #22 #23 #24 #26 #27 #28 #29 #30 #32 #34 #35 #36 已关闭，其内容已进入 `origin/dev@24f62e2`。这些 PR 及远程分支不属于当前待合入队列；需要恢复时应从本地保留分支重新推送，并先确认不会重复合入。

### 1.4 dev 集成历史（旧快照）
历史记录：旧 dev 快照曾从 `origin/main@2b95bf0` 起步并入维护者提交至 `bd9fbd2`，当时领先 main 较多提交。以下数字属于那次历史快照，不代表当前 dev 或当前 main。

实测（隔离快照，含门禁用例后）：

| 入口 | 结果 |
|---|---|
| 裸 `pytest tests`（靠 `pytest.ini` 的 asyncio auto） | **610 passed**（当时 main 基线：5 failed / 589 passed） |
| 根 runner `tests/_run_all.py` | **69 passed / 0 failed**（main 为 65/1；issue #10 已修） |
| `tests/test_concurrency.py` | 全部通过 |
| `ruff E9,F63,F7` / `compileall` | 通过 |

**#18 的 P1 合并风险已由测试把守**（本轮最重要的落地）：在 #18 分支新增用例 `test_nonstream_tool_followup_failure_keeps_diagnostic_marker`（head `6c32dce`），断言非流式链式"跟进失败"出口返回 `LLMDiagnostic`，并用真实 `MessageHandler` 端到端断言诊断可见但**上下文/DB/情感零落盘**。变异验证：

- #18 分支原样：**8 passed**；把该出口改成普通 `str` → **1 failed**（杀死）；
- dev 合并态原样：**8 passed**；把它按 naive 解析改成普通 `str` → **1 failed**（杀死）。

即：如果将来有人把 #18/#19 的冲突解成普通字符串，这条用例会直接失败，而不是让 issue #4 的 P1 静默复活。

**独立复审（针对 head `6c32dce`）确认解除**：N1 [P1·合并流程] → **[Closed]**、N2 [P2] → **[Closed]**，结论 PASS。复审自己做了三向变异，其中 M3（只把 `message_handler.py:1259` 消费点改成 `if False:`、标记保留）→ 3 failed，证明端到端那半段确实承重，不是空转。naive 态下全仓根 runner 由 69/0 变 **68 passed / 1 failed**，而 #19 自带用例仍全绿——新用例是唯一红灯。

新增两条 P3：**N6**（PR 正文滞后：写"6 个用例/6 passed"，实际 8）→ **已修**，正文已更新为 8 个用例、补上"与 #19 的合并解析要求"一节；**N7**（门禁入口依赖：head 无 `pytest.ini`，裸 pytest 7 failed；`tests/_run_all.py` 与 #24 的 `asyncio_mode=auto` 下才执行）→ 记录，不阻断。

### 1.5 dev 集成分支现状
`origin/dev` 当前为 **`24f62e2`**（领先当前 `main@0e87ea2` 50 个提交；main 还有两条维护者提交未进入 dev）。该分支包含 16 个已审查通过的 PR：#17 #18 #19 #22 #23 #24 #26 #27 #28 #29 #30 #32 #33 #34 #35 #36。

- 合并方式：从 `origin/main@bd9fbd2` 逐个 `--no-ff` 合并；#18↔#19 的 `llm_service.py` 冲突按 `return LLMDiagnostic(...), tool_results` 解析。
- 实测（隔离快照）：裸 pytest **613 passed**；根 runner **69 passed / 0 failed**；并发专项、`ruff E9,F63,F7`、compileall 均通过。
- CI：draft PR #37（`dev → main`）旧 head 四项检查已全绿；PR #39 修复合入后需刷新 #37 再重跑。
- 未进入 dev：#20（本 PR，复审中）、#38（PASS，待合入 dev）、#39（PASS，待合入 dev）。

**已知缺口（合入后应修，非本分支引入）**：非流式"工具轮无文本"兜底在 #19 里是普通 `str`，而流式同一兜底在 #18 里是 `LLMDiagnostic` → 合并态两者不一致（后者不入上下文，前者会被当正文落盘）。修法：合并后把非流式那行也包成 `LLMDiagnostic`（本次 dev 的一次演练版本曾这么做并全绿，但为保持"纯合并结果可复现"未纳入）。

另外：**#24 最终 head `4fde47c` 的真实 CI 四项全绿**（Tests 3.12 2m46s / Syntax / Meme plugin / Ruff 报告），即本轮对 CI 自身的修改（compat `if: always()`、README 口径、lint 报告三分支）已被真实 Actions 验证。

> 说明：`dev` 是集成预览分支，**不是发布分支**，也**不改变**各 PR 仍需维护者合并的事实。

### 1.6 分支清理（2026-10-15）
历史：本地分支曾从 28 收敛到 19（当时 = 17 个开放 PR 分支 + `main` + `dev`）。删除的是：
- 已关闭 PR 的分支：#12 `fix/meme-list-scope`、#15 `fix/sqlite-legacy-wal-migration`、#21 `fix/chat-manager-partial-forward`、#31 `lint/core-unused-imports-2`（后两个同时删了 origin 远程分支）；
- 已合并 PR 的本地残留：#11 `fix/reply-filter-nesting`、#13 `fix/relationship-kick-blacklist`、#14 `fix/qzone-per-post-reply-cap`、#16 `fix/outbound-cancellation-state`（origin 侧早已随合并删除）；
- 历史快照：本地 `docs/handover-audit-main`、`docs/audit-handover-and-rules`（内容已重放进 #27 / #24）。

**保留**所有开放 PR 的分支——删除它们会直接关掉对应 PR。另外 `fork` 远程仍留有 #11–#16 的旧副本与一个非本会话创建的 `feat/knowledge-base`，未动。

### 1.7 审查发现 → 处理状态（2026-10-16）
**已修（代码/配置/文档）**

| 出处 | 发现 | 处理 |
|---|---|---|
| #18 复审 N1 [P1·合并流程] | naive 冲突解析会静默重开 issue #4 的 P1 | **Closed**：新增门禁用例 `test_nonstream_tool_followup_failure_keeps_diagnostic_marker`（head `6c32dce`） |
| #18 复审 N2 [P2] | 用例集不覆盖非流式跟进失败出口 | **Closed**（同上，即该用例本身） |
| #18 复审 N6 [P3] | PR 正文滞后（6 用例/6 passed、缺合并要求） | 已更新正文为 8 用例 + 新增「合并注意」一节 |
| #19 N1 [P2] | 非流式工具轮无文本 → 用户静默 | **已修**（head `0e87563`）：回落 `[工具调用完成，但模型未返回文本]`，与流式一致；原固化 `reply == ""` 的用例已改断言 |
| #19 N2 [P3] | 空 `choices` 丢弃已累积正文 | **已修**：改为 `break` 保留已累积文本 + 新增用例 |
| #23 N1 [P2] | issue #10 第 2 条「全静默跳过」零覆盖 | **已修**（head `b8a077d`）：新增 `test_merged_reply_all_bots_silent_sends_nothing`；变异「全静默仍发占位转发」由 5 passed 变 **1 failed**（原先逃逸） |
| #24 N3 [P2] | compat 步骤截断、3.10 信号只剩半份 | **已修**：runner/concurrency 加 `if: always()` |
| #24 N4 [P3] | job 超时 < 各 step 之和 | **已修**：py310 job 15m → 30m |
| #24 N7 [P3] | README 仍称 3.10+ | **已修**：标注「3.12 为 CI 实际验证版本；3.10/3.11 尚未验证」 |
| #24 N8 [P3] | lint job 会把「没跑成」报成 0 条 | **已修**：三分支（有告警/无告警/Ruff 未正常完成），用 stderr 判断；已实测三种情况 |
| #24 N1 [P2] | PR 自述与合并事实不符 | 已更正正文（merge-base 仍 `b4548cd`、三点 diff 不收缩、携带 #23 旧版断言） |
| #28 归因 | 3.10 停滞被归因「跨用例累积」 | 已修 `docs/DEVELOPMENT.md` + `compat.yml` 注释为真因 issue #25 |
| #22 N1 [P3] | PR 正文 67 passed 未注明异步配置 | 正文已补复现命令与解释 |
| #26 N3 [P3] | 口径不一致（67/1 vs 66/1、pytest 需 auto） | 已在 PR 评论中更正 |
| **#28 N1/N2 [P3]** | `enqueue_timeout_sec` 成无效配置却仍被示例配置/文档宣称；`except QueueFull` 不可达、`self._enqueue_timeout` 死状态 | **已修（head `299fb9c`）**：删前置 `queue.full()` 预检，让 `put_nowait` 的 `QueueFull` 成为唯一快速失败路径；删死属性；构造参数保留但 docstring 说明不再影响行为；示例配置 / config 字段注释 / DEVELOPMENT 均标「已废弃、不再生效」。探针实测立即失败 `elapsed=0.000077s`、死属性消失；5 passed |
| **#26 N2 [P3]** | 部分失败提示把「读取条数」当「已转发条数」，高估投递（实测投递 3/4、文案 5） | **已修（head `8469ece`）**：改为如实报批次 `成功 {ok} 批 / 失败 {failed} 批（本次读取 {len(messages)} 条, 失败批次中的消息可能未送达）`；回归加 `assert "已转发 3 条" not in reply`；变异回旧文案 → 1 failed；21 passed |

| **#28 复审残留（N1 尾行 / 新增 N4）** | `DEVELOPMENT.md` 8.2 节仍写「入队超时也转成这个异常」（与 399 自相矛盾）；快速失败分支零测试 | **已修（head `5a1fbdd`）**：8.2 节改为「已废弃、不影响行为」；新增 `test_full_queue_fails_fast_with_outbound_queue_full`（`queue_maxsize=1`，立即抛错、耗时 <0.1s、失败项不入队），变异「满队列改为等待 0.5s」→ 1 failed；6 passed |

**复审结果**：#28 复审（head `299fb9c`）**PASS**，N2 Closed、N1 部分关闭（尾行已修）、N3 维持开放、新增 N4（快速失败零测试，已补）；#26 复审（head `8469ece`）**PASS**，N2 **Closed**、N1 维持 P3、N3 部分关闭、新增 N4（base 前移导致无法快进合并，非阻断）。

> **关于「改不改会不会让 review 失效」**：这**不是**不修的理由，正确做法是**改完再复审**。本轮已据此执行：#18 补门禁用例后复审 PASS（N1/N2 Closed）、#29 换 head 后复审 PASS、#28 与 #26 改动后各自安排了复审。只有三类是**真延期**：① 目标文件正被其它未合并 PR 重写（`llm_service.py` 的死桩与未使用导入）；② 改了会立刻让门禁变红（#24 给阻塞集加 `F821`，而现有 F821 未清完）；③ 需要维护者拍板（#18 N3「失败轮是否连用户消息一起不落盘」）。

**记录为后续项**

- **#28 N3 [P3]**：同类「`wait_for` 包住可能同回合完成的 awaitable」模式在 `close()` 及仓内其它位置仍存在（超出本 PR 范围）。
- **#24 N2 [P2]**：阻塞 Ruff 只选 `E9,F63,F7`，`F821/F811` 不在门禁；待 #17/#26/#29/#32 清掉现有 F821 后（剩 `bot_manager.py` 3 处字符串注解），可把 `F821` 纳入阻塞集——它在本仓抓到过真 bug（`chat_manager` 的 `nodes`）。
- **#24 N5/N6 [P3]**：`requirements.txt` 全 `>=` 不可复现、缺 `pip check`；Actions 未按 SHA 钉版本。
- **#26 N1 [P3]**：回归替身 stub 掉真实分批路径（真实分批已由独立审查端到端探针覆盖：全成功/全失败/中间批失败/最后不满批次），保留为后续项。
- **#18 N3/N4/N5 [P3]**：失败轮连用户消息一起不落盘（待维护者确认是否有意）；`emotion/manager.py` 前缀兜底冗余；早退跳过 `ab_service.note_context`（当前不可达）。
- **#19 N3/N4/N5 [P3]**：`max_tokens` 上限对齐、后续轮异常覆盖正文口径、新文件裸 ruff 告警（RUF100/RUF059）。
- **知识库 P3**：`mohobot/llm_service.py` 的 `MessageSegment` 未使用导入 + 重复死桩的 `since`（`_compute_session_usage` 定义两次），待 #18/#19 合入后清理。

### 1.8 合并顺序（历史快照，已完成）
> 以下顺序是 2026-10-15 的合并历史：`#22/#23 → #24 → 独立修复 → #18/#19`，已全部整合进 `dev@24f62e2`，**不要按此表重复合入**。

当前可执行路径：

1. 维护者审查本 PR（#20）与 #38（分支模型），按需先合 #38 到 `dev`；
2. 管理员在维护窗口完成 `main → master` 重命名与分支保护配置；
3. 审查 draft PR #37（`dev → main`，或切换后 `dev → master`）后再进入发布线。

**文件重叠矩阵**（历史，仅作冲突排查参考）：

| 文件 | PR | 后果 |
|---|---|---|
| `mohobot/llm_service.py` | #18、#19 | 曾真冲突 1 块，已按 `return LLMDiagnostic(...), tool_results` 解析 |
| `tests/test_merged_group_reply.py` | #23、#24 | #24 携带旧版断言；三分合并保留 main 侧强版本 |
| `tests/test_qzone.py` | #22、#24 | #24 副本与 #22 逐字节相同，合并为空操作 |

维护者已表态暂缓、相关 PR 已关闭：

- #12（meme 列表作用域）——「meme 后面要大改一次……相关的先不用修了」
- #15（旧库 WAL 迁移）——「感觉没什么必要，先不合并吧」

### 1.9 开放 PR 收敛记录（2026-10-16，历史）

**平台事实**：GitHub **不支持删除 PR**（`gh pr` 只有 `close`/`reopen`，REST 也没有删除端点，实测 `DELETE /repos/.../pulls/24` 返回 404）。能删的只有分支，关闭的 PR 会永久保留在 Closed 列表里。

**已执行**：把已整合进 `dev` 的 14 个 PR **关闭 + 删除其远程分支**——#18 #19 #22 #23 #24 #26 #27 #28 #29 #30 #32 #34 #35 #36（每个 PR 留了说明评论，指向当时的 dev 集成快照）。

**仍开放（当时）**：#17、#33 从未关闭；后续又新增 #20、#37、#38、#39。

**可恢复性**：本地分支全部保留（含已关闭的 14 个），需要单独合并时 `git push origin <branch>` 即可恢复远程分支，再 Reopen PR 即可；`dev` 本身不受影响（当前 `origin/dev@24f62e2`）。

**副作用提醒**：`main` 上没有 `.github/`，**CI 目前只存在于 `dev`**（#24 的内容）。要让 CI 真正作用于 main，必须先把 dev（或至少那 5 个 CI/配置文件）合进 main。

## 2. 接入 CI

**上述 main 基线没有项目 CI。** 没有 `.github/workflows/`，也没有 pytest / ruff 配置或 `pyproject.toml`。`main` 未设分支保护。
（CI PR 之前的唯一 Actions 记录是 GitHub 托管的 Copilot PR 审查，属于平台功能。）

**当前 main 仍未接入 CI**；CI workflow 在 `dev`（来自已关闭的 #24），draft PR #37 的检查已全绿，但 main 自身没有 `.github/workflows/`。合入 #37 后 main 才会获得 CI。

### 2.1 已完成的前置项（历史）

- [x] **#10**：PR #23 将 `tests/test_merged_group_reply.py` 的占位断言更新为当前“静默 bot 不生成节点”行为；组合根 runner `66 passed, 0 failed`。
- [x] **4 个 qzone 用例**：PR #22 将 `test_session_fetch_and_cache` / `test_session_reset_clears_cache` / `test_session_invalid_uin` / `test_session_no_o_prefix_uin` 改为真正的 `async def`，pytest qzone `67 passed`，根 runner 不再出现其 never-awaited 警告。

PR #24 的 workflow 当前在 `dev`，main 尚未接入；draft #37 合入前需刷新至最新 dev。以下原始诊断作为历史记录保留：

- 原 qzone 用例是**普通函数却返回协程**（`return run()`）。
  - 根 runner 只对 `async def` 做 `await`，所以这 4 个协程被直接丢弃（日志里是 `coroutine '...run' was never awaited`），整个文件却记为 `PASS`；
  - pytest 下这 4 个直接报错。

  改成 `async def` 即可。验证：pytest 下 4 个通过，且根 runner 日志中不再出现这 4 条 never-awaited。
- [x] **根目录开发依赖清单**：PR #24 新增根 `requirements-dev.txt`，固定 pytest / pytest-asyncio / ruff；全新虚拟环境安装后 `pip check` 无冲突。
- [ ] **清理剩余静态告警**：当前 `main@0e87ea2` 实测 160 条、`dev@24f62e2` 实测 87 条（`E4,E7,E9,F`，忽略 E402），全部来自既有代码。CI 只阻塞 `E9,F63,F7`；完整 `E4,E7,E9,F` 由信息性 job 报告（计数进 step summary + artifact），避免把已知债务显示成 PR 的失败，也避免隐藏告警。

### 2.2 workflow 设计（已在 dev，未进入 main）

`main` 变绿后，新增 `.github/workflows/ci.yml`，`push main/master` 与 `pull_request` 触发：

| job | 内容 | 是否阻塞合并 |
|---|---|---|
| `tests` (Python 3.12) | 开发依赖安装、compileall、逐用例 pytest、根 runner、独立并发专项 | 是 |
| `meme-plugin` | 在 `plugins/meme_stealer/` 下按其 `CLAUDE.md` 跑 `pytest tests` 与 `ruff check` | 是 |
| `syntax` | Ruff `E9,F63,F7`：阻塞语法和控制流错误 | 是 |
| `Ruff debt report` | 既有 `E4,E7,E9,F` 告警：计数进 step summary + 完整 artifact；job 保持绿色 | 否（信息性，不隐藏告警） |
| `compat.yml` (Python 3.10) | 手工触发 / 每周日 / `push main/master`，带线程栈与日志 artifact | 否（刻意不挂 PR） |

选型依据：

- **Python 3.12**：生产实际运行 3.12.13（README 部署章节）；
- **3.10 / 3.11**：README 声明支持 3.10+，但兼容性**未验证**——套件会挂死，已定位到根因并开 **#25**（见第 3 节 P2）。放在 PR 门禁上只会让每个 PR 变红，因此移到独立 `compat.yml`（手工触发 / 每周日 / `push main/master`），保留线程栈与日志 artifact 继续采样；#25 修好后可把 3.10/3.11 重新纳入；
- **两个 runner 都跑**：根 runner 是项目文档里的入口，但它按**文件**计数、文件内首个失败即中止、并且会漏掉"返回协程"的用例；pytest 能逐用例报告。等 pytest 稳定后可以只保留 pytest。

当前基线耗时（本地）：根 runner 约 70s，pytest 约 86s，meme 插件约 8s。

### 2.3 之后再做

- **Ruff 债务进度**：`main@b4548cd` 基线为 160 条（`F401` 83、`F821` 25、`F841` 20、`F541` 12、`E741` 8、`E731` 5、`E401` 3、`E702` 2、`F811` 1、`E701` 1）。已按小批次清理：#30（测试 5 文件）、#32（核心 6 文件）、#33（3 文件）、#34（persona_ab）、#35（wifepicker）、#36（核心 7 文件）。**全部合入后预计剩余约 90 条**，主要是未触碰文件的 `F401`（plugins/mohobot/tests）、`F821` 字符串注解与 `E731/E741`。
- **节奏建议**：当前已有 10+ 个开放 PR 且维护者尚未审阅。继续逐个开小 PR 会让待审队列更难处理，建议**先让维护者消化现有队列**，剩余告警再按"plugins 未使用导入 / tests 未使用导入 / 注解与风格"合并成 2–3 个稍大的 PR；不要用全仓 `ruff --fix` 一把梭（会把 24 条 unsafe fix 和无关格式变更混进来）。
- PR #24 已加 `pytest.ini` 统一 async 模式；后续可统一 ruff 配置，清理告警后再收紧 blocking rules；
- **裸 `pytest tests/` 的现状（复核实测）**：`main@0e87ea2` 上没有任何根 pytest 配置，pytest-asyncio 1.4 默认 strict，直接跑会得到历史口径 `378 failed / 221 passed`；加 `-o asyncio_mode=auto` 或等 #24 的 `pytest.ini` 合入后才正常。因此 #24 不只是"加 CI"，也是让仓库裸 `pytest` 可用的前置，建议排在 #22/#23 之后尽早合入。
- `main` 当前未设置分支保护（GitHub API 返回 404）；建议在 CI 合入后要求「CI 通过 + 1 人审查」才能合并。

## 3. 已确认的缺陷

| 级别 | 位置 | 问题 | 证据 |
|---|---|---|---|
| **P2** | `mohobot/services/outbound.py:125` 的 `asyncio.wait_for(queue.put(...))` / 首次由 #16 引入 | Python **3.10 与 3.11** 上，调用方在"入队完成→开始等结果"窗口被取消时，取消被 `wait_for` 吞掉：调用方拿不到 `CancelledError`，会永久挂起或返回自己以为已取消的结果。生产跑 3.12 不受影响，但 README 声明的 3.10+ 属未验证 | 已开 **#25**（含最小复现、任务栈、3.10/3.11/3.12 状态对照、`wait_for` 源码定位：`except CancelledError: if fut.done(): return fut.result()`；3.12 用 `asyncio.timeout` 重写后正常） |
| **P2** | `plugins/chat_manager/main.py:167` | `/查看聊天` 在部分批次失败时引用未定义 `nodes` | PR #26 直接基于 main 修复，独立审查中；历史探针明确 `NameError`，全成功/全失败正常 |
| P3 | `tests/test_new_features_2.py:316-323` | `run()` 开头就 `return`，后面的断言**从未执行**——这个用例实际已失效 | ruff `F821`（`handled` / `reply` 未定义） |
| P3 | `tests/test_tool_followup.py:206` | `__main__` 入口调用未定义的 `main()`，直接执行该文件会 `NameError` | ruff `F821` |
| P3 | `mohobot/llm_service.py:688-689` | **重复定义**：一个只有 `since = self._range_since(...)` 的 `_compute_session_usage` 死桩被 702 行的真实实现覆盖；死桩恒返回 `None`，若将来有人把真实实现挪走会静默出错 | ruff `F841`（`since` 未使用）；该文件正被 #18/#19 修改，**待其合入后单独清理**，避免冲突 |
| P3 | `plugins/perception/main.py:264` | 注释称"失败也缓存空值 60s"，但 `ttl` 算完从未使用，读取侧（第 249 行）固定用 `_GROUP_NAME_TTL`(600s) → 空结果实际缓存 600s，**意图未实现** | ruff `F841`（`ttl` 未使用）。删除变量行为不变，但真正的问题是缓存时长与注释不符，需单独评估是否实现 per-entry TTL |
| P3 | `tests/test_qzone.py:859,871` | `_PubFakeAPI` 定义了两个 `__init__`，第一个被静默覆盖 | ruff `F811` |
| P3 | `tests/test_neteasemusic.py:276` | 替身 `FailingAPI.get` 是 `async def`，而真实调用方是 `async with self._http().get(url)`。用例仍通过，但触发降级的其实是替身形状错误引起的 `TypeError`，而不是它想模拟的 `RuntimeError("connection refused")` | 根 runner 日志：`coroutine 'FailingAPI.get' was never awaited` |
| — | `mohobot/bot_manager.py:31,36,362`、`mohobot/file_store.py:19,23` | 字符串注解里引用了未导入的 `websockets` / `asyncio`，运行时无影响 | ruff `F821`；用 `TYPE_CHECKING` 导入即可消除 |
| — | `plugins/relationship/relationship_core/config.py:269-272` | 不可达残留代码 | 由 #17 修复 |

## 4. 尚未完成的 Issue

| Issue | 剩余内容 | 状态 |
|---|---|---|
| #4 | 诊断落盘（P1）、非流式工具断链（P2） | PR #18/#19 已在 dev；#39 待合入 dev |
| #7 | 审批文本换行注入；批量任务未纳入生命周期管理 | 被踢拉黑部分已由 #13 修复 |
| #8 | review 面板：历史缓存身份、作用域约束、审核状态并发 | 未开始 |
| #9 | 插件送达回执、运行时生命周期策略 | 单帖回复上限已由 #14 修复 |
| #10 | 根回归基线失败 | #23 已进入 `dev@24f62e2`；main 要等 #37 合入后才解决 |
| #25 | Python 3.10/3.11 取消被 `wait_for` 吞掉，调用方可能永久挂起 | 修复已在 PR #28；3.10/3.11/3.12 出站取消测试均 5 passed，已独立审查，待 #37/维护者验收 |
| #26 | chat-manager 部分转发失败时的 `nodes` NameError | 修复已进入 `dev@24f62e2`；待 #37 验收 |
| #2 | 旧库迁移丢 WAL | 维护者暂缓，PR #15 已关闭 |
| #6 | meme 作用域与图片来源边界 | 维护者表示 meme 将重写，PR #12 已关闭 |

## 5. 测试基础设施

- 根 runner（`tests/_run_all.py`）按**文件**计数，文件内首个失败即中止，后续用例的结果被隐藏；只收集模块顶层 `test_*` 函数（类内用例、只写在 `__main__` 里的用例不会执行）。
- meme 插件套件里 `tests/test_external_sources.py` 用 `8.8.8.8` 作"公网地址"样例，离线防护下记录到 1 次被拦截的外网 DNS 尝试，测试仍通过。CI 有网络时行为可能不同，需确认它不依赖真实网络。

## 6. 文档

- [ ] 仿照 [Agent-LuoTianyi 的 docs 组织](https://github.com/SheepLiu712/Agent-LuoTianyi/tree/dev/docs) 完善本项目文档体系：补齐/统一 `TODO.md`、代码地图、开发守则、规格文档模板、开发进程/实现计划、项目说明与贡献指南等职责。参考目录结构和文档职责，不直接复制其他项目内容；按模块和当前代码逐项核对。
- [ ] 交接与审计文档（`AGENTS.md`、`docs/HANDOVER.md`、`docs/AUDIT-2026-10-12.md`）曾整理到已关闭的 PR #27（分支 head `c17fea0`），但尚未进入当前 main；需要时从本地分支恢复或另行提交。审计快照基于 `b63ad48`，已标注历史边界；后续修复以当前 TODO、issue、PR 和 CI 为准。
- [ ] **审计完整性的已知缺口**：`/查看聊天` 部分转发失败的 `nodes` NameError（引入于 `115ac17`，早于审计基点 `b63ad48`）**没有被那份审计报告收录**，是后来从 CI 基线的 `F821` 检查发现的（修复见 #26）。说明该审计的 P1/P2 清单不是完备清单；若将来把 `AUDIT-2026-10-12.md` 当作"已审计范围"引用，需同时说明这一点。

