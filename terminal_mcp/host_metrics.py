"""Local host resource metrics -- pure stdlib (Linux `/proc`), no psutil
dependency (this project's own existing discipline: no new dependency for
something the stdlib + `/proc` already gives us). Used by both the local
node (in-process, via LocalNodeClient) and terminal-node-agent (a remote
node's own process) to answer the exact same `NodeMetrics` shape, so a
remote node's numbers are computed by the identical code a local one
uses -- never two divergent implementations of "what does CPU% mean here".

Every reader here is defensive: a missing/unreadable `/proc` file (a
non-Linux host, an unusual container/sandbox) returns `None` for the
fields it would have populated rather than raising -- a metrics
collection failure must never take down the node agent's own health
endpoint, and a caller (node_registry.py's overload heuristic) already
treats `None` as "unknown", never as zero.
"""
from __future__ import annotations

import os
import shutil
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


def collect(*, workspace_path: str) -> NodeMetrics:
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
