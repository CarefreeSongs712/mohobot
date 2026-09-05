"""审核面板(review/)回归测试 — 指纹身份 / 数据加载 join / 审核状态流转 / API 集成。

运行: python tests/_run_all.py (从仓库根)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from review.config import ReviewConfig, ReviewUser, make_config, verify_password  # noqa: E402
from review.hash_password import hash_password  # noqa: E402
from review.loader import (  # noqa: E402
    EXEMPT_ROLES,
    MohobotData,
    fingerprint,
    nickname_of_role,
    parse_session_key,
    session_key,
    user_id_of_role,
)
from review.store import ReviewStore  # noqa: E402


# ── 指纹与身份 ────────────────────────────────────────────────

def test_fingerprint_stable_and_sensitive():
    sk = "bot_001/private/10001/sess_main"
    a = fingerprint(sk, "10001-张三", 1700000000, "你好")
    b = fingerprint(sk, "10001-张三", 1700000000, "你好")
    assert a == b and len(a) == 64
    # 任一字段变化 → 指纹变化
    assert fingerprint(sk, "10001-张三", 1700000001, "你好") != a
    assert fingerprint(sk, "10001-张三", 1700000000, "你好呀") != a
    assert fingerprint(sk + "x", "10001-张三", 1700000000, "你好") != a


def test_role_parsing():
    assert user_id_of_role("1070813311-次瓦音匀") == "1070813311"
    assert user_id_of_role("assistant") == ""
    assert user_id_of_role("summary") == ""
    assert user_id_of_role("user") == ""
    assert user_id_of_role("not-a-number-x") == ""
    assert nickname_of_role("1070813311-次瓦音匀") == "次瓦音匀"
    assert nickname_of_role("no-dash") == "no-dash"
    assert "summary" in EXEMPT_ROLES and "system" in EXEMPT_ROLES


def test_session_key_roundtrip():
    sk = session_key("bot_002", "group", "1005160336", "main")
    assert sk == "bot_002/group/1005160336/main"
    assert parse_session_key(sk) == ("bot_002", "group", "1005160336", "main")


# ── 配置与密码 ────────────────────────────────────────────────

def test_password_hash_roundtrip():
    h = hash_password("s3cret-密码")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password("s3cret-密码", h)
    assert not verify_password("wrong", h)
    assert not verify_password("s3cret-密码", "garbage")


def test_config_example_generated():
    text = make_config()
    assert "users" in text and "9091" in text


# ── 测试数据目录构造 ──────────────────────────────────────────

def _build_fake_data(root: Path) -> None:
    """构造一个迷你 mohobot data 目录: 1 个私聊会话 + history + 图片缓存。"""
    ctx_dir = root / "contexts" / "bot_001" / "private" / "10001"
    ctx_dir.mkdir(parents=True)
    entries = [
        {"role": "10001-张三", "content": "今天天气怎么样", "timestamp": 1700000001},
        {"role": "assistant", "content": "今天晴, 25 度", "timestamp": 1700000010},
        {"role": "10001-张三", "content": "[图片]", "timestamp": 1700000020},
        {"role": "assistant", "content": "这张图是星尘的演唱会海报呢", "timestamp": 1700000030},
        {"role": "summary", "content": "更早的对话总结", "timestamp": 1700000000},
    ]
    (ctx_dir / "sess_main.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    hist_dir = root / "history" / "bot_001" / "private"
    hist_dir.mkdir(parents=True)
    events = [
        {"post_type": "message", "message_type": "private", "time": 1700000001,
         "user_id": "10001", "message_id": "M-100", "message": [{"type": "text", "data": {"text": "今天天气怎么样"}}]},
        {"post_type": "message", "message_type": "private", "time": 1700000020,
         "user_id": "10001", "message_id": "M-102",
         "message": [{"type": "image", "data": {"url": "https://img.example/1.jpg"}}]},
    ]
    with open(hist_dir / "10001.jsonl", "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    cache_dir = root / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "image_cache_map.json").write_text(json.dumps({
        "https://img.example/1.jpg": {
            "path": "images/x.jpg", "phash": "abc",
            "description": "这是星尘演唱会海报", "cached_at": 1.0, "size": 1,
        }
    }, ensure_ascii=False), encoding="utf-8")

    bots_dir = root / "bots" / "bot_001"
    bots_dir.mkdir(parents=True)
    (bots_dir / "config.json").write_text(
        json.dumps({"bot_id": "bot_001", "nickname": "天依", "qq": 111}, ensure_ascii=False),
        encoding="utf-8")


# ── 加载器 ────────────────────────────────────────────────────

def test_loader_scan_join_enrich():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _build_fake_data(root)
        data = MohobotData(root)
        store = ReviewStore(root / "review.db")

        sessions = data.list_sessions()
        assert len(sessions) == 1
        s = sessions[0]
        assert s["bot_id"] == "bot_001" and s["chat_type"] == "private"
        assert s["total"] == 4  # summary 不计入
        assert s["display_name"] == "张三"

        sk = s["session_key"]
        entries = data.enrich_entries(sk, {}, {})
        assert len(entries) == 5
        # join: 用户消息拿到 message_id; 图片拿到 URL 与 VLM 概括
        e0 = entries[0]
        assert e0["message_id"] == "M-100"
        assert e0["status"] == "unreviewed" and e0["kind"] == "user"
        e2 = entries[2]
        assert e2["message_id"] == "M-102"
        assert e2["image_url"] == "https://img.example/1.jpg"
        assert e2["vlm"] == "这是星尘演唱会海报"
        # assistant 无 message_id
        assert entries[1]["message_id"] == "" and entries[1]["kind"] == "assistant"
        # summary 豁免
        assert entries[4]["status"] == "exempt"
        # bot 昵称
        assert data.bot_nicknames() == {"bot_001": "天依"}
        store.close()


def test_loader_cache_invalidation_on_mtime():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _build_fake_data(root)
        data = MohobotData(root)
        path = root / "contexts" / "bot_001" / "private" / "10001" / "sess_main.json"
        assert len(data.load_entries("bot_001/private/10001/sess_main")) == 5
        # 修改文件 + 显式改 mtime → 缓存失效重读
        entries = json.loads(path.read_text(encoding="utf-8"))
        entries.append({"role": "10001-张三", "content": "新消息", "timestamp": 1700001000})
        path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        os.utime(path, (2000000000, 2000000000))
        assert len(data.load_entries("bot_001/private/10001/sess_main")) == 6
        assert data.list_sessions(force=True)[0]["total"] == 5


# ── 审核存储 ──────────────────────────────────────────────────

def test_store_judge_rejudge_skip():
    with tempfile.TemporaryDirectory() as td:
        store = ReviewStore(Path(td) / "review.db")
        sk = "bot_001/private/10001/sess_main"
        fp1, fp2 = "a" * 64, "b" * 64

        assert store.judge(sk, [fp1, fp2], "normal", "alice") == 2
        sts = store.statuses_by_session()[sk]
        assert sts[fp1]["status"] == "normal" and sts[fp1]["reviewer"] == "alice"

        # 同结论重复提交 → 无变化
        assert store.judge(sk, [fp1], "normal", "bob") == 0
        assert store.statuses_by_session()[sk][fp1]["reviewer"] == "alice"
        # 改判
        assert store.judge(sk, [fp1], "abnormal", "bob") == 1
        assert store.statuses_by_session()[sk][fp1]["status"] == "abnormal"
        assert store.statuses_by_session()[sk][fp1]["reviewer"] == "bob"

        # 跳过不改变状态
        store.skip(sk, "bob", "先放一放")
        assert store.statuses_by_session()[sk][fp2]["status"] == "normal"
        assert store.log_count() >= 4  # judge*2 + rejudge + skip
        store.close()


def test_store_abnormal_and_stats():
    with tempfile.TemporaryDirectory() as td:
        store = ReviewStore(Path(td) / "review.db")
        sk = "bot_001/group/20002/main"
        fp = "c" * 64
        store.judge(sk, [fp], "abnormal", "alice")
        rid = store.add_abnormal(sk, fp, "20002-李四", "李四", "违规内容", "M-9",
                                 ["辱骂", "其他"], "骂人", "alice")
        assert rid > 0
        records = store.list_abnormal(bot="bot_001")
        assert len(records) == 1 and records[0]["tags"] == ["辱骂", "其他"]
        assert store.list_abnormal(tag="色情") == []

        assert store.update_abnormal(rid, ["政治"], "补充说明", "bob")
        rec = store.get_abnormal(rid)
        assert rec["tags"] == ["政治"] and rec["note"] == "补充说明"
        assert rec["reviewer"] == "alice"  # 编辑不改归属, 编辑动作留痕于 review_log

        stats = store.reviewer_stats()
        assert stats and stats[0]["reviewer"] == "alice" and stats[0]["judged"] == 1
        assert any(log["action"] == "abnormal_edit" for log in store.recent_log())
        store.close()


# ── 增量审核(含上下文压缩模拟) ───────────────────────────────

def test_incremental_review_with_compaction():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _build_fake_data(root)
        data = MohobotData(root)
        store = ReviewStore(root / "review.db")
        sk = "bot_001/private/10001/sess_main"

        def pending_fps():
            entries = data.enrich_entries(sk, store.statuses_by_session().get(sk, {}),
                                          store.abnormal_by_fingerprint())
            return [e["fingerprint"] for e in entries if e["status"] == "unreviewed"]

        assert len(pending_fps()) == 4
        # 审掉当前全部
        store.judge(sk, pending_fps(), "normal", "alice")
        assert pending_fps() == []

        # 模拟上下文压缩: 最早 2 条替换为 summary 块(压缩后指纹集合变化)
        path = root / "contexts" / "bot_001" / "private" / "10001" / "sess_main.json"
        entries = json.loads(path.read_text(encoding="utf-8"))
        compacted = [{"role": "summary", "content": "总结: 聊了天气和图片", "timestamp": 1700000000}] + entries[2:]
        compacted.append({"role": "10001-张三", "content": "那明天呢", "timestamp": 1700001000})
        compacted.append({"role": "assistant", "content": "明天多云", "timestamp": 1700001010})
        path.write_text(json.dumps(compacted, ensure_ascii=False), encoding="utf-8")
        os.utime(path, (2000000000, 2000000000))

        # 旧消息(压缩后仍在的)已审 → 不重复审; summary 豁免; 只有新轮次待审
        pending = pending_fps()
        assert len(pending) == 2
        store.close()


# ── API 集成 ─────────────────────────────────────────────────

def _make_client(root: Path):
    from fastapi.testclient import TestClient

    from review.app import create_app

    # 写真实配置文件(修改密码功能要写回它)
    cfg_path = root / "config.yaml"
    users = [("admin", hash_password("pw123")), ("bob", hash_password("pw456"))]
    lines = ["users:"]
    for name, h in users:
        lines.append(f"  - username: {name}")
        lines.append(f'    password_hash: "{h}"')
    cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cfg = ReviewConfig(
        users=[ReviewUser(n, h) for n, h in users],
    )
    data = MohobotData(root)
    store = ReviewStore(root / "review.db")
    app = create_app(cfg, data, store, config_path=cfg_path)
    return TestClient(app), store


def _login(client, username, password):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_api_full_flow():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _build_fake_data(root)
        client, store = _make_client(root)
        try:
            _assert_full_flow(client, store)
        finally:
            store.close()


def _assert_full_flow(client, store) -> None:

        # 登录
        assert client.post("/api/login", json={"username": "admin", "password": "bad"}).status_code == 401
        assert client.get("/api/sessions").status_code == 401
        tok = _login(client, "admin", "pw123")
        tok_bob = _login(client, "bob", "pw456")

        # bootstrap / sessions
        boot = client.get("/api/bootstrap", headers=_auth(tok)).json()
        assert boot["bots"][0]["nickname"] == "天依"
        sess = client.get("/api/sessions", headers=_auth(tok)).json()["sessions"]
        assert len(sess) == 1 and sess[0]["unreviewed"] == 4
        sk = sess[0]["session_key"]

        # 明细
        detail = client.get("/api/session/" + sk, headers=_auth(tok)).json()
        assert detail["unreviewed"] == 4 and len(detail["entries"]) == 5
        img_entry = detail["entries"][2]
        assert img_entry["image_url"] and img_entry["vlm"] == "这是星尘演唱会海报"

        # 正常判定(默认全部待审)
        r = client.post("/api/review", headers=_auth(tok),
                        json={"session_key": sk, "action": "normal"}).json()
        assert r["changed"] == 4 and r["remaining_unreviewed"] == 0

        # 改判一条为异常(带标签/备注) — 指定指纹
        fp = detail["entries"][1]["fingerprint"]  # assistant 那条
        r = client.post("/api/review", headers=_auth(tok_bob),
                        json={"session_key": sk, "action": "abnormal",
                              "fingerprints": [fp], "tags": ["辱骂"], "note": "不当言论"}).json()
        assert r["changed"] == 1

        # 异常记录可查/可改
        recs = client.get("/api/abnormal", headers=_auth(tok)).json()["records"]
        assert len(recs) == 1 and recs[0]["tags"] == ["辱骂"]
        rid = recs[0]["id"]
        assert client.put(f"/api/abnormal/{rid}", headers=_auth(tok),
                          json={"tags": ["色情"], "note": "改判"}).status_code == 200
        recs = client.get("/api/abnormal?tag=色情", headers=_auth(tok)).json()["records"]
        assert len(recs) == 1 and recs[0]["note"] == "改判"

        # CSV 导出
        r = client.get("/api/export", headers=_auth(tok))
        assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
        body = r.content.decode("utf-8-sig")
        assert "改判" in body and "色情" in body

        # 跳过只是留痕
        n_before = store.log_count()
        client.post("/api/review", headers=_auth(tok),
                    json={"session_key": sk, "action": "skip"})
        assert store.log_count() == n_before + 1

        # 统计 (judged 按最新结论归属: admin 判 4 条其中 1 条被 bob 改判 → admin 3 / bob 1)
        stats = client.get("/api/stats", headers=_auth(tok)).json()
        assert stats["overall"]["normal"] == 3 and stats["overall"]["abnormal"] == 1
        assert stats["overall"]["unreviewed"] == 0
        reviewers = {r["reviewer"]: r for r in stats["per_reviewer"]}
        assert reviewers["admin"]["judged"] == 3 and reviewers["bob"]["judged"] == 1
        assert reviewers["bob"]["abnormal"] == 1


def test_api_incremental_via_api():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _build_fake_data(root)
        client, store = _make_client(root)
        try:
            tok = _login(client, "admin", "pw123")
            sk = "bot_001/private/10001/sess_main"

            client.post("/api/review", headers=_auth(tok), json={"session_key": sk, "action": "normal"})
            assert client.get("/api/sessions?status=unreviewed", headers=_auth(tok)).json()["sessions"] == []

            # 新增消息(改 mtime 触发缓存失效) → 出现增量待审
            path = root / "contexts" / "bot_001" / "private" / "10001" / "sess_main.json"
            entries = json.loads(path.read_text(encoding="utf-8"))
            entries.append({"role": "10001-张三", "content": "又来了新消息", "timestamp": 1700002000})
            path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
            os.utime(path, (2100000000, 2100000000))

            sess = client.get("/api/sessions?status=unreviewed", headers=_auth(tok)).json()["sessions"]
            assert len(sess) == 1 and sess[0]["unreviewed"] == 1
            detail = client.get("/api/session/" + sk, headers=_auth(tok)).json()
            pending = [e for e in detail["entries"] if e["status"] == "unreviewed"]
            assert len(pending) == 1 and pending[0]["content"] == "又来了新消息"
        finally:
            store.close()


def test_api_change_password():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _build_fake_data(root)
        client, store = _make_client(root)
        try:
            tok = _login(client, "admin", "pw123")
            tok_bob = _login(client, "bob", "pw456")
            cfg_path = root / "config.yaml"

            # 未登录 → 401; 旧密码错 → 400
            assert client.post("/api/password", json={"old_password": "x", "new_password": "yyyyyy"}).status_code == 401
            r = client.post("/api/password", headers=_auth(tok),
                            json={"old_password": "wrong", "new_password": "newpass1"})
            assert r.status_code == 400 and "旧密码" in r.json()["detail"]
            # 新密码太短 / 与旧密码相同
            assert client.post("/api/password", headers=_auth(tok),
                               json={"old_password": "pw123", "new_password": "123"}).status_code == 400
            assert client.post("/api/password", headers=_auth(tok),
                               json={"old_password": "pw123", "new_password": "pw123"}).status_code == 400

            # 正常修改 → 200; 配置文件已写回且保持注释/结构
            r = client.post("/api/password", headers=_auth(tok),
                            json={"old_password": "pw123", "new_password": "brand-new"})
            assert r.status_code == 200, r.text
            text = cfg_path.read_text(encoding="utf-8")
            assert "brand-new" not in text  # 文件里只有哈希
            import yaml as _yaml
            raw = _yaml.safe_load(text)
            hashes = {u["username"]: u["password_hash"] for u in raw["users"]}
            assert verify_password("brand-new", hashes["admin"])
            assert verify_password("pw456", hashes["bob"])  # 别人不受影响

            # 旧密码立即失效, 新密码可登录; 其它用户 token 不受影响
            assert client.post("/api/login", json={"username": "admin", "password": "pw123"}).status_code == 401
            assert _login(client, "admin", "brand-new")
            assert client.get("/api/sessions", headers=_auth(tok_bob)).status_code == 200
        finally:
            store.close()


def test_api_bad_inputs():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _build_fake_data(root)
        client, store = _make_client(root)
        try:
            tok = _login(client, "admin", "pw123")
            assert client.get("/api/session/bot_x/private/1/none", headers=_auth(tok)).status_code == 404
            assert client.post("/api/review", headers=_auth(tok),
                               json={"session_key": "bad/key", "action": "normal"}).status_code == 400
            assert client.post("/api/review", headers=_auth(tok),
                               json={"session_key": "bot_001/private/10001/sess_main",
                                     "action": "wat"}).status_code == 400
        finally:
            store.close()
