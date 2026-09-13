"""FAST_FIX / NORMAL / SAFE classification and the investigation order.

The rule this file protects: exclusions are checked FIRST, and a caller in a
hurry cannot talk a dangerous change onto the fast path.
"""

from __future__ import annotations

import pytest

from terminal_mcp import task_classifier as tc


def test_a_css_tweak_is_a_fast_fix():
    result = tc.classify("Badge WORK bị chồng lên tên session trên mobile",
                         changed_paths=["terminal_mcp/dashboard.py"])
    assert result.mode == tc.FAST_FIX
    assert result.budget == tc.SMALL
    assert result.deploy_level == tc.PREVIEW
    assert result.requires_approval is False


@pytest.mark.parametrize("description,paths", [
    ("thêm migration cho bảng work_runs", ["terminal_mcp/work_store.py"]),
    ("sửa permission check cho admin", ["terminal_mcp/access_policy.py"]),
    ("đổi cách lưu token đăng nhập", ["terminal_mcp/grants.py"]),
    ("xoá dữ liệu cũ trong bảng events", ["terminal_mcp/work_store.py"]),
])
def test_dangerous_areas_are_never_fast(description, paths):
    result = tc.classify(description, changed_paths=paths)
    assert result.mode == tc.SAFE
    assert result.requires_approval is True
    assert result.deploy_level == tc.PRODUCTION
    assert result.exclusions_hit


def test_a_requested_fast_fix_cannot_override_an_exclusion():
    result = tc.classify("chỉ sửa nhỏ thôi, đổi permission check",
                         changed_paths=["terminal_mcp/access_policy.py"],
                         requested_mode=tc.FAST_FIX)
    # Not negotiable by the party in a hurry.
    assert result.mode == tc.SAFE
    assert result.escalated_from == tc.FAST_FIX
    assert any("excluded from FAST_FIX" in reason for reason in result.reasons)


def test_a_caller_may_always_escalate():
    result = tc.classify("đổi màu badge", changed_paths=["terminal_mcp/dashboard.py"],
                         requested_mode=tc.SAFE)
    assert result.mode == tc.SAFE


def test_a_cosmetic_change_in_a_backend_file_is_not_fast_by_path_alone():
    result = tc.classify("sửa logic tính progress", changed_paths=["terminal_mcp/work_loop.py"])
    assert result.mode in (tc.NORMAL, tc.SAFE)
    assert result.mode != tc.FAST_FIX


def test_a_mixed_change_is_not_fast():
    result = tc.classify("sửa badge và thêm điều kiện dispatch trong queue_engine",
                         changed_paths=["terminal_mcp/dashboard.py",
                                        "terminal_mcp/queue_engine.py"])
    assert result.mode == tc.NORMAL


def test_classification_explains_itself():
    result = tc.classify("badge lệch trên mobile", changed_paths=["terminal_mcp/dashboard.py"])
    assert result.reasons
    payload = result.as_dict()
    assert set(payload) >= {"mode", "budget", "reasons", "deploy_level"}


def test_investigation_starts_with_knowledge_and_widens_only_on_evidence():
    plan = tc.investigation_plan(
        tc.classify("badge lệch", changed_paths=["terminal_mcp/dashboard.py"]))
    steps = " -> ".join(step["step"] for step in plan).lower()
    order = [steps.index(key) for key in ("knowledge", "git", "search", "read")
             if key in steps]
    assert order == sorted(order), steps


def test_a_safe_change_gets_a_wider_budget_than_a_fast_fix():
    fast = tc.classify("badge lệch", changed_paths=["terminal_mcp/dashboard.py"])
    safe = tc.classify("thêm migration", changed_paths=["terminal_mcp/work_store.py"])
    assert (fast.budget, safe.budget) == (tc.SMALL, tc.LARGE)


def test_the_knowledge_policy_tells_agents_the_code_is_the_truth():
    text = tc.AGENT_KNOWLEDGE_POLICY.lower()
    assert "source of truth" in text
    assert "knowledge" in text
