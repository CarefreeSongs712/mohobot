"""多来源歌曲记录合并 — 身份对齐 + 字段级补全 + 冲突记录。

设计依据(实测):
- 名称不能当唯一键: 本地库去括号后有 81 个同名冲突组, 其中 50 组演唱集合
  不同, 是不同歌曲。因此对齐采用"强标识优先 + 多信号确认"。
- 字段优先级按来源实测可得率排:
    歌词/演唱/歌名  VCPedia > voca.wiki > 萌娘百科
    词曲编混调/PV/曲绘/简介  萌娘百科 > voca.wiki > VCPedia
    年份/BV 号  萌娘百科 > voca.wiki > VCPedia
  (萌百 60 条抽样: 年份 100%、歌词 98%、作词 73%、作曲 70%、编曲 68%;
   voca.wiki: Songbox 演唱 100%、Introduction 72.5%、LyricsKai 82.5%;
   VCPedia 是现有库来源, 龙天依曲最全。)
- 非空值永不被空值覆盖; 同一字段多来源冲突时保留优先级高者并把差异写入
  SongSource.fields_json 的 `_conflicts`, 便于人工复核(不静默丢弃)。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from mohobot.music_knowledge.normalize import (
    bracket_core,
    classify_suffix,
    clean_year,
    detect_language,
    is_version_name,
    normalize_name,
    singers_key,
    singers_overlap,
)
from mohobot.music_knowledge.song_database import Song, SongSource
from mohobot.music_knowledge.sources.base import SourceRecord

# 单值字段: 按来源优先级择一
SINGLE_FIELDS = (
    "name", "singers", "uploader", "lyricist", "composer", "arranger",
    "mixer", "tuner", "mastering", "pv", "illustrator", "year",
    "introduction", "lyrics", "bvid",
)

# 每个字段的来源优先级(靠前者胜出)
FIELD_PRIORITY: dict[str, tuple[str, ...]] = {
    "lyrics": ("vcpedia", "vocawiki", "moegirl"),
    "singers": ("vcpedia", "vocawiki", "moegirl"),
    "name": ("vcpedia", "vocawiki", "moegirl"),
    "uploader": ("moegirl", "vocawiki", "vcpedia"),
    "lyricist": ("moegirl", "vocawiki", "vcpedia"),
    "composer": ("moegirl", "vocawiki", "vcpedia"),
    "arranger": ("moegirl", "vocawiki", "vcpedia"),
    "mixer": ("moegirl", "vocawiki", "vcpedia"),
    "tuner": ("moegirl", "vocawiki", "vcpedia"),
    "mastering": ("moegirl", "vocawiki", "vcpedia"),
    "pv": ("moegirl", "vocawiki", "vcpedia"),
    "illustrator": ("moegirl", "vocawiki", "vcpedia"),
    "introduction": ("moegirl", "vocawiki", "vcpedia"),
    "year": ("moegirl", "vocawiki", "vcpedia"),
    "bvid": ("moegirl", "vocawiki", "vcpedia"),
}

CONFLICT_KEY = "_conflicts"


def _priority(field_name: str, source: str) -> int:
    order = FIELD_PRIORITY.get(field_name, ())
    try:
        return order.index(source)
    except ValueError:
        return len(order)


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


@dataclass
class MergeResult:
    """一次合并的统计, 供同步任务汇报。"""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    filled_fields: int = 0
    conflicts: int = 0
    unmatched: int = 0
    # 逐条明细(限制长度, 避免内存膨胀)
    created_names: list[str] = field(default_factory=list)
    unmatched_titles: list[str] = field(default_factory=list)
    conflict_samples: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "filled_fields": self.filled_fields,
            "conflicts": self.conflicts,
            "unmatched": self.unmatched,
            "created_names": self.created_names[:20],
            "unmatched_titles": self.unmatched_titles[:20],
            "conflict_samples": self.conflict_samples[:10],
        }


class _SharedIdentityIndex:
    """进程级共享身份索引: 让并行运行的多个来源看到彼此新建的歌曲。

    存的是纯数据快照(uuid/name/normalized/bvid), 不是 ORM 实体:
    不同来源各用独立 Session, 跨 Session 持有 ORM 对象会 DetachedInstanceError。
    命中后由当前 Session 按 uuid 重新取实体。
    """

    def __init__(self) -> None:
        self.by_norm: dict[str, list[str]] = {}
        self.by_core: dict[str, list[str]] = {}
        self.by_bvid: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.users = 0
        self.loaded = False

    def clear(self) -> None:
        self.by_norm.clear()
        self.by_core.clear()
        self.by_bvid.clear()
        self.names.clear()
        self.loaded = False

    def index(self, song: Song) -> None:
        uid = song.uuid
        if not uid:
            return
        self.names[uid] = song.name or ""
        norm = (song.normalized_name or "").strip() or normalize_name(song.name)
        if norm:
            bucket = self.by_norm.setdefault(norm, [])
            if uid not in bucket:
                bucket.append(uid)
        core = normalize_name(bracket_core(song.name))
        if core and core != norm:
            bucket = self.by_core.setdefault(core, [])
            if uid not in bucket:
                bucket.append(uid)
        if song.bvid:
            self.by_bvid[song.bvid] = uid

    def unindex(self, song: Song) -> None:
        uid = song.uuid
        if not uid:
            return
        norm = (song.normalized_name or "").strip() or normalize_name(song.name)
        core = normalize_name(bracket_core(song.name))
        for key, table in ((norm, self.by_norm), (core, self.by_core)):
            bucket = table.get(key)
            if bucket and uid in bucket:
                bucket.remove(uid)
                if not bucket:
                    table.pop(key, None)
        if song.bvid and self.by_bvid.get(song.bvid) == uid:
            self.by_bvid.pop(song.bvid, None)
        self.names.pop(uid, None)


class SongMerger:
    """把来源记录写进 songs 宽表, 并维护 song_sources 溯源行。

    多来源并行时必须注意: 两个来源同时抓到同一首新歌时, 各自都查不到对方
    尚未提交的行, 会各建一条重复记录。因此 merge() 内部用类级锁串行化
    "查重 + 写入"这段毫秒级临界区; 网络抓取仍在锁外并行。
    """

    # 进程级合并锁: 保护"身份查找 + 新建/更新"的原子性。
    # 锁只覆盖数据库读写, 不包含任何网络/解析动作。
    _merge_lock = threading.RLock()
    # 进程级共享身份索引(纯数据快照; 引用计数, 最后一个使用者释放时清空)
    _shared = _SharedIdentityIndex()

    def __init__(self, db: Session, *, max_delta: int = 4000) -> None:
        self._db = db
        # 歌词差异超过该字数时不覆盖, 只记冲突(防把完整歌词换成残页)
        self._max_delta = max_delta
        self._loaded = False
        SongMerger._shared.users += 1

    @property
    def _index(self) -> _SharedIdentityIndex:
        return SongMerger._shared

    def close(self) -> None:
        """释放引用; 最后一个使用者退出时清空共享索引。"""
        if self._loaded is None:
            return
        self._loaded = None
        SongMerger._shared.users = max(0, SongMerger._shared.users - 1)
        if SongMerger._shared.users == 0:
            SongMerger._shared.clear()

    def __enter__(self) -> "SongMerger":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ── 索引 ────────────────────────────────────────────────

    def load_index(self, *, force: bool = False) -> None:
        """载入共享索引; 已由其它来源加载过则直接复用。"""
        with self._merge_lock:
            if self._index.loaded and not force:
                self._loaded = True
                return
            songs = self._db.query(Song).all()
            self._index.clear()
            for song in songs:
                self._index.index(song)
            self._index.loaded = True
            self._loaded = True

    def _index_song(self, song: Song) -> None:
        self._index.index(song)

    def _unindex_song(self, song: Song) -> None:
        self._index.unindex(song)

    def _entities(self, uuids: list[str]) -> list[Song]:
        """按 uuid 在当前 Session 取实体(保持传入顺序, 忽略已删除的)。"""
        if not uuids:
            return []
        rows = self._db.query(Song).filter(Song.uuid.in_(uuids)).all()
        by_uuid = {row.uuid: row for row in rows}
        return [by_uuid[uid] for uid in uuids if uid in by_uuid]

    # ── 身份对齐 ────────────────────────────────────────────

    def find_matches(self, record: SourceRecord) -> list[Song]:
        """返回候选歌曲(0 个=新歌, 1 个=确定, >1=歧义需多信号消解)。"""
        if not self._loaded:
            self.load_index()
        index = self._index
        bvid = (record.bvid or "").strip()
        if bvid and bvid in index.by_bvid:
            found = self._entities([index.by_bvid[bvid]])
            if found:
                return found

        names = {normalize_name(record.name), normalize_name(record.source_title)}
        names.discard("")
        uuids: list[str] = []
        for name in names:
            uuids.extend(index.by_norm.get(name, []))
        if not uuids:
            core = normalize_name(bracket_core(record.name or record.source_title))
            if core:
                uuids.extend(index.by_core.get(core, []))
        # 去重保序
        seen: set[str] = set()
        unique: list[str] = []
        for uid in uuids:
            if uid in seen:
                continue
            seen.add(uid)
            unique.append(uid)
        return self._entities(unique)

    def _resolve(self, record: SourceRecord, candidates: list[Song]) -> tuple[Song | None, str]:
        """在候选中选出唯一目标; 选不出返回 (None, 原因)。

        规则: 演唱集合有交集 > 年份一致 > 唯一候选;
        同名但演唱集合完全不相交视为不同歌曲(本地库有 50 组这类同名字);
        版本名(中文版/翻唱/remix)不同也不合并; 仍歧义则放弃并记录。
        """
        if not candidates:
            return None, "new"
        if len(candidates) == 1:
            only = candidates[0]
            existing_singers = (only.singers or "").strip()
            incoming_singers = (record.singers or "").strip()
            if existing_singers and incoming_singers and not singers_overlap(
                incoming_singers, existing_singers
            ):
                # 同名不同歌手: 极可能是另一首歌(同名曲), 必须新建而不是合并
                return None, "singer_mismatch"
            if is_version_name(record.name) != is_version_name(only.name) and not singers_overlap(
                incoming_singers, existing_singers
            ):
                return None, "version_mismatch"
            return only, "single"

        year = clean_year(record.year)
        scored: list[tuple[int, Song]] = []
        for song in candidates:
            existing_singers = (song.singers or "").strip()
            incoming_singers = (record.singers or "").strip()
            overlap = singers_overlap(incoming_singers, existing_singers)
            if existing_singers and incoming_singers and not overlap:
                # 同名但演唱者完全不同: 视为不同歌曲, 不参与打分
                continue
            score = 2 if overlap else 0
            if year and song.year and year == song.year:
                score += 1
            if record.uploader and song.uploader and normalize_name(
                record.uploader
            ) == normalize_name(song.uploader):
                score += 1
            if is_version_name(record.name) == is_version_name(song.name):
                score += 1
            scored.append((score, song))
        if not scored:
            return None, "singer_mismatch"
        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else -1
        if best_score >= 2 and best_score > runner_up:
            return best, "scored"
        return None, "ambiguous"

    # ── 合并 ────────────────────────────────────────────────

    def merge(self, record: SourceRecord, result: MergeResult) -> Song | None:
        """合并一条来源记录; 返回目标歌曲(歧义/无法对齐时返回 None)。

        临界区(查重 + 新建/更新 + 溯源行)由类级锁串行化, 因此多个来源
        可以并行抓取、并发调用本方法而不会产生重复歌曲。
        """
        with self._merge_lock:
            candidates = self.find_matches(record)
            song, reason = self._resolve(record, candidates)
            if song is None:
                if reason == "new":
                    song = self._create(record)
                    result.created += 1
                    if len(result.created_names) < 20:
                        result.created_names.append(song.name)
                elif reason == "singer_mismatch":
                    # 同名不同歌手: 新建为该歌手的同名曲, 不覆盖既有记录
                    song = self._create(record)
                    result.created += 1
                    if len(result.created_names) < 20:
                        result.created_names.append(f"{song.name}({record.singers})")
                    self._save_source(record, song, "matched")
                    return song
                else:
                    result.unmatched += 1
                    if len(result.unmatched_titles) < 20:
                        result.unmatched_titles.append(record.source_title)
                    self._save_source(record, None, reason)
                    return None
            else:
                changed = self._apply_fields(song, record, result)
                if changed:
                    result.updated += 1
                else:
                    result.unchanged += 1

            self._save_source(record, song, "matched")
            return song

    def merge_and_commit(self, record: SourceRecord, result: MergeResult) -> Song | None:
        """合并并在**同一把锁内**提交; 并行来源必须用它而不是 merge()+外部 commit()。

        `merge()` 在锁内 flush 并把新歌写进进程级共享索引, 但事务仍未提交。
        若调用方在锁释放之后才 commit, 另一来源这时按共享索引命中该歌, 却用自己
        的 Session 查不到那行(事务隔离), 于是又新建一条, 产生重复歌曲。
        所以"查重 → 写入 → 提交"必须是同一个临界区。

        提交失败时回滚 Session, 并按库内真实状态重建共享索引: `_create()` 已把
        未落库的歌写进索引, 不清理会让其它来源命中一个不存在的 uuid。
        """
        with self._merge_lock:
            try:
                song = self.merge(record, result)
                self._db.commit()
                return song
            except Exception:
                self._db.rollback()
                # 回滚后库里没有本次新建的行; 索引必须与库一致。重建失败不能盖住
                # 原始异常(调用方要看到真正的提交/合并错误)。
                try:
                    self.load_index(force=True)
                except Exception as reload_error:  # noqa: BLE001
                    logger.warning(f"提交失败后重建身份索引也失败: {reload_error}")
                raise

    def _create(self, record: SourceRecord) -> Song:
        fields = record.fields()
        name = (record.name or record.source_title).strip()
        song = Song(
            name=name,
            safe_name="".join(c for c in name if c.isalnum() or c in (" ", "-", "_")).strip(),
            introduction=record.introduction or "",
            lyrics=record.lyrics or "",
        )
        for key in SINGLE_FIELDS:
            if key in ("name", "introduction", "lyrics"):
                continue
            value = fields.get(key)
            if not _is_empty(value):
                setattr(song, key, clean_year(value) if key == "year" else value)
        song.aliases = json.dumps(self._aliases(record), ensure_ascii=False)
        song.lang = self._language(record)
        song.normalized_name = normalize_name(name)
        song.primary_source = record.source
        song.updated_at = datetime.now()
        self._db.add(song)
        self._db.flush()
        self._index_song(song)
        return song

    def _apply_fields(self, song: Song, record: SourceRecord, result: MergeResult) -> bool:
        """按字段优先级补全/替换; 返回是否有实际变更。"""
        changed = False
        fields = record.fields()
        conflicts: dict[str, Any] = {}

        for key in SINGLE_FIELDS:
            value = fields.get(key)
            if _is_empty(value):
                continue
            if key == "year":
                value = clean_year(value)
                if value is None:
                    continue
            current = getattr(song, key, None)
            if _is_empty(current):
                setattr(song, key, value)
                result.filled_fields += 1
                changed = True
                continue
            if str(current).strip() == str(value).strip():
                continue
            # 歌词: 差异过大时不覆盖, 只记录冲突
            if key == "lyrics":
                current_len, new_len = len(str(current)), len(str(value))
                if abs(current_len - new_len) > self._max_delta and new_len < current_len:
                    conflicts[key] = {"kept_len": current_len, "incoming_len": new_len,
                                      "incoming_source": record.source}
                    continue
            current_source = song.primary_source or ""
            if _priority(key, record.source) < _priority(key, current_source):
                setattr(song, key, value)
                changed = True
                conflicts[key] = {
                    "replaced_source": current_source or "unknown",
                    "new_source": record.source,
                }
            else:
                conflicts[key] = {
                    "kept_source": current_source or "unknown",
                    "rejected_source": record.source,
                }

        # 别名合并(只增不减)
        aliases = self._existing_aliases(song)
        merged_aliases = list(dict.fromkeys(aliases + self._aliases(record)))
        if merged_aliases != aliases:
            song.aliases = json.dumps(merged_aliases, ensure_ascii=False)
            changed = True

        if not (song.lang or "").strip():
            song.lang = self._language(record)
            changed = True
        if not (song.normalized_name or "").strip():
            song.normalized_name = normalize_name(song.name)
            changed = True
        if not (song.primary_source or "").strip():
            song.primary_source = record.source
            changed = True

        if conflicts:
            result.conflicts += len(conflicts)
            if len(result.conflict_samples) < 10:
                result.conflict_samples.append(
                    {"song": song.name, "source": record.source, "conflicts": conflicts}
                )

        if changed:
            song.updated_at = datetime.now()
        return changed

    # ── 辅助 ────────────────────────────────────────────────

    @staticmethod
    def _aliases(record: SourceRecord) -> list[str]:
        candidates = [record.name, record.source_title, *record.aliases]
        seen: set[str] = set()
        out: list[str] = []
        for item in candidates:
            name = (item or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
        return out

    @staticmethod
    def _existing_aliases(song: Song) -> list[str]:
        try:
            value = json.loads(song.aliases or "[]")
            return [str(x) for x in value] if isinstance(value, list) else []
        except (TypeError, ValueError):
            return []

    @staticmethod
    def _language(record: SourceRecord) -> str:
        value = (record.extra or {}).get("lang")
        if value:
            return str(value)
        return detect_language(record.lyrics or record.name)

    def _save_source(self, record: SourceRecord, song: Song | None, status: str) -> None:
        """写入/更新溯源行; 同 (source, source_title) 只保留最新一次。"""
        row = (
            self._db.query(SongSource)
            .filter(SongSource.source == record.source,
                    SongSource.source_title == record.source_title)
            .first()
        )
        payload = json.dumps(
            {k: v for k, v in record.fields().items() if not _is_empty(v)},
            ensure_ascii=False, default=str,
        )
        if row is None:
            row = SongSource(
                song_uuid=song.uuid if song else None,
                source=record.source,
                source_title=record.source_title,
            )
            self._db.add(row)
        row.song_uuid = song.uuid if song else row.song_uuid
        row.source_url = record.source_url or row.source_url
        row.license = record.license or row.license
        row.content_hash = record.content_hash()
        row.fields_json = payload
        row.fetched_at = datetime.now()
        row.status = status
        row.error = None if status == "matched" else status

    def commit(self) -> None:
        self._db.commit()


def normalize_singers_key(value: str) -> str:
    """对外暴露的歌手集合键(测试/脚本用)。"""
    return singers_key(value)


def classify_disambiguation(name: str) -> str:
    """对外暴露的后缀分类(测试/脚本用)。"""
    return classify_suffix(name)
