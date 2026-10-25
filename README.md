# Mohobot — 多机器人 AI 框架

> ⚠️ **开发阶段**: 本项目目前处于积极开发中，API、数据格式与功能均可能发生破坏性变更，请谨慎用于生产环境。

基于 Python 异步框架 + [OneBot v11](https://github.com/botuniverse/onebot-11) 标准的**多 Bot AI 框架**。支持同时接入多个 QQ 机器人，通过 LLM 驱动对话。

## 📚 项目文档与接手

- [接手与开发守则](AGENTS.md)：数据边界、消息主链、扩展契约、验证和发布授权。
- [开发指南](docs/DEVELOPMENT.md)：插件、配置、LLM 工具、数据库、RAG 知识库和面板修改清单。
- [项目待办清单](TODO.md)：全量已合入状态、当前基准与 9 大长期演进任务清单（REQ-1至REQ-9）。
- [项目接手说明](docs/HANDOVER.md)：历史代码索引、文档校核与验证记录快照。
- [缺陷审查与修复方案](docs/AUDIT-2026-10-12.md)：历史缺陷与验收用例归档。

接手说明和审计报告是带代码基点的历史快照，不代表生产实时状态或发布授权；当前状态以代码、开放 PR、issue 和 CI 为准。文档与实现冲突应核查后报告，现有生产操作仍遵循下方发布流程。

## ✨ 功能特性

- **多 Bot 接入** — 反向 WebSocket (Reverse WebSocket) 服务端，一个进程同时服务多个机器人
- **LLM 驱动对话** — OpenAI 兼容 API，支持流式回复（标点+长度分段发送）、函数调用（Tools）、视觉识别（Vision，支持 `describe`/`direct` 两种识图模式，见下）、空白/纯符号消息语义标注与历史防 400 过滤
- **智能群聊触发与定向** — 群聊中仅在 @机器人、引用机器人消息、命令或 `ping` 时触发 LLM 回复；群聊 `@` 点名定向机制（PR #44），被明确 `@` 的 Bot 独占执行，未被 `@` 的 Bot 静默跳过且不产生多 Bot 合并刷屏
- **虚拟歌手知识库 (RAG)** — 基于 `mohobot/knowledge/`，支持 Markdown/文本切片分块、SQLite 向量存储、Embedding 语义检索与可选 Rerank 重排序，支持 LLM function call 主动召回与 WebUI 管理
- **会话上下文管理** — 私聊支持多会话切换，群聊单一会话；记录每条消息的说话人（QQ号-昵称）
- **上下文 AI 总结压缩** — 上下文满 40 轮时，用 AI 总结最早的 15 轮并作为“总结块”插入对话最前（总结块记 1 轮，可与后续旧对话一同再总结；周期压缩不单独反复总结孤立块）；满轮总结失败自动降级为直接裁剪
- **群聊最近消息** — 回复时临时注入群内最近 10 条消息（仅内存、不写入上下文、不参与总结），感知群聊氛围
- **历史对话入库** — 聊天记录写入独立 SQLite `conversations` 表（`mohobot.db`），原始事件另以 JSONL 只读归档（群聊合并归档支持基于指纹跨账号去重）
- **智能群聊触发** — 群聊中仅在 @机器人、引用机器人消息、命令或 `ping` 时触发 LLM 回复；`ping`（忽略大小写，无需斜杠）直接回复 `PONG`
- **全局指令去重** — 群内多个 bot 时，全局指令（`/占卜` `/help` `/status` `/点歌` 等，含"命令+空格参数"形式）只由随机选中的一个 bot 回复
- **图片缓存与去重** — phash 感知哈希去重 + LRU 缓存（300MB 上限），同一条消息**只处理首张图片**
- **识图两种模式（`llm.vision_mode`）** — `describe`（默认）：先用独立视觉模型（`vision_model`）把图片转述成文本，再交给主模型，适合主模型无视觉能力；`direct`：把图片以多模态 `image_url` 与文本一起直发主模型，省一次视觉调用，**要求 `chat_model` 本身支持视觉**（请求失败会自动回退 `describe` 重试一次）
- **插件系统** — 从 `plugins/` 目录动态加载插件，可拦截消息、响应事件；插件配置由 `_conf_schema.json` 驱动，WebUI 可视化编辑热生效
- **Web 管理面板** — FastAPI + SSE 实时日志流、文件系统浏览器、配置在线编辑、统计看板
- **可配置拦截器** — 指令拦截（`/` 开头）、关键词拦截（预设回复）
- **私聊自动回复过滤** — QQ 自动回复（`[自动回复]` 前缀，或同一用户连续 3 条相同文本且间隔 < 5 分钟）静默忽略：不回复、不记上下文，归档保留（全局开关，默认开）
- **TTS 语音（可切换后端）** — 自建 HTTP 合成（默认）/ MiniMax 云端（仅下线）；LLM 回复自动朗读（模型自标 `<tts>` 句，模糊识别容错）+ `/tts` 指令直读，单飞行队列、队列满丢最新，失败降级纯文本，字符计费入用量统计

## 私聊匿名人设评估

后台「人设评估」从共享人设库选择**全局基准/测试**两版；默认关闭，仅支持
`bot_001`、`bot_009`、`bot_002` 的私聊。默认抽样概率 0.05、冷却 900 秒、
待投票 86400 秒，均可热配置；每 Bot/用户最多一个生成中、发送中或待投票评估。

命中后先提示，再使用同一模型参数与同一准备输入生成两版，只有人设不同，
禁用工具和 TTS。匿名 A 左/B 右随机映射，以自动换行、分页的双栏图卡展示。
仅完整图片组确认发送成功才计入展示；超限或失败降级为对应候选完整纯文字。
正常聊天上下文仅保留当前人设对应候选（不在两版中时保留基准），投票不会改历史。

- `/ab A`、`/ab B`、`/ab 平局`、`/ab 都不好`、`/ab 跳过`：给唯一待投票记录投票。
- `/ab vote <id> A`：指定编号补投，也支持其他四种选项；重复提交不改票。
- `/ab switch <id> A|B`：明确切换本人原会话并保留历史；原会话必须当前活动，
  且 generation、人设正文 hash 均未变化。投票本身**不会切换**人设。
- 可继续聊天再补投，待投票期间不再抽样；群聊、跨用户/Bot、过期操作拒绝。
  `/persona` 仍仅全局管理员可使用。

统计反映的是**单轮同历史偏好，不是长期实验**。面板支持 Bot/正文版本 hash
过滤、分页详情及独立 JSON 导出。评估数据保存在 `data_dir/persona_ab/`，
与 SQL 开关无关；**聊天备份不包含评估数据，需单独导出，首版不支持恢复导入**。
服务重启保留 pending/冷却，中断生成标为 aborted，中断发送标为 delivery_unknown，
不自动重发。修改配置或人设不会修改旧记录快照。

离线 UI 演示（临时数据，无机器人/LLM/真实配置）运行
`python tests/mock_persona_ab_panel.py --port 19091`，打开 `http://127.0.0.1:19091`，
账号 `admin`，密码 `mock-ab-only`。该演示密码仅用于本机模拟，不能用于生产。

## 🏗️ 技术栈

| 分类 | 技术 |
|------|------|
| 语言 | **Python 3.12+**（CI 验证 3.12；不再支持 3.10/3.11） |
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
  "persona_id": "persona_001",
  "enabled": true
}
```

> `qq` 为 0 表示未绑定；`persona_id` 引用「人设管理」页面创建的预设，作为该 bot 的默认 System Prompt。

### 人设预设与私聊专属人设

管理面板提供独立「人设管理」页面。填写名称与正文即可创建预设，ID 由系统自动生成；Bot 配置通过下拉选择名称，预览对应正文。编辑预设会影响全部引用者的下一轮回复，正在生成的回复保留原人设。使用中的人设禁止删除，页面可查看引用位置；系统默认预设始终保留。

Bot 下拉从内存快照加载，不扫描会话历史。管理页引用数量使用 30 秒缓存，配置或绑定修改会立即使缓存失效；删除和恢复仍强制检查磁盘真实引用，不能依赖显示的缓存数量。首次统计在后台线程批量只读扫描索引，不迁移或改写会话数据；页面显示加载、失败及重试状态。

私聊可以给一个明确的 `bot_id＋用户QQ＋session_id` 设置专属人设。仅全局管理员可执行以下命令，可从任意群或私聊指定目标：

```text
/persona list
/persona sessions bot_001 123456789
/persona set bot_001 123456789 sess_main persona_002
/persona get bot_001 123456789 sess_main
/persona clear bot_001 123456789 sess_main
```

目标 bot、私聊会话及人设必须已经存在；命令不创建会话，也不切换活动会话。`clear` 只取消专属覆盖。实际优先级为 **私聊 session 专属人设 → bot 默认人设 → 系统默认人设**。群聊及 QQ空间发说说、自动回复使用 bot 默认人设；总结、情感分析、识图等保留任务提示词。

切换人设不清空历史、总结或情感关系。新建私聊 session 默认继承 bot 人设；删除 session 同时删除其专属引用，清空聊天内容则保留引用。生成期间切换或删除会话，不会把旧回复写进新活动会话或复用同名的新会话。

已有 bot 的内联 `persona` 首次启动会迁入 `data/personas/personas.json`，同文复用预设；修改 bot 配置前保存时间戳备份，重复启动不会重复迁移。源码中原有的特定 bot＋用户硬编码人设已移除，不自动导入或绑定。人设正文只作请求级系统提示，不写入对话上下文。预设库支持数据备份，专属绑定随 contexts 备份；恢复前校验使用中的人设，联合恢复检查替换后的引用，写入失败回滚并刷新共享缓存；人设库不能批量清理，必须逐项确认无引用后删除。现有备份不等于完整 bot 配置备份。

### 3. 验证

私聊机器人发送任意消息即可得到回复（流式回复按标点+长度分段发送、首段引用触发消息）。

## 🧠 LLM 模型与多提供商（可选）

默认是**单提供商**：直接填 `llm.chat_model` / `chat_base_url` / `chat_api_key`（vision、emotion 各自还有一组同名字段）。需要**同时接入多家**（例如主模型走一个中转、识图走另一家，或不同 bot 用不同 Key）时用 `providers`：

```yaml
llm:
  providers:
    - id: deepseek
      name: DeepSeek
      base_url: "https://api.deepseek.com"   # 自定义/中转地址也填这里
      api_key: "sk-..."
      default_model: "deepseek-chat"         # 该家默认模型
      models: ["deepseek-chat", "deepseek-reasoner"]
    - id: relay
      name: 中转服务
      base_url: "https://relay.example/v1"
      api_key: "sk-..."
      default_model: "gpt-4o"
  chat_provider: "deepseek"   # 默认 chat 用哪套
  vision_provider: ""         # 留空 → 用 vision_base_url / vision_model / vision_api_key
  emotion_provider: ""        # 留空 → 用 emotion_* 旧字段
```

- 也可在 Web 面板「模型配置 → 🏢 多提供商」维护：Key 只显示掩码，留空表示不修改；写入口是 `PUT /api/providers`，`GET /api/config` 同样会掩码。
- 单个 bot 可覆盖：`data/bots/{bot_id}/config.json` 里的 `chat_provider_override` / `vision_provider_override`（Bot 配置页有下拉）。
- 解析顺序：**bot 覆盖 → 全局 `<role>_provider` → 旧字段**；模型优先级：bot `*_model_override` > 提供商 `default_model` > 全局模型。
- 配了不存在的 provider 会打 warning 并回退，不会让消息处理失败。
- 安全：提供商 `api_key` 留空时，只有**同一端点**（未写 `base_url`，或与 `chat_base_url` 相同）才允许继承 chat Key，避免把主 Key 发往未声明的第三方地址。
- 未配置 `providers` 时，行为与旧版完全一致。环境变量 `MOHOBOT_LLM_API_KEY` / `MOHOBOT_VISION_API_KEY` / `MOHOBOT_EMOTION_API_KEY` 仍可作为 Key 来源。

### 识图模式

`llm.vision_mode`：

| 值 | 行为 |
|---|---|
| `describe`（默认） | 先用视觉模型把图片转述成文字再交给主模型；主模型不需要视觉能力 |
| `direct` | 图片直接放进主模型消息（要求 chat 模型多模态），失败自动回退 `describe`；不额外消耗视觉模型调用 |

实测（2026-10-22，DeepSeek 官方接口）：`deepseek-chat` 能正确读出图片文字，可直接用于 `direct` 模式。

## 🚀 生产部署（Ubuntu 服务器）

### 现有生产环境与发布流程

以下为 **2026-10-10 核实的生产环境**；后续部署先检查实际状态。下方 `/opt/mohobot`、专用用户及 `mohobot.service` 是新部署模板，不应在现有生产上重复安装另一套服务。

| 项目 | 现有生产配置 |
|------|------|
| 连接 | `ssh -p 10139 root@103.236.75.12` |
| 工作目录 | `/root/QQBot/mohobot` |
| Python | `/root/.pyenv/versions/3.12.13/bin/python` |
| systemd unit | `migration-mohobot.service` |
| 主进程端口 | 8081（OneBot）、9090（管理面板）、9092（表情包插件启用时） |
| review | 独立子进程，端口 9091；可能随同一 systemd unit 重启 |

**仅以 main 发布。** 本地完成测试后，按用户授权提交、推送，再同步生产。远程变更必须已获明确授权；只改文档不代表授权部署。部署前检查工作区、分支和服务归属，不能直接用本地目录覆盖生产配置或 data。

```bash
# 生产只读预检：检查输出，保留未跟踪的配置备份
ssh -p 10139 root@103.236.75.12 'cd /root/QQBot/mohobot && git status --short && git branch --show-current && git log -1 --oneline && systemctl show migration-mohobot.service -p ActiveState -p MainPID -p WorkingDirectory -p ExecStart'

# 确认已授权、目标正确且没有待处理修改后同步
ssh -p 10139 root@103.236.75.12 'set -eu; cd /root/QQBot/mohobot; test "$(git branch --show-current)" = main; test -z "$(git status --porcelain --untracked-files=no)"; git pull --ff-only origin main; git log -1 --oneline'

# 确认配置有效、所需依赖已具备后，重启现有 unit
ssh -p 10139 root@103.236.75.12 'set -eu; systemctl restart migration-mohobot.service; sleep 14; systemctl show migration-mohobot.service -p ActiveState -p SubState -p MainPID -p NRestarts; ss -ltnp | grep -E ":8081|:9090|:9091|:9092"'
```

- `--ff-only` 失败、存在未处理修改或运行方式不符时，暂停并报告。历史分叉即使 tree 相同也不能自动 reset/merge；备份旧历史和配置后，另行取得处理方案的确认。Git bundle 只备份已提交历史，不包含运行数据或未提交修改。
- 现有生产不使用 screen，不能在 systemd 之外再启动 `main.py`。手工终止进程可能触发自动重启；优先管理已核实的 unit。异常时若必须定位进程，应同时核对 Python exe、项目 cwd 和主入口，避免误操作其他项目。
- Web 面板“重启”是在原进程中重新装配服务，不能代替发布新 Python 代码后的进程重启。
- SSH 中断后重新核对提交、配置、unit/PID、端口和启动日志，不假定剩余命令已执行。
- 验收须确认 8081/9090 属于新 MainPID，按启用情况检查 9091/9092，并在**本次启动**日志确认歌曲索引数量。5495 首仅为 2026-10-10 的核实值，后续应与实际歌曲库核对。
- gateway 是 `/root/QQBot/gateway` 下的另一个项目，历史端口 36712；本次核实时未监听，不能据此擅自启动或重启它。

生产已按用户要求配置 GitHub HTTPS 代理；配置形式如下，不必每次部署重写：

```bash
git config --global url."https://gh-proxy.org/https://github.com/".insteadOf "https://github.com/"
git -C /root/QQBot/mohobot remote set-url origin https://github.com/CarefreeSongs712/mohobot.git
```

global 重写影响该用户所有匹配的 GitHub HTTPS 地址，不作用于 SSH origin。代理是第三方服务，不向其传递凭据、私有配置或运行数据；不关闭 host key/TLS 校验绕过连接问题。

控制台日志使用 `journalctl -u migration-mohobot.service --since '<本次重启时间>' --no-pager`；应用日志仍在 `logs/`，review 另有 `review/panel.log`。解析长 journal 消息时使用 `--all -o json` 并处理 MESSAGE 为字符串、null 或字节列表的情况，避免把省略的消息误判为未启动。

数据库同步、历史迁移和备份恢复不属于常规代码发布，须按需单独授权。现有业务配置应保留：2026-10-10 上线的评估为基准 `persona_002`、新版 `persona_008`，5% 概率、15 分钟冷却；这是部署快照，不改变新安装默认关闭的行为。评估数据需独立导出，不随聊天备份保存。

### 1. 新部署环境准备

```bash
# Python 3.12（示例用 pyenv）
sudo apt update
curl -fsSL https://pyenv.run | bash   # 需要 Python 3.12+
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

### 4. 新部署使用 systemd 运行

仓库提供 [`deploy/mohobot.service`](deploy/mohobot.service) 作为新部署模板。现有生产使用上文的 `migration-mohobot.service`，不要重复安装。新环境先创建专用用户和虚拟环境，再安装服务：

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

> 仅用于开发或临时环境。现有生产由 systemd 管理；不要同时用 screen、nohup 或手工 `python main.py` 启动第二个生产实例。

### 6. 连接 OneBot 客户端

NapCat / LLOneBot / Lagrange 等配置**反向 WebSocket** 连接 `ws://<服务器IP>:8081`（端口见 `config/global.yaml` 的 `server.port`）。多个机器人各开一个连接，`X-Self-ID` 头为各自 QQ；Web 面板（`http://<IP>:9090`）创建 bot 并绑定 QQ 后即可使用。

### 7. 部署后验证与日常维护

- 日志确认插件加载：`grep "Loaded plugin" logs/mohobot_*.log`
- Web 面板返回 200：`curl -o /dev/null -w "%{http_code}" http://127.0.0.1:9090/`
- 私聊 bot 发消息验证回复；群内 `/status` 验证多 bot 去重（只由一个 bot 回复）
- 更新部署：参见上方“现有生产环境与发布流程”；常规使用 `git pull --ff-only origin main`，生产通过 systemd 重启，切勿随意 `kill -9` 或用面板内重启代替代码发布
- **history 合并布局迁移（旧版本升级到群聊合并存储时执行一次）**：停机后 `python scripts/migrate_history_layout.py --data-dir /opt/mohobot/data`（先把旧 `history/{bot_id}/group/` 与遗留 QQ 号目录按 message_id 去重合并进 `history/group/`，旧文件移入 `data/history_legacy_{时间戳}/` 备份；可用 `--dry-run` 预览）。新代码只写合并布局（旧 `history_dual_write` 配置与旧布局写入已移除，读侧仍兼容存量旧文件）

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

**插件配置系统**：插件目录放 `_conf_schema.json` 声明配置项（类型：`string/int/float/bool/list/object`+items，含 `description/hint/default/slider/invisible`），配置存于 `data/plugins_config/{name}.json`（全局一份），Web 面板"插件管理"页自动渲染表单编辑、保存即热生效（调用插件 `on_config_update` 回调）。

**事件钩子**：`on_message` / `on_notice` / `on_meta` / `on_request`（好友申请、群邀请，插件接管后框架不再自动同意）。

**注入**：`inject_ws_server` / `inject_bot_manager` / `inject_data_dir` / `inject_anysearch_client` / `inject_llm_service`（视觉描述/情感等 LLM 能力）/ `inject_admin_ids`（全局管理员，与封禁系统共用配置顶层 `admins`）。

## 👥 关系管理器插件（移植自 astrbot_plugin_relationship）

`plugins/relationship/` — 帮助管理 QQ 好友和群聊（命令带 `/` 前缀，管理员=全局 `admins`，审批员=管理员+配置的额外审批员）：

- **查询/管理**：`/群列表` `/好友列表` `/退群 <序号|群号|区间>` `/删好友 <@|QQ|序号|区间>`（管理员）
- **审批流**：好友申请/群邀请 → 自动规则（黑名单自动拒绝、`auto_agree/reject` 开关）→ 未自动处理时转发审批消息到**审批群**（`manage_group`）或私发审批员 → 审批员**引用该消息**回复 `/同意` `/拒绝` `/拉黑`
- **通知自动处理**：被设为/撤管理员、被禁言（超时自动退群）、被踢（自动拉黑群/用户）、被拉群（小群/大群/群容量/互斥成员检查自动退群）
- **其他**：`/推荐 <群号|@qq>` 发送名片、`/加审批员 @某人` `/减审批员 @某人`

> 移植自 [astrbot_plugin_relationship](https://github.com/Zhalslar/astrbot_plugin_relationship) v3.0.5（Zhalslar），去掉 afdian 校验与"加好友/加群"扩展（无对应依赖），OneBot API 走 mohobot 通用 `send_to_bot`。

## 🗂️ 聊天记录管理插件

`plugins/chat_manager/` — 查看指定会话的聊天记录、以 bot 身份代发消息。两个命令均**仅全局管理员**（`admins`）、带 `/` 前缀，群内多 bot 时只由一个 bot 响应：

- **`/查看聊天 <QQ号|群号> [条数]`** — 从本地 `data/history` 归档读最近条数（默认 20，WebUI 可改；归档不足则全部；**含用户发言与 bot 自己的发言**，转出来是一段完整对话；群聊读跨 bot 合并归档，所有 bot 的发言都在），按时间正序**合并转发到当前会话**（私聊里下命令就走私聊合并转发）。不调用任何历史查询接口，无归档时明确报错。转发可靠性：剥离 reply 引用段（目标会话无法解析、会被 NapCat 拒整批）；图片/语音等 URL 过期导致整批失败时，自动降级为纯文本占位重试一次，保证记录必达
- **`/发送消息 <QQ号|群号> <内容>`** — 把内容以**纯文本段**发到指定会话（不解析 `[CQ:...]`，避免被当消息构造入口）；成功回执、失败给出原因
- **目标自动判定**（群号与 QQ 号都是纯数字，字面无法区分）：`/查看聊天` 按本地归档判定（群归档优先）；`/发送消息` 按 `get_group_list` 判定（在群列表里即群，否则当私聊）——群列表接口失败时**取消发送**，不猜测
- **配置**（WebUI 插件页可改、热生效）：`default_count`（默认条数）/ `batch_size`（单批转发条数，超过自动分批、批间隔 0.5 秒）

> 原 relationship 插件的 `/抽查` 与"被拉进新群自动抽查"已移除，查看聊天记录功能由此插件接管（不再依赖 `@` 定位、不再调用历史查询 API）。

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

## 📝 QQ空间说说插件（移植自 astrbot_plugin_qzone_lite）

`plugins/qzone/` — QQ 空间说说：看/发/删/评/回评/赞（命令带 `/` 前缀，多 bot 群去重）：

- **命令**：`/看说说 [@QQ] [序号/范围]`（无 @ 时看动态流；范围如 `0~3`）、`/评说说 [@QQ] [序号] <内容>`、`/回评 [@QQ] [说说序号] [评论序号] <内容>`、`/赞说说 [@QQ] [序号/范围]`；管理员：`/发说说 <文本> [图片]` `/删说说 [序号/范围]` `/重置QQCookies` — **管理员命令仅私聊处理，群聊内一律静默忽略**
- **登录态 per-bot**：每个 bot 经自己的 OneBot 连接调 `get_cookies(user.qzone.qq.com)` 自动获取 Cookies，缓存于 `data/plugins_data/qzone/cookies_{bot_id}.json`，失效自动重取（可关 `auto_reset_on_login_expired`）
- **发图**：`/发说说 文本` 支持消息自带图片 + 引用（回复）一条带图消息取图（经 `get_msg` API）
- **看说说图片分析**（`analyze_images_on_view_feed`，默认关）：带图说说经框架视觉模型描述图片内容（走 `inject_llm_service` 注入 + ImageCache 缓存）
- **防限流**：请求最小间隔 + 随机抖动、429 指数退避、登录失效自动重登录；说说查询带 LRU+TTL 缓存（复用正文/图片，只刷新评论）
- **自动回复**（LLM 生成，默认关）：① **自己说说被评论** — 周期扫描自己最新 N 条说说，新评论自动回复（有 tid 回评、无 tid 评论；首启基线不回复历史；只回复时限内的新评论）；② **被@自动回复** — 双模式：`feeds_scan`（默认，扫描好友动态流正文/评论中的 `@bot昵称`，开箱即用）或 `api`（调用「与我相关」内部接口，需配置 `atme_api_url`，支持 `{uin}` 占位；`/与我相关` 命令可查看接口原始响应用于适配解析）。回复由 chat 模型生成（system 带 bot 人设，提示词模板 `{nick}/{content}/{post}` 可配，失败降级固定文案），按 bot 去重持久化（`auto_reply_{bot_id}.json`），轮询失败退避 10 分钟
- **配置**（WebUI 插件页可改、热生效）：`send_feedback` / `analyze_images_on_view_feed` / `feed_cache_max_size` / `feed_cache_ttl_seconds` / `timeout` / `request_interval` / `request_jitter` / `comment_reply_enabled` / `atme_reply_enabled` / `atme_mode` / `atme_api_url` / `scan_interval_sec` / `auto_reply_prompt` 等
- **主动发说说**（`auto_publish_enabled`，默认关）：每天在时间窗（`auto_publish_window_start/end`）内随机抽 N 个时刻（`auto_publish_daily_count`，相邻最小间隔可配）由各 bot 用自己的人设发一条说说。话题来自**与用户的正式互动**——从情感系统长期记忆提炼抽象话题词（LLM，12h TTL 自动过期，`auto_publish_topic_ttl_sec` 可调），**绝不输出用户昵称/QQ/对话原话等具体内容**（隐私禁令写死在 system prompt）；无可用话题时回退自由发挥，静态话题池 `auto_publish_topics` 可叠加兜底。计划持久化（重启不重发）、最近 20 条历史作为去重素材、可排除指定 bot、发布后可私聊通知管理员。命令（管理员/私聊）：`/发日常` 立即发一条测试、`/发布记录` 查看计划与历史

> 移植自 [astrbot_plugin_qzone_lite](https://github.com/Zhalslar/astrbot_plugin_qzone)（上游 Zhalslar/astrbot_plugin_qzone 的 Lite 裁剪版，GPL-3.0）。不迁移 LLM Tools；Cookies 由协议端自动获取（需 NapCat/LLOneBot 等支持 `get_cookies`），不再提供手动 cookies 配置；可选依赖 `json5`（缺失时降级 `json.loads` 解析响应）。

## 🎵 歌曲知识（全局歌曲识别 + LLM 前注入）

歌曲知识为全局能力（私聊+群聊）：用户消息里含歌曲信息（歌名或歌词）时，在发送给 LLM 之前识别出是哪首歌，把「歌曲介绍 + 词/曲/混/调等创作人员 + 歌词」加入本轮主系统提示的 **【歌曲参考资料（服务端检索）】**。它是请求级服务端参考，不属于用户发言，不写入 context 文件；不再输出 `[sing]` 唱歌文本。

实现位于 `mohobot/music_knowledge/`：

1. **SQLite 事实库**（`song_database.py`）— `songs` 宽表存合并后的歌曲字段（`name / safe_name / uploader / singers / lyricist / composer / arranger / mixer / tuner / mastering / pv / illustrator / year / introduction / lyrics`，以及 `aliases / bvid / lang / normalized_name / primary_source`）；`song_sources` 记录每个来源的原始抓取结果与许可，`sync_state` 记录同步断点。
2. **匹配器**（`matcher.py` + `fuzzy.py`，保守优先）— `《歌名》` → 裸文本歌名（长度≥3 且含语境词）→ 歌词片段 → **模糊层**：歌名允许漏字/错字/相邻颠倒（编辑距离 ≤1 且候选唯一），歌词用 6-gram 倒排索引（漏字、错字仍可命中）。模糊命中带置信度，低置信或候选歧义时不注入，避免把日常聊天误认成歌曲。
3. **多来源同步**（`sources/` + `merge.py` + `sync.py`）— 三个来源：
   - **VCPedia**：原有来源，洛天依曲最全，内置 Anubis PoW 反爬与 cookie 复用；
   - **voca.wiki**：MediaWiki API + wikitext，结构最干净，可补 BV 号与词曲编；
   - **萌娘百科**：中文曲覆盖最厚（洛天依 3524、乐正绫 1026、言和 1000），分类与歌词走 HTML，需移动 UA 与 4–5 秒间隔。

   合并时按字段分来源优先级（歌词/演唱取 VCPedia 或 voca.wiki；词曲编混调/PV/曲绘/简介/年份取萌娘百科），**只补空字段、不覆盖非空值**，同名不同歌手的歌不会被误合并。支持三种模式：`new_only`（补未收录）、`fill_missing`（补残缺字段，默认）、`full_refresh`（全量重抓），断点续跑、单来源熔断。
4. **定时与手动触发** — 在 `music_knowledge.sync` 打开 `enabled` 后由 `song_sync` 插件按 `interval_hours` 自动同步；也可手动 `/sync-songs [new|fill|refresh|status|vcpedia|vocawiki|moegirl]`（管理员）或 `python scripts/sync_songs.py`。
5. **注入预算** — 默认只注入「介绍摘要 + STAFF + 歌词命中处前后几行」，完整歌词由 LLM 调 `song_get_lyrics` 工具按需获取；改 `music_knowledge.injection.max_chars` 可调大或设为 0 恢复完整歌词。
6. **旧数据迁移** — `python scripts/migrate_legacy_songs.py` 把旧版数据迁移进新 schema。

**网易云点歌插件（`/点歌`）不受影响**：仍走独立命令路径，不进入本歌曲知识链路。

配置在顶层 `music_knowledge`（完整示例见 `config/global.example.yaml`）：

```yaml
music_knowledge:
  enabled: true
  song_database:
    db_folder: "./data/song_knowledge"
    db_file: "knowledge_db.db"
  fuzzy_enabled: true
  min_confidence: 0.8
  injection:
    max_chars: 1200        # 0 = 注入完整歌词
    lyric_context_lines: 4
  sync:
    enabled: false         # 定时同步开关
    interval_hours: 24
    mode: fill_missing     # new_only / fill_missing / full_refresh
    order: [vcpedia, vocawiki, moegirl]
    sources:
      vcpedia: {enabled: true, interval: 0.8}
      vocawiki: {enabled: true, interval: 0.4}
      moegirl: {enabled: true, interval: 4.5}   # 实测更快会触发 403
```

## 🔊 TTS 语音（可切换后端：自建 HTTP / MiniMax）

把文字转成语音发送，支持两种合成后端（`tts.backend` 配置切换，WebUI 保存即热切换）：

- **`http`（默认，当前启用）**：自建 HTTP 合成服务 —— `POST {base_url}/tts`（Bearer 鉴权，`{"text": ...}`）→ wav 字节流，本地 ffmpeg 转 mp3 后发送（NapCat 兼容性已验证；ffmpeg 不可用/转换失败自动降级发原始 wav）。
- **`minimax`（保留仅下线）**：MiniMax `t2a_v2` 云端合成，音色为 MiniMax 克隆音色（voice_id），全局默认 + 每 bot `tts_voice_id` 覆盖（仅此后端生效；http 后端音色由服务端固定）。

两条通路（与后端无关）：

- **LLM 自动朗读**：系统提示词引导模型用 `<tts></tts>` 标注想朗读的内容——**尽量每条回复都标注，长短与多少由模型自行决定**（可一句、多段甚至整条回复，不限字数）。框架剥掉标签后文本照常分段发送，标注内容**仍显示**；全文发送完毕后把所有标注**合并为一条语音**（按出现顺序拼接），经全局单飞行队列合成，语音跟在最后一个文本段之后由回复的 bot 单独发出。没标注/合成失败/队列满 → 当轮无语音，文本不受影响。主回复产物过滤支持**模糊识别**：`< tts >` 空格、`<voice>/<speech>/<say>` 等别名（大小写不敏感）、`【tts】/[tts]` 方括号及圆括号变体；已知尖括号标签可带属性，近似未知名如 `<ttsx>` 不计入朗读、按格式标签剥壳保正文。工具/内部块按过滤策略丢弃；普通比较文本不会按朗读标签处理，具体边界见回复过滤回归样本。
- **`/tts <文本>` 指令**（群聊多 bot 按全局命令机制抽签去重）：文本直接转语音；非管理员限 30 字 + 120 秒冷却（全局配置可改），管理员不限。
- **无标注概率朗读**（`tts.untagged_tts_prob`，默认 0.1，WebUI 可配）：回复**没有**任何 `<tts>` 标注时，按此概率（0~1，0=关闭）把整条展示文本入队朗读；**仅队列未满时参与抽样**，命中后与标注路径同管道（同一队列 + OOC 守卫），合成失败同样静默。有标注的回复不受该概率影响。

**并发与用量**：全局 FIFO 队列单飞行串行；队列满（上限可配，默认 16）**丢弃最新**请求。每次成功合成把字符数记入用量统计（`module="tts"`，按 bot 维度），WebUI 用量页可见。

**WebUI「🔊 TTS 语音」独立板块**：服务与队列状态（后端/配置完整度/当前合成/队列深度/成功·失败·丢弃·字符计数，5 秒自动刷新）+ 合成配置表单（后端切换、API Key 掩码、超时、mp3 转码开关、队列上限、指令限制、标注提示词模板、MiniMax 参数组）+ **测试合成**按钮（绕过队列直调后端，显示延迟并可在浏览器试听）。

**配置**（WebUI 独立板块可编辑；HTTP Token 用 MOHOBOT_TTS_API_KEY 兜底，MiniMax 用 MOHOBOT_MINIMAX_API_KEY；队列容量修改需重启，启动时未装配 TTS 服务则后续启用也需重新装配/重启）：

```yaml
tts:
  enabled: true
  backend: "http"                      # http=自建服务(默认); minimax=云端(仅下线)
  base_url: "http://127.0.0.1:9890"    # 自建服务地址
  api_key: "Bearer token"              # 或环境变量 MOHOBOT_TTS_API_KEY
  convert_to_mp3: true                 # http 后端: wav → 本地 ffmpeg 转 mp3
  queue_maxsize: 16
  timeout: 60
  cmd_max_chars: 30
  cmd_cooldown: 120
  untagged_tts_prob: 0.10              # 无标注回复的概率朗读(0~1, 0=关闭)
  # ── 以下仅 backend=minimax 时生效 ──
  model: "speech-2.8-hd"
  voice_id: "ltyclone01"
  speed: 1.0
  vol: 1.0
  pitch: 0
  sample_rate: 32000
  bitrate: 128000
  format: "mp3"
```

主服务在 [tts.py](mohobot/services/tts.py)（HttpTTSClient + 可切换后端的 TTSService + ffmpeg 转码）、[minimax_tts.py](mohobot/services/minimax_tts.py)（MiniMax 客户端）。主回复过滤使用 [reply_filter.py](mohobot/utils/reply_filter.py)，[tts_marker.py](mohobot/utils/tts_marker.py) 保留兼容解析；行为边界参见 [回复过滤回归](tests/test_reply_filter.py)。

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
│   ├── history_dedup.py           # 跨账号稳定的消息指纹与群消息归档去重
│   ├── knowledge/                 # 虚拟歌手知识库（RAG 分块 + 向量存储 + 语义检索与 Rerank）
│   ├── music_knowledge/           # 歌曲知识库（SQLite 事实库 + 匹配 + 多源定时同步）
│   ├── ban/                       # 封禁系统（移植自 reneban，去 server/云同步）
│   │   ├── models.py              # 封禁记录数据模型
│   │   ├── time_utils.py          # 时间解析（1d/2h/30m/10s）
│   │   ├── store.py               # 名单存储（全局统一 + 缓存 + 过期清理）
│   │   └── ban_filter.py          # 拦截器（静默过滤 + 管理命令）
│   ├── interceptors/              # 拦截器（封禁、指令、关键词、插件系统）
│   ├── models/                    # OneBot 协议与配置模型
│   ├── utils/                     # 日志、CQ 码解析等工具
│   └── web_panel/                 # FastAPI 管理面板（含封禁管理、情感管理板块）
├── plugins/                       # 插件目录（动态加载：status / praise / divination /
│                                  #   neteasemusic / wifepicker / relationship / song_sync /
│                                  #   qzone / perception / welcome / usage_stats / chat_manager ...）
├── tests/                         # 冒烟测试（smoke_*）与单测（tests/_run_all.py 全量回归）
├── data/                          # 运行时数据（自动生成，勿提交）
│   ├── bots/{bot_id}/             # Bot 配置与状态
│   ├── history/group/             # 群聊消息事件 JSONL（跨 bot 合并存储、写入去重）
│   ├── history/{bot_id}/private/  # 私聊消息事件 JSONL（收到的 message + bot 发送的 message_sent）
│   ├── contexts/{bot_id}/         # 【可读写】会话上下文
│   ├── database/                  # SQLite（mohobot.db）
│   └── cache/images/              # 图片缓存
└── logs/                          # 日志（自动生成，勿提交）
```

## 🧠 数据架构

**原则：原始数据不可变，工作数据可变；历史入库，上下文不变。**

| 数据 | 位置 | 性质 | 格式 | 用途 |
|------|------|------|------|------|
| 聊天历史 | `data/history/` | 追加式归档 | JSONL（每行一个事件） | 审计、全量回溯、审核面板数据源 |
| 会话上下文 | `data/contexts/` | 可读写工作区 | JSON（数组） | LLM 实时推理的记忆（**保持原有管理方式不变**） |
| 对话记录 | SQLite `conversations` 表 | 可读写 | SQL | 历史入库（独立 `mohobot.db`） |

- **聊天历史**：**群聊跨 bot 合并存储**在 `data/history/group/{群号}.jsonl`（同一群号一个文件；同一条群消息多只 bot 都会收到，写入时按 `message_id` 近期窗口去重只落一份，行内 `bot_id` 标注接收 bot）；**私聊按 bot 分目录** `data/history/{bot_id}/private/{QQ号}.jsonl`（不同 bot 与同一用户的私聊是不同对话）。**绝不**用于 LLM 实时输入；包含两类事件：收到的消息（`post_type: "message"`）与 **bot 自己发送的消息**（`post_type: "message_sent"`，由 WSServer 出站层在发送时归档，echo 返回的 `message_id` 一并落档；图片/语音的 `base64://` 大字段替换为占位防膨胀）。旧布局（`history/{bot_id}/group/`）已停止写入（`history_dual_write` 配置与写入代码已删除，2026-10-23）；存量旧文件仍可按旧 session key 被 review loader 读取（面板会话列表只枚举 `history/group/`，需要时先跑迁移脚本）；存量数据可用 `python scripts/migrate_history_layout.py`（**停机运行**）一次性去重合并，旧文件移入 `data/history_legacy_{时间戳}/` 备份
- **会话上下文**：私聊一个用户可有多个会话（`sess_001`…由 `session_index.json` 索引），群聊固定 `main.json`；满 40 轮触发 AI 总结压缩（最早的 15 轮 → 总结块插最前，详见上文配置）
- **数据隔离**：会话上下文按 bot_id 分目录；群聊历史为所有 bot 共享

## 📋 聊天记录审核面板（review/, 端口 9091）

半独立 WebUI：**独立进程**（mohobot 启动时若 `review/config.yaml` 存在且 `enabled: true` 则拉起；手工退出主进程通常不影响它，但现有 systemd unit 操作可能一并停止同 cgroup 子进程，重启后须检查 9091 恢复）、独立端口（默认 9091）、独立配置与独立 SQLite（`review/data/review.db`），对 mohobot 的 `data/` **严格只读**。

- **数据源**：`data/history` 消息事件流（唯一来源）。history 只增不删，消息身份用 **message_id** —— 审核结论永不因上下文压缩/改写而失联
- **审核范围（面板侧过滤，归档保持完整）**：群聊只审 **bot 的发言** 与 **用户 @ 某只 bot 或引用某只 bot 发言** 的消息；私聊全部审
- **群聊单文件会话**：群聊归档本身已跨 bot 合并存储（`history/group/{群号}.jsonl`，写侧去重），审核面板直接读单文件；bot 发言按行内 `bot_id` 归属标注，用户消息按 @/引用 命中归属到对应 bot，侧栏显示会话涉及的 bot
- **界面**：审核（按会话分页浏览，默认锚定第一条待审）、异常记录（标签：色情/政治/辱骂/其他，可编辑、可导出 CSV）、统计（总量/按 Bot/按审核员/操作日志）
- **账号**：`review/config.yaml` 手工维护用户（密码只存 PBKDF2 哈希，`python -m review.hash_password` 生成）；登录后可自助改密（仅自己，需旧密码，写回保留注释）
- **登录防爆破**：登录处理全局串行化 + 每次尝试固定 0.5s 硬延迟（登录耗时恒定防计时侧信道）

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
8. 💗 **情感管理** — 每用户好感度/亲密度/关系阶段/长期记忆查看（只读记忆），设置好感/亲密/态度、重置用户、清空 bot 数据（确认框防护），分析队列/burst/熔断运行状态 5 秒自动刷新

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
- [x] 全局指令去重（群内多 bot 按消息抽签选择一只回复，支持命令+空格参数）
- [x] Web 管理面板（总览/配置/模型/插件/对话/日志/设置及 TTS/数据/封禁/情感/人设管理/人设评估）
- [x] 插件系统（status / praise / divination / neteasemusic / wifepicker / relationship / song_sync / qzone / chat_manager）
- [x] /help 图片渲染（PIL 深色卡片，按插件分组 + 管理员标注）
- [x] 每 bot 消息发送限流与出站队列（容量/间隔可配置）
- [x] 本地回归 runner、并发专项与离线装配冒烟测试（参见开发指南；当前环境验证状态见接手说明）
- [x] 自动化 CI（已合入 main）：PR 门禁为 Python 3.12 测试、语法检查、meme 插件回归与信息性 Ruff 债务报告；不再跟踪 3.10/3.11
- [ ] Docker 部署

## 📄 License

核心框架采用 [MIT](LICENSE)。移植插件保留各自上游许可；表情包插件与主动聊天插件分别有 [AGPL-3.0](plugins/meme_stealer/LICENSE)、[AGPL-3.0](plugins/proactive_chat/LICENSE)，其他模块参见其声明。分发或提供网络服务前应核对对应义务，不能把全部插件统一视为 MIT。

## 🙏 致谢

- [OneBot 标准](https://github.com/botuniverse/onebot-11) — 聊天机器人应用接口标准
