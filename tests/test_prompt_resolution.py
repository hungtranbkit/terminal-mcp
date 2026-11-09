import pytest
from terminal_mcp.prompt_resolution import classify_safe_prompt, prompt_fingerprint, resolve_choice_key


def test_codex_update_only_skips_notice_with_escape():
    lines = ["Update available", "› 1. Update now (runs installer)",
             "2. Skip", "3. Skip until next version", "enter continue · esc skip"]
    prompt = classify_safe_prompt(lines, "codex")
    assert prompt is not None
    assert (prompt.prompt_id, prompt.choice_id, prompt.key) == (
        "codex.optional_update.v1", "skip_optional_update", "Escape")
    assert resolve_choice_key(lines, "codex", prompt.prompt_id, prompt.choice_id) == "Escape"


def test_codex_hook_review_can_only_continue_without_trusting():
    lines = ["Hooks need review", "1. Review hooks", "2. Trust all and continue",
             "› 3. Continue without trusting (hooks won't run)"]
    prompt = classify_safe_prompt(lines, "codex")
    assert prompt is not None
    assert (prompt.prompt_id, prompt.choice_id, prompt.key) == (
        "codex.hook_review.v1", "continue_without_trusting", "Enter")
    assert resolve_choice_key(lines, "codex", prompt.prompt_id, "trust_all") is None


def test_claude_notice_requires_non_saving_choice_and_exact_prompt():
    lines = ["Save auto-mode settings?", "1. Save settings", "› 2. Don't save"]
    prompt = classify_safe_prompt(lines, "claude")
    assert prompt is not None
    assert (prompt.prompt_id, prompt.choice_id, prompt.key) == (
        "claude.optional_notice.v1", "dismiss_without_saving", "Enter")
    assert resolve_choice_key(lines, "codex", prompt.prompt_id, prompt.choice_id) is None


def test_unknown_tool_approval_and_stale_menu_are_never_resolved():
    approval = ["Run shell command?", "1. Allow once", "2. Always allow", "3. Deny"]
    assert classify_safe_prompt(approval, "codex") is None
    menu = ["Background server has incompatible feature settings",
            "› 1. Run without daemon this time", "2. Restart with these settings", "3. Cancel"]
    prompt = classify_safe_prompt(menu, "codex")
    assert prompt is not None
    changed = menu[:-1] + ["3. Quit"]
    assert resolve_choice_key(changed, "codex", prompt.prompt_id, prompt.choice_id) is None


def test_supervisor_resolves_only_current_owner_menu_once(tmp_path):
    from terminal_mcp.direct_task import DirectTaskStore, DirectTaskSupervisor

    menu = ["Update available", "› 1. Update now (runs installer)",
            "2. Skip", "3. Skip until next version", "enter continue · esc skip"]
    generation = {"session_id": "s1", "pane_id": "%1", "created_epoch": 123}
    state = {"state": "WAITING_INPUT", "permission_mode": "owner",
             "session_generation": generation, "prompt_fingerprint": prompt_fingerprint(menu)}
    sent = []

    def press(target, key):
        sent.append((target, key))
        state["state"] = "IDLE"
        state["prompt_fingerprint"] = "transitioned"
        return {"sent": True}

    store = DirectTaskStore(tmp_path / "direct.db")
    now = 1.0
    task = store.insert({"task_id": "dt_0000000000000001", "target": "node/s1",
                         "mode": "agent", "agent": "codex", "state": "WAITING_INPUT",
                         "steps": ["do task"], "next_step": 1,
                         "max_continuations": 2, "created_at": now, "updated_at": now,
                         "session_generation": generation})
    sup = DirectTaskSupervisor(store, send=lambda *_: {"sent": True}, status=lambda _: dict(state),
                               tail=lambda *_: {"output": "\n".join(menu)}, prompt_key=press)

    result = sup.resolve_prompt(task["task_id"], expected_prompt_fingerprint=state["prompt_fingerprint"],
                                prompt_id="codex.optional_update.v1", choice_id="skip_optional_update",
                                session_generation=generation, idempotency_key="prompt-k1")
    assert result["status"] == "RESOLVED"
    assert result["transition_verified"] is True
    assert sent == [("node/s1", "Escape")]
    retry = sup.resolve_prompt(task["task_id"], expected_prompt_fingerprint=prompt_fingerprint(menu),
                               prompt_id="codex.optional_update.v1", choice_id="skip_optional_update",
                               session_generation=generation, idempotency_key="prompt-k1")
    assert retry["status"] == "RESOLVED" and len(sent) == 1


@pytest.mark.parametrize("change,expected_error", [
    ("fingerprint", "STALE_OR_UNSUPPORTED_PROMPT"),
    ("generation", "SESSION_GENERATION_MISMATCH"),
    ("permission", "OWNER_POLICY_REQUIRED"),
    ("approval", "STALE_OR_UNSUPPORTED_PROMPT"),
])
def test_supervisor_rejects_stale_unowned_or_granting_prompt(tmp_path, change, expected_error):
    from terminal_mcp.direct_task import DirectTaskStore, DirectTaskSupervisor

    menu = ["Update available", "› 1. Update now (runs installer)",
            "2. Skip", "3. Skip until next version", "enter continue · esc skip"]
    generation = {"session_id": "s1", "pane_id": "%1", "created_epoch": 123}
    supplied_generation = dict(generation)
    observed_fp = prompt_fingerprint(menu)
    lines = menu
    state = {"state": "WAITING_INPUT", "permission_mode": "owner",
             "session_generation": generation, "prompt_fingerprint": observed_fp}
    if change == "fingerprint":
        observed_fp = "different"
    elif change == "generation":
        supplied_generation["created_epoch"] = 124
    elif change == "permission":
        state["permission_mode"] = "native"
    elif change == "approval":
        lines = ["Run shell command?", "1. Allow once", "2. Always allow", "3. Deny"]
    store = DirectTaskStore(tmp_path / f"{change}.db")
    task = store.insert({"task_id": "dt_0000000000000002", "target": "node/s1",
                         "mode": "agent", "agent": "codex", "state": "WAITING_INPUT",
                         "steps": ["do task"], "next_step": 1, "max_continuations": 2,
                         "created_at": 1.0, "updated_at": 1.0, "session_generation": generation})
    sent = []
    sup = DirectTaskSupervisor(store, send=lambda *_: {"sent": True}, status=lambda _: dict(state),
                               tail=lambda *_: {"output": "\n".join(lines)},
                               prompt_key=lambda target, key: sent.append((target, key)) or {"sent": True})
    result = sup.resolve_prompt(task["task_id"], expected_prompt_fingerprint=observed_fp,
                                prompt_id="codex.optional_update.v1", choice_id="skip_optional_update",
                                session_generation=supplied_generation, idempotency_key=f"k-{change}")
    assert result["error"] == expected_error
    assert sent == []


def test_prompt_idempotency_receipt_is_bound_to_session_generation(tmp_path):
    from terminal_mcp.direct_task import DirectTaskStore, DirectTaskSupervisor

    menu = ["Update available", "› 1. Update now (runs installer)",
            "2. Skip", "3. Skip until next version", "enter continue · esc skip"]
    generation = {"session_id": "s1", "pane_id": "%1", "created_epoch": 123}
    state = {"state": "WAITING_INPUT", "permission_mode": "owner",
             "session_generation": generation, "prompt_fingerprint": prompt_fingerprint(menu)}
    task = DirectTaskStore(tmp_path / "direct.db").insert({
        "task_id": "dt_0000000000000003", "target": "node/s1", "mode": "agent",
        "agent": "codex", "state": "WAITING_INPUT", "steps": ["do task"],
        "next_step": 1, "max_continuations": 2, "created_at": 1.0,
        "updated_at": 1.0, "session_generation": generation})
    store = DirectTaskStore(tmp_path / "direct.db")
    sent = []

    def press(target, key):
        sent.append((target, key))
        state["state"] = "IDLE"
        state["prompt_fingerprint"] = "transitioned"
        return {"sent": True}

    sup = DirectTaskSupervisor(store, send=lambda *_: {"sent": True}, status=lambda _: dict(state),
                               tail=lambda *_: {"output": "\n".join(menu)}, prompt_key=press)
    kwargs = {"expected_prompt_fingerprint": state["prompt_fingerprint"],
              "prompt_id": "codex.optional_update.v1", "choice_id": "skip_optional_update",
              "session_generation": generation, "idempotency_key": "same-key"}
    assert sup.resolve_prompt(task["task_id"], **kwargs)["status"] == "RESOLVED"
    different = {**generation, "created_epoch": 124}
    retry = sup.resolve_prompt(task["task_id"], **{**kwargs, "session_generation": different})
    assert retry["error"] == "SESSION_GENERATION_MISMATCH"
    assert sent == [("node/s1", "Escape")]


def test_supervisor_auto_resolves_current_owner_update_notice_once(tmp_path):
    from terminal_mcp.direct_task import DirectTaskStore, DirectTaskSupervisor

    menu = ["Update available", "› 1. Update now (runs installer)",
            "2. Skip", "3. Skip until next version", "enter continue · esc skip"]
    generation = {"session_id": "s1", "pane_id": "%1", "created_epoch": 321}
    state = {"state": "WAITING_INPUT", "permission_mode": "owner",
             "session_generation": generation, "prompt_fingerprint": prompt_fingerprint(menu)}
    task = DirectTaskStore(tmp_path / "direct.db").insert({
        "task_id": "dt_0000000000000004", "target": "node/s1", "mode": "agent",
        "agent": "codex", "state": "WAITING_INPUT", "steps": ["do task"],
        "next_step": 1, "max_continuations": 2, "created_at": 1.0,
        "updated_at": 1.0, "session_generation": generation})
    sent = []

    def press(target, key):
        sent.append((target, key))
        state.update(state="IDLE", prompt_fingerprint="transitioned")
        return {"sent": True}

    sup = DirectTaskSupervisor(DirectTaskStore(tmp_path / "direct.db"),
                               send=lambda *_: {"sent": True}, status=lambda _: dict(state),
                               tail=lambda *_: {"output": "\n".join(menu)}, prompt_key=press)
    result = sup._auto_resolve_prompt(task, dict(state), "\n".join(menu))
    assert result["status"] == "RESOLVED" and result["transition_verified"]
    assert sent == [("node/s1", "Escape")]
