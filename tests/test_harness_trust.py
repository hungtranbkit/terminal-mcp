"""Workspace trust, granted for Harness worktrees and refused for everything else.

Almost every test here asserts a REFUSAL, because the whole risk of this
feature points one way. A trust grant that is too narrow costs one
PERMISSION_REQUIRED decision -- annoying, visible, recoverable. A trust grant
that is too wide silently marks a directory a person owns as approved for an
autonomous agent, and nobody finds out from the behaviour.

So the tests are organised around what must NOT happen:

  * a path outside the approved roots          -> test_*_outside_*
  * a symlink pointing out of an approved root -> test_a_symlink_*
  * a directory nobody ran anything in         -> test_*_not_harness_owned
  * the approved root itself                   -> test_the_root_itself_*
  * a corrupt config replaced rather than kept -> test_a_corrupt_config_*
  * unrelated config entries lost              -> test_*_preserves_*

SAFETY: every config file here is tmp_path-scoped and every store is a
tmp_path sqlite file. Nothing in this file reads or writes the real
~/.claude.json -- `config_path` is always passed explicitly.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from terminal_mcp import harness_trust as trust
from terminal_mcp.harness_store import HarnessStore
from terminal_mcp.harness_trust import WorkspaceTrust


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return HarnessStore(tmp_path / "queue.db")


@pytest.fixture
def repo(tmp_path):
    """A real git repository, because a real worktree needs one."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


@pytest.fixture
def worktree_root(tmp_path):
    root = tmp_path / ".terminal-mcp-worktrees" / "harness"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def config(tmp_path):
    """A config with unrelated entries that must survive every write."""
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({
        "hasCompletedOnboarding": True,
        "oauthAccount": {"emailAddress": "someone@example.com"},
        "projects": {
            "/home/someone/private-work": {
                "hasTrustDialogAccepted": True,
                "allowedTools": ["Bash(ls)"],
            },
        },
    }, indent=2))
    return path


def _worktree(repo, worktree_root, name="T-1"):
    path = worktree_root / name
    subprocess.run(["git", "worktree", "add", "--force", "-B", f"harness/{name.lower()}",
                    str(path), "HEAD"],
                   cwd=repo, capture_output=True, text=True, check=True)
    return path


def _run_owning(store, path, task_id="T-1"):
    """A harness run that records this worktree -- the ownership evidence."""
    run, _ = store.create_run(task_id=task_id, prompt="p", title=task_id)
    store.patch_run(run.id, worktree_path=str(path))
    return store.require_run(run.id)


def _trust(config, store, worktree_root):
    return WorkspaceTrust(config_path=config, store=store,
                          worktree_roots=[str(worktree_root)])


# ---------------------------------------------------------------------------
# the happy path, which is deliberately narrow
# ---------------------------------------------------------------------------

def test_a_harness_owned_worktree_is_trusted(config, store, repo, worktree_root):
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)

    decision = _trust(config, store, worktree_root).register(path, run_id=run.id)

    assert decision.granted is True
    assert decision.reason == "registered"
    written = json.loads(config.read_text())
    assert written["projects"][os.path.realpath(path)]["hasTrustDialogAccepted"] is True


def test_the_exact_path_is_the_key_claude_code_looks_for(config, store, repo, worktree_root):
    """Trust is keyed by exact path, not prefix: trusting the parent does
    nothing for the child, which is the whole reason this module exists."""
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)

    _trust(config, store, worktree_root).register(path, run_id=run.id)

    projects = json.loads(config.read_text())["projects"]
    assert os.path.realpath(path) in projects
    assert str(worktree_root) not in projects


def test_granting_twice_writes_once_and_reports_the_second_honestly(
        config, store, repo, worktree_root):
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)
    service = _trust(config, store, worktree_root)

    first = service.register(path, run_id=run.id)
    after_first = config.read_text()
    second = service.register(path, run_id=run.id)

    assert first.granted and not first.already
    assert second.granted and second.already, \
        "idempotent means the same OUTCOME, not pretending work was done"
    assert config.read_text() == after_first, "no second write"


# ---------------------------------------------------------------------------
# what is refused
# ---------------------------------------------------------------------------

def test_an_arbitrary_user_directory_is_refused(config, store, tmp_path, worktree_root):
    """The failure this whole module is shaped to prevent."""
    someones_work = tmp_path / "my-actual-project"
    someones_work.mkdir()

    decision = _trust(config, store, worktree_root).register(someones_work)

    assert decision.granted is False
    assert decision.reason == trust.OUTSIDE_APPROVED_ROOTS
    assert "projects" not in json.loads(config.read_text()) or \
        os.path.realpath(someones_work) not in json.loads(config.read_text())["projects"]


def test_the_home_directory_is_refused(config, store, worktree_root):
    decision = _trust(config, store, worktree_root).register(os.path.expanduser("~"))
    assert decision.granted is False
    assert decision.reason == trust.OUTSIDE_APPROVED_ROOTS


def test_a_relative_path_is_refused_before_anything_else(config, store, worktree_root):
    decision = _trust(config, store, worktree_root).register("../../etc")
    assert decision.granted is False
    assert decision.reason == trust.NOT_ABSOLUTE


def test_a_path_that_does_not_exist_is_refused(config, store, worktree_root):
    decision = _trust(config, store, worktree_root).register(worktree_root / "never-made")
    assert decision.granted is False
    assert decision.reason == trust.NOT_A_DIRECTORY


def test_the_root_itself_is_never_trusted(config, store, worktree_root):
    """The root holds many worktrees and is not one. Trusting it would not
    even help -- Claude Code keys by exact path."""
    decision = _trust(config, store, worktree_root).register(worktree_root)
    assert decision.granted is False
    assert decision.reason == trust.IS_A_ROOT_ITSELF


def test_a_sibling_with_a_shared_prefix_is_refused(config, store, tmp_path, worktree_root):
    """`/a/bc`.startswith(`/a/b`) is True. commonpath is used precisely so
    this directory -- which is NOT inside the root -- stays refused."""
    sibling = tmp_path / ".terminal-mcp-worktrees" / "harness-elsewhere"
    sibling.mkdir(parents=True)

    decision = _trust(config, store, worktree_root).register(sibling)

    assert decision.granted is False
    assert decision.reason == trust.OUTSIDE_APPROVED_ROOTS


def test_a_symlink_out_of_an_approved_root_is_refused(
        config, store, tmp_path, worktree_root):
    """The containment check resolves symlinks on both sides, so a link
    planted inside the root cannot smuggle an outside path in."""
    outside = tmp_path / "somewhere-else"
    outside.mkdir()
    (outside / ".git").mkdir()
    link = worktree_root / "looks-legitimate"
    link.symlink_to(outside, target_is_directory=True)

    decision = _trust(config, store, worktree_root).register(link)

    assert decision.granted is False
    assert decision.reason == trust.OUTSIDE_APPROVED_ROOTS
    assert decision.checks["resolved"] == os.path.realpath(outside)


def test_a_plain_directory_in_the_right_place_is_not_a_worktree(
        config, store, worktree_root):
    ordinary = worktree_root / "just-a-folder"
    ordinary.mkdir()

    decision = _trust(config, store, worktree_root).register(ordinary)

    assert decision.granted is False
    assert decision.reason == trust.NOT_A_GIT_WORKTREE


def test_a_worktree_no_run_ever_used_is_refused(config, store, repo, worktree_root):
    """Well-located is not the same as Harness-owned. Only the database can
    attest that this process created the directory."""
    path = _worktree(repo, worktree_root, "ORPHAN")

    decision = _trust(config, store, worktree_root).register(path)

    assert decision.granted is False
    assert decision.reason == trust.NOT_HARNESS_OWNED


def test_without_a_store_nothing_is_ever_trusted(config, repo, worktree_root):
    """Fail closed: a trust boolean with no way to check ownership is a no."""
    path = _worktree(repo, worktree_root)
    service = WorkspaceTrust(config_path=config, store=None,
                             worktree_roots=[str(worktree_root)])

    decision = service.register(path)

    assert decision.granted is False
    assert decision.reason == trust.NO_STORE


def test_with_no_configured_roots_nothing_is_trusted(config, store, repo, tmp_path):
    """The empty-set reading of an allowlist is the dangerous one."""
    root = tmp_path / ".terminal-mcp-worktrees" / "harness"
    root.mkdir(parents=True)
    path = _worktree(repo, root)
    _run_owning(store, path)

    decision = WorkspaceTrust(config_path=config, store=store,
                              worktree_roots=[]).register(path)

    assert decision.granted is False
    assert decision.reason == trust.OUTSIDE_APPROVED_ROOTS


# ---------------------------------------------------------------------------
# ownership can come from a dispatch or a checkpoint too
# ---------------------------------------------------------------------------

def test_a_dispatch_is_ownership_evidence(config, store, repo, worktree_root):
    path = _worktree(repo, worktree_root, "D-1")
    run, _ = store.create_run(task_id="D-1", prompt="p")
    store.open_dispatch(run_id=run.id, task_id="D-1", iteration=1, role="builder",
                        attempt=1, idempotency_key="k1", nonce="n1",
                        worktree_path=str(path))

    decision = _trust(config, store, worktree_root).register(path, run_id=run.id)

    assert decision.granted is True
    assert decision.checks["harness_record"]["kind"] == "dispatch"


def test_a_checkpoint_is_ownership_evidence(config, store, repo, worktree_root):
    path = _worktree(repo, worktree_root, "C-1")
    run, _ = store.create_run(task_id="C-1", prompt="p")
    store.save_checkpoint(run.id, task_id="C-1", iteration=1, branch="harness/c-1",
                          worktree_path=str(path))

    decision = _trust(config, store, worktree_root).register(path, run_id=run.id)

    assert decision.granted is True
    assert decision.checks["harness_record"]["kind"] == "checkpoint"


# ---------------------------------------------------------------------------
# the file is treated as somebody's real configuration
# ---------------------------------------------------------------------------

def test_every_unrelated_entry_survives(config, store, repo, worktree_root):
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)
    before = json.loads(config.read_text())

    _trust(config, store, worktree_root).register(path, run_id=run.id)

    after = json.loads(config.read_text())
    assert after["hasCompletedOnboarding"] is True
    assert after["oauthAccount"] == before["oauthAccount"]
    assert after["projects"]["/home/someone/private-work"] == \
        before["projects"]["/home/someone/private-work"], \
        "another project's trust and tool permissions are untouched"


def test_only_the_trust_key_is_added_to_an_existing_entry(
        config, store, repo, worktree_root):
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)
    resolved = os.path.realpath(path)
    existing = json.loads(config.read_text())
    existing["projects"][resolved] = {"allowedTools": ["Bash(git status)"],
                                      "lastCost": 1.23}
    config.write_text(json.dumps(existing, indent=2))

    _trust(config, store, worktree_root).register(path, run_id=run.id)

    entry = json.loads(config.read_text())["projects"][resolved]
    assert entry["hasTrustDialogAccepted"] is True
    assert entry["allowedTools"] == ["Bash(git status)"], "not clobbered"
    assert entry["lastCost"] == 1.23


def test_the_config_is_backed_up_before_it_is_changed(
        config, store, repo, worktree_root):
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)
    original = config.read_text()

    decision = _trust(config, store, worktree_root).register(path, run_id=run.id)

    assert decision.backup_path, "a backup path is reported"
    assert os.path.exists(decision.backup_path)
    assert open(decision.backup_path).read() == original, \
        "the backup is the file as it was BEFORE the write"


def test_an_absent_config_is_created_rather_than_refused(
        tmp_path, store, repo, worktree_root):
    missing = tmp_path / "no-config-here.json"
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)

    decision = WorkspaceTrust(config_path=missing, store=store,
                              worktree_roots=[str(worktree_root)]).register(
        path, run_id=run.id)

    assert decision.granted is True
    assert decision.backup_path is None, "nothing existed to back up"
    assert json.loads(missing.read_text())["projects"][
        os.path.realpath(path)]["hasTrustDialogAccepted"] is True


def test_the_written_config_is_private(config, store, repo, worktree_root):
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)

    _trust(config, store, worktree_root).register(path, run_id=run.id)

    assert (os.stat(config).st_mode & 0o077) == 0, "0600, like the original"


# ---------------------------------------------------------------------------
# a corrupt config is preserved, never replaced
# ---------------------------------------------------------------------------

def test_a_corrupt_config_is_backed_up_and_the_grant_refused(
        tmp_path, store, repo, worktree_root):
    """Writing a clean file over an unparseable one would 'succeed' while
    destroying every account, server and permission it held."""
    broken = tmp_path / "claude.json"
    broken.write_text('{"projects": {"a": ' )  # truncated mid-object
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)

    decision = WorkspaceTrust(config_path=broken, store=store,
                              worktree_roots=[str(worktree_root)]).register(
        path, run_id=run.id)

    assert decision.granted is False
    assert decision.reason == trust.CONFIG_CORRUPT
    assert decision.backup_path and os.path.exists(decision.backup_path)
    assert broken.read_text() == '{"projects": {"a": ', \
        "the corrupt file is left exactly as it was"


def test_a_config_that_is_not_an_object_is_refused(
        tmp_path, store, repo, worktree_root):
    odd = tmp_path / "claude.json"
    odd.write_text('["not", "an", "object"]')
    path = _worktree(repo, worktree_root)
    _run_owning(store, path)

    decision = WorkspaceTrust(config_path=odd, store=store,
                              worktree_roots=[str(worktree_root)]).register(path)

    assert decision.granted is False
    assert decision.reason == trust.CONFIG_CORRUPT
    assert json.loads(odd.read_text()) == ["not", "an", "object"]


def test_a_config_whose_projects_key_is_wrong_is_repaired_not_lost(
        tmp_path, store, repo, worktree_root):
    """`projects` being the wrong TYPE is not the same as the file being
    corrupt: the rest of it parses and must survive."""
    odd = tmp_path / "claude.json"
    odd.write_text(json.dumps({"projects": "surprise", "numStartups": 42}))
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)

    decision = WorkspaceTrust(config_path=odd, store=store,
                              worktree_roots=[str(worktree_root)]).register(
        path, run_id=run.id)

    after = json.loads(odd.read_text())
    assert decision.granted is True
    assert after["numStartups"] == 42, "the rest of the config survived"
    assert after["projects"][os.path.realpath(path)]["hasTrustDialogAccepted"] is True


# ---------------------------------------------------------------------------
# the audit record
# ---------------------------------------------------------------------------

def test_a_grant_is_audited_with_run_path_and_source(
        config, store, repo, worktree_root):
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)

    _trust(config, store, worktree_root).register(path, run_id=run.id,
                                                  source="pilot-smoke")

    events = [e for e in store.events(run.id)
              if e["event_type"] == trust.TRUST_REGISTERED]
    assert len(events) == 1
    event = events[0]
    assert event["metadata"]["path"] == os.path.realpath(path)
    assert event["metadata"]["source"] == "pilot-smoke"
    assert event["metadata"]["granted"] is True
    assert event["actor"] == "pilot-smoke"


def test_a_refusal_is_audited_too(config, store, repo, worktree_root, tmp_path):
    """A grant that quietly did nothing is indistinguishable, to the caller,
    from one that worked -- and the caller is an engine that will otherwise
    spend a spawn and a human decision finding out."""
    run, _ = store.create_run(task_id="X", prompt="p")
    outside = tmp_path / "not-ours"
    outside.mkdir()

    _trust(config, store, worktree_root).register(outside, run_id=run.id)

    events = [e for e in store.events(run.id)
              if e["event_type"] == trust.TRUST_REFUSED]
    assert len(events) == 1
    assert events[0]["reason"] == trust.OUTSIDE_APPROVED_ROOTS


def test_an_external_audit_sink_sees_every_decision(
        config, store, repo, worktree_root):
    seen = []
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)
    service = WorkspaceTrust(config_path=config, store=store,
                             worktree_roots=[str(worktree_root)],
                             audit=lambda event, payload: seen.append((event, payload)))

    service.register(path, run_id=run.id)

    assert [e for e, _ in seen] == [trust.TRUST_REGISTERED]
    assert seen[0][1]["path"] == os.path.realpath(path)


def test_a_failing_audit_sink_never_fails_the_grant(
        config, store, repo, worktree_root):
    def explode(event, payload):
        raise RuntimeError("the audit backend is down")

    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)
    service = WorkspaceTrust(config_path=config, store=store,
                             worktree_roots=[str(worktree_root)], audit=explode)

    decision = service.register(path, run_id=run.id)

    assert decision.granted is True


# ---------------------------------------------------------------------------
# reading back
# ---------------------------------------------------------------------------

def test_is_trusted_reflects_what_was_written(config, store, repo, worktree_root):
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)
    service = _trust(config, store, worktree_root)

    assert service.is_trusted(path) is False
    service.register(path, run_id=run.id)
    assert service.is_trusted(path) is True


def test_is_trusted_is_false_on_a_corrupt_config(tmp_path, store, worktree_root):
    broken = tmp_path / "claude.json"
    broken.write_text("{{{")
    service = WorkspaceTrust(config_path=broken, store=store,
                             worktree_roots=[str(worktree_root)])
    assert service.is_trusted(worktree_root / "anything") is False


def test_every_refusal_reason_is_on_the_closed_list():
    """A reason nobody enumerated is a reason nobody handles."""
    for name in ("NOT_ABSOLUTE", "NOT_A_DIRECTORY", "OUTSIDE_APPROVED_ROOTS",
                 "IS_A_ROOT_ITSELF", "NOT_A_GIT_WORKTREE", "NOT_HARNESS_OWNED",
                 "NO_STORE", "CONFIG_CORRUPT", "CONFIG_UNWRITABLE"):
        assert getattr(trust, name) in trust.REFUSAL_REASONS


# ---------------------------------------------------------------------------
# the roots an operator's existing declaration implies
# ---------------------------------------------------------------------------

def test_the_derived_root_is_narrower_than_the_approved_root():
    """A new config key would be a second place to get this wrong. What is
    derived is strictly narrower than what the operator already approved."""
    roots = trust.default_worktree_roots(["/home/x/workspace", "/home/x"])
    assert roots == ("/home/x/workspace/.terminal-mcp-worktrees",
                     "/home/x/.terminal-mcp-worktrees")


def test_a_directory_directly_under_an_approved_root_is_still_refused(
        config, store, tmp_path):
    """The whole point of narrowing. `allowed_cwd_roots` lets a SESSION open
    there; it does not make every directory under it auto-trustable."""
    approved = tmp_path / "workspace"
    (approved / "my-real-project").mkdir(parents=True)
    service = WorkspaceTrust(config_path=config, store=store,
                             worktree_roots=trust.default_worktree_roots([str(approved)]))

    decision = service.register(approved / "my-real-project")

    assert decision.granted is False
    assert decision.reason == trust.OUTSIDE_APPROVED_ROOTS


def test_a_harness_worktree_under_the_derived_root_is_accepted(
        config, store, tmp_path):
    approved = tmp_path / "workspace"
    repo = approved / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=repo, check=True)

    path = approved / ".terminal-mcp-worktrees" / "harness" / "T-9"
    subprocess.run(["git", "worktree", "add", "--force", "-B", "harness/t-9",
                    str(path), "HEAD"], cwd=repo, capture_output=True, check=True)
    run, _ = store.create_run(task_id="T-9", prompt="p")
    store.patch_run(run.id, worktree_path=str(path))

    service = WorkspaceTrust(config_path=config, store=store,
                             worktree_roots=trust.default_worktree_roots([str(approved)]))
    decision = service.register(path, run_id=run.id)

    assert decision.granted is True, decision.reason


def test_an_empty_declaration_yields_no_roots_and_trusts_nothing():
    assert trust.default_worktree_roots([]) == ()
    assert trust.default_worktree_roots(["", "   "]) == ()


# ---------------------------------------------------------------------------
# trust must not outlive the worktree it was granted for
# ---------------------------------------------------------------------------

def test_revoking_a_removed_worktree_drops_its_entry(config, store, repo, worktree_root):
    """Harness worktrees are named after their task, so the same path recurs
    every time that task runs. An entry left behind pre-approves whatever
    appears at that path next."""
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)
    service = _trust(config, store, worktree_root)
    service.register(path, run_id=run.id)
    assert service.is_trusted(path) is True

    subprocess.run(["git", "worktree", "remove", "--force", str(path)],
                   cwd=repo, capture_output=True, check=True)
    decision = service.revoke(path, run_id=run.id)

    assert decision.granted is True and decision.reason == "revoked"
    assert os.path.realpath(path) not in json.loads(config.read_text())["projects"]


def test_a_path_that_still_exists_is_not_revoked(config, store, repo, worktree_root):
    """This is cleanup for a removed worktree, not a general untrust verb."""
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)
    service = _trust(config, store, worktree_root)
    service.register(path, run_id=run.id)

    decision = service.revoke(path, run_id=run.id)

    assert decision.granted is False
    assert decision.reason == trust.STILL_EXISTS
    assert service.is_trusted(path) is True, "still trusted; nothing was touched"


def test_revoke_will_not_touch_a_path_outside_the_approved_roots(
        config, store, worktree_root, tmp_path):
    """It can never remove a person's own entry, even by mistake."""
    service = _trust(config, store, worktree_root)
    before = json.loads(config.read_text())["projects"]

    decision = service.revoke(tmp_path / "somebody-elses-deleted-dir")

    assert decision.granted is False
    assert decision.reason == trust.OUTSIDE_APPROVED_ROOTS
    assert json.loads(config.read_text())["projects"] == before


def test_revoking_what_was_never_trusted_is_not_an_error(
        config, store, worktree_root):
    decision = _trust(config, store, worktree_root).revoke(worktree_root / "GONE")
    assert decision.granted is True and decision.already is True
    assert decision.reason == "not_present"


def test_revoke_preserves_every_other_entry(config, store, repo, worktree_root):
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)
    service = _trust(config, store, worktree_root)
    service.register(path, run_id=run.id)
    subprocess.run(["git", "worktree", "remove", "--force", str(path)],
                   cwd=repo, capture_output=True, check=True)

    service.revoke(path, run_id=run.id)

    after = json.loads(config.read_text())
    assert after["hasCompletedOnboarding"] is True
    assert after["projects"]["/home/someone/private-work"]["allowedTools"] == ["Bash(ls)"]


def test_a_revocation_is_audited(config, store, repo, worktree_root):
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)
    service = _trust(config, store, worktree_root)
    service.register(path, run_id=run.id)
    subprocess.run(["git", "worktree", "remove", "--force", str(path)],
                   cwd=repo, capture_output=True, check=True)

    service.revoke(path, run_id=run.id, source="pilot-cleanup")

    events = [e for e in store.events(run.id) if e["event_type"] == trust.TRUST_REVOKED]
    assert len(events) == 1
    assert events[0]["metadata"]["path"] == os.path.realpath(path)
    assert events[0]["actor"] == "pilot-cleanup"


def test_the_same_path_can_be_trusted_again_after_a_revoke(
        config, store, repo, worktree_root):
    """The recurrence this exists for: task T runs, is cleaned up, runs again."""
    path = _worktree(repo, worktree_root)
    run = _run_owning(store, path)
    service = _trust(config, store, worktree_root)
    service.register(path, run_id=run.id)
    subprocess.run(["git", "worktree", "remove", "--force", str(path)],
                   cwd=repo, capture_output=True, check=True)
    service.revoke(path, run_id=run.id)
    assert service.is_trusted(path) is False

    again = _worktree(repo, worktree_root)
    run2 = _run_owning(store, again, task_id="T-1-again")
    decision = service.register(again, run_id=run2.id)

    assert decision.granted is True
    assert decision.already is False, "the fresh worktree earned trust on its own"
