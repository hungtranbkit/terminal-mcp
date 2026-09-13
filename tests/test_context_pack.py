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
