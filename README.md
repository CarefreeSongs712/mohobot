# Mohobot

Mohobot 是基于 Python asyncio 与 OneBot v11 反向 WebSocket 的多机器人框架，通过 OpenAI 兼容接口驱动对话，并提供插件、Web 管理面板与独立数据存储。

## 文档导航

| 文档 | 用途 |
|---|---|
| [开发守则](AGENTS.md) | 数据边界、生命周期、扩展、安全与授权约束 |
| [开发指南](docs/DEVELOPMENT.md) | 当前代码结构、配置/插件/LLM 工具开发、测试与 CI |
| [部署指南](docs/DEPLOYMENT.md) | 通用新环境部署模板及发布前检查；不包含任何特定生产实例信息 |
| [待办清单](TODO.md) | 当前记录的未完成事项与简要历史决策；实时 PR/CI 状态以 GitHub 为准 |
| [接手说明](docs/HANDOVER.md) | 接手步骤与文档索引，不记录环境实例状态 |
| [历史审计](docs/AUDIT-2026-10-12.md) | 仅适用于 `main/b63ad48` 的历史缺陷审计，不代表当前问题清单 |
| 插件文档 | [表情包插件](plugins/meme_stealer/README.md)、[主动聊天](plugins/proactive_chat/README.md) 及各插件目录内说明 |

## 能力概览

- **多 Bot 接入**：一个进程通过反向 WebSocket 接入多个 OneBot v11 客户端。
- **LLM 对话**：OpenAI 兼容 API、流式回复、工具调用和视觉输入；`vision_mode` 支持 `describe` 与 `direct`，配置示例默认使用 `describe`。
- **群聊路由**：支持 @/引用定向、命令与 `ping`；群内全局指令进行多 Bot 去重。
- **上下文与归档**：私聊多会话、群聊单会话；工作上下文、SQLite 对话记录和 JSONL 原始归档分开管理。群聊归档按 message_id 去重，并用跨账号稳定指纹处理不同 Bot 收到的同一条消息。
- **知识与插件**：虚拟歌手 RAG 知识库、歌曲识别与多来源同步；插件系统提供消息钩子、配置 schema 和 LLM 工具注册。
- **管理与审核**：FastAPI Web 面板；可选的独立审核面板读取消息归档。
- **其他功能**：封禁、情感系统、TTS、人设管理与单轮匿名人设评估等功能按配置启用。详细功能、命令与插件状态见对应插件文档及代码配置。

## 安装与最小启动

支持 Python 3.12+。下面示例使用虚拟环境；依赖版本以仓库 requirements 文件为准。

```bash
git clone https://github.com/CarefreeSongs712/mohobot.git
cd mohobot
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config/global.example.yaml config/global.yaml
```

编辑 `config/global.yaml`，配置可用的 Chat API Key（也可用 `MOHOBOT_LLM_API_KEY` 环境变量）。示例配置默认启用 Web 面板，因此启动前必须设置 `web_panel.password_hash`，或用 `MOHOBOT_WEB_PASSWORD` 初始化密码；若不使用面板，可将 `web_panel.enabled` 设为 `false`。之后启动：

```bash
.venv/bin/python main.py
```

配置示例的 OneBot 服务端口为 `8081`，连接地址格式为 `ws://<主机>:<端口>/ws`。新连接不会自动分配 Bot：在 Web 面板创建 Bot 并绑定 QQ 后，才会处理该连接的消息。端口、鉴权和 systemd 部署见[部署指南](docs/DEPLOYMENT.md)。

## Web 面板与审核

Web 面板默认监听回环地址的 `9090` 端口，登录密码必须显式配置；不要将管理面板直接暴露到不受信任的网络。更多访问与部署安全要求见[部署指南](docs/DEPLOYMENT.md)。

审核面板为可选组件，需提供 `review/config.yaml` 并启用后由应用拉起；示例配置监听回环地址的 `9091` 端口。审核面板使用独立数据库，并只读访问 Mohobot 的消息归档。模板见 [`review/config.example.yaml`](review/config.example.yaml)。

## 常用指令

以下为少量内置入口；插件指令和权限以 `/help` 输出及各插件文档为准。

| 指令 | 说明 |
|---|---|
| `/help [分类]` | 查看可用指令；管理员指令按权限折叠 |
| `/status` | 查看运行状态 |
| `/sess list`、`/sess new <名称>`、`/sess switch <ID>`、`/sess del <ID>` | 管理当前私聊会话 |
| `/forget <数量>`、`/clear` | 删除近期上下文或清空当前上下文 |
| `ping` | 不区分大小写、无需斜杠，回复 `PONG` |
| `/tts <文本>` | 按 TTS 配置合成语音；需要启用 TTS 服务 |

## 技术与状态

主要依赖包括 Python 3.12+、asyncio、websockets、FastAPI、SQLAlchemy/SQLite、OpenAI SDK、Pillow。部分功能有可选依赖，例如关系图渲染使用 Playwright；详见 [`requirements.txt`](requirements.txt) 与插件文档。

项目持续演进，配置、命令与接口可能变化。提交 PR 前按[开发指南](docs/DEVELOPMENT.md)运行与改动相关的测试；PR 的通过状态以对应 GitHub Actions 运行为准。

## 许可证

核心框架采用 [MIT](LICENSE)。移植插件保留各自上游许可；表情包插件和主动聊天插件分别包含 [AGPL-3.0](plugins/meme_stealer/LICENSE) 与 [AGPL-3.0](plugins/proactive_chat/LICENSE)。分发或提供网络服务前应检查各组件许可义务。
