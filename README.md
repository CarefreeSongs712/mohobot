# Mohobot

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/CarefreeSongs712/mohobot/actions/workflows/ci.yml/badge.svg)](https://github.com/CarefreeSongs712/mohobot/actions/workflows/ci.yml)

Mohobot 是基于 Python asyncio 与 OneBot v11 反向 WebSocket 的多机器人框架：一个进程接入多个 QQ 机器人，通过 OpenAI 兼容接口驱动对话，并提供插件、Web 管理面板与独立数据存储。

> ⚠️ **开发阶段**：本项目目前处于积极开发中，API、数据格式与功能均可能发生破坏性变更，请谨慎用于生产环境。
>
> 本项目绝大部分代码为 vibecoding，且没有经过人工详细审查；极大可能存在 AI slop，请谨慎使用。

发布记录见 GitHub [Releases](https://github.com/CarefreeSongs712/mohobot/releases)。

## 能力概览

- **多 Bot 接入**：单进程通过反向 WebSocket 接入多个 OneBot v11 客户端。
- **LLM 对话**：OpenAI 兼容接口、流式分段回复、工具调用与视觉输入。
- **群聊路由**：@ 或引用定向、命令、`ping` 触发；多 Bot 全局指令去重。
- **上下文与归档**：私聊多会话、群聊单会话；工作上下文、SQLite 对话记录与 JSONL 原始归档分开管理。
- **知识与插件**：虚拟歌手 RAG 知识库、歌曲识别；插件支持消息钩子、配置 schema 与 LLM 工具注册。
- **管理与审核**：FastAPI Web 面板；可选的独立审核面板只读访问消息归档。

## 安装与最小启动

支持 Python 3.12+；依赖版本以仓库 requirements 文件为准。

```bash
git clone https://github.com/CarefreeSongs712/mohobot.git
cd mohobot
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config/global.example.yaml config/global.yaml
```

编辑 `config/global.yaml`，至少配置 Chat API Key（`llm.chat_api_key`，或环境变量 `MOHOBOT_LLM_API_KEY`）。示例配置默认启用 Web 面板，启动前需设置 `web_panel.password_hash`，或用 `MOHOBOT_WEB_PASSWORD` 初始化密码；不使用面板可把 `web_panel.enabled` 设为 `false`。

```bash
.venv/bin/python main.py
```

OneBot 客户端连接 `ws://<主机>:8081/ws`（端口以 `server.port` 为准）。新连接不会自动分配 Bot，需先在 Web 面板创建 Bot 并绑定 QQ。鉴权、systemd 与发布检查见[部署指南](docs/DEPLOYMENT.md)；配置字段、面板与命令见[配置与管理员参考](docs/ADMIN.md)与 `/help`。

## 文档导航

| 文档 | 用途 |
|---|---|
| [开发指南](docs/DEVELOPMENT.md) | 代码结构、配置、插件与 LLM 工具开发、测试与 CI |
| [代码地图](docs/CODE-MAP.md) | 模块定位、关键调用链与修改入口 |
| [插件速查](docs/PLUGINS.md) | 内置插件命令与配置入口 |
| [配置与管理员参考](docs/ADMIN.md) | 配置入口、管理员权限、常用命令、数据与备份边界 |
| [部署指南](docs/DEPLOYMENT.md) | 新环境部署、systemd 模板与发布前检查 |
| [贡献指南](CONTRIBUTING.md) | 分支与 PR 流程、提交前检查 |
| [开发守则](AGENTS.md) | 数据、生命周期、扩展与授权的硬性约束 |
| [安全策略](SECURITY.md) | 漏洞的私密报告渠道 |
| [待办清单](TODO.md) | 未完成事项与决策记录 |

插件另有[表情包](plugins/meme_stealer/README.md)与[主动聊天](plugins/proactive_chat/README.md)独立文档。

## 安全提示

- 不要提交 API Key、Cookies、密码哈希、`config/global.yaml`、`data/` 或 `logs/`。
- 管理面板与审核面板默认只监听回环地址，必须显式配置密码；远程访问使用 HTTPS 反向代理、VPN 或白名单。
- 反向 WS 面向不可信网络时应配置 `server.access_token` 并限制来源。
- 安全问题按[安全策略](SECURITY.md)私密报告，不要公开披露细节或真实凭据。

## 许可证

核心框架采用 [MIT](LICENSE)。移植插件保留上游许可：表情包插件与主动聊天插件分别为 [AGPL-3.0](plugins/meme_stealer/LICENSE) 与 [AGPL-3.0](plugins/proactive_chat/LICENSE)，分发或提供网络服务前应核对各自义务。
