"""Policy auto-load on real Work runs, including across a process restart."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from terminal_mcp import work_policy as wp
from terminal_mcp import work_service, work_store


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    wp.ensure_policy_file(str(root))
    return root


@pytest.fixture()
def service(tmp_path):
    store = work_store.WorkStore(tmp_path / "work.db")
    yield work_service.WorkService(store)
    store.close()


def test_a_work_run_records_the_policy_it_loaded(service, project):
    created = service.create(title="Fix badge", goal="badge không chồng", lane="demo-work",
                             created_by="dev", project_root=str(project))
    policy = created["work"]["metadata"]["policy"]
    assert policy["policy_version"] == wp.WORK_POLICY_VERSION
    assert policy["policy_hash"] and policy["loaded_at"]
    assert policy["source"].endswith(wp.POLICY_FILENAME)
    assert policy["override_present"] is False


def test_a_run_created_after_a_bump_records_the_new_version(service, project):
    first = service.create(title="A", goal="g", lane="demo-work", project_root=str(project))
    path = wp.policy_dir(str(project)) / wp.POLICY_FILENAME
    path.write_text(path.read_text().replace(
        f"WORK_POLICY_VERSION: {wp.WORK_POLICY_VERSION}", "WORK_POLICY_VERSION: 1.3.0", 1))
    second = service.create(title="B", goal="g", lane="demo-work", project_root=str(project))
    assert first["work"]["metadata"]["policy"]["policy_version"] == wp.WORK_POLICY_VERSION
    assert second["work"]["metadata"]["policy"]["policy_version"] == "1.3.0"
    # The earlier run keeps the rules it planned against.
    stored = service.store.get_run(first["work"]["work_id"])
    assert stored.metadata["policy"]["policy_version"] == wp.WORK_POLICY_VERSION


def test_a_project_override_is_visible_on_the_run(service, project):
    (wp.policy_dir(str(project)) / wp.PROJECT_POLICY_FILENAME).write_text(
        "<!-- WORK_POLICY_VERSION: 1.0.0-proj.2 -->\n"
        "## Release Levels\n\nPRODUCTION requires a fresh backup.\n")
    created = service.create(title="A", goal="g", lane="demo-work", project_root=str(project))
    policy = created["work"]["metadata"]["policy"]
    assert policy["override_present"] is True
    assert policy["override_version"] == "1.0.0-proj.2"


def test_an_unreadable_policy_does_not_prevent_work_from_being_created(service, tmp_path):
    created = service.create(title="A", goal="g", lane="demo-work",
                             project_root=str(tmp_path / "does-not-exist"))
    # Degrades to the built-in rules; the run is still created and the
    # fallback is on the record rather than assumed.
    assert "work" in created
    assert created["work"]["metadata"]["policy"]["source"] == "built-in"


def test_ordinary_lanes_are_still_refused_and_store_nothing(service, project):
    result = service.create(title="A", goal="g", lane="m1", project_root=str(project))
    assert result["error"] == "LANE_NOT_A_WORK_SESSION"
    # Rejected before anything is written: a durable run bound to an ordinary
    # session would invite every later scheduling decision to do the wrong thing.
    assert service.store.list_runs() == []


def test_policy_survives_a_restart_because_it_lives_on_disk(project, tmp_path):
    """A fresh interpreter -- the restart case -- still loads the rules.

    This is the point of the whole module: after `systemctl restart`, a new
    Work task must know FAST_FIX and NEEDS_REDEFINE without anyone
    re-explaining them. Running it in a subprocess is what proves the
    knowledge is on disk and not in this process's memory.
    """
    script = textwrap.dedent(f"""
        import json, sys
        sys.path.insert(0, {str(project.parent.parent)!r})
        from terminal_mcp.work_policy import policy_for_task
        result = policy_for_task({str(project)!r}, session="fresh-work",
                                 sections=["modes", "needs_redefine"])
        print(json.dumps({{
            "version": result["binding"]["policy_version"],
            "knows_fast_fix": "FAST_FIX" in result["text"],
            "knows_redefine": "NEEDS_REDEFINE" in result["text"],
            "chars": len(result["text"]),
        }}))
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          cwd=str(project))
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["version"] == wp.WORK_POLICY_VERSION
    assert payload["knows_fast_fix"] and payload["knows_redefine"]
    # And it learned them without pulling the whole policy into context.
    assert payload["chars"] < len(wp.CANONICAL_POLICY) / 3
