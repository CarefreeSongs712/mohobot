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
| #28 | 修复 Python 3.10/3.11 `wait_for` 吞取消 | `outbound.py` 改 `put_nowait`；3.10/3.11/3.12 出站测试均 5 passed，独立审查中 |
| #29 | 清理三个失效/错误形状的 P3 回归测试 | 新 head `35f75f4` 已补 `await run()`；19 个 pytest、原生入口和 Ruff 已重跑通过，等待新 head 独立复核 |
| #22 | 修复 4 个未 await 的 qzone 会话测试 | 独立审查通过，67 个 qzone 用例通过 |
| #23 | 修正 #10 过时的 merged-reply 占位断言 | 独立复审通过；最新补充断言非选中 bot 不发普通回复 |
| #24 | GitHub Actions CI、根开发依赖、pytest 配置 | 依赖 #22/#23；真实 Actions 已全绿（3.12 测试 / 语法 / meme 插件 / Ruff 债务报告），3.10 移出 PR 门禁到 `compat.yml` |

建议合入顺序：先 #22、#23，再 #24；#17、#26、#28、#29 可独立合入；#18/#19 需先处理文本冲突。#27 是文档独立 PR；#25 已由 #28 实现修复，待维护者合入后关闭 issue。

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

- 风格类告警（`F401` 83、`F841` 20、`F541` 12、`E741` 8、`E731` 5、`E401` 3、`E702` 2、`E701` 1，合计 134 处）逐步清理后再纳入 `lint`；
- PR #24 已加 `pytest.ini` 统一 async 模式；后续可统一 ruff 配置，清理告警后再收紧 blocking rules；
- `main` 分支保护：要求 CI 通过才能合并。

## 3. 已确认的缺陷

| 级别 | 位置 | 问题 | 证据 |
|---|---|---|---|
| **P2** | `mohobot/services/outbound.py:125` 的 `asyncio.wait_for(queue.put(...))` / 首次由 #16 引入 | Python **3.10 与 3.11** 上，调用方在"入队完成→开始等结果"窗口被取消时，取消被 `wait_for` 吞掉：调用方拿不到 `CancelledError`，会永久挂起或返回自己以为已取消的结果。生产跑 3.12 不受影响，但 README 声明的 3.10+ 属未验证 | 已开 **#25**（含最小复现、任务栈、3.10/3.11/3.12 状态对照、`wait_for` 源码定位：`except CancelledError: if fut.done(): return fut.result()`；3.12 用 `asyncio.timeout` 重写后正常） |
| **P2** | `plugins/chat_manager/main.py:167` | `/查看聊天` 在部分批次失败时引用未定义 `nodes` | PR #26 直接基于 main 修复，独立审查中；历史探针明确 `NameError`，全成功/全失败正常 |
| P3 | `tests/test_new_features_2.py:316-323` | `run()` 开头就 `return`，后面的断言**从未执行**——这个用例实际已失效 | ruff `F821`（`handled` / `reply` 未定义） |
| P3 | `tests/test_tool_followup.py:206` | `__main__` 入口调用未定义的 `main()`，直接执行该文件会 `NameError` | ruff `F821` |
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
- [ ] 交接与审计文档（`AGENTS.md`、`docs/HANDOVER.md`、`docs/AUDIT-2026-10-12.md`）已从历史 fork 分支整理到 PR #27（当前 head `b82e537` 的 docs 分支），待独立审查和维护者合入。审计快照基于 `b63ad48`，已标注历史边界；后续修复以当前 TODO、issue、PR 和 CI 为准。
