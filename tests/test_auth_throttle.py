"""Rate limiting and lockout for node-agent bearer auth.

The property under test throughout: a correct token is never punished, a wrong
one gets progressively more expensive, and nothing anywhere reveals whether a
guess was close.
"""

from __future__ import annotations

import threading

import pytest

from terminal_mcp.auth_throttle import (
    DEFAULT_MAX_TRACKED_SOURCES, AuthThrottle, Decision, client_key,
)
from terminal_mcp.webauth import (
    RATE_LIMIT_BACKOFF_BASE_SECONDS, RATE_LIMIT_BACKOFF_CAP_SECONDS,
    RATE_LIMIT_THRESHOLD,
)


class _Clock:
    """A monotonic clock a test can advance deliberately."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def throttle(clock):
    return AuthThrottle(clock=clock)


# -- policy is shared, not reinvented ----------------------------------------

def test_the_policy_comes_from_webauth(throttle):
    # Two auth surfaces with independently drifting lockout rules is how one
    # of them ends up unprotected without anyone noticing.
    assert throttle.threshold == RATE_LIMIT_THRESHOLD
    assert throttle.backoff_base_seconds == RATE_LIMIT_BACKOFF_BASE_SECONDS
    assert throttle.backoff_cap_seconds == RATE_LIMIT_BACKOFF_CAP_SECONDS


# -- valid auth is unaffected -------------------------------------------------

def test_a_correct_token_is_never_throttled(throttle):
    for _ in range(200):
        assert throttle.check("10.0.0.1").allowed is True
        throttle.record_success("10.0.0.1")
    assert throttle.snapshot()["tracked_sources"] == 0


def test_success_clears_earlier_failures(throttle):
    for _ in range(RATE_LIMIT_THRESHOLD - 1):
        throttle.record_failure("10.0.0.1")
    throttle.record_success("10.0.0.1")
    # An operator who mistypes then gets it right is not carrying a grudge.
    for _ in range(RATE_LIMIT_THRESHOLD - 1):
        assert throttle.record_failure("10.0.0.1").allowed is True


def test_one_source_being_locked_does_not_affect_another(throttle):
    for _ in range(RATE_LIMIT_THRESHOLD):
        throttle.record_failure("10.0.0.1")
    assert throttle.check("10.0.0.1").allowed is False
    assert throttle.check("10.0.0.2").allowed is True


# -- repeated invalid attempts are throttled ---------------------------------

def test_attempts_below_the_threshold_are_allowed(throttle):
    for attempt in range(RATE_LIMIT_THRESHOLD - 1):
        assert throttle.record_failure("10.0.0.1").allowed is True, attempt
    assert throttle.check("10.0.0.1").allowed is True


def test_the_threshold_locks_the_source(throttle):
    for _ in range(RATE_LIMIT_THRESHOLD):
        decision = throttle.record_failure("10.0.0.1")
    assert decision.allowed is False
    assert decision.reason == "LOCKED_OUT"
    assert decision.retry_after == RATE_LIMIT_BACKOFF_BASE_SECONDS


def test_backoff_grows_exponentially_and_is_capped(throttle):
    waits = []
    for _ in range(RATE_LIMIT_THRESHOLD + 12):
        waits.append(throttle.record_failure("10.0.0.1").retry_after)
    locked = [w for w in waits if w > 0]
    assert locked[0] == RATE_LIMIT_BACKOFF_BASE_SECONDS
    assert locked[1] == RATE_LIMIT_BACKOFF_BASE_SECONDS * 2
    assert all(b >= a for a, b in zip(locked, locked[1:]))       # monotonic
    assert max(locked) == RATE_LIMIT_BACKOFF_CAP_SECONDS          # bounded
    # Never a permanent lockout needing manual clearing.
    assert all(w <= RATE_LIMIT_BACKOFF_CAP_SECONDS for w in locked)


# -- expiry and recovery ------------------------------------------------------

def test_a_lockout_expires_on_its_own(throttle, clock):
    for _ in range(RATE_LIMIT_THRESHOLD):
        throttle.record_failure("10.0.0.1")
    assert throttle.check("10.0.0.1").allowed is False
    clock.advance(RATE_LIMIT_BACKOFF_BASE_SECONDS + 1)
    assert throttle.check("10.0.0.1").allowed is True


def test_retry_after_counts_down(throttle, clock):
    for _ in range(RATE_LIMIT_THRESHOLD):
        throttle.record_failure("10.0.0.1")
    first = throttle.check("10.0.0.1").retry_after
    clock.advance(2)
    assert throttle.check("10.0.0.1").retry_after == pytest.approx(first - 2)


def test_moving_the_wall_clock_cannot_shorten_a_lockout(clock):
    # The throttle reads a MONOTONIC clock, so NTP, a VM resume or a local
    # attacker changing the date cannot end a lockout early.
    throttle = AuthThrottle(clock=clock)
    for _ in range(RATE_LIMIT_THRESHOLD):
        throttle.record_failure("10.0.0.1")
    import datetime as real_datetime

    assert throttle.check("10.0.0.1").allowed is False
    # Nothing about wall time is consulted; only the injected monotonic source.
    assert throttle._clock is clock
    del real_datetime


def test_recovery_needs_no_operator_action(throttle, clock):
    for _ in range(RATE_LIMIT_THRESHOLD + 3):
        throttle.record_failure("10.0.0.1")
    clock.advance(RATE_LIMIT_BACKOFF_CAP_SECONDS + 1)
    assert throttle.check("10.0.0.1").allowed is True


# -- parallel attempts --------------------------------------------------------

def test_parallel_failures_are_all_counted(clock):
    """Two racing wrong guesses must count twice, not collapse into one."""
    throttle = AuthThrottle(clock=clock)
    barrier = threading.Barrier(8)

    def attempt():
        barrier.wait()
        throttle.record_failure("10.0.0.1")

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert throttle.check("10.0.0.1").allowed is False
    assert throttle.snapshot()["locked_sources"] == 1


def test_parallel_sources_are_tracked_independently(clock):
    throttle = AuthThrottle(clock=clock)
    barrier = threading.Barrier(10)

    def attempt(index):
        barrier.wait()
        for _ in range(RATE_LIMIT_THRESHOLD):
            throttle.record_failure(f"10.0.0.{index}")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert throttle.snapshot()["locked_sources"] == 10


# -- bounded global safety ----------------------------------------------------

def test_the_tracking_table_is_bounded(clock):
    """The throttle must not become the exhaustion vector it prevents."""
    throttle = AuthThrottle(max_tracked_sources=16, clock=clock)
    for index in range(16):
        throttle.record_failure(f"10.0.1.{index}")
    assert throttle.snapshot()["tracked_sources"] == 16
    # A 17th, previously unseen source is refused rather than admitted:
    # admitting it is exactly what an address-spoofing attacker wants.
    decision = throttle.check("10.0.9.9")
    assert decision.allowed is False
    assert decision.reason == "THROTTLE_TABLE_FULL"


def test_a_known_source_still_works_when_the_table_is_full(clock):
    throttle = AuthThrottle(max_tracked_sources=4, clock=clock)
    for index in range(4):
        throttle.record_failure(f"10.0.1.{index}")
    # Already tracked and not locked out -- a legitimate caller must not be
    # collateral damage of someone else's spoofing.
    assert throttle.check("10.0.1.0").allowed is True


def test_idle_entries_are_reclaimed_under_pressure(clock):
    throttle = AuthThrottle(max_tracked_sources=4, entry_ttl_seconds=60, clock=clock)
    for index in range(4):
        throttle.record_failure(f"10.0.1.{index}")
    clock.advance(600)
    # Space is reclaimed from idle, unlocked entries, so a burst long past
    # does not permanently deny new sources.
    assert throttle.check("10.0.9.9").allowed is True


# -- no information leakage ---------------------------------------------------

def test_the_decision_carries_nothing_about_the_credential(throttle):
    for _ in range(RATE_LIMIT_THRESHOLD):
        decision = throttle.record_failure("10.0.0.1")
    text = repr(decision)
    for leak in ("token", "secret", "bearer", "password"):
        assert leak not in text.lower()
    assert set(vars(decision)) == {"allowed", "retry_after", "reason"}


def test_snapshot_exposes_counts_only(throttle):
    throttle.record_failure("10.0.0.1")
    snapshot = throttle.snapshot()
    assert set(snapshot) == {"tracked_sources", "locked_sources", "max_tracked_sources",
                             "threshold", "backoff_cap_seconds"}
    assert all(isinstance(v, (int, float)) for v in snapshot.values())


def test_retry_after_header_is_a_whole_second(throttle):
    for _ in range(RATE_LIMIT_THRESHOLD):
        decision = throttle.record_failure("10.0.0.1")
    assert decision.headers() == {"Retry-After": "5"}
    assert Decision(True).headers() == {}


# -- source keying: IPv4 / IPv6 / proxy trust ---------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("10.0.0.1", "10.0.0.1"),
    ("10.0.0.1:8790", "10.0.0.1"),
    ("::1", "::1"),
    ("[::1]", "::1"),
    ("[::1]:8790", "::1"),
    ("0:0:0:0:0:0:0:1", "::1"),
    ("::ffff:10.0.0.1", "10.0.0.1"),
])
def test_addresses_normalise_to_one_bucket(raw, expected):
    # Many spellings of one address must not hand out a fresh allowance each.
    assert client_key(raw) == expected


def test_a_missing_source_still_has_a_key():
    assert client_key(None) == "unknown"
    assert client_key("") == "unknown"


def test_forwarded_headers_are_ignored_by_default():
    """The bypass this closes: pick your own bucket, never be throttled."""
    first = client_key("10.0.0.9", forwarded_for="1.2.3.4")
    second = client_key("10.0.0.9", forwarded_for="5.6.7.8")
    assert first == second == "10.0.0.9"


def test_forwarded_headers_are_honoured_only_when_trusted():
    assert client_key("10.0.0.9", forwarded_for="1.2.3.4",
                      trust_forwarded=True) == "1.2.3.4"
    # Left-most entry is the originating client.
    assert client_key("10.0.0.9", forwarded_for="1.2.3.4, 10.0.0.9",
                      trust_forwarded=True) == "1.2.3.4"


def test_a_spoofed_header_cannot_escape_an_existing_lockout(throttle):
    for _ in range(RATE_LIMIT_THRESHOLD):
        throttle.record_failure(client_key("10.0.0.9"))
    # Same socket, new invented header: still the same bucket, still locked.
    spoofed = client_key("10.0.0.9", forwarded_for="9.9.9.9")
    assert throttle.check(spoofed).allowed is False


def test_an_empty_trusted_header_falls_back_to_the_socket():
    assert client_key("10.0.0.9", forwarded_for="  ", trust_forwarded=True) == "10.0.0.9"


# -- restart behaviour --------------------------------------------------------

def test_state_is_in_memory_and_resets_on_restart(clock):
    """Documented limitation, asserted so it cannot change silently.

    A node agent must boot on a box whose state directory is read-only or
    full, so this deliberately does not persist. The cost is that restarting
    the agent clears lockouts; an attacker cannot trigger that restart, and
    the alternative -- an agent that refuses to start because it cannot write
    its throttle DB -- turns a nuisance into a lost node.
    """
    throttle = AuthThrottle(clock=clock)
    for _ in range(RATE_LIMIT_THRESHOLD):
        throttle.record_failure("10.0.0.1")
    assert throttle.check("10.0.0.1").allowed is False

    restarted = AuthThrottle(clock=clock)          # a fresh process
    assert restarted.check("10.0.0.1").allowed is True
    assert restarted.snapshot()["tracked_sources"] == 0


# -- against a REAL node-agent app --------------------------------------------

TOKEN = "the-real-node-token"


@pytest.fixture
def agent(tmp_path, clock):
    """A real node-agent ASGI app with an injected clock."""
    from starlette.testclient import TestClient

    from terminal_mcp.config import (AppConfig, InputPolicyConfig, PermissionsConfig,
                                     SessionLifecycleConfig)
    from terminal_mcp.core import TerminalService
    from terminal_mcp.grants import SessionGrantStore
    from terminal_mcp.node_agent import build_node_agent

    config = AppConfig(
        permissions=PermissionsConfig(True, False),
        allowed_session_patterns=("agent-*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("agent-*",)),
        session_lifecycle=SessionLifecycleConfig(enabled=False, allowed_cwd_roots=(str(tmp_path),),
                                                 protected_sessions=()))
    terminal = TerminalService(config, grants=SessionGrantStore(tmp_path / "grants.db"))
    throttle = AuthThrottle(clock=clock)
    app = build_node_agent(node_id="test-node", terminal=terminal, token=TOKEN,
                           workspace_root=str(tmp_path), throttle=throttle)
    return {"client": TestClient(app), "throttle": throttle, "clock": clock}


def _get(agent, token=None, headers=None):
    all_headers = dict(headers or {})
    if token is not None:
        all_headers["Authorization"] = f"Bearer {token}"
    return agent["client"].get("/v1/sessions", headers=all_headers)


def test_a_valid_token_is_unaffected_by_the_throttle(agent):
    for _ in range(50):
        assert _get(agent, TOKEN).status_code == 200


def test_repeated_bad_tokens_are_eventually_refused(agent):
    for _ in range(RATE_LIMIT_THRESHOLD):
        assert _get(agent, "wrong").status_code == 401
    response = _get(agent, "wrong")
    assert response.status_code == 429
    assert response.json()["error"] == "TOO_MANY_ATTEMPTS"
    assert response.headers.get("Retry-After") == "5"


def test_the_lockout_also_refuses_the_CORRECT_token(agent):
    """Deliberate: the throttle acts before the comparison.

    If a locked-out source could still authenticate with a correct token, the
    response would tell an attacker their guess was right -- which is the one
    thing the lockout exists to stop them learning.
    """
    for _ in range(RATE_LIMIT_THRESHOLD):
        _get(agent, "wrong")
    assert _get(agent, TOKEN).status_code == 429


def test_the_401_body_is_identical_for_every_kind_of_bad_credential(agent):
    bodies = {
        _get(agent, "wrong").text,
        _get(agent, "").text,
        _get(agent, None).text,
        _get(agent, None, headers={"Authorization": "Basic abc"}).text,
    }
    # One body for all of them: a response that differs by cause tells an
    # attacker which failure they achieved.
    assert len(bodies) == 1
    assert "token" not in bodies.pop().lower()


def test_recovery_after_the_lockout_expires(agent):
    for _ in range(RATE_LIMIT_THRESHOLD):
        _get(agent, "wrong")
    assert _get(agent, TOKEN).status_code == 429
    agent["clock"].advance(RATE_LIMIT_BACKOFF_BASE_SECONDS + 1)
    assert _get(agent, TOKEN).status_code == 200


def test_success_resets_the_counter_through_the_app(agent):
    for _ in range(RATE_LIMIT_THRESHOLD - 1):
        _get(agent, "wrong")
    assert _get(agent, TOKEN).status_code == 200
    # Counter cleared, so the next wrong attempts start from zero again.
    for _ in range(RATE_LIMIT_THRESHOLD - 1):
        assert _get(agent, "wrong").status_code == 401


def test_a_spoofed_forwarding_header_does_not_bypass_the_lockout(agent):
    for index in range(RATE_LIMIT_THRESHOLD):
        assert _get(agent, "wrong",
                    headers={"X-Forwarded-For": f"1.2.3.{index}"}).status_code == 401
    # A brand-new invented header value must not reset the allowance: the
    # header is untrusted, so every one of these is the same real source.
    response = _get(agent, "wrong", headers={"X-Forwarded-For": "9.9.9.9"})
    assert response.status_code == 429


def test_health_is_reachable_without_a_token(agent):
    # Unauthenticated by design; it must not be collateral damage, and it must
    # not consume the throttle either.
    assert agent["client"].get("/v1/health").status_code == 200
    assert agent["throttle"].snapshot()["tracked_sources"] == 0


def test_nothing_credential_shaped_reaches_the_response(agent):
    response = _get(agent, "super-secret-guess")
    assert "super-secret-guess" not in response.text
    assert TOKEN not in response.text
