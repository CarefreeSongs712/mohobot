# beszel_servers —— Beszel 多服务器状态插件

`/servers`（别名 `/服务器`）读取 Beszel Hub 的 `systems` 集合，生成与 `/status` 同风格的图卡：状态点、CPU/内存/磁盘占用、负载、运行时长与 agent 版本；图卡渲染或发送失败时降级为文字。群内无需 @，按 `global_triggers` 多 bot 去重，**全体用户可用**（与 `/status` 一致）。

## 前置条件

- 已有一个 Beszel Hub，且各服务器已运行 agent 并向该 Hub 上报；
- 一个能登录 Hub 并读取 `systems` 集合的 Beszel 账号；
- 插件所在机器能访问 Hub 地址（默认 Hub 只监听本机时，通过 SSH 隧道或反向代理暴露；不要把 Hub 直接裸奔到公网）。

## 配置

在 Web 面板「插件管理 → beszel_servers → 配置」填写：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `base_url` | `http://127.0.0.1:8090` | 插件所在机器可访问的 Hub 地址；必须是 `http(s)://主机[:端口][/路径]`，不能带用户名/密码、查询串或片段 |
| `email` | 空 | Beszel 登录邮箱 |
| `password` | 空 | Beszel 密码；**明文保存在 `data/plugins_config/beszel_servers.json`**，主面板不会掩码插件配置；建议手动把该文件权限设为 `600`，插件保存配置不会自动改权限 |
| `timeout_secs` | `8` | 单次请求超时（秒），最小 1 |
| `max_systems` | `12` | 卡片最多展示的服务器数；超出只提示剩余台数 |

三项凭据未填齐时命令返回“Beszel 未配置”，不会发起请求。保存配置即时热生效。

## 命令

- `/servers`、`/服务器`：查询并发送状态卡片。

只展示名称、状态、CPU/内存/磁盘、负载、运行时长和 agent 版本；不展示 `host`/`port`、用户列表或登录 token。

## 认证与配置切换

- 使用 Hub 的 PocketBase 用户登录接口 `POST /api/collections/users/auth-with-password` 换取 token；
- token 按 JWT 过期时间缓存；查询返回 401/403 时清缓存并只重试一次；同一次配置内并发 401/403 只刷新一次 token；
- 每次查询固定一份配置快照（Hub、账号、超时）。**修改配置后，新查询才使用新快照**；已在途的旧查询只用旧快照完成，不会把旧账号 token 发往新 Hub；退役 client 在下次请求结束或 shutdown 时关闭；
- 同一配置 generation 内的并发 401/403 只刷新一次 token；401/403 连续失败不会无限重试。
- 登录/查询超时、认证失败都会转换成文字提示，不抛出到消息主链。

## 故障排查

| 现象 | 处理 |
|---|---|
| `Beszel 未配置` | 检查 Hub 地址、邮箱、密码是否都已填写 |
| `登录失败（邮箱或密码错误）` | 用浏览器登录 Hub 核对账号；注意 Beszel 用户与 agent 不是同一凭据 |
| `无法连接 Hub` / `连接超时` | 从插件所在机器 `curl <base_url>/api/health`；检查端口、隧道/代理和防火墙 |
| `Beszel 里还没有已接入的服务器` | 在 Hub 界面确认 agent 已上线，且当前账号有权限读取 `systems` |
| 卡片超过预期 | 调小 `max_systems`；只取前 200 条 systems 并按名称排序展示 |
| 图卡发送失败 | 会自动降级文字；检查协议端图片发送能力与磁盘空间 |

## 权限与安全

- 命令对全体用户开放，等同于公开“全局服务器资源/在线状态”；不希望普通群成员看到时，应改为管理员命令（修改 `on_message` 的权限判断）或停用插件。
- 凭据由主面板登录保护；不要在群聊、日志或 issue 中贴 Hub 地址、账号或密码。
- 插件当前不缓存 `systems` 数据，每次命令都会访问 Hub；Hub 较慢或调用频繁时应降低使用频率，后续可加短 TTL 缓存。

## 测试

```bash
.venv/bin/python -m pytest -q tests/test_beszel_servers_plugin.py
```

## 已知限制

- 只读取 Hub 的第一页 200 条 systems，超过部分不展示；`max_systems` 上限为 50。
- 非有限指标（NaN/Infinity）显示为 `--`；离线实例不展示最后一次上报的 CPU/内存/磁盘值。
- 更新时间显示为 `MM-DD HH:MM`（UTC+8），不显示年份。
- 退役 HTTP 客户端在请求 release 或 shutdown 时关闭；配置切换后若不再有查询，旧 client 会保留到 shutdown。
