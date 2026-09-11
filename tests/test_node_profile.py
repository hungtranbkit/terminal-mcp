"""Environment convergence: what a node needs to actually take over.

Converging source code is not enough. A node can run the exact commit the
controller runs and still be useless at failover because it has no Claude
login, no tmux, or no Tailscale -- and that stays invisible until the moment
it matters. These tests pin the audit that makes it visible, and the
property that makes the audit safe to run fleet-wide: it never touches
credential contents.
"""
from __future__ import annotations

import json
import subprocess
import textwrap

import pytest
import yaml

from terminal_mcp import node_profile as np


# -- profile loading ---------------------------------------------------------

def test_the_shipped_profile_loads_and_declares_what_matters():
    profile = np.load_profile()
    assert profile["schema_version"] == np.SUPPORTED_SCHEMA_VERSION
    assert {"node", "controller"} <= set(profile["roles"])
    auth_ids = {a["id"] for a in profile["auth"]}
    assert {"claude_auth", "tailscale_auth", "github_auth"} <= auth_ids
    assert profile["minimum_failover_auth_set"]["node"]


def test_an_unsupported_schema_is_refused_not_best_effort(tmp_path):
    """Silently ignoring a requirement shape this build does not understand is
    how a node passes an audit it should have failed."""
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump({"schema_version": 99, "profile_version": 1,
                                    "roles": {}, "auth": [],
                                    "minimum_failover_auth_set": {}}))
    with pytest.raises(np.ProfileError, match="schema_version"):
        np.load_profile(path)


def test_fingerprint_tracks_content_not_file_identity(tmp_path):
    base = {"schema_version": 1, "profile_version": 1, "roles": {}, "auth": [],
            "minimum_failover_auth_set": {}}
    changed = {**base, "profile_version": 2}
    assert np.profile_fingerprint(base) == np.profile_fingerprint(dict(base))
    assert np.profile_fingerprint(base) != np.profile_fingerprint(changed)


# -- version drift -----------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Python 3.14.4", (3, 14, 4)),
    ("tmux 3.4a", (3, 4)),
    ("git version 2.43.0", (2, 43, 0)),
    ("no digits here", ()),
])
def test_version_parsing_reads_the_numeric_prefix(text, expected):
    assert np.parse_version(text) == expected


def test_version_drift_is_distinguished_from_missing(tmp_path, monkeypatch):
    """MISSING and DRIFT need different fixes -- install vs upgrade -- so they
    must never collapse into one status."""
    monkeypatch.setattr(np.shutil, "which", lambda b: "/usr/bin/faketool")
    monkeypatch.setattr(np, "_run", lambda cmd: (0, "faketool 1.0.0"))
    drifted = np.check_tool({"name": "faketool", "binary": "faketool", "required": True,
                             "version_command": ["faketool", "--version"],
                             "min_version": "9.0"}, "linux")
    assert drifted.status == np.DRIFT
    assert drifted.found_version and drifted.wanted_version == "9.0"

    monkeypatch.setattr(np.shutil, "which", lambda b: None)
    absent = np.check_tool({"name": "faketool", "binary": "faketool", "required": True}, "linux")
    assert absent.status == np.MISSING


def test_a_tool_for_another_os_is_skipped_not_failed():
    check = np.check_tool({"name": "tmux", "binary": "tmux", "required": True,
                           "os": ["linux", "darwin"]}, "windows")
    assert check.status == np.SKIPPED


def test_a_controller_host_is_not_required_to_run_a_node_agent():
    """A controller serves its own machine as `local` in-process. Running an
    agent beside it would make it register itself as its own remote node --
    the loop docs/CONTROLLER_RUNBOOK.md calls out."""
    entry = {"name": "terminal-node-agent", "kind": "systemd-user", "os": ["linux"],
             "must_be": "enabled", "unless_roles": ["controller"]}
    assert np.check_service(entry, "linux", ("node", "controller")).status == np.SKIPPED
    assert np.check_service(entry, "linux", ("node",)).status != np.SKIPPED


# -- auth: status only, never contents --------------------------------------

def test_auth_probe_reports_presence_without_reading_the_file(tmp_path, monkeypatch):
    secret = tmp_path / "creds.json"
    secret.write_text('{"token": "sk-DO-NOT-LEAK-THIS"}')
    monkeypatch.setattr(np, "_expand", lambda p: secret if "creds" in p else tmp_path / "nope")

    check = np.check_auth({"id": "fake_auth", "provider": "fake",
                           "probe": {"kind": "file_exists", "paths": ["~/creds.json"]},
                           "setup_command": "fake login"})
    assert check.status == np.PASS
    blob = json.dumps(check.as_dict())
    assert "sk-DO-NOT-LEAK-THIS" not in blob
    assert "token" not in blob


def test_missing_auth_is_actionable_not_just_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(np, "_expand", lambda p: tmp_path / "absent")
    check = np.check_auth({"id": "fake_auth", "provider": "fake",
                           "probe": {"kind": "file_exists", "paths": ["~/absent"]},
                           "setup_command": "fake login --device"})
    assert check.status == np.NEEDS_AUTH
    assert check.remediation == "fake login --device"


def test_no_auth_probe_in_the_shipped_profile_reads_file_contents():
    """Structural guard on the profile itself: every probe kind here is
    existence- or status-based. If someone adds a `file_contains` style probe
    later, this fails and they have to justify it."""
    allowed = {"file_exists", "glob_exists", "command_ok", "command_json", "any_of"}

    def kinds(probe):
        yield probe.get("kind")
        for sub in probe.get("probes", []):
            yield from kinds(sub)

    for entry in np.load_profile()["auth"]:
        for kind in kinds(entry.get("probe") or {}):
            assert kind in allowed, f"{entry['id']} uses unvetted probe kind {kind!r}"


# -- failover readiness ------------------------------------------------------

def _profile_with(failover, auth_ready):
    return {
        "schema_version": 1, "profile_version": 1,
        "roles": {"node": {"tools": [], "services": []}},
        "auth": [{"id": aid, "provider": "x",
                  "probe": {"kind": "command_ok",
                            "command": ["true"] if ready else ["false"]},
                  "setup_command": f"{aid} login"}
                 for aid, ready in auth_ready.items()],
        "minimum_failover_auth_set": {"node": failover, "advisory": []},
    }


def test_an_advisory_auth_gap_does_not_block_failover():
    """The distinction this whole idea rests on: a missing GitHub push
    credential is annoying, it is not a reason to call a node unable to carry
    the fleet."""
    profile = _profile_with(["tailscale_auth"],
                            {"tailscale_auth": True, "github_auth": False})
    result = np.inventory(("node",), profile=profile, os_name="linux")
    assert result["failover_ready"] is True
    assert result["missing_failover_auth"] == []
    assert any(c["id"] == "github_auth" and c["status"] == np.NEEDS_AUTH
               for c in result["checks"])


def test_a_required_failover_auth_gap_does_block():
    profile = _profile_with(["tailscale_auth"],
                            {"tailscale_auth": False, "github_auth": True})
    result = np.inventory(("node",), profile=profile, os_name="linux")
    assert result["failover_ready"] is False
    assert result["missing_failover_auth"] == ["tailscale_auth"]


def test_an_unprobeable_requirement_never_counts_as_passing():
    """UNKNOWN is not PASS. A requirement whose probe could not run is exactly
    the case where optimism is most expensive."""
    profile = {
        "schema_version": 1, "profile_version": 1,
        "roles": {"node": {"tools": [], "services": [
            {"name": "svc", "kind": "totally-unknown-kind", "must_be": "enabled"}]}},
        "auth": [], "minimum_failover_auth_set": {"node": []},
    }
    result = np.inventory(("node",), profile=profile, os_name="linux")
    assert result["failover_ready"] is False
    assert "svc" in result["blocking"]


def test_this_machine_audits_without_raising():
    """Smoke: the real profile against the real host."""
    result = np.inventory(("node", "controller"))
    assert result["os"] in ("linux", "darwin", "windows")
    assert isinstance(result["failover_ready"], bool)
    assert result["profile_fingerprint"]


# -- which roles the audit runs as -------------------------------------------

def test_detect_roles_reports_controller_when_the_unit_is_installed(monkeypatch):
    # Getting this wrong is not cosmetic: the node-agent requirement is
    # `unless_roles: [controller]`, so a controller audited as a plain node
    # is told its deliberately-disabled agent blocks failover.
    monkeypatch.setattr(np, "_has_systemd_user_unit", lambda unit: unit == "terminal-mcp-http")
    assert np.detect_roles() == ("node", "controller")


def test_detect_roles_reports_only_node_without_a_controller_unit(monkeypatch):
    monkeypatch.setattr(np, "_has_systemd_user_unit", lambda unit: False)
    assert np.detect_roles() == ("node",)


def test_a_controller_is_not_told_its_disabled_node_agent_blocks_failover(monkeypatch):
    # The end-to-end consequence of the two above.
    monkeypatch.setattr(np, "_has_systemd_user_unit", lambda unit: unit == "terminal-mcp-http")
    result = np.inventory(np.detect_roles(), os_name="linux")
    agent = [c for c in result["checks"] if c["id"] == "terminal-node-agent"]
    assert agent and all(c["status"] == np.SKIPPED for c in agent)
    assert "terminal-node-agent" not in result["blocking"]


def test_an_installed_but_disabled_unit_still_counts_as_holding_the_role(monkeypatch):
    # A controller stopped for maintenance has not stopped being one; only
    # "not-found" means the role is absent.
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 1, stdout="disabled\n", stderr="")

    monkeypatch.setattr(np.subprocess, "run", fake_run)
    assert np._has_systemd_user_unit("terminal-mcp-http") is True

    def fake_missing(cmd, **kwargs):
        # Verbatim what `systemctl --user is-enabled <missing>` returns.
        return subprocess.CompletedProcess(cmd, 4, stdout="not-found\n", stderr="")

    monkeypatch.setattr(np.subprocess, "run", fake_missing)
    assert np._has_systemd_user_unit("terminal-mcp-http") is False
