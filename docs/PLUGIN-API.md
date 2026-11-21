# 插件 API 与清单

## 目录约定

内置插件放在主仓库 `plugins/`，从市场安装的插件放在 `data/plugins/{name}/`。两处都支持单文件插件与目录插件；目录插件必须包含 `main.py`，入口类字面名为 `Plugin`。

外置插件仓库推荐结构：

```text
mohobot-plugin-demo/
  metadata.yaml
  main.py
  _conf_schema.json     # 可选
  requirements.txt      # 可选
  README.md
  LICENSE
  tests/
```

## metadata.yaml

```yaml
name: demo
author: CarefreeSongs712
display_name: 演示插件
version: 1.0.0
description: 一句话说明
repo: https://github.com/CarefreeSongs712/mohobot-plugin-demo
license: MIT
api_version: 1
mohobot_version: ">=0.1.0"   # 可选，PEP 440
entry: main.py                # 可选，默认 main.py
tags: [demo]                  # 可选
logo: logo.png                # 可选
```

- `name` 匹配 `^[a-z][a-z0-9_]{1,63}$`，安装目录使用该名称。
- `author` 必须是 GitHub 登录名；`plugin_id` 为 `author/name`。
- `api_version` 与宿主 `mohobot.api.api_version` 必须相同。
- `repo` 只接受 `https://github.com/{owner}/{repo}`。
- 已安装插件缺少 `metadata.yaml` 会直接加载失败。

## 导入边界

外置插件只允许导入 `mohobot.api`，不得导入 `mohobot` 其他子模块。`mohobot.api` 当前导出：

- 事件模型：`Event`、`MessageEvent`、`GroupMessageEvent`、`PrivateMessageEvent`、`NoticeEvent`、`MetaEvent`、`RequestEvent`、`MessageSegment`
- CQ 工具：`build_cq_code`、`parse_cq_code`、`extract_plain_text`、`extract_at_targets`
- 文件 I/O：`json_read`、`json_write`、`json_update`、`JSONLWriter`、`ensure_dir`
- 消息归属：`Sender`、`SentMessageClaims`、`message_signature`
- 媒体与文本：`extract_image_urls`
- 封禁：`BanStore`、`UserDataList`、`UserDataModel`
- LLM：`LLMTool`、`registry`、`tool_schema`、`LLMDiagnostic`
- 渲染与时间：`render_info_card`、`render_help_card`、`find_cjk_font`、`format_duration`、`TZ_UTC8`、`now_utc8`、`format_utc8`
- 状态卡颜色：`status_card` 颜色常量与 `usage_color`
- `logger`

## 生命周期钩子

同步钩子按普通函数调用；以下钩子必须声明为 `async def`（加载时校验，错误声明会拒绝加载）：

`on_message`、`on_message_observed`、`on_notice`、`on_meta`、`on_request`、`on_perception`、`on_tick`。

`on_config_update(config)` 必须同步；`interval_sec` 必须是正数，配合 `on_tick` 使用。

## 注入

在插件类上声明以下 `@classmethod`，框架在加载和热重载后调用：

`inject_ws_server`、`inject_bot_manager`、`inject_data_dir`、`inject_plugin_data_dir`、`inject_anysearch_client`、`inject_llm_service`、`inject_emotion_manager`、`inject_context_manager`、`inject_persona_service`、`inject_message_handler`、`inject_task_supervisor`、`inject_song_matcher`、`inject_admin_ids`。

`inject_plugin_data_dir` 传入 `data/plugins_data/{name}/`，插件私有数据写这里。配置仍然通过 `plugin_config` 读取，存档在 `data/plugins_config/{name}.json`。

## 热重载

`PluginSystem.reload_plugins()` 全量重载，`reload_plugin(name)` 只重载一个插件。重载会：

1. 停止该插件周期任务并调用 `on_shutdown`；
2. 从 `sys.modules` 移除插件模块与子模块；
3. 移除插件加入的 `sys.path` 并清理 `__pycache__`；
4. 重新导入、校验钩子、注入引用并启动周期任务。

插件不得在导入阶段启动不可取消的后台任务；需要后台任务时使用注入的 `TaskSupervisor`，owner 使用 `plugins`。
