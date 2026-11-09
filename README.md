# Mohobot

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/CarefreeSongs712/mohobot/actions/workflows/ci.yml/badge.svg)](https://github.com/CarefreeSongs712/mohobot/actions/workflows/ci.yml)

Mohobot 是基于 Python asyncio 与 OneBot v11 反向 WebSocket 的多机器人框架，通过 OpenAI 兼容接口驱动对话，并提供插件、Web 管理面板与独立数据存储。

## 文档导航

| 文档 | 用途 |
|---|---|
| [开发守则](AGENTS.md) | 数据边界、生命周期、扩展、安全与授权约束 |
| [开发指南](docs/DEVELOPMENT.md) | 当前代码结构、配置/插件/LLM 工具开发、测试与 CI |
| [代码地图](docs/CODE-MAP.md) | 模块定位、关键调用链与修改入口 |
| [插件速查](docs/PLUGINS.md) | 各内置插件的命令与配置入口 |
| [贡献指南](CONTRIBUTING.md) | 分支/PR 流程与提交前检查 |
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

## 项目结构

```text
mohobot/
├─ main.py             # 装配入口：创建/注入/启动/热同步/优雅关闭
├─ mohobot/            # 框架核心（消息主链、LLM、上下文、插件系统、面板…）
├─ plugins/            # 内置插件（命令见 docs/PLUGINS.md）
├─ review/             # 独立审核面板（只读 history）
├─ scripts/            # 迁移/同步/维护脚本（多数需停机或授权）
├─ tests/              # pytest、fake OneBot、runner 与冒烟测试
├─ config/             # global.example.yaml 模板；本地 global.yaml 不入库
├─ deploy/             # 新环境 systemd 模板
└─ docs/               # 开发/部署/代码地图/插件/历史审计
```

模块级定位见[代码地图](docs/CODE-MAP.md)。

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

以下为少量内置入口；插件指令和权限以 `/help` 输出及[插件速查](docs/PLUGINS.md)为准。

| 指令 | 说明 |
|---|---|
| `/help [分类]` | 查看可用指令；管理员指令按权限折叠 |
| `/status` | 查看运行状态 |
| `/sess list`、`/sess new <名称>`、`/sess switch <ID>`、`/sess del <ID>` | 管理当前私聊会话 |
| `/persona …`【管理员】 | 管理私聊专属人设，见下文「私聊专属人设与人设评估」 |
| `/forget <数量>`、`/clear` | 删除近期上下文或清空当前上下文 |
| `/查看聊天 <QQ号|群号> [条数]`、`/发送消息 <QQ号|群号> <内容>`【管理员】 | 查看/代发消息（chat_manager 插件） |
| `/点歌 <关键词>`（别名 `/music` `/听歌` `/网易云`） | 网易云点歌，回复数字选歌 |
| `ping` | 不区分大小写、无需斜杠，回复 `PONG` |
| `/tts <文本>` | 按 TTS 配置合成语音；需要启用 TTS 服务 |

## 私聊专属人设与人设评估

- 人设预设统一在 `data/personas/` 管理（可备份/恢复）；Bot 用配置选择默认人设，正文仅作请求级提示、不写入上下文。
- 私聊可为 `bot_id + 用户QQ + session_id` 设专属人设；解析顺序**私聊 session 专属 → bot 默认 → 系统默认**。`/persona list|sessions|set|get|clear`（管理员）不创建也不切换会话，目标需已存在；删除 session 会同步移除专属引用，清空消息则保留。
- **匿名人设评估（`/ab`，默认关闭，仅指定 bot 的私聊）**：命中后生成「基准/测试」两版并双栏图卡展示，投票/切换不改变历史。`/ab A|B|平局|都不好|跳过` 投票；`/ab vote <id> <选项>` 补投；`/ab switch <id> A|B` 切回原会话（需 generation 与人设 hash 未变）。统计表示单轮同历史偏好；数据存 `data/persona_ab/`，独立 JSON 导出，聊天备份不含该数据且首版不支持恢复导入；重启保留 pending/冷却，中断不自动重发。

## Web 面板主要板块

数据总览 ｜ 配置文件（全局 + 每 Bot）｜ 模型配置（含多提供商）｜ TTS 语音 ｜ 插件管理（schema 表单，保存热生效）｜ 对话数据 ｜ 实时日志（SSE）｜ 系统设置（改密/进程内重启）｜ 数据管理（备份/恢复/清理）｜ 封禁管理 ｜ 情感管理。密码与 API Key 行为见上文及[部署指南](docs/DEPLOYMENT.md)。

## 数据与目录速览

```
data/
├── bots/{bot_id}/            # Bot 配置
├── history/group/            # 群聊合并归档（message_id+跨账号指纹去重）
├── history/{bot_id}/private/ # 私聊归档（按 bot 隔离）
├── contexts/{bot_id}/        # 工作上下文（可读写）
├── database/                 # SQLite 对话库
└── cache/                    # 图片缓存 / 歌曲索引等
logs/                         # 运行日志（不入库）
```

原始归档是只读回溯/审核材料，不作 LLM 实时输入；数据层细节见 [DEVELOPMENT §4](docs/DEVELOPMENT.md)。

## 技术与状态

主要依赖包括 Python 3.12+、asyncio、websockets、FastAPI、SQLAlchemy/SQLite、OpenAI SDK、Pillow。部分功能有可选依赖，例如关系图渲染使用 Playwright；详见 [`requirements.txt`](requirements.txt) 与插件文档。

项目持续演进，配置、命令与接口可能变化。提交 PR 前按[开发指南](docs/DEVELOPMENT.md)运行与改动相关的测试；PR 的通过状态以对应 GitHub Actions 运行为准。

## 参与贡献

欢迎功能、修复、测试与文档 PR：流程、提交前检查与文档职责见[贡献指南](CONTRIBUTING.md)，硬性约束见[开发守则](AGENTS.md)，社区行为规范见[行为准则](CODE_OF_CONDUCT.md)。代码定位从[代码地图](docs/CODE-MAP.md)开始；插件用法与配置见[插件速查](docs/PLUGINS.md)。

## 安全提示

- 不要将 API Key、Cookies、密码哈希、`config/global.yaml`、`data/`、`logs/` 提交到仓库或贴进 issue/PR。
- 管理面板与审核面板默认只监听回环地址，首次部署必须显式配置密码；不要把面板直接暴露到不受信任网络，需要远程访问时使用 HTTPS 反向代理、VPN 或白名单。
- 反向 WS 面向不可信网络时应配置 `server.access_token` 并限制来源；部署前按[部署指南](docs/DEPLOYMENT.md)的检查单核实实际服务与权限。
- 发现安全问题请按[安全策略](SECURITY.md)通过私密渠道报告，不要在公开 issue 中贴出可利用细节与真实凭据。

## 更新日志

版本发布说明与合并记录见 GitHub [Releases](https://github.com/CarefreeSongs712/mohobot/releases) 与 `main` 的提交历史；未完成事项与决策记录见[待办清单](TODO.md)。本 README 不维护逐版本流水，避免与发布线脱节。

## 许可证

核心框架采用 [MIT](LICENSE)。移植插件保留各自上游许可；表情包插件和主动聊天插件分别包含 [AGPL-3.0](plugins/meme_stealer/LICENSE) 与 [AGPL-3.0](plugins/proactive_chat/LICENSE)。分发或提供网络服务前应检查各组件许可义务。
