"""Song tools may read the current pool but must not change a sync worker's binding."""

import asyncio
import importlib.util
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from mohobot.music_knowledge import pool
from mohobot.music_knowledge.song_database import Song
from mohobot.music_knowledge.sync_worker import try_reserve, wait_until_idle
from mohobot.services import llm_tools


@contextmanager
def _song_tools(tmp_path):
    """隔离 cwd/registry/pool 后加载 song_tools 模块, 退出时全部还原。

    不用 pytest fixture: 项目自研 legacy runner 只支持无参或仅 ``tmp_path``
    的用例入口, 而 CI 在 push main 时仍会执行该 runner。
    """
    cwd = os.getcwd()
    registry = llm_tools.registry
    saved_pool = {
        attr: getattr(pool, attr) for attr in ("_engine", "_SessionLocal", "_engine_url")
    }
    os.chdir(tmp_path)
    llm_tools.registry = llm_tools.LLMToolRegistry()
    # Keep the process-wide pool and tool registry from other tests intact.
    for attr, value in (("_engine", None), ("_SessionLocal", None), ("_engine_url", "")):
        setattr(pool, attr, value)
    source = Path(__file__).resolve().parents[1] / "plugins" / "song_tools.py"
    spec = importlib.util.spec_from_file_location("_test_song_tool_pool", source)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        pool.close_all()
        os.chdir(cwd)
        llm_tools.registry = registry
        for attr, value in saved_pool.items():
            setattr(pool, attr, value)


def test_song_tools_read_active_pool_while_sync_worker_is_running(tmp_path):
    with _song_tools(tmp_path) as song_tools:
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


def test_song_tools_only_initialize_fallback_when_pool_is_free(tmp_path):
    with _song_tools(tmp_path) as song_tools:
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
