"""多来源歌曲同步编排 — 发现 → 抓取 → 合并 → 统计, 支持断点续跑与限速。

与旧 `sync_vcpedia_new_songs` 的区别:
- 不再"已存在就跳过": 按 mode 决定是否对库内歌曲做字段级补全
  (new_only / fill_missing / full_refresh)。
- 每个来源独立限速、独立失败计数与熔断, 一个源挂掉不影响其它源。
- 游标、已完成标题、失败标题都写进 `sync_state` 表, 进程重启可续跑。
- 抓取结果同时写 `song_sources`, 已存在且内容哈希未变时可跳过合并。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from loguru import logger
from sqlalchemy.orm import Session

from mohobot.music_knowledge.merge import MergeResult, SongMerger
from mohobot.music_knowledge.pool import ensure_init, get_session
from mohobot.music_knowledge.song_database import (
    Song,
    SongSource,
    get_sync_state,
    set_sync_state,
    update_song_stats,
)
from mohobot.music_knowledge.sources.base import SourceAdapter, SourceError, SourceRecord

MODES = ("new_only", "fill_missing", "full_refresh")

# 当前同步使用的库位置(由 sync_default/build_adapters 之外的调用方设置)
_DB_LOCATION: tuple[str, str] = ("./data/song_knowledge", "knowledge_db.db")


def set_db_location(db_folder: str, db_file: str) -> None:
    """显式指定歌曲库位置(供脚本/测试使用)。"""
    global _DB_LOCATION
    _DB_LOCATION = (db_folder, db_file)


def _db_location() -> tuple[str, str]:
    return _DB_LOCATION

_STATE_KEY = "song_sync"


@dataclass
class SourceRunStats:
    """单个来源一次同步的统计。"""

    source: str
    listed: int = 0
    fetched: int = 0
    skipped_unchanged: int = 0
    failed: int = 0
    elapsed: float = 0.0
    aborted: str = ""
    merge: MergeResult = field(default_factory=MergeResult)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "listed": self.listed,
            "fetched": self.fetched,
            "skipped_unchanged": self.skipped_unchanged,
            "failed": self.failed,
            "elapsed": round(self.elapsed, 1),
            "aborted": self.aborted,
            **self.merge.as_dict(),
        }


@dataclass
class SyncReport:
    """一次完整同步(可能跨多个来源)的汇总。"""

    mode: str
    started_at: datetime
    finished_at: datetime | None = None
    sources: list[SourceRunStats] = field(default_factory=list)
    total_songs: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": self.finished_at.isoformat(timespec="seconds") if self.finished_at else None,
            "total_songs": self.total_songs,
            "sources": [s.as_dict() for s in self.sources],
        }

    def summary_text(self) -> str:
        """给聊天回复用的多行摘要。"""
        lines = [f"歌曲库同步完成({self.mode}), 当前共 {self.total_songs} 首"]
        for s in self.sources:
            parts = [f"新增 {s.merge.created}", f"更新 {s.merge.updated}"]
            if s.merge.filled_fields:
                parts.append(f"补字段 {s.merge.filled_fields}")
            if s.merge.unmatched:
                parts.append(f"未对齐 {s.merge.unmatched}")
            if s.failed:
                parts.append(f"失败 {s.failed}")
            line = f"- {s.source}: " + ", ".join(parts)
            if s.aborted:
                line += f" (中止: {s.aborted})"
            lines.append(line)
        return "\n".join(lines)


def _is_song_title(title: str) -> bool:
    """过滤分类页里的非歌曲条目(用户页/分类页/文件/模板/消歧义索引等)。

    实测萌百分类里混有 `User:318cyz/Sandbox`、`A-Side系列`、`2025年度梗集`
    这类页面, 抓下来只会污染歌曲表。
    """
    if not title or not title.strip():
        return False
    low = title.strip()
    for prefix in ("User:", "用户:", "Category:", "分类:", "File:", "文件:",
                   "Template:", "模板:", "Special:", "特殊:", "Talk:", "讨论:"):
        if low.startswith(prefix):
            return False
    if "/" in low and low.split("/", 1)[0].endswith(("Sandbox", "沙盒")):
        return False
    # 系列页/梗集/列表页不是单曲
    for marker in ("年度梗集", "系列", "歌曲列表", "曲目列表", "消歧义"):
        if marker in low:
            return False
    return True


def _load_state(db: Session) -> dict[str, Any]:
    """读取旧版合并状态(仅用于汇总展示与迁移)。"""
    state = get_sync_state(db, _STATE_KEY)
    state.setdefault("sources", {})
    return state


def _save_state(db: Session, state: dict[str, Any]) -> None:
    set_sync_state(db, _STATE_KEY, state)


def _source_state_key(source: str) -> str:
    """每个来源独立的断点键。

    多来源并行时若共用 `song_sync` 一个 key, 各线程读到的都是自己启动时的
    快照, 后写者会整体覆盖先写者, 导致其它来源的 done 列表丢失、重启后
    重抓。按来源分键后各写各的, 互不覆盖。
    """
    return f"{_STATE_KEY}:source:{source}"


def _load_source_state(db: Session, source: str) -> dict[str, Any]:
    return get_sync_state(db, _source_state_key(source))


def _save_source_state(db: Session, source: str, value: dict[str, Any]) -> None:
    set_sync_state(db, _source_state_key(source), value)


def merge_legacy_source_state(db: Session) -> dict[str, Any]:
    """把旧版 `song_sync` 里各来源的状态迁移到独立键(幂等)。

    旧版并行同步会互相覆盖, 这里只迁移仍然存在的数据; 已完成的标题若在
    旧 key 里丢失无法恢复, 只能重抓(代价有限, 且内容哈希会拦住重复合并)。
    """
    legacy = _load_state(db)
    sources = legacy.get("sources") or {}
    for name, value in sources.items():
        if not isinstance(value, dict):
            continue
        current = _load_source_state(db, name)
        merged = dict(value)
        if current:
            merged.update(current)
            merged["done"] = sorted(
                set(current.get("done") or []) | set(value.get("done") or [])
            )
        _save_source_state(db, name, merged)
    return sources


def _persist_progress(db: Session, source_state: dict[str, Any],
                      done: set[str], failed: list[str]) -> None:
    """把已完成/失败标题写回状态。

    不做"只留最近 N 条"的截断: 全量分类有上万首, 截断会导致已抓过的
    歌曲在下一轮被重新抓取(萌百 4.5s/首, 代价很高)。10 万标题的 JSON
    约 2MB, 仍在 SQLite 单行可接受范围内。
    """
    source_state["done"] = sorted(done)
    source_state["failed"] = failed[-2000:]
    source_state["done_count"] = len(done)
    source_state["failed_count"] = len(failed)
    source_state["updated_at"] = datetime.now().isoformat(timespec="seconds")


def _write_progress_file(path: str | None, payload: dict[str, Any]) -> None:
    """把当前进度写到 JSON 文件(供外部每 10 分钟汇报读取)。

    只写非敏感统计, 失败不影响同步主流程。
    """
    if not path:
        return
    import json as _json
    from pathlib import Path as _Path

    try:
        target = _Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            _json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(target)
    except Exception as e:  # pragma: no cover - 进度文件失败不应影响同步
        logger.debug(f"进度文件写入失败: {e}")


def _write_source_progress(path: str | None, source: str, payload: dict[str, Any]) -> None:
    """多来源并行时各写各的分片文件, 避免互相覆盖。

    最终路径形如 `logs/song_sync_progress.moegirl.json`。
    """
    if not path:
        return
    from pathlib import Path as _Path

    base = _Path(path)
    _write_progress_file(str(base.with_name(f"{base.stem}.{source}{base.suffix}")), payload)


def _needs_fetch(db: Session, record_source: str, title: str, record: SourceRecord) -> bool:
    """内容哈希一致时跳过合并(仍算 fetched, 但不算变更)。"""
    row = (
        db.query(SongSource)
        .filter(SongSource.source == record_source, SongSource.source_title == title)
        .first()
    )
    if row is None:
        return True
    return (row.content_hash or "") != record.content_hash()


def _existing_song(db: Session, name: str) -> Song | None:
    from mohobot.music_knowledge.normalize import bracket_core, normalize_name

    norm = normalize_name(name)
    if norm:
        hit = db.query(Song).filter(Song.normalized_name == norm).first()
        if hit is not None:
            return hit
    hit = db.query(Song).filter(Song.name == name).first()
    if hit is not None:
        return hit
    core = normalize_name(bracket_core(name))
    if core and core != norm:
        return db.query(Song).filter(Song.normalized_name == core).first()
    return None


def sync_source(
    adapter: SourceAdapter,
    *,
    mode: str = "fill_missing",
    limit: int | None = None,
    max_fail: int = 30,
    interval: float | None = None,
    progress=None,
    progress_file: str | None = None,
) -> SourceRunStats:
    """同步单个来源。

    mode:
      - new_only:    只抓库里没有的歌(旧行为)
      - fill_missing: 库内已有也抓, 只填空字段/按优先级更新(默认)
      - full_refresh: 全部重新抓取并合并
    progress: 可选回调 `progress(stats, current_title, index, total)`。
    progress_file: 可选 JSON 进度文件路径, 每个标题处理完就更新。
    """
    if mode not in MODES:
        raise ValueError(f"未知 mode: {mode!r}, 可选 {MODES}")

    stats = SourceRunStats(source=adapter.name)
    started = time.monotonic()
    # 与匹配器共用 pool 的 engine: 同一进程内不再开第二套连接池,
    # 也保证 WAL/忙等待设置对同步写入生效。
    ensure_init(*_db_location())
    db = get_session()
    # 断点按来源独立存储: 并行时各来源互不覆盖(见 _source_state_key)
    source_state = dict(_load_source_state(db, adapter.name))
    done: set[str] = set(source_state.get("done") or [])
    failed_titles: list[str] = list(source_state.get("failed") or [])
    interval = adapter.default_interval if interval is None else max(0.0, interval)

    merger = SongMerger(db)
    merger.load_index()
    consecutive_failures = 0
    last_request = 0.0

    try:
        titles = list(adapter.list_titles(limit=limit))
        # 跳过已完成的标题(断点续跑); full_refresh 时清空重抓
        if mode == "full_refresh":
            done.clear()
        todo: list[str] = []
        for title in titles:
            if not _is_song_title(title):
                stats.skipped_unchanged += 1
                continue
            if title in done:
                continue
            if mode == "new_only" and _existing_song(db, title) is not None:
                stats.skipped_unchanged += 1
                done.add(title)
                continue
            todo.append(title)
        stats.listed = len(titles)
        logger.info(f"[{adapter.name}] 待抓 {len(todo)} / 共 {len(titles)} (mode={mode})")

        for index, title in enumerate(todo, 1):
            if interval > 0:
                wait = interval - (time.monotonic() - last_request)
                if wait > 0:
                    time.sleep(wait)
            last_request = time.monotonic()
            try:
                record = adapter.fetch(title)
                stats.fetched += 1
                consecutive_failures = 0
                if not (record.name or "").strip():
                    raise SourceError(f"解析结果缺少歌名: {title}")
                if not (record.lyrics or record.introduction):
                    # 没有任何正文的页面不是有效歌曲记录, 记为失败但不重试
                    raise SourceError(f"解析结果为空(无歌词/无介绍): {title}")
                if not _needs_fetch(db, adapter.name, title, record):
                    stats.skipped_unchanged += 1
                    done.add(title)
                    continue
                # 按 mode 决定是否允许对已有歌补全
                if mode == "new_only" and _existing_song(db, record.name or title) is not None:
                    stats.skipped_unchanged += 1
                    done.add(title)
                    continue
                merger.merge(record, stats.merge)
                db.commit()
                done.add(title)
            except SourceError as e:
                stats.failed += 1
                consecutive_failures += 1
                failed_titles.append(title)
                logger.warning(f"[{adapter.name}] 抓取失败 {title}: {e}")
                if consecutive_failures >= max_fail:
                    stats.aborted = f"连续失败 {consecutive_failures} 次"
                    break
            except Exception as e:  # 解析/合并的意外错误不应中断整轮
                db.rollback()
                stats.failed += 1
                consecutive_failures += 1
                failed_titles.append(title)
                logger.exception(f"[{adapter.name}] 处理失败 {title}: {e}")
                if consecutive_failures >= max_fail:
                    stats.aborted = f"连续失败 {consecutive_failures} 次"
                    break
            finally:
                if progress is not None:
                    try:
                        progress(stats, title, index, len(todo))
                    except Exception:
                        pass
                if progress_file:
                    elapsed = time.monotonic() - started
                    rate = index / elapsed if elapsed > 0 else 0.0
                    remaining = (len(todo) - index) / rate if rate > 0 else None
                    _write_source_progress(progress_file, adapter.name, {
                        "source": adapter.name,
                        "mode": mode,
                        "total": len(titles),
                        "todo": len(todo),
                        "index": index,
                        "current": title,
                        "fetched": stats.fetched,
                        "failed": stats.failed,
                        "skipped_unchanged": stats.skipped_unchanged,
                        "created": stats.merge.created,
                        "updated": stats.merge.updated,
                        "filled_fields": stats.merge.filled_fields,
                        "unmatched": stats.merge.unmatched,
                        "conflicts": stats.merge.conflicts,
                        "elapsed_sec": round(elapsed, 1),
                        "rate_per_sec": round(rate, 3),
                        "eta_sec": round(remaining, 1) if remaining is not None else None,
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    })
                if index % 50 == 0:
                    _persist_progress(db, source_state, done, failed_titles)
                    _save_source_state(db, adapter.name, source_state)

        update_song_stats(db)
        _persist_progress(db, source_state, done, failed_titles)
        source_state["last_run"] = datetime.now().isoformat(timespec="seconds")
        source_state["last_mode"] = mode
        _save_source_state(db, adapter.name, source_state)
    finally:
        stats.elapsed = time.monotonic() - started
        total = db.query(Song).count()
        merger.close()
        db.close()

    logger.info(
        f"[{adapter.name}] 完成: 新增 {stats.merge.created}, 更新 {stats.merge.updated}, "
        f"补字段 {stats.merge.filled_fields}, 失败 {stats.failed}, 用时 {stats.elapsed:.1f}s "
        f"(库内 {total} 首)"
    )
    return stats


def sync_all(
    adapters: Iterable[SourceAdapter],
    *,
    mode: str = "fill_missing",
    limit: int | None = None,
    max_fail: int = 30,
    progress=None,
    progress_file: str | None = None,
    parallel: bool = True,
) -> SyncReport:
    """同步多个来源。

    parallel=True(默认)时三个来源同时跑: 站点不同、限速互不影响, 总耗时
    取最慢的那个来源。并发安全由 SongMerger 的进程级合并锁 + 共享身份
    索引保证(见 merge.py): 网络抓取并行, "查重+写入"串行。
    parallel=False 保留旧的顺序执行, 方便单站限流或排查。
    """
    adapters = list(adapters)
    report = SyncReport(mode=mode, started_at=datetime.now())

    def _run(adapter: SourceAdapter) -> SourceRunStats:
        try:
            return sync_source(
                adapter, mode=mode, limit=limit, max_fail=max_fail,
                progress=progress, progress_file=progress_file,
            )
        except Exception as e:
            logger.exception(f"来源 {adapter.name} 同步失败: {e}")
            failed = SourceRunStats(source=adapter.name)
            failed.aborted = str(e)[:200]
            return failed

    if parallel and len(adapters) > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=len(adapters),
                                thread_name_prefix="song-sync") as pool:
            # 保持配置里的顺序输出, 便于报告阅读
            report.sources = list(pool.map(_run, adapters))
    else:
        report.sources = [_run(adapter) for adapter in adapters]

    ensure_init(*_db_location())
    db = get_session()
    try:
        report.total_songs = db.query(Song).count()
    finally:
        db.close()
    report.finished_at = datetime.now()
    return report


def build_adapters(config: dict[str, Any]) -> list[SourceAdapter]:
    """按配置构造启用的来源适配器(导入失败/未启用则跳过)。

    接受两种入参, 避免调用方传错层导致 `sync` 段被静默忽略:
    - 完整配置: {"music_knowledge": {...}}
    - music_knowledge 段本身: {"song_database": ..., "sync": ...}
    """
    music_cfg = config or {}
    if isinstance(music_cfg.get("music_knowledge"), dict):
        music_cfg = music_cfg["music_knowledge"]
    sync_cfg = music_cfg.get("sync") or {}
    source_cfgs: dict[str, Any] = sync_cfg.get("sources") or {}
    enabled_order = sync_cfg.get("order") or ["vcpedia", "vocawiki", "moegirl"]
    adapters: list[SourceAdapter] = []

    for name in enabled_order:
        item = source_cfgs.get(name) or {}
        if item.get("enabled") is False:
            continue
        try:
            if name == "vocawiki":
                from mohobot.music_knowledge.sources.vocawiki import VocawikiAdapter
                adapters.append(VocawikiAdapter(item))
            elif name == "moegirl":
                from mohobot.music_knowledge.sources.moegirl import MoegirlAdapter
                adapters.append(MoegirlAdapter(item))
            elif name == "vcpedia":
                from mohobot.music_knowledge.sources.vcpedia_source import VcpediaAdapter
                adapters.append(VcpediaAdapter(item))
            else:
                logger.warning(f"未知歌曲来源: {name}")
        except Exception as e:
            logger.warning(f"歌曲来源 {name} 初始化失败, 已跳过: {e}")
    return adapters


def sync_default(config: dict[str, Any], *, mode: str | None = None, limit: int | None = None,
                 progress=None) -> SyncReport:
    """按 `music_knowledge.sync` 配置执行一次同步(供插件/脚本调用)。

    与 build_adapters 一样兼容"完整配置"和"music_knowledge 段"两种入参。
    """
    music_cfg = config or {}
    if isinstance(music_cfg.get("music_knowledge"), dict):
        music_cfg = music_cfg["music_knowledge"]
    sync_cfg = music_cfg.get("sync") or {}
    mode = mode or sync_cfg.get("mode") or "fill_missing"
    limit = limit if limit is not None else sync_cfg.get("limit")
    max_fail = int(sync_cfg.get("max_fail", 30) or 30)
    song_db = music_cfg.get("song_database") or {}
    if song_db.get("db_folder") and song_db.get("db_file"):
        set_db_location(str(song_db["db_folder"]), str(song_db["db_file"]))
    adapters = build_adapters(music_cfg)
    if not adapters:
        raise RuntimeError("没有启用的歌曲来源, 请检查 music_knowledge.sync.sources")
    return sync_all(adapters, mode=mode, limit=limit, max_fail=max_fail, progress=progress)
