"""WebPanel 两阶段关闭回归测试。

背景: 面板重启时 stop() 曾立刻强制关闭 server sockets, 硬杀浏览器在途
请求 → audit 中间件抛 "No response returned" / EndOfStream 噪音。

覆盖:
1. 在途慢请求: stop() 应等 uvicorn 排空(请求正常返回 200), 而非硬杀
2. 关闭后端口立即可重绑(优雅路径不泄漏 socket)
"""

import asyncio
import importlib.util
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from loguru import logger

_PANEL = Path(__file__).resolve().parent.parent / "mohobot" / "web_panel" / "app.py"


def _load():
    spec = importlib.util.spec_from_file_location("webpanel_test", _PANEL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_graceful_drain() -> None:
    mod = _load()
    WebPanel = mod.WebPanel
    tmp = tempfile.mkdtemp(prefix="webpanel_")
    panel = WebPanel(
        host="127.0.0.1", port=0, username="admin",
        password_hash=WebPanel._hash_password("test-pass"),
        data_dir=tmp, config_path=str(Path(tmp) / "none.yaml"),
    )

    # 注册一个慢端点, 模拟在途请求
    @panel._app.get("/__test_slow")
    async def _slow():
        await asyncio.sleep(2.5)
        return {"status": "ok"}

    logs: list[str] = []
    sink_id = logger.add(lambda m: logs.append(m), level="INFO")

    task = asyncio.create_task(panel.start())
    # 等待监听就绪并拿到实际端口(port=0 由 OS 分配)
    deadline = asyncio.get_event_loop().time() + 10
    port = None
    while asyncio.get_event_loop().time() < deadline:
        server = getattr(panel, "_server_instance", None)
        servers = list(getattr(server, "servers", None) or []) if server else []
        if servers and getattr(server, "started", False):
            port = servers[0].sockets[0].getsockname()[1]
            break
        await asyncio.sleep(0.05)
    assert port, "panel did not start"

    # 发起在途慢请求(2.5s), 0.3s 后触发 stop()
    async with httpx.AsyncClient(timeout=15.0) as client:
        slow_req = asyncio.create_task(client.get(f"http://127.0.0.1:{port}/__test_slow"))
        await asyncio.sleep(0.3)
        t0 = asyncio.get_event_loop().time()
        await panel.stop()
        elapsed = asyncio.get_event_loop().time() - t0
        # 优雅排空: stop() 等慢请求完成(>=2s), 而不是立刻硬杀
        assert elapsed >= 2.0, f"stop returned too early ({elapsed:.2f}s), in-flight request likely aborted"
        resp = await slow_req
        assert resp.status_code == 200, resp.status_code
        assert resp.json() == {"status": "ok"}

    # 端口立即可重绑(无 socket 泄漏)
    rebinder = await asyncio.start_server(lambda r: None, "127.0.0.1", port)
    rebinder.close()
    await rebinder.wait_closed()

    # 未走强制关闭兜底
    assert not any("forcing socket close" in m for m in logs), logs[-10:]
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    logger.remove(sink_id)
    print("[1] 在途请求优雅排空 + 端口可重绑 OK")


async def main() -> None:
    await test_graceful_drain()
    print("\nALL WEBPANEL STOP TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())