"""Single-writer market filesystem transactions, recovered before plugin discovery.

A pending operation keeps both versions in dedicated slots. Rename positions
identify the completed prefix even if the process exits between two calls.
Only terminal operations may discard slots. This is not a power-loss log or
coordination with other processes editing the same installation.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from loguru import logger

from mohobot.plugins._atomic_json import read_object, thread_until_done, write_object
from mohobot.plugins.installer import InstallError
from mohobot.plugins.sources import load_sources_strict, replace_source_record

_ID = re.compile(r"[0-9a-f]{32}")
_NAME = re.compile(r"[a-z0-9_]{1,64}")
_SCHEMA = 1
_FILE_SLOTS = frozenset({"config", "saved-config", "seed"})
Stamp = tuple[int, int]
Record = dict[str, Any] | None
Decision = Literal["pending", "committed", "rolled_back"]


class MarketRecoveryError(RuntimeError):
    """Market evidence cannot be reconciled; do not load the plugins."""


def _stat(path: Path) -> os.stat_result | None:
    # Check components without following a link. External concurrent edits are
    # not supported; the owning application controls these paths.
    for parent in reversed(path.parents):
        try:
            mode = parent.lstat().st_mode
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(mode):
            raise ValueError(f"不是普通目录: {parent}")
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _stamp(path: Path, *, directory: bool = True) -> Stamp | None:
    info = _stat(path)
    if info is None:
        return None
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError(f"事务路径类型不符: {path}")
    return info.st_dev, info.st_ino


def _move(source: Path, destination: Path) -> None:
    if _stat(destination) is not None:
        raise FileExistsError(f"事务目标已存在: {destination}")
    source.rename(destination)


def _discard(path: Path) -> None:
    if _stamp(path) is not None:
        shutil.rmtree(path)


@dataclass(frozen=True)
class _Intent:
    name: str
    kind: Literal["replace", "uninstall"]
    decision: Decision
    old: Record
    new: Record
    stamps: dict[str, Stamp | None]

    def payload(self) -> dict:
        return {"format": _SCHEMA, "name": self.name, "kind": self.kind,
                "decision": self.decision, "old": self.old, "new": self.new, "stamps": self.stamps}

    @classmethod
    def load(cls, path: Path) -> _Intent:
        obj = read_object(path)
        if (set(obj) != {"format", "name", "kind", "decision", "old", "new", "stamps"}
                or type(obj["format"]) is not int or obj["format"] != _SCHEMA):
            raise ValueError("未知市场事务日志格式")
        name, kind, decision = obj["name"], obj["kind"], obj["decision"]
        old, new, raw = obj["old"], obj["new"], obj["stamps"]
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("事务插件名非法")
        if decision not in ("pending", "committed", "rolled_back"):
            raise ValueError("未知事务决定")
        if (old is not None and not isinstance(old, dict)) or not isinstance(raw, dict):
            raise ValueError("事务来源记录或移动槽格式非法")
        if kind == "replace":
            if set(raw) != {"old", "candidate", "seed"} or raw["candidate"] is None or not isinstance(new, dict):
                raise ValueError("安装/更新事务槽或来源记录非法")
        elif kind == "uninstall":
            if set(raw) != {"old", "config", "data", "legacy"} or raw["old"] is None or new is not None:
                raise ValueError("卸载事务槽或来源记录非法")
        else:
            raise ValueError("未知事务类型")
        stamps = {}
        for slot, value in raw.items():
            if value is None:
                stamps[slot] = None
            elif isinstance(value, list) and len(value) == 2 and all(type(i) is int and i >= 0 for i in value):
                stamps[slot] = tuple(value)
            else:
                raise ValueError(f"事务槽 {slot} 的文件身份非法")
        present = [value for value in stamps.values() if value is not None]
        if len(present) != len(set(present)):
            raise ValueError("事务槽的文件身份重叠")
        return cls(name, kind, decision, old, new, stamps)


class MarketJournal:
    """One live operation at a time; caller holds market and lifecycle locks."""

    def __init__(self, data_dir: str | Path, plugins_dir: str | Path) -> None:
        self.data_dir = Path(os.path.abspath(data_dir))
        self.plugins_dir = Path(os.path.abspath(plugins_dir))
        self.root = self.plugins_dir / ".market-journal"

    def _enumerate(self) -> list[Path]:
        if _stamp(self.root) is None:
            return []
        active = []
        for path in sorted(self.root.iterdir()):
            if _ID.fullmatch(path.name):
                _stamp(path)
                active.append(path)
            elif re.fullmatch(r"\.(?:draft|done)-[0-9a-f]{32}", path.name):
                _stamp(path)
            else:
                raise ValueError(f"未知事务目录条目: {path}")
        if len(active) > 1:
            raise ValueError(f"存在多个未清理事务: {', '.join(p.name for p in active)}")
        return active

    def _error(self, exc: Exception, path: Path | None = None) -> MarketRecoveryError:
        return MarketRecoveryError(f"插件市场事务 {path or self.root}: {exc}")

    @staticmethod
    def _check_name(name: str) -> None:
        if not _NAME.fullmatch(name):
            raise InstallError("非法插件名")

    def _begin(self, intent: _Intent, candidate: Path | None = None, config_seed: dict | None = None) -> MarketOperation:
        if self._enumerate():
            raise ValueError("前一市场事务尚未清理")
        self.root.mkdir(parents=True, exist_ok=True)
        op_id = uuid4().hex
        active = self.root / op_id
        operation = MarketOperation(self, active, intent)
        if intent.kind == "uninstall" and _stat(operation.recycle_path) is not None:
            raise FileExistsError(f"事务目标已存在: {operation.recycle_path}")
        draft = self.root / f".draft-{op_id}"
        draft.mkdir()
        try:
            if candidate is not None:
                if _stamp(candidate) != intent.stamps["candidate"]:
                    raise ValueError(f"候选目录已变更: {candidate}")
                _move(candidate, draft / "candidate")
            if config_seed is not None:
                live = self.data_dir / "plugins_config" / f"{intent.name}.json"
                if _stamp(live, directory=False) is None:
                    seed = draft / "config-seed"
                    write_object(seed, config_seed)
                    intent = replace(intent, stamps={**intent.stamps, "seed": _stamp(seed, directory=False)})
            write_object(draft / "journal.json", intent.payload())
            _move(draft, active)
        finally:
            _discard(draft)
        return MarketOperation(self, active, intent)

    async def begin_replace(
        self, name: str, candidate: Path, new: dict[str, Any], *, expect_installed: bool,
        config_seed: dict | None = None,
    ) -> MarketOperation:
        self._check_name(name)
        candidate = Path(os.path.abspath(candidate))
        try:
            relative = candidate.relative_to(self.plugins_dir)
            if not relative.parts or not relative.parts[0].startswith(".") or relative.parts[0] == self.root.name:
                raise ValueError(f"候选目录不在不可发现的暂存路径: {candidate}")
            current = await thread_until_done(_stamp, self.plugins_dir / name)
            if (current is not None) != expect_installed:
                raise InstallError(f"插件{'未安装' if expect_installed else '已安装'}: {name}")
            staged = await thread_until_done(_stamp, candidate)
            if staged is None:
                raise ValueError(f"候选目录缺失: {candidate}")
            old = (await load_sources_strict(self.data_dir)).get(name)
            intent = _Intent(name, "replace", "pending", old, new, {"old": current, "candidate": staged, "seed": None})
            return await thread_until_done(self._begin, intent, candidate, config_seed)
        except (OSError, ValueError) as exc:
            raise self._error(exc) from exc

    async def begin_uninstall(self, name: str, *, delete_config: bool, delete_data: bool) -> MarketOperation:
        self._check_name(name)
        try:
            live = await thread_until_done(_stamp, self.plugins_dir / name)
            if live is None:
                raise InstallError(f"插件未安装: {name}")

            def stamps():
                return {"old": live,
                        "config": _stamp(self.data_dir / "plugins_config" / f"{name}.json", directory=False) if delete_config else None,
                        "data": _stamp(self.data_dir / "plugins_data" / name) if delete_data else None,
                        "legacy": _stamp(self.data_dir / "plugin_data" / name) if delete_data else None}

            old = (await load_sources_strict(self.data_dir)).get(name)
            intent = _Intent(name, "uninstall", "pending", old, None, await thread_until_done(stamps))
            return await thread_until_done(self._begin, intent)
        except (OSError, ValueError) as exc:
            raise self._error(exc) from exc

    async def recover(self) -> None:
        try:
            paths = await thread_until_done(self._enumerate)
            for path in paths:
                try:
                    intent = await thread_until_done(_Intent.load, path / "journal.json")
                except (OSError, ValueError) as exc:
                    raise self._error(exc, path) from exc
                op = MarketOperation(self, path, intent)
                if op.intent.decision == "pending":
                    await op.rollback()
                await op.cleanup()
            if self.root.is_dir():
                for path in self.root.iterdir():
                    # Neither draft nor done has any outstanding live move.
                    await thread_until_done(_discard, path)
        except (OSError, ValueError, MarketRecoveryError) as exc:
            error = exc if isinstance(exc, MarketRecoveryError) else self._error(exc)
            logger.error(str(error))
            if error is exc:
                raise
            raise error from exc


class MarketOperation:
    def __init__(self, journal: MarketJournal, path: Path, intent: _Intent) -> None:
        self.journal = journal
        self.path = path
        self.intent = intent
        self._cleanup_renamed = False

    @property
    def recycle_path(self) -> Path:
        return self.journal.data_dir / "plugin_recycle" / f"{self.intent.name}-{self.path.name}"

    def _layout(self):
        """Return fixed path labels, legal rename steps, and their full layouts."""
        name, stamps = self.intent.name, self.intent.stamps
        data = self.journal.data_dir
        paths = {"live": self.journal.plugins_dir / name}
        initial = {"live": stamps["old"]}
        moves = []
        if self.intent.kind == "replace":
            paths.update(previous=self.path / "previous", candidate=self.path / "candidate")
            initial.update(previous=None, candidate=stamps["candidate"])
            if stamps["old"] is not None:
                moves.append(("live", "previous"))
            moves.append(("candidate", "live"))
            paths["seed"] = self.path / "config-seed"
            initial["seed"] = stamps["seed"]
            if stamps["seed"] is not None:
                paths["config"] = data / "plugins_config" / f"{name}.json"
                initial["config"] = None
                moves.append(("seed", "config"))
        else:
            paths.update(recycle=self.recycle_path)
            initial["recycle"] = None
            moves.append(("live", "recycle"))
            for slot, path in (("config", data / "plugins_config" / f"{name}.json"),
                               ("data", data / "plugins_data" / name), ("legacy", data / "plugin_data" / name)):
                staged = f"saved-{slot}"
                paths[staged] = self.path / staged
                initial[staged] = None
                if stamps[slot] is not None:
                    paths[slot] = path
                    initial[slot] = stamps[slot]
                    moves.append((slot, staged))
        layouts = [initial]
        for src, dst in moves:
            layouts.append({**layouts[-1], src: None, dst: layouts[-1][src]})
        return paths, moves, layouts

    def _current_layout(self, paths, *, allow_live_data: bool = False):
        private = {path.name for path in paths.values() if path.parent == self.path}
        for item in self.path.iterdir():
            if item.name not in private | {"journal.json"} and not re.fullmatch(r"\.journal\.json-[a-z0-9_]{8}", item.name):
                raise ValueError(f"未知事务槽: {item}")
            if item.name not in private:
                _stamp(item, directory=False)
        return {label: _stamp(path, directory=label not in _FILE_SLOTS)
                for label, path in paths.items() if not (allow_live_data and label in {"config", "data", "legacy"})}

    def _position(self, paths, layouts) -> int:
        actual = self._current_layout(paths)
        try:
            return layouts.index(actual)
        except ValueError as exc:
            raise ValueError(f"事务文件与记录的移动槽不一致: {actual}") from exc

    def _move_files(self, *, reverse: bool) -> None:
        paths, moves, layouts = self._layout()
        position = self._position(paths, layouts)
        steps = (
            [(b, a, layouts[i + 1][b]) for i, (a, b) in reversed(list(enumerate(moves[:position])))]
            if reverse else
            [(a, b, layouts[i][a]) for i, (a, b) in enumerate(moves[position:], start=position)]
        )
        # Detect cross-volume moves before touching a live directory, including
        # when a selected path is a mount or the recycle parent is missing.
        for src, dst, stamp in steps:
            parent = paths[dst].parent
            while (target := _stamp(parent)) is None:
                parent = parent.parent
            if stamp is None or target[0] != stamp[0]:
                raise OSError(f"事务路径不在同一文件系统: {paths[src]} → {paths[dst]}")
        for src, dst, _ in steps:
            paths[dst].parent.mkdir(parents=True, exist_ok=True)
            _move(paths[src], paths[dst])

    async def _source(self) -> Record:
        return (await load_sources_strict(self.journal.data_dir)).get(self.intent.name)

    async def _replace_source(self, current: Record, desired: Record) -> None:
        await replace_source_record(self.journal.data_dir, self.intent.name, expected=current, record=desired)

    async def apply(self) -> None:
        try:
            if self.intent.decision != "pending" or (current := await self._source()) not in (self.intent.old, self.intent.new):
                raise ValueError("事务状态或来源记录已改变")
            await thread_until_done(self._apply_files)
            await self._replace_source(current, self.intent.new)
        except (OSError, ValueError) as exc:
            raise self.journal._error(exc, self.path) from exc

    def _apply_files(self) -> None:
        self._move_files(reverse=False)

    def _rollback_files(self) -> None:
        self._move_files(reverse=True)

    def _write_decision(self, decision: Decision) -> None:
        terminal = replace(self.intent, decision=decision)
        write_object(self.path / "journal.json", terminal.payload())
        self.intent = terminal

    async def rollback(self) -> None:
        try:
            if self.intent.decision != "pending" or (current := await self._source()) not in (self.intent.old, self.intent.new):
                raise ValueError("无法回退：事务状态或来源记录已改变")
            await thread_until_done(self._rollback_files)
            await self._replace_source(current, self.intent.old)
            await thread_until_done(self._write_decision, "rolled_back")
        except (OSError, ValueError) as exc:
            raise self.journal._error(exc, self.path) from exc

    def _commit_files(self) -> None:
        paths, moves, layouts = self._layout()
        if self._position(paths, layouts) != len(moves):
            raise ValueError("文件移动未完成，不能提交")
        self._write_decision("committed")

    async def commit(self) -> None:
        try:
            if self.intent.decision != "pending" or await self._source() != self.intent.new:
                raise ValueError("来源记录未提交或事务已终结")
            await thread_until_done(self._commit_files)
        except (OSError, ValueError) as exc:
            raise self.journal._error(exc, self.path) from exc

    def _clean_slots(self) -> None:
        done = self.path.with_name(f".done-{self.path.name}")
        if self._cleanup_renamed:
            _discard(done)
            return
        paths, _, layouts = self._layout()
        committed = self.intent.decision == "committed"
        desired = layouts[-1] if committed else layouts[0]
        actual = self._current_layout(paths, allow_live_data=committed and self.intent.kind == "uninstall")
        for label, value in actual.items():
            if value != desired[label] and not (value is None and paths[label].parent == self.path):
                raise ValueError(f"终态事务槽与文件不符: {paths[label]}")
        for label, value in actual.items():
            path = paths[label]
            if value is not None and path.parent == self.path:
                if label in _FILE_SLOTS:
                    path.unlink()
                else:
                    shutil.rmtree(path)
        _move(self.path, done)
        self._cleanup_renamed = True
        _discard(done)

    async def cleanup(self) -> None:
        try:
            if self.intent.decision == "pending":
                raise ValueError("未完成的事务不能清理")
            expected = self.intent.new if self.intent.decision == "committed" else self.intent.old
            if await self._source() != expected:
                raise ValueError("来源记录与事务终态不一致")
            await thread_until_done(self._clean_slots)
        except (OSError, ValueError) as exc:
            raise self.journal._error(exc, self.path) from exc
