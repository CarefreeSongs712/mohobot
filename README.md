# Mohobot — 多机器人 AI 框架

> ⚠️ **开发阶段**: 本项目目前处于积极开发中，API、数据格式与功能均可能发生破坏性变更，请谨慎用于生产环境。

基于 Python 异步框架 + [OneBot v11](https://github.com/botuniverse/onebot-11) 标准的**多 Bot AI 框架**。支持同时接入多个 QQ 机器人，通过 LLM 驱动对话。

## ✨ 功能特性

- **多 Bot 接入** — 反向 WebSocket (Reverse WebSocket) 服务端，一个进程同时服务多个机器人
- **LLM 驱动对话** — OpenAI 兼容 API，支持流式回复（标点+长度分段发送）、函数调用（Tools）、视觉识别（Vision）
- **会话上下文管理** — 私聊支持多会话切换，群聊单一会话；记录每条消息的说话人（QQ号-昵称）
- **上下文 AI 总结压缩** — 上下文满 40 轮时，用 AI 总结最早的 15 轮并作为"总结块"插入对话最前（不参与后续总结的只有它自己，可嵌套再总结）；总结失败自动降级为直接裁剪
- **群聊最近消息** — 回复时临时注入群内最近 10 条消息（仅内存、不写入上下文、不参与总结），感知群聊氛围
- **历史对话入库** — 聊天记录写入独立 SQLite `conversations` 表（`mohobot.db`），原始事件另以 JSONL 只读归档
- **智能群聊触发** — 群聊中仅在 @机器人、引用机器人消息、命令或 `ping` 时触发 LLM 回复；`ping`（忽略大小写，无需斜杠）直接回复 `PONG`
- **全局指令去重** — 群内多个 bot 时，全局指令（`/占卜` `/help` `/status` `/点歌` 等，含"命令+空格参数"形式）只由随机选中的一个 bot 回复
- **图片缓存与去重** — phash 感知哈希去重 + LRU 缓存（300MB 上限），图片消息只解析首张
- **插件系统** — 从 `plugins/` 目录动态加载插件，可拦截消息、响应事件；插件配置由 `_conf_schema.json` 驱动，WebUI 可视化编辑热生效
- **Web 管理面板** — FastAPI + SSE 实时日志流、文件系统浏览器、配置在线编辑、统计看板
- **可配置拦截器** — 指令拦截（`/` 开头）、关键词拦截（预设回复）
- **TTS 语音（GPT-SoVITS）** — LLM 回复自动朗读（模型自标 `<tts>` 句）+ `/tts` 指令直读，全局单飞行队列、队列满丢最新，合成失败降级纯文本

## 🏗️ 技术栈

| 分类 | 技术 |
|------|------|
| 语言 | Python 3.10+ |
| 异步 | asyncio |
| WebSocket | websockets |
| Web 面板 | FastAPI + SSE |
| 存储 | SQLite（历史）+ JSON/JSONL（会话上下文与原始归档） |
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
export MOHOBOT_VISION_API_KEY="sk-xxx"   # Vision 模型（无视觉需求可留空）

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

**Bot 与 QQ 分离**：bot 使用自动编号的内部标识（`bot_001`…），一个 bot 绑定一个 QQ 号（QQ 唯一绑定）。新 QQ 连接默认**不分配** bot，需在 Web 面板"配置文件 → 创建 Bot / 绑定 QQ"操作；启动时会自动迁移旧版（`data/bots/{qq}`）配置。

框架启动后，`data/bots/{bot_id}/config.json` 会自动创建，可编辑：

```json
{
  "bot_id": "bot_001",
  "qq": 123456789,
  "nickname": "我的机器人",
  "persona": "你是 Mohobot，一个有用的 AI 助手。",
  "enabled": true
}
```

> `qq` 为 0 表示未绑定；`persona` 作为该 bot 的 System Prompt。

### 3. 验证

私聊机器人发送任意消息即可得到回复（流式回复按标点+长度分段发送、首段引用触发消息）。

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

### 3. 初始化 Web 管理密码

WebUI 不再提供 `admin/admin` 默认密码。首次部署必须通过环境变量初始化，服务会将 PBKDF2-SHA256 哈希写入 `config/global.yaml`，不会保存明文：

```bash
export MOHOBOT_WEB_PASSWORD='替换为高强度密码'
python main.py
# 首次成功启动后，从 shell 历史和服务环境中移除明文变量。
```

使用 systemd 时，将变量放入仅 root 可读的 `/etc/mohobot/mohobot.env`：

```bash
sudo install -d -m 700 /etc/mohobot
sudo sh -c 'printf "%s\n" "MOHOBOT_WEB_PASSWORD=替换为高强度密码" > /etc/mohobot/mohobot.env'
sudo chmod 600 /etc/mohobot/mohobot.env
```

LLM、Vision、Anysearch 和 embedding 密钥同样应保存在未纳入 Git 的 `config/global.yaml` 或服务 Secret 中。WebUI 只返回密钥是否已设置，不回传原始值。

### 4. 使用 systemd 运行（推荐）

仓库提供 [`deploy/mohobot.service`](deploy/mohobot.service)。先创建专用用户和虚拟环境，再安装服务：

```bash
sudo useradd --system --home /opt/mohobot --shell /usr/sbin/nologin mohobot || true
sudo python3 -m venv /opt/mohobot/.venv
sudo /opt/mohobot/.venv/bin/pip install -r /opt/mohobot/requirements.txt
sudo chown -R mohobot:mohobot /opt/mohobot
sudo install -m 644 deploy/mohobot.service /etc/systemd/system/mohobot.service
sudo systemctl daemon-reload
sudo systemctl enable --now mohobot
sudo systemctl status mohobot
journalctl -u mohobot -f
```

服务通过 SIGTERM 优雅停止：停止接收新事件，关闭插件周期任务和网络会话，等待发送队列，再关闭 LLM 和文件写入器。

### 5. 运行（screen，开发/临时部署）

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

### 上下文压缩与群聊最近消息

```yaml
context_summary_enabled: true                 # 启用 AI 总结压缩
context_trim_at_rounds: 40                    # 满多少轮触发一次压缩
context_trim_remove_rounds: 15                # 每次把最早的多少轮交给 AI 总结
context_summary_age_hours: 3                  # 旧对话判定年龄(小时)
context_summary_sweep_enabled: true           # 启用周期时间压缩
context_summary_sweep_interval_minutes: 30    # 周期扫描间隔(分钟)
context_summary_min_interval_hours: 24        # 已压缩会话再次周期压缩的最小间隔
group_recent_msgs_count: 10                   # 群聊最近消息条数(回复时临时注入, 0=关闭)
```

- **AI 总结压缩**：上下文满 `context_trim_at_rounds` 轮时，把最早的 `context_trim_remove_rounds` 轮交给 LLM 总结（复用全局 chat 模型，prompt 要求"全局概要 + 重点轮次浓缩"），总结作为 `role="summary"` 的块插入对话最前；总结块视为 1 轮参与后续再总结；总结失败（API 不可用）自动降级为直接裁剪
- **时间压缩**：满轮压缩时会**顺带**把超过 `context_summary_age_hours`（默认 3h）的旧对话一并收走（两个前缀取更长者）；另有后台周期任务按 `context_summary_sweep_interval_minutes`（默认 30 分钟）扫描群聊 main 与私聊当前活动会话，把超龄旧对话交给 AI 总结；已压缩过的会话距上次压缩不足 `context_summary_min_interval_hours`（默认 24h）且未超触发轮数时不重复压缩；周期压缩总结失败会保留数据待下次重试（不裁剪）
- **群聊最近消息**：MessageHandler 内存缓冲每群最近 N 条消息（含未 @bot 的，单条截断 80 字），生成回复时临时注入 prompt，**不写入上下文文件、不参与总结压缩**

### 数据库配置

```yaml
database:
  enabled: true
  folder: "./data/database"
  file: "mohobot.db"           # 独立数据库文件
```

## 🧩 插件系统与插件配置

**插件形态**：单文件插件（`plugins/xxx.py`）或目录插件（`plugins/xxx/main.py` + 可选 `core/` 子模块），热加载/热重载/启停无需重启。

**插件配置系统**：插件目录放 `_conf_schema.json` 声明配置项（类型：`string/int/bool/list/object`+items，含 `description/hint/default/slider/invisible`），配置存于 `data/plugins_config/{name}.json`（全局一份），Web 面板"插件管理"页自动渲染表单编辑、保存即热生效（调用插件 `on_config_update` 回调）。

**事件钩子**：`on_message` / `on_notice` / `on_meta` / `on_request`（好友申请、群邀请，插件接管后框架不再自动同意）。

**注入**：`inject_ws_server` / `inject_bot_manager` / `inject_data_dir` / `inject_anysearch_client` / `inject_admin_ids`（全局管理员，与封禁系统共用配置顶层 `admins`）。

## 👥 关系管理器插件（移植自 astrbot_plugin_relationship）

`plugins/relationship/` — 帮助管理 QQ 好友和群聊（命令带 `/` 前缀，管理员=全局 `admins`，审批员=管理员+配置的额外审批员）：

- **查询/管理**：`/群列表` `/好友列表` `/退群 <序号|群号|区间>` `/删好友 <@|QQ|序号|区间>`（管理员）
- **审批流**：好友申请/群邀请 → 自动规则（黑名单自动拒绝、`auto_agree/reject` 开关）→ 未自动处理时转发审批消息到**审批群**（`manage_group`）或私发审批员 → 审批员**引用该消息**回复 `/同意` `/拒绝` `/拉黑`
- **抽查**：`/抽查 <群号|@群友|@QQ> <数量>` — 转发最近聊天记录（分批发；消息取自本地 `data/history` 归档，不再调用历史查询 API，无归档时给出提示）
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
- **多 bot 去重**：点歌命令由群内随机选中的一个 bot 回复（`global_triggers`）；数字选择状态按 `(bot_id, 会话)` 隔离，只有发起搜索的 bot 消费
- **配置**（WebUI 插件页可改、热生效）：`api_url`（自部署的 NeteaseCloudMusicApi 地址）/ `quality`（优先音质）/ `search_limit`（结果数）/ `cookie`（VIP 解锁）
- 明确**不支持**"来一首xxx"等自然语言模糊匹配

> 移植自 [astrbot_plugin_netease_music](https://github.com/NachoCrazy/netease-music-astrbot-plugin) v2.0.0（作者：NachoCrazy），依赖自部署的 NeteaseCloudMusicApi 服务。

## 🎵 歌曲知识（全局歌曲识别 + LLM 前注入）

歌曲知识为全局能力（私聊+群聊）：用户消息里含歌曲信息（歌名或歌词）时，在发送给 LLM 之前识别出是哪首歌，把「歌曲介绍 + 词/曲/混/调等创作人员 + 完整歌词」作为一段注脚，**紧跟该条用户消息下方**拼进 LLM 请求（仅请求级注入，不写入 context 文件）；不再输出 `[sing]` 唱歌文本。

实现位于 `mohobot/music_knowledge/`：

1. **SQLite 事实库**（`song_database.py`，新 schema）— `songs` 表字段：`name / safe_name / uploader(UP主) / singers(演唱) / lyricist(作词) / composer(作曲) / arranger(编曲) / mixer(混音) / tuner(调教) / mastering(母带) / pv / illustrator(曲绘) / year(年份) / introduction(介绍) / lyrics(完整歌词，保留换行)`；启动时按新 schema 建空库（不再复制内置库）
2. **匹配器**（`matcher.py`）— `SongInfoMatcher` 直接和爬取库比对（不再依赖 FlashText/关键词 txt）：`《歌名》` 书名号高置信命中 → 裸文本含歌名（长度≥3）且含歌曲语境词（唱/听/歌/歌词等）→ 歌词片段（采样 3 个 12–20 字子串做包含检测）。命中即取详情节并格式化为 `【歌曲信息】` 注解段（介绍 + 演唱/UP主 + 词/曲/编/混/调 + 完整歌词）
3. **VCPedia 新歌同步**（`vcpedia.py`，重写）— 站点现为 **Anubis PoW 反爬**（明文请求 403），同步器内置 PoW 解题 + auth cookie 持久化复用；列表走 `api.php list=categorymembers` 全量分页；词条优先 `rest.php` wikitext（兜底渲染 HTML）；解析完整创作人员与完整歌词。手动触发：`/sync-songs`（管理员）或 `python scripts/sync_vcpedia.py`
4. **旧数据迁移** — `python scripts/migrate_legacy_songs.py` 把旧版 3412 首（name/uploader/singers/introduction/lyrics）迁移进新 schema（credits 留空）

**网易云点歌插件（`/点歌`）不受影响**：仍走独立命令路径，不进入本歌曲知识链路。

配置在顶层 `music_knowledge`：

```yaml
music_knowledge:
  enabled: true
  song_database:
    db_folder: "./data/song_knowledge"
    db_file: "knowledge_db.db"
  crawler:
    base_url: "https://vcpedia.cn"
    category: "Category:洛天依歌曲"
```

## 🔊 TTS 语音（GPT-SoVITS 接入）

基于本地/局域网 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) `api_v2` 服务（`python api_v2.py -a 127.0.0.1 -p 9880`）把文字转成语音发送。两条通路：

- **LLM 自动朗读**：系统提示词引导模型用 `<tts></tts>` 标注一句适合朗读的话（可省略，尽量 ≤20 字；超长时截到第一个句末标点，多标注取第一个，忘写闭标签自动容错）。框架剥掉标签后文本照常分段发送，标注内容**仍显示**；全文发送完毕后取出标注句经全局单飞行队列合成，语音跟在最后一个文本段之后由回复的 bot 单独发出。没标注/合成失败/队列满 → 当轮无语音，文本不受影响。
- **`/tts <文本>` 指令**（群聊多 bot 由 bot_id 最小者响应）：文本直接转语音；非管理员限 30 字 + 120 秒冷却（全局配置可改），管理员不限。

**并发**：GSV 一次只能合成一条 → 框架侧全局 FIFO 队列串行消费；队列满（上限可配，默认 16）**丢弃最新**请求。模型权重不运行时切换，GSV 服务端启动时通过 `tts_infer.yaml` 自行加载。

**WebUI「🔊 TTS 语音」独立板块**（与模型配置同级）：

- **GSV 服务控制**：手动启动/停止/重启 GSV 后台进程（`tts.service_command + service_cwd` 拉起 detached 进程，日志重定向到 `service_log_path`）。**GSV 进程独立于 mohobot 生命周期**——mohobot 启动不拉起它、关闭也不停它。停止流程：`/control exit` 优雅退出 → 等待 `stop_wait_seconds`（默认 10s）→ 仍在监听则 kill 监听该端口的进程兜底（按端口找 pid，不按命令名 pgrep，防误杀）。
- **合成队列监控**：运行状态（TCP 探测 base_url 端口）、当前合成中的任务、队列深度、累计成功/失败/丢弃计数（页面打开时 5 秒自动刷新）。
- **发送配置**：`/tts` 请求参数全部可调（语速/切分方式/句间停顿/top_k/top_p/temperature/超时等），保存后原位热同步立即生效（仅队列上限需重启）。
- **GSV 模型配置**：表单编辑 `tts_infer.yaml` 的 `custom:` 段（device/is_half/version/GPT 权重/SoVITS 权重/BERT 路径），保存自动 `.bak` 时间戳备份，其余段保留不动；重启 GSV 后生效。可查看 GSV 日志尾部。

**配置**：GSV 相关全部在全局 `tts:` 段（WebUI 独立板块可编辑）；每 bot 仅 `tts_enabled` 开关（WebUI Bot 配置页），修改开关需重启生效。

```yaml
tts:
  enabled: true
  base_url: "http://127.0.0.1:9880"
  media_type: "wav"          # wav/ogg/aac(ogg/aac 需 GSV 端 ffmpeg)
  text_lang: "zh"
  prompt_lang: "zh"
  ref_audio_path: "D:/GSV/refs/voice.wav"   # GSV 服务器本机路径
  prompt_text: "参考音频里说的那句话"
  speed_factor: 1.0
  text_split_method: "cut5"  # cut0 不切/cut1 每4句/cut2 凑50字/cut3 句号/cut4 句点/cut5 按标点
  fragment_interval: 0.3     # 句间停顿(秒)
  top_k: 15
  top_p: 1.0
  temperature: 1.0
  queue_maxsize: 16
  timeout: 300               # CPU 推理一句约 40s, 300 起步
  cmd_max_chars: 30
  cmd_cooldown: 120
  # GSV 后台进程管理(面板手动启停)
  service_command: '/root/QQBot/GSV/GPT-SoVITS/.venv/bin/python api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer_cpu.yaml'
  service_cwd: "/root/QQBot/GSV/GPT-SoVITS"
  service_log_path: "/root/QQBot/GSV/api_v2.log"
  gsv_config_path: "/root/QQBot/GSV/GPT-SoVITS/GPT_SoVITS/configs/tts_infer_cpu.yaml"
  stop_wait_seconds: 10
```

实现在 `mohobot/services/gsv_tts.py`（客户端+队列+进程管理）、`mohobot/utils/tts_marker.py`（流式 `<tts>` 标记剥离）。另有独立脚本 `scripts/tts_standalone.py`（不依赖 mohobot，仅 httpx，可直接验证 GSV 服务连通性）。

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
│   ├── db/                        # 数据库层（独立 SQLite）
│   │   ├── sql_database.py        # SQLAlchemy 模型 + 迁移
│   │   └── database_manager.py    # 用户/会话记录读写
│   ├── ws_server.py               # 反向 WebSocket 服务器
│   ├── bot_manager.py             # Bot 生命周期与连接管理
│   ├── message_handler.py         # 消息处理管线（拦截器 → LLM 流式回复）
│   ├── context_manager.py         # 会话上下文管理（JSON，不变）
│   ├── llm_service.py             # LLM 服务（流式、工具调用、视觉）
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

- **聊天历史**：按 Bot ID → 私聊/群聊 → 用户/群号 分文件，**绝不**用于 LLM 实时输入
- **会话上下文**：私聊一个用户可有多个会话（`sess_001`…由 `session_index.json` 索引），群聊固定 `main.json`；满 40 轮触发 AI 总结压缩（最早的 15 轮 → 总结块插最前，详见上文配置）
- **数据隔离**：会话数据按 bot_id 分目录

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

启动后访问 `http://127.0.0.1:9090`。管理面板不提供默认密码：必须先在 `web_panel.password_hash` 配置 PBKDF2 哈希，或使用 `MOHOBOT_WEB_PASSWORD` 完成首次初始化。面板默认只允许本机来源，并且不会回传已保存的 API Key：留空保存即保留原密钥。

> 不要将 WebUI 直接暴露到公网。若确有远程管理需求，应使用 HTTPS 反向代理、VPN 或 IP 白名单，并将可信 Origin 显式加入部署配置后再开放访问。

1. 📊 **数据总览** — 系统/框架/Bot/LLM token 统计
2. ⚙️ **配置文件** — 全局 + 每 Bot 配置可视化编辑（上下文压缩、群聊最近消息、戳一戳回复等；数据库/日志/数据/插件目录属服务端路径，不在 WebUI 修改）
3. 🧠 **模型配置** — Chat / Vision 模型、端点与密钥；**可用模型列表**（每行一个，可增删）
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

- [x] 反向 WebSocket 多 Bot 接入（重连竞态防护）
- [x] 历史对话入库（独立 SQLite `mohobot.db`）
- [x] LLM 流式对话（分段回复、工具调用、视觉识别）
- [x] VLM 图片理解（base64:// 兼容）
- [x] 会话上下文管理（多会话、说话人记录，context 机制不变）
- [x] 上下文 AI 总结压缩（满 40 轮裁剪 15 轮，总结块插入最前，失败降级直接裁剪）
- [x] 群聊最近消息注入（可配置条数，不写上下文、不参与总结）
- [x] 群聊触发门控（@机器人 / 引用机器人消息）+ 戳一戳固定回复 + ping/PONG
- [x] 全局指令去重（群内多 bot 只由最小 bot_id 回复，支持命令+空格参数）
- [x] Web 管理面板（7 板块：总览/配置/模型/插件/对话/日志/设置；模型页含可用模型列表）
- [x] 插件系统（status / praise / divination / neteasemusic / wifepicker / relationship / song_sync）
- [x] /help 图片渲染（PIL 深色卡片，按插件分组 + 管理员标注）
- [ ] 消息发送限流与队列（当前仅图片突发限流）
- [ ] 单元测试与 CI（当前为本地冒烟测试 `tests/`）
- [ ] Docker 部署

## 📄 License

[MIT](LICENSE)

## 🙏 致谢

- [OneBot 标准](https://github.com/botuniverse/onebot-11) — 聊天机器人应用接口标准
