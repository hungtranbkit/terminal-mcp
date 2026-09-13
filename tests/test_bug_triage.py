"""Difficulty triage, developer assist, human hints and the file/search budget."""

from __future__ import annotations

import pytest

from terminal_mcp import bug_spec as bs


def _spec(**kwargs):
    base = dict(title="t", symptom="s")
    base.update(kwargs)
    return bs.plan_from_report(**base)


def test_a_localized_visual_change_is_easy():
    verdict = bs.triage("Badge bị lệch", "padding sai trên mobile", module="work_ui",
                        files=["terminal_mcp/dashboard.py"], has_runbook=True)
    assert verdict["difficulty"] == bs.EASY
    assert verdict["assist_recommended"] is False


def test_an_easy_surface_over_a_hard_signal_is_still_hard():
    # "Misaligned, but only after re-login" is an auth bug wearing a CSS
    # costume. Triaging it EASY sends a worker into the wrong file for a day.
    verdict = bs.triage("Badge bị lệch", "chỉ lệch sau khi re-login, session bị mất",
                        module="work_ui", files=["terminal_mcp/dashboard.py"])
    assert verdict["difficulty"] == bs.HARD
    assert "auth/session" in verdict["hard_signals"]


@pytest.mark.parametrize("symptom,signal", [
    ("thỉnh thoảng race condition khi hai worker chạy", "concurrency"),
    ("dữ liệu bị duplicate giữa hai node", "data consistency"),
    ("nó used to work before bản v2", "unknown regression"),
    ("lỗi xảy ra across nodes khi federat sang node khác", "multi-service"),
    ("systemd tunnel chết sau khi đổi certificate", "infra/security"),
])
def test_hard_signals_are_detected(symptom, signal):
    verdict = bs.triage("Sự cố", symptom, module="core", files=["a.py"])
    assert verdict["difficulty"] == bs.HARD
    assert signal in verdict["hard_signals"]


def test_an_unknown_area_is_hard_with_low_confidence():
    verdict = bs.triage("Có gì đó sai", "app chậm")
    assert verdict["difficulty"] == bs.HARD
    assert verdict["confidence"] == "LOW"      # honest about not knowing
    assert verdict["assist_recommended"] is True


def test_a_known_module_without_a_pinned_cause_is_medium():
    verdict = bs.triage("Export sai số liệu", "tổng không khớp", module="reports")
    assert verdict["difficulty"] == bs.MEDIUM_DIFFICULTY
    assert verdict["assist_recommended"] is False


def test_triage_travels_on_every_spec():
    spec = _spec(title="Badge lệch", symptom="padding sai mobile", module="work_ui",
                 files=["terminal_mcp/dashboard.py"])
    assert spec.difficulty == bs.EASY
    assert spec.difficulty_reasons
    assert spec.human_assist_status == bs.ASSIST_NOT_NEEDED


def test_a_hard_spec_requests_assistance():
    spec = _spec(title="Session mất", symptom="mất session sau re-login, used to work",
                 module="auth")
    assert spec.difficulty == bs.HARD
    assert spec.human_assist_status == bs.ASSIST_REQUESTED


def test_assist_questions_are_specific_and_capped():
    spec = _spec(title="Session mất", symptom="mất session sau re-login, used to work",
                 module="auth")
    verdict = bs.triage(spec.title, spec.user_symptom, module="auth")
    request = bs.developer_assist_request(spec, verdict, findings=["grant rows unchanged"],
                                          hypotheses=["identity pin goes stale"])
    assert 1 <= len(request["questions"]) <= bs.MAX_ASSIST_QUESTIONS
    # Every question names a concrete discriminator; none is a request for
    # unspecified "more detail", which is what wastes a developer's time.
    for question in request["questions"]:
        assert question.endswith("?")
        assert "more detail" not in question.lower()
    # The planner's own analysis travels WITH the questions so the developer
    # can answer in one line instead of reconstructing context.
    assert request["current_findings"] == ["grant rows unchanged"]
    assert request["current_hypotheses"] == ["identity pin goes stale"]


def test_assist_never_blocks_when_no_answer_arrives():
    spec = _spec(title="Session mất", symptom="mất session sau re-login, used to work",
                 module="auth")
    verdict = bs.triage(spec.title, spec.user_symptom, module="auth")
    assert "never block" in bs.developer_assist_request(spec, verdict)["if_unavailable"]


def test_easy_bugs_do_not_interrupt_the_developer():
    spec = _spec(title="Badge lệch", symptom="padding sai mobile", module="work_ui",
                 files=["terminal_mcp/dashboard.py"])
    verdict = bs.triage(spec.title, spec.user_symptom, module="work_ui",
                        files=["terminal_mcp/dashboard.py"])
    assert bs.developer_assist_request(spec, verdict) is None


def test_human_hints_reach_the_worker_marked_as_guidance():
    spec = _spec(title="Session mất", symptom="mất session sau re-login", module="auth")
    spec.human_hints = ("cookie domain đổi sau khi bật tunnel",)
    spec.human_assist_status = bs.ASSIST_RECEIVED
    handoff = spec.handoff()
    assert "cookie domain" in handoff
    # A hint is guidance, not truth: the worker is told to check it.
    assert "verify against code" in handoff


def test_handoff_shows_difficulty_beside_level():
    spec = _spec(title="Badge lệch", symptom="padding sai mobile", module="work_ui",
                 files=["terminal_mcp/dashboard.py"])
    assert f"difficulty={spec.difficulty}" in spec.handoff()


def test_triage_survives_a_round_trip_through_the_store(tmp_path):
    store = bs.BugSpecStore(tmp_path / "specs.db")
    spec = _spec(title="Session mất", symptom="mất session sau re-login, used to work",
                 module="auth")
    spec.human_hints = ("chỉ xảy ra qua tunnel",)
    store.save(spec)
    loaded = store.get(spec.bug_id)
    assert loaded.difficulty == spec.difficulty
    assert loaded.difficulty_confidence == spec.difficulty_confidence
    assert loaded.human_hints == ("chỉ xảy ra qua tunnel",)
    assert loaded.human_assist_status == spec.human_assist_status
    store.close()


def test_specs_written_before_triage_existed_still_load(tmp_path):
    store = bs.BugSpecStore(tmp_path / "specs.db")
    spec = _spec(title="Cũ", symptom="cũ", module="legacy")
    store.save(spec)
    # Simulate a row persisted by an older build: no triage keys at all.
    payload = store._connection.execute(
        "SELECT payload FROM bug_specs WHERE bug_id=?", (spec.bug_id,)).fetchone()["payload"]
    import json
    raw = json.loads(payload)
    for key in ("difficulty", "difficulty_confidence", "difficulty_reasons",
                "human_hints", "human_assist_status"):
        raw.pop(key, None)
    with store._connection:
        store._connection.execute("UPDATE bug_specs SET payload=? WHERE bug_id=?",
                                  (json.dumps(raw), spec.bug_id))
    loaded = store.get(spec.bug_id)
    assert loaded.difficulty == "MEDIUM"
    assert loaded.human_hints == ()
    assert loaded.human_assist_status == "NOT_NEEDED"
    store.close()


def test_budget_allows_work_within_the_allowance():
    spec = _spec(title="Badge lệch", symptom="padding sai mobile", module="work_ui",
                 files=["terminal_mcp/dashboard.py"], suspected_cause="thiếu gap",
                 cause_confidence="HIGH", fix_strategy=["thêm gap"], acceptance=["ok"])
    assert spec.level() == bs.L1
    assert bs.budget_check(spec, files_read=3, search_rounds=1)["action"] == "continue"


def test_an_l1_that_blows_its_budget_goes_back_to_the_planner():
    spec = _spec(title="Badge lệch", symptom="padding sai mobile", module="work_ui",
                 files=["terminal_mcp/dashboard.py"], suspected_cause="thiếu gap",
                 cause_confidence="HIGH", fix_strategy=["thêm gap"], acceptance=["ok"])
    verdict = bs.budget_check(spec, files_read=9, search_rounds=1)
    assert verdict["within_budget"] is False
    assert verdict["action"] == bs.NEEDS_REDEFINE
    assert "9 files read (budget 5)" in verdict["exceeded"]


def test_a_deeper_spec_escalates_with_a_reason_instead_of_redefining():
    spec = _spec(title="Export sai", symptom="tổng không khớp", module="reports")
    assert spec.level() in (bs.L2, bs.L3)
    verdict = bs.budget_check(spec, files_read=999, search_rounds=99)
    assert verdict["action"] == "escalate_with_reason"


def test_budget_is_a_guard_not_a_kill_switch():
    spec = _spec(title="Export sai", symptom="tổng không khớp", module="reports")
    verdict = bs.budget_check(spec, files_read=999, search_rounds=99)
    # Nothing here tells a worker to stop mid-task; it tells it to explain.
    assert "stop" not in verdict["note"].lower()
    assert verdict["limits"]["max_files"] > 0
