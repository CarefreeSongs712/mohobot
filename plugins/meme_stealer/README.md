# 表情包小偷（mohobot 插件）

[mohobot](https://github.com/CareFreeSongs712/mohobot) 版的「表情包小偷」，移植自 AstrBot 插件 `astrbot_plugin_stealer`。

- **偷**：观察群聊/私聊里的表情包，按概率（或冷却）偷取；视觉模型一次完成内容审核与语义标注（图上文字、描述、标签、适用场景），通过即入库。
- **发**：bot 要回复的消息按概率附带一段选图提示，会话模型自己调用 `search_meme` 按语义检索、`send_meme` 选图；表情在本条回复全部发出后再发送。用户主动要表情包时不受概率限制。
- **管**：`/meme` 指令，以及独立端口的 WebUI（浏览、编辑、待审核、外部表情包源导入、语义空间）。

## 安装

1. 把整个 `meme_stealer/` 目录放到 mohobot 的 `plugins/` 下（目录名就是插件名，必须叫 `meme_stealer`）：

   ```
   mohobot/plugins/meme_stealer/
   ├── main.py              # mohobot 入口（class Plugin）
   ├── _conf_schema.json    # 面板配置
   ├── meme_stealer_core/   # 插件核心
   ├── pages/  i18n/  prompts.json  ...
   ```

2. 依赖：插件用到的库（Pillow、numpy、aiohttp、openai、pydantic、starlette、uvicorn、python-multipart）mohobot 都已自带，无需额外安装。可选 `pip install umap-learn`，WebUI「语义空间」会用 UMAP 投影，未安装时自动改用 PCA。
3. 重启 mohobot，或在面板「插件管理」点「重载」。

> 需要较新的 mohobot：`LLMService._execute_tool` 要能执行任意名字的插件工具（2026-10 的 main 分支已支持）。更早的版本只执行 `song_` 前缀的工具，`search_meme` 等会返回「未知工具」。

## 配置

面板 → 插件管理 → `meme_stealer` → ⚙️ 配置，保存即生效。存档在 `data/plugins_config/meme_stealer.json`。

最少需要：

| 配置项 | 说明 |
|---|---|
| 【偷图】开启表情包偷取 | 默认关闭；也可用 `/meme on` |
| 【模型】视觉模型 | 留空沿用 mohobot 全局 `llm.vision_model` |
| 【模型】嵌入模型 | mohobot 没有全局嵌入模型，要语义检索就必须填（OpenAI 兼容 `/embeddings`，如 `text-embedding-v4`、`BAAI/bge-m3`）；不填则检索降级为关键词（BM25） |

模型接口的回退规则：

- 视觉：插件填了「接口地址」就只用插件里的 API Key；地址留空时用 mohobot 的 `llm.vision_base_url` 与密钥（`llm.vision_api_key` → `MOHOBOT_VISION_API_KEY` → chat 密钥）。
- 嵌入、检索改写：地址留空时用 mohobot 的 `llm.chat_base_url` 与 chat 密钥；改写模型名留空时用 `llm.chat_model`。
- 地址和密钥成对回退，不会把全局密钥发给你在插件里填的另一个服务商。
- 用量记入 mohobot「数据总览」，模块名 `meme_stealer`。
- 更换嵌入模型后，旧向量会自动清空并在后台重建。

注意：mohobot 面板显示插件配置时不做掩码，插件里填的 API Key 对能登录面板的人可见。

## 使用

**偷图**：开启后，群里所有消息（包括没 @ bot 的）都会被观察；QQ 表情（`sub_type=1` / 商城表情）按概率偷取，交给视觉模型审核标注。标注时会附上图片发出前的聊天记录帮助理解梗，但描述里不写具体人名和话题。同一条群消息被多个 bot 收到时只处理一次。

**配表情**：bot 要回复的消息（私聊，或群里 @ 了 bot / 引用了 bot 的消息）按「提示配表情包的概率」附带选图提示。提示经 mohobot 的环境感知机制并入本轮请求，不写入对话上下文。会话模型：

1. `search_meme(description)`：描述想发的表情包（画面、图上文字、语气），按语义返回若干候选；
2. `send_meme(emoji_id)`：选一张，等本条回复发完后以 QQ 表情包形式发出。

同一会话内发出表情后有 20 秒冷却（按 bot 分别计算）。

**让 bot 收图**：对 bot 说“偷一下”并附图，或**引用**一张图再 @ bot 说“偷了”，会话模型会调用 `steal_meme`。

### 指令

| 指令 | 权限 | 说明 |
|---|---|---|
| `/meme status` | 所有人 | 运行状态与统计 |
| `/meme list [分类] [每页数量] [页码]` | 所有人 | 以图片列出表情包 |
| `/meme help` | 所有人 | 指令列表 |
| `/meme on` / `off` | 管理员 | 开关偷取 |
| `/meme auto_on` / `auto_off` | 管理员 | 开关聊天配表情 |
| `/meme 偷` | 管理员 | 30 秒内发的下一张图直接入库 |
| `/meme group <send\|steal> <wl\|bl> <add\|del\|clear\|show> [目标]` | 管理员 | 名单管理，目标写 `group:群号` / `user:QQ` |
| `/meme delete <序号\|文件名>` / `blacklist <…>` | 管理员 | 删除 / 删除并拉黑 |
| `/meme scope <序号\|文件名> <public\|local>` | 管理员 | 设为仅来源群可用 |
| `/meme clean [raw]` / `capacity` / `rebuild_index` / `tag_stats [N]` | 管理员 | 清理、容量控制、重建索引、标签统计 |

管理员即 mohobot 全局配置的 `admins`。群里有多个 bot 时 `/meme` 只由其中一个回复。

## WebUI

1. 配置里打开「【WebUI】启用表情包管理页面」，并设置「登录密码」。
2. 默认监听 `http://127.0.0.1:9092`（避开 mohobot 面板的 9090 和审核面板的 9091）。
3. 要从其他机器访问，把监听地址改成 `0.0.0.0`，并建议放在 HTTPS 反向代理之后。

登录后会话保存在 HttpOnly Cookie 里（12 小时，插件重载后需重新登录），修改密码会让已有会话立即失效。登录有固定延迟以减缓暴力尝试。

## 数据

```
data/plugins_data/meme_stealer/
├── categories/<分类>/   # 表情文件
├── pending/             # 待审核
├── cache/emoji.db       # 索引、标签、向量（SQLite）
├── external_sources/    # 外部源下载与上传暂存
└── kv_store.json        # WebUI 偏好
```

### 从 AstrBot 版迁移

1. 停掉 mohobot，把 AstrBot 的 `data/plugin_data/astrbot_plugin_stealer/` 整个复制为 `data/plugins_data/meme_stealer/`。
2. 启动后插件会把数据库里的旧绝对路径改到新目录（只要文件仍在 `categories/<分类>/` 或 `pending/` 的同一位置，Windows 路径也行），描述、标签、收藏、使用次数、作用域都会保留。
3. 配置项名大多相同，可以把 AstrBot 的插件配置值抄到面板里。变化的只有模型：
   - `vision_provider_id` → `vision_model`（+ 可选 `vision_base_url` / `vision_api_key`）
   - `embedding_provider_id` → `embedding_model`（+ `embedding_base_url` / `embedding_api_key` / `embedding_dimensions`）
   - `meme_query_rewrite_provider_id` → `meme_query_rewrite_model`（+ 地址 / 密钥）
   - 新增 `webui_*`；`qqofficial_steal_mode` 已删除（mohobot 只接 OneBot，Telegram / QQ 官方平台的代码都已去掉），旧存档里的这个键会被忽略。
4. 嵌入模型若与原来不同，旧向量会在后台自动重建。

## 与 AstrBot 版的差异

- 只支持 OneBot v11（NapCat 等），按 mohobot 的反向 WebSocket 发消息；表情以 `base64://` 图片段发送，开启「以 QQ 表情包形式发送」时带 `sub_type=1`。
- 选图提示并入本轮请求的系统提示（mohobot「环境感知」段），而不是追加在用户消息之后；同样不写入对话上下文。
- mohobot 不把 bot 自己的回复交给插件，标注时的聊天记录里没有本 bot 的发言（其他 bot 的发言会作为普通消息出现）。
- WebUI 是插件自己起的独立端口与登录，不在 mohobot 面板里。
- 没有 AstrBot 的通用消息工具，因此去掉了“统计 `send_message_to_user` 发出的表情”。

## 开发

```bash
pip install -r requirements-dev.txt   # 在 mohobot 的环境里
python -m pytest tests
python tests/web/test_server.py --port 8091   # 用内存假数据预览前端，不需要 mohobot
```

测试不需要 mohobot 本体：`tests/test_plugin_entry.py` 用最小替身驱动 `main.py` 的真实 `Plugin`。

结构：

- `main.py`：mohobot 入口。把 `on_perception`（选图提示）、`on_message_observed`（偷图、聊天记录）、`on_message`（`/meme`）、LLM 工具和 WebUI 生命周期接到核心上；当前消息经 `ContextVar` 传给工具。
- `meme_stealer_core/host/`：适配层，核心代码只通过它接触宿主（消息事件、OneBot 段、模型网关、日志、配置存档）。
- `meme_stealer_core/app.py`：`StealerApp`，原插件 `Main` 去掉框架装饰器后的服务容器。
- `meme_stealer_core/core/`、`api/`：与原插件基本一致，只替换了框架相关的导入和模型调用。
- `meme_stealer_core/web/`：Starlette 实现的 Quart 兼容请求层 + WebUI 服务。
