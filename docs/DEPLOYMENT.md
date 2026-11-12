# Deployment Guide

This guide describes the repository's generic new-install template. It does not describe any live production instance. Before operating an existing deployment, obtain authorization and independently verify its repository, configuration, service manager, process, and data paths.

## Requirements and configuration

- Python 3.12 or newer. The CI workflow currently tests Python 3.12.
- A OneBot v11 implementation that can connect to a reverse WebSocket server.
- Network access to configured LLM and optional service endpoints.

Create a virtual environment, install runtime dependencies, and copy the example configuration:

```bash
git clone https://github.com/CarefreeSongs712/mohobot.git
cd mohobot
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config/global.example.yaml config/global.yaml
```

Edit `config/global.yaml` and configure an available Chat API key (`llm.chat_api_key` or `MOHOBOT_LLM_API_KEY`). Optional services have their own configuration and credentials; see the example file and [DEVELOPMENT.md](DEVELOPMENT.md#53-环境变量).

The example configuration sets the reverse WebSocket server to `0.0.0.0:8081`; the dataclass fallback port is `8060` if a configuration file omits the port. An empty `server.access_token` disables WebSocket token validation for compatibility. On untrusted networks, configure a token and firewall the listener to trusted clients. When set, clients may send `Authorization: Bearer <token>`; query-string tokens are also accepted by the implementation but should be avoided where URLs may be logged.

The main Web panel defaults to `127.0.0.1:9090` and requires a password hash. Set `web_panel.password_hash`, or use `MOHOBOT_WEB_PASSWORD` on first startup to create and persist a hash. Do not expose management panels directly to untrusted networks.

## First connection

Start Mohobot with the repository virtual environment:

```bash
.venv/bin/python main.py
```

Configure the OneBot client to connect to `ws://<host>:<server.port>/ws` (the checked-in example uses port `8081`). A connection is not assigned to a Bot automatically. Create a Bot and bind its QQ account in the Web panel before expecting it to handle messages.

The optional review panel uses a separate configuration and database. Copy [`review/config.example.yaml`](../review/config.example.yaml) to `review/config.yaml`, add reviewer accounts with password hashes, and set `enabled: true` if it should run. Its example listener is loopback port `9091`; review data is separate from the main panel and read-only with respect to message history.

Optional features may need additional dependencies. For example, relationship-graph rendering uses Playwright and Chromium; without them, the feature falls back to text. Follow the current dependency instructions for the selected feature rather than relying on historical pinned browser downloads.

## systemd template

[`deploy/mohobot.service`](../deploy/mohobot.service) is a template for a new installation under `/opt/mohobot` with a dedicated `mohobot` user. It expects `config/global.yaml`, a virtual environment at `.venv`, writable `data/`, `logs/`, and `config/` directories, and an optional `/etc/mohobot/mohobot.env` file. Adapt paths, ownership, environment-file permissions, and network policy before installing it.

A typical new installation prepares the account and virtual environment, installs dependencies, makes the required directories writable by the service account, then installs and enables the unit:

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

Before starting the unit, configure `web_panel.password_hash` in the protected `config/global.yaml`, or create `/etc/mohobot/mohobot.env` with `MOHOBOT_WEB_PASSWORD` and mode `0600`. The unit loads that file before switching to the service account; on first startup the app hashes the value and writes the hash to `config/global.yaml`. The config directory must therefore be writable by the service account and should remain private. Remove the plaintext environment entry after successful initialization. Keep all other credentials out of the repository and shell history.

## Release and maintenance

- Publish only reviewed, CI-verified commits merged to `main`; do not deploy feature branches or an unreviewed working tree.
- Before updating an existing installation, verify its actual branch, clean/dirty state, service unit, working directory, process, configuration, and data location. Do not copy this template over an existing unit without a separate plan.
- Fetch and fast-forward only after authorization and a successful preflight. If the branch diverged, the working tree is dirty, or the service differs from expectations, stop and investigate; do not reset or force-push.
- Restart the verified unit after code updates; the Web panel's restart action reassembles services inside the existing process and does not load newly deployed Python code.
- Verify the unit is active, inspect its current startup logs, and confirm only the expected listeners are bound. Do not assume optional review or plugin services started merely because the main unit did.
- Data migrations, history-layout conversion, backup/restore, and synchronization jobs are separate operations. Read the relevant script, take a verified backup, schedule downtime where required, and obtain explicit authorization before running them.

For code contribution checks use [DEVELOPMENT.md](DEVELOPMENT.md#1-开发环境与日常循环). For repository authorization and data-safety rules see [AGENTS.md](../AGENTS.md).
