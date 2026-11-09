# 参与贡献

欢迎提交功能、修复、测试与文档 PR。开始前请先读：

| 文档 | 用途 |
|---|---|
| [AGENTS](AGENTS.md) | 数据、并发、生命周期、扩展、验证、安全与授权的硬性约束 |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | 代码结构、扩展步骤、测试与 CI 口径 |
| [docs/CODE-MAP.md](docs/CODE-MAP.md) | 快速定位模块与修改入口 |
| [docs/PLUGINS.md](docs/PLUGINS.md) | 插件命令与配置入口 |
| [docs/SPEC-TEMPLATE.md](docs/SPEC-TEMPLATE.md) | 跨模块功能/行为变更的规格模板 |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | 社区行为准则与举报方式 |
| [SECURITY.md](SECURITY.md) | 漏洞的私密报告渠道（不要公开披露细节） |

## 分支与 PR

1. 从最新 `origin/dev` 建分支：`feat/<topic>`、`bugfix/<topic>` 或 `docs/<topic>`。
2. 改动只提交到该分支，通过 PR 合入 `dev`；`main` 只接受维护者的 release PR。提交 PR 时按 `.github/PULL_REQUEST_TEMPLATE.md` 填写验证与检查项。
3. PR 描述写清：改了什么、为什么、如何验证；涉及数据/配置/脚本时说明影响面与回滚方式。
4. 涉及运行时行为的改动，按 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) 补回归测试；纯文档改动请在验证中注明"未运行测试"。
5. spec 与 PR 应经过**非作者**审核；维护者的实现 PR 由独立复核确认，审核意见与结论留在 PR 中。

## 开发流程

### 新功能

1. 明确目标、场景与验收标准；跨模块、行为有歧义或影响多个入口时，先按 [docs/SPEC-TEMPLATE.md](docs/SPEC-TEMPLATE.md) 写 spec（放在 `docs/designs/`，标注草案/已评审）。
2. 建 `feat/<topic>` 分支实现；小功能可一个 PR 完成，大功能可拆多个 PR 与 issue。
3. PR 描述写清验收标准与验证结果；审核者核对实际行为、架构边界与证据。验收未覆盖或失败的范围必须如实说明。

### Bug 修复

1. 从 `dev` 建 `bugfix/<topic>`；若已确认需从发布线紧急修复，从 `main` 建 `hotfix/<topic>`，合入 `main` 发布后回流 `dev`。
2. PR 描述必须写明：**触发条件、被违反的不变量、根因、修复点、验证结果**（模板中已留字段）。
3. 修复需有能证明该场景的回归测试；无法自动化的部分说明手工验证方式。

### 发布

`dev` 经审查与 CI 后由维护者通过 release PR 合入 `main` 并打版本标签；不直接推送 `main`。发布与部署检查见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

### 规格生命周期

- 进行中的 spec 标"草案/已评审"，实现完成后补交付记录并标"已实现（历史）"。
- 已完成的历史设计**不删除**（与部分项目的清理策略不同）：本仓库保留 `docs/designs/` 作为决策历史，但不得把历史设计当作当前行为说明；持久约束由测试和 [AGENTS](AGENTS.md) 承载。

## 提交前检查

- 按改动范围运行相关 pytest；涉及主链/并发/插件重载/装配时补跑对应专项（命令见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#1-开发环境与日常循环)）。
- `compileall` 通过；Ruff 门禁为 `E9,F63,F7`（CI 定义见 `.github/workflows/ci.yml`）。
- 改前端时提取内联脚本执行 `node --check`。
- 不要提交密钥、Cookies、本地配置、`data/`、`logs/` 或真实用户数据；测试使用临时目录与替身。
- 文档与实现不符时，先核对当前调用链再改文档；历史审计/设计稿要标明时间基点，不能当作当前事实。

## 文档改动

文档职责划分见 [README](README.md) 的文档导航与 [docs/HANDOVER.md](docs/HANDOVER.md)：

- 用户向内容（能力、命令、插件用法）→ README / docs/PLUGINS.md；
- 开发与架构 → docs/DEVELOPMENT.md / docs/CODE-MAP.md；
- 部署与发布 → docs/DEPLOYMENT.md；
- 未完成事项与决策 → TODO.md；
- 历史审计与设计稿 → 保持日期/版本标注，不改写成"当前状态"。

## 关于 AI 辅助

- 人类负责目标、场景与验收结论；AI 可以起草代码、测试与文档（含 spec 与代码地图），但提交者必须能解释、调试并维护所提交的内容。
- AI 生成的说明不能替代实际验证结果；描述行为时以源码、测试和本次运行的退出码为依据，跳过/失败项如实报告。
- spec 可以由人或 AI 起草，但需经非作者审核；审核可随实现 PR 一起进行。

## 许可

核心框架为 [MIT](LICENSE)；各移植插件保留上游许可（如 `plugins/meme_stealer`、`plugins/proactive_chat` 的 AGPL-3.0），提交前确认改动未违反对应许可。
