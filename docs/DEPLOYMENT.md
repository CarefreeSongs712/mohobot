# 部署指南

本文是仓库的**通用新环境部署模板**，不描述任何线上实例。操作既有部署前必须取得授权，并独立核实其仓库、配置、服务管理器、进程与数据路径；不要把本文当作某个环境的实时状态。

## 环境与配置

- Python 3.12 或更新版本；CI 当前只测试 Python 3.12。
- 一个能连接反向 WebSocket 服务端的 OneBot v11 实现。
- 能访问所配置的 LLM 与可选服务端点。

创建虚拟环境、安装依赖并复制示例配置：

```bash
git clone https://github.com/CarefreeSongs712/mohobot.git
cd mohobot
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config/global.example.yaml config/global.yaml
```

编辑 `config/global.yaml`，配置可用的 Chat API Key（`llm.chat_api_key`，或环境变量 `MOHOBOT_LLM_API_KEY`）。可选服务各有自己的配置与凭据；完整字段说明见示例文件与 [DEVELOPMENT §5](DEVELOPMENT.md#5-配置系统)，面向使用者的配置/权限/备份说明见 [配置与管理员参考](ADMIN.md)。

示例配置把反向 WebSocket 服务端设为 `0.0.0.0:8081`；若配置文件省略端口，数据类回退值是 `8060`。`server.access_token` 为空时不校验 WebSocket token（仅为兼容）；面向不可信网络时应配置 token 并限制监听来源。设置后客户端可发送 `Authorization: Bearer <token>`；实现也接受查询串 token，但 URL 可能被日志记录，应避免。

管理面板默认监听 `127.0.0.1:9090`，必须配置密码哈希：设置 `web_panel.password_hash`，或首次启动时用 `MOHOBOT_WEB_PASSWORD` 生成并持久化哈希。不要把管理面板直接暴露到不可信网络。

## 首次连接

启动：

```bash
.venv/bin/python main.py
```

在 OneBot 客户端配置连接 `ws://<主机>:<server.port>/ws`（示例使用 `8081`）。新连接不会自动分配 Bot：需要先在 Web 面板创建 Bot 并绑定 QQ，之后才会处理该连接的消息。

审核面板是可选组件，使用独立配置与数据库：复制 [`review/config.example.yaml`](../review/config.example.yaml) 为 `review/config.yaml`，添加带密码哈希的审核账号，并在需要时启用；示例监听回环 `9091`。审核数据独立于主面板，对消息归档只读。

可选功能可能需要额外依赖，例如关系图渲染使用 Playwright 与 Chromium，缺失时自动降级为文本；按所选功能的当前依赖说明安装，不要依赖历史固定版本。

## systemd 模板

[`deploy/mohobot.service`](../deploy/mohobot.service) 是新装模板，假设使用专用 `mohobot` 用户、部署在 `/opt/mohobot`，需要 `config/global.yaml`、`.venv` 以及可写的 `data/`、`logs/`、`config/`，并可选加载 `/etc/mohobot/mohobot.env`。安装前按实际环境调整路径、属主、环境文件权限与网络策略。

典型的新装流程：创建账号与虚拟环境、安装依赖、准备目录并赋予服务账号写权限，然后安装并启用 unit：

```bash
sudo useradd --system --home /opt/mohobot --shell /usr/sbin/nologin mohobot
sudo install -d -o mohobot -g mohobot /opt/mohobot/data /opt/mohobot/logs /opt/mohobot/config
sudo cp /opt/mohobot/config/global.example.yaml /opt/mohobot/config/global.yaml
sudo chown mohobot:mohobot /opt/mohobot/config/global.yaml
sudo chmod 600 /opt/mohobot/config/global.yaml
sudo python3.12 -m venv /opt/mohobot/.venv
sudo /opt/mohobot/.venv/bin/pip install -r /opt/mohobot/requirements.txt
sudo install -d -m 700 /etc/mohobot
sudo install -m 644 /opt/mohobot/deploy/mohobot.service /etc/systemd/system/mohobot.service
sudo systemctl daemon-reload
sudo systemctl enable --now mohobot
```

启动 unit 前，在受保护的 `config/global.yaml` 配置 `web_panel.password_hash`，或创建权限 `0600` 的 `/etc/mohobot/mohobot.env` 并写入 `MOHOBOT_WEB_PASSWORD`。unit 会在切换服务账号前加载该文件；应用首次启动会哈希该值并写回 `config/global.yaml`，因此配置目录必须对服务账号可写并保持私密。初始化成功后删除环境文件中的明文密码；其他凭据不要写进仓库或 shell 历史。

## 数据备份与恢复

- 数据目录与分层语义见 [DEVELOPMENT §4](DEVELOPMENT.md)；备份边界见 [配置与管理员参考 §4](ADMIN.md#4-数据备份与恢复边界)。
- 对运行中的实例，优先使用已验证的备份工具/脚本；直接复制 `data/` 前应确认一致性（history 追加写入、contexts/SQLite 正在更新）。
- **备份与恢复是独立操作**：先读脚本、停机（如需要）、做已验证备份，并取得明确授权；不要在未确认路径与范围时执行迁移、清理或恢复。
- 群聊归档为跨 bot 合并布局，按 bot 过滤不会拆分群数据；`data/persona_ab/` 与审核数据库需要单独备份。
- 歌曲索引缓存默认跳过备份，恢复时保留现有缓存（删除目录等价于下次重建）。

## 发布与维护

- 只发布经审查、CI 通过并合入 `main` 的提交；不要部署功能分支或未审查的工作区。
- 更新既有安装前，核实实际分支、工作区是否干净、systemd unit、工作目录、进程与数据位置；没有单独方案时不要把本模板覆盖到既有 unit 上。
- 取得授权并完成只读预检后再 fetch 与 fast-forward；若分支分叉、工作区有改动或服务与预期不符，立即停止排查，不要 reset 或 force push。
- 代码更新后重启已核实的 unit；面板内的“重启”只是进程内重新装配，不会加载新代码。
- 验收 unit 为 active、检查本次启动日志、确认只监听预期端口；不要因为主 unit 起来了就假设审核/插件服务也已启动。
- 数据迁移、history 布局转换、备份/恢复与同步任务是独立操作：先读相关脚本、做已验证备份、按需安排停机，并取得明确授权后再执行。

开发侧检查入口见 [DEVELOPMENT §1](DEVELOPMENT.md#1-开发环境与日常循环)；仓库授权与数据安全底线见 [AGENTS](../AGENTS.md)；漏洞报告见 [SECURITY](../SECURITY.md)。
