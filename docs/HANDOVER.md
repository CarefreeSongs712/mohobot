# Mohobot 项目接手说明

> 本文是接手入口与文档索引，不保存个人机器信息、凭据、生产地址或实时部署状态。仓库状态、开放 PR/issue、CI 和部署情况都可能变化；每次接手应从 Git 与 GitHub 当前记录重新核实。

## 1. 文档职责

| 文档 | 职责 | 适用范围 |
|---|---|---|
| [README](../README.md) | 项目定位、能力概览、安装入口和常用命令 | 使用者与初次接触者 |
| [AGENTS](../AGENTS.md) | 数据、并发、生命周期、扩展、验证、安全与授权的稳定约束 | 所有代码任务 |
| [DEVELOPMENT](DEVELOPMENT.md) | 当前代码结构、扩展契约、配置、测试与 CI 开发说明 | 代码贡献者 |
| [CODE-MAP](CODE-MAP.md) | 模块定位、关键调用链与修改入口（不复制签名/配置/测试结果） | 定位代码时 |
| [PLUGINS](PLUGINS.md) | 内置插件命令与配置入口 | 使用者与插件维护者 |
| [CONTRIBUTING](../CONTRIBUTING.md) | 分支/PR、Bug 修复流程与提交前检查 | 代码贡献者 |
| [SPEC-TEMPLATE](SPEC-TEMPLATE.md) | 跨模块功能/行为变更的规格模板（验收标准/模块/接口） | 功能设计时 |
| [DEPLOYMENT](DEPLOYMENT.md) | 新环境部署模板、网络边界和发布前检查 | 经授权的部署操作 |
| [TODO](../TODO.md) | 有明确状态和验证方式的未完成项目，以及简要决策记录 | 维护者规划 |
| [历史缺陷审计](AUDIT-2026-10-12.md) | 绑定 `main/b63ad48`、日期为 2026-10-12 的历史发现与复现 | 仅供追溯，不代表当前缺陷清单 |
| `plugins/*/README.md` 与 `docs/designs/` | 插件专属用法、历史设计或明确标注的提案 | 按文档标注的版本/状态阅读 |

文档与实现冲突时，以目标分支的代码、配置模板、测试和 CI 定义为核对依据；不要把旧的交接快照、设计建议或 README 描述当成运行实例的实时状态。

## 2. 接手步骤

1. 阅读本文件、[AGENTS](../AGENTS.md)、[README](../README.md) 与 [DEVELOPMENT](DEVELOPMENT.md)；定位代码用 [CODE-MAP](CODE-MAP.md)；如涉及部署，再读 [DEPLOYMENT](DEPLOYMENT.md)。
2. 在仓库根目录检查 `git status`、当前分支、HEAD、目标分支及其远端引用；按需检查开放 PR/issue 和对应 CI 运行。不能从旧文档推断这些状态。
3. 追踪相关调用链和测试。配置示例描述的是默认/模板值，不等于任一运行环境的实际配置；历史审计问题需对当前代码重新验证。
4. 保留现有配置与数据。未经授权不连接生产、不启动真实机器人、不执行迁移/同步/清理、不提交或部署。

## 3. 稳定入口

- `main.py`：应用装配与生命周期。
- `mohobot/message_handler.py`：消息主链。
- `mohobot/ws_server.py`：OneBot 反向 WebSocket 与出站发送。
- `mohobot/interceptors/`：封禁、插件、命令与关键词拦截器。
- `mohobot/services/`：跨模块服务，包括 LLM 工具注册、任务监管与用量。
- `plugins/`：可选功能插件；具体入口和配置以各插件代码及 `_conf_schema.json` 为准。
- `tests/` 与 `.github/workflows/ci.yml`：回归入口和 CI 实际门禁。测试说明见 [DEVELOPMENT §1](DEVELOPMENT.md#1-开发环境与日常循环)。

架构与数据不变量集中写在 [AGENTS](../AGENTS.md)，本文件不重复保存详细调用顺序、测试基线或生产参数。

## 4. 历史资料边界

- [AUDIT-2026-10-12](AUDIT-2026-10-12.md) 只核对 `main/b63ad48`；问题是否已修、是否仍存在，应按目标提交重新审查。
- [TODO](../TODO.md) 中带日期的 PR 时间线、性能基准和决策记录仅说明对应时点；活动项才代表该文档更新时记录的待办，不替代 GitHub 当前状态。
- `docs/designs/` 中标为“提案/历史实施记录”的文件不构成当前代码规格。实施状态以文档状态标记、对应提交和当前源码为准。

本仓库文档不记录开发者的私人账号、QQ 号、电脑用户名/目录、代理配置、真实密钥、未公开主机地址或具体生产运行参数。需要真实环境操作时，按授权流程通过受控渠道核实，不把信息复制进 Git。
