# Mohobot — 多机器人 AI 框架

> ⚠️ **开发阶段**: 本项目目前处于积极开发中，API、数据格式与功能均可能发生破坏性变更，请谨慎用于生产环境。

基于 Python 异步框架 + [OneBot v11](https://github.com/botuniverse/onebot-11) 标准的**多 Bot AI 框架**。支持同时接入多个 QQ 机器人，通过 LLM 驱动对话。

> **Agent 子系统（beta）**：回复路径移植自 [Agent-LuoTianyi](https://github.com/CarefreeSongs712/Agent-LuoTianyi) 的意识/潜意识双层架构（话题规划 → 注意力 → 风格化回复 → 反思记忆），按 bot 隔离；历史对话写入独立 SQLite（架构借鉴 Agent-LuoTianyi），会话上下文仍由 JSON/JSONL 管理。

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
- **会话上下文管理** — 私聊支持多会话切换，群聊单一会话；记录每条消息的说话人（QQ号-昵称）
- **上下文 AI 总结压缩** — 上下文满 40 轮时，用 AI 总结最早的 15 轮并作为"总结块"插入对话最前（不参与后续总结的只有它自己，可嵌套再总结）；总结失败自动降级为直接裁剪
- **群聊最近消息** — 回复时临时注入群内最近 10 条消息（仅内存、不写入上下文、不参与总结），感知群聊氛围
- **历史对话入库** — 聊天记录写入独立 SQLite `conversations` 表（`mohobot.db`），原始事件另以 JSONL 只读归档
- **智能群聊触发** — 群聊中仅在 @机器人、引用机器人消息、命令或 `ping` 时触发 LLM 回复；`ping`（忽略大小写，无需斜杠）直接回复 `PONG`
- **全局指令去重** — 群内多个 bot 时，全局指令（`/占卜` `/help` `/status` `/点歌` 等，含"命令+空格参数"形式）只由 bot_id 最小者回复
- **LLM 备用模型回退** — beta 各 LLM 模块主模型遇连接类错误（连接失败/超时）时自动换用全局备用模型重试一次
- **图片缓存与去重** — phash 感知哈希去重 + LRU 缓存（300MB 上限），图片消息只解析首张
- **插件系统** — 从 `plugins/` 目录动态加载插件，可拦截消息、响应事件；插件配置由 `_conf_schema.json` 驱动，WebUI 可视化编辑热生效
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

**Bot 与 QQ 分离**：bot 使用自动编号的内部标识（`bot_001`…），一个 bot 绑定一个 QQ 号（QQ 唯一绑定）。新 QQ 连接默认**不分配** bot，需在 Web 面板"配置文件 → 创建 Bot / 绑定 QQ"操作；启动时会自动迁移旧版（`data/bots/{qq}`）配置。

框架启动后，`data/bots/{bot_id}/config.json` 会自动创建，可编辑：

```json
{
  "bot_id": "bot_001",
  "qq": 123456789,
  "nickname": "我的机器人",
  "persona": "你是 Mohobot，一个有用的 AI 助手。",
  "enabled": true,
  "agent_enabled": true
}
```

> `qq` 为 0 表示未绑定；`persona` 直接作为该 bot 的 Agent 子系统人设（角色名=昵称，人设=persona）；`agent_enabled` 单独控制该 bot 是否走 Agent 流水线。

### 3. 验证

私聊机器人发送任意消息即可得到回复。默认启用 Agent 流水线（话题提取 → 回复，回复按行分段发送、首段引用触发消息）；若在 `config/global.yaml` 中设置 `agent.enabled: false`，或在某个 bot 的私有配置里设置 `agent_enabled: false`，该 bot 回退到旧版直接流式回复路径。

## 🚀 生产部署（Ubuntu 服务器）

### 1. 环境准备

```bash
# Python 3.12（示例用 pyenv）
sudo apt update
curl -fsSL https://pyenv.run | bash   # 或直接使用系统 Python 3.10+
pyenv install 3.12.13
pyenv global 3.12.13

# 拉取代码 + 依赖
git clone https://github.com/CarefreeSongs712/mohobot.git /opt/mohobot
cd /opt/mohobot
pip install -r requirements.txt
cp config/global.example.yaml config/global.yaml   # 填写 LLM API Key
```

### 2. 可选：关系图渲染（Playwright + Chromium）

`/关系图`、`/rbq排行` 用 Playwright 渲染 HTML→PNG，未安装时自动降级为文本列表：

```bash
pip install playwright
# 官方 CDN 不可用时用国内镜像（生产实测方案）:
# 1) 安装与镜像匹配的 playwright 版本（chromium revision 需与镜像同步版本一致）
pip install playwright==1.52.0
# 2) 手动下载 chromium 到缓存目录（revision 1169 的 npmmirror 有 x64 包）
curl -L -o /tmp/chromium-linux.zip \
  "https://registry.npmmirror.com/-/binary/playwright/builds/chromium/1169/chromium-linux.zip"
curl -L -o /tmp/headless.zip \
  "https://registry.npmmirror.com/-/binary/playwright/builds/chromium/1169/chromium-headless-shell-linux.zip"
mkdir -p ~/.cache/ms-playwright/chromium-1169
cd ~/.cache/ms-playwright/chromium-1169 && python -m zipfile -e /tmp/chromium-linux.zip . && python -m zipfile -e /tmp/headless.zip .
touch INSTALLATION_COMPLETE
chmod +x chrome-linux/chrome chrome-linux/headless_shell
# headless shell 需放到 Playwright 期望的目录（chromium_headless_shell-1169/chrome-linux/）
# 安装系统依赖库
sudo apt-get install -y libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 \
  libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
  libpango-1.0-0 libcairo2 libasound2t64 libatspi2.0-0t64 fonts-noto-cjk
# 验证渲染
python -c "from playwright.async_api import async_playwright; import asyncio
async def t():
    p = await async_playwright().start(); b = await p.chromium.launch(args=['--no-sandbox'])
    pg = await b.new_page(); await pg.set_content('<h1>测试</h1>'); await pg.screenshot(path='/tmp/t.png'); await b.close()
asyncio.run(t())"
```

> 中文渲染需安装 CJK 字体（`fonts-noto-cjk`）；无外网访问时头像（qlogo）加载会留白，其余正常。

### 3. 运行（screen 守护）

```bash
screen -S mohobot
cd /opt/mohobot && python main.py
# Ctrl+A D 脱离；常用管理：
#   screen -ls                         查看会话
#   screen -S mohobot -X stuff "..."   向会话发送命令
#   优雅停止: kill -TERM <pid>          (main.py 捕获 SIGTERM 落盘退出)
```

### 4. 连接 OneBot 客户端

NapCat / LLOneBot / Lagrange 等配置**反向 WebSocket** 连接 `ws://<服务器IP>:8081`（端口见 `config/global.yaml` 的 `server.port`）。多个机器人各开一个连接，`X-Self-ID` 头为各自 QQ；Web 面板（`http://<IP>:9090`）创建 bot 并绑定 QQ 后即可使用。

### 5. 部署后验证

- 日志确认插件加载：`grep "Loaded plugin" logs/mohobot_*.log`
- Web 面板返回 200：`curl -o /dev/null -w "%{http_code}" http://127.0.0.1:9090/`
- 私聊 bot 发消息验证回复；群内 `/status` 验证多 bot 去重（只由一个 bot 回复）
- 更新部署：`git pull origin main` 后 `kill -TERM <pid>` 重启

## 🤖 Agent 子系统（beta）

回复路径移植自 [Agent-LuoTianyi](https://github.com/CarefreeSongs712/Agent-LuoTianyi)，`config/global.yaml` 的 `agent:` 段配置：

```yaml
llm:
  chat_model: "DeepSeek-V4-Flash"   # 全局默认模型(未单独配置的模块使用)
  chat_base_url: "http://127.0.0.1:36712/v1"
  chat_api_key: "sk-xxx"
  fallback_model: "DeepSeek-V4-Flash"  # 全局备用模型: 各模块连接类失败自动回退(空=不回退)
  models:                            # 可用模型列表(WebUI 下拉选项, 可增删)
    - "DeepSeek-V4-Flash"
    - "Qwen3-8B"
    - "mimo-v2.5"

agent:
  enabled: true                # 全局总开关(需重启); 各 bot 是否启用流水线
                               # 由 bot 私有配置的 agent_enabled 单独控制
  # 角色名/人设/说话风格不在此配置 —— 每个 bot 自动使用自己的 nickname/persona
  llm_modules:                 # 各模块模型在 WebUI 从 llm.models 下拉选择;
                               # base_url/api_key 留空继承 llm.* 全局配置
    main_chat:                 # 回复生成(默认 DeepSeek-V4-Flash)
      model: "DeepSeek-V4-Flash"
      temperature: 0.7
      max_tokens: 2048
    topic_extractor:           # 话题提取, JSON 模式(默认 DeepSeek-V4-Flash)
      model: "DeepSeek-V4-Flash"
      temperature: 0.3
      max_tokens: 1024
    memory_writer:             # 记忆抽取, JSON 模式(默认 Qwen3-8B)
      model: "Qwen3-8B"
      temperature: 0.3
      max_tokens: 1024
    user_profile_updater:      # 用户画像更新(默认 Qwen3-8B)
      model: "Qwen3-8B"
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
    touch_replies:             # 戳一戳固定回复列表(每行一条)
      - "呜哇！吓我一跳～"
      - "嘿嘿，别戳啦～"
      # 留空则各 bot 使用自己的 BotConfig.touch_replies(再留空用内置默认)
```

> 戳一戳固定回复对所有 bot 生效（不依赖 Agent 开关），优先级：bot 私有 `touch_replies` > 全局 `agent.reflex.touch_replies` > 内置默认。

### 上下文压缩与群聊最近消息

```yaml
context_max_rounds: 30                 # 上下文最大轮数(旧版直接裁剪阈值, 保留)
context_summary_enabled: true          # 启用 AI 总结压缩
context_trim_at_rounds: 40             # 满多少轮触发一次压缩
context_trim_remove_rounds: 15         # 每次把最早的多少轮交给 AI 总结
group_recent_msgs_count: 10            # 群聊最近消息条数(回复时临时注入, 0=关闭)
```

- **AI 总结压缩**：上下文满 `context_trim_at_rounds` 轮时，把最早的 `context_trim_remove_rounds` 轮交给 LLM 总结（复用全局 chat 模型，prompt 要求"全局概要 + 重点轮次浓缩"），总结作为 `role="summary"` 的块插入对话最前；总结块视为 1 轮参与后续再总结；总结失败（API 不可用）自动降级为直接裁剪
- **群聊最近消息**：MessageHandler 内存缓冲每群最近 N 条消息（含未 @bot 的，单条截断 80 字），生成回复时临时注入 prompt（agent 路径与旧版路径都生效），**不写入上下文文件、不参与总结压缩**

### 数据库配置

```yaml
database:
  enabled: true
  folder: "./data/database"
  file: "mohobot.db"           # 独立数据库文件
```

> 记忆写入采用"向量索引 + 数据库正本"双写：向量检索仅用于召回线索，数据库正本为最终依据；未配置 embedding 时检索自动降级为空实现，记忆仍会写入数据库正本。

## 🧩 插件系统与插件配置

**插件形态**：单文件插件（`plugins/xxx.py`）或目录插件（`plugins/xxx/main.py` + 可选 `core/` 子模块），热加载/热重载/启停无需重启。

**插件配置系统**：插件目录放 `_conf_schema.json` 声明配置项（类型：`string/int/bool/list/object`+items，含 `description/hint/default/slider/invisible`），配置存于 `data/plugins_config/{name}.json`（全局一份），Web 面板"插件管理"页自动渲染表单编辑、保存即热生效（调用插件 `on_config_update` 回调）。

**事件钩子**：`on_message` / `on_notice` / `on_meta` / `on_request`（好友申请、群邀请，插件接管后框架不再自动同意）。

**注入**：`inject_ws_server` / `inject_bot_manager` / `inject_data_dir` / `inject_anysearch_client` / `inject_admin_ids`（全局管理员，与封禁系统共用配置顶层 `admins`）。

## 👥 关系管理器插件（移植自 astrbot_plugin_relationship）

`plugins/relationship/` — 帮助管理 QQ 好友和群聊（命令带 `/` 前缀，管理员=全局 `admins`，审批员=管理员+配置的额外审批员）：

- **查询/管理**：`/群列表` `/好友列表` `/退群 <序号|群号|区间>` `/删好友 <@|QQ|序号|区间>`（管理员）
- **审批流**：好友申请/群邀请 → 自动规则（黑名单自动拒绝、`auto_agree/reject` 开关）→ 未自动处理时转发审批消息到**审批群**（`manage_group`）或私发审批员 → 审批员**引用该消息**回复 `/同意` `/拒绝` `/拉黑`
- **抽查**：`/抽查 <群号|@群友|@QQ> <数量>` — 转发最近聊天记录（分批发）
- **通知自动处理**：被设为/撤管理员、被禁言（超时自动退群）、被踢（自动拉黑群/用户）、被拉群（小群/大群/群容量/互斥成员检查自动退群 + 自动抽查新群）
- **其他**：`/推荐 <群号|@qq>` 发送名片、`/加审批员 @某人` `/减审批员 @某人`

> 移植自 [astrbot_plugin_relationship](https://github.com/Zhalslar/astrbot_plugin_relationship) v3.0.5（Zhalslar），去掉 afdian 校验与"加好友/加群"扩展（无对应依赖），OneBot API 走 mohobot 通用 `send_to_bot`。

## 🌸 抽老婆插件（移植自 astrbot-plugin-wifepicker）

`plugins/wifepicker/` — 群聊互动：活跃成员抽"今日老婆"（活跃池筛选 + 640px 头像 + @）、我的老婆、强娶（冷却 + 排除列表）、关系图（vis-network 渲染）、rbq排行、求婚（30 秒"同意/拒绝"交互 + 拒绝后强娶确认）：

- **命令**（带 `/` 前缀 + 英文缩写别名）：`/今日老婆`(jrlp/抽老婆) `/我的老婆`(wdlp) `/强娶 @某人`(qiangqu) `/关系图`(gxt) `/rbq排行`(rbqph) `/求婚 @某人`(qh) `/抽老婆帮助`(clpbz)；管理员：`/重置记录` `/重置强娶时间` `/重置求婚时间`
- **关键词触发**（配置开关，默认关）：直接发 `jrlp`/`抽老婆` 等无需 `/` 前缀（仅群聊，支持 exact/starts_with/contains 匹配模式）
- **求婚交互**：框架新增**消息观察钩子**（`PluginSystem.dispatch_observed`）——所有群消息（含未 @bot 的）在 gate 前先过插件，用于活跃记录、求婚"同意/拒绝"回复、无前缀关键词触发
- **数据**：合并为 `data/plugins_data/wifepicker/data.json`（原 5 文件合一），file_store 原子读写，并发安全
- **关系图/rbq排行**：Playwright 渲染 HTML→PNG（生产需 `pip install playwright && playwright install chromium`），未安装/失败时自动降级为文本列表
- **适配**：管理员=全局 `admins`；数据隔离于各群白/黑名单（`whitelist_groups`/`blacklist_groups`）；`auto_withdraw`（自动撤回）因 mohobot 无删除消息追踪而停用

> 移植自 [astrbot-plugin-wifepicker](https://github.com/astrbot/astrbot-plugin-wifepicker) v3.2.6（作者：Nayukiiii），核心逻辑/模板/关键词路由原样移植，事件模型与数据存储适配 mohobot。

## 🎵 网易云点歌插件（移植自 astrbot_plugin_netease_music）

`plugins/neteasemusic/` — 网易云音乐点歌：`/点歌 <关键词>`（别名 `/music` `/听歌` `/网易云`）搜索并展示编号列表，**群内任意成员回复数字即可选歌（无需 @bot）**，60 秒内有效：

- **播放**：详情文本 + 封面图合并为同一条消息（text + image 段）→ record 语音（NapCat 直接拉取 URL 转码，失败自动降级"🔊 点击播放: 链接"）
- **音质回退**：按配置的优先音质自动降级（lossless → exhigh → higher → standard），VIP/无版权歌曲给出提示
- **多 bot 去重**：点歌命令由群内 bot_id 最小者回复（`global_triggers`）；数字选择状态按 `(bot_id, 会话)` 隔离，只有发起搜索的 bot 消费
- **配置**（WebUI 插件页可改、热生效）：`api_url`（自部署的 NeteaseCloudMusicApi 地址）/ `quality`（优先音质）/ `search_limit`（结果数）/ `cookie`（VIP 解锁）
- 明确**不支持**"来一首xxx"等自然语言模糊匹配

> 移植自 [astrbot_plugin_netease_music](https://github.com/NachoCrazy/netease-music-astrbot-plugin) v2.0.0（作者：NachoCrazy），依赖自部署的 NeteaseCloudMusicApi 服务。

## 🎵 歌曲知识（移植自 Agent-LuoTianyi，beta 板块）

歌曲知识部分**直接移植自 [Agent-LuoTianyi](https://github.com/CarefreeSongs712/Agent-LuoTianyi) 的 server 端实现**（`src/subconscious/music_knowledge/`、`src/subconscious/memory/song_knowledge.py`、`src/world/get_new_songs/`），包含：

1. **SQLite 事实库**（`mohobot/agent/music_knowledge/song_database.py`）— `songs` 表：`name / safe_name(过滤非字母数字的规范化名) / uploader(UP主) / singers(演唱) / introduction / lyrics`
2. **查询服务**（`knowledge_service.py`）— 精确匹配优先（name/safe_name 相等），兜底 `ilike` 模糊；按 UP主/歌手（逗号分隔）/歌词片段查询
3. **FlashText 关键词链接器**（`jargon.py`）— `SongEntityLinker` 加载两份 txt（歌名/歌词关键词）为 Aho-Corasick 风格匹配器；**触发动词门控**：消息必须命中 `{听,唱,点,循环,安利,写,作曲,调教,歌}` 才激活歌名识别（防日常误触发），产出 `《歌名》是一首歌` / `歌词是《歌名》的歌词` 术语写入消息 `terms`
4. **对话使用链路** — 术语进话题提取 prompt → LLM 产出 `fact_constraints`（歌名约束）→ 并行检索 SQLite 返回《歌名》的介绍/歌词去重文本 → 注入回复 prompt 约束输出防编造；`sing_attempts`（点歌）→ 从事实库取歌词文本呈现（**无 TTS/音频，只发歌词文本**）
5. **VCPedia 新歌同步**（`vcpedia.py`）— 手动触发（`/sync-songs` 管理员命令 或 `scripts/sync_vcpedia.py`），无定时器

**默认知识库**随仓库提供（`res/song_knowledge/`：`knowledge_db.db` 3412 首 + 两个关键词 txt，数据来自用户提供的《音乐知识库0726》，非仓库代码）；**首次启动自动复制到 `data/song_knowledge/`**，已有数据不会被覆盖。配置在 `agent.music_knowledge`：

```yaml
agent:
  music_knowledge:
    enabled: true
    song_database:
      db_folder: "./data/song_knowledge"
      db_file: "knowledge_db.db"
    songname_file: "./data/song_knowledge/song_name_keywords.txt"
    lyric_file: "./data/song_knowledge/song_lyric_keywords.txt"
    crawler:
      base_url: "https://vcpedia.cn"
```

> 依赖：`flashtext`（已从 PyPI 下架，需 `pip install --no-build-isolation git+https://github.com/vi3k6i5/flashtext.git`）、`requests`、`beautifulsoup4`（VCPedia 同步用）。

## 🚫 封禁系统（参考 astrbot_plugin_reneban 移植）

多 bot 聚合场景的全局统一封禁名单（所有 bot 共享），**语义：bot 静默忽略被禁用户的消息**（不是 QQ 群管理封禁）。存储于 `data/ban/`（4 个 JSON），不依赖外部服务。

```yaml
ban:
  enabled: true
  admins: []                  # 管理员 QQ 号(可执行封禁命令)
```

- **命令**（管理员可执行；`/banlist` `/ban-help` 所有人可用）：
  - `/ban <@|QQ> [时间] [理由]` — 封禁于当前会话（群=本群，私聊=对方私聊）
  - `/ban-all <@|QQ> [时间] [理由]` — 全局封禁
  - `/pass <@|QQ> [时间] [理由]` / `/pass-all` — 临时解禁（会话级/全局）
  - `/dec-ban <@|QQ> [时间]` / `/dec-ban-all` / `/dec-pass` / `/dec-pass-all` — 删除/削减记录
  - `/ban-reset <@|QQ>` — 清除用户全部记录；`/banlist` 查看名单
  - `/ban-enable` / `/ban-disable` — 临时启停；`/ban-help` 帮助
- **优先级**：会话解禁 > 会话封禁 > 全局解禁 > 全局封禁；过期自动清理
- **时间格式**：`1d` `2h` `30m` `10s` 可组合（如 `1d2h`），不带时间 = 永久
- Web 面板「封禁管理」板块可视化查看/增删记录；封禁数据纳入备份/恢复/清理范围

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
│   ├── db/                        # 数据库层（独立 SQLite）
│   │   ├── sql_database.py        # SQLAlchemy 模型 + 迁移
│   │   └── database_manager.py    # 用户/会话记录/记忆正本读写
│   ├── ws_server.py               # 反向 WebSocket 服务器
│   ├── bot_manager.py             # Bot 生命周期与连接管理
│   ├── message_handler.py         # 消息处理管线（拦截器 → Agent 流水线）
│   ├── context_manager.py         # 会话上下文管理（JSON，不变）
│   ├── llm_service.py             # LLM 服务（旧路径：流式、工具调用、视觉）
│   ├── file_store.py              # 异步文件存储（锁保护）
│   ├── image_cache.py             # 图片缓存与 phash 去重
│   ├── ban/                       # 封禁系统（移植自 reneban，去 server/云同步）
│   │   ├── models.py              # 封禁记录数据模型
│   │   ├── time_utils.py          # 时间解析（1d/2h/30m/10s）
│   │   ├── store.py               # 名单存储（全局统一 + 缓存 + 过期清理）
│   │   └── ban_filter.py          # 拦截器（静默过滤 + 管理命令）
│   ├── interceptors/              # 拦截器（封禁、指令、关键词、插件系统）
│   ├── models/                    # OneBot 协议与配置模型
│   ├── utils/                     # 日志、CQ 码解析等工具
│   └── web_panel/                 # FastAPI 管理面板（8 板块，含封禁管理）
├── plugins/                       # 插件目录（动态加载：status / praise / divination /
│                                  #   neteasemusic / wifepicker / relationship / song_sync）
├── tests/                         # 冒烟测试（smoke_*）与单测（tests/_run_all.py 全量回归）
├── data/                          # 运行时数据（自动生成，勿提交）
│   ├── bots/{bot_id}/             # Bot 配置与状态
│   ├── history/{bot_id}/          # 【只读】原始聊天记录 JSONL
│   ├── contexts/{bot_id}/         # 【可读写】会话上下文
│   ├── database/                  # SQLite（mohobot.db）
│   └── cache/images/              # 图片缓存
└── logs/                          # 日志（自动生成，勿提交）
```

## 🧠 数据架构

**原则：原始数据不可变，工作数据可变；历史入库，上下文不变。**

| 数据 | 位置 | 性质 | 格式 | 用途 |
|------|------|------|------|------|
| 聊天历史 | `data/history/` | 只读归档 | JSONL（每行一个事件） | 审计、全量回溯、训练导出 |
| 会话上下文 | `data/contexts/` | 可读写工作区 | JSON（数组） | LLM 实时推理的记忆（**保持原有管理方式不变**） |
| 对话记录 | SQLite `conversations` 表 | 可读写 | SQL | 历史入库（独立 `mohobot.db`） |
| 长期记忆 | SQLite `agent_memory_records`/`memory_chunks` | 可读写 | SQL | 记忆正本（向量库仅作索引，可降级） |
| 向量索引 | `data/database/chroma/`（可选） | 可读写 | ChromaDB | 记忆语义检索（未配置 embedding 时自动降级为空实现） |

- **聊天历史**：按 Bot ID → 私聊/群聊 → 用户/群号 分文件，**绝不**用于 LLM 实时输入
- **会话上下文**：私聊一个用户可有多个会话（`sess_001`…由 `session_index.json` 索引），群聊固定 `main.json`；满 40 轮触发 AI 总结压缩（最早的 15 轮 → 总结块插最前，详见上文配置）
- **数据隔离**：记忆按 bot（`owner_character_id`）隔离，会话数据按 bot_id 分目录

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
| `/help` | 显示全部可用指令（PIL 渲染成图片，按插件分组、标注管理员权限；失败降级文本） |
| `/status` | 显示框架与系统状态（插件，图片） |
| `/点歌 <关键词>` | 网易云点歌（别名 `/music` `/听歌` `/网易云`；回复数字选歌） |
| `ping` | 全局功能：发送 ping（忽略大小写、无需斜杠、群聊不 @ 也回复）→ 回复 PONG |
| `赞我` / `zanwo` | 给自己点赞（插件，数量/回复模板可在插件配置中调整，每日上限缓存） |

## 🖥️ Web 管理面板

启动后访问 `http://127.0.0.1:9090`（默认用户名 `admin`，密码在 `config/global.yaml` 的 `web_panel.password_hash` 中配置）：

1. 📊 **数据总览** — 系统/框架/Bot/LLM token 统计
2. ⚙️ **配置文件** — 全局 + 每 Bot 配置可视化编辑（上下文压缩、群聊最近消息、Agent 模型下拉等；数据库/日志/数据/插件目录属服务端路径，不在 WebUI 修改）
3. 🧠 **模型配置** — Chat / Vision 模型、端点与密钥；**可用模型列表**（每行一个，beta 模块下拉的选项来源）；**备用模型**（连接失败自动回退，可"不使用"）
4. 🔌 **插件管理** — 启停/热重载插件；带 `_conf_schema.json` 的插件可"⚙️ 配置"（表单驱动，保存热生效）
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
- [x] 历史对话入库（独立 SQLite `mohobot.db`）
- [x] 长期记忆：向量检索（ChromaDB，未配置时优雅降级）+ 数据库正本 + 用户画像
- [x] LLM 流式对话（旧路径：分段回复、工具调用、视觉识别）
- [x] VLM 图片理解（agent 路径自动描述图片；base64:// 兼容）
- [x] 会话上下文管理（多会话、说话人记录，context 机制不变）
- [x] 上下文 AI 总结压缩（满 40 轮裁剪 15 轮，总结块插入最前，失败降级直接裁剪）
- [x] 群聊最近消息注入（可配置条数，不写上下文、不参与总结）
- [x] 群聊触发门控（@机器人 / 引用机器人消息）+ 戳一戳反射回复 + ping/PONG
- [x] 全局指令去重（群内多 bot 只由最小 bot_id 回复，支持命令+空格参数）
- [x] LLM 备用模型回退（连接类失败自动换用全局备用模型重试）
- [x] Web 管理面板（7 板块：总览/配置/模型/插件/对话/日志/设置；模型页含可用模型列表与备用模型）
- [x] 插件系统（status / praise / divination / neteasemusic / wifepicker / relationship / song_sync）
- [x] /help 图片渲染（PIL 深色卡片，按插件分组 + 管理员标注）
- [ ] 消息发送限流与队列（当前仅图片突发限流）
- [ ] 单元测试与 CI（当前为本地冒烟测试 `tests/`）
- [ ] Docker 部署
- [ ] Agent 子系统由 beta 转正前：真实多 bot 长期运行验证

## 📄 License

[MIT](LICENSE)

## 🙏 致谢

- [OneBot 标准](https://github.com/botuniverse/onebot-11) — 聊天机器人应用接口标准
- [Agent-LuoTianyi](https://github.com/CarefreeSongs712/Agent-LuoTianyi) — Agent 子系统架构参考（话题规划/潜意识/反思记忆）
