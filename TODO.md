# TODO

项目待办清单。每一项都尽量链接到对应的 Issue / PR，并写明**如何验证已完成**。
完成后直接删掉该项（历史留在 PR 里），不要只打勾。

> 事实基线：2026-10-14，`main` = `b4548cd`。下文的测试/静态检查数字都是在这个提交上实测的。

## 工作约定

- 所有改动走 **分支 → PR → 审查 → 合并**。即使有 push 权限，也不直接推 `main`。
- 每个修复 PR 附带：先失败后通过的回归用例（RED/GREEN）、全量根回归结果、触发条件的如实说明。
- 改动消息发送 / 上下文 / 存储语义时，补跑 `tests/test_concurrency.py`（根 runner 不包含它）。

---

## 1. 等待处理的 PR

| PR | 内容 | 备注 |
|---|---|---|
| #17 | 删除 `RequestConfig._clean()` 后残留的 4 行不可达代码 | 纯删除；修复 ruff `F821` |
| #18 | #4 P1：LLM 诊断文案不再写入上下文 / 数据库 / 情感分析 | — |
| #19 | #4 P2：非流式模式支持链式多轮工具调用 | **与 #18 在 `llm_service.py` 文本冲突**，后合并的一方需 rebase：把循环内"跟进失败"的返回值包成 `LLMDiagnostic(...)` |

维护者已表态暂缓：

- #12（meme 列表作用域）—— "meme 后面要大改一次……相关的先不用修了"
- #15（旧库 WAL 迁移）—— "感觉没什么必要，先不合并吧"

## 2. 接入 CI

**现状：项目没有 CI。** 没有 `.github/workflows/`，也没有 pytest / ruff 配置或 `pyproject.toml`。`main` 未设分支保护。
（Actions 页面能看到的唯一记录是 GitHub 托管的 Copilot PR 审查，属于平台功能，不是项目 CI。）

**今天直接打开 CI 会是红的**，所以要先完成下面的前置项，再加 workflow。

### 2.1 前置项（让 `main` 先变绿）

- [ ] **#10**：根 runner 唯一失败 `tests/test_merged_group_reply.py`（用例断言的占位节点已被 `b6e0bf6` 有意移除）。
  验证：`python tests/_run_all.py` 输出 `N passed, 0 failed`。
- [ ] **4 个 qzone 用例从未真正执行过**：`tests/test_qzone.py` 的 `test_session_fetch_and_cache` / `test_session_reset_clears_cache` / `test_session_invalid_uin` / `test_session_no_o_prefix_uin` 是**普通函数却返回协程**（`return run()`）。
  - 根 runner 只对 `async def` 做 `await`，所以这 4 个协程被直接丢弃（日志里是 `coroutine '...run' was never awaited`），整个文件却记为 `PASS`；
  - pytest 下这 4 个直接报错。

  改成 `async def` 即可。验证：pytest 下 4 个通过，且根 runner 日志中不再出现这 4 条 never-awaited。
- [ ] **根目录缺开发依赖清单**：`requirements.txt` 里没有 pytest / pytest-asyncio / ruff，但 `tests/test_outbound_cancellation.py`（#16 引入）顶层 `import pytest`。只装 `requirements.txt` 的干净环境里，根 runner 会在这个文件上失败。
  新增根目录 `requirements-dev.txt`（可参照 `plugins/meme_stealer/requirements-dev.txt` 的版本约束）。
- [ ] **先修掉"真错误"类静态告警**（见第 3 节）：`F821` 未定义名 25 处、`F811` 重定义 1 处。

### 2.2 workflow 设计

`main` 变绿后，新增 `.github/workflows/ci.yml`，`push main` 与 `pull_request` 触发：

| job | 内容 | 是否阻塞合并 |
|---|---|---|
| `tests` (Python 3.12) | `pip install -r requirements.txt -r requirements-dev.txt` → `python -m compileall -q main.py mohobot plugins review scripts tests` → `python tests/_run_all.py` → `python tests/test_concurrency.py` | 是 |
| `pytest` (Python 3.12) | `python -m pytest -q --asyncio-mode=auto tests --ignore=tests/test_concurrency.py`：逐用例报告，能抓住上面那类"协程未执行"的假 PASS | 是（前置项完成后） |
| `meme-plugin` | 在 `plugins/meme_stealer/` 下按其 `CLAUDE.md` 跑 `pytest tests` 与 `ruff check` | 是 |
| `lint` | `ruff check --select E9,F63,F7,F82`：只拦真错误，先不管风格类 | 是 |
| `py310` | 同 `tests`，Python 3.10 | 否（先观察） |

选型依据：

- **Python 3.12**：生产实际运行 3.12.13（README 部署章节）；
- **3.10**：README 声明支持 3.10+，按常见 3.11+ 特性（`TaskGroup`、`tomllib`、`ExceptionGroup`、`except*`、`typing.Self` 等）检索没有命中，但从未在 3.10 上实际跑过，所以先作非阻塞 job；
- **两个 runner 都跑**：根 runner 是项目文档里的入口，但它按**文件**计数、文件内首个失败即中止、并且会漏掉"返回协程"的用例；pytest 能逐用例报告。等 pytest 稳定后可以只保留 pytest。

当前基线耗时（本地）：根 runner 约 70s，pytest 约 86s，meme 插件约 8s。

### 2.3 之后再做

- 风格类告警（`F401` 83、`F841` 20、`F541` 12、`E741` 8、`E731` 5、`E401` 3、`E702` 2、`E701` 1，合计 134 处）逐步清理后再纳入 `lint`；
- 加 `pyproject.toml` 统一 pytest（`asyncio_mode`）与 ruff 配置，去掉命令行参数；
- `main` 分支保护：要求 CI 通过才能合并。

## 3. 已确认的缺陷（静态检查发现，未开 Issue）

| 级别 | 位置 | 问题 | 证据 |
|---|---|---|---|
| **P2** | `plugins/chat_manager/main.py:167` | `/查看聊天` 在**部分批次转发失败**时引用未定义的 `nodes`，抛 `NameError`，用户看不到"部分失败"提示 | 探针：`ok=1 failed=1 -> NameError: name 'nodes' is not defined`；全部成功 / 全部失败两条分支正常。应为 `len(messages)` |
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
| #10 | 根回归基线失败 | 见 2.1，CI 前置 |
| #2 | 旧库迁移丢 WAL | 维护者暂缓（#15） |
| #6 | meme 作用域与图片来源边界 | 维护者表示 meme 将重写，暂不修 |

## 5. 测试基础设施

- 根 runner（`tests/_run_all.py`）按**文件**计数，文件内首个失败即中止，后续用例的结果被隐藏；只收集模块顶层 `test_*` 函数（类内用例、只写在 `__main__` 里的用例不会执行）。
- meme 插件套件里 `tests/test_external_sources.py` 用 `8.8.8.8` 作"公网地址"样例，离线防护下记录到 1 次被拦截的外网 DNS 尝试，测试仍通过。CI 有网络时行为可能不同，需确认它不依赖真实网络。

## 6. 文档

- [ ] 交接与审计文档（`AGENTS.md`、`docs/HANDOVER.md`、`docs/AUDIT-2026-10-12.md`）在 fork `w-sli/mohobot` 的 `docs/audit-handover-and-rules` 分支（`e5d1c01`）上，尚未开 PR。提交前需按独立复核结论修正审计文档里的过度结论（例如"所有取消都会丢归档"不成立：等待回执阶段有 8 秒兜底）。
