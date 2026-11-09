# 插件速查

本文件承接 README 的能力概览，记录各内置插件的命令与配置入口。命令细节以 `/help` 输出、各插件 `_conf_schema.json` 与源码为准；权限说明中的「管理员」均为全局 `admins`。

## 插件一览

| 插件 | 位置 | 功能 | 有独立文档 |
|---|---|---|---|
| status | `plugins/status/` | `/status` 状态卡 | 否（配置 `admin_qqs`/`community_group`/`help_url`，见 `_conf_schema.json`） |
| praise | `plugins/praise.py` | `赞我`/`zanwo` 点赞 | 否 |
| divination | `plugins/divination.py` | `/占卜` | 否 |
| perception | `plugins/perception/` | 日历/节日感知注入 | 否 |
| welcome | `plugins/welcome.py` | 欢迎语 | 否 |
| usage_stats | `plugins/usage_stats/` | `/用量` 统计 | 否 |
| anysearch | `plugins/anysearch.py` | `/搜索` + LLM 搜索工具 | 否 |
| song_sync | `plugins/song_sync/` | 歌曲库定时同步 | 否（详见 [DEVELOPMENT §8.6](DEVELOPMENT.md)） |
| chat_manager | `plugins/chat_manager/` | 查看聊天记录 / 代发消息 | 否（见下） |
| relationship | `plugins/relationship/` | 好友/群管理与审批流 | 否（见下） |
| wifepicker | `plugins/wifepicker/` | 抽老婆互动 | 否（见下） |
| neteasemusic | `plugins/neteasemusic/` | 网易云点歌 | 否（见下） |
| qzone | `plugins/qzone/` | QQ 空间说说 | 否（见下） |
| meme_stealer | `plugins/meme_stealer/` | 表情包偷取与管理 | [README](../plugins/meme_stealer/README.md) |
| proactive_chat | `plugins/proactive_chat/` | 主动聊天 | [README](../plugins/proactive_chat/README.md) |

## 插件系统与配置

- **形态**：单文件插件（`plugins/xxx.py`）或目录插件（`plugins/xxx/main.py` + 可选 `core/`）。
- **配置**：目录插件在自己的 `_conf_schema.json` 声明配置项；配置存于 `data/plugins_config/{name}.json`（全局一份），WebUI「插件管理」页表单编辑、保存即时生效（调用插件的 `on_config_update`）。单文件插件无法拥有独立 schema，需 per-plugin 配置时用目录形态。
- **事件钩子**：`on_message` / `on_notice` / `on_meta` / `on_request`（好友申请、群邀请，插件接管后框架不再自动同意）。
- **注入**：`inject_ws_server` / `inject_bot_manager` / `inject_data_dir` / `inject_anysearch_client` / `inject_llm_service` / `inject_admin_ids`。

## 🗂️ 聊天记录管理插件（chat_manager）

查看指定会话的聊天记录、以 bot 身份代发消息。两个命令均**仅全局管理员**、带 `/` 前缀，群内多 bot 时只由一个 bot 响应。

- **`/查看聊天 <QQ号|群号> [条数]`** — 从本地 `data/history` 归档读最近条数（默认 20，可配置；归档不足则全部；含用户与 bot 的发言，群聊读跨 bot 合并归档），按时间正序合并转发。不调用历史查询接口，无归档时明确报错；图片/语音 URL 过期导致整批失败时，自动降级纯文本占位重试一次。
- **`/发送消息 <QQ号|群号> <内容>`** — 把内容以纯文本段发到指定会话（不解析 `[CQ:...]`，避免被当消息构造入口）；成功回执、失败给出原因。
- **目标判定**：群号与 QQ 都是纯数字，按上下文判定（`/发送消息` 按 `get_group_list`，接口失败即取消）；配置项 `default_count` / `batch_size`。

## 👥 关系管理器插件（relationship）

管理 QQ 好友和群聊；审批员 = 管理员 + 插件配置的额外审批员。

- **查询/管理（管理员）**：`/群列表` `/好友列表` `/退群 <序号|群号|区间>` `/删好友 <@|QQ|序号|区间>`
- **审批流**：好友申请/群邀请 → 自动规则（黑名单自动拒绝、`auto_agree/reject` 开关）→ 未自动处理时转发到审批群（`manage_group`）或私发审批员 → 审批员引用消息回复 `/同意` `/拒绝` `/拉黑`
- **通知自动处理**：设为/撤管理员、被禁言（超时自动退群）、被踢（自动拉黑群/操作者）、被拉群（小群/大群/容量/互斥成员检查后自动退群）
- **其他**：`/推荐 <群号|@qq>` 发送名片；`/加审批员 @某人` `/减审批员 @某人`

## 🌸 抽老婆插件（wifepicker）

群聊互动：活跃成员抽「今日老婆」、我的老婆、强娶（冷却+排除列表）、关系图、rbq 排行、求婚（30 秒同意/拒绝交互，拒绝后可强娶确认）。

- **命令**（带 `/` + 别名）：`/今日老婆`(jrlp) `/我的老婆`(wdlp) `/强娶 @某人`(qiangqu) `/关系图`(gxt) `/rbq排行`(rbqph) `/求婚 @某人`(qh) `/抽老婆帮助`(clpbz)；管理员：`/重置记录` `/重置强娶时间` `/重置求婚时间`
- **关键词触发**（默认关）：直接发 `jrlp`/`抽老婆` 等无需 `/`（仅群聊）。
- **渲染**：关系图/排行用 Playwright 渲染图片；未安装依赖时自动降级文本。
- **数据**：`data/plugins_data/wifepicker/data.json`；支持群白/黑名单；自动撤回停用。

## 🎵 网易云点歌插件（neteasemusic）

- **`/点歌 <关键词>`**（别名 `/music` `/听歌` `/网易云`）：搜索展示编号列表，群内任意成员回复数字即可选歌（60 秒内有效）。
- 播放走 record 语音（NapCat 拉取 URL，失败降级文本链接）；音质按配置优先级回退（lossless → exhigh → higher → standard）。
- 点歌命令群内多 bot 抽签去重；数字选择状态按 bot/会话隔离。
- **配置**：`api_url`（自部署 NeteaseCloudMusicApi）/ `quality` / `search_limit` / `cookie`；不支持自然语言点歌。

## 📝 QQ 空间说说插件（qzone）

看/发/删/评/回评/赞说说；管理员命令仅私聊处理，群内忽略。

- **命令**：`/看说说 [@QQ] [序号/范围]`、`/评说说`、`/回评`、`/赞说说`；管理员：`/发说说 <文本> [图片]` `/删说说` `/重置QQCookies`；另有 `/发日常`（立即发一条）、`/发布记录`、`/与我相关`。
- **登录态**：经各 bot 的 OneBot 连接自动获取 Cookies（需协议端支持 `get_cookies`），缓存 `data/plugins_data/qzone/cookies_{bot_id}.json`，失效自动重取。
- **看说说图片分析**（默认关）：带图说说经视觉模型描述内容。
- **防限流**：请求间隔+随机抖动、429 指数退避、登录失效自动重登；说说查询 LRU+TTL 缓存。
- **自动回复**（默认关）：自评自动回评（周期扫描新评论），被 @ 自动回复（`feeds_scan` 默认 / `api` 模式），固定文案兜底。
- **主动发说说**（默认关）：每日时间窗内随机发 N 条；话题取自情感长期记忆的抽象话题词（不输出用户昵称/QQ/原话），可配 `auto_publish_topics` 兜底。

## 封禁 / 歌曲知识 / TTS

全局封禁名单 commands、歌曲识别与注入、TTS 后端的完整说明见折 [DEVELOPMENT §8](DEVELOPMENT.md)（封禁 §8.5、歌曲 §8.6、TTS §8.4）；配置示例见 `config/global.example.yaml`。
