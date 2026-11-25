"""On-disk transaction recovery with synthetic plugins and abrupt child exits."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from mohobot.plugin_market.journal import MarketJournal, MarketRecoveryError
from mohobot.plugins.sources import load_sources_strict, save_source, sources_path

OLD = {"schema_version": 1, "version": "1.0.0", "extra": {"keep": True}}
NEW = {"schema_version": 1, "version": "2.0.0"}
PEER = {"schema_version": 1, "version": "9.0.0"}


def _plugin(path: Path, version: str) -> None:
    path.mkdir(parents=True)
    (path / "main.py").write_text(f"VERSION = {version!r}\n", encoding="utf-8")


def _fixture(tmp_path, *, installed=True):
    data = tmp_path / "data"
    plugins = data / "plugins"
    plugins.mkdir(parents=True)
    if installed:
        _plugin(plugins / "demo", "1.0.0")
        asyncio.run(save_source(data, "demo", OLD))
    asyncio.run(save_source(data, "peer", PEER))
    return data, plugins


_CHILD = '''
import asyncio
import os
import sys
from pathlib import Path

from mohobot.plugin_market import journal

async def main():
    data, plugins = map(Path, sys.argv[1:3])
    kind, cut = sys.argv[3:5]
    txns = journal.MarketJournal(data, plugins)
    if kind == "uninstall":
        op = await txns.begin_uninstall("demo", delete_config=True, delete_data=True)
    else:
        op = await txns.begin_replace("demo", plugins / ".fixture-candidate", {
            "schema_version": 1, "version": "2.0.0",
        }, expect_installed=kind != "install", config_seed={"mode": "default"} if kind == "seed" else None)
    if cut == "intent":
        os._exit(77)
    if cut.startswith("rename-"):
        remaining = int(cut.split("-")[1])
        real = journal._move
        def move(a, b):
            nonlocal remaining
            real(a, b)
            remaining -= 1
            if not remaining:
                os._exit(77)
        journal._move = move
    await op.apply()
    if cut == "source":
        os._exit(77)
    if cut == "rollback-rename":
        real = journal._move
        def move(a, b):
            real(a, b)
            os._exit(77)
        journal._move = move
        await op.rollback()
    await op.commit()
    if cut == "commit":
        os._exit(77)
    if cut == "cleanup":
        real = journal.shutil.rmtree
        def remove(path, *args, **kwargs):
            real(path, *args, **kwargs)
            os._exit(77)
        journal.shutil.rmtree = remove
        await op.cleanup()
    raise AssertionError(f"did not reach {kind}/{cut}")

asyncio.run(main())
'''


def _exit_mid_operation(data, plugins, kind, cut):
    run = subprocess.run(
        [sys.executable, "-c", _CHILD, str(data), str(plugins), kind, cut],
        check=False, capture_output=True, text=True, timeout=20,
    )
    assert run.returncode == 77, (run.stdout, run.stderr)


def _probe_replace_recovers_after_abrupt_exit(tmp_path, cut):
    data, plugins = _fixture(tmp_path)
    _plugin(plugins / ".fixture-candidate", "2.0.0")
    _exit_mid_operation(data, plugins, "update", cut)
    expected = NEW if cut in {"commit", "cleanup"} else OLD
    # A peer write must survive rollback of this target's old/new record.
    asyncio.run(save_source(data, "peer", {**PEER, "updated": True}))
    journal = MarketJournal(data, plugins)
    for _ in range(2):
        asyncio.run(journal.recover())
        assert (plugins / "demo" / "main.py").read_text() == f"VERSION = {expected['version']!r}\n"
        assert asyncio.run(load_sources_strict(data)) == {
            "demo": expected, "peer": {**PEER, "updated": True},
        }
    assert not list(journal.root.iterdir())


def test_uncommitted_first_install_is_removed_on_recovery(tmp_path):
    data, plugins = _fixture(tmp_path, installed=False)
    _plugin(plugins / ".fixture-candidate", "2.0.0")
    _exit_mid_operation(data, plugins, "install", "source")
    journal = MarketJournal(data, plugins)
    asyncio.run(journal.recover())
    assert not (plugins / "demo").exists()
    assert asyncio.run(load_sources_strict(data)) == {"peer": PEER}


def _probe_uninstall_recovery_is_reversible_and_cleanup_keeps_new_data(tmp_path, cut):
    data, plugins = _fixture(tmp_path)
    config = data / "plugins_config" / "demo.json"
    config.parent.mkdir()
    config.write_text('{"old": true}')
    old_data = [data / layout / "demo" for layout in ("plugins_data", "plugin_data")]
    for path in old_data:
        path.mkdir(parents=True)
        (path / "original").touch()
    recycle = data / "plugin_recycle"
    _plugin(recycle / "demo-older-entry", "0.1.0")
    _exit_mid_operation(data, plugins, "uninstall", cut)
    journal = MarketJournal(data, plugins)
    committed = cut in {"commit", "cleanup"}
    if committed:
        config.write_text('{"new": true}')
        for path in old_data:
            path.mkdir()
            (path / "replacement").touch()
    for _ in range(2):
        asyncio.run(journal.recover())
    assert (recycle / "demo-older-entry" / "main.py").read_text() == "VERSION = '0.1.0'\n"
    if committed:
        assert not (plugins / "demo").exists()
        assert len(list(recycle.iterdir())) == 2
        assert json.loads(config.read_text()) == {"new": True}
        assert all({item.name for item in path.iterdir()} == {"replacement"} for path in old_data)
        assert asyncio.run(load_sources_strict(data)) == {"peer": PEER}
    else:
        assert (plugins / "demo" / "main.py").is_file()
        assert len(list(recycle.iterdir())) == 1
        assert json.loads(config.read_text()) == {"old": True}
        assert all((path / "original").is_file() for path in old_data)
        assert asyncio.run(load_sources_strict(data)) == {"demo": OLD, "peer": PEER}
    assert not list(journal.root.iterdir())


def _probe_ambiguous_state_stops_recovery_without_destroying_evidence(tmp_path, damage):
    data, plugins = _fixture(tmp_path)
    _plugin(plugins / ".fixture-candidate", "2.0.0")
    _exit_mid_operation(data, plugins, "update", "source")
    journal = MarketJournal(data, plugins)
    op, = journal.root.iterdir()
    descriptor = op / "journal.json"
    if damage == "journal":
        descriptor.write_text('{"format": 99}')
    elif damage == "source":
        asyncio.run(save_source(data, "demo", {"version": "unknown"}))
    elif damage == "destination":
        (plugins / "demo").rename(plugins / ".moved-away")
        _plugin(plugins / "demo", "unknown")
    else:
        (plugins / "demo").rename(plugins / ".moved-away")
        (plugins / "demo").symlink_to(plugins / ".moved-away", target_is_directory=True)
    evidence = descriptor.read_bytes()
    source = sources_path(data).read_bytes()
    live = (plugins / "demo" / "main.py").read_bytes()
    with pytest.raises(MarketRecoveryError, match=op.name):
        asyncio.run(journal.recover())
    assert descriptor.read_bytes() == evidence
    assert sources_path(data).read_bytes() == source
    assert (plugins / "demo" / "main.py").read_bytes() == live
    assert (op / "previous" / "main.py").is_file()


def _probe_config_seed_moves_with_candidate_but_existing_archive_is_untouched(tmp_path, cut, exists):
    if exists and cut == "rename-3":
        # The existing archive means there are just two renames to interrupt.
        cut = "rename-2"
    data, plugins = _fixture(tmp_path)
    _plugin(plugins / ".fixture-candidate", "2.0.0")
    config = data / "plugins_config" / "demo.json"
    original = '{ "private_option": false }\n'
    if exists:
        config.parent.mkdir()
        config.write_text(original)
    _exit_mid_operation(data, plugins, "seed", cut)
    asyncio.run(MarketJournal(data, plugins).recover())
    if exists:
        assert config.read_text() == original
    elif cut == "commit":
        assert json.loads(config.read_text()) == {"mode": "default"}
    else:
        assert not config.exists()


def _probe_cleanup_retries_final_discard_in_same_process(tmp_path, monkeypatch, committed):
    from mohobot.plugin_market import journal as module

    async def run():
        txns = MarketJournal(data, plugins)
        op = await txns.begin_uninstall("demo", delete_config=False, delete_data=False)
        await op.apply()
        if committed:
            await op.commit()
        else:
            await op.rollback()
        discard = module._discard
        failed = False

        def transient(path):
            nonlocal failed
            if path.name.startswith(".done-") and not failed:
                failed = True
                raise PermissionError("fixture final discard")
            return discard(path)

        monkeypatch.setattr(module, "_discard", transient)
        with pytest.raises(MarketRecoveryError):
            await op.cleanup()
        assert failed and not op.path.exists()
        assert op.path.with_name(f".done-{op.path.name}").is_dir()
        for _ in range(2):
            await op.cleanup()
        assert not list(txns.root.iterdir())
        assert (await load_sources_strict(data)).get("demo") == (None if committed else OLD)
        assert (plugins / "demo").exists() != committed

    data, plugins = _fixture(tmp_path)
    asyncio.run(run())


def test_uninstall_refuses_recycle_collision_before_moving_live_files(tmp_path):
    async def run():
        journal = MarketJournal(data, plugins)
        op = await journal.begin_uninstall("demo", delete_config=False, delete_data=False)
        _plugin(op.recycle_path, "previous recycle entry")
        with pytest.raises(MarketRecoveryError):
            await op.apply()
        assert (op.recycle_path / "main.py").read_text() == "VERSION = 'previous recycle entry'\n"
        assert (plugins / "demo" / "main.py").read_text() == "VERSION = '1.0.0'\n"

    data, plugins = _fixture(tmp_path)
    asyncio.run(run())


_MISSING = object()


class _MP:
    """最小 monkeypatch 替身: legacy runner 只支持无参或仅 tmp_path 的入口。"""

    def __init__(self):
        self._undo = []

    def _resolve(self, target):
        if isinstance(target, str):
            import importlib
            parts = target.split(".")
            obj = importlib.import_module(parts[0])
            for part in parts[1:-1]:
                obj = getattr(obj, part)
            return obj, parts[-1]
        return target, None

    def setattr(self, target, name, value=None):  # noqa: A003 - 对齐 pytest 接口
        obj, attr = self._resolve(target)
        if attr is not None:
            name, value = attr, name
        self._undo.append((obj, name, vars(obj).get(name, _MISSING)))
        setattr(obj, name, value)

    def setitem(self, mapping, key, value):
        self._undo.append((mapping, key, mapping.get(key, _MISSING)))
        mapping[key] = value

    def undo(self):
        while self._undo:
            obj, name, old = self._undo.pop()
            if isinstance(obj, dict):
                if old is _MISSING:
                    obj.pop(name, None)
                else:
                    obj[name] = old
            elif old is _MISSING:
                try:
                    delattr(obj, name)
                except AttributeError:
                    pass
            else:
                setattr(obj, name, old)


def test_replace_recovers_after_abrupt_exit(tmp_path):
    for cut in ["intent", "rename-1", "rename-2", "source", "rollback-rename", "commit", "cleanup"]:
        case_dir = tmp_path / ("case-" + str(abs(hash((cut)))))
        case_dir.mkdir(exist_ok=True)
        mp = _MP()
        try:
            _probe_replace_recovers_after_abrupt_exit(case_dir, cut)
        finally:
            mp.undo()


def test_uninstall_recovery_is_reversible_and_cleanup_keeps_new_data(tmp_path):
    for cut in ["intent", "rename-1", "rename-2", "rename-3", "rename-4", "source", "rollback-rename", "commit", "cleanup"]:
        case_dir = tmp_path / ("case-" + str(abs(hash((cut)))))
        case_dir.mkdir(exist_ok=True)
        mp = _MP()
        try:
            _probe_uninstall_recovery_is_reversible_and_cleanup_keeps_new_data(case_dir, cut)
        finally:
            mp.undo()


def test_ambiguous_state_stops_recovery_without_destroying_evidence(tmp_path):
    for damage in ["journal", "source", "destination", "symlink"]:
        case_dir = tmp_path / ("case-" + str(abs(hash((damage)))))
        case_dir.mkdir(exist_ok=True)
        mp = _MP()
        try:
            _probe_ambiguous_state_stops_recovery_without_destroying_evidence(case_dir, damage)
        finally:
            mp.undo()


def test_config_seed_moves_with_candidate_but_existing_archive_is_untouched(tmp_path):
    for values in __import__("itertools").product(["rename-2", "rename-3", "source", "commit"], [False, True]):
        cut, exists = values
        case_dir = tmp_path / ("case-" + str(abs(hash((cut, exists)))))
        case_dir.mkdir(exist_ok=True)
        mp = _MP()
        try:
            _probe_config_seed_moves_with_candidate_but_existing_archive_is_untouched(case_dir, cut, exists)
        finally:
            mp.undo()


def test_cleanup_retries_final_discard_in_same_process(tmp_path):
    for committed in [True, False]:
        case_dir = tmp_path / ("case-" + str(abs(hash((committed)))))
        case_dir.mkdir(exist_ok=True)
        mp = _MP()
        try:
            _probe_cleanup_retries_final_discard_in_same_process(case_dir, mp, committed)
        finally:
            mp.undo()
