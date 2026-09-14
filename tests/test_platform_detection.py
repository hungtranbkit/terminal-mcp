"""Platform detection end-to-end (blg_20dc778df7ac: "macOS nodes report
platform='linux' in the node registry").

THE BUG: node_agent._heartbeat_loop took `platform="linux"` as a
parameter default and the POSIX main() never passed one. windows_agent
passes PLATFORM_WINDOWS explicitly, so Windows was right by
construction -- but macOS runs that same POSIX agent, so it inherited a
default that LOOKED like a real answer. That is why this is tested at
every hop rather than only at the mapping function: the mapping was
never wrong, the missing call site was.

Covered here, per the task's own list: Darwin/macOS/Linux/Windows, rows
stored before this existed, and a mixed fleet.
"""
from __future__ import annotations

import sqlite3

import pytest

from terminal_mcp import node_agent
from terminal_mcp.host_metrics import NodeMetrics
from terminal_mcp.node_models import (
    KNOWN_PLATFORMS,
    PLATFORM_LINUX,
    PLATFORM_MACOS,
    PLATFORM_WINDOWS,
    canonical_platform,
)
from terminal_mcp.node_registry import NodeRegistry


@pytest.fixture
def registry(tmp_path) -> NodeRegistry:
    return NodeRegistry(tmp_path / "nodes.db")


def _metrics() -> NodeMetrics:
    # Same shape as tests/test_node_registry.py's own helper -- the values
    # are irrelevant here, only the platform field is under test.
    return NodeMetrics(cpu_percent=20.0, load1=1.0, load5=1.0, load15=1.0, cpu_count=8,
                       ram_total_bytes=16_000_000_000, ram_used_bytes=4_000_000_000,
                       ram_percent=25.0, swap_total_bytes=1_000_000_000, swap_used_bytes=0,
                       swap_percent=0.0, disk_total_bytes=500_000_000_000,
                       disk_used_bytes=100_000_000_000, disk_free_bytes=400_000_000_000,
                       disk_percent=20.0)


def _heartbeat(registry: NodeRegistry, node_id: str, **kwargs):
    return registry.heartbeat(
        node_id, metrics=_metrics(), tmux_session_count=0, agent_counts={},
        agent_types=("shell",), agent_version="0.12.0", labels=(), **kwargs)


class TestCanonicalMapping:
    @pytest.mark.parametrize("raw", ["darwin", "Darwin", "DARWIN", "  darwin  ", "macos",
                                     "MacOS", "osx", "mac", "mac os x"])
    def test_every_macos_spelling_maps_to_one_value(self, raw):
        assert canonical_platform(raw) == PLATFORM_MACOS

    @pytest.mark.parametrize("raw", ["linux", "Linux", "linux2", " linux "])
    def test_linux_is_unchanged(self, raw):
        assert canonical_platform(raw) == PLATFORM_LINUX

    @pytest.mark.parametrize("raw", ["win32", "win64", "windows", "Windows", "cygwin", "msys"])
    def test_windows_is_unchanged(self, raw):
        assert canonical_platform(raw) == PLATFORM_WINDOWS

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_an_absent_report_keeps_the_old_default(self, raw):
        """An agent too old to report a platform must keep meaning
        exactly what it meant before this function existed."""
        assert canonical_platform(raw) == PLATFORM_LINUX

    def test_an_explicit_default_is_honoured(self):
        assert canonical_platform(None, default=PLATFORM_WINDOWS) == PLATFORM_WINDOWS

    def test_an_unknown_platform_is_preserved_not_flattened_to_linux(self):
        """Flattening the unknown into `linux` is precisely the bug this
        item is about; doing it again for the next OS would be worse for
        having been warned."""
        assert canonical_platform("freebsd") == "freebsd"
        assert canonical_platform("FreeBSD") == "freebsd"
        assert canonical_platform("freebsd") not in KNOWN_PLATFORMS

    def test_it_never_raises_on_junk(self):
        for raw in (123, object(), "\x00", "a" * 500):
            assert isinstance(canonical_platform(raw), str)

    def test_it_does_not_look_at_the_hostname(self):
        """A hostname is the operator's naming habit, not a fact about
        the OS -- a Linux build box called `macbook-builder` must stay
        Linux."""
        assert canonical_platform("linux") == PLATFORM_LINUX
        assert canonical_platform("macbook") == "macbook"  # not macos: it is not a platform string


class TestAgentDetection:
    @pytest.mark.parametrize("sys_platform,expected", [
        ("darwin", PLATFORM_MACOS),
        ("linux", PLATFORM_LINUX),
        ("linux2", PLATFORM_LINUX),
        ("win32", PLATFORM_WINDOWS),
        ("freebsd13", "freebsd13"),
    ])
    def test_detect_platform_reads_sys_platform(self, monkeypatch, sys_platform, expected):
        monkeypatch.setattr(node_agent.sys, "platform", sys_platform)
        assert node_agent.detect_platform() == expected

    def test_a_mac_running_the_posix_agent_no_longer_says_linux(self, monkeypatch):
        """The regression itself, named after what actually happened."""
        monkeypatch.setattr(node_agent.sys, "platform", "darwin")
        assert node_agent.detect_platform() == PLATFORM_MACOS
        assert node_agent.detect_platform() != PLATFORM_LINUX

    def test_the_heartbeat_loop_default_no_longer_asserts_linux(self):
        """The bug was a DEFAULT that looked like an answer. The default
        is now None ('detect it'), so forgetting the argument can no
        longer produce a confident wrong value."""
        import inspect
        default = inspect.signature(node_agent._heartbeat_loop).parameters["platform"].default
        assert default is None

    def test_windows_agent_still_passes_its_platform_explicitly(self):
        source = (node_agent.Path(node_agent.__file__).parent / "windows_agent.py").read_text()
        assert "platform=PLATFORM_WINDOWS" in source


class TestRegistryStorage:
    def test_a_darwin_heartbeat_is_stored_canonically(self, registry):
        registry.register("mac-1", display_name="MacBook", hostname="mac-1",
                          endpoint="http://mac-1:8790", auth_token_ref="t")
        node = _heartbeat(registry, "mac-1", platform="darwin")
        assert node.platform == PLATFORM_MACOS
        assert registry.get("mac-1").platform == PLATFORM_MACOS

    @pytest.mark.parametrize("reported,expected", [
        ("darwin", PLATFORM_MACOS), ("macos", PLATFORM_MACOS),
        ("linux", PLATFORM_LINUX), ("win32", PLATFORM_WINDOWS),
        ("windows", PLATFORM_WINDOWS),
    ])
    def test_each_platform_round_trips(self, registry, reported, expected):
        registry.register("n", display_name="n", hostname="n", endpoint="http://n:1", auth_token_ref="t")
        assert _heartbeat(registry, "n", platform=reported).platform == expected

    def test_an_agent_that_reports_no_platform_still_defaults_to_linux(self, registry):
        """Backward compatibility for a node running an agent older than
        the multi-node work: it omits the field entirely."""
        registry.register("old", display_name="old", hostname="old", endpoint="http://old:1",
                          auth_token_ref="t")
        assert _heartbeat(registry, "old").platform == PLATFORM_LINUX

    def test_a_node_corrects_itself_on_the_next_heartbeat(self, registry):
        """The upgrade path: the row says linux because the old agent
        said so, and one heartbeat from the new agent fixes it. No
        migration rewrites the column, because nothing can know which
        stored 'linux' rows were really macOS without guessing."""
        registry.register("mac-1", display_name="MacBook", hostname="mac-1",
                          endpoint="http://mac-1:8790", auth_token_ref="t")
        _heartbeat(registry, "mac-1", platform="linux")
        assert registry.get("mac-1").platform == PLATFORM_LINUX
        _heartbeat(registry, "mac-1", platform="darwin")
        assert registry.get("mac-1").platform == PLATFORM_MACOS


class TestStoredRowsFromBeforeThisExisted:
    def test_a_row_written_before_the_column_existed_reads_as_linux(self, tmp_path):
        """The registry adds `platform` with ALTER TABLE ... DEFAULT
        'linux'. A row that predates the column keeps that value and must
        keep working."""
        path = tmp_path / "nodes.db"
        NodeRegistry(path)  # create the schema
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE nodes SET platform = 'linux'")
        registry = NodeRegistry(path)
        registry.register("legacy", display_name="legacy", hostname="legacy",
                          endpoint="http://legacy:1", auth_token_ref="t")
        assert registry.get("legacy").platform == PLATFORM_LINUX

    def test_a_stored_value_is_not_rewritten_on_read(self, tmp_path):
        """Normalisation happens on WRITE only. A row holding an odd
        historical spelling reads back exactly as stored -- read-time
        rewriting would mean the same database answered differently
        depending on which version last read it."""
        path = tmp_path / "nodes.db"
        registry = NodeRegistry(path)
        registry.register("odd", display_name="odd", hostname="odd", endpoint="http://odd:1",
                          auth_token_ref="t")
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE nodes SET platform = 'Darwin' WHERE id = 'odd'")
        assert NodeRegistry(path).get("odd").platform == "Darwin"

    def test_no_migration_guesses_which_linux_rows_were_macos(self, tmp_path):
        """A hostname-based backfill is explicitly out of bounds: a Linux
        box named `macbook-builder` would be silently relabelled."""
        path = tmp_path / "nodes.db"
        registry = NodeRegistry(path)
        for node_id in ("macbook-pro", "macbook-builder"):
            registry.register(node_id, display_name=node_id, hostname=node_id,
                              endpoint=f"http://{node_id}:1", auth_token_ref="t")
            _heartbeat(registry, node_id, platform="linux")
        reopened = NodeRegistry(path)
        assert reopened.get("macbook-pro").platform == PLATFORM_LINUX
        assert reopened.get("macbook-builder").platform == PLATFORM_LINUX


class TestMixedFleet:
    def test_four_platforms_coexist_without_interfering(self, registry):
        fleet = {
            "mac-1": ("darwin", PLATFORM_MACOS),
            "linux-1": ("linux", PLATFORM_LINUX),
            "win-1": ("win32", PLATFORM_WINDOWS),
            "old-1": (None, PLATFORM_LINUX),
            "bsd-1": ("freebsd", "freebsd"),
        }
        for node_id, (reported, _) in fleet.items():
            registry.register(node_id, display_name=node_id, hostname=node_id,
                              endpoint=f"http://{node_id}:1", auth_token_ref="t")
            if reported is None:
                _heartbeat(registry, node_id)
            else:
                _heartbeat(registry, node_id, platform=reported)

        stored = {node.id: node.platform for node in registry.list()}
        for node_id, (_, expected) in fleet.items():
            assert stored[node_id] == expected, node_id

    def test_scheduler_routing_is_exact_and_fails_closed(self, registry):
        """The behaviour change this fix carries: a macOS node used to
        match required_platform='linux' because it was mislabelled, and
        no longer does. An unknown platform matches nothing rather than
        falling back to Linux."""
        from terminal_mcp.scheduler import choose_node

        for node_id, reported in (("mac-1", "darwin"), ("linux-1", "linux"), ("win-1", "win32")):
            registry.register(node_id, display_name=node_id, hostname=node_id,
                              endpoint=f"http://{node_id}:1", auth_token_ref="t")
            _heartbeat(registry, node_id, platform=reported)

        nodes = registry.list()
        assert choose_node(nodes, required_platform=PLATFORM_MACOS).node_id == "mac-1"
        assert choose_node(nodes, required_platform=PLATFORM_LINUX).node_id == "linux-1"
        assert choose_node(nodes, required_platform=PLATFORM_WINDOWS).node_id == "win-1"
        assert choose_node(nodes, required_platform="freebsd").node_id is None


class TestOperatorFacingSurfaces:
    """Acceptance criterion 3 of blg_20dc778df7ac: "Dashboard/doctor show
    the real platform". Both used `windows ? X : Linux`, so a macOS node
    was not merely unlabelled -- it was labelled Linux."""

    def _node_row(self, platform: str) -> dict:
        """Exactly the keys _print_nodes_human reads, so this pins the
        platform label rather than a fixture's own completeness."""
        return {"id": "mac-1", "display_name": "MacBook", "platform": platform,
                "session_backend": "tmux", "status": "online", "capacity_status": "healthy",
                "tmux_session_count": 0, "cpu_percent": 1.0, "ram_percent": 2.0,
                "draining": False, "claude_available": False, "codex_available": False,
                "wsl_available": False, "shell_capabilities": [], "overload_reasons": []}

    @pytest.mark.parametrize("platform,shown", [
        (PLATFORM_MACOS, "macos"), (PLATFORM_LINUX, "linux"), (PLATFORM_WINDOWS, "win"),
        ("freebsd", "freebsd"),
    ])
    def test_doctor_prints_the_real_platform(self, capsys, platform, shown):
        from terminal_mcp.doctor import _print_nodes_human

        _print_nodes_human({"nodes": [self._node_row(platform)], "skipped_remote_nodes": []})
        line = capsys.readouterr().out
        assert f"[{shown}/tmux]" in line

    def test_doctor_does_not_call_a_mac_linux(self, capsys):
        from terminal_mcp.doctor import _print_nodes_human

        _print_nodes_human({"nodes": [self._node_row(PLATFORM_MACOS)], "skipped_remote_nodes": []})
        assert "[linux/tmux]" not in capsys.readouterr().out

    def test_the_dashboard_ui_knows_all_three_platforms(self):
        """Source-level, matching this suite's existing posture for the
        dashboard's inline JS (node is not available on every host, so the
        semantic checks elsewhere are skipped rather than run)."""
        from pathlib import Path

        import terminal_mcp.dashboard as dashboard_module

        source = Path(dashboard_module.__file__).read_text(encoding="utf-8")
        assert "macos: 'macOS'" in source
        assert "macos: '🍎'" in source
        # The old two-way branches must be gone, not merely supplemented.
        assert "node.platform === 'windows' ? 'Windows' : 'Linux'" not in source
        assert "node.platform === 'windows' ? '🪟' : '🐧'" not in source
