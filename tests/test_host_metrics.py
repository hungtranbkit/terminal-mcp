"""host_metrics.py: real Linux metrics collection (already covered
indirectly by other tests calling collect() for real; a few direct
sanity checks here), the platform dispatch in collect(), and the Windows
CPU%/memory sampling MATH (verified via monkeypatched inputs, since
`ctypes.windll` does not exist on this Linux host at all -- see
host_metrics.py's own module docstring for why the real Windows API
calls themselves are NOT live-verified here, only the surrounding logic)."""
from __future__ import annotations

import terminal_mcp.host_metrics as hm


def test_collect_real_linux_sanity(tmp_path):
    metrics = hm.collect(workspace_path=str(tmp_path))
    assert metrics.cpu_count is not None and metrics.cpu_count > 0
    assert metrics.ram_total_bytes is not None and metrics.ram_total_bytes > 0
    assert metrics.disk_total_bytes is not None


def test_collect_dispatches_to_linux_path_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(hm.sys, "platform", "linux")
    called = []
    monkeypatch.setattr(hm, "_collect_linux", lambda *, workspace_path: called.append(workspace_path) or hm.NodeMetrics(
        cpu_percent=1.0, load1=None, load5=None, load15=None, cpu_count=1,
        ram_total_bytes=None, ram_used_bytes=None, ram_percent=None,
        swap_total_bytes=None, swap_used_bytes=None, swap_percent=None,
        disk_total_bytes=None, disk_used_bytes=None, disk_free_bytes=None, disk_percent=None,
    ))
    result = hm.collect(workspace_path=str(tmp_path))
    assert called == [str(tmp_path)]
    assert result.cpu_percent == 1.0


def test_collect_dispatches_to_windows_path_when_platform_is_win32(monkeypatch, tmp_path):
    monkeypatch.setattr(hm.sys, "platform", "win32")
    called = []
    monkeypatch.setattr(hm, "_collect_windows", lambda *, workspace_path: called.append(workspace_path) or hm.NodeMetrics(
        cpu_percent=2.0, load1=None, load5=None, load15=None, cpu_count=1,
        ram_total_bytes=None, ram_used_bytes=None, ram_percent=None,
        swap_total_bytes=None, swap_used_bytes=None, swap_percent=None,
        disk_total_bytes=None, disk_used_bytes=None, disk_free_bytes=None, disk_percent=None,
    ))
    result = hm.collect(workspace_path=str(tmp_path))
    assert called == [str(tmp_path)]
    assert result.cpu_percent == 2.0


def test_collect_unknown_platform_returns_graceful_all_unknown_except_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(hm.sys, "platform", "darwin")
    result = hm.collect(workspace_path=str(tmp_path))
    assert result.cpu_percent is None
    assert result.load1 is None
    assert result.ram_total_bytes is None
    assert result.disk_total_bytes is not None  # shutil.disk_usage is genuinely cross-platform


def test_windows_metrics_never_report_load_average(monkeypatch, tmp_path):
    monkeypatch.setattr(hm.sys, "platform", "win32")
    monkeypatch.setattr(hm, "_read_windows_memory", lambda: (100, 50, 50.0, 20, 5, 25.0))
    result = hm.collect(workspace_path=str(tmp_path))
    assert result.load1 is None and result.load5 is None and result.load15 is None
    assert result.ram_total_bytes == 100
    assert result.swap_percent == 25.0


def test_windows_memory_helper_failure_is_all_none():
    # _read_windows_memory imports ctypes lazily and catches any failure
    # (including the guaranteed-to-fail `ctypes.windll` lookup on this
    # non-Windows host, exercised here for real, not mocked) -- must
    # degrade to an all-None tuple, never raise.
    result = hm._read_windows_memory()
    assert result == (None, None, None, None, None, None)


def test_windows_system_times_helper_failure_is_none(monkeypatch):
    # Same guarantee for the CPU-times helper: `ctypes.windll` genuinely
    # does not exist on this host, so this exercises the REAL failure
    # path (not a monkeypatched one) -- proving collect() can never crash
    # a Windows-dispatch call on a host where the Win32 API isn't there,
    # which incidentally is exactly this test environment's own situation.
    result = hm._read_windows_system_times()
    assert result is None


def test_windows_cpu_percent_sampler_first_call_returns_none_then_computes_delta():
    sampler = hm._WindowsCpuPercentSampler()
    calls = iter([(1000, 10000), (1500, 20000)])  # (idle, total) in fake 100ns ticks
    import terminal_mcp.host_metrics as hm_module
    original = hm_module._read_windows_system_times
    try:
        hm_module._read_windows_system_times = lambda: next(calls)
        first = sampler.sample()
        assert first is None  # no rate on the very first sample
        second = sampler.sample()
        # delta_idle=500, delta_total=10000 -> busy = 1 - 500/10000 = 0.95 -> 95%
        assert second == 95.0
    finally:
        hm_module._read_windows_system_times = original


def test_windows_cpu_percent_sampler_handles_unavailable_api():
    sampler = hm._WindowsCpuPercentSampler()
    import terminal_mcp.host_metrics as hm_module
    original = hm_module._read_windows_system_times
    try:
        hm_module._read_windows_system_times = lambda: None
        assert sampler.sample() is None
    finally:
        hm_module._read_windows_system_times = original
