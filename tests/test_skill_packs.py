"""Shared skill packs (issue #71): pinned install, rollback, lazy router skills,
and gate verdicts that never call a partial review a pass.

Everything runs against a throwaway git repository and fake gate scripts
written in Python, so no network, no Node and no browser are needed. The real
kit is exercised by tests/test_skill_packs_e2e.py.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from terminal_mcp import skill_packs as sp

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own HOME, pack home and manifest dir, so nothing can
    reach the operator's real ~/.claude, ~/.codex or claude CLI."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_SKILLS_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv(sp.PACK_HOME_ENV, str(tmp_path / "packs"))
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    monkeypatch.setenv(sp.MANIFEST_DIR_ENV, str(manifests))
    monkeypatch.setattr(sp, "find_executable", lambda name: None if name in ("claude", "npm") else
                        __import__("shutil").which(name))
    return tmp_path


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


def make_upstream(tmp_path: Path, version: str, *, extra: str = "") -> tuple[Path, str]:
    """A tiny kit repo tagged v<version>: LICENSE, package.json, two skills."""
    repo = tmp_path / f"upstream-{version}"
    (repo / ".claude" / "skills" / "gate").mkdir(parents=True)
    (repo / ".claude" / "skills" / "design-screen").mkdir(parents=True)
    (repo / "LICENSE").write_text("MIT License\n\nCopyright (c) 2026 Test\n")
    (repo / "package.json").write_text(json.dumps({"name": "kit", "version": version}))
    (repo / "AGENTS.md").write_text("# kit doctrine\n")
    (repo / ".claude" / "skills" / "gate" / "SKILL.md").write_text(
        "---\nname: gate\ndescription: Run the one-command quality gate.\n---\nbody\n")
    (repo / ".claude" / "skills" / "design-screen" / "SKILL.md").write_text(
        "---\nname: design-screen\ndescription: Design a page end to end.\n---\nbody\n" + extra)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", f"v{version}", cwd=repo)
    _git("tag", f"v{version}", cwd=repo)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    return repo, commit


def write_manifest(tmp_path: Path, repo: Path, commit: str, version: str, **overrides) -> sp.Manifest:
    checkout = tmp_path / f"hash-{version}"
    subprocess.run(["git", "clone", "-q", str(repo), str(checkout)], check=True)
    count, digest = sp.tree_digest(checkout)
    data = {
        "schema": 1, "id": "kit-pack", "display_name": "Kit", "version": version,
        "source": {"repo": str(repo), "tag": f"v{version}", "commit": commit,
                   "tree_sha256": digest, "tree_files": count},
        "license": {"spdx": "MIT", "file": "LICENSE", "sha256": sp._sha256_file(checkout / "LICENSE")},
        "runtime": {"node_min": "20", "playwright": ""},
        "router_skill": "ux-ui-kit", "hosts": ["claude", "codex"],
        "register_mcp": {"claude": False, "codex": False}, "mcp_server_name": "ux-ui-gates",
    }
    data.update(overrides)
    path = Path(os.environ[sp.MANIFEST_DIR_ENV]) / "kit-pack.json"
    path.write_text(json.dumps(data))
    return sp.parse_manifest(data, str(path))


# -- the shipped manifest ----------------------------------------------------

def test_the_shipped_manifest_pins_a_full_commit_hash_and_licence():
    path = REPO_ROOT / "deploy" / "skill-packs" / "ux-ui-agent-skills.json"
    manifest = sp.parse_manifest(json.loads(path.read_text()), str(path))
    assert manifest.version == "2.9.8" and manifest.tag == "v2.9.8"
    assert manifest.commit == "62092bd83bc3df0bcb9a2b3b643305ab11c547da"
    assert manifest.license_spdx == "MIT"
    assert manifest.upstream_counts["claude_skills"] == 25
    assert manifest.upstream_counts["design_systems"] == 138
    # MCP is opt-in for both hosts: registering writes the user config and loads in every session.
    assert manifest.register_mcp == {"claude": False, "codex": False}


@pytest.mark.parametrize("field,value", [
    ("commit", "62092bd"),                       # a short hash is not a pin
    ("tree_sha256", "abc"),
    ("repo", "git@github.com:x/y.git"),
])
def test_a_manifest_that_is_not_a_real_pin_is_refused(field, value):
    data = json.loads((REPO_ROOT / "deploy" / "skill-packs" / "ux-ui-agent-skills.json").read_text())
    if field == "repo":
        data["source"]["repo"] = value
    else:
        data["source"][field] = value
    with pytest.raises(sp.SkillPackError):
        sp.parse_manifest(data)


def test_a_manifest_id_cannot_traverse():
    data = json.loads((REPO_ROOT / "deploy" / "skill-packs" / "ux-ui-agent-skills.json").read_text())
    data["id"] = "../../etc"
    with pytest.raises(sp.SkillPackError, match="slug"):
        sp.parse_manifest(data)


# -- install / verify / rollback ---------------------------------------------

def test_install_verifies_the_pin_makes_the_kit_read_only_and_records_provenance(isolated):
    repo, commit = make_upstream(isolated, "1.0.0")
    manifest = write_manifest(isolated, repo, commit, "1.0.0")
    result = sp.install(manifest)
    assert result["switched"] is True and result["previous"] is None
    kit = sp.pack_dir("kit-pack") / "current" / "kit"
    assert (kit / "AGENTS.md").read_text() == "# kit doctrine\n"
    assert not (kit / ".git").exists()
    assert not os.access(kit / "AGENTS.md", os.W_OK)
    provenance = json.loads((sp.pack_dir("kit-pack") / "current" / "PROVENANCE.json").read_text())
    assert provenance["commit"] == commit and provenance["license"] == "MIT"
    assert sp.install(manifest)["reused"] is True        # idempotent


def test_a_moved_tag_is_refused_and_nothing_becomes_current(isolated):
    repo, commit = make_upstream(isolated, "1.0.0")
    manifest = write_manifest(isolated, repo, "0" * 40, "1.0.0")
    with pytest.raises(sp.SkillPackError, match="moved tag"):
        sp.install(manifest)
    assert not (sp.pack_dir("kit-pack") / "current").exists()


def test_content_that_does_not_match_the_tree_hash_is_refused(isolated):
    repo, commit = make_upstream(isolated, "1.0.0")
    manifest = write_manifest(isolated, repo, commit, "1.0.0")
    tampered = sp.parse_manifest({**json.loads(Path(manifest.path).read_text()),
                                  "source": {"repo": str(repo), "tag": "v1.0.0", "commit": commit,
                                             "tree_sha256": "f" * 64}})
    with pytest.raises(sp.SkillPackError, match="tree sha256"):
        sp.install(tampered)
    assert not (sp.pack_dir("kit-pack") / "current").exists()


def test_status_detects_a_tampered_tree(isolated):
    repo, commit = make_upstream(isolated, "1.0.0")
    manifest = write_manifest(isolated, repo, commit, "1.0.0")
    sp.install(manifest)
    target = sp.pack_dir("kit-pack") / "current" / "kit" / "AGENTS.md"
    os.chmod(target, 0o644)
    target.write_text("# edited by someone\n")
    report = sp.status("kit-pack")
    assert report["state"] == sp.TAMPERED
    assert "tree sha256" in report["detail"]


def test_rollback_swaps_current_and_previous_and_sync_respects_the_pin(isolated):
    repo1, commit1 = make_upstream(isolated, "1.0.0")
    sp.install(write_manifest(isolated, repo1, commit1, "1.0.0"))
    repo2, commit2 = make_upstream(isolated, "2.0.0", extra="new\n")
    manifest2 = write_manifest(isolated, repo2, commit2, "2.0.0")
    assert sp.install(manifest2)["previous"] == f"1.0.0-{commit1[:12]}"

    back = sp.rollback("kit-pack")
    assert back["current"] == f"1.0.0-{commit1[:12]}"
    assert sp.status("kit-pack")["state"] == sp.DRIFT       # honest: not the manifest pin

    sp.sync(["kit-pack"])                                    # a pinned rollback is not undone by sync
    assert sp.status("kit-pack")["release"] == f"1.0.0-{commit1[:12]}"
    sp.sync(["kit-pack"], force=True)
    assert sp.status("kit-pack")["release"] == f"2.0.0-{commit2[:12]}"


def test_rollback_without_a_previous_release_is_an_error(isolated):
    repo, commit = make_upstream(isolated, "1.0.0")
    sp.install(write_manifest(isolated, repo, commit, "1.0.0"))
    with pytest.raises(sp.SkillPackError, match="no previous"):
        sp.rollback("kit-pack")


# -- router skills -------------------------------------------------------------

def test_sync_writes_one_router_skill_per_host_and_touches_nothing_else(isolated):
    repo, commit = make_upstream(isolated, "1.0.0")
    write_manifest(isolated, repo, commit, "1.0.0")
    project = isolated / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("project rules\n")
    report = sp.sync(["kit-pack"])
    assert report["ok"] is True
    claude = Path.home() / ".claude" / "skills" / "ux-ui-kit" / "SKILL.md"
    codex = Path.home() / ".codex" / "skills" / "ux-ui-kit" / "SKILL.md"
    text = claude.read_text()
    assert text.startswith("---\nname: ux-ui-kit\ndescription: ")
    assert "Use ONLY for UI" in text and "non-UI tasks" in text           # lazy by description
    assert "Never copy the kit into the project" in text
    assert "existing design tokens, theme, components and brand" in text  # project branding wins
    assert "`design-screen` - Design a page end to end." in text
    assert "Exit 2 = INCOMPLETE" in text
    assert "not automatic here" in codex.read_text()                      # Codex caveat stated
    assert (project / "AGENTS.md").read_text() == "project rules\n"
    assert sorted(p.name for p in (Path.home() / ".claude" / "skills").iterdir()) == ["ux-ui-kit"]


def test_a_foreign_skill_of_the_same_name_is_never_overwritten(isolated):
    repo, commit = make_upstream(isolated, "1.0.0")
    write_manifest(isolated, repo, commit, "1.0.0")
    foreign = Path.home() / ".claude" / "skills" / "ux-ui-kit"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("mine\n")
    report = sp.sync(["kit-pack"])
    routers = {r["host"]: r for r in report["packs"][0]["integrate"]["router_skills"]}
    assert routers["claude"]["status"] == "CONFLICT"
    assert (foreign / "SKILL.md").read_text() == "mine\n"
    sp.set_enabled("kit-pack", False)
    assert (foreign / "SKILL.md").read_text() == "mine\n"                  # disable leaves it too


def test_disable_removes_what_sessions_see_and_enable_restores_it_offline(isolated):
    repo, commit = make_upstream(isolated, "1.0.0")
    write_manifest(isolated, repo, commit, "1.0.0")
    sp.sync(["kit-pack"])
    router = Path.home() / ".codex" / "skills" / "ux-ui-kit"
    sp.set_enabled("kit-pack", False)
    assert not router.exists()
    assert sp.status("kit-pack")["state"] == sp.DISABLED
    assert sp.sync(["kit-pack"])["packs"][0]["status"] == sp.DISABLED       # sync does not re-enable
    subprocess.run(["git", "tag", "-d", "v1.0.0"], cwd=repo, check=True, capture_output=True)
    sp.set_enabled("kit-pack", True)                                        # no fetch needed
    assert (router / "SKILL.md").is_file()


def test_codex_mcp_block_is_added_once_and_removed_cleanly(isolated):
    repo, commit = make_upstream(isolated, "1.0.0")
    manifest = write_manifest(isolated, repo, commit, "1.0.0")
    config = Path.home() / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('model = "x"\n')
    assert sp._codex_mcp(manifest, register=True)["status"] == "REGISTERED"
    assert sp._codex_mcp(manifest, register=True)["status"] == "PRESENT"
    assert config.read_text().count("[mcp_servers.ux-ui-gates]") == 1
    assert sp._codex_mcp(manifest, register=False)["status"] == "REMOVED"
    assert config.read_text().strip() == 'model = "x"'


def test_codex_mcp_never_overwrites_an_existing_table(isolated):
    repo, commit = make_upstream(isolated, "1.0.0")
    manifest = write_manifest(isolated, repo, commit, "1.0.0")
    config = Path.home() / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[mcp_servers.ux-ui-gates]\ncommand = "theirs"\n')
    assert sp._codex_mcp(manifest, register=True)["status"] == "CONFLICT"
    assert sp._codex_mcp(manifest, register=False)["status"] == "UNCHANGED"
    assert 'command = "theirs"' in config.read_text()


def test_the_launcher_runs_from_a_standalone_copy(isolated):
    """The launcher must keep working after the checkout that installed it is
    replaced, so it runs a copy of this module, stdlib only."""
    repo, commit = make_upstream(isolated, "1.0.0")
    write_manifest(isolated, repo, commit, "1.0.0")
    sp.sync(["kit-pack"])
    proc = subprocess.run([str(sp.launcher_path()), "status", "kit-pack", "--no-verify"],
                          capture_output=True, text=True, cwd=str(isolated),
                          env={**os.environ, "PYTHONPATH": ""})
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["state"] in (sp.READY, sp.DEGRADED)


# -- gate classification -------------------------------------------------------

@pytest.mark.parametrize("code,output,expected", [
    (0, "OK: no hardcoded values found.", sp.PASS),
    (0, "usage: node scripts/verify_focustrap.mjs <file.html> --open=<trigger>", sp.NOT_RUN),
    (0, "verify_rtl: playwright not installed — SKIPPED", sp.BLOCKED),
    (1, "axe_audit: playwright not installed — REQUIRED, FAILING", sp.BLOCKED),
    (1, "browserType.launch: Executable doesn't exist at /x/chrome", sp.BLOCKED),
    (2, "ERROR: no target given. A gate with nothing to measure is not a pass.", sp.BLOCKED),
    (1, "FAIL: 2 required pair(s) below WCAG minimum", sp.FAIL),
    (1, "  at app.css:12:5  raw hex #ff0000", sp.FAIL),
    (1, "Traceback (most recent call last):\n  File \"x.py\", line 1", sp.ERROR),
    (1, "TypeError: boom\n    at run (file:///kit/scripts/x.mjs:10:3)", sp.ERROR),
    (None, "timed out", sp.ERROR),
    (139, "", sp.ERROR),
])
def test_classify_never_turns_an_unmeasured_run_into_a_pass(code, output, expected):
    assert sp.classify(code, output) == expected


FAKE_GATES = [
    {"name": "check_no_emoji", "file": "check_no_emoji.py", "runner": "python3", "browser": False},
    {"name": "validate_tokens", "file": "validate_tokens.py", "runner": "python3", "browser": False},
    {"name": "validate_theme_refs", "file": "validate_theme_refs.py", "runner": "python3", "browser": False},
    {"name": "validate_component_spec", "file": "validate_component_spec.py", "runner": "python3",
     "browser": False},
    {"name": "verify_focustrap", "file": "verify_focustrap.py", "runner": "python3", "browser": True},
    {"name": "axe_audit", "file": "axe_audit.py", "runner": "python3", "browser": True},
]


@pytest.fixture
def fake_kit(isolated, monkeypatch):
    """An installed release whose gates are Python scripts. A gate fails when
    its target contains the word BROKEN, which is all a verdict test needs."""
    release = sp.pack_dir("ux-ui-agent-skills") / "releases" / "fake"
    scripts = release / "kit" / "scripts"
    scripts.mkdir(parents=True)
    body = ("import sys, pathlib\n"
            "args=[a for a in sys.argv[1:] if not a.startswith('--')]\n"
            "bad=any('BROKEN' in pathlib.Path(a).read_text() for a in args if pathlib.Path(a).is_file())\n"
            "print('FAIL: found it' if bad else 'OK: clean'); sys.exit(1 if bad else 0)\n")
    for gate in FAKE_GATES:
        (scripts / gate["file"]).write_text(body)
    (sp.pack_dir("ux-ui-agent-skills")).mkdir(parents=True, exist_ok=True)
    sp._point(sp.pack_dir("ux-ui-agent-skills") / "current", release)
    monkeypatch.setattr(sp, "catalogue", lambda kit: [dict(g, measures="") for g in FAKE_GATES])
    project = isolated / "project"
    (project / "spec" / "components").mkdir(parents=True)
    (project / "index.html").write_text("<h1>ok</h1>")
    (project / "tokens.json").write_text("{}")
    (project / "theme.css").write_text(":root{}")
    return project


def full_inputs(project):
    return dict(tokens=str(project / "tokens.json"), theme=str(project / "theme.css"),
                focustrap_open="#open", component_root=str(project / "spec"))


def test_review_passes_only_when_every_gate_ran_and_passed(fake_kit):
    result = sp.review([str(fake_kit / "index.html")], **full_inputs(fake_kit))
    assert result["verdict"] == sp.PASS and result["exit_code"] == 0
    assert result["counts"][sp.PASS] == len(FAKE_GATES)
    assert all(run["exit_code"] == 0 for run in result["runs"])


def test_review_fails_with_the_real_exit_code_of_the_gate_that_found_a_defect(fake_kit):
    (fake_kit / "index.html").write_text("<h1>BROKEN</h1>")
    result = sp.review([str(fake_kit / "index.html")], **full_inputs(fake_kit))
    assert result["verdict"] == sp.FAIL and result["exit_code"] == 1
    assert result["gates"]["axe_audit"]["exit_codes"] == [1]
    assert "found it" in sp.format_review(result)


def test_a_review_missing_inputs_is_incomplete_even_when_everything_that_ran_passed(fake_kit):
    result = sp.review([str(fake_kit / "index.html")])
    assert result["verdict"] == sp.INCOMPLETE and result["exit_code"] == 2
    reasons = {name: g.get("reasons", [""])[0] for name, g in result["gates"].items()}
    assert result["gates"]["validate_tokens"]["status"] == sp.NOT_RUN
    assert "kit's own tokens" in reasons["validate_tokens"]
    assert "kit's own specs" in reasons["validate_component_spec"]
    assert "--focustrap-open" in reasons["verify_focustrap"]
    assert "PASS:" not in result["summary"]


def test_skipping_the_browser_is_incomplete_not_a_pass(fake_kit):
    result = sp.review([str(fake_kit / "index.html")], skip_browser=True, **full_inputs(fake_kit))
    assert result["verdict"] == sp.INCOMPLETE
    assert result["gates"]["axe_audit"]["status"] == sp.NOT_RUN


def test_a_path_that_does_not_exist_measures_nothing(fake_kit):
    result = sp.review([str(fake_kit / "nope.html")])
    assert result["verdict"] == sp.INCOMPLETE and result["exit_code"] == 2
    assert "not a pass" in result["summary"]


def test_review_records_the_last_gate_result_for_admin_status(fake_kit):
    sp.review([str(fake_kit / "index.html")], **full_inputs(fake_kit))
    assert sp.load_state("ux-ui-agent-skills")["last_gate"]["verdict"] == sp.PASS


# -- MCP surface ---------------------------------------------------------------

def _call(messages):
    out = io.StringIO()
    sp.serve_mcp(io.StringIO("".join(json.dumps(m) + "\n" for m in messages)), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_mcp_handshake_lists_the_gate_tools(fake_kit):
    replies = _call([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                     {"jsonrpc": "2.0", "method": "notifications/initialized"},
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    assert replies[0]["result"]["protocolVersion"] == sp.MCP_PROTOCOL
    assert "never passes" in replies[0]["result"]["instructions"]
    names = {t["name"] for t in replies[1]["result"]["tools"]}
    assert {"review_ui", "run_gate", "list_gates", "get_doctrine", "get_design_system",
            "get_tokens", "pack_status"} == names


def test_mcp_review_is_an_error_unless_it_is_a_full_pass(fake_kit):
    html = str(fake_kit / "index.html")
    replies = _call([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "review_ui", "arguments": {"paths": [html]}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "review_ui", "arguments": {"paths": [html], **full_inputs(fake_kit)}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
    ])
    assert replies[0]["result"]["isError"] is True
    assert replies[0]["result"]["content"][0]["text"].startswith("INCOMPLETE")
    assert replies[1]["result"]["isError"] is False
    assert replies[2]["error"]["code"] == -32602


# -- activation ------------------------------------------------------------------

def test_only_ui_tasks_activate_the_pack(isolated):
    manifest_src = REPO_ROOT / "deploy" / "skill-packs" / "ux-ui-agent-skills.json"
    (Path(os.environ[sp.MANIFEST_DIR_ENV]) / "ux-ui-agent-skills.json").write_text(manifest_src.read_text())
    assert sp.activation("Build a responsive landing page with our design tokens")["activate"] is True
    for task in ("rotate the node token", "fix the tmux screen capture", "migrate the postgres schema",
                 "add a form field to the API payload"):
        assert sp.activation(task)["activate"] is False, task


# -- fleet view ------------------------------------------------------------------

def test_node_profile_reports_the_shared_pack(isolated):
    from terminal_mcp import node_profile

    repo, commit = make_upstream(isolated, "1.0.0")
    write_manifest(isolated, repo, commit, "1.0.0")
    entry = {"id": "kit-pack", "provider": "terminal-mcp-pack", "pack": "kit-pack",
             "install_hint": "terminal-mcp-skill-packs sync"}
    assert node_profile.check_skill_pack(entry).status == node_profile.MISSING
    sp.sync(["kit-pack"])
    check = node_profile.check_skill_pack(entry)
    # No node/browser in this sandbox is not a pass: the gates would BLOCK.
    assert check.status in (node_profile.PASS, node_profile.DRIFT)
    assert check.found_version == f"1.0.0-{commit[:12]}"
    assert "router claude=installed, codex=installed" in check.detail


def test_the_shipped_node_profile_declares_the_pack():
    import yaml

    data = yaml.safe_load((REPO_ROOT / "deploy" / "node-profile.yaml").read_text())
    entry = next(e for e in data["skill_packs"] if e["id"] == "ux-ui-agent-skills")
    assert entry["provider"] == "terminal-mcp-pack" and entry.get("required") is False


def test_the_join_scripts_sync_packs_without_blocking_the_join():
    for script in ("install-node-agent.sh", "install-node-agent-macos.sh"):
        text = (REPO_ROOT / "deploy" / script).read_text()
        assert "terminal-mcp-skill-packs sync --best-effort" in text
        assert "TERMINAL_MCP_SKIP_SKILL_PACKS" in text


# -- upstream selfcheck (the README's "52") ----------------------------------------

def _fake_accuracy(monkeypatch, body: str) -> None:
    """accuracy_report.mjs as a Python script, run by a `node` that is Python."""
    (sp.current_kit() / "scripts" / "accuracy_report.mjs").write_text(body)
    monkeypatch.setattr(sp, "_selfcheck_workdir", lambda pack_id: (sp.current_kit(pack_id), "fake"))
    monkeypatch.setattr(sp, "node_runtime",
                        lambda *a: {"ok": True, "node": sys.executable, "version": "v99", "detail": "ok"})


def test_selfcheck_reports_the_real_count_and_exit_code(fake_kit, monkeypatch):
    _fake_accuracy(monkeypatch, "print('  PASS  Tokens valid')\nprint('  FAIL  axe sample-app')\n"
                                    "print('  PASS  RTL')\nimport sys; sys.exit(1)\n")
    result = sp.selfcheck()
    assert (result["verdict"], result["exit_code"], result["passed"], result["total"]) == (sp.FAIL, 1, 2, 3)
    assert result["failed"] == ["axe sample-app"]
    assert sp.load_state("ux-ui-agent-skills")["last_selfcheck"]["total"] == 3


def test_selfcheck_exit_zero_with_no_checks_printed_is_not_a_pass(fake_kit, monkeypatch):
    _fake_accuracy(monkeypatch, "print('nothing measured')\n")
    assert sp.selfcheck()["verdict"] == sp.ERROR


def test_selfcheck_without_node_is_blocked(fake_kit, monkeypatch):
    monkeypatch.setattr(sp, "node_runtime", lambda *a: {"ok": False, "node": None, "detail": "node not found"})
    result = sp.selfcheck()
    assert result["verdict"] == sp.BLOCKED and result["exit_code"] is None


def test_sync_registers_no_mcp_server_unless_asked(isolated, monkeypatch):
    repo, commit = make_upstream(isolated, "1.0.0")
    write_manifest(isolated, repo, commit, "1.0.0", register_mcp={"claude": False, "codex": False})

    def refuse(*a, **k):
        raise AssertionError("MCP registration touches the user's global config; it must be opt-in")

    monkeypatch.setattr(sp, "_claude_mcp", refuse)
    monkeypatch.setattr(sp, "_codex_mcp", refuse)
    report = sp.sync(["kit-pack"])
    assert report["packs"][0]["integrate"]["mcp"] == []


def test_selfcheck_runs_in_a_writable_copy_and_leaves_the_pin_read_only(isolated):
    repo, commit = make_upstream(isolated, "1.0.0")
    write_manifest(isolated, repo, commit, "1.0.0")
    sp.sync(["kit-pack"])
    kit, deps = sp._selfcheck_workdir("kit-pack")
    assert kit != sp.current_kit("kit-pack") and os.access(kit, os.W_OK)
    assert "npm not found" in deps
    assert not os.access(sp.current_kit("kit-pack"), os.W_OK)
    assert sp.status("kit-pack")["state"] != sp.TAMPERED
