"""Local host resource metrics -- pure stdlib, no psutil dependency (this
project's own existing discipline: no new dependency for something the
stdlib already gives us, on either platform). Used by every node type --
local (in-process, via LocalNodeClient), a Linux terminal-node-agent, and
a Windows terminal-node-agent (windows_agent.py) -- to answer the exact
same `NodeMetrics` shape, so a remote node's numbers are computed by the
identical per-platform code any node of that platform uses -- never two
divergent implementations of "what does CPU% mean here" for the same OS.

`collect()` dispatches on `sys.platform`: Linux reads `/proc/loadavg`,
`/proc/meminfo`, `/proc/stat` (unchanged from before multi-node Windows
support); Windows uses `ctypes` + a handful of well-known kernel32 calls
(`GlobalMemoryStatusEx`, `GetSystemTimes`) -- still pure stdlib, no
psutil, matching the exact same "no new dependency" discipline. Windows
has no native "load average" concept at all (a Unix-specific metric) --
`load1`/`load5`/`load15` are always `None` there; the overload heuristic
(node_registry.py's classify_capacity) already treats a `None` metric as
simply not contributing to that one check, never as zero or as
"unknown" overall, so this degrades gracefully rather than breaking
anything. Windows "swap" is approximated from page-file commit
accounting (`ullTotalPageFile`/`ullAvailPageFile`), which is NOT the
same exact semantic as Linux's own separate swap partition/file
accounting -- directionally useful (a node running low on page-file
headroom), documented here as an approximation, not a precise match.

Every reader here is defensive: a missing/unreadable source (a
non-Linux host hitting the Linux path, an unusual container/sandbox, a
failed Windows API call) returns `None` for the fields it would have
populated rather than raising -- a metrics collection failure must never
take down the node agent's own health endpoint, and every caller already
treats `None` as "unknown", never as zero.

**Not live-verified on real Windows** -- `ctypes.windll` does not exist
on this Linux development host at all (referencing it here would fail to
even import), so `_collect_windows`'s own correctness (the ctypes
struct layout matches the real Win32 API, the GetSystemTimes delta math
is right) could only be exercised by unit-testing the DISPATCH logic
(does `collect()` call the Windows path when `sys.platform` says
Windows) with the Windows-specific function itself monkeypatched out --
see tests/test_host_metrics_windows.py. Report this honestly as
unverified, not silently assumed correct.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class NodeMetrics:
    cpu_percent: float | None
    load1: float | None
    load5: float | None
    load15: float | None
    cpu_count: int | None
    ram_total_bytes: int | None
    ram_used_bytes: int | None
    ram_percent: float | None
    swap_total_bytes: int | None
    swap_used_bytes: int | None
    swap_percent: float | None
    disk_total_bytes: int | None
    disk_used_bytes: int | None
    disk_free_bytes: int | None
    disk_percent: float | None


def read_loadavg() -> tuple[float | None, float | None, float | None]:
    try:
        with open("/proc/loadavg") as handle:
            parts = handle.read().split()
        return float(parts[0]), float(parts[1]), float(parts[2])
    except (OSError, ValueError, IndexError):
        return None, None, None


def read_meminfo() -> dict[str, int]:
    """Raw `/proc/meminfo` values in bytes, keyed by field name (MemTotal,
    MemAvailable, SwapTotal, SwapFree, ...) -- kept as a dict rather than
    a fixed dataclass since callers only ever need a handful of fields
    and `/proc/meminfo` itself is not a stable, fully-enumerable schema
    across kernel versions."""
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as handle:
            for line in handle:
                if ":" not in line:
                    continue
                key, _, rest = line.partition(":")
                digits = rest.strip().split()[0]
                try:
                    values[key] = int(digits) * 1024  # /proc/meminfo is in KiB
                except ValueError:
                    continue
    except OSError:
        return {}
    return values


class _CpuPercentSampler:
    """CPU% needs two `/proc/stat` samples with a real time gap between
    them (a single snapshot has no rate to compute) -- this keeps the
    previous sample across calls so `sample_cpu_percent()` can return a
    real, non-blocking answer on every call after the first, rather than
    every caller having to `time.sleep()` inside a metrics collection
    path (which the health/metrics HTTP handlers here must never do)."""

    def __init__(self) -> None:
        self._previous: tuple[int, int] | None = None  # (idle, total) jiffies

    @staticmethod
    def _read_cpu_line() -> tuple[int, int] | None:
        try:
            with open("/proc/stat") as handle:
                first_line = handle.readline()
        except OSError:
            return None
        parts = first_line.split()
        if not parts or parts[0] != "cpu":
            return None
        try:
            fields = [int(x) for x in parts[1:]]
        except ValueError:
            return None
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
        total = sum(fields)
        return idle, total

    def sample(self) -> float | None:
        current = self._read_cpu_line()
        if current is None:
            return None
        if self._previous is None:
            self._previous = current
            return None  # first call ever: no rate to compute yet
        prev_idle, prev_total = self._previous
        idle, total = current
        self._previous = current
        delta_total = total - prev_total
        delta_idle = idle - prev_idle
        if delta_total <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (1 - delta_idle / delta_total)))


_CPU_SAMPLER = _CpuPercentSampler()


def sample_cpu_percent() -> float | None:
    """Process-wide singleton sampler (module-level, not per-call) -- CPU%
    is meaningless from one isolated snapshot; this keeps the previous
    `/proc/stat` reading across every call from this process for as long
    as it runs (the node agent / controller process), so the SECOND and
    every later call returns a real instantaneous rate with no sleep."""
    return _CPU_SAMPLER.sample()


def disk_usage(path: str) -> tuple[int | None, int | None, int | None]:
    try:
        usage = shutil.disk_usage(path)
        return usage.total, usage.used, usage.free
    except OSError:
        return None, None, None


def _collect_linux(*, workspace_path: str) -> NodeMetrics:
    load1, load5, load15 = read_loadavg()
    mem = read_meminfo()
    ram_total = mem.get("MemTotal")
    # MemAvailable (kernel's own "how much could a new process actually
    # get" estimate, accounting for reclaimable cache) is the correct
    # "used" complement -- MemFree alone wildly overstates memory
    # pressure on any host with real page cache, which is every host.
    ram_available = mem.get("MemAvailable")
    ram_used = (ram_total - ram_available) if (ram_total is not None and ram_available is not None) else None
    ram_percent = (100.0 * ram_used / ram_total) if (ram_used is not None and ram_total) else None
    swap_total = mem.get("SwapTotal")
    swap_free = mem.get("SwapFree")
    swap_used = (swap_total - swap_free) if (swap_total is not None and swap_free is not None) else None
    swap_percent = (100.0 * swap_used / swap_total) if (swap_used is not None and swap_total) else None
    disk_total, disk_used, disk_free = disk_usage(workspace_path)
    disk_percent = (100.0 * disk_used / disk_total) if (disk_used is not None and disk_total) else None
    return NodeMetrics(
        cpu_percent=sample_cpu_percent(),
        load1=load1, load5=load5, load15=load15,
        cpu_count=os.cpu_count(),
        ram_total_bytes=ram_total, ram_used_bytes=ram_used, ram_percent=ram_percent,
        swap_total_bytes=swap_total, swap_used_bytes=swap_used, swap_percent=swap_percent,
        disk_total_bytes=disk_total, disk_used_bytes=disk_used, disk_free_bytes=disk_free,
        disk_percent=disk_percent,
    )


class _WindowsCpuPercentSampler:
    """GetSystemTimes-based analogue of _CpuPercentSampler above -- same
    two-sample-delta technique, Windows' own kernel32 API instead of
    /proc/stat. Windows' GetSystemTimes semantics: lpKernelTime INCLUDES
    idle time (unlike Linux's /proc/stat, where idle is one of several
    peer fields) -- so total = kernel + user (no separate idle addend)."""

    def __init__(self) -> None:
        self._previous: tuple[int, int] | None = None  # (idle, total) in 100ns ticks

    def sample(self) -> float | None:
        current = _read_windows_system_times()
        if current is None:
            return None
        idle, total = current
        if self._previous is None:
            self._previous = (idle, total)
            return None
        prev_idle, prev_total = self._previous
        self._previous = (idle, total)
        delta_total = total - prev_total
        delta_idle = idle - prev_idle
        if delta_total <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (1 - delta_idle / delta_total)))


_WINDOWS_CPU_SAMPLER = _WindowsCpuPercentSampler()


def _read_windows_system_times() -> tuple[int, int] | None:
    """Returns (idle_ticks, total_ticks) via kernel32!GetSystemTimes, or
    None on any failure. `ctypes.windll` only exists on a real Windows
    interpreter -- this function is never called except from
    _collect_windows, itself only called when sys.platform == "win32"
    (see collect() below), so this never even attempts the import
    elsewhere. See this module's own docstring: not live-verified."""
    try:
        import ctypes
        from ctypes import wintypes

        class _FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        idle_ft, kernel_ft, user_ft = _FILETIME(), _FILETIME(), _FILETIME()
        ok = ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle_ft), ctypes.byref(kernel_ft), ctypes.byref(user_ft),
        )
        if not ok:
            return None

        def _to_int(ft: "_FILETIME") -> int:
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        idle = _to_int(idle_ft)
        # kernel time already includes idle time on Windows -- total is
        # kernel + user, NOT kernel + user + idle (that would double-count).
        total = _to_int(kernel_ft) + _to_int(user_ft)
        return idle, total
    except Exception:  # noqa: BLE001 -- any ctypes/API failure -> unknown, never a crash
        return None


def _read_windows_memory() -> tuple[int | None, int | None, float | None, int | None, int | None, float | None]:
    """Returns (ram_total, ram_used, ram_percent, swap_total, swap_used,
    swap_percent) via kernel32!GlobalMemoryStatusEx, or all-None on any
    failure. Swap is approximated from page-file commit accounting -- see
    this module's own docstring for why that's not an exact match to
    Linux's own swap semantic. Not live-verified -- see module docstring."""
    try:
        import ctypes
        from ctypes import wintypes

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                ("sullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        if not ok:
            return None, None, None, None, None, None

        ram_total = int(stat.ullTotalPhys)
        ram_avail = int(stat.ullAvailPhys)
        ram_used = ram_total - ram_avail
        ram_percent = (100.0 * ram_used / ram_total) if ram_total else None

        pagefile_total = int(stat.ullTotalPageFile)
        pagefile_avail = int(stat.ullAvailPageFile)
        pagefile_used = pagefile_total - pagefile_avail
        # Windows' page file commit accounting covers physical RAM too --
        # treat "swap" as whatever commit is beyond physical RAM, a
        # reasonable (not exact) analogue.
        swap_total = max(0, pagefile_total - ram_total)
        swap_used = max(0, pagefile_used - ram_used)
        swap_percent = (100.0 * swap_used / swap_total) if swap_total else 0.0
        return ram_total, ram_used, ram_percent, swap_total, swap_used, swap_percent
    except Exception:  # noqa: BLE001
        return None, None, None, None, None, None


def _collect_windows(*, workspace_path: str) -> NodeMetrics:
    ram_total, ram_used, ram_percent, swap_total, swap_used, swap_percent = _read_windows_memory()
    disk_total, disk_used, disk_free = disk_usage(workspace_path)  # shutil.disk_usage -- already cross-platform
    disk_percent = (100.0 * disk_used / disk_total) if (disk_used is not None and disk_total) else None
    return NodeMetrics(
        cpu_percent=_WINDOWS_CPU_SAMPLER.sample(),
        load1=None, load5=None, load15=None,  # no load-average concept on Windows
        cpu_count=os.cpu_count(),
        ram_total_bytes=ram_total, ram_used_bytes=ram_used, ram_percent=ram_percent,
        swap_total_bytes=swap_total, swap_used_bytes=swap_used, swap_percent=swap_percent,
        disk_total_bytes=disk_total, disk_used_bytes=disk_used, disk_free_bytes=disk_free,
        disk_percent=disk_percent,
    )


def collect(*, workspace_path: str) -> NodeMetrics:
    if sys.platform == "win32":
        return _collect_windows(workspace_path=workspace_path)
    if sys.platform.startswith("linux"):
        return _collect_linux(workspace_path=workspace_path)
    # Any other platform (e.g. macOS) -- graceful all-unknown rather than
    # a wrong/misleading number; disk usage is genuinely cross-platform
    # via shutil, so that one field is still populated.
    disk_total, disk_used, disk_free = disk_usage(workspace_path)
    disk_percent = (100.0 * disk_used / disk_total) if (disk_used is not None and disk_total) else None
    return NodeMetrics(
        cpu_percent=None, load1=None, load5=None, load15=None, cpu_count=os.cpu_count(),
        ram_total_bytes=None, ram_used_bytes=None, ram_percent=None,
        swap_total_bytes=None, swap_used_bytes=None, swap_percent=None,
        disk_total_bytes=disk_total, disk_used_bytes=disk_used, disk_free_bytes=disk_free,
        disk_percent=disk_percent,
    )
