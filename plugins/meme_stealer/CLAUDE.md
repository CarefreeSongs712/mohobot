# CLAUDE.md

mohobot 插件「表情包小偷」（目录插件 `plugins/meme_stealer/`），移植自 AstrBot 插件 `astrbot_plugin_stealer`。用户文档见 README.md。

## 结构与边界

- `main.py`：mohobot 加载的入口，字面名 `Plugin`。只做钩子转发、LLM 工具注册、WebUI 生命周期；不要把业务逻辑放这里。
- `meme_stealer_core/host/`：核心代码接触宿主的唯一边界（事件视图 `StealerEvent`、OneBot 段与组件、`ModelGateway`、`ConfigStore`、loguru 包装）。`core/`、`api/`、`app.py` 不得 import `mohobot.*`。
- `core/`、`api/` 尽量与上游 `astrbot_plugin_stealer` 保持一致（只改导入和框架调用），方便以后同步上游。
- `api/*` 的处理器沿用 Quart 写法（全局 `request`、`jsonify`、`(响应, 状态码)`），由 `web/http.py` 的 Starlette 兼容层提供。

## mohobot 约定（改动前必看）

- 钩子顺序（同一条消息、同一个 task）：`on_perception` → `on_message_observed` → `on_message`（拦截链）→ LLM（工具调用）。工具 handler 拿不到事件，靠 `main.py` 的 `_CURRENT_EVENT` ContextVar；`send_meme` 排队的表情在该 task 结束后（回复已发完）由 done-callback 发出。
- `on_perception` 在群 gate 之前对每条消息调用：选图提示的概率门控只在 `_will_reply` 为真时才评估。mohobot 只在感知文本非空时更新缓存，所以 `_clear_stale_hint` 会清掉残留的提示。
- `on_config_update` 必须是同步函数；插件配置的 `invisible` 字段在面板保存时会被重置为默认值，不要用。面板不渲染 `options`，可选值写在 `hint` 里。
- 被 mohobot 加载时（模块名 `mohobot_plugin_*`）`main.py` 会清掉 `sys.modules` 里的 `meme_stealer_core*`，热重载才能生效；测试里用别的模块名加载就不会清。
- 同一条群消息会经每个 bot 推送一次：偷图/聊天记录按 `(会话, message_id)` 去重；发送冷却和待发表情按 bot 区分（`get_event_bot_session_key`）。
- 嵌入式 uvicorn 不能接管信号、端口冲突时 `sys.exit()` 不能冒泡（见 `web/server.py`）。

## 开发

```bash
python -m pytest tests          # 不需要 mohobot 本体
python -m ruff check --isolated --select E4,E7,E9,F --ignore E402 main.py meme_stealer_core tests
```

- 加配置项：`core/config/config.py` 的 `PluginConfig` 字段 + `_conf_schema.json`；`tests/test_plugin_manifest.py` 会校验两边默认值一致。
- 文件统一 LF 换行（与 mohobot 一致）。
