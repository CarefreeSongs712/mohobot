# Mohobot — 多机器人 AI 框架

> ⚠️ **开发阶段**: 本项目目前处于积极开发中，API、数据格式与功能均可能发生破坏性变更，请谨慎用于生产环境。

基于 Python 异步框架 + [OneBot v11](https://github.com/botuniverse/onebot-11) 标准的**多 Bot AI 框架**。支持同时接入多个 QQ 机器人，通过 LLM 驱动对话。

> **Agent 子系统（beta）**：回复路径移植自 [Agent-LuoTianyi](https://github.com/CarefreeSongs712/Agent-LuoTianyi) 的意识/潜意识双层架构（话题规划 → 注意力 → 风格化回复 → 反思记忆），按 bot 隔离；历史对话写入 SQLite（与 Agent-LuoTianyi 共享同一数据库文件），会话上下文仍由 JSON/JSONL 管理。

## ✨ 功能特性

- **多 Bot 接入** — 反向 WebSocket (Reverse WebSocket) 服务端，一个进程同时服务多个机器人
- **Agent 子系统（beta）** — 移植自 Agent-LuoTianyi 的回复流水线，按 bot 隔离：
  - `TopicPlanner` 缓冲未读消息、判断用户是否说完，批量提取话题
  - `TopicExtractor` LLM 提取话题与记忆检索线索（含群聊说话人标注）
  - `AttentionPlanner` 并行召回记忆 / 事实 / 唱歌规划（无 TTS 时自动跳过）
  - `MainChat` 结构化回复（`[tone]内容` 每行一句），`CharacterReflex` 戳一戳等低延迟反射
  - `ReflectionWorker` 回合后串行反思：写入长期记忆 + 更新用户画像
  - `SubconsciousMemory` 向量检索（ChromaDB，未配置时优雅降级）+ 数据库记忆正本
- **LLM 驱动对话** — OpenAI 兼容 API，支持流式回复（标点+长度分段发送）、函数调用（Tools）、视觉识别（Vision）
- **会话上下文管理** — 私聊支持多会话切换，群聊单一会话；上下文自动裁剪（最近 30 轮），记录每条消息的说话人（QQ号-昵称）
- **历史对话入库** — 聊天记录写入 SQLite `conversations` 表（与 Agent-LuoTianyi 共用 `luotianyi.db`，用户名以 `qq_` 前缀隔离），原始事件另以 JSONL 只读归档
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
| 存储 | SQLite（历史/记忆）+ JSON/JSONL（会话上下文与原始归档） |
| LLM | OpenAI SDK（兼容适配层） |
| 向量检索 | ChromaDB（可选，缺失时自动降级） |
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
export MOHOBOT_VISION_API_KEY="sk-xxx"   # Vision 模型（无视觉需求可留空）

# 4. 启动
python main.py
```

> 当前开发分支为 `beta`（Agent 子系统 + 数据库存储），主分支 `main` 为旧版文件存储架构。

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

私聊机器人发送任意消息即可得到回复。默认启用 Agent 流水线（话题提取 → 回复，回复按行分段发送、首段引用触发消息）；若在 `config/global.yaml` 中设置 `agent.enabled: false`，则回退到旧版直接流式回复路径。

## 🤖 Agent 子系统（beta）

回复路径移植自 [Agent-LuoTianyi](https://github.com/CarefreeSongs712/Agent-LuoTianyi)，`config/global.yaml` 的 `agent:` 段配置：

```yaml
agent:
  enabled: true                # false = 旧版直接流式回复
  persona:
    # 默认留空: 每个 bot 使用各自的 data/bots/{bot_id}/config.json 的
    # persona / 昵称作为子系统人设(按 bot 隔离, 互不影响)。
    # 如需全局统一人设, 在此填写即可覆盖所有 bot。
    character_name: ""         # 留空 = 使用各 bot 昵称
    character_persona: ""      # 留空 = 使用各 bot 自己的 persona
    speaking_style: ""         # 留空 = 自然、简洁
  llm_modules:                 # 各模块可独立覆盖模型；留空继承 llm.* 全局配置
    main_chat:                 # 回复生成
      model: ""
      base_url: ""
      api_key: ""
      temperature: 0.7
      max_tokens: 2048
    topic_extractor:           # 话题提取（JSON 模式）
      model: ""
      base_url: ""
      api_key: ""
      temperature: 0.3
      max_tokens: 1024
    memory_writer:             # 记忆抽取（JSON 模式）
      model: ""
      base_url: ""
      api_key: ""
      temperature: 0.3
      max_tokens: 1024
    user_profile_updater:      # 用户画像更新
      model: ""
      base_url: ""
      api_key: ""
      temperature: 0.3
      max_tokens: 1024
  memory:
    user_memory_dedup_threshold: 0.72
    vector_store:              # ChromaDB 向量存储（未配置 key 自动降级）
      enabled: false
      provider: chroma
      persist_dir: "./data/database/chroma"
      collection_name: "mohobot_memories"
      embedding_model: "BAAI/bge-large-zh-v1.5"   # SiliconFlow 等 OpenAI 兼容端点
      embedding_base_url: ""
      embedding_api_key: ""
  main_chat:
    max_output_lines: 12
  topic_planner:
    listen_timer:
      timeout: 1.5             # 等待用户说完的静默时长（秒）
    unread_store:
      max_size: 50
  topic_replier: {}
  reflection_worker:
    enabled: true              # 回合后反思
    write_memory: true         # 写入长期记忆
    update_user_profile: true  # 更新用户画像
  reflex:
    enabled: true              # 戳一戳反射回复
```

### 数据库配置

```yaml
database:
  enabled: true
  folder: "./data/database"
  file: "luotianyi.db"         # 与 Agent-LuoTianyi 共享同一数据库文件
```

> 记忆写入采用"向量索引 + 数据库正本"双写：向量检索仅用于召回线索，数据库正本为最终依据；未配置 embedding 时检索自动降级为空实现，记忆仍会写入数据库正本。

## 📁 目录结构

```
mohobot/
├── main.py                        # 入口
├── config/
│   ├── global.example.yaml        # 配置模板（复制为 global.yaml 使用）
│   └── global.yaml                # 本地配置（已 gitignore，含密钥）
├── mohobot/
│   ├── agent/                     # Agent 子系统（beta，移植自 Agent-LuoTianyi）
│   │   ├── topic_planner.py       # 未读消息缓冲 + 静默计时 + 话题提取调度
│   │   ├── topic_extractor.py     # LLM 话题提取（含群聊说话人标注）
│   │   ├── attention.py           # 注意力规划（记忆/事实/唱歌并行检索）
│   │   ├── main_chat.py           # 结构化回复（[tone]内容 每行一句）
│   │   ├── topic_replier.py       # 话题回复队列
│   │   ├── reflection_worker.py   # 回合后反思（写记忆 + 更新画像）
│   │   ├── character_mind.py      # 潜意识门面（召回/规划/写入）
│   │   ├── character_reflex.py    # 低延迟反射（戳一戳）
│   │   ├── subconscious_memory.py # 记忆门面（向量检索 + DB 正本回查）
│   │   ├── memory_writer.py       # 记忆抽取与双写（批量去重）
│   │   ├── user_profile_updater.py# 用户画像更新
│   │   ├── vector_store.py        # ChromaDB 向量存储（可降级为空实现）
│   │   ├── llm_module.py          # 模块化 LLM 调用（Jinja2 模板）
│   │   ├── runtime.py             # 按 bot 组装（BotAgentRuntime + 会话流水线）
│   │   ├── domain.py / prompts.py # 数据模型与提示词模板
│   ├── db/                        # 数据库层（与 Agent-LuoTianyi 共享 SQLite）
│   │   ├── sql_database.py        # SQLAlchemy 模型 + 迁移
│   │   └── database_manager.py    # 用户/会话记录/记忆正本读写
│   ├── ws_server.py               # 反向 WebSocket 服务器
│   ├── bot_manager.py             # Bot 生命周期与连接管理
│   ├── message_handler.py         # 消息处理管线（拦截器 → Agent 流水线）
│   ├── context_manager.py         # 会话上下文管理（JSON，不变）
│   ├── llm_service.py             # LLM 服务（旧路径：流式、工具调用、视觉）
│   ├── file_store.py              # 异步文件存储（锁保护）
│   ├── image_cache.py             # 图片缓存与 phash 去重
│   ├── interceptors/              # 拦截器（指令、关键词、插件系统）
│   ├── models/                    # OneBot 协议与配置模型
│   ├── utils/                     # 日志、CQ 码解析等工具
│   └── web_panel/                 # FastAPI 管理面板（7 板块）
├── plugins/                       # 插件目录（动态加载：status / praise）
├── tests/                         # 冒烟测试（smoke_*）与单测
├── data/                          # 运行时数据（自动生成，勿提交）
│   ├── bots/{bot_id}/             # Bot 配置与状态
│   ├── history/{bot_id}/          # 【只读】原始聊天记录 JSONL
│   ├── contexts/{bot_id}/         # 【可读写】会话上下文
│   ├── database/                  # SQLite（luotianyi.db，与洛天依共享）
│   └── cache/images/              # 图片缓存
└── logs/                          # 日志（自动生成，勿提交）
```

## 🧠 数据架构

**原则：原始数据不可变，工作数据可变；历史入库，上下文不变。**

| 数据 | 位置 | 性质 | 格式 | 用途 |
|------|------|------|------|------|
| 聊天历史 | `data/history/` | 只读归档 | JSONL（每行一个事件） | 审计、全量回溯、训练导出 |
| 会话上下文 | `data/contexts/` | 可读写工作区 | JSON（数组） | LLM 实时推理的记忆（**保持原有管理方式不变**） |
| 对话记录 | SQLite `conversations` 表 | 可读写 | SQL | 历史入库，与 Agent-LuoTianyi 共享同一 `luotianyi.db` |
| 长期记忆 | SQLite `agent_memory_records`/`memory_chunks` | 可读写 | SQL | 记忆正本（向量库仅作索引，可降级） |
| 向量索引 | `data/database/chroma/`（可选） | 可读写 | ChromaDB | 记忆语义检索（未配置 embedding 时自动降级为空实现） |

- **聊天历史**：按 Bot ID → 私聊/群聊 → 用户/群号 分文件，**绝不**用于 LLM 实时输入
- **会话上下文**：私聊一个用户可有多个会话（`sess_001`…由 `session_index.json` 索引），群聊固定 `main.json`；每个上下文仅保留最近 30 轮
- **数据库隔离**：mohobot 用户以 `qq_{QQ}` 前缀写入共享库，与洛天依用户互不干扰；记忆按 bot（`owner_character_id`）隔离

## 💬 聊天指令

| 指令 | 说明 |
|------|------|
| `/sess list` | 列出当前用户的所有会话 |
| `/sess new <name>` | 创建新会话并切换 |
| `/sess switch <id>` | 切换会话 |
| `/sess del <id>` | 删除会话 |
| `/forget <n>` | 删除当前会话最近 n 条记录 |
| `/hist` | 打印当前会话内容（调试） |
| `/clear` | 清空当前会话 |
| `/help` | 显示全部可用指令 |
| `/status` | 显示框架与系统状态（插件） |
| `赞我` / `zanwo` | 给自己点 20 个赞（插件，每日上限 10 次/人） |

## 🖥️ Web 管理面板

启动后访问 `http://127.0.0.1:9090`（默认用户名 `admin`，密码在 `config/global.yaml` 的 `web_panel.password_hash` 中配置）：

1. 📊 **数据总览** — 系统/框架/Bot/LLM token 统计
2. ⚙️ **配置文件** — 全局 + 每 Bot 配置可视化编辑（含 Agent 子系统与数据库配置）
3. 🧠 **模型配置** — Chat / Vision 模型、端点与密钥
4. 🔌 **插件管理** — 启停插件
5. 💬 **对话数据** — 浏览/编辑各会话上下文
6. 📋 **实时日志** — SSE 日志流，支持多选级别筛选（DEBUG/INFO/WARN/ERROR）
7. 🔧 **系统设置** — 修改密码、重启服务

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

当前处于 **beta 开发阶段**：Agent 子系统为实验性功能，API、数据格式与配置均可能发生破坏性变更，请谨慎用于生产环境。

- [x] 反向 WebSocket 多 Bot 接入（重连竞态防护）
- [x] Agent 子系统（beta）：话题规划 → 注意力 → 结构化回复 → 反思记忆，按 bot 隔离
- [x] 历史对话入库（SQLite，与 Agent-LuoTianyi 共享 `luotianyi.db`，`qq_` 前缀隔离）
- [x] 长期记忆：向量检索（ChromaDB，未配置时优雅降级）+ 数据库正本 + 用户画像
- [x] LLM 流式对话（旧路径：分段回复、工具调用、视觉识别）
- [x] VLM 图片理解（agent 路径自动描述图片；base64:// 兼容）
- [x] 会话上下文管理（多会话、裁剪、说话人记录，context 机制不变）
- [x] 群聊触发门控（@机器人 / 引用机器人消息）+ 戳一戳反射回复
- [x] Web 管理面板（7 板块：总览/配置/模型/插件/对话/日志/设置）
- [x] 插件系统（status / praise）
- [ ] 消息发送限流与队列（当前仅图片突发限流）
- [ ] 单元测试与 CI（当前为本地冒烟测试 `tests/`）
- [ ] Docker 部署
- [ ] Agent 子系统由 beta 转正前：真实多 bot 长期运行验证

## 📄 License

[GPL-3.0](LICENSE)

## 🙏 致谢

- [OneBot 标准](https://github.com/botuniverse/onebot-11) — 聊天机器人应用接口标准
- [Agent-LuoTianyi](https://github.com/CarefreeSongs712/Agent-LuoTianyi) — Agent 子系统架构参考（话题规划/潜意识/反思记忆）
