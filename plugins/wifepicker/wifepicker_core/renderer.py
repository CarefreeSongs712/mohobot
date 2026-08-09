"""HTML→PNG 渲染 — 基于 Playwright(生产需安装 playwright + chromium)。

未安装 playwright/chromium 或渲染失败时返回 False, 调用方降级为文本输出。
浏览器实例进程内复用(单例), 渲染互不干扰用异步锁串行。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger

_browser = None
_playwright_instance = None
_launch_lock: asyncio.Lock | None = None

# 头像等外网图片加载等待时间(ms)
IMAGE_WAIT_MS = 4000


def available() -> bool:
    """playwright + chromium 是否可用。"""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


async def _get_browser():
    """惰性启动 chromium(带锁, 避免并发重复启动)。"""
    global _browser, _playwright_instance, _launch_lock
    if _browser is not None:
        return _browser
    if _launch_lock is None:
        _launch_lock = asyncio.Lock()
    async with _launch_lock:
        if _browser is not None:
            return _browser
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            logger.warning(f"Playwright 未安装, 关系图/排行降级为文本: {e}")
            return None
        try:
            _playwright_instance = await async_playwright().start()
            _browser = await _playwright_instance.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            logger.info("Playwright chromium 已启动(关系图渲染)")
        except Exception as e:
            logger.error(f"chromium 启动失败, 关系图/排行降级为文本: {e}")
            _browser = None
    return _browser


async def render_png(
    html_content: str,
    output_path: str | Path,
    *,
    width: int,
    height: int,
    wait_ms: int = IMAGE_WAIT_MS,
) -> bool:
    """渲染 HTML 到 PNG 文件。失败返回 False。"""
    browser = await _get_browser()
    if browser is None:
        return False
    page = None
    try:
        page = await browser.new_page(viewport={"width": width, "height": height})
        await page.set_content(html_content, wait_until="load")
        # 等待头像等外网图片加载 + 布局稳定
        await page.wait_for_timeout(wait_ms)
        await page.screenshot(
            path=str(Path(output_path)),
            full_page=False,
            clip={"x": 0, "y": 0, "width": width, "height": height},
        )
        return True
    except Exception as e:
        logger.error(f"HTML 渲染失败: {e}")
        return False
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


async def shutdown() -> None:
    """关闭浏览器(进程退出时)。"""
    global _browser, _playwright_instance
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None
    if _playwright_instance is not None:
        try:
            await _playwright_instance.stop()
        except Exception:
            pass
        _playwright_instance = None
