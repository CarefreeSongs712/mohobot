"""Song tools may read the current pool but must not change a sync worker's binding."""

import asyncio
import importlib.util
import json
import threading
from pathlib import Path

import pytest

from mohobot.music_knowledge import pool
from mohobot.music_knowledge.song_database import Song
from mohobot.music_knowledge.sync_worker import try_reserve, wait_until_idle
from mohobot.services import llm_tools


@pytest.fixture
def song_tools(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm_tools, "registry", llm_tools.LLMToolRegistry())
    # Keep the process-wide pool and tool registry from other tests intact.
    for attr, value in (("_engine", None), ("_SessionLocal", None), ("_engine_url", "")):
        monkeypatch.setattr(pool, attr, value)
    source = Path(__file__).resolve().parents[1] / "plugins" / "song_tools.py"
    spec = importlib.util.spec_from_file_location("_test_song_tool_pool", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        pool.close_all()


def test_song_tools_read_active_pool_while_sync_worker_is_running(tmp_path, song_tools):
    async def run():
        loop = asyncio.get_running_loop()
        started, unblock = asyncio.Event(), threading.Event()
        reservation = try_reserve()
        assert reservation is not None
        folder = tmp_path / "configured"

        def work():
            pool.ensure_init(str(folder), "custom.db")
            with pool.get_session() as db:
                db.add(Song(name="测试歌曲", safe_name="测试歌曲", introduction="介绍", lyrics="歌词"))
                db.commit()
                engine = db.get_bind()
            loop.call_soon_threadsafe(started.set)
            assert unblock.wait(10)
            with pool.get_session() as db:
                assert db.get_bind() is engine

        task = asyncio.create_task(reservation.run(work))
        try:
            await asyncio.wait_for(started.wait(), 5)
            assert json.loads(song_tools.song_search("测试"))["songs"] == ["测试歌曲"]
            assert json.loads(song_tools.song_get_detail("测试歌曲"))["name"] == "测试歌曲"
            assert json.loads(song_tools.song_get_lyrics("测试歌曲"))["lyrics"] == "歌词"
            assert not (tmp_path / "data").exists()
        finally:
            unblock.set()
            await asyncio.wait_for(task, 5)
            assert await wait_until_idle(timeout=0)

    asyncio.run(run())


def test_song_tools_only_initialize_fallback_when_pool_is_free(tmp_path, song_tools):
    reservation = try_reserve()
    assert reservation is not None
    try:
        with pytest.raises(RuntimeError, match="歌曲库正在初始化"):
            song_tools.song_search("测试")
        assert not (tmp_path / "data").exists()
    finally:
        reservation.release_if_unsubmitted()

    assert json.loads(song_tools.song_search("测试")) == {"query": "测试", "songs": []}
    assert (tmp_path / "data" / "song_knowledge" / "knowledge_db.db").is_file()
