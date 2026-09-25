"""WebUI 登录防爆破回归测试:
1. 每次登录(无论成败)固定等待 ≥0.5s
2. 并发登录被全局串行化(3 个并发总耗时 ≥1.5s)
3. 登录失败写入 loguru 日志(用户名不存在/密码错误分别告警)
"""

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from loguru import logger


def _make_app():
    from mohobot.web_panel.app import WebPanel
    tmp = tempfile.mkdtemp(prefix="login_guard_")
    panel = WebPanel(
        host="127.0.0.1", port=0, username="admin",
        password_hash=WebPanel._hash_password("right-pass"),
        data_dir=tmp, config_path=str(Path(tmp) / "none.yaml"),
    )
    return panel._app


async def test_login_constant_delay() -> None:
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        t0 = time.monotonic()
        r = await c.post("/api/login", json={"username": "admin", "password": "wrong"})
        elapsed = time.monotonic() - t0
        assert r.status_code == 401, r.text
        assert elapsed >= 0.5, f"失败登录耗时 {elapsed:.2f}s, 未达 0.5s 硬延迟"

        t0 = time.monotonic()
        r = await c.post("/api/login", json={"username": "nobody", "password": "x"})
        assert r.status_code == 401
        assert time.monotonic() - t0 >= 0.5

        t0 = time.monotonic()
        r = await c.post("/api/login", json={"username": "admin", "password": "right-pass"})
        assert r.status_code == 200, r.text
        assert time.monotonic() - t0 >= 0.5
    print("[1] 登录恒定 0.5s 延迟(失败/未知用户/成功) OK")


async def test_login_serialized() -> None:
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        t0 = time.monotonic()
        results = await asyncio.gather(*(
            c.post("/api/login", json={"username": "admin", "password": f"guess{i}"})
            for i in range(3)
        ))
        elapsed = time.monotonic() - t0
        assert all(r.status_code == 401 for r in results)
        # 串行化: 3 次尝试必须排队, 总耗时 ≥ 3 × 0.5s
        assert elapsed >= 1.5, f"3 个并发登录总耗时 {elapsed:.2f}s, 未串行化"
    print("[2] 并发登录全局串行化 OK")


async def test_failed_login_logged() -> None:
    app = _make_app()
    logs: list[str] = []
    sink_id = logger.add(lambda m: logs.append(m), level="WARNING")
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            await c.post("/api/login", json={"username": "nobody", "password": "x"})
            await c.post("/api/login", json={"username": "admin", "password": "wrong"})
    finally:
        logger.remove(sink_id)
    joined = "".join(logs)
    assert "登录失败(用户名不存在)" in joined
    assert "登录失败(密码错误)" in joined
    print("[3] 登录失败写入日志 OK")


async def main() -> None:
    await test_login_constant_delay()
    await test_login_serialized()
    await test_failed_login_logged()


if __name__ == "__main__":
    asyncio.run(main())
