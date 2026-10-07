"""External-service group isolation regression tests."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _FakeSock:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


class _DispatchProbe:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace

    async def dispatch_notice(self, bot_id, event, raw):
        self.trace.append("notice")

    async def dispatch_request(self, bot_id, event, raw):
        self.trace.append("request")
        return False


async def test_group_events_archive_then_gate_including_notice_and_request() -> None:
    from mohobot.message_handler import MessageHandler
    from mohobot.models.config import GlobalConfig
    from mohobot.models.onebot import Event

    with tempfile.TemporaryDirectory(prefix="external_groups_") as td:
        trace: list[str] = []
        probe = _DispatchProbe(trace)
        cfg = GlobalConfig(
            external_service_groups=[482999198, 1059744894],
            history_dual_write=False,
        )

        class Handler(MessageHandler):
            async def _archive_event(self, bot_id, event, raw):
                trace.append("archive")
                await super()._archive_event(bot_id, event, raw)

            async def _handle_message(self, bot_id, event, raw):
                trace.append("message")

            async def _handle_notice(self, bot_id, event, raw):
                trace.append("notice_handler")

            async def _handle_request(self, bot_id, event, raw):
                trace.append("request_handler")

        handler = Handler(
            ws_server=None,
            context_manager=None,
            llm_service=None,
            plugin_system=probe,
            data_dir=td,
            global_config=cfg,
        )
        try:
            message = {
                "time": 1,
                "self_id": 11,
                "post_type": "message",
                "message_type": "group",
                "group_id": 482999198,
                "user_id": 22,
                "sender": {"user_id": 22},
                "message_id": 7,
                "message": "ignored",
            }
            await handler.handle_event("bot_001", Event.from_dict(message), message)
            notice = {
                "time": 1,
                "self_id": 11,
                "post_type": "notice",
                "notice_type": "group_upload",
                "group_id": 482999198,
            }
            await handler.handle_event("bot_001", Event.from_dict(notice), notice)
            request = {
                "time": 1,
                "self_id": 11,
                "post_type": "request",
                "request_type": "group",
                "group_id": 482999198,
                "user_id": 22,
                "sender": {"user_id": 22},
            }
            await handler.handle_event("bot_001", Event.from_dict(request), request)
            outside = dict(message, group_id=700, message_id=9)
            await handler.handle_event("bot_001", Event.from_dict(outside), outside)
            private = {
                "time": 1,
                "self_id": 11,
                "post_type": "message",
                "message_type": "private",
                "group_id": 482999198,
                "user_id": 22,
                "sender": {"user_id": 22},
                "message_id": 8,
                "message": "private is not gated",
            }
            await handler.handle_event("bot_001", Event.from_dict(private), private)

            assert trace == ["archive", "archive", "message", "archive", "message"], trace
            archive = Path(td) / "history" / "group" / "482999198.jsonl"
            assert archive.exists()
            assert json.loads(archive.read_text(encoding="utf-8"))["message_id"] == 7
        finally:
            await handler.close()


def test_external_service_config_defaults_and_roundtrip() -> None:
    from mohobot.models.config import GlobalConfig, _int_list, is_external_service_group

    assert GlobalConfig().external_service_groups == []
    assert is_external_service_group(482999198, []) is False
    assert is_external_service_group(1059744894, []) is False
    assert _int_list({482999198}) == []  # preserve the original parser contract

    with tempfile.TemporaryDirectory(prefix="external_config_") as td:
        path = Path(td) / "global.yaml"
        cfg = GlobalConfig(external_service_groups=[482999198, 1059744894])
        cfg.save(path)
        loaded = GlobalConfig.load(path)
        assert loaded.external_service_groups == [482999198, 1059744894]
        assert loaded.to_dict()["external_service_groups"] == [482999198, 1059744894]


async def test_outbound_group_guard_allows_private_and_outside_and_blocks_configured() -> None:
    from mohobot.bot_manager import BotInstance, BotManager
    from mohobot.models.config import BotConfig, GlobalConfig
    from mohobot.ws_server import WSServer

    with tempfile.TemporaryDirectory(prefix="external_outbound_") as td:
        manager = BotManager(data_dir=td)
        sock = _FakeSock()
        instance = BotInstance(
            "bot_001", sock, BotConfig(bot_id="bot_001", qq=11), bound=True
        )
        manager._bots["bot_001"] = instance
        cfg = GlobalConfig(external_service_groups=[482999198, 1059744894])
        ws = WSServer(bot_manager=manager, global_config=cfg, data_dir=td)
        try:
            for group_id in (482999198, 1059744894):
                await ws.send_to_bot(
                    "bot_001", "send_group_msg",
                    {"group_id": group_id, "message": "blocked"},
                )
                await ws.send_to_bot(
                    "bot_001", "send_group_forward_msg",
                    {"group_id": group_id, "messages": []},
                )
                await ws.send_to_bot(
                    "bot_001", "send_msg",
                    {"message_type": "group", "group_id": group_id, "message": "blocked"},
                )
            await asyncio.sleep(0.05)
            assert sock.sent == []

            await ws.send_to_bot(
                "bot_001", "send_group_msg",
                {"group_id": 700, "message": "allowed"},
            )
            await ws.send_to_bot(
                "bot_001", "send_private_msg",
                {"user_id": 482999198, "message": "private allowed"},
            )
            assert len(sock.sent) == 2
            assert json.loads(sock.sent[0])["params"]["group_id"] == 700
            assert json.loads(sock.sent[1])["params"]["user_id"] == 482999198
        finally:
            await ws.stop()


async def test_queued_group_send_rechecks_live_config_before_dispatch() -> None:
    from mohobot.bot_manager import BotInstance, BotManager
    from mohobot.models.config import BotConfig, GlobalConfig
    from mohobot.ws_server import WSServer

    with tempfile.TemporaryDirectory(prefix="external_queue_") as td:
        manager = BotManager(data_dir=td)
        sock = _FakeSock()
        instance = BotInstance(
            "bot_001", sock, BotConfig(bot_id="bot_001", qq=11), bound=True
        )
        manager._bots["bot_001"] = instance
        cfg = GlobalConfig(external_service_groups=[])
        ws = WSServer(
            bot_manager=manager,
            global_config=cfg,
            data_dir=td,
            outbound_interval=0.25,
        )
        try:
            # Prime the worker so the target send waits behind the rate limiter.
            await ws.send_group_msg("bot_001", 700, "prime")
            pending = asyncio.create_task(
                ws.send_group_msg("bot_001", 482999198, "queued")
            )
            await asyncio.sleep(0.02)
            cfg.external_service_groups = [482999198]
            await pending
            assert len(sock.sent) == 1
            assert json.loads(sock.sent[0])["params"]["group_id"] == 700
        finally:
            await ws.stop()


async def main() -> None:
    await test_group_events_archive_then_gate_including_notice_and_request()
    test_external_service_config_defaults_and_roundtrip()
    await test_outbound_group_guard_allows_private_and_outside_and_blocks_configured()
    await test_queued_group_send_rechecks_live_config_before_dispatch()
    print("ALL EXTERNAL SERVICE GROUP TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
