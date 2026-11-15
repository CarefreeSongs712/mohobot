# 配置与管理员参考

本文件面向**使用者与管理员**：如何改配置、谁有管理权限、哪些行为可被用户感知、数据如何备份与恢复。
开发视角的模块/扩展/测试说明见 [DEVELOPMENT](DEVELOPMENT.md)；命令清单见 [PLUGINS](PLUGINS.md) 与 `/help`。

## 1. 配置文件

- 模板：`config/global.example.yaml`；本地配置：`config/global.yaml`（gitignore，不入库）。
- 首次启动前至少配置一项可用的 Chat API Key（`llm.chat_api_key`，或环境变量 `MOHOBOT_LLM_API_KEY`）。
- Web 面板密码用 `web_panel.password_hash`，或首次启动时用 `MOHOBOT_WEB_PASSWORD` 生成并持久化哈希。
- 字段的完整定义、默认值与解析顺序以示例文件、`mohobot/models/config.py` 和 [DEVELOPMENT §5](DEVELOPMENT.md#5-配置系统) 为准；本文件不复制全部默认值，避免与代码漂移。

常用配置分组：

| 分组 | 用途 |
|---|---|
| `server` | 反向 WS 监听地址/端口、`access_token` |
| `web_panel` | 管理面板监听地址/端口、密码哈希 |
| `llm` | Chat/视觉模型、提供商、超时、工具开关 |
| `admins` | 全局管理员 QQ（封禁、管理命令、插件共用） |
| `ignore_auto_reply` | 私聊自动回复过滤（见 §3） |
| `llm_excluded_groups` / `external_service_groups` | 群聊链路/外部服务独占群 |
| `snitch_admin_group` | 告警/管理群（snitch 与 bot 离线告警共用） |
| `tts` / `emotion` / `ban` / `knowledge` / `news` / `music_knowledge` | 可选功能，默认多较保守 |

### 热生效与重启

- Web 面板保存全局配置后经 `sync_config` 分发给组件，多数标量立即生效；插件配置保存调用 `on_config_update`。
- 监听端口/`access_token`、面板端口、TTS/情感实例容量等构造期参数需要**重启进程**；面板里的“重启”只是进程内重新装配，不能加载新代码。
- 判断依据：[DEVELOPMENT §5](DEVELOPMENT.md) 与各组件 `sync_config`。

## 2. 管理员与权限

- 权限统一由全局 `admins` 决定；插件可额外配置审批员（如 relationship 的审批流）。
- 常见管理员命令（完整列表以 `/help` 的“管理”分类与 [PLUGINS](PLUGINS.md) 为准）：`/persona …`、`/查看聊天`、`/发送消息`、`/发说说`、`/删说说`、`/重置QQCookies`、`/群列表`、`/好友列表`、`/退群`、`/删好友`、`/加审批员`、`/减审批员`、`/重置记录` 等。
- 面板登录独立于 QQ 管理员身份；不要把管理员面板暴露到不可信网络。

## 3. 私聊自动回复过滤

`ignore_auto_reply` 默认开启，用于过滤其他 bot/协议端产生的自动回复；命中后**仍先归档 history**，但不进入插件、命令与 LLM 链路。当前规则（实现常量见 `mohobot/message_handler.py`，回归见 `tests/test_auto_reply.py`）：

- 文本以 `[自动回复]` 开头；或
- 同一 bot + 同一用户连续 **3 条**相同文本，且间隔在 **300 秒**内的重复。

需要保留自动回复时关闭该开关；阈值属于用户可感知行为，改动需同时更新实现、测试与本文件。

## 4. 数据、备份与恢复边界

数据目录结构见 [README](../README.md#数据与目录速览)；分层语义（history 追加归档 / contexts 工作记忆 / SQLite 完成轮次）见 [DEVELOPMENT §4](DEVELOPMENT.md)。

备份/恢复时需要注意：

- **群聊 history 是跨 bot 合并归档**：按 bot 过滤备份不会把群数据拆开；恢复/迁移以合并布局为准，不要恢复旧的 per-bot 双写布局。
- **`data/persona_ab/` 不包含在聊天备份 ZIP 中**：它是独立 JSON，当前首版**不支持恢复导入**，需要单独备份。
- **歌曲索引缓存**（`data/cache/song_index`）默认跳过备份，恢复时保留现有缓存；删除该目录等价于下次强制重建。
- 审核面板使用独立数据库，只读 history；备份/恢复主程序数据不等于备份审核数据。
- 数据迁移、history 布局转换、同步与清理都是**单独操作**：先停机、备份、读脚本、再执行；相关脚本多数需要显式授权（见 [DEPLOYMENT](DEPLOYMENT.md) 与 [AGENTS](../AGENTS.md)）。
- 配置、`data/`、`logs/` 与凭据不入库；备份文件本身也应放在受控位置。

## 5. 面板与网络边界

- 管理与审核面板默认只监听回环地址，必须显式配置密码；远程访问使用 HTTPS 反向代理、VPN 或白名单。
- 反向 WS 面向不可信网络时应配置 `server.access_token` 并限制来源；`access_token`、面板密码、API Key 不要写入仓库或聊天记录。
- 漏洞报告方式见 [SECURITY](../SECURITY.md)；部署与 systemd 模板见 [DEPLOYMENT](DEPLOYMENT.md)。
