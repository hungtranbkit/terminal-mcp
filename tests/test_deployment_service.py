"""Probing, dispatch, leases and reconcile.

Every destructive-looking thing here runs against a fake runner or a tmp_path
-- no test in this file touches a real target, a real authorized_keys, or the
operator's own ~/.ssh/config.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from terminal_mcp.deployment_redundancy import (DEGRADED, FAIL, READY, DeploymentPath,
                                                DeploymentTarget, deploy_lease_key)
from terminal_mcp.deployment_service import (BLOCK_BEGIN, BLOCK_END, DeploymentRegistry,
                                             DeploymentService, apply_managed_block,
                                             ensure_deploy_key, probe_path,
                                             render_managed_block)
from terminal_mcp.fleet_registry import FleetRegistryStore
from terminal_mcp.lease import ResourceLockStore


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _path(node="dell-linux", **kwargs):
    fields = {"target_id": "mesflow-vps", "node_id": node, "host": "100.64.0.9",
              "username": "deploy", "port": 22, "ssh_alias": f"{node}-vps"}
    fields.update(kwargs)
    return DeploymentPath(**fields)


# -- probing is a read ---------------------------------------------------------

def test_the_probe_never_asks_the_target_to_do_anything():
    """A doctor run that could change what it measures is not a doctor run."""
    seen = {}

    def runner(argv, **_kwargs):
        seen["argv"] = argv
        return _completed()

    probe_path(_path(), runner=runner, deploy_prereqs=("git", "docker"))
    argv = seen["argv"]
    assert "-o" in argv and "BatchMode=yes" in argv, "must never hang on a prompt"
    assert "StrictHostKeyChecking=yes" in argv
    remote = argv[-1]
    assert remote == "true && command -v git >/dev/null && command -v docker >/dev/null"
    for forbidden in ("rm", "systemctl", "authorized_keys", "restart", "tee", ">>"):
        assert forbidden not in remote


def test_a_successful_probe_reports_auth_and_prereqs_together():
    result = probe_path(_path(), runner=lambda *a, **k: _completed())
    assert (result.ok, result.auth_ok, result.deploy_prereqs_ok) == (True, True, True)
    assert result.latency_ms is not None


def test_permission_denied_is_reachable_but_unauthenticated():
    """Different from unreachable, and telling an operator the wrong one
    wastes a day: one needs a public key installed, the other a network."""
    result = probe_path(_path(), runner=lambda *a, **k: _completed(
        255, stderr="deploy@host: Permission denied (publickey)."))
    assert result.ok is True and result.auth_ok is False
    assert "authentication was refused" in result.reason


def test_a_missing_prereq_is_authenticated_but_not_deployable():
    result = probe_path(_path(), runner=lambda *a, **k: _completed(
        127, stderr="bash: command not found"))
    assert (result.ok, result.auth_ok, result.deploy_prereqs_ok) == (True, True, False)


def test_host_key_verification_failure_is_not_quietly_retried():
    result = probe_path(_path(), runner=lambda *a, **k: _completed(
        255, stderr="Host key verification failed."))
    assert result.ok is False and "host key verification failed" in result.reason


def test_a_timeout_is_data_not_an_exception():
    def runner(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=12)

    result = probe_path(_path(), runner=runner)
    assert result.ok is False and "TimeoutExpired" in result.reason


def test_a_proxy_jump_is_passed_through_so_the_probe_tests_the_real_route():
    seen = {}

    def runner(argv, **_kwargs):
        seen["argv"] = argv
        return _completed()

    probe_path(_path(proxy_jump="dell-linux"), runner=runner)
    assert "-J" in seen["argv"] and "dell-linux" in seen["argv"]


# -- registry round trip -------------------------------------------------------

@pytest.fixture
def registry(tmp_path):
    return DeploymentRegistry(FleetRegistryStore(tmp_path / "fleet.db",
                                                 local_node_id="m910"),
                              local_node_id="m910")


def test_targets_and_paths_survive_a_round_trip(registry):
    registry.put_target(DeploymentTarget(target_id="mesflow-vps", display_name="MESFlow VPS",
                                         project_id="mesflow", primary_node="dell-linux",
                                         backup_nodes=("hp-linux",)))
    registry.put_path(_path("dell-linux", role="primary"))
    registry.put_path(_path("hp-linux"))
    assert [t.target_id for t in registry.targets()] == ["mesflow-vps"]
    assert [p.node_id for p in registry.paths("mesflow-vps")] == ["dell-linux", "hp-linux"]


def test_a_path_is_owned_by_the_node_it_describes(registry):
    """Only that machine can honestly say whether ITS route works -- the same
    single-writer rule the rest of the fleet registry runs on."""
    registry.put_path(_path("hp-linux"))
    obj = registry.store.get("deploy_path", "deploy_path:mesflow-vps:hp-linux")
    assert obj.owner_node == "hp-linux"


def test_recording_a_probe_updates_only_the_probe_fields(registry):
    original = _path("dell-linux", ssh_alias="keep-me", independence_group="office")
    registry.put_path(original)
    from terminal_mcp.deployment_service import ProbeResult

    updated = registry.record_probe(original, ProbeResult(
        ok=True, reason="fine", latency_ms=12.0, auth_ok=True, deploy_prereqs_ok=True))
    assert updated.ssh_alias == "keep-me" and updated.independence_group == "office"
    assert updated.last_probe_ok is True and updated.latency_ms == 12.0


def test_retiring_a_target_tombstones_its_paths_too(registry):
    registry.put_target(DeploymentTarget(target_id="t", display_name="T"))
    registry.put_path(_path("dell-linux", target_id="t"))
    registry.retire_target("t")
    assert registry.targets() == []
    assert registry.paths("t") == []
    assert registry.store.get("deploy_path", "deploy_path:t:dell-linux").deleted is True


# -- dispatch + lease ----------------------------------------------------------

@pytest.fixture
def service(registry, tmp_path):
    registry.put_target(DeploymentTarget(
        target_id="mesflow-vps", display_name="MESFlow VPS", project_id="mesflow",
        min_independent_paths=2, primary_node="dell-linux", backup_nodes=("hp-linux",)))
    fresh = datetime.now(timezone.utc).isoformat()
    for node in ("dell-linux", "hp-linux"):
        registry.put_path(_path(node, last_probe_at=fresh, last_probe_ok=True,
                                auth_ok=True, deploy_prereqs_ok=True, latency_ms=10.0,
                                role="primary" if node == "dell-linux" else "backup"))
    locks = ResourceLockStore(tmp_path / "locks.db")
    online = {"dell-linux": True, "hp-linux": True}
    return DeploymentService(registry, locks=locks, node_online=lambda: dict(online)), online


def test_a_second_dispatcher_is_refused_rather_than_handed_a_node(service):
    """The double-deploy case. The lease is taken BEFORE the choice is
    reported, so the loser is told who holds it instead of also deploying."""
    svc, _ = service
    first = svc.dispatch("mesflow-vps")
    assert first["dispatched"] is True and first["node_id"] == "dell-linux"

    second = svc.dispatch("mesflow-vps")
    assert second["dispatched"] is False
    assert second["lease"]["acquired"] is False
    assert "already in flight" in second["reason"]
    assert second["lease"]["held_by"] == first["lease"]["owner_id"]


def test_releasing_the_lease_lets_the_next_deploy_through(service):
    svc, _ = service
    first = svc.dispatch("mesflow-vps")
    assert svc.release_deploy_lease("mesflow-vps", first["lease"]["owner_id"]) is True
    assert svc.dispatch("mesflow-vps")["dispatched"] is True


def test_one_lease_covers_the_target_even_across_different_nodes(service):
    """Two dispatchers choosing DIFFERENT nodes must still not both run."""
    svc, online = service
    first = svc.dispatch("mesflow-vps")
    online["dell-linux"] = False          # the second caller would pick hp-linux
    second = svc.dispatch("mesflow-vps")
    assert second["node_id"] == "hp-linux"
    assert second["dispatched"] is False, "a different node is still the same deploy"


def test_dry_run_failover_answers_without_breaking_anything(service):
    svc, online = service
    result = svc.dry_run_failover("mesflow-vps", assume_offline=["dell-linux"])
    assert result["status"] == DEGRADED
    assert result["deploy_available"] is True
    assert result["would_choose"]["node_id"] == "hp-linux"
    # The hypothetical must not have leaked into reality.
    assert online["dell-linux"] is True
    assert svc.evaluate("mesflow-vps")["status"] == READY


def test_dry_run_of_losing_both_nodes_reports_fail(service):
    svc, _ = service
    result = svc.dry_run_failover("mesflow-vps", assume_offline=["dell-linux", "hp-linux"])
    assert result["status"] == FAIL and result["deploy_available"] is False


def test_an_unknown_target_is_an_error_not_an_empty_success(service):
    svc, _ = service
    assert svc.choose("nope")["error"] == "UNKNOWN_TARGET"
    assert svc.dry_run_failover("nope")["error"] == "UNKNOWN_TARGET"


# -- key material --------------------------------------------------------------

def test_a_node_generates_its_own_key_and_only_the_public_half_comes_back(tmp_path):
    key = tmp_path / "ssh" / "id_deploy"

    def runner(argv, **_kwargs):
        if argv[0] == "ssh-keygen" and "-t" in argv:
            Path(argv[argv.index("-f") + 1]).write_text("PRIVATE-DO-NOT-LEAK")
            Path(argv[argv.index("-f") + 1] + ".pub").write_text("ssh-ed25519 AAAAC3Nz node\n")
            return _completed()
        return _completed(stdout="256 SHA256:abcdef node (ED25519)\n")

    summary = ensure_deploy_key(key, comment="node@fleet", runner=runner)
    assert summary.created is True and summary.exists is True
    assert summary.public_key.startswith("ssh-ed25519 ")
    assert summary.fingerprint == "SHA256:abcdef"
    # The private half is never read, never returned, and has no field to
    # live in.
    assert "PRIVATE-DO-NOT-LEAK" not in str(summary.as_dict())
    assert not any("private_key" == k for k in summary.as_dict() if k != "private_key_path")


def test_key_permissions_are_what_openssh_will_actually_accept(tmp_path):
    key = tmp_path / "ssh" / "id_deploy"

    def runner(argv, **_kwargs):
        if "-t" in argv:
            target = Path(argv[argv.index("-f") + 1])
            target.write_text("k")
            Path(str(target) + ".pub").write_text("ssh-ed25519 AAAA node\n")
        return _completed(stdout="256 SHA256:x node (ED25519)\n")

    ensure_deploy_key(key, comment="c", runner=runner)
    assert oct(key.stat().st_mode)[-3:] == "600", "OpenSSH refuses a looser key"
    assert oct(key.parent.stat().st_mode)[-3:] == "700"


def test_an_existing_key_is_never_regenerated(tmp_path):
    key = tmp_path / "id_deploy"
    key.write_text("existing")
    (tmp_path / "id_deploy.pub").write_text("ssh-ed25519 AAAA existing\n")
    calls = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        return _completed(stdout="256 SHA256:x existing (ED25519)\n")

    summary = ensure_deploy_key(key, comment="c", runner=runner)
    assert summary.created is False
    assert key.read_text() == "existing", "regenerating would lock the node out"
    assert not any("-t" in argv for argv in calls)


def test_nothing_is_generated_when_create_is_off(tmp_path):
    summary = ensure_deploy_key(tmp_path / "absent", comment="c", create=False,
                                runner=lambda *a, **k: _completed())
    assert (summary.exists, summary.created, summary.public_key) == (False, False, None)


# -- managed ssh config block --------------------------------------------------

def test_the_managed_block_never_writes_a_proxy_jump():
    """Writing one would manufacture exactly the dependency this feature
    exists to detect."""
    block = render_managed_block([_path("dell-linux", proxy_jump="somewhere")],
                                 identity_file="~/.ssh/id_deploy")
    assert "ProxyJump" not in block and "ProxyCommand" not in block
    assert "Host dell-linux-vps" in block and "HostName 100.64.0.9" in block
    assert "IdentitiesOnly yes" in block


def test_reconcile_rewrites_only_its_own_lines():
    existing = ("Host my-own-box\n    HostName 10.0.0.1\n\n"
                + BLOCK_BEGIN + "\nHost stale-alias\n" + BLOCK_END + "\n"
                + "Host another-of-mine\n    HostName 10.0.0.2\n")
    updated = apply_managed_block(existing, render_managed_block([_path("hp-linux")]))
    assert "Host my-own-box" in updated and "Host another-of-mine" in updated
    assert "stale-alias" not in updated
    assert "Host hp-linux-vps" in updated
    assert updated.count(BLOCK_BEGIN) == 1


def test_a_config_with_no_block_yet_is_appended_to_not_replaced():
    existing = "Host mine\n    HostName 10.0.0.1\n"
    updated = apply_managed_block(existing, render_managed_block([_path("hp-linux")]))
    assert updated.startswith("Host mine")
    assert BLOCK_BEGIN in updated


def test_applying_the_same_block_twice_is_stable():
    block = render_managed_block([_path("hp-linux")])
    once = apply_managed_block("Host mine\n", block)
    assert apply_managed_block(once, block) == once
