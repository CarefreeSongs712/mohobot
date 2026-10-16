# TODO

项目待办清单。每一项都尽量链接到对应的 Issue / PR，并写明**如何验证已完成**。
实现但尚未合入时保留条目并注明 PR；维护者合入后删除已完成条目，历史留在 PR 中。

> 原始事实基线：2026-10-14，`main` = `b4548cd`。历史静态检查数字基于此提交；PR #22–#24 的组合测试/CI 结果另列，不与 main 基线混用。

## 工作约定

- 所有改动走 **分支 → PR → 审查 → 合并**。即使有 push 权限，也不直接推 `main`。
- 每个修复 PR 附带：先失败后通过的回归用例（RED/GREEN）、全量根回归结果、触发条件的如实说明。
- 改动消息发送 / 上下文 / 存储语义时，补跑 `tests/test_concurrency.py`（根 runner 不包含它）。

---

## 1. 等待处理的 PR

| PR | 内容 | 备注 |
|---|---|---|
| #17 | 删除 `RequestConfig._clean()` 后残留的 4 行不可达代码 | 纯删除；修复 ruff `F821` |
| #18 | #4 P1：LLM 诊断文案不再写入上下文 / 数据库 / 情感分析 | 独立复审通过，流式诊断前正文顺序缺陷已修复 |
| #19 | #4 P2：非流式模式支持链式多轮工具调用 | 独立复审通过；与 #18 在 `llm_service.py` 有文本冲突，后合并的一方需 rebase |
| #20 | 本 TODO 清单与 CI 接入计划 | 按已验证测试和 GitHub PR 状态更新，待维护者审查 |
| #26 | chat-manager 部分批次失败时修复 `nodes` NameError | 直接基于 main，替代已关闭的叠放 PR #21；独立审查通过，待维护者合入 |
| #28 | 修复 Python 3.10/3.11 `wait_for` 吞取消 | 独立复核 PASS；3.10/3.11/3.12 出站测试均 5 passed，待维护者合入。`enqueue_timeout_sec` 参数暂保兼容但不再控制入队等待，后续可清理文档/配置 |
| #29 | 清理三个失效/错误形状的 P3 回归测试 | 新 head `35f75f4` 已补 `await run()`；独立复核 PASS，19 个 pytest、原生入口和 Ruff 通过，待维护者合入 |
| #30 | 第一批低风险 Ruff 测试债务清理 | 独立复核 PASS；5 个独立测试文件，12 个 pytest、原生入口、Ruff 均通过，待维护者合入 |
| #32 | 第二批低风险 Ruff 核心导入清理（替代已关闭的 #31） | 独立复核 PASS；直接基于 main，仅 6 个核心文件；29 个 pytest、Ruff 通过，待维护者合入 |
| #33 | 第三批低风险 Ruff 告警清理 | 独立核验 PASS；3 个文件，24 个 pytest、Ruff、compileall 通过，待维护者合入 |
| #34 | 第四批低风险 Ruff 清理（persona_ab） | 独立复核 PASS（base 6 条对应告警 → head 0）；17 个 pytest、Ruff 通过，待维护者合入 |
| #35 | 第五批低风险 Ruff 清理（wifepicker） | 独立复核 PASS（base 3×F841 → head 0，AST 确认无副作用）；16 个 pytest，待维护者合入 |
| #36 | 第六批：核心模块残留局部与导入清理 | 独立复核 PASS（7 文件命中 0 条告警、53 passed）；内含两处需单独处理的问题（见第 3 节），待维护者合入 |
| #22 | 修复 4 个未 await 的 qzone 会话测试 | 独立审查通过，67 个 qzone 用例通过 |
| #23 | 修正 #10 过时的 merged-reply 占位断言 | 独立复审通过；最新补充断言非选中 bot 不发普通回复 |
| #24 | GitHub Actions CI、根开发依赖、pytest 配置 | 依赖 #22/#23；真实 Actions 已全绿（3.12 测试 / 语法 / meme 插件 / Ruff 债务报告），3.10 移出 PR 门禁到 `compat.yml` |

**正式审查（pr-review-report 规范，2026-10-15）**：

| PR | 结论 | 要点 |
|---|---|---|
| #19 | PASS（0 P1 / 1 P2 / 4 P3） | N1 [P2]：非流式缺终态兜底——工具跑完但模型不返文本时 `chat()` 返回空串，`stream=false` 下用户完全静默，而流式有 `[工具调用完成，但模型未返回文本]`；新用例把 `reply == ""` 固化为期望，与"语义一致"自述矛盾（**base 同样为空，非本 PR 引入**）。已提交 review（COMMENTED） |
| #26 | PASS（0 P1 / 0 P2 / 3 P3） | base RED `NameError` @ main.py:167、head GREEN、变异杀死、四种批次情形端到端核对；与已关闭 #21 的两文件 blob 完全相同（替换忠实）。P3：回归 stub 掉真实分批；提示把"读取条数"当"已转发条数"（实测投递 3/4 vs 文案 5）；commit 称根 runner 67/1 实测 66/1。已提交 review（COMMENTED） |
| #18 | 原 **CHANGES REQUESTED**（N1 [P1·合并流程] + N2 [P2]）→ **N2 已落地，N1 已转为门禁** | 代码本身在其 base 上无缺陷（8 个诊断出口全标记、3 个消费点、真实链路 71/71 断言）；阻断点是**合并解析**：照搬 #19 那一侧会把标记退回 `str`，静默重开 issue #4 的 P1（naive 合并下测试全绿但链路探针 5 项失败）。**已在 head `6c32dce` 补上 N2 门禁用例**（见 §1.2），naive 解析会被测试杀死。原 review 以 COMMENTED 提交（GitHub 不允许对自建 PR 投 request-changes） |
| #24 | PASS（0 P1；P2×3、P3×5） | 合入最新 main（含 ctx_restore）**无冲突**，合并快照上阻塞入口全绿：pytest 594 passed、根 runner 66/0、并发通过、meme 488/2 skipped、ruff `E9,F63,F7` 通过 → 合入后首次 `push: main` 预计绿。纠正了作者三处结论，见 §1.1。已提交 review（COMMENTED）。**已修**：`docs/DEVELOPMENT.md` 与 `compat.yml` 里"3.10 停滞=跨用例累积"的归因改为真因 issue #25（head `79bb18f`） |
| #28 | PASS（0 阻断；3 P3） | 跨版本实测：base 3.10/3.11 该文件**挂死 exit 124**（含单跑 issue 点名用例），head 三版本均 5 passed；issue #25 三种现象在 base 全部复现、head 全部消除；根因行号本地核实（3.10 `tasks.py:433-435`、3.11 `:477-479`、3.12 `:519` 已重写）。P3：`enqueue_timeout_sec` 成为死状态但文档/配置仍宣称该超时（建议独立 follow-up）；compat.yml 时机建议；根因归因更正。已提交 review（COMMENTED） |
| #22 | PASS（0 阻断；1 P3） | AST 证明 base 是"普通 def 返回协程"、head 为真 async 且函数体逐语句全等；**决定性变异**：把 `session.py:90` 改回缺陷 `[1:]`，base 根 runner 仍 PASS（变异逃逸）、head 变 FAIL → 证明用例现在真的执行；根 runner 的 4 条 never-awaited 消失。P3：PR 正文的 "67 passed" 需注明 `-o asyncio_mode=auto`。已提交 review（COMMENTED） |
| #23 | PASS（0 阻断；1 P2） | base 复现 issue #10（根 runner 65/1、`IndexError`），head 66/0；交叉实验（base 生产代码 + head 测试）66/0 → 只改期望；变异 mut1/mut2 均被杀死，且证明 #24 带回的副本在同样变异下会漏（`5701be49` 是净增覆盖）。P2：issue #10 方案第 2 条"全部 bot 无回复时静默"零覆盖（`message_handler.py:1780-1782`），建议补用例或在关闭 #10 时记录缺口。已提交 review（COMMENTED） |

### 1.2 dev 集成分支（本地演练，不动 main）

按用户要求建了 `dev` 分支把已审查的 PR 全部合并演练：起点 `origin/main@2b95bf0`，**17 个分支零冲突合入**（唯一冲突是 #18↔#19 的 `llm_service.py`，按审查结论解析为 `return LLMDiagnostic(...), tool_results`），随后并入维护者新提交（`origin/main` 已到 **`bd9fbd2`**，仍只动 `plugins/ctx_restore/`），当前领先 main **75 个提交**。

实测（隔离快照，含门禁用例后）：

| 入口 | 结果 |
|---|---|
| 裸 `pytest tests`（靠 `pytest.ini` 的 asyncio auto） | **610 passed**（main 为 5 failed/589 passed） |
| 根 runner `tests/_run_all.py` | **69 passed / 0 failed**（main 为 65/1；issue #10 已修） |
| `tests/test_concurrency.py` | 全部通过 |
| `ruff E9,F63,F7` / `compileall` | 通过 |

**#18 的 P1 合并风险已由测试把守**（本轮最重要的落地）：在 #18 分支新增用例 `test_nonstream_tool_followup_failure_keeps_diagnostic_marker`（head `6c32dce`），断言非流式链式"跟进失败"出口返回 `LLMDiagnostic`，并用真实 `MessageHandler` 端到端断言诊断可见但**上下文/DB/情感零落盘**。变异验证：

- #18 分支原样：**8 passed**；把该出口改成普通 `str` → **1 failed**（杀死）；
- dev 合并态原样：**8 passed**；把它按 naive 解析改成普通 `str` → **1 failed**（杀死）。

即：如果将来有人把 #18/#19 的冲突解成普通字符串，这条用例会直接失败，而不是让 issue #4 的 P1 静默复活。

**独立复审（针对 head `6c32dce`）确认解除**：N1 [P1·合并流程] → **[Closed]**、N2 [P2] → **[Closed]**，结论 PASS。复审自己做了三向变异，其中 M3（只把 `message_handler.py:1259` 消费点改成 `if False:`、标记保留）→ 3 failed，证明端到端那半段确实承重，不是空转。naive 态下全仓根 runner 由 69/0 变 **68 passed / 1 failed**，而 #19 自带用例仍全绿——新用例是唯一红灯。

新增两条 P3：**N6**（PR 正文滞后：写"6 个用例/6 passed"，实际 8）→ **已修**，正文已更新为 8 个用例、补上"与 #19 的合并解析要求"一节；**N7**（门禁入口依赖：head 无 `pytest.ini`，裸 pytest 7 failed；`tests/_run_all.py` 与 #24 的 `asyncio_mode=auto` 下才执行）→ 记录，不阻断。

> 说明：`dev` 只在本地，用于验证合并可行性与顺序；**未推送、未动 main**。维护者合并时仍走各 PR。

### 1.3 分支清理（2026-10-15）

本地分支 **28 → 19**（= 17 个开放 PR 分支 + `main` + `dev`）。删除的是：
- 已关闭 PR 的分支：#12 `fix/meme-list-scope`、#15 `fix/sqlite-legacy-wal-migration`、#21 `fix/chat-manager-partial-forward`、#31 `lint/core-unused-imports-2`（后两个同时删了 origin 远程分支）；
- 已合并 PR 的本地残留：#11 `fix/reply-filter-nesting`、#13 `fix/relationship-kick-blacklist`、#14 `fix/qzone-per-post-reply-cap`、#16 `fix/outbound-cancellation-state`（origin 侧早已随合并删除）；
- 历史快照：本地 `docs/handover-audit-main`、`docs/audit-handover-and-rules`（内容已重放进 #27 / #24）。

**保留**所有开放 PR 的分支——删除它们会直接关掉对应 PR。另外 `fork` 远程仍留有 #11–#16 的旧副本与一个非本会话创建的 `feat/knowledge-base`，未动。

### 1.4 审查发现 → 处理状态（2026-10-16）

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

### 1.1 合并顺序与合并后果（基于当前 head 实测）

> **⚠️ main 已前进（2026-10-16 复核）**：`origin/main` = **`bd9fbd2`**，比本文档最初引用的 `b4548cd` 多 6 个提交，**全部只新增/修改 `plugins/ctx_restore/`**（维护者 10-15 晚 22:49–23:23 连推 6 次，直接推 main、无 PR、无测试），与所有开放 PR 零重叠。因此各 PR 记录的 base `b4548cd` 已过期；此前"main 未动"的说法作废。

**最新 main 的实测基线（`2b95bf0`，本机隔离快照）**：`pytest -o asyncio_mode=auto tests` = **5 failed / 589 passed**；根 runner = **65 passed / 1 failed**；`ruff E9,F63,F7` 与 compileall 通过。**这 5 个失败正是 #22（4 个 qzone 假通过）与 #23（#10 占位断言）要修的**——即当前 main 上若已有 CI，阻塞测试 job 会是红的，#22/#23（或带这两项修复的 #24）是让 main 变绿的前提。

**裸 `pytest tests/` 的真实数字（更正）**：无根配置时 pytest-asyncio 1.4 默认 strict，实测 **378 failed / 221 passed**（不是此前记的 13 failed——那是 53 个用例的子集口径）；加 `-o asyncio_mode=auto` 后 main = 5 failed/589 passed，`#24` head = **594 passed**。所以 `pytest.ini` 的作用是让 378 个 async 用例真正执行，而不是"修 13 个失败"。

**没有任何堆叠 PR**：除 #18/#19 的历史关系外，全部开放 PR 的 base 都是 `main@b4548cd`。

**文件重叠矩阵**（全仓只有 3 个文件被多个开放 PR 触碰）：

| 文件 | PR | 后果 |
|---|---|---|
| `mohobot/llm_service.py` | #18、#19 | **真冲突，且只有 1 个冲突块**：位于 `chat()` 的跟进 `except` 分支（#19 的 `return f"[LLM 调用失败…]", tool_results` ↔ #18 的 `reply_text = LLMDiagnostic(...)`），#18 其余 8 处自动合并。**rebase 时必须改成 `return LLMDiagnostic(...), tool_results`**，否则 #18 的 P1（诊断文案当正文落盘）会在链式路径重现；而 #19 把这个返回点从 1 处扩到最多 5 轮，等于把 P1 暴露面复制多份 |
| `tests/test_merged_group_reply.py` | #23、#24 | **更正**：#24 分支里的副本是 #23 的**旧版**（`test_merged_group_reply.py:129` 少 `ws.replies == []`），且 `merge-base(#23head, #24head)` 仍是 `b4548cd` → 先合 #23 **不会**让 #24 的展示 diff 自动收缩（需 rebase / "Update branch"）。若直接按现状合 #24，冲突解决必须**保留 main 侧（带 `ws.replies == []` 的那版）**，否则会把这行断言改弱 |
| `tests/test_qzone.py` | #22、#24 | #24 里的副本与 #22 head **逐字节相同**（`08ca85f`），内容无分歧；但同样因为 merge-base 未变而不会自动收缩，合并时该文件实际是空操作。推荐做法：**先合 #22、#23，再 rebase/Update branch #24**，让它只剩 CI 文件 |

其余生产文件各自只被一个 PR 触碰（`outbound.py`←#28、`chat_manager/main.py`←#26、`relationship config`←#17、`llm_service/message_handler/llm_diagnostic/engine`←#18、`ban/store`+`file_store`+`image_cache` 等←#32、`ban_filter`+`vcpedia`+`app.py` 等←#36）。

**建议顺序与合入后立即发生的变化**：

1. **#22、#23 先合**（测试可信度）：#23 让 main 根 runner 从 `65 passed/1 failed` 变 **`66 passed/0 failed`**，issue #10 可关；#22 让 4 个此前从未真正执行的 qzone 用例开始执行。两者都只改测试文件。
2. **#24 再合**（CI + `pytest.ini`）：推荐**先合 #22/#23，再 rebase / Update branch #24**，让它只剩 CI 文件（否则见上表：展示 diff 不收缩，且可能把 #23 的断言改弱）。合入后 `push: main` 立即触发 CI；审查员在"最新 main + #24"的合并快照上实测阻塞入口全绿（pytest 594 passed、根 runner 66/0、并发通过、meme 488/2 skipped、ruff `E9,F63,F7` 通过），预计首次运行即绿。`pytest.ini` 的作用是让 **378 个 async 用例真正执行**（无配置时裸跑 378 failed/221 passed），不是"修 13 个失败"。3.10/3.11 走独立 `compat.yml`（手工/每周/`push main`）；注意该 workflow **至今 0 次真实运行**，且上次 py310 job 在 pytest exit 124 后跳过了 runner/concurrency 步骤、artifact 只剩半份——合入后应手工 dispatch 一次确认端到端。
3. **#26、#28、#32–#36、#30、#29、#17、#27、#20** 可独立合入，互不冲突（见上表）。
4. **#18/#19 最后处理**：两者都在 `llm_service.py`，必须串行 + rebase；建议先合 #18（P1：诊断不落盘，涉及数据正确性），再 rebase #19 并重跑 `tests/test_llm_nonstream_tool_chain.py` 与 `tests/test_llm_runtime_tools.py`。只合其中之一不会让 main 语义不一致（各自自洽），但两个都未合时 issue #4 仍未关闭。

维护者已表态暂缓，相关 PR 已关闭：

- #12（meme 列表作用域）—— "meme 后面要大改一次……相关的先不用修了"
- #15（旧库 WAL 迁移）—— "感觉没什么必要，先不合并吧"

## 2. 接入 CI

**上述 main 基线没有项目 CI。** 没有 `.github/workflows/`，也没有 pytest / ruff 配置或 `pyproject.toml`。`main` 未设分支保护。
（CI PR 之前的唯一 Actions 记录是 GitHub 托管的 Copilot PR 审查，属于平台功能。）

**当前 main 仍未接入 CI**，但两个测试前置 PR 已验证可组合变绿：PR #22 修复 qzone async 测试，PR #23 修复 #10 过时断言；组合基线 pytest `594 passed`、根 runner `66 passed, 0 failed`。CI workflow 已在 PR #24，真实 Actions 已**全绿**：3.12 测试、语法检查、meme 插件、Ruff 债务报告（信息性）四项均成功。

### 2.1 已完成/待维护者合入的前置项

- [x] **#10**：PR #23 将 `tests/test_merged_group_reply.py` 的占位断言更新为当前“静默 bot 不生成节点”行为；组合根 runner `66 passed, 0 failed`。
- [x] **4 个 qzone 用例**：PR #22 将 `test_session_fetch_and_cache` / `test_session_reset_clears_cache` / `test_session_invalid_uin` / `test_session_no_o_prefix_uin` 改为真正的 `async def`，pytest qzone `67 passed`，根 runner 不再出现其 never-awaited 警告。

PR #24 依赖 #22/#23 合入后再以 `main` 为基线启用 workflow。以下原始诊断作为历史记录保留：

- 原 qzone 用例是**普通函数却返回协程**（`return run()`）。
  - 根 runner 只对 `async def` 做 `await`，所以这 4 个协程被直接丢弃（日志里是 `coroutine '...run' was never awaited`），整个文件却记为 `PASS`；
  - pytest 下这 4 个直接报错。

  改成 `async def` 即可。验证：pytest 下 4 个通过，且根 runner 日志中不再出现这 4 条 never-awaited。
- [x] **根目录开发依赖清单**：PR #24 新增根 `requirements-dev.txt`，固定 pytest / pytest-asyncio / ruff；全新虚拟环境安装后 `pip check` 无冲突。
- [ ] **清理剩余静态告警**：当前 159 条（`F401` 82、`F821` 25、`F841` 20、`F541` 12、`E741` 8 等），全部来自既有代码。CI 只阻塞 `E9,F63,F7`；完整 `E4,E7,E9,F` 由信息性 job 报告（计数进 step summary + artifact），避免把已知债务显示成 PR 的失败，也避免隐藏告警。

### 2.2 workflow 设计（PR #24 已实现，待合入）

`main` 变绿后，新增 `.github/workflows/ci.yml`，`push main` 与 `pull_request` 触发：

| job | 内容 | 是否阻塞合并 |
|---|---|---|
| `tests` (Python 3.12) | 开发依赖安装、compileall、逐用例 pytest、根 runner、独立并发专项 | 是 |
| `meme-plugin` | 在 `plugins/meme_stealer/` 下按其 `CLAUDE.md` 跑 `pytest tests` 与 `ruff check` | 是 |
| `syntax` | Ruff `E9,F63,F7`：阻塞语法和控制流错误 | 是 |
| `Ruff debt report` | 既有 `E4,E7,E9,F` 告警：计数进 step summary + 完整 artifact；job 保持绿色 | 否（信息性，不隐藏告警） |
| `compat.yml` (Python 3.10) | 手工触发 / 每周日 / `push main`，带线程栈与日志 artifact | 否（刻意不挂 PR） |

选型依据：

- **Python 3.12**：生产实际运行 3.12.13（README 部署章节）；
- **3.10 / 3.11**：README 声明支持 3.10+，但兼容性**未验证**——套件会挂死，已定位到根因并开 **#25**（见第 3 节 P2）。放在 PR 门禁上只会让每个 PR 变红，因此移到独立 `compat.yml`（手工触发 / 每周日 / `push main`），保留线程栈与日志 artifact 继续采样；#25 修好后可把 3.10/3.11 重新纳入；
- **两个 runner 都跑**：根 runner 是项目文档里的入口，但它按**文件**计数、文件内首个失败即中止、并且会漏掉"返回协程"的用例；pytest 能逐用例报告。等 pytest 稳定后可以只保留 pytest。

当前基线耗时（本地）：根 runner 约 70s，pytest 约 86s，meme 插件约 8s。

### 2.3 之后再做

- **Ruff 债务进度**：`main@b4548cd` 基线为 160 条（`F401` 83、`F821` 25、`F841` 20、`F541` 12、`E741` 8、`E731` 5、`E401` 3、`E702` 2、`F811` 1、`E701` 1）。已按小批次清理：#30（测试 5 文件）、#32（核心 6 文件）、#33（3 文件）、#34（persona_ab）、#35（wifepicker）、#36（核心 7 文件）。**全部合入后预计剩余约 90 条**，主要是未触碰文件的 `F401`（plugins/mohobot/tests）、`F821` 字符串注解与 `E731/E741`。
- **节奏建议**：当前已有 10+ 个开放 PR 且维护者尚未审阅。继续逐个开小 PR 会让待审队列更难处理，建议**先让维护者消化现有队列**，剩余告警再按"plugins 未使用导入 / tests 未使用导入 / 注解与风格"合并成 2–3 个稍大的 PR；不要用全仓 `ruff --fix` 一把梭（会把 24 条 unsafe fix 和无关格式变更混进来）。
- PR #24 已加 `pytest.ini` 统一 async 模式；后续可统一 ruff 配置，清理告警后再收紧 blocking rules；
- **裸 `pytest tests/` 的现状（复核实测）**：`main` 上没有任何根 pytest 配置，pytest-asyncio 1.4 默认 strict，直接跑会得到 `13 failed / 40 passed`；加 `-o asyncio_mode=auto` 或等 #24 的 `pytest.ini` 合入后才正常。因此 #24 不只是"加 CI"，也是让仓库裸 `pytest` 可用的前置，建议排在 #22/#23 之后尽早合入。
- `main` 分支保护：要求 CI 通过才能合并。

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
| #4 | 诊断落盘（P1）、非流式工具断链（P2） | PR #18、#19 待合并 |
| #7 | 审批文本换行注入；批量任务未纳入生命周期管理 | 被踢拉黑部分已由 #13 修复 |
| #8 | review 面板：历史缓存身份、作用域约束、审核状态并发 | 未开始 |
| #9 | 插件送达回执、运行时生命周期策略 | 单帖回复上限已由 #14 修复 |
| #10 | 根回归基线失败 | PR #23 已实现并审查通过，待维护者合入后才在 main 上解决 |
| #25 | Python 3.10/3.11 取消被 `wait_for` 吞掉，调用方可能永久挂起 | 修复已在 PR #28；3.10/3.11/3.12 出站取消测试均 5 passed，待独立审查与维护者合入 |
| #26 | chat-manager 部分转发失败时的 `nodes` NameError | 直接基于 main，独立审查通过，待维护者合入 |
| #2 | 旧库迁移丢 WAL | 维护者暂缓，PR #15 已关闭 |
| #6 | meme 作用域与图片来源边界 | 维护者表示 meme 将重写，PR #12 已关闭 |

## 5. 测试基础设施

- 根 runner（`tests/_run_all.py`）按**文件**计数，文件内首个失败即中止，后续用例的结果被隐藏；只收集模块顶层 `test_*` 函数（类内用例、只写在 `__main__` 里的用例不会执行）。
- meme 插件套件里 `tests/test_external_sources.py` 用 `8.8.8.8` 作"公网地址"样例，离线防护下记录到 1 次被拦截的外网 DNS 尝试，测试仍通过。CI 有网络时行为可能不同，需确认它不依赖真实网络。

## 6. 文档

- [ ] 仿照 [Agent-LuoTianyi 的 docs 组织](https://github.com/SheepLiu712/Agent-LuoTianyi/tree/dev/docs) 完善本项目文档体系：补齐/统一 `TODO.md`、代码地图、开发守则、规格文档模板、开发进程/实现计划、项目说明与贡献指南等职责。参考目录结构和文档职责，不直接复制其他项目内容；按模块和当前代码逐项核对。
- [ ] 交接与审计文档（`AGENTS.md`、`docs/HANDOVER.md`、`docs/AUDIT-2026-10-12.md`）已从历史 fork 分支整理到 PR #27（docs 分支 head `c17fea0`），新建独立审查 PASS 后待维护者合入。审计快照基于 `b63ad48`，已标注历史边界；后续修复以当前 TODO、issue、PR 和 CI 为准。
- [ ] **审计完整性的已知缺口**：`/查看聊天` 部分转发失败的 `nodes` NameError（引入于 `115ac17`，早于审计基点 `b63ad48`）**没有被那份审计报告收录**，是后来从 CI 基线的 `F821` 检查发现的（修复见 #26）。说明该审计的 P1/P2 清单不是完备清单；若将来把 `AUDIT-2026-10-12.md` 当作"已审计范围"引用，需同时说明这一点。
