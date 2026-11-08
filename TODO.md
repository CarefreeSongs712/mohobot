# TODO

维护者待办与决策摘要。此文件不是 GitHub PR/issue、CI 运行或部署状态的实时接口；带日期的快照需按目标代码和在线记录重新核实。

> 本次核对基点：2026-11-08，`origin/dev` = `62841723`，`origin/main` = `71b3169`。核对时 PR #113 仍开放，reviewDecision 为 APPROVED；后续状态请查看 GitHub。发布规则与授权边界见 [AGENTS.md](AGENTS.md)，部署操作见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 1. 未完成事项

| # | 事项 | 当前依据/边界 | 验证与完成条件 |
|---|---|---|---|
| 1 | PR #113：`/status` 与 `/help` 卡片重构 | 2026-11-08 核对时开放且 reviewDecision 为 APPROVED；该状态可能变化。 | 以 PR 最新 review、CI 和合并记录为准；合入后同步相关文档。 |
| 2 | 情感 burst 模型默认值决策 | 当前 `EmotionConfig.burst_model` 默认 `DeepSeek-V4-Flash`；候选模型列表也保留该名称。此项会影响默认行为，不能仅为文案清理而更改。 | 维护者确认要保留或替换；若替换，独立代码 PR 验证 provider/模型兼容和配置迁移。 |
| 3 | 新闻注入窗口行为 | 配置模型定义 `news.inject_window_days`，但目标基点的新闻注入实现尚未按该值过滤。 | 若需要此过滤，补实现与覆盖边界测试；完成后验证过期/窗口边界，并同步配置说明。 |
| 4 | 插件市场与格式适配设计 | 当前插件由仓库目录加载，未提供市场安装/升级/卸载流程；兼容 AstrBot 仍是提案，不是已批准的实现计划。 | 先核对加载器、schema 与信任边界，完成并评审独立设计后再决定是否实施。 |
| 5 | 外部服务功能验收 | RAG embedding、B 站采集器和真实模型 bad case 需要外部服务或授权环境；源码存在不代表线上可用。 | 明确测试环境和凭据授权，使用隔离数据验证，记录可复现样本与限制。 |

## 2. 已完成需求摘要

以下仅记录需求交付方向，具体行为以当前代码与测试为准，不重复维护 PR 状态表。

- **REQ-1 文档同步**：此前已通过 PR #51/#53/#68 完成一轮文档更新；本次及后续纠偏以代码和专属文档为准。
- **REQ-2 知识库**：RAG 模块 `mohobot/knowledge/` 已合入；`knowledge.enabled` 默认关闭。
- **REQ-3 表情包插件**：被动概览抓取默认关闭，工具抓取审核、来源范围及多 Bot 过滤已实现；见 [`plugins/meme_stealer/README.md`](plugins/meme_stealer/README.md)。
- **REQ-4 歌曲知识**：多来源同步、字段合并、模糊匹配已实现；定时同步开关默认关闭。
- **REQ-5 视觉输入**：`vision_mode` 支持 `describe`/`direct`，配置示例默认 `describe`。
- **REQ-6 新闻动态**：审核、工具和可选采集器已实现；总开关默认关闭。字段是否实际参与运行需以代码核验（见未完成项）。
- **REQ-7 OOC 治理**：过滤与参数入口已实现；模型行为改善程度不作为静态代码结论，需要样例评估。
- **REQ-8 OneBot 契约测试**：fake OneBot 与协议测试已纳入测试目录。
- **REQ-9 history 写入**：群聊旧 per-bot 双写已移除，合并归档仍逐条写入；批量 flush 暂不实施，以保留既有落盘语义。

## 3. 历史性能决策

**history flush 基准（2026-10-19，隔离快照）**：当时以 3000 条归档事件比较逐条 flush 与每 50 条 flush；后者测得约 57% 耗时下降，但会扩大崩溃前尚未落盘的数据窗口。该数据只描述当时的代码与环境，不是当前性能基线。若重新评估，需使用当前实现复测并记录环境、命令、样本规模和数据持久性取舍。

## 4. 测试与 CI

CI 触发条件、阻塞 job 和 runner 覆盖以 [`.github/workflows/ci.yml`](.github/workflows/ci.yml) 为准。常用本地测试命令、覆盖缺口与安全注意事项见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。本文件不固定通过数量、运行耗时或 lint 条目数，也不保存仅存在于忽略目录中的报告路径。
