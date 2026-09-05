"""审核面板配置加载 — review/config.yaml。

字段:
  enabled: true            # mohobot 拉起钩子据此决定是否启动
  server.host / server.port
  data_dir: "data"         # mohobot 数据目录(只读), 相对于仓库根(即本文件上级目录)
  users:                   # 手工维护, 密码存 PBKDF2 哈希(review/hash_password.py 生成)
    - username: admin
      password_hash: "pbkdf2_sha256$<salt>$<hex>"
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# 与主面板一致的哈希格式: pbkdf2_sha256$salt$hex (100000 轮)
PBKDF2_ITERATIONS = 100000


@dataclass
class ReviewUser:
    username: str
    password_hash: str


@dataclass
class ReviewConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 9091
    data_dir: str = "data"
    token_expiry: int = 3600
    users: list[ReviewUser] = field(default_factory=list)

    def resolve_data_dir(self, base_dir: Path) -> Path:
        """把 data_dir 解析为绝对路径(相对于 base_dir, 默认仓库根)。"""
        p = Path(self.data_dir)
        if not p.is_absolute():
            p = base_dir / p
        return p.resolve()


def load_config(path: str | Path) -> ReviewConfig:
    """读取 config.yaml; 文件缺失时返回默认值(无用户 → 无法登录)。"""
    cfg = ReviewConfig()
    path = Path(path)
    if not path.exists():
        return cfg

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    server = raw.get("server") or {}
    cfg.enabled = bool(raw.get("enabled", True))
    cfg.host = str(server.get("host", "127.0.0.1"))
    cfg.port = int(server.get("port", 9091))
    cfg.data_dir = str(raw.get("data_dir", "../data"))
    cfg.token_expiry = int(raw.get("token_expiry", 3600))

    users_raw = raw.get("users") or []
    for u in users_raw:
        if not isinstance(u, dict):
            continue
        username = str(u.get("username", "")).strip()
        password_hash = str(u.get("password_hash", "")).strip()
        if username and password_hash:
            cfg.users.append(ReviewUser(username, password_hash))
    return cfg


def verify_password(password: str, stored: str) -> bool:
    """校验 pbkdf2_sha256$salt$hex 格式哈希(与主面板同构)。"""
    import hashlib
    import hmac

    parts = stored.split("$")
    if len(parts) != 3 or parts[0] != "pbkdf2_sha256":
        return False
    _algo, salt, digest = parts
    try:
        calc = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
        ).hex()
    except Exception:
        return False
    return hmac.compare_digest(calc, digest)


def make_config(data_dir: str = "data") -> str:
    """生成示例配置文本(hash_password.py 生成哈希后替换)。"""
    return yaml.dump(
        {
            "enabled": True,
            "server": {"host": "127.0.0.1", "port": 9091},
            "data_dir": data_dir,
            "token_expiry": 3600,
            "users": [{"username": "admin", "password_hash": "pbkdf2_sha256$<salt>$<hex>"}],
        },
        allow_unicode=True,
        sort_keys=False,
    )


if __name__ == "__main__":
    # python review/config.py  → 打印示例配置
    sys.stdout.write(make_config())
