

def test_create_bot_with_qq_promotes_online_unbound_connection():
    """创建 Bot 时填了 QQ: 已在线但未绑定的连接必须立即晋升(不用重连)。"""
    import asyncio
    import tempfile
    from pathlib import Path as _Path

    from mohobot.bot_manager import BotManager
    from mohobot.models.config import BotConfig

    with tempfile.TemporaryDirectory() as td:
        mgr = BotManager(data_dir=_Path(td))
        # 模拟 SnowLuma 已连上(未绑定)
        inst = mgr.register("2774641554", object())
        assert inst.bound is False
        cfg = mgr.create_bot(nickname="bot_001", qq=2774641554)
        promoted = mgr.get(cfg.bot_id)
        assert promoted is not None, "创建后应接管在线连接"
        assert promoted.bound is True and promoted.bot_id == cfg.bot_id
        loaded = BotConfig.load(_Path(td) / "bots" / cfg.bot_id / "config.json")
        assert loaded.qq == 2774641554
