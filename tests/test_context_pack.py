"""Module context packs and similar-bug retrieval."""

from __future__ import annotations

import pytest

from terminal_mcp import context_pack as cp
from terminal_mcp.bug_spec import BugSpecStore, plan_from_report


@pytest.fixture()
def store(tmp_path):
    store = BugSpecStore(tmp_path / "specs.db")
    yield store
    store.close()


def _badge_bug(**kwargs):
    base = dict(title="Badge WORK bị chồng lên tên session",
                symptom="badge overlap trên màn hình hẹp",
                module="work_ui", files=["terminal_mcp/dashboard.py"])
    base.update(kwargs)
    return plan_from_report(**base)


def test_fingerprint_buckets_the_same_defect_despite_different_wording():
    first = _badge_bug()
    second = _badge_bug(title="Badge đè lên tên session ở mobile",
                        symptom="nhãn chồng nhau khi thu nhỏ")
    assert cp.fingerprint(first) == cp.fingerprint(second)


def test_fingerprint_separates_different_modules():
    assert cp.fingerprint(_badge_bug()) != cp.fingerprint(
        plan_from_report(title="Queue kẹt", symptom="task PENDING mãi", module="queue_engine"))


def test_strong_match_is_offered_for_reuse_with_its_provenance(store):
    previous = _badge_bug(suspected_cause="thiếu flex gap", cause_confidence="HIGH",
                          fix_strategy=["thêm gap 6px"], acceptance=["không chồng"])
    previous.source_commit = "aaa111"
    store.save(previous)
    result = cp.retrieval_result(store, _badge_bug(title="Badge đè tên session trên mobile"),
                                 current_commit="bbb222")
    assert result["status"] == cp.REUSED
    assert result["reused_bug_id"] == previous.bug_id
    assert result["reused_from_commit"] == "aaa111"
    # Written against another commit: the worker is told to expect drift
    # rather than trusting the paths the old spec names.
    assert result["reused_is_stale"] is True
    assert "VERIFY" in result["guidance"]


def test_reuse_is_not_flagged_stale_on_the_same_commit(store):
    previous = _badge_bug()
    previous.source_commit = "aaa111"
    store.save(previous)
    result = cp.retrieval_result(store, _badge_bug(title="Badge đè tên session"),
                                 current_commit="aaa111")
    assert result["status"] == cp.REUSED
    assert result["reused_is_stale"] is False


def test_same_module_different_defect_is_related_not_reusable(store):
    store.save(_badge_bug())
    result = cp.retrieval_result(
        store, plan_from_report(title="Nút Refresh không phản hồi",
                                symptom="click không gọi API", module="work_ui",
                                files=["terminal_mcp/dashboard.py"]))
    # Offering this as a starting point would send the worker to the wrong cause.
    assert result["status"] == cp.RELATED_ONLY
    assert "do not assume the same cause" in result["guidance"]


def test_unrelated_bug_retrieves_nothing_rather_than_a_weak_guess(store):
    store.save(_badge_bug())
    result = cp.retrieval_result(store, plan_from_report(
        title="TLS certificate hết hạn", symptom="tunnel handshake lỗi", module="infra"))
    assert result["status"] == cp.NO_MATCH
    assert result["matches"] == []


def test_matches_explain_themselves(store):
    store.save(_badge_bug())
    match = cp.similar_bugs(store, _badge_bug(title="Badge đè tên session"))[0]
    joined = " ".join(match.reasons)
    assert "same module" in joined and "shared files" in joined
    assert 0.0 < match.score <= 1.0


def test_a_spec_never_matches_itself(store):
    spec = _badge_bug()
    store.save(spec)
    assert all(m.spec.bug_id != spec.bug_id for m in cp.similar_bugs(store, spec))


def test_pack_without_a_knowledge_map_says_what_it_does_not_cover(store):
    store.save(_badge_bug(suspected_cause="thiếu flex gap", cause_confidence="HIGH",
                          fix_strategy=["thêm gap"], acceptance=["ok"]))
    pack = cp.build_context_pack("work_ui", knowledge=None, store=store)
    rendered = pack.render()
    assert "PAST BUG" in rendered
    assert "NOT COVERED" in rendered
    assert any("no knowledge map" in gap for gap in pack.gaps)


def test_pack_reports_an_empty_module_honestly(store):
    pack = cp.build_context_pack("brand_new_module", knowledge=None, store=store)
    assert any("no previous bugs" in gap for gap in pack.gaps)
    assert pack.past_bugs == ()


def test_pack_is_bounded(store):
    for index in range(20):
        store.save(_badge_bug(title=f"Badge bug {index}"))
    pack = cp.build_context_pack("work_ui", knowledge=None, store=store)
    assert len(pack.past_bugs) <= cp.MAX_PACK_BUGS
    assert len(pack.render()) <= cp.MAX_PACK_CHARS + 32


def test_pack_from_a_knowledge_map_carries_confidence_and_staleness(tmp_path, store):
    import subprocess

    from terminal_mcp.project_knowledge import ProjectKnowledge

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "mod.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@e", "-c", "user.name=t",
                    "commit", "-qm", "init"], check=True)
    knowledge = ProjectKnowledge(tmp_path)
    knowledge.record_module("core", paths=["mod.py"], summary="the core module")
    pack = cp.build_context_pack("core", knowledge=knowledge, store=store)
    assert pack.summary == "the core module"
    assert pack.confidence in ("HIGH", "MEDIUM", "LOW")
    assert "mod.py" in pack.files
    assert "MODULE core" in pack.render()


# -- verifying a reused spec's paths against the code as it is now ------------
#
# Reuse is only safe because of this check. The match is made on wording, and
# wording matching says nothing about whether the files that fixed it last
# time still exist -- so a spec is checked BEFORE it is offered, not after a
# worker has trusted it.

@pytest.fixture()
def repo(tmp_path):
    import subprocess

    from terminal_mcp.project_knowledge import ProjectKnowledge

    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@e",
                        "-c", "user.name=t", *args], check=True, capture_output=True)

    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "steady.py").write_text("steady = 1\n")
    (root / "rewritten.py").write_text("old = 1\n")
    (root / "deleted.py").write_text("gone = 1\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    first = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                           check=True, capture_output=True, text=True).stdout.strip()
    (root / "rewritten.py").write_text("new = 2\n")
    (root / "deleted.py").unlink()
    git("add", "-A")
    git("commit", "-qm", "move things around")
    knowledge = ProjectKnowledge(root)
    return knowledge, first


def _spec_naming(paths, *, commit=None, **kwargs):
    spec = plan_from_report(title="Badge overlaps the session name",
                            symptom="the work badge overlaps the session name "
                                    "on a narrow screen",
                            module="work_ui", files=paths, **kwargs)
    spec.source_commit = commit
    return spec


def test_each_named_path_gets_its_own_verdict(repo):
    knowledge, first = repo
    check = cp.verify_reused_paths(
        _spec_naming(["steady.py", "rewritten.py", "deleted.py"], commit=first),
        knowledge=knowledge)
    verdicts = {entry["path"]: entry["status"] for entry in check["paths"]}
    assert verdicts == {"steady.py": cp.PATH_UNCHANGED,
                        "rewritten.py": cp.PATH_CHANGED,
                        "deleted.py": cp.PATH_MISSING}
    assert check["verified"] is True
    assert check["missing"] == ["deleted.py"] and check["changed"] == ["rewritten.py"]


def test_an_uncommitted_edit_counts_as_drift(repo):
    knowledge, first = repo
    # The working tree outranks the commit graph: an edited file is what will
    # actually run, committed or not.
    (knowledge.root / "steady.py").write_text("steady = 99\n")
    check = cp.verify_reused_paths(_spec_naming(["steady.py"], commit=first),
                                   knowledge=knowledge)
    assert check["changed"] == ["steady.py"]


def test_a_path_that_cannot_be_checked_is_never_reported_unchanged(repo):
    knowledge, _ = repo
    # No commit on the spec: the file is there, but there is nothing to
    # measure drift from, and saying UNCHANGED would be a claim nobody made.
    check = cp.verify_reused_paths(_spec_naming(["steady.py"]), knowledge=knowledge)
    assert check["paths"][0]["status"] == cp.PATH_UNVERIFIED
    assert check["verified"] is False
    assert any("records no commit" in gap for gap in check["gaps"])


def test_an_unknown_commit_is_reported_as_unknown_not_as_clean(repo):
    knowledge, _ = repo
    check = cp.verify_reused_paths(_spec_naming(["steady.py"], commit="0" * 40),
                                   knowledge=knowledge)
    assert check["unverified"] == ["steady.py"]
    assert any("not in" in gap for gap in check["gaps"])


def test_without_a_repository_nothing_is_claimed_to_be_verified():
    check = cp.verify_reused_paths(_spec_naming(["steady.py"], commit="abc123"))
    assert check["verified"] is False
    assert check["paths"][0]["status"] == cp.PATH_UNVERIFIED
    assert any("no repository" in gap for gap in check["gaps"])


def test_a_spec_naming_no_path_says_so_rather_than_passing_silently():
    check = cp.verify_reused_paths(_spec_naming([]))
    assert check["checked"] == 0
    assert any("names no path" in gap for gap in check["gaps"])


def test_entry_points_are_split_from_their_symbols_and_bare_names_ignored():
    spec = _spec_naming(["a/one.py"])
    spec.entry_points = ("a/two.py:render", "render_badge", "b/three.py")
    assert cp.named_paths(spec) == ["a/one.py", "a/two.py", "b/three.py"]


def test_a_path_escaping_the_repository_is_refused_not_followed(repo):
    knowledge, first = repo
    spec = _spec_naming(["../outside.py", "/etc/passwd"], commit=first)
    check = cp.verify_reused_paths(spec, knowledge=knowledge)
    assert {entry["status"] for entry in check["paths"]} == {cp.PATH_UNVERIFIED}
    assert all("repository-relative" in entry["why"] for entry in check["paths"])


# -- retrieval offers a reused spec only once its paths have been checked -----

def _reuse_target():
    return plan_from_report(title="Badge overlaps the session name on mobile",
                            symptom="Badge overlaps the session name on mobile",
                            module="work_ui")


def test_reuse_carries_the_path_check_and_names_what_moved(store, repo):
    knowledge, first = repo
    store.save(_spec_naming(["steady.py", "rewritten.py"], commit=first,
                            suspected_cause="missing flex gap", cause_confidence="HIGH",
                            fix_strategy=["add a 6px gap"], acceptance=["no overlap"]))
    result = cp.retrieval_result(store, _reuse_target(), knowledge=knowledge)
    assert result["status"] == cp.REUSED
    assert result["path_check"]["changed"] == ["rewritten.py"]
    # The worker is told which path moved, in the guidance it actually reads.
    assert "rewritten.py" in result["guidance"]
    assert "VERIFY" in result["guidance"]


def test_a_match_whose_every_path_is_gone_is_reading_not_a_plan(store, repo):
    knowledge, first = repo
    store.save(_spec_naming(["deleted.py"], commit=first,
                            suspected_cause="missing flex gap", cause_confidence="HIGH",
                            fix_strategy=["add a 6px gap"], acceptance=["no overlap"]))
    result = cp.retrieval_result(store, _reuse_target(), knowledge=knowledge)
    # The symptom still matched, so the root cause is worth reading -- but a
    # fix strategy for code that no longer exists points at nothing.
    assert result["status"] == cp.RELATED_ONLY
    assert result["downgraded_from"] == cp.REUSED
    assert "reused_bug_id" not in result
    assert result["matches"][0]["root_cause"] == "missing flex gap"


def test_retrieval_reads_the_current_commit_off_the_repository(store, repo):
    knowledge, first = repo
    store.save(_spec_naming(["steady.py"], commit=first,
                            suspected_cause="missing flex gap", cause_confidence="HIGH",
                            fix_strategy=["add a 6px gap"], acceptance=["no overlap"]))
    # Nobody passed current_commit: HEAD has moved past `first`, and the
    # staleness has to be noticed without being told.
    result = cp.retrieval_result(store, _reuse_target(), knowledge=knowledge)
    assert result["reused_is_stale"] is True


def test_reuse_without_a_repository_admits_the_paths_are_unchecked(store):
    store.save(_spec_naming(["steady.py"], commit="aaa111",
                            suspected_cause="missing flex gap", cause_confidence="HIGH",
                            fix_strategy=["add a 6px gap"], acceptance=["no overlap"]))
    result = cp.retrieval_result(store, _reuse_target())
    assert result["status"] == cp.REUSED
    assert result["path_check"]["verified"] is False
    assert "not every path could be checked" in result["guidance"]
