"""新闻/动态采集器 — B站官号与 UP 主动态/视频轮询。

职责边界:
- **只采集, 不入库**: 采到的每一条都通过 `NewsService.submit()` 提交为
  `pending`, 由管理员在 WebUI 审核通过后才生效(与 REQ-6 的审核要求一致);
- **默认关闭**: 由 `news.collector_enabled` 控制; 关闭时不发任何网络请求;
- **可测**: 网络访问集中在 `_fetch_json()`, 测试通过替换该方法注入假响应,
  不在测试里访问外网;
- **限速与容错**: 单个 UID 失败不影响其它 UID; 相邻请求有间隔; 响应字段
  缺失/格式变化时跳过而不是抛异常。

数据源说明(公开接口, 无需登录):
- 用户动态: `/x/polymer/web-dynamic/v1/feed/space?host_mid=<uid>`
- 投稿视频: `/x/space/wbi/arc/search?mid=<uid>&ps=5&pn=1`
两者都可能因风控返回空/错误码, 采集器只记录 warning 并跳过本轮。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from loguru import logger

from mohobot.news.service import NewsService

_BILI_DYNAMIC_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
_BILI_VIDEO_API = "https://api.bilibili.com/x/space/wbi/arc/search"
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


class NewsCollector:
    """按配置轮询 B 站 UID 的动态与投稿, 提交到待审池。"""

    def __init__(
        self,
        service: NewsService,
        config: Any = None,
        *,
        http_get=None,
    ) -> None:
        self._service = service
        self._cfg = config
        # 可注入的 HTTP 取数函数: (url, params) -> dict; 默认用 httpx
        self._http_get = http_get
        self._task: Optional[asyncio.Task] = None
        self._stopped = False
        # 每个 UID 上一次成功采集的时间, 避免重复拉取
        self._last_poll: dict[str, float] = {}

    # ── 配置读取 ───────────────────────────────────────────────

    def _cfg_value(self, name: str, default: Any) -> Any:
        return getattr(self._cfg, name, default) if self._cfg is not None else default

    @property
    def enabled(self) -> bool:
        return bool(self._cfg_value("collector_enabled", False))

    @property
    def interval_seconds(self) -> float:
        try:
            value = float(self._cfg_value("collector_interval_seconds", 1800) or 1800)
        except (TypeError, ValueError):
            value = 1800.0
        return max(300.0, value)

    def _uids(self) -> list[str]:
        raw = self._cfg_value("collector_uids", []) or []
        if isinstance(raw, str):
            raw = raw.replace("，", ",").split(",")
        out: list[str] = []
        for item in raw:
            uid = str(item).strip()
            if uid and uid not in out:
                out.append(uid)
        return out

    def _ttl_days(self) -> float:
        try:
            return float(self._cfg_value("collector_ttl_days", 14) or 14)
        except (TypeError, ValueError):
            return 14.0

    # ── 生命周期 ───────────────────────────────────────────────

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        if not self._uids():
            logger.info("[News] 采集器已启用但没有配置 collector_uids, 跳过启动")
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run_loop(), name="news-collector")
        logger.info(
            f"[News] 采集器启动: {len(self._uids())} 个 UID, "
            f"间隔 {self.interval_seconds:.0f}s"
        )

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run_loop(self) -> None:
        while not self._stopped:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 采集失败不得终止循环
                logger.warning(f"[News] 采集轮次失败: {exc}")
            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                raise

    # ── 单轮采集 ───────────────────────────────────────────────

    async def poll_once(self) -> dict[str, int]:
        """采集所有配置 UID; 返回 {"submitted": n, "skipped": m}。"""
        if not self.enabled:
            return {"submitted": 0, "skipped": 0}
        submitted = 0
        skipped = 0
        for index, uid in enumerate(self._uids()):
            if index:
                await asyncio.sleep(1.0)  # 相邻 UID 之间限速
            for item in await self._collect_uid(uid):
                if not item.get("title"):
                    skipped += 1
                    continue
                try:
                    await self._service.submit(
                        str(item["title"]),
                        content=str(item.get("content") or ""),
                        source=str(item.get("source") or "bilibili"),
                        url=str(item.get("url") or ""),
                        author_name=str(item.get("author_name") or ""),
                        author_id=str(item.get("author_id") or ""),
                        tags=list(item.get("tags") or []),
                        published_at=float(item.get("published_at") or 0.0),
                        ttl_days=self._ttl_days(),
                    )
                    submitted += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[News] 提交条目失败({uid}): {exc}")
                    skipped += 1
            self._last_poll[uid] = time.time()
        if submitted or skipped:
            logger.info(f"[News] 本轮采集: submitted={submitted} skipped={skipped}")
        return {"submitted": submitted, "skipped": skipped}

    async def _collect_uid(self, uid: str) -> list[dict[str, Any]]:
        """采集单个 UID 的动态与投稿; 任一接口失败按空处理。"""
        items: list[dict[str, Any]] = []
        limit = int(self._cfg_value("collector_max_items_per_uid", 5) or 5)
        limit = max(1, min(limit, 20))
        try:
            items.extend(await self._fetch_dynamics(uid, limit))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[News] 拉取动态失败 uid={uid}: {exc}")
        try:
            items.extend(await self._fetch_videos(uid, limit))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[News] 拉取投稿失败 uid={uid}: {exc}")
        # 按发布时间倒序, 只保留最新的 limit 条, 避免一次灌入太多待审
        items.sort(key=lambda it: float(it.get("published_at") or 0.0), reverse=True)
        return items[:limit]

    async def _fetch_dynamics(self, uid: str, limit: int) -> list[dict[str, Any]]:
        payload = await self._fetch_json(_BILI_DYNAMIC_API, {"host_mid": uid})
        data = (payload or {}).get("data") or {}
        cards = data.get("items") or []
        out: list[dict[str, Any]] = []
        for card in cards[:limit]:
            try:
                modules = card.get("modules") or {}
                author = (modules.get("module_author") or {})
                dynamic = (modules.get("module_dynamic") or {})
                major = dynamic.get("major") or {}
                desc = (dynamic.get("desc") or {}).get("text") or ""
                archive = major.get("archive") or {}
                opus = major.get("opus") or {}
                title = (
                    archive.get("title")
                    or (opus.get("title") if isinstance(opus, dict) else "")
                    or desc.strip().split("\n")[0]
                )
                content = desc or (archive.get("desc") or "")
                author_name = author.get("name") or f"UID {uid}"
                pub = author.get("pub_ts") or 0
                dynamic_id = card.get("id_str") or card.get("id") or ""
                url = f"https://t.bilibili.com/{dynamic_id}" if dynamic_id else ""
                out.append({
                    "title": str(title or "").strip()[:120],
                    "content": str(content or "").strip(),
                    "source": "bilibili_dynamic",
                    "url": url,
                    "author_name": str(author_name),
                    "author_id": str(uid),
                    "tags": ["B站动态"],
                    "published_at": float(pub or 0.0),
                })
            except Exception:  # noqa: BLE001 - 单条格式异常不影响整批
                continue
        return out

    async def _fetch_videos(self, uid: str, limit: int) -> list[dict[str, Any]]:
        payload = await self._fetch_json(
            _BILI_VIDEO_API, {"mid": uid, "ps": str(limit), "pn": "1"},
        )
        data = (payload or {}).get("data") or {}
        vlist = ((data.get("list") or {}).get("vlist")) or []
        out: list[dict[str, Any]] = []
        for video in vlist[:limit]:
            try:
                bvid = str(video.get("bvid") or "")
                title = str(video.get("title") or "").strip()
                if not title:
                    continue
                out.append({
                    "title": title[:120],
                    "content": str(video.get("description") or "").strip(),
                    "source": "bilibili_video",
                    "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                    "author_name": str(video.get("author") or f"UID {uid}"),
                    "author_id": str(uid),
                    "tags": ["B站视频"],
                    "published_at": float(video.get("created") or 0.0),
                })
            except Exception:  # noqa: BLE001
                continue
        return out

    async def _fetch_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """HTTP 取数(唯一网络出口; 测试可替换)。"""
        if self._http_get is not None:
            return await self._http_get(url, params)
        import httpx

        async with httpx.AsyncClient(timeout=15.0, headers=_DEFAULT_HEADERS) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("响应不是 JSON 对象")
        if data.get("code") not in (0, None):
            raise ValueError(f"接口返回错误码: {data.get('code')} {data.get('message')}")
        return data


__all__ = ["NewsCollector"]
