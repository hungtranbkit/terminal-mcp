"""Read policy: what an authenticated operator may see, and what nobody may.

The audit that produced this file found the opposite of what was reported.
Operators said they were "blocked" from the audit log; in fact reads were
already default-open and there was simply no surface to ask through. Both
halves are tested here: the surfaces now exist and serve operational data in
full, and the secret boundary did not move an inch.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from terminal_mcp.access_policy import (ROLE_ANONYMOUS, ROLE_OPERATOR, ROLE_OWNER,
                                        TIER_OPERATIONAL, TIER_SECRET, TIER_SENSITIVE,
                                        Decision, filter_record, may_read, policy_table,
                                        role_for_identity, tier_for)
from terminal_mcp.audit import AuditStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import AUDIT_HTML, register_dashboard
from terminal_mcp.mcp_app import build_mcp


# -- the tiers ----------------------------------------------------------------

@pytest.mark.parametrize("field", [
    "password", "passphrase", "token", "auth_token", "api_key", "private_key",
    "cookie", "authorization", "bearer", "secret_env", "aws_secret_access_key",
])
def test_a_secret_has_no_read_path_for_any_role(field):
    """Not "redacted unless a flag": absent. A masked value still tells an
    attacker the field is populated, and tempts the next person to add a
    debug switch that unmasks it."""
    for role in (ROLE_ANONYMOUS, ROLE_OPERATOR, ROLE_OWNER):
        decision = may_read(field, role=role)
        assert decision.allowed is False
        assert decision.tier == TIER_SECRET
        assert "SECRET" in decision.reason


@pytest.mark.parametrize("field", [
    "timestamp", "actor", "action", "session", "node_id", "result", "reason",
    "correlation_id", "latency_ms", "policy_source", "policy_version",
    "auth_status", "auth_source", "capability", "readiness", "last_verified_at",
])
def test_operational_data_is_readable_by_an_authenticated_operator(field):
    """This is the half that was too strict in effect, if not in code: an
    operator debugging an incident needs every one of these."""
    assert may_read(field, role=ROLE_OPERATOR).allowed is True
    assert tier_for(field) == TIER_OPERATIONAL


@pytest.mark.parametrize("field", ["text_preview", "text_sha256", "last_output",
                                   "host_key_fingerprint", "public_key_id"])
def test_sensitive_metadata_is_readable_but_classified_as_such(field):
    assert tier_for(field) == TIER_SENSITIVE
    assert may_read(field, role=ROLE_OPERATOR).allowed is True


def test_an_unclassified_credential_shaped_field_defaults_to_secret():
    """A column added next year without a policy row must not be served
    because nobody remembered to classify it."""
    assert tier_for("brand_new_token") == TIER_SECRET
    assert tier_for("some_password_field") == TIER_SECRET
    assert may_read("future_api_key", role=ROLE_OPERATOR).allowed is False


def test_an_unclassified_ordinary_field_is_not_gratuitously_denied():
    """The other half of the same rule. Defaulting everything unknown to
    denied is precisely the deny-all posture this pass was called to fix."""
    assert tier_for("dispatch_duration") == TIER_OPERATIONAL
    assert may_read("dispatch_duration", role=ROLE_OPERATOR).allowed is True
    # ...and metadata ABOUT a secret stays readable.
    assert may_read("auth_token_ref", role=ROLE_OPERATOR).allowed is True
    assert may_read("token_file", role=ROLE_OPERATOR).allowed is True
    assert may_read("auth_status", role=ROLE_OPERATOR).allowed is True


def test_an_anonymous_caller_reads_nothing_operational():
    """The boundary that must not move. Loosening for operators must not
    loosen for someone with no verified identity."""
    for field in ("actor", "reason", "session", "text_preview"):
        decision = may_read(field, role=ROLE_ANONYMOUS)
        assert decision.allowed is False
        assert "verified identity" in decision.reason


def test_a_refusal_always_says_which_field_and_which_tier():
    """A generic 403 teaches an operator nothing and trains them to escalate
    instead of read -- which is how guards end up switched off."""
    decision = may_read("password", role=ROLE_OPERATOR)
    assert decision.field == "password" and decision.tier == TIER_SECRET
    assert decision.as_dict()["reason"]


def test_filter_record_drops_secrets_and_keeps_the_rest():
    filtered = filter_record({
        "actor": "alice@example.com", "action": "send_text", "reason": "ACCESS_DENIED",
        "auth_token": "should-not-survive", "password": "nor-this",
        "text_preview": "redacted preview", "latency_ms": 12.0,
    }, role=ROLE_OPERATOR)
    assert filtered == {"actor": "alice@example.com", "action": "send_text",
                        "reason": "ACCESS_DENIED", "text_preview": "redacted preview",
                        "latency_ms": 12.0}


def test_access_not_being_configured_does_not_lock_the_operator_out():
    """A self-hosted deployment protects this service at the network edge.
    Treating that as anonymous would deny every operator their own audit log
    -- the exact over-restriction under audit."""
    assert role_for_identity(None, access_configured=False) == ROLE_OPERATOR
    assert role_for_identity(None, access_configured=True) == ROLE_ANONYMOUS
    assert role_for_identity(object(), access_configured=True) == ROLE_OPERATOR


def test_the_policy_table_is_published_not_inferred():
    table = policy_table()
    tiers = {row["tier"] for row in table}
    assert tiers == {TIER_SECRET, TIER_SENSITIVE, TIER_OPERATIONAL}
    assert all(row["rationale"] for row in table), "every row explains itself"


# -- the audit store ------------------------------------------------------------

@pytest.fixture
def audit(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    now = datetime.now(timezone.utc)
    store.record(action="send_text", session="m1", result="SENT", actor="alice@example.com",
                 node_id="local", text="hello world", latency_ms=8.0,
                 policy_source="session_grant", policy_version="v2",
                 correlation_id="corr-1")
    store.record(action="send_text", session="hp1", result="DENIED",
                 actor="bob@example.com", node_id="hp-linux", reason="ACCESS_DENIED",
                 latency_ms=2.0, policy_source="input_policy", policy_version="v1")
    store.record(action="grant_read", session="m2", result="BLOCKED",
                 actor="alice@example.com", node_id="local", reason="STALE_IDENTITY_PIN",
                 policy_source="session_grant")
    return store


def test_a_denied_row_records_both_who_and_why(audit):
    """The defect this replaced: actor and reason shared one column, so a
    BLOCKED action recorded why it failed and forgot who attempted it -- the
    one row where losing half matters most."""
    denied = audit.search(denied_only=True)["events"]
    blocked = [e for e in denied if e["result"] == "BLOCKED"][0]
    assert blocked["actor"] == "alice@example.com"
    assert blocked["reason"] == "STALE_IDENTITY_PIN"


def test_every_field_an_operator_needs_to_debug_is_present(audit):
    event = audit.search(action="send_text", result="SENT")["events"][0]
    for field in ("timestamp", "actor", "action", "session", "node_id", "result",
                  "correlation_id", "latency_ms", "policy_source", "policy_version",
                  "source_transport", "server_version", "text_sha256", "preview"):
        assert field in event, field


def test_raw_text_is_never_stored_only_a_fingerprint_and_a_preview(audit):
    event = audit.search(action="send_text", result="SENT")["events"][0]
    assert event["text_sha256"] and len(event["text_sha256"]) == 64
    assert event["preview"] == "hello world"
    assert "text" not in event


def test_a_secret_in_the_sent_text_is_redacted_in_the_preview(tmp_path):
    store = AuditStore(tmp_path / "a.db")
    store.record(action="send_text", session="m1", result="SENT", actor="a@b",
                 text="export OPENAI_API_KEY=sk-realsecret123 && Authorization: Bearer abc123")
    preview = store.search()["events"][0]["preview"]
    assert "sk-realsecret123" not in preview
    assert "abc123" not in preview
    assert "<REDACTED>" in preview


@pytest.mark.parametrize("kwargs,expected", [
    ({"actor": "alice@example.com"}, 2),
    ({"action": "grant_read"}, 1),
    ({"node_id": "hp-linux"}, 1),
    ({"result": "SENT"}, 1),
    ({"denied_only": True}, 2),
    ({"query": "ACCESS_DENIED"}, 1),
    ({"query": "corr-1"}, 1),
])
def test_the_log_can_actually_be_narrowed(audit, kwargs, expected):
    """Without filters this was a 50-row reverse-chronological list -- not a
    privacy control, just an unusable one."""
    assert audit.search(**kwargs)["total"] == expected


def test_a_time_range_narrows_the_log(audit):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert audit.search(since=future)["total"] == 0
    assert audit.search(since=past)["total"] == 3
    assert audit.search(until=past)["total"] == 0


def test_paging_reports_where_it_is(audit):
    page = audit.search(limit=2, offset=0)
    assert (page["returned"], page["total"], page["has_more"]) == (2, 3, True)
    assert audit.search(limit=2, offset=2)["has_more"] is False


def test_search_cannot_be_used_to_confirm_a_guessed_plaintext(audit):
    """Searching the hash would turn a fingerprint back into an oracle: a
    caller could confirm a guess by seeing whether it matched."""
    digest = audit.search(action="send_text", result="SENT")["events"][0]["text_sha256"]
    assert audit.search(query=digest)["total"] == 0


def test_the_filter_value_helper_does_not_take_sql_from_the_caller(audit):
    assert audit.distinct_values("actor") == ["alice@example.com", "bob@example.com"]
    assert audit.distinct_values("actor; DROP TABLE input_audit") == []
    assert audit.distinct_values("text_sha256") == [], "not an allowlisted filter column"
    assert audit.search()["total"] == 3, "the table is still here"


def test_the_legacy_list_signature_still_works(audit):
    """Existing callers must not break on a permission fix."""
    assert len(audit.list(2)) == 2
    assert audit.list(10, None, "m1")[0]["session"] == "m1"


# -- the HTTP and MCP surfaces --------------------------------------------------

def _config():
    return AppConfig(PermissionsConfig(True, True), ("test-*",), 50, 20,
                     InputPolicyConfig(allowed_session_patterns=("test-*",)))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_AUDIT_DB", str(tmp_path / "audit.db"))
    # A disposable fleet store passed to BOTH surfaces. Without it the MCP
    # side falls back to opening the real ~/.local/state fleet_registry.db --
    # the same "a test must never touch production state" discipline the rest
    # of this codebase keeps, and the reason the two surfaces appeared to
    # disagree about auth status when they were simply reading different
    # databases.
    from terminal_mcp.fleet_registry import FleetRegistryStore
    from terminal_mcp.fleet_service import FleetService

    fleet = FleetService(FleetRegistryStore(tmp_path / "fleet.db", local_node_id="local"),
                         local_node_id="local")
    service = TerminalService(_config())
    service.audit.record(action="send_text", session="m1", result="SENT",
                         actor="alice@example.com", node_id="local",
                         text="deploy now OPENAI_API_KEY=sk-leak", latency_ms=5.0,
                         policy_source="session_grant", policy_version="v2")
    service.audit.record(action="send_text", session="hp1", result="DENIED",
                         actor="bob@example.com", node_id="hp-linux",
                         reason="ACCESS_DENIED", policy_source="input_policy")
    server = build_mcp(service, fleet=fleet)
    register_dashboard(server, service, fleet=fleet)
    return TestClient(server.streamable_http_app()), server, service


def test_the_audit_log_is_reachable_over_http_at_all(client):
    """The headline finding: there was no route. A missing surface and a
    deny-all guard are indistinguishable from outside."""
    http, _server, _service = client
    payload = http.get("/dashboard/api/audit").json()
    assert payload["total"] == 2
    assert {e["actor"] for e in payload["events"]} == {"alice@example.com", "bob@example.com"}
    assert payload["role"] == "operator"


def test_the_http_audit_serves_deny_reasons(client):
    http, _server, _service = client
    denied = http.get("/dashboard/api/audit?denied=1").json()["events"]
    assert denied[0]["reason"] == "ACCESS_DENIED"
    assert denied[0]["policy_source"] == "input_policy"


def test_the_http_audit_offers_the_filter_values_a_ui_needs(client):
    http, _server, _service = client
    filters = http.get("/dashboard/api/audit").json()["filters"]
    assert filters["actor"] == ["alice@example.com", "bob@example.com"]
    assert "send_text" in filters["action"]


def test_no_secret_survives_the_http_audit(client):
    http, _server, _service = client
    flat = json.dumps(http.get("/dashboard/api/audit").json())
    assert "sk-leak" not in flat
    for forbidden in ('"password"', '"auth_token"', '"private_key"', '"cookie"'):
        assert forbidden not in flat


def test_the_csv_export_uses_the_same_filter_and_the_same_policy(client):
    http, _server, _service = client
    body = http.get("/dashboard/api/audit/export?denied=1").text
    assert "timestamp,actor,action" in body.splitlines()[0]
    assert "ACCESS_DENIED" in body
    assert "alice@example.com" not in body, "the denied filter applied"
    assert "sk-leak" not in body
    assert "password" not in body.splitlines()[0]


def test_auth_status_says_who_is_authenticated_without_a_credential(client):
    http, _server, _service = client
    payload = http.get("/dashboard/api/auth-status").json()
    assert "nodes" in payload and "readiness" in payload
    # Scoped to the DATA, not the whole response: the payload also carries
    # the policy table, which necessarily contains the word "token" in the
    # row declaring that tokens are SECRET. Asserting on the envelope would
    # ban the declaration along with the thing it forbids.
    flat = json.dumps({"nodes": payload["nodes"], "readiness": payload["readiness"]})
    for forbidden in ("BEGIN OPENSSH", '"token"', '"password"', '"private_key"'):
        assert forbidden not in flat


def test_the_policy_table_is_served(client):
    http, _server, _service = client
    payload = http.get("/dashboard/api/access-policy").json()
    assert {row["tier"] for row in payload["tiers"]} == {
        "SECRET", "SENSITIVE_METADATA", "OPERATIONAL"}


def test_the_audit_page_renders(client):
    http, _server, _service = client
    response = http.get("/dashboard/audit")
    assert response.status_code == 200
    assert "Audit" in response.text
    assert "/dashboard/api/audit" in response.text


def test_the_page_explains_the_tiers_rather_than_leaving_them_to_be_guessed():
    for word in ("SECRET", "SENSITIVE", "OPERATIONAL"):
        assert word in AUDIT_HTML


def test_mcp_and_http_return_the_same_rows(client):
    """A surface that can see something the other cannot is how an operator
    gets told to "just use the UI" for data the API refuses."""
    http, server, _service = client
    over_http = http.get("/dashboard/api/audit?limit=50").json()["events"]
    over_mcp = server._tool_manager._tools["terminal_audit_search"].fn(limit=50)["events"]
    assert [e["correlation_id"] for e in over_http] == [e["correlation_id"] for e in over_mcp]
    assert [sorted(e) for e in over_http] == [sorted(e) for e in over_mcp]


def test_the_mcp_tools_expose_auth_status_and_the_policy(client):
    _http, server, _service = client
    policy = server._tool_manager._tools["terminal_access_policy"].fn()
    assert {row["tier"] for row in policy["tiers"]} == {
        "SECRET", "SENSITIVE_METADATA", "OPERATIONAL"}


def test_reading_the_audit_is_not_gated_on_session_permissions(client):
    """Requirement 5: an operator with system access must read the full
    sanitized history. A stale per-session grant row must not hide it."""
    http, _server, service = client
    service.audit.record(action="send_text", session="not-whitelisted-at-all",
                         result="SENT", actor="alice@example.com", text="x")
    sessions = {e["session"] for e in http.get("/dashboard/api/audit").json()["events"]}
    assert "not-whitelisted-at-all" in sessions


# -- staleness --------------------------------------------------------------

def test_a_stale_cache_never_reports_a_node_as_authenticated():
    """Observed live on 2026-09-12, minutes after deploying this feature:
    hp-linux had been offline for five minutes while the auth view reported
    AUTHENTICATED, because it read `status` straight out of a 6.8-hour-old
    cache. Asserting a live fact from stale evidence is the failure this
    whole codebase refuses everywhere else.
    """
    from terminal_mcp.fleet_service import auth_status_for_node

    stale = {"node_id": "hp-linux", "status": "online",
             "metadata_stale": True, "metadata_age_seconds": 24474.0}
    status, reason = auth_status_for_node(stale)
    assert status == "UNKNOWN_STALE"
    assert "6h old" in reason

    fresh_online = {"node_id": "a", "status": "online", "metadata_stale": False,
                    "metadata_age_seconds": 10.0}
    assert auth_status_for_node(fresh_online)[0] == "AUTHENTICATED"

    fresh_offline = {"node_id": "b", "status": "offline", "metadata_stale": False,
                     "metadata_age_seconds": 10.0}
    assert auth_status_for_node(fresh_offline)[0] == "UNREACHABLE"


def test_both_surfaces_agree_on_auth_status(client):
    """A status that disagreed between the dashboard and MCP would be worse
    than either answer on its own."""
    http, server, _service = client
    over_http = {n["node_id"]: n["auth_status"]
                 for n in http.get("/dashboard/api/auth-status").json()["nodes"]}
    over_mcp = {n["node_id"]: n["auth_status"]
                for n in server._tool_manager._tools["terminal_auth_status"].fn()["nodes"]}
    assert over_http == over_mcp
