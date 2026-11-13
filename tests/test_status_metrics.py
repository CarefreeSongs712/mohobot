"""`/status` 指标口径回归：CPU 剔除 steal、内存用 available、框架运行时长。

背景：0.35s 的 CPU 短采样 + psutil 把宿主机 steal 算作 busy，会让卡片在
VPS 上显示 99%；内存标题把 page cache 算进已用，显示 96%。本文件锁定修复后口径。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import plugins.status.main as status_main
from mohobot.utils.status_card import format_cpu_label, format_memory_label

GB = 1024 ** 3


class _FakePsutilCPU:
    version_info = (7, 2, 2)

    def cpu_times_percent(self, interval=None, percpu=False):
        return [
            SimpleNamespace(user=10.0, system=5.0, idle=55.0, iowait=0.0, steal=30.0),
            SimpleNamespace(user=5.0, system=5.0, idle=70.0, iowait=5.0, steal=15.0),
        ]

    def cpu_percent(self, interval=None, percpu=False):
        raise AssertionError("有 cpu_times_percent 时不应退化到 cpu_percent")

    def cpu_count(self, logical=True):
        return 2


class _FakePsutilFallback:
    version_info = (5, 9, 0)

    def cpu_percent(self, interval=None, percpu=False):
        return [10.0, 20.0, 30.0]

    def cpu_count(self, logical=True):
        return 3


def test_cpu_reader_excludes_steal_and_iowait() -> None:
    old = status_main.HAS_PSUTIL
    status_main.HAS_PSUTIL = True
    try:
        with patch.object(status_main, "psutil", _FakePsutilCPU()):
            data = status_main._read_cpu()
    finally:
        status_main.HAS_PSUTIL = old
    # 100-55-0-30=15; 100-70-5-15=10
    assert data["cores"] == [15.0, 10.0]
    assert data["average"] == 12.5
    assert data["steal"] == 22.5
    assert data["count"] == 2


def test_cpu_reader_falls_back_without_times_percent() -> None:
    old = status_main.HAS_PSUTIL
    status_main.HAS_PSUTIL = True
    try:
        with patch.object(status_main, "psutil", _FakePsutilFallback()):
            data = status_main._read_cpu()
    finally:
        status_main.HAS_PSUTIL = old
    assert data["cores"] == [10.0, 20.0, 30.0]
    assert data["average"] == 20.0
    assert data["steal"] == 0.0


def test_memory_available_percent() -> None:
    class VM:
        def __init__(self):
            self.total = 100 * GB
            self.free = 10 * GB
            self.available = 60 * GB
            self.used = 50 * GB
            self.buffers = 5 * GB
            self.cached = 35 * GB

    class Swap:
        total = 0
        used = 0
        percent = 0.0

    class FakePsutil:
        def virtual_memory(self):
            return VM()

        def swap_memory(self):
            return Swap()

    old = status_main.HAS_PSUTIL
    status_main.HAS_PSUTIL = True
    try:
        with patch.object(status_main, "psutil", FakePsutil()):
            data = status_main._read_memory()
    finally:
        status_main.HAS_PSUTIL = old
    # 堆叠条口径: app + cache + free 守恒, percent 仍是 app+cache
    assert data["used"] + data["cache"] + data["free"] == 100 * GB
    assert round(data["percent"]) == 90
    # 展示/告警口径: 只按不可回收部分算 = (100-60)%
    assert round(data["available_percent"]) == 40


def test_format_cpu_label_marks_steal() -> None:
    cpu = {"count": 8, "average": 12.0, "cores": [10.0] * 8, "steal": 31.0}
    label = format_cpu_label(cpu)
    assert "平均 12%" in label
    assert "steal 31%" in label

    no_steal = {"count": 8, "average": 12.0, "cores": [10.0] * 8}
    assert "steal" not in format_cpu_label(no_steal)


def test_format_memory_label_uses_available() -> None:
    mem = {
        "total": 8 * GB, "used": int(5.4 * GB), "cache": 2 * GB,
        "free": int(0.3 * GB), "available": 2 * GB,
        "percent": 95.0, "available_percent": 75.0,
    }
    label = format_memory_label(mem)
    assert "75%" in label and "95%" not in label
    assert "可用 2.0GB" in label

    # 无 available 的旧数据回退到 percent(app+cache 口径)
    legacy = {"total": 8 * GB, "used": 5 * GB, "cache": 2 * GB, "percent": 87.5}
    assert "88%" in format_memory_label(legacy)


def test_framework_runtime_prefers_process_start() -> None:
    class Proc:
        def create_time(self):
            return time.time() - 100

    class GoodPsutil:
        Process = Proc

    class BadProc:
        def create_time(self):
            raise RuntimeError("no proc info")

    class BadPsutil:
        Process = BadProc

    old = status_main.HAS_PSUTIL
    status_main.HAS_PSUTIL = True
    try:
        with patch.object(status_main, "psutil", GoodPsutil()):
            runtime = status_main._framework_runtime_secs(time.time() - 50000)
        assert 90 <= runtime <= 110

        with patch.object(status_main, "psutil", BadPsutil()):
            fallback = status_main._framework_runtime_secs(time.time() - 5000)
        assert 4990 <= fallback <= 5010
    finally:
        status_main.HAS_PSUTIL = old


def test_status_card_renders_with_steal_and_available() -> None:
    from pathlib import Path

    from mohobot.utils.status_card import render_status_card

    data = {
        "bot_id": "bot_002",
        "nickname": "测试",
        "now": "2026-11-13 22:35",
        "runtime_secs": 86400,
        "plugin_runtime_secs": 86400,
        "system_uptime_secs": 16 * 86400,
        "cpu": {"count": 8, "average": 12.0, "cores": [10.0] * 8, "steal": 31.0},
        "memory": {
            "total": 8 * GB, "used": 5 * GB, "cache": 2 * GB,
            "free": int(0.3 * GB), "available": 2 * GB,
            "percent": 95.0, "available_percent": 75.0,
            "swap_total": 12 * GB, "swap_used": 4 * GB, "swap_percent": 33.0,
        },
        "disks": [{"mount": "/", "used": 47 * GB, "total": 58 * GB, "percent": 81.0}],
        "bots": [{"bot_id": "bot_002", "nickname": "测试", "online_secs": 3600}],
        "contacts": {"admins": [], "group": "", "url": ""},
    }
    path = render_status_card(data)
    assert path is not None
    tmp = Path(path)
    try:
        assert tmp.exists() and tmp.stat().st_size > 0
    finally:
        tmp.unlink(missing_ok=True)


def test_cpu_sample_window_is_locked_to_1s() -> None:
    seen: list[float | None] = []

    class FakePsutil:
        def cpu_times_percent(self, interval=None, percpu=False):
            seen.append(interval)
            return [SimpleNamespace(idle=80.0, iowait=0.0, steal=0.0)]

        def cpu_count(self, logical=True):
            return 1

    old = status_main.HAS_PSUTIL
    status_main.HAS_PSUTIL = True
    try:
        with patch.object(status_main, "psutil", FakePsutil()):
            status_main._read_cpu()
    finally:
        status_main.HAS_PSUTIL = old
    assert status_main.CPU_SAMPLE_SECS == 1.0
    assert seen == [1.0]


def test_text_report_uses_available_and_steal() -> None:
    data = {
        "bot_id": "bot_001", "nickname": "测试", "now": "t",
        "runtime_secs": 100, "plugin_runtime_secs": 100,
        "system_uptime_secs": 100,
        "cpu": {"count": 8, "average": 12.0, "cores": [10.0] * 8, "steal": 31.0},
        "memory": {
            "total": 8 * GB, "used": int(5.4 * GB), "cache": 2 * GB,
            "free": int(0.3 * GB), "available": 2 * GB,
            "percent": 95.0, "available_percent": 75.0, "swap_total": 0,
        },
        "disks": [{"mount": "/", "used": 47 * GB, "total": 58 * GB, "percent": 81.0}],
        "bots": [], "contacts": {},
    }
    text = status_main._text_report(data)
    assert "steal 31%" in text
    assert "75%" in text and "95%" not in text
    assert "已用" in text and "可用 2.0GB" in text


def test_alert_level_ignores_steal_and_page_cache() -> None:
    from mohobot.utils.status_card import level_percent

    data = {
        "cpu": {"average": 12.0, "steal": 90.0},
        "memory": {
            "total": 8 * GB, "available": 2 * GB,
            "percent": 95.0, "available_percent": 75.0,
        },
        "disks": [{"percent": 81.0}],
    }
    # steal/cache 再高也不该触发"告警"；磁盘 81% 才是最高项
    assert level_percent(data) == 81.0

    high_cpu = dict(data)
    high_cpu["cpu"] = {"average": 91.0, "steal": 0.0}
    assert level_percent(high_cpu) == 91.0


def test_memory_label_boundaries() -> None:
    assert format_memory_label({"total": 0}) == "内存  —"
    # available 缺失时回退 percent(app+cache) 口径
    legacy = {"total": 8 * GB, "used": 5 * GB, "cache": 2 * GB, "percent": 87.5}
    assert "88%" in format_memory_label(legacy)
