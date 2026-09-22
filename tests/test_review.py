"""审核面板(review/)回归测试 — history 数据源 / 群聊过滤 / message_id 身份 / 审核状态流转 / API 集成。

运行: python tests/_run_all.py (从仓库根)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from review.config import (  # noqa: E402
    ReviewConfig,
    ReviewUser,
    load_config,
    make_config,
    verify_password,
)
from review.hash_password import hash_password  # noqa: E402
from review.loader import (  # noqa: E402
    MohobotData,
    content_fingerprint,
    entry_identity,
    parse_session_key,
    session_key,
)
from review.store import ReviewStore  # noqa: E402


# ── 会话键与身份 ──────────────────────────────────────────────

def test_session_key_roundtrip():
    sk = session_key("bot_002", "group", "1005160336")
    assert sk == "bot_002/group/1005160336"
    assert parse_session_key(sk) == ("bot_002", "group", "1005160336")
    try:
        parse_session_key("a/b/c/d")
        assert False, "4 段的旧格式应报错"
    except ValueError:
        pass


def test_identity_prefers_message_id():
    assert entry_identity("M-1", "hash:xyz") == "mid:M-1"
    assert entry_identity("", "hash:xyz") == "hash:xyz"


def test_content_fingerprint_stable_and_sensitive():
    a = content_fingerprint("bot/p/c", "user", 1, "hi")
    assert a == content_fingerprint("bot/p/c", "user", 1, "hi")
    assert a != content_fingerprint("bot/p/c", "user", 2, "hi")
    assert a != content_fingerprint("bot/p/c", "user", 1, "hi!")
    assert a.startswith("hash:")


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


def test_config_missing_data_dir_defaults_to_repo_data():
    """省略 data_dir 时必须回退到 data(而非 ../data — 否则生产会静默指错目录)。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "c.yaml"
        p.write_text('users:\n  - username: a\n    password_hash: "pbkdf2_sha256$x$y"\n',
                     encoding="utf-8")
        cfg = load_config(p)
        assert cfg.data_dir == "data"


# ── 测试数据目录构造 ──────────────────────────────────────────

def _write_jsonl(path: Path, events: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _private_events() -> list[dict]:
    return [
        {"post_type": "message", "message_type": "private", "time": 1700000001,
         "self_id": 111, "user_id": 10001, "message_id": "M-100",
         "sender": {"user_id": 10001, "nickname": "张三"},
         "message": [{"type": "text", "data": {"text": "今天天气怎么样"}}]},
        {"post_type": "message_sent", "message_type": "private", "time": 1700000010,
         "self_id": 111, "user_id": 111, "message_id": "MB-1",
         "sender": {"user_id": 111, "nickname": "天依"},
         "message": [{"type": "text", "data": {"text": "今天晴, 25 度"}}]},
        {"post_type": "message", "message_type": "private", "time": 1700000020,
         "self_id": 111, "user_id": 10001, "message_id": "M-102",
         "sender": {"user_id": 10001, "nickname": "张三"},
         "message": [{"type": "image", "data": {"url": "https://img.example/1.jpg"}}]},
        {"post_type": "message_sent", "message_type": "private", "time": 1700000030,
         "self_id": 111, "user_id": 111, "message_id": "",
         "sender": {"user_id": 111, "nickname": "天依"},
         "message": [{"type": "text", "data": {"text": "无 id 的 bot 发言"}}]},
    ]


def _group_events() -> list[dict]:
    """bot_001(qq=111) 的群 20002 归档。"""
    return [
        {"post_type": "message_sent", "message_type": "group", "time": 1700000100,
         "self_id": 111, "group_id": 20002, "message_id": "GB-1",
         "sender": {"user_id": 111, "nickname": "天依"},
         "message": [{"type": "text", "data": {"text": "大家好"}}]},
        # 无关群消息 → 过滤
        {"post_type": "message", "message_type": "group", "time": 1700000101,
         "self_id": 111, "group_id": 20002, "user_id": 10001, "message_id": "M-101",
         "sender": {"card": "张三", "user_id": 10001},
         "message": [{"type": "text", "data": {"text": "群里闲聊"}}]},
        # @ 别的 bot → 过滤
        {"post_type": "message", "message_type": "group", "time": 1700000102,
         "self_id": 111, "group_id": 20002, "user_id": 10001, "message_id": "M-102",
         "sender": {"card": "张三", "user_id": 10001},
         "message": [{"type": "at", "data": {"qq": "999"}},
                     {"type": "text", "data": {"text": "@别人"}}]},
        # @ 本 bot → 保留
        {"post_type": "message", "message_type": "group", "time": 1700000103,
         "self_id": 111, "group_id": 20002, "user_id": 10001, "message_id": "M-103",
         "sender": {"card": "张三", "user_id": 10001},
         "message": [{"type": "at", "data": {"qq": "111"}},
                     {"type": "text", "data": {"text": "@bot 你好"}}]},
        # 引用 bot 发言 → 保留
        {"post_type": "message", "message_type": "group", "time": 1700000104,
         "self_id": 111, "group_id": 20002, "user_id": 10001, "message_id": "M-104",
         "sender": {"card": "张三", "user_id": 10001},
         "message": [{"type": "reply", "data": {"id": "GB-1"}},
                     {"type": "text", "data": {"text": "引用回复"}}]},
        # 同一条消息重复推送 → 去重
        {"post_type": "message", "message_type": "group", "time": 1700000104,
         "self_id": 111, "group_id": 20002, "user_id": 10001, "message_id": "M-104",
         "sender": {"card": "张三", "user_id": 10001},
         "message": [{"type": "text", "data": {"text": "重复推送"}}]},
        # 同时 @ 两个 bot → 两个归档各自命中, 合并后跨 bot 去重
        {"post_type": "message", "message_type": "group", "time": 1700000110,
         "self_id": 111, "group_id": 20002, "user_id": 10001, "message_id": "M-106",
         "sender": {"card": "张三", "user_id": 10001},
         "message": [{"type": "at", "data": {"qq": "111"}},
                     {"type": "at", "data": {"qq": "222"}},
                     {"type": "text", "data": {"text": "都在吧"}}]},
    ]


def _group_events_bot2() -> list[dict]:
    """bot_002(qq=222) 的同一群 20002 归档 — 合并会话的另一半。"""
    return [
        {"post_type": "message_sent", "message_type": "group", "time": 1700000107,
         "self_id": 222, "group_id": 20002, "message_id": "GB-B1",
         "sender": {"user_id": 222, "nickname": "天依beta"},
         "message": [{"type": "text", "data": {"text": "我是beta"}}]},
        # @ 别的 bot → 在 bot_002 视角过滤掉
        {"post_type": "message", "message_type": "group", "time": 1700000108,
         "self_id": 222, "group_id": 20002, "user_id": 10001, "message_id": "M-103",
         "sender": {"card": "张三", "user_id": 10001},
         "message": [{"type": "at", "data": {"qq": "111"}},
                     {"type": "text", "data": {"text": "@bot 你好"}}]},
        # @ 本 bot → 保留
        {"post_type": "message", "message_type": "group", "time": 1700000105,
         "self_id": 222, "group_id": 20002, "user_id": 10001, "message_id": "M-105",
         "sender": {"card": "张三", "user_id": 10001},
         "message": [{"type": "at", "data": {"qq": "222"}},
                     {"type": "text", "data": {"text": "@beta 你好"}}]},
        # 与 bot_001 归档同一条多 @ 消息 → 合并去重
        {"post_type": "message", "message_type": "group", "time": 1700000110,
         "self_id": 222, "group_id": 20002, "user_id": 10001, "message_id": "M-106",
         "sender": {"card": "张三", "user_id": 10001},
         "message": [{"type": "at", "data": {"qq": "111"}},
                     {"type": "at", "data": {"qq": "222"}},
                     {"type": "text", "data": {"text": "都在吧"}}]},
    ]


def _build_fake_data(root: Path) -> None:
    """构造迷你 mohobot data: 2 个 bot + 私聊/群聊 history + 图片 VLM 缓存。"""
    for bid, nick, qq in (("bot_001", "天依", 111), ("bot_002", "天依beta", 222)):
        bots = root / "bots" / bid
        bots.mkdir(parents=True)
        (bots / "config.json").write_text(json.dumps(
            {"bot_id": bid, "nickname": nick, "qq": qq}, ensure_ascii=False),
            encoding="utf-8")

    priv = root / "history" / "bot_001" / "private"
    priv.mkdir(parents=True)
    _write_jsonl(priv / "10001.jsonl", _private_events())

    grp1 = root / "history" / "bot_001" / "group"
    grp1.mkdir(parents=True)
    _write_jsonl(grp1 / "20002.jsonl", _group_events())
    grp2 = root / "history" / "bot_002" / "group"
    grp2.mkdir(parents=True)
    _write_jsonl(grp2 / "20002.jsonl", _group_events_bot2())

    cache_dir = root / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "image_cache_map.json").write_text(json.dumps({
        "https://img.example/1.jpg": {
            "path": "images/x.jpg", "phash": "abc",
            "description": "这是星尘演唱会海报", "cached_at": 1.0, "size": 1,
        }
    }, ensure_ascii=False), encoding="utf-8")


# ── 加载器 ────────────────────────────────────────────────────

def test_loader_scan_and_group_filter():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _build_fake_data(root)
        data = MohobotData(root)
        store = ReviewStore(root / "review.db")

        by_key = {s["session_key"]: s for s in data.list_sessions()}
        assert set(by_key) == {"bot_001/private/10001", "_merged/group/20002"}

        priv = by_key["bot_001/private/10001"]
        grp = by_key["_merged/group/20002"]
        assert priv["total"] == 4 and priv["display_name"] == "张三"
        # 群聊合并会话: bot_001 4 条 + bot_002 2 条, 多 @ 消息跨 bot 去重后 6 条
        assert grp["total"] == 6
        assert grp["bots"] == ["bot_001", "bot_002"]
        assert grp["display_name"] == "群 20002"

        # 私聊明细
        entries = data.enrich_entries(priv["session_key"], {}, {})
        assert [e["kind"] for e in entries] == ["user", "assistant", "user", "assistant"]
        assert entries[0]["message_id"] == "M-100" and entries[0]["speaker"] == "张三"
        assert entries[0]["fingerprint"] == "mid:M-100"
        assert entries[1]["fingerprint"] == "mid:MB-1" and entries[1]["speaker"] == ""
        img = entries[2]
        assert img["image_url"] == "https://img.example/1.jpg"
        assert img["vlm"] == "这是星尘演唱会海报"
        # 无 message_id 的条目 → 内容指纹兜底
        assert entries[3]["fingerprint"].startswith("hash:")

        # 群聊明细(按时间混流, 已去重; bot 发言按条标注归属)
        gentries = data.enrich_entries(grp["session_key"], {}, {})
        assert [e["content"] for e in gentries] == [
            "大家好", "@bot 你好", "引用回复", "@beta 你好", "我是beta", "都在吧",
        ]
        assert gentries[0]["kind"] == "assistant" and gentries[1]["kind"] == "user"
        # bot 发言逐条归属: bot_001 天依 / bot_002 天依beta
        assert gentries[0]["bot_id"] == "bot_001" and gentries[0]["bot_nickname"] == "天依"
        assert gentries[4]["bot_id"] == "bot_002" and gentries[4]["bot_nickname"] == "天依beta"
        # 用户消息也带来源归档
        assert gentries[1]["bot_id"] == "bot_001"
        assert gentries[3]["bot_id"] == "bot_002"

        assert data.bot_nicknames() == {"bot_001": "天依", "bot_002": "天依beta"}
        assert data.bot_self_id("bot_001") == "111"
        store.close()


def test_loader_dedup_repushed_same_mid():
    """同一 message_id 重复推送(如好友请求隔天重发, 时间不同) → 只留首条,
    列表 total 与明细身份数一致(否则出现"列表显示待审、点进去全审完"的死审核)。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _build_fake_data(root)
        path = root / "history" / "bot_001" / "private" / "10001.jsonl"
        repush = {"post_type": "message", "message_type": "private", "time": 1700005000,
                  "self_id": 111, "user_id": 10001, "message_id": "M-100",
                  "sender": {"user_id": 10001, "nickname": "张三"},
                  "message": [{"type": "text", "data": {"text": "请求添加你为好友"}}]}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(repush, ensure_ascii=False) + "\n")
        os.utime(path, (2200000000, 2200000000))
        data = MohobotData(root)
        rows = data.filtered_rows("bot_001/private/10001")
        assert len(rows) == 4, f"重推同 mid 不应新增行: {len(rows)}"
        assert sum(1 for r in rows if r["mid"] == "M-100") == 1
        sessions = {s["session_key"]: s for s in data.list_sessions(force=True)}
        assert sessions["bot_001/private/10001"]["total"] == 4
        # 逐条增量追加的重推同样被去重
        data2 = MohobotData(root, cache_path=root / "c.json")
        assert data2.filtered_rows("bot_001/private/10001") == rows
        data2.flush_cache()


def test_loader_incremental_append_and_rewrite():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _build_fake_data(root)
        data = MohobotData(root)
        sk = "bot_001/private/10001"
        path = root / "history" / "bot_001" / "private" / "10001.jsonl"
        assert len(data.filtered_rows(sk)) == 4

        # 追加一行(改 mtime) → 只解析新增内容, 旧行保留
        append = {"post_type": "message", "message_type": "private", "time": 1700001000,
                  "self_id": 111, "user_id": 10001, "message_id": "M-200",
                  "sender": {"user_id": 10001, "nickname": "张三"},
                  "message": [{"type": "text", "data": {"text": "新消息"}}]}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(append, ensure_ascii=False) + "\n")
        os.utime(path, (2000000000, 2000000000))
        rows = data.filtered_rows(sk)
        assert len(rows) == 5 and rows[-1]["text"] == "新消息"

        # 文件被整体重写(仅 1 行) → 全量重读, 旧行不残留
        _write_jsonl(path, [_private_events()[0]])
        os.utime(path, (2100000000, 2100000000))
        rows = data.filtered_rows(sk)
        assert len(rows) == 1 and rows[0]["mid"] == "M-100"
        assert data.list_sessions(force=True)[0]["total"] == 1


def test_loader_sidecar_cache_persistence():
    """sidecar 持久化: 重启(新实例)直接加载缓存, 不再冷解析; 损坏缓存安全降级。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _build_fake_data(root)
        cache = root / "sidecar" / "loader_cache.json"
        sk = "bot_001/private/10001"

        # 第一次: 正常解析 + flush 落盘
        data1 = MohobotData(root, cache_path=cache)
        rows1 = data1.filtered_rows(sk)
        assert len(rows1) == 4
        data1.flush_cache()
        assert cache.exists()
        payload = json.loads(cache.read_text(encoding="utf-8"))
        assert payload["version"] == MohobotData._CACHE_VERSION
        assert any("history" in k for k in payload["files"])

        # 第二次(模拟重启): 新实例直接加载缓存, 结果一致
        data2 = MohobotData(root, cache_path=cache)
        rows2 = data2.filtered_rows(sk)
        assert len(rows2) == len(rows1)
        assert [r["mid"] for r in rows2] == [r["mid"] for r in rows1]
        # group 合并会话也来自缓存(含 bot_mids 与 bot 归属, 过滤仍生效)
        grow = data2.filtered_rows("_merged/group/20002")
        assert len(grow) == 6

        # 损坏的缓存 → 忽略并从磁盘重新解析, 结果不变
        cache.write_text("{corrupted", encoding="utf-8")
        data3 = MohobotData(root, cache_path=cache)
        assert len(data3.filtered_rows(sk)) == len(rows1)
        data3.flush_cache()
        assert json.loads(cache.read_text(encoding="utf-8"))["version"] == MohobotData._CACHE_VERSION


# ── 审核存储(身份换成 message_id, 语义不变) ──────────────────

def test_store_judge_rejudge_skip():
    with tempfile.TemporaryDirectory() as td:
        store = ReviewStore(Path(td) / "review.db")
        sk = "bot_001/private/10001"
        fp1, fp2 = "mid:M-1", "mid:M-2"

        assert store.judge(sk, [fp1, fp2], "normal", "alice") == 2
        sts = store.statuses_by_session()[sk]
        assert sts[fp1]["status"] == "normal" and sts[fp1]["reviewer"] == "alice"

        # 同结论重复提交 → 无变化
        assert store.judge(sk, [fp1], "normal", "bob") == 0
        assert store.statuses_by_session()[sk][fp1]["reviewer"] == "alice"
        # 改判
        assert store.judge(sk, [fp1], "abnormal", "bob") == 1
        assert store.statuses_by_session()[sk][fp1]["status"] == "abnormal"

        # 跳过不改变状态
        store.skip(sk, "bob", "先放一放")
        assert store.statuses_by_session()[sk][fp2]["status"] == "normal"
        assert store.log_count() >= 4
        store.close()


def test_store_statuses_cache_and_external_write():
    """状态缓存: 命中时不重建; 本地写入增量补丁立即可见; 外部写入(库戳记变化)自动重建。"""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "review.db"
        store = ReviewStore(db)
        sk = "bot_001/private/10001"

        first = store.statuses_by_session()
        assert first == {} and store.counts_by_session() == {}
        assert store.statuses_by_session() is first, "无变化时应命中同一缓存对象"

        # 本地 judge → 缓存增量补丁(不重建), 计数同步
        assert store.judge(sk, ["mid:A", "mid:B"], "normal", "alice") == 2
        st = store.statuses_by_session()
        assert st[sk]["mid:A"]["status"] == "normal"
        assert store.counts_by_session()[sk] == (2, 0)

        # 改判 → 计数从 normal 挪到 abnormal
        assert store.judge(sk, ["mid:A"], "abnormal", "bob") == 1
        assert store.statuses_by_session()[sk]["mid:A"]["status"] == "abnormal"
        assert store.counts_by_session()[sk] == (1, 1)

        # 外部写入(另一个连接写同一个库) → db/wal 戳记变化 → 自动重建
        other = ReviewStore(db)
        other.judge(sk, ["mid:C"], "normal", "carol")
        other.close()
        assert store.statuses_by_session()[sk]["mid:C"]["status"] == "normal"
        assert store.counts_by_session()[sk] == (2, 1)
        store.close()


def test_store_abnormal_and_stats():
    with tempfile.TemporaryDirectory() as td:
        store = ReviewStore(Path(td) / "review.db")
        sk = "bot_001/group/20002"
        fp = "mid:GB-9"
        store.judge(sk, [fp], "abnormal", "alice")
        rid = store.add_abnormal(sk, fp, "10001", "李四", "违规内容", "M-9",
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


# ── API 集成 ─────────────────────────────────────────────────

def _make_client(root: Path):
    from fastapi.testclient import TestClient

    from review.app import create_app

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

        # 登录(失败同样吃 0.5s 硬延迟 — 防爆破语义本身)
        assert client.post("/api/login", json={"username": "admin", "password": "bad"}).status_code == 401
        assert client.get("/api/sessions").status_code == 401
        tok = _login(client, "admin", "pw123")
        tok_bob = _login(client, "bob", "pw456")

        # bootstrap / sessions(私聊 + 群聊)
        boot = client.get("/api/bootstrap", headers=_auth(tok)).json()
        assert boot["bots"][0]["nickname"] == "天依"
        sess = client.get("/api/sessions", headers=_auth(tok)).json()["sessions"]
        assert len(sess) == 2
        by_key = {s["session_key"]: s for s in sess}
        assert by_key["bot_001/private/10001"]["unreviewed"] == 4
        assert by_key["_merged/group/20002"]["unreviewed"] == 6
        assert by_key["_merged/group/20002"]["bots"] == ["bot_001", "bot_002"]
        sk = "bot_001/private/10001"

        # 明细(自动锚定第一条待审所在页)
        detail = client.get("/api/session/" + sk, headers=_auth(tok)).json()
        assert detail["unreviewed"] == 4 and len(detail["entries"]) == 4
        assert detail["page"] == 1 and detail["pages"] == 1
        img_entry = detail["entries"][2]
        assert img_entry["image_url"] and img_entry["vlm"] == "这是星尘演唱会海报"

        # 正常判定(默认全部待审)
        r = client.post("/api/review", headers=_auth(tok),
                        json={"session_key": sk, "action": "normal"}).json()
        assert r["changed"] == 4 and r["remaining_unreviewed"] == 0

        # 改判一条为异常(带标签/备注) — 指定指纹
        fp = detail["entries"][1]["fingerprint"]  # bot 发言那条
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

        # 群聊合并会话也判完 → 全局无待审
        r2 = client.post("/api/review", headers=_auth(tok),
                         json={"session_key": "_merged/group/20002", "action": "normal"}).json()
        assert r2["changed"] == 6

        # 统计 (admin: 私聊 3 正常 + 群聊 6 正常 = 9; bob 改判 1 异常)
        stats = client.get("/api/stats", headers=_auth(tok)).json()
        assert stats["overall"]["normal"] == 9 and stats["overall"]["abnormal"] == 1
        assert stats["overall"]["unreviewed"] == 0
        reviewers = {r["reviewer"]: r for r in stats["per_reviewer"]}
        assert reviewers["admin"]["judged"] == 9 and reviewers["bob"]["judged"] == 1
        assert reviewers["bob"]["abnormal"] == 1


def test_api_pagination_anchor():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _build_fake_data(root)
        client, store = _make_client(root)
        try:
            tok = _login(client, "admin", "pw123")
            sk = "bot_001/private/10001"
            d1 = client.get(f"/api/session/{sk}?page_size=2", headers=_auth(tok)).json()
            assert d1["pages"] == 2 and d1["page"] == 1 and len(d1["entries"]) == 2

            # 判掉第一页 → 重开(不指定页)自动锚定下一条待审(第 2 页)
            fps = [e["fingerprint"] for e in d1["entries"]]
            r = client.post("/api/review", headers=_auth(tok),
                            json={"session_key": sk, "action": "normal", "fingerprints": fps})
            assert r.json()["remaining_unreviewed"] == 2
            d2 = client.get(f"/api/session/{sk}?page_size=2", headers=_auth(tok)).json()
            assert d2["page"] == 2 and len(d2["entries"]) == 2
            assert all(e["status"] == "unreviewed" for e in d2["entries"])

            # 显式翻页: 第 1 页全是已审
            d0 = client.get(f"/api/session/{sk}?page=1&page_size=2", headers=_auth(tok)).json()
            assert all(e["status"] == "normal" for e in d0["entries"])
        finally:
            store.close()


def test_api_incremental_via_api():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _build_fake_data(root)
        client, store = _make_client(root)
        try:
            tok = _login(client, "admin", "pw123")
            sk = "bot_001/private/10001"

            client.post("/api/review", headers=_auth(tok), json={"session_key": sk, "action": "normal"})
            # 群聊会话也判完, 才能断言"无待审"
            client.post("/api/review", headers=_auth(tok),
                        json={"session_key": "_merged/group/20002", "action": "normal"})
            assert client.get("/api/sessions?status=unreviewed", headers=_auth(tok)).json()["sessions"] == []

            # 追加新消息(改 mtime 触发增量解析) → 出现增量待审
            path = root / "history" / "bot_001" / "private" / "10001.jsonl"
            append = {"post_type": "message", "message_type": "private", "time": 1700002000,
                      "self_id": 111, "user_id": 10001, "message_id": "M-300",
                      "sender": {"user_id": 10001, "nickname": "张三"},
                      "message": [{"type": "text", "data": {"text": "又来了新消息"}}]}
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(append, ensure_ascii=False) + "\n")
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

            # 正常修改 → 200; 配置文件已写回且只有哈希
            r = client.post("/api/password", headers=_auth(tok),
                            json={"old_password": "pw123", "new_password": "brand-new"})
            assert r.status_code == 200, r.text
            text = cfg_path.read_text(encoding="utf-8")
            assert "brand-new" not in text
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
            # 旧 4 段路由已不存在 → 404
            assert client.get("/api/session/bot_x/private/1/none", headers=_auth(tok)).status_code == 404
            assert client.post("/api/review", headers=_auth(tok),
                               json={"session_key": "bad/key", "action": "normal"}).status_code == 400
            assert client.post("/api/review", headers=_auth(tok),
                               json={"session_key": "bot_001/private/10001",
                                     "action": "wat"}).status_code == 400
        finally:
            store.close()


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except Exception as e:  # noqa: BLE001
                _failed += 1
                print(f"FAIL {_name}: {e}")
                import traceback
                traceback.print_exc()
    print("---")
    assert _failed == 0, f"{_failed} 个测试失败"
    print("ALL REVIEW TESTS PASSED")
