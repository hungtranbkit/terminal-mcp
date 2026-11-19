"""P1 bounded AI Project Coordinator (issue #106): the model only PROPOSES.

A plan is strict, versioned JSON; the validator rejects anything outside the
project, any cycle, oversize/unknown/sensitive/secret content and unapproved
high risk; apply is deterministic and idempotent and can only APPEND tasks. A
provider that is missing, failing or over budget never stops the project --
the deterministic runtime keeps dispatching existing ready work.
"""
from __future__ import annotations

import json

import pytest

from terminal_mcp import autopilot_planner as pl
from terminal_mcp.autopilot_policy import PlannerProviderConfig
from terminal_mcp.outcomes import OutcomeStore
from terminal_mcp.queue_store import QUEUED, QueueStore

PROJECT = "synthetic/brandagent-staging"
LANES = ("brand-work-1", "brand-work-2")


@pytest.fixture
def world(tmp_path):
    store = QueueStore(tmp_path / "queue.db")
    outcomes = OutcomeStore(store)
    outcome = outcomes.create(PROJECT, "Brief generator", acceptance_criteria=["renders"])
    [a] = store.append_tasks("brand-work-1", [{
        "title": "A", "prompt": "SECRET-PROMPT-BODY do A", "project_id": PROJECT}])
    [foreign] = store.append_tasks("other-1", [{
        "title": "foreign", "prompt": "x", "project_id": "synthetic/other"}])
    return {"store": store, "outcomes": outcomes, "outcome": outcome, "a": a, "foreign": foreign}


def plan(*tasks, project=PROJECT, **extra):
    return {"schema": pl.PLAN_SCHEMA, "project_id": project, "tasks": list(tasks), **extra}


def task(key, *, depends_on=(), risk="low", prompt=None, **extra):
    return {"key": key, "title": f"Task {key}", "prompt": prompt or f"Implement {key} with tests.",
            "depends_on": list(depends_on), "priority": 50, "risk": risk,
            "capabilities": [], "rationale": f"{key} is next because its deps are done", **extra}


# -- context --------------------------------------------------------------

def test_context_is_project_scoped_and_carries_no_prompt_bodies(world):
    ctx = pl.build_context(world["store"], world["outcomes"], PROJECT)
    text = json.dumps(ctx)
    assert ctx["project_id"] == PROJECT
    assert [t["id"] for t in ctx["tasks"]] == [world["a"]]
    assert "SECRET-PROMPT-BODY" not in text and world["foreign"] not in text
    assert ctx["outcomes"][0]["acceptance_criteria"] == ["renders"]


def test_context_redacts_secret_like_titles_and_is_capped(world):
    world["store"].append_tasks("brand-work-1", [{
        "title": "rotate ghp_" + "a" * 36, "prompt": "x", "project_id": PROJECT}])
    for i in range(300):
        world["store"].append_tasks("brand-work-2", [{
            "title": f"filler {i} " + "y" * 80, "prompt": "x", "project_id": PROJECT}])
    ctx = pl.build_context(world["store"], world["outcomes"], PROJECT, max_chars=4000)
    text = json.dumps(ctx)
    assert "ghp_" + "a" * 36 not in text
    assert len(text) <= 4000 and ctx["truncated"] is True


# -- validation -----------------------------------------------------------

def test_valid_plan_passes(world):
    result = pl.validate_plan(plan(task("b", depends_on=[world["a"]]), task("c", depends_on=["b"])),
                              store=world["store"], project_id=PROJECT, lanes=LANES, max_tasks=8)
    assert result.ok, result.errors


@pytest.mark.parametrize("mutate,code", [
    (lambda p: p.update(schema="projectflow.plan/v0"), "SCHEMA_VERSION"),
    (lambda p: p.update(project_id="synthetic/other"), "PROJECT_MISMATCH"),
    (lambda p: p.update(extra="x"), "UNKNOWN_FIELD"),
    (lambda p: p["tasks"][0].update(status="COMPLETED"), "UNKNOWN_FIELD"),
    (lambda p: p["tasks"].append(task("b")), "DUPLICATE_KEY"),
    (lambda p: p["tasks"][0].update(depends_on=["b"]), "DEPENDENCY_CYCLE"),
    (lambda p: p["tasks"][0].update(depends_on=["nope"]), "UNKNOWN_DEPENDENCY"),
    (lambda p: p["tasks"][0].update(priority=1000), "PRIORITY_RANGE"),
    (lambda p: p["tasks"][0].update(risk="high"), "HIGH_RISK_NEEDS_APPROVAL"),
    (lambda p: p["tasks"][0].update(prompt="then force-push to main"), "SENSITIVE_PROMPT"),
    (lambda p: p["tasks"][0].update(prompt="use token ghp_" + "b" * 36), "SECRET_LIKE_CONTENT"),
    (lambda p: p["tasks"][0].update(lane="window"), "LANE_NOT_ALLOWED"),
    (lambda p: p["tasks"][0].update(rationale=""), "RATIONALE_REQUIRED"),
])
def test_invalid_plans_are_rejected(world, mutate, code):
    raw = plan(task("b"))
    mutate(raw)
    result = pl.validate_plan(raw, store=world["store"], project_id=PROJECT, lanes=LANES, max_tasks=8)
    assert not result.ok and code in {e["code"] for e in result.errors}


def test_cross_project_dependency_is_rejected(world):
    raw = plan(task("b", depends_on=[world["foreign"]]))
    result = pl.validate_plan(raw, store=world["store"], project_id=PROJECT, lanes=LANES, max_tasks=8)
    assert "CROSS_PROJECT_DEPENDENCY" in {e["code"] for e in result.errors}


def test_oversize_plan_is_rejected(world):
    raw = plan(*[task(f"t{i}") for i in range(4)])
    result = pl.validate_plan(raw, store=world["store"], project_id=PROJECT, lanes=LANES, max_tasks=3)
    assert "TOO_MANY_TASKS" in {e["code"] for e in result.errors}


def test_high_risk_passes_only_with_operator_approval(world):
    raw = plan(task("b", risk="high"))
    result = pl.validate_plan(raw, store=world["store"], project_id=PROJECT, lanes=LANES,
                              max_tasks=8, approve_high_risk=True)
    assert result.ok


# -- apply ------------------------------------------------------------------

def test_apply_appends_only_and_is_idempotent(world):
    raw = plan(task("b", depends_on=[world["a"]]), task("c", depends_on=["b"]))
    store = world["store"]
    first = pl.apply_plan(raw, store=store, project_id=PROJECT, lanes=LANES, max_tasks=8)
    second = pl.apply_plan(raw, store=store, project_id=PROJECT, lanes=LANES, max_tasks=8)
    assert first["status"] == pl.APPLIED and first["created"] == second["created"]
    b, c = first["created"]
    assert store.get_task(b).depends_on == (world["a"],)
    assert store.get_task(c).depends_on == (b,)
    for task_id in (b, c):
        row = store.get_task(task_id)
        assert row.status == QUEUED and row.project_id == PROJECT
        assert row.metadata["plan"]["rationale"]
        assert "autopilot" not in row.metadata
    assert store.get_task(world["a"]).status == QUEUED  # existing work untouched


def test_apply_refuses_an_invalid_plan_and_writes_nothing(world):
    store = world["store"]
    before = len(store.list_tasks_for_project(PROJECT))
    result = pl.apply_plan(plan(task("b", depends_on=["b"])), store=store, project_id=PROJECT,
                           lanes=LANES, max_tasks=8)
    assert result["status"] == pl.REJECTED
    assert len(store.list_tasks_for_project(PROJECT)) == before


# -- providers, fallback, budget ----------------------------------------------

class CountingProvider:
    name = "fake-gateway"

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0
        self.contexts = []

    def propose(self, context):
        self.calls += 1
        self.contexts.append(context)
        if self.error:
            raise self.error
        return self.response


def _coordinator(world, provider, **cfg):
    return pl.ProjectPlanner(store=world["store"], outcomes=world["outcomes"], provider=provider,
                             config=PlannerProviderConfig(**cfg), lanes=LANES)


def test_provider_plan_is_validated_and_proposed_not_applied(world):
    provider = CountingProvider(plan(task("b", depends_on=[world["a"]])))
    result = _coordinator(world, provider).propose(PROJECT)
    assert result["status"] == pl.PROPOSED and result["provider"] == "fake-gateway"
    assert len(world["store"].list_tasks_for_project(PROJECT)) == 1  # nothing written


def test_provider_failure_falls_back_to_deterministic(world):
    result = _coordinator(world, CountingProvider(error=TimeoutError("gateway"))).propose(PROJECT)
    assert result["status"] == pl.FALLBACK_DETERMINISTIC
    assert result["plan"]["tasks"] == []


def test_invalid_provider_output_is_rejected_not_repaired(world):
    provider = CountingProvider({"schema": pl.PLAN_SCHEMA, "project_id": PROJECT,
                                 "tasks": [task("b", depends_on=["b"])]})
    result = _coordinator(world, provider).propose(PROJECT)
    assert result["status"] == pl.REJECTED and provider.calls == 1


def test_same_context_is_not_resent_and_budget_is_enforced(world):
    provider = CountingProvider(plan(task("b")))
    coord = _coordinator(world, provider, max_calls_per_hour=2)
    coord.propose(PROJECT)
    coord.propose(PROJECT)
    assert provider.calls == 1  # unchanged context -> cached, no repeat call
    world["store"].append_tasks("brand-work-2", [{"title": "n1", "prompt": "x", "project_id": PROJECT}])
    coord.propose(PROJECT)
    world["store"].append_tasks("brand-work-2", [{"title": "n2", "prompt": "x", "project_id": PROJECT}])
    result = coord.propose(PROJECT)
    assert provider.calls == 2 and result["status"] == pl.BUDGET_EXHAUSTED


def test_gateway_without_authorization_is_blocked_provider(world, tmp_path):
    provider = pl.GatewayPlanner(PlannerProviderConfig(
        kind="gateway", endpoint="https://gateway.invalid/v1/plan",
        token_file=str(tmp_path / "missing-token")))
    result = _coordinator(world, provider).propose(PROJECT)
    assert result["status"] == pl.FALLBACK_DETERMINISTIC
    assert result["provider_status"] == pl.BLOCKED_PROVIDER


def test_gateway_request_carries_bounded_context_and_never_the_token(world, tmp_path):
    token = tmp_path / "token"
    token.write_text("sk-test-not-real-0123456789\n")
    token.chmod(0o600)
    sent = {}

    def http_post(url, body, headers, timeout):
        sent.update(url=url, body=body, headers=headers)
        return json.dumps(plan(task("b"))).encode()

    provider = pl.GatewayPlanner(PlannerProviderConfig(
        kind="gateway", endpoint="https://gateway.invalid/v1/plan", token_file=str(token)),
        http_post=http_post)
    result = _coordinator(world, provider).propose(PROJECT)
    assert result["status"] == pl.PROPOSED
    assert "sk-test" not in sent["body"].decode() and "SECRET-PROMPT-BODY" not in sent["body"].decode()
    assert sent["headers"]["Authorization"].startswith("Bearer ")
    assert "sk-test" not in json.dumps(result)


def test_world_readable_token_file_is_refused(world, tmp_path):
    token = tmp_path / "token"
    token.write_text("sk-test-not-real\n")
    token.chmod(0o644)
    provider = pl.GatewayPlanner(PlannerProviderConfig(
        kind="gateway", endpoint="https://gateway.invalid/v1/plan", token_file=str(token)),
        http_post=lambda *a: pytest.fail("must not call"))
    result = _coordinator(world, provider).propose(PROJECT)
    assert result["provider_status"] == pl.BLOCKED_PROVIDER
