"""Standalone LOCAL mock panel, temporary data only; no bot, LLM or real config.
Run: python tests/mock_persona_ab_panel.py --port 19091
Login: admin / mock-ab-only (never use for production).
"""
import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mohobot.bot_manager import BotManager
from mohobot.context_manager import ContextManager
from mohobot.models.config import GlobalConfig, PersonaABConfig
from mohobot.models.onebot import PrivateMessageEvent, Sender
from mohobot.persona_service import PersonaService
from mohobot.services.persona_ab import PersonaABService
from mohobot.web_panel.app import WebPanel


async def build_panel(root, port=19091):
    root = Path(root)
    cfg = GlobalConfig(data_dir=str(root), plugins_dir=str(root / "plugins"))
    cfg.web_panel.password_hash = WebPanel._hash_password("mock-ab-only")
    cfg.music_knowledge = {"enabled": False}
    cfg.database.enabled = False
    manager = BotManager(str(root))
    manager.create_bot(nickname="模拟 Bot", qq=123456)
    context = ContextManager(str(root), summary_enabled=False)
    personas = PersonaService(str(root), manager, context)
    await personas.startup()
    test = await personas.create_persona("模拟测试人设 <script>（安全转义）", "模拟测试正文，不连接模型")
    cfg.persona_ab = PersonaABConfig(enabled=False, baseline_id="persona_001", test_id=test["id"], probability=1)
    service = PersonaABService(str(root), cfg.persona_ab, personas, None)
    await service.startup()
    cfg.persona_ab.enabled = True
    for user, vote, state in ((10001, "A", "voted"), (10002, "平局", "voted"), (10003, None, "pending"), (10004, None, "expired"), (10005, None, "failed")):
        ev = PrivateMessageEvent(time=1, self_id=123456, post_type="message", user_id=user, message_id=user, message="模拟问题", sender=Sender(user_id=user, nickname="模拟用户"))
        session = await context.capture_session("bot_001", "private", str(user))
        record = await service.reserve("bot_001", ev, session, {"id": "persona_001"})
        await service.repo.update("bot_001", user, record["id"], state=state, vote=vote,
            exposed_at=time.time() if state != "failed" else None,
            question='<img src=x onerror="window.abXss=1"> 这是模拟问题，不能执行脚本。',
            candidates={"baseline": "模拟基准回复\n" + "中文自动换行测试。" * 30, "test": "模拟测试回复 <script>window.abXss=2</script>"},
            parameters={"model": "mock-model", "temperature": 0.7, "max_tokens": 4096})
    cfg.persona_ab.enabled = False
    cfg.persona_ab.probability = .05
    config_path = root / "mock-config.yaml"
    cfg.save(config_path)
    panel = WebPanel(host="127.0.0.1", port=port, password_hash=cfg.web_panel.password_hash,
        data_dir=str(root), config_path=str(config_path), bot_manager=manager,
        context_manager=context, persona_service=personas, persona_ab_service=service)
    return panel


async def main(port):
    import uvicorn
    with tempfile.TemporaryDirectory(prefix="mohobot_ab_panel_") as directory:
        panel = await build_panel(directory, port)
        print(f"MOCK ONLY: http://127.0.0.1:{port}   admin / mock-ab-only", flush=True)
        print(f"Temporary data: {directory}", flush=True)
        await uvicorn.Server(uvicorn.Config(panel._app, host="127.0.0.1", port=port, log_level="warning")).serve()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=19091)
    asyncio.run(main(parser.parse_args().port))
