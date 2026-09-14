"""The persistent project knowledge map.

The load-bearing property under test: the map is a MAP. Current code and git
state are the source of truth, and every confidence value here is derived
from that -- never asserted, never a fabricated percentage.
"""

from __future__ import annotations

import subprocess
import threading

import pytest

from terminal_mcp import project_knowledge as pk


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "alpha.py").write_text("alpha = 1\n")
    (root / "beta.py").write_text("beta = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


# -- secrets -----------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n",
    "API_TOKEN=ghp_" + "a" * 36,
    "Authorization: Bearer abcdefghijklmnopqrstuvwx",
    "DB_PASSWORD: hunter2secret",
    "aws key AKIA" + "A" * 16,
    "Cookie: session=abcdefghijkl",
])
def test_knowledge_refuses_credentials(text):
    with pytest.raises(pk.SecretInKnowledge):
        pk.scrub_knowledge(text)


@pytest.mark.parametrize("text", [
    "The token is read from the TERMINAL_MCP_NODE_TOKEN environment variable.",
    "Set DB_PASSWORD=your-password-here in the local env file.",
    "Authentication uses a bearer token issued by Cloudflare Access.",
    "API_KEY=<redacted>",
])
def test_knowledge_allows_env_var_names_and_prose(text):
    # Over-refusing is its own failure: a map that cannot discuss auth is
    # useless for the bugs most likely to need it.
    assert pk.scrub_knowledge(text) == text


def test_a_secret_is_refused_rather_than_silently_stripped(repo):
    knowledge = pk.ProjectKnowledge(repo)
    with pytest.raises(pk.SecretInKnowledge):
        knowledge.write_document("PROJECT.md", "token: ghp_" + "b" * 36)
    # Nothing partial is left behind.
    assert knowledge.document("PROJECT.md") in (None, "")


def test_secret_error_names_the_line(repo):
    with pytest.raises(pk.SecretInKnowledge) as excinfo:
        pk.scrub_knowledge("line one\nline two\nAPI_TOKEN=ghp_" + "c" * 36, where="PROJECT.md")
    assert "line 3" in str(excinfo.value)


# -- canonical root ----------------------------------------------------------

def test_worktrees_resolve_to_one_canonical_map(repo, tmp_path):
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(worktree))
    # Two agents in two worktrees must not silently fork the map.
    assert pk.canonical_root(str(worktree)) == pk.canonical_root(str(repo))


def test_outside_a_repository_there_is_no_canonical_root(tmp_path):
    assert pk.canonical_root(str(tmp_path)) is None


def test_a_worktree_root_is_the_checkout_not_the_shared_one(repo, tmp_path):
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(worktree))
    # The opposite question to the one above, and it must get the opposite
    # answer: asking whether a file still exists, or has moved, has to be
    # answered about the tree that will actually run -- the main checkout
    # would report confidently about code nobody is about to execute.
    assert pk.worktree_root(str(worktree)) == worktree.resolve()
    assert pk.worktree_root(str(worktree)) != pk.canonical_root(str(worktree))


def test_a_subdirectory_still_resolves_to_the_checkout_top(repo):
    nested = repo / "pkg" / "deep"
    nested.mkdir(parents=True)
    assert pk.worktree_root(str(nested)) == repo.resolve()


def test_outside_a_repository_there_is_no_worktree_root(tmp_path):
    assert pk.worktree_root(str(tmp_path)) is None


# -- confidence is derived ---------------------------------------------------

def test_a_module_verified_at_head_is_high_confidence(repo):
    knowledge = pk.ProjectKnowledge(repo)
    module = knowledge.record_module("alpha", paths=["alpha.py"], summary="alpha module")
    assert module.confidence == pk.HIGH


def test_a_module_whose_files_changed_drops_to_low(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.record_module("alpha", paths=["alpha.py"], summary="alpha module")
    (repo / "alpha.py").write_text("alpha = 2\n")
    _git(repo, "commit", "-aqm", "touch alpha")
    state = next(m for m in knowledge.module_states() if m.name == "alpha")
    # Its paths moved since it was verified: a lead to check, not a fact.
    assert knowledge._confidence(state, knowledge.head()) == pk.LOW
    assert [m.name for m in knowledge.stale_modules()] == ["alpha"]


def test_an_untouched_module_is_not_marked_stale_by_someone_elses_commit(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.record_module("alpha", paths=["alpha.py"])
    knowledge.record_module("beta", paths=["beta.py"])
    (repo / "alpha.py").write_text("alpha = 3\n")
    _git(repo, "commit", "-aqm", "touch alpha only")
    stale = {m.name for m in knowledge.stale_modules()}
    assert stale == {"alpha"}      # beta's confidence survives


def test_confidence_is_never_a_fabricated_number(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.record_module("alpha", paths=["alpha.py"])
    status = knowledge.status()
    payload = repr(status)
    assert "%" not in payload
    for module in status["modules"]:
        assert module["confidence"] in (pk.HIGH, pk.MEDIUM, pk.LOW)


# -- documents and search ----------------------------------------------------

def test_documents_round_trip(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.write_document("ARCHITECTURE.md", "# Architecture\n\nController plus agents.\n")
    assert "Controller plus agents" in knowledge.document("ARCHITECTURE.md")


def test_search_finds_the_document_and_the_line(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.write_document("DEBUG_MAP.md", "# Debug\n\nQueue stalls come from lane locks.\n")
    hits = knowledge.search("lane locks")
    assert len(hits["matches"]) >= 1
    first = hits["matches"][0]
    assert first["document"] == "DEBUG_MAP.md"
    assert "lane locks" in first["text"]
    # A hit without its section is a fact with no address.
    assert first["heading"] == "Debug"
    assert first["line"] == 3


def test_search_for_something_absent_returns_nothing_not_noise(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.write_document("DEBUG_MAP.md", "# Debug\n")
    assert knowledge.search("zzz-not-present")["matches"] == []
    assert knowledge.search("  ")["error"] == "QUERY_REQUIRED"


def test_validate_reports_what_is_missing(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.record_module("ghost", paths=["does_not_exist.py"])
    report = knowledge.validate()
    joined = repr(report)
    assert "does_not_exist.py" in joined


# -- concurrency and durability ----------------------------------------------

def test_two_workers_do_not_lose_each_others_modules(repo):
    knowledge = pk.ProjectKnowledge(repo)
    errors: list[BaseException] = []

    def record(name: str, path: str) -> None:
        try:
            pk.ProjectKnowledge(repo).record_module(name, paths=[path])
        except BaseException as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=record, args=(f"m{i}", f"f{i}.py")) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    names = {m.name for m in knowledge.module_states()}
    assert names == {f"m{i}" for i in range(8)}


def test_state_survives_a_new_instance(repo):
    pk.ProjectKnowledge(repo).record_module("alpha", paths=["alpha.py"], summary="s")
    reopened = pk.ProjectKnowledge(repo)
    assert [m.name for m in reopened.module_states()] == ["alpha"]


def test_a_stale_lock_is_broken_rather_than_blocking_forever(repo):
    knowledge = pk.ProjectKnowledge(repo)
    lock_path = knowledge.dir / ".knowledge.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("owner=ghost pid=999999 at=1970-01-01T00:00:00+00:00")
    import os
    os.utime(lock_path, (0, 0))     # long dead
    knowledge.record_module("alpha", paths=["alpha.py"])
    assert [m.name for m in knowledge.module_states()] == ["alpha"]


# -- the working tree beats the commit graph ---------------------------------

def test_an_uncommitted_edit_stops_a_module_claiming_high(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.record_module("alpha", paths=["alpha.py"])
    assert next(m for m in knowledge.module_states()
                if m.name == "alpha").confidence == pk.HIGH
    (repo / "alpha.py").write_text("alpha = 99\n")     # edited, not committed
    module = next(m for m in knowledge.module_states() if m.name == "alpha")
    # The code that will run is not the code the map describes.
    assert module.confidence == pk.MEDIUM
    assert "uncommitted" in module.confidence_reason


def test_an_untracked_file_in_a_module_directory_counts(repo):
    knowledge = pk.ProjectKnowledge(repo)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "a.py").write_text("a = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "pkg")
    knowledge.record_module("pkg", paths=["pkg"])
    (repo / "pkg" / "new.py").write_text("new = 1\n")   # untracked
    module = next(m for m in knowledge.module_states() if m.name == "pkg")
    assert module.confidence == pk.MEDIUM
    assert "new.py" in module.confidence_reason


def test_an_uncommitted_edit_elsewhere_does_not_age_a_module(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.record_module("alpha", paths=["alpha.py"])
    (repo / "beta.py").write_text("beta = 99\n")
    module = next(m for m in knowledge.module_states() if m.name == "alpha")
    assert module.confidence == pk.HIGH


def test_a_committed_change_still_outranks_a_clean_tree(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.record_module("alpha", paths=["alpha.py"])
    (repo / "alpha.py").write_text("alpha = 5\n")
    _git(repo, "commit", "-aqm", "change alpha")
    module = next(m for m in knowledge.module_states() if m.name == "alpha")
    assert module.confidence == pk.LOW
    assert "changed since" in module.confidence_reason


def test_every_confidence_carries_its_reason(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.record_module("alpha", paths=["alpha.py"])
    for module in knowledge.module_states():
        # A bare label says what to feel; the reason says what to do next.
        assert module.confidence_reason
        assert module.as_dict()["confidence_reason"]


def test_an_unreadable_working_tree_is_medium_not_high(repo, monkeypatch):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.record_module("alpha", paths=["alpha.py"])
    monkeypatch.setattr(knowledge, "uncommitted_paths", lambda: None)
    module = next(m for m in knowledge.module_states() if m.name == "alpha")
    assert module.confidence == pk.MEDIUM
    assert "could not be read" in module.confidence_reason


# -- incremental re-verification ---------------------------------------------
#
# The map ages whether or not anybody re-reads it, and MEDIUM is what sends a
# worker off to re-derive a module that did not change. `rebuild` is the cheap
# half of that problem: what git can prove, it advances; what only a reader
# can judge, it refuses to touch and names instead.

def test_rebuild_advances_a_module_whose_paths_never_moved(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.record_module("alpha", paths=["alpha.py"], summary="alpha module")
    (repo / "beta.py").write_text("beta = 2\n")
    _git(repo, "commit", "-aqm", "touch beta only")
    # Verified against an ancestor now, which is what makes a worker re-read it.
    assert knowledge.module_state("alpha").confidence == pk.MEDIUM

    report = knowledge.rebuild()

    assert report["advanced"] == ["alpha"]
    assert knowledge.module_state("alpha").confidence == pk.HIGH
    assert knowledge.module_state("alpha").last_verified_commit == knowledge.head()


def test_an_advanced_module_says_it_was_advanced_rather_than_re_read(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.record_module("alpha", paths=["alpha.py"])
    (repo / "beta.py").write_text("beta = 3\n")
    _git(repo, "commit", "-aqm", "beta")
    knowledge.rebuild()
    reason = knowledge.module_state("alpha").confidence_reason
    # "A reader checked this" and "git proved nothing moved" are different
    # claims, and the weaker one must not wear the stronger one's name.
    assert "re-reading" in reason
    assert knowledge.module_state("alpha").last_refreshed_at


def test_rebuild_never_advances_a_module_whose_paths_changed(repo):
    knowledge = pk.ProjectKnowledge(repo)
    before = knowledge.record_module("alpha", paths=["alpha.py"]).last_verified_commit
    (repo / "alpha.py").write_text("alpha = 9\n")
    _git(repo, "commit", "-aqm", "touch alpha")

    report = knowledge.rebuild()

    assert report["advanced"] == []
    review = next(r for r in report["needs_review"] if r["module"] == "alpha")
    assert "alpha.py" in review["paths"]
    assert knowledge.module_state("alpha").last_verified_commit == before
    assert knowledge.module_state("alpha").confidence == pk.LOW


def test_rebuild_re_verifies_but_never_rewrites_what_only_a_reader_can_judge(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.record_module("alpha", paths=["alpha.py"], summary="serialises rows to CSV")
    (repo / "beta.py").write_text("beta = 4\n")
    _git(repo, "commit", "-aqm", "beta")
    knowledge.rebuild()
    # A machine that rewrote the prose would be inventing the one thing this
    # map is not allowed to invent.
    assert knowledge.module_state("alpha").summary == "serialises rows to CSV"
    assert knowledge.module_state("alpha").last_verified_at == \
        knowledge.load_state()["modules"]["alpha"]["last_verified_at"]


def test_an_uncommitted_edit_blocks_an_advance(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.record_module("alpha", paths=["alpha.py"])
    (repo / "beta.py").write_text("beta = 5\n")
    _git(repo, "commit", "-aqm", "beta")
    (repo / "alpha.py").write_text("alpha = 99\n")      # on disk, not committed

    report = knowledge.rebuild()

    assert report["advanced"] == []
    assert "uncommitted" in next(r for r in report["needs_review"]
                                 if r["module"] == "alpha")["why"]


def test_rebuild_names_a_path_that_no_longer_exists_instead_of_stamping_head(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.record_module("alpha", paths=["alpha.py", "gone.py"])
    report = knowledge.rebuild()
    assert {m["path"] for m in report["missing_paths"]} == {"gone.py"}
    assert report["advanced"] == []


def test_the_map_is_only_indexed_at_head_when_every_module_is(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.record_module("alpha", paths=["alpha.py"])
    knowledge.record_module("beta", paths=["beta.py"])
    (repo / "alpha.py").write_text("alpha = 7\n")
    _git(repo, "commit", "-aqm", "touch alpha")

    stale = knowledge.rebuild()
    assert stale["indexed_advanced"] is False       # alpha still needs a reader

    knowledge.record_module("alpha", paths=["alpha.py"])   # a reader re-verified it
    clean = knowledge.rebuild()
    assert clean["indexed_advanced"] is True
    assert clean["last_indexed_commit"] == knowledge.head()


def test_rebuilding_named_modules_leaves_the_rest_alone(repo):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.record_module("alpha", paths=["alpha.py"])
    knowledge.record_module("beta", paths=["beta.py"])
    (repo / "gamma.md").write_text("notes\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unrelated")

    report = knowledge.rebuild(modules=["alpha", "nosuch"])

    assert report["advanced"] == ["alpha"]
    assert report["unknown"] == ["nosuch"]
    assert knowledge.module_state("beta").confidence == pk.MEDIUM   # untouched


def test_rebuild_outside_a_repository_reports_why_rather_than_raising(tmp_path):
    knowledge = pk.ProjectKnowledge(tmp_path / "nowhere")
    report = knowledge.rebuild()
    # This runs on the planning path: a refresh that failed must not take a
    # plan down with it.
    assert report["ok"] is False and report["head"] is None
    assert "HEAD" in report["why"]


def test_rebuild_of_an_empty_map_says_there_is_nothing_to_verify(repo):
    report = pk.ProjectKnowledge(repo).rebuild()
    assert report["checked"] == 0 and "no modules" in report["why"]


# -- loading only what was asked for -----------------------------------------

def test_loading_named_modules_never_lists_the_whole_map(repo, monkeypatch):
    knowledge = pk.ProjectKnowledge(repo)
    knowledge.record_module("alpha", paths=["alpha.py"], summary="alpha")
    knowledge.record_module("beta", paths=["beta.py"], summary="beta")

    def _refuse():
        raise AssertionError("the whole map was listed to answer about one module")

    monkeypatch.setattr(knowledge, "module_states", _refuse)
    found, unknown = knowledge.load_modules(["alpha", "alpha", "nosuch"])

    assert [m.name for m in found] == ["alpha"]
    assert found[0].confidence == pk.HIGH
    # "We loaded nothing" and "that module is not in the map" send a worker in
    # opposite directions.
    assert unknown == ["nosuch"]


def test_asking_for_a_module_the_map_does_not_have_returns_nothing_not_an_error(repo):
    assert pk.ProjectKnowledge(repo).module_state("nosuch") is None
