"""数据管理(备份/恢复/清理)端点测试 — 通过 FastAPI TestClient。

覆盖: 范围选择、密码校验、备份 zip 内容、恢复覆盖、清理删除、zip slip 防护。
"""

import hashlib
import io
import json
import secrets
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def make_pwd_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()
    return f"pbkdf2_sha256${salt}${h}"


def setup():
    from mohobot.web_panel.app import WebPanel

    tmp = Path(tempfile.mkdtemp(prefix="dm_"))
    data_dir = tmp / "data"
    # 测试数据
    h1 = data_dir / "history" / "bot_001" / "private"
    h1.mkdir(parents=True)
    (h1 / "1.jsonl").write_text('{"t": 1}\n', encoding="utf-8")
    c1 = data_dir / "contexts" / "bot_002"
    c1.mkdir(parents=True)
    (c1 / "main.json").write_text('[]', encoding="utf-8")
    cache = data_dir / "cache" / "images"
    cache.mkdir(parents=True)
    (cache / "a.jpg").write_bytes(b"jpegdata")

    cfg_path = tmp / "global.yaml"
    cfg_path.write_text("data_dir: ./data\n", encoding="utf-8")

    panel = WebPanel(
        host="127.0.0.1", port=0, username="admin",
        password_hash=make_pwd_hash("secret123"),
        data_dir=str(data_dir), config_path=str(cfg_path),
    )
    return panel, data_dir


def login(client):
    r = client.post("/api/login", json={"username": "admin", "password": "secret123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_all():
    from fastapi.testclient import TestClient

    panel, data_dir = setup()
    client = TestClient(panel._app)
    auth = login(client)

    # ── 备份 ──
    r = client.post("/api/data/backup", json={"data": {"bots": "all", "dirs": ["history", "contexts", "cache"]}}, headers=auth)
    assert r.status_code == 200, r.text
    content = r.content
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = sorted(zf.namelist())
    print("[1] backup zip entries:", names)
    assert "history/bot_001/private/1.jsonl" in names
    assert "contexts/bot_002/main.json" in names
    assert "cache/images/a.jpg" in names

    # 未选范围 → 400
    r = client.post("/api/data/backup", json={"data": {"bots": "all", "dirs": []}}, headers=auth)
    assert r.status_code == 400

    # 按 bot 过滤备份
    r = client.post("/api/data/backup", json={"data": {"bots": ["bot_001"], "dirs": ["history", "contexts"]}}, headers=auth)
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names2 = zf.namelist()
    assert "history/bot_001/private/1.jsonl" in names2
    assert "contexts/bot_002/main.json" not in names2
    print("[2] scope filtering OK")

    # ── 清理(密码校验) ──
    r = client.post("/api/data/cleanup", json={"data": {"password": "wrong", "scope": {"bots": "all", "dirs": ["cache"]}}}, headers=auth)
    assert r.status_code == 400, "错误密码应被拒绝"
    assert (data_dir / "cache" / "images" / "a.jpg").exists()
    r = client.post("/api/data/cleanup", json={"data": {"password": "secret123", "scope": {"bots": "all", "dirs": ["cache"]}}}, headers=auth)
    assert r.status_code == 200 and r.json()["removed"] == 1
    assert not (data_dir / "cache").exists()
    print("[3] cleanup password check + delete OK")

    # 清理指定 bot 的 history
    r = client.post("/api/data/cleanup", json={"data": {"password": "secret123", "scope": {"bots": ["bot_001"], "dirs": ["history"]}}}, headers=auth)
    assert r.status_code == 200
    assert not (data_dir / "history" / "bot_001").exists()
    assert (data_dir / "contexts" / "bot_002" / "main.json").exists(), "未选 bot 不受影响"
    print("[4] cleanup per-bot OK")

    # ── 恢复 ──
    # 再造数据 → 备份 → 删除 → 恢复
    h1 = data_dir / "history" / "bot_001" / "private"
    h1.mkdir(parents=True)
    (h1 / "1.jsonl").write_text('{"t": 1}\n', encoding="utf-8")
    r = client.post("/api/data/backup", json={"data": {"bots": "all", "dirs": ["history"]}}, headers=auth)
    backup_bytes = r.content
    import shutil
    shutil.rmtree(data_dir / "history")

    r = client.post("/api/data/restore", headers=auth,
                    files={"file": ("b.zip", io.BytesIO(backup_bytes), "application/zip")},
                    data={"password": "wrong", "scope": json.dumps({"bots": "all", "dirs": ["history"]})})
    assert r.status_code == 400, "恢复密码错误应被拒绝"
    r = client.post("/api/data/restore", headers=auth,
                    files={"file": ("b.zip", io.BytesIO(backup_bytes), "application/zip")},
                    data={"password": "secret123", "scope": json.dumps({"bots": "all", "dirs": ["history"]})})
    assert r.status_code == 200, r.text
    assert (data_dir / "history" / "bot_001" / "private" / "1.jsonl").exists()
    print("[5] restore with password OK")

    # ── zip slip 防护 ──
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../evil.txt", "pwned")
    evil.seek(0)
    r = client.post("/api/data/restore", headers=auth,
                    files={"file": ("evil.zip", evil, "application/zip")},
                    data={"password": "secret123", "scope": json.dumps({"bots": "all", "dirs": ["history"]})})
    assert r.status_code == 400, "zip slip 应被拒绝"
    assert not (data_dir.parent / "evil.txt").exists()
    print("[6] zip slip protection OK")

    print("\nALL DATA MANAGEMENT TESTS PASSED")


if __name__ == "__main__":
    test_all()
