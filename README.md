# Mohobot — 多机器人 AI 框架

> ⚠️ **开发阶段**: 本项目目前处于积极开发中，API、数据格式与功能均可能发生破坏性变更，请谨慎用于生产环境。

基于 Python 异步框架 + [OneBot v11](onebot-11-doc/) 标准的**多 Bot AI 框架**。支持同时接入多个 QQ 机器人，通过 LLM 驱动对话，全部数据使用本地文件存储（无数据库）。

## ✨ 功能特性

- **多 Bot 接入** — 反向 WebSocket (Reverse WebSocket) 服务端，一个进程同时服务多个机器人
- **LLM 驱动对话** — OpenAI 兼容 API，支持流式回复（标点+长度分段发送）、函数调用（Tools）、视觉识别（Vision）
- **会话上下文管理** — 私聊支持多会话切换，群聊单一会话；上下文自动裁剪（最近 30 轮），记录每条消息的说话人（QQ号-昵称）
- **原始数据不可变** — 聊天记录以 JSONL 只读归档，与可变的 AI 工作上下文分离
- **智能群聊触发** — 群聊中仅在 @机器人 或 引用机器人自己的消息 时才触发 LLM 回复
- **图片缓存与去重** — phash 感知哈希去重 + LRU 缓存（300MB 上限），图片消息只解析首张
- **插件系统** — 从 `plugins/` 目录动态加载插件，可拦截消息、响应事件
- **Web 管理面板** — FastAPI + SSE 实时日志流、文件系统浏览器、配置在线编辑、统计看板
- **可配置拦截器** — 指令拦截（`/` 开头）、关键词拦截（预设回复）

## 🏗️ 技术栈

| 分类 | 技术 |
|------|------|
| 语言 | Python 3.10+ |
| 异步 | asyncio |
| WebSocket | websockets |
| Web 面板 | FastAPI + SSE |
| 存储 | JSON / JSONL（无数据库） |
| LLM | OpenAI SDK（兼容适配层） |
| 日志 | loguru（轮换） |
| 图片 | Pillow + phash |

## 📦 安装

```bash
# 1. 克隆仓库
git clone https://github.com/CarefreeSongs712/mohobot.git
cd mohobot

# 2. 安装依赖（建议使用虚拟环境）
pip install -r requirements.txt

# 3. 配置 API Key
cp config/global.example.yaml config/global.yaml   # 生成本地配置（已被 .gitignore 排除）
# 编辑 config/global.yaml 填入 API Key，或设置环境变量：
export MOHOBOT_LLM_API_KEY="sk-xxx"      # Chat 模型
export MOHOBOT_VISION_API_KEY="sk-xxx"   # Vision 模型

# 4. 启动
python main.py
```

## 🚀 快速开始

### 1. 配置机器人

在 OneBot 实现（如 NapCat、LLOneBot、Lagrange 等）中配置反向 WebSocket 连接：

```
ws://127.0.0.1:8081/ws
```

> 端口在 `config/global.yaml` 的 `server.port` 中配置。

### 2. 添加 Bot 配置

框架启动后，`data/bots/{bot_id}/config.json` 会自动创建，可编辑：

```json
{
  "qq": 123456789,
  "nickname": "我的机器人",
  "persona": "你是 Mohobot，一个有用的 AI 助手。",
  "enabled": true
}
```

### 3. 验证

私聊机器人发送任意消息即可得到流式分段回复。

## 📁 目录结构

```
mohobot/
├── main.py                        # 入口
├── config/
│   ├── global.example.yaml        # 配置模板（复制为 global.yaml 使用）
│   └── global.yaml                # 本地配置（已 gitignore，含密钥）
├── mohobot/
│   ├── ws_server.py               # 反向 WebSocket 服务器
│   ├── bot_manager.py             # Bot 生命周期与连接管理
│   ├── message_handler.py         # 消息处理管线
│   ├── context_manager.py         # 会话上下文管理
│   ├── llm_service.py             # LLM 服务（流式、工具调用、视觉）
│   ├── file_store.py              # 异步文件存储（锁保护）
│   ├── image_cache.py             # 图片缓存与 phash 去重
│   ├── interceptors/              # 拦截器（指令、关键词、插件系统）
│   ├── models/                    # OneBot 协议与配置模型
│   ├── utils/                     # 日志、CQ 码解析等工具
│   └── web_panel/                 # FastAPI 管理面板
├── plugins/                       # 插件目录（动态加载）
├── data/                          # 运行时数据（自动生成，勿提交）
│   ├── bots/{bot_id}/             # Bot 配置与状态
│   ├── history/{bot_id}/          # 【只读】原始聊天记录 JSONL
│   ├── contexts/{bot_id}/         # 【可读写】会话上下文
│   └── cache/images/              # 图片缓存
└── logs/                          # 日志（自动生成，勿提交）
```

## 🧠 数据架构

**原则：原始数据不可变，工作数据可变。**

| 数据 | 目录 | 性质 | 格式 | 用途 |
|------|------|------|------|------|
| 聊天历史 | `data/history/` | 只读归档 | JSONL（每行一个事件） | 审计、全量回溯、训练导出 |
| 会话上下文 | `data/contexts/` | 可读写工作区 | JSON（数组） | LLM 实时推理的记忆 |

- **聊天历史**：按 Bot ID → 私聊/群聊 → 用户/群号 分文件，**绝不**用于 LLM 实时输入
- **会话上下文**：私聊一个用户可有多个会话（`sess_001`…由 `session_index.json` 索引），群聊固定 `main.json`；每个上下文仅保留最近 30 轮

## 💬 聊天指令

| 指令 | 说明 |
|------|------|
| `/sess list` | 列出当前用户的所有会话 |
| `/sess new <name>` | 创建新会话并切换 |
| `/sess switch <id>` | 切换会话 |
| `/sess del <id>` | 删除会话 |
| `/forget <n>` | 删除当前会话最近 n 条记录 |
| `/hist` | 打印当前会话内容（调试） |
| `/status` | 显示框架与系统状态（插件） |

## 🖥️ Web 管理面板

启动后访问 `http://127.0.0.1:9090`（默认 `admin`，密码在 `config/global.yaml` 的 `password_hash` 中配置）：
- 📋 实时日志流（SSE）
- 📁 文件浏览器（查看 `history` / `contexts` 下的 JSON/JSONL）
- ✏️ 在线编辑配置文件
- 📊 统计看板（消息数、Bot 数等）

生成密码哈希：

```bash
python -c "import hashlib,secrets; salt=secrets.token_hex(16); h=hashlib.pbkdf2_hmac('sha256',b'你的密码',salt.encode(),100000).hex(); print(f'pbkdf2_sha256${salt}${h}')"
```

## 🔌 开发插件

在 `plugins/` 目录创建 `.py` 文件，定义 `Plugin` 类：

```python
class Plugin:
    async def on_message(self, bot_id, event, raw_event):
        """返回 (handled, response)，handled=True 则拦截消息"""
        return (False, None)

    async def on_notice(self, bot_id, event, raw_event):
        pass

    async def on_meta(self, bot_id, event, raw_event):
        pass
```

参考示例：`plugins/status.py`（`/status` 命令）。

## ⚠️ 开发状态

- [x] 反向 WebSocket 多 Bot 接入
- [x] LLM 流式对话（分段回复、工具调用、视觉识别）
- [x] 会话上下文管理（多会话、裁剪、说话人记录）
- [x] 群聊触发门控（@机器人 / 引用机器人消息）
- [x] Web 管理面板
- [x] 插件系统
- [ ] 消息发送限流与队列
- [ ] 会话上下文持久化索引增强
- [ ] 单元测试与 CI
- [ ] Docker 部署

## 📄 License

[GPL-3.0](LICENSE)

## 🙏 致谢

- [OneBot 标准](https://github.com/botuniverse/onebot-11) — 聊天机器人应用接口标准
