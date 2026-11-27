"""PR161 lifecycle/receipt/ops regressions with real local protocol endpoints."""

import asyncio
import importlib.util
import json
import sqlite3
import subprocess
import sys
import threading
from contextlib import asynccontextmanager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import websockets
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins" / "wifepicker"))
sys.path.insert(0, str(ROOT / "plugins" / "relationship"))

from mohobot.bot_manager import BotManager
from mohobot.models.onebot import GroupMessageEvent, Sender
from mohobot.services.task_supervisor import TaskSupervisor
from mohobot.ws_server import WSServer
from relationship_core.utils import _parse_retcode, ERROR_TEXT_LIMIT, summarize_error
from scripts import clean_knowledge_db


def load_plugin(name):
    spec = importlib.util.spec_from_file_location(f"pr161_{name}", ROOT / "plugins" / name / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Plugin


@asynccontextmanager
async def onebot(tmp_path, *, nickname_gate=0, retcode=0):
    manager = BotManager(str(tmp_path / "bot-data"))
    cfg = manager.create_bot(qq=1000)
    server = WSServer(manager, host="127.0.0.1", port=0, data_dir=str(tmp_path / "ws-data"),
                      outbound_interval=0)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}",
                                      additional_headers={"X-Self-ID": "1000"}) as socket:
            async def protocol_peer():
                nickname_count = 0
                async for raw in socket:
                    request = json.loads(raw)
                    calls.append(request)
                    action = request["action"]
                    if action == "get_group_member_info":
                        nickname_count += 1
                        if nickname_count == nickname_gate:
                            entered.set()
                            await release.wait()
                    data = {"nickname": "测试成员", "card": "测试群友", "message_id": len(calls)}
                    response = {"status": "ok", "retcode": retcode if action.startswith("set_") else 0,
                                "data": data, "echo": request.get("echo")}
                    await socket.send(json.dumps(response))
            peer = asyncio.create_task(protocol_peer())
            try:
                async with asyncio.timeout(5):
                    while manager.get(cfg.bot_id) is None:
                        await asyncio.sleep(0.01)
                yield server, cfg.bot_id, calls, entered, release
            finally:
                release.set()
                peer.cancel()
                await asyncio.gather(peer, return_exceptions=True)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_late_proposal_cannot_revive_stopped_instance(tmp_path):
    for supervised in (False, True):
        for nickname_gate in (1, 2):
            case = tmp_path / f"proposal-{supervised}-{nickname_gate}"
            case.mkdir()
            await _late_proposal_case(case, nickname_gate, supervised)


async def _late_proposal_case(tmp_path, nickname_gate, supervised):
    supervisor = TaskSupervisor() if supervised else None
    async with onebot(tmp_path, nickname_gate=nickname_gate) as (ws, bot_id, calls, entered, release):
        cls = load_plugin("wifepicker")
        cls.inject_ws_server(ws)
        cls.inject_data_dir(str(tmp_path / "wife-data"))
        cls.inject_task_supervisor(supervisor)
        plugin = cls()
        event = GroupMessageEvent(time=0, self_id=1000, post_type="message", message_type="group",
                                  message_id=1, user_id=2001, group_id=888888,
                                  sender=Sender(user_id=2001),
                                  message=[{"type": "at", "data": {"qq": "1002"}}])
        handler = asyncio.create_task(plugin.cmd_propose(bot_id, event, ""))
        await asyncio.wait_for(entered.wait(), 5)
        await plugin.on_shutdown()
        release.set()
        assert await asyncio.wait_for(handler, 5) is None
        assert not plugin._propose_requests and not plugin._background_tasks
        assert plugin._spawn_task(asyncio.sleep(100), name="late", owner="propose") is None
        assert not any(call["action"] == "send_group_msg" for call in calls)
    if supervisor:
        await supervisor.shutdown()


@pytest.mark.asyncio
async def test_overlong_retcode_notifies_applicant_and_admin(tmp_path):
    for index, retcode in enumerate(("0" * 5000, "9" * 5000, "+" + "0" * 5000)):
        case = tmp_path / f"retcode-{index}"
        case.mkdir()
        await _overlong_retcode_case(case, retcode)


async def _overlong_retcode_case(tmp_path, retcode):
    async with onebot(tmp_path, retcode=retcode) as (ws, bot_id, calls, _, _release):
        cls = load_plugin("relationship")
        cls.inject_ws_server(ws)
        cls.inject_data_dir(str(tmp_path / "relationship-data"))
        cls.inject_admin_ids(["7777"])
        plugin = cls()
        plugin.plugin_config = {"manage_group": "100", "manage_users": [],
                                "request": {"auto_agree_friend": True}}
        raw = {"post_type": "request", "request_type": "friend", "user_id": 2002,
               "flag": "friend:isolated", "comment": "测试申请"}
        assert await plugin.on_request(bot_id, SimpleNamespace(), raw)
        await asyncio.sleep(0.02)
        messages = [call for call in calls if call["action"] in {"send_group_msg", "send_private_msg"}]
        assert len(messages) == 2
        texts = [str(call["params"]["message"]) for call in messages]
        assert all("未确认" in text and len(text) < 1000 for text in texts)
        assert not any("已自动同意" in text for text in texts)
        assert _parse_retcode(retcode) is None
        assert len(summarize_error(retcode)) <= ERROR_TEXT_LIMIT


def test_huge_integer_retcode_also_has_bounded_diagnostic():
    value = 10**5000
    assert _parse_retcode(value) is None
    assert len(summarize_error(value)) <= ERROR_TEXT_LIMIT


@contextmanager
def vcpedia_endpoint(failure):
    class Endpoint(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            status, data = 200, {"query": {"categorymembers": []}}
            if self.path.startswith("/api.php"):
                later = "cmcontinue" in query
                if failure == "later503" and not later:
                    data = {"query": {"categorymembers": [{"title": "partial"} ]},
                            "continue": {"cmcontinue": "next"}}
                elif failure in {"503", "later503"}:
                    status = 503
                elif failure == "badjson":
                    data = None
                elif failure == "apierror":
                    data = {"error": {"code": "readonly", "info": "maintenance"}}
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"not json" if data is None else json.dumps(data).encode())
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_vcpedia_real_http_failure_makes_cli_nonzero(tmp_path):
    for failure in ("503", "later503", "badjson", "apierror"):
        case = tmp_path / f"vcpedia-{failure}"
        case.mkdir()
        _vcpedia_failure_case(case, failure)


def _vcpedia_failure_case(tmp_path, failure):
    with vcpedia_endpoint(failure) as base_url:
        (tmp_path / "config").mkdir()
        config = {"music_knowledge": {"song_database": {"db_folder": str(tmp_path / "songs"),
                                                       "db_file": "songs.db"},
                                       "crawler": {"base_url": base_url, "timeout": 2,
                                                   "cookie_dir": str(tmp_path / "cookies"),
                                                   "category_depth": 0}}}
        (tmp_path / "config" / "global.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "sync_vcpedia.py")],
                                cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "同步失败" in result.stderr and "本次同步结果" not in result.stdout
    if "503" in failure:
        assert "HTTP 503" in result.stderr


def song_db(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    fields = ",".join(f"{field} TEXT" for field in clean_knowledge_db.CREDIT_FIELDS)
    conn.execute(f"CREATE TABLE songs(uuid TEXT, introduction TEXT, lyrics TEXT, {fields})")
    conn.execute("INSERT INTO songs(uuid, introduction, lyrics) VALUES('base', '''' || 'text', 'lyrics')")
    conn.commit()
    return conn


def test_closed_wal_dryrun_discloses_only_sql_readonly(tmp_path):
    path = tmp_path / "songs.db"
    song_db(path).close()
    assert {x.name for x in tmp_path.iterdir()} == {path.name}
    before = path.read_bytes()
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "clean_knowledge_db.py"),
                             "--db", str(path), "--dry-run"], cwd=tmp_path,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert path.read_bytes() == before
    assert {x.name for x in tmp_path.iterdir()} <= {path.name, path.name+"-wal", path.name+"-shm"}
    assert "辅助文件" in result.stdout


def test_dryrun_reads_uncheckpointed_committed_wal_rows(tmp_path):
    path = tmp_path / "songs.db"
    writer = song_db(path)
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer.execute("INSERT INTO songs(uuid,introduction,lyrics) VALUES('wal-row', '正文', '歌词')")
    writer.commit()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if not p.name.endswith("-shm")}
    try:
        total, _updates, _lost = clean_knowledge_db._compute_updates(path, readonly=True)
        assert total == 2
        after = {p.name: p.read_bytes() for p in tmp_path.iterdir() if not p.name.endswith("-shm")}
        assert before == after
        assert {p.name for p in tmp_path.iterdir()} == {path.name, path.name+"-wal", path.name+"-shm"}
    finally:
        writer.close()
