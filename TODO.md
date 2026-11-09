# TODO

维护者待办与决策摘要。此文件不是 GitHub PR/issue、CI 运行或部署状态的实时接口；带日期的快照需按目标代码和在线记录重新核实。

> 本次核对基点：2026-11-08，`origin/dev` = `62841723`，`origin/main` = `71b3169`。核对时 PR #113 仍开放，reviewDecision 为 APPROVED；后续状态请查看 GitHub。发布规则与授权边界见 [AGENTS.md](AGENTS.md)，部署操作见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 1. 未完成事项

### 1.1 待决策 / 在途

| # | 事项 | 当前依据/边界 | 验证与完成条件 |
|---|---|---|---|
| 1 | PR #113：`/status` 与 `/help` 卡片重构 | 2026-11-08 核对时开放且 reviewDecision 为 APPROVED；该状态可能变化。 | 以 PR 最新 review、CI 和合并记录为准；合入后同步相关文档。 |
| 2 | 情感 burst 模型默认值决策 | 当前 `EmotionConfig.burst_model` 默认 `DeepSeek-V4-Flash`，候选模型列表同。属默认行为变更，不能仅为文案清理而改。 | 维护者确认后独立代码 PR；验证 provider/模型兼容与配置迁移。 |
| 3 | `llm.stable_system_prefix` 启用决策 | 配置默认 `false`（实验开关）。是否启用需成对 A/B 数据：同一会话两阶段对比。 | 在隔离/授权环境按既定顺序与样本运行，采集 `scripts/usage_hit_rate.py --by module` 两阶段差异后再决策。 |
| 4 | 新闻注入窗口行为 | 配置模型定义 `news.inject_window_days`，但当前注入实现未按该值过滤。 | 补实现 + 边界测试（过期/窗口），并同步配置说明。 |
| 5 | 插件市场与格式适配设计 | 插件由仓库目录加载，无市场安装/升级/卸载；兼容 AstrBot 是提案，非已批准计划。 | 先核对加载器、schema 与第三方代码信任边界，评审设计后再实施。 |
| 6 | 外部服务功能验收 | RAG embedding、B 站采集器、真实模型 bad case 依赖外部服务/授权环境。 | 明确测试环境与凭据授权，用隔离数据验证并记录可复现样本与限制。 |

### 1.2 可选优化 / 技术债（P3、按需）

| # | 事项 | 说明 |
|---|---|---|
| 1 | prompt 前缀缓存布局改造 | `stable_system_prefix` 是否推进取决于 #1.3 实验；2026-11 初的历史运行曾测得 24h chat 命中约 39%（仅当日期望，非当前基线）。 |
| 2 | 情感分析成本优化 | 频率（force_interval/cooldown）与模板精简；含「命中被 free 模型零上报」的统计口径核对。 |
| 3 | 代理环境变量净化 | 携带 `socks://` 代理 URL 或含 `[::1]` 的 `no_proxy` 时，httpx 新版本可能在启动/定位阶段失败或解析异常。当前规避是运行前清理相关变量；根治方向是在启动器/测试引导里统一净化（未实施）。 |
| 4 | Ruff 存量债务 | 较宽规则集下存在未清理告警（随代码变化，准确计数与清单以各次 CI 的 `ruff-debt-report` artifact 为准）；`E9,F63,F7` 门禁不受影响。 |
| 5 | ImageCache per-bot vision | 缓存按图片共享描述，per-bot 视觉配置只在首次识别生效；属缓存层设计问题，需单独排期。 |
| 6 | #105 迁移护栏测试补齐 | 旧库迁移的 quick_check/行数校验缺少独立测试（此前变异存活）；未断言旧 `-wal/-shm` 文件保留；Windows 打开句柄场景可能令 `os.replace` 测试失败。补断言后变异应变红。 |

## 2. 已完成需求摘要

以下仅记录需求交付方向，具体行为以当前代码与测试为准，不重复维护 PR 状态表。

- **REQ-1 文档同步**：此前已通过 PR #51/#53/#68 完成一轮文档更新；本次及后续纠偏以代码和专属文档为准。
- **REQ-2 知识库**：RAG 模块 `mohobot/knowledge/` 已合入；`knowledge.enabled` 默认关闭。
- **REQ-3 表情包插件**：被动概览抓取默认关闭，工具抓取审核、来源范围及多 Bot 过滤已实现；见 [`plugins/meme_stealer/README.md`](plugins/meme_stealer/README.md)。
- **REQ-4 歌曲知识**：多来源同步、字段合并、模糊匹配已实现；定时同步开关默认关闭。
- **REQ-5 视觉输入**：`vision_mode` 支持 `describe`/`direct`，配置示例默认 `describe`。
- **REQ-6 新闻动态**：审核、工具和可选采集器已实现；总开关默认关闭。字段实际参与情况见未完成项。
- **REQ-7 OOC 治理**：过滤与参数入口已实现；模型行为改善程度需样例评估。
- **REQ-8 OneBot 契约测试**：fake OneBot 与协议测试已纳入测试目录。
- **REQ-9 history 写入**：群聊旧 per-bot 双写已移除，合并归档仍逐条写入；批量 flush 暂不实施，以保留既有落盘语义。

## 3. 历史性能决策

**history flush 基准（2026-10-19，隔离快照）**：当时以 3000 条归档事件比较逐条 flush 与每 50 条 flush；后者测得约 57% 耗时下降，但会扩大崩溃前尚未落盘的数据窗口。该数据只描述当时的代码与环境，不是当前性能基线。若重新评估，需使用当前实现复测并记录环境、命令、样本规模和数据持久性取舍。

## 4. 测试与 CI

CI 触发条件、阻塞 job 和 runner 覆盖以 [`.github/workflows/ci.yml`](.github/workflows/ci.yml) 为准。常用本地测试命令、覆盖缺口与安全注意事项见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。本文件不固定通过数量、运行耗时或 lint 条目数，也不保存仅存在于忽略目录中的报告路径。
