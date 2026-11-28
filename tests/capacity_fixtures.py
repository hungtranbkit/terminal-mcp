"""Explicit synthetic capacity for tests of behavior beyond admission."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


GIB = 1024 ** 3


def supply_positive_local_capacity(monkeypatch, expected_path: str | Path) -> None:
    """Make local admission realistic and deterministic for isolated fixtures.

    Call only in tests whose purpose is downstream behavior. Capacity denial
    tests must use the real collector or their own explicit negative metrics.
    """
    from terminal_mcp import host_metrics

    expected = Path(expected_path).resolve()

    def disk_usage(path):
        assert Path(path).resolve() == expected
        return 100 * GIB, 60 * GIB, 40 * GIB

    def statvfs(path):
        assert Path(path).resolve() == expected
        return SimpleNamespace(f_files=1_000_000, f_ffree=800_000, f_favail=800_000)

    monkeypatch.setattr(host_metrics, "disk_usage", disk_usage)
    monkeypatch.setattr(host_metrics.os, "statvfs", statvfs)
