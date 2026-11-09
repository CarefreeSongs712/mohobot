# 参与贡献

欢迎提交功能、修复、测试与文档 PR。开始前请先读：

| 文档 | 用途 |
|---|---|
| [AGENTS](AGENTS.md) | 数据、并发、生命周期、扩展、验证、安全与授权的硬性约束 |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | 代码结构、扩展步骤、测试与 CI 口径 |
| [docs/CODE-MAP.md](docs/CODE-MAP.md) | 快速定位模块与修改入口 |
| [docs/PLUGINS.md](docs/PLUGINS.md) | 插件命令与配置入口 |

## 分支与 PR

1. 从最新 `origin/dev` 建分支：`feat/<topic>`、`bugfix/<topic>` 或 `docs/<topic>`。
2. 改动只提交到该分支，通过 PR 合入 `dev`；`main` 只接受维护者的 release PR。
3. PR 描述写清：改了什么、为什么、如何验证；涉及数据/配置/脚本时说明影响面与回滚方式。
4. 涉及运行时行为的改动，按 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) 补回归测试；纯文档改动请在验证中注明"未运行测试"。

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

允许使用 AI 辅助编写代码与文档，但提交者需要能解释、调试并维护所提交的内容；AI 生成的说明不能替代实际验证结果。描述行为时请以源码、测试和本次运行的退出码为依据。

## 许可

核心框架为 [MIT](LICENSE)；各移植插件保留上游许可（如 `plugins/meme_stealer`、`plugins/proactive_chat` 的 AGPL-3.0），提交前确认改动未违反对应许可。
