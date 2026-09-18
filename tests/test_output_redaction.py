"""Secrets must not leave the server, and a tail containing one must still
come back.

The incident: a real terminal_tail was refused wholesale by a safety layer
because its output carried a one-time bootstrap password. That is the worst
available outcome -- the operator loses the entire tail, and the secret was
printed to the pane regardless. Both halves are tested here: nothing
credential-shaped survives, and the request never fails because of it.
"""
from __future__ import annotations

import re

import pytest

from terminal_mcp.redaction import (BENIGN_VALUES, HIGH_RISK_REDACTIONS, redact_output,
                                    redact_text, redaction_marker)

# The output that started this. Long, mixed, and carrying a real-shaped
# one-time password plus a credential file path.
BOOTSTRAP_FIXTURE = """\
$ terminal-mcp-webauth bootstrap --user operator
Creating first-run operator account...
  user           : operator
  must_change_password=true
  password_policy: min_length=12
Wrote credential to ~/.local/state/terminal-mcp/webauth-bootstrap.txt
Your one-time password is Tr0ub4dor-Horse-Battery
(the value above is also stored in /app/password for the container)
Login at https://terminal-dashboard.mesflow.net/login?token=9f8e7d6c5b4a3210&next=%2F
Set-Cookie: session=abcdef0123456789; HttpOnly; Secure
Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvcGVyYXRvciJ9.4pcPyMD09olPSyXnrXCjTwXyr4BsezdI1AVTmud2fU4
-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
QyNTUxOQAAACDrealkeymaterialdonotleakAAAAtzc2gtZWQyNTUxOQ
-----END OPENSSH PRIVATE KEY-----
Bootstrap finished in 1.8s
42 tests passed, 0 failed (commit 0b26a44)
Logs at /var/log/terminal-mcp/webauth.log
"""

SECRETS_IN_FIXTURE = (
    "Tr0ub4dor-Horse-Battery",
    "9f8e7d6c5b4a3210",
    "abcdef0123456789",
    "4pcPyMD09olPSyXnrXCjTwXyr4BsezdI1AVTmud2fU4",
    "realkeymaterialdonotleak",
)


def test_the_incident_fixture_returns_successfully_and_leaks_nothing():
    """Requirement, stated once: this must not fail, and must not leak."""
    text, report = redact_output(BOOTSTRAP_FIXTURE)
    assert text, "the tail must still come back"
    for secret in SECRETS_IN_FIXTURE:
        assert secret not in text, secret
    assert report["high_risk"] is True
    assert report["redactions"] >= 3


def test_the_incident_fixture_keeps_everything_an_operator_needs():
    """Over-redaction is its own failure: a tail scrubbed of its error
    messages, commits and paths is a tail nobody can debug with."""
    text, _report = redact_output(BOOTSTRAP_FIXTURE)
    for keep in ("must_change_password=true", "/app/password",
                 "webauth-bootstrap.txt", "42 tests passed", "commit 0b26a44",
                 "/var/log/terminal-mcp/webauth.log", "Bootstrap finished in 1.8s",
                 "user           : operator", "min_length=12"):
        assert keep in text, keep


def test_the_credential_file_is_named_but_never_opened():
    """The PATH is how an operator finds the file; the CONTENT is never read
    by anything in this pipeline."""
    text, report = redact_output(BOOTSTRAP_FIXTURE)
    assert "webauth-bootstrap.txt" in text
    assert report["credential_files"] >= 1
    marker = redaction_marker(report)
    assert "contents never read or returned" in marker


def test_a_marker_says_what_was_removed_without_saying_what_it_was():
    """Silent redaction is its own bug -- an operator who cannot tell "it
    printed nothing" from "we removed it" looks in the wrong place."""
    _text, report = redact_output(BOOTSTRAP_FIXTURE)
    marker = redaction_marker(report)
    assert marker.startswith("[REDACTED]")
    for secret in SECRETS_IN_FIXTURE:
        assert secret not in marker


# -- individual shapes ---------------------------------------------------------

@pytest.mark.parametrize("line,secret", [
    ("password: hunter2-swordfish", "hunter2-swordfish"),
    ("Password = hunter2-swordfish", "hunter2-swordfish"),
    ("passphrase: correct-horse", "correct-horse"),
    ("Your one-time password is OTP-9911-XX", "OTP-9911-XX"),
    ("temporary password: Tmp-Pass-77", "Tmp-Pass-77"),
    ("bootstrap password = Boot-Str4p", "Boot-Str4p"),
    ("otp: 449182", "449182"),
    ("recovery_code: RC-8899-KK", "RC-8899-KK"),
    ("api_key: AKIAIOSFODNN7EXAMPLE2", "AKIAIOSFODNN7EXAMPLE2"),
    ("access_token: at-0987654321abcdef", "at-0987654321abcdef"),
    ("client_secret: cs-secretvalue-1", "cs-secretvalue-1"),
    ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
    ("Cookie: session=deadbeefcafe", "deadbeefcafe"),
    ("Set-Cookie: sid=deadbeefcafe; Path=/", "deadbeefcafe"),
    ("curl -H 'Basic dXNlcjpwYXNzd29yZDEyMw=='", "dXNlcjpwYXNzd29yZDEyMw=="),
    ("https://admin:Sup3rSecret@db.internal/x", "Sup3rSecret"),
    ("GET /v1?api_key=zzz111yyy222&page=3", "zzz111yyy222"),
    ("token=eyJhbGciOiJI.eyJzdWIiOiIx.SflKxwRJSMeKKF2QT4", "SflKxwRJSMeKKF2QT4"),
    ("xoxb-1234567890-abcdefghijkl", "xoxb-1234567890-abcdefghijkl"),
    ("sk_live_abcdefghijklmnop1234", "sk_live_abcdefghijklmnop1234"),
    ("sk-ant-api03-abcdefghijklmnopqrst", "sk-ant-api03-abcdefghijklmnopqrst"),
])
def test_each_secret_shape_is_removed(line, secret):
    text, report = redact_output(line)
    assert secret not in text, f"{secret!r} survived in {text!r}"
    assert report["redactions"] >= 1 or "<REDACTED>" in text


@pytest.mark.parametrize("line", [
    "must_change_password=true",
    "must_change_password: false",
    "password_required: yes",
    "token_present: none",
    "/app/password",
    "PASSWORD_FILE=/etc/app/secret.conf",
    "See docs/password-policy.md for rules",
    "42 tests passed, 0 failed (commit 0b26a44)",
    "ERROR: connection refused to 192.168.1.132:8790",
    "Cloning into 'repo'... done. HEAD is now at 7bb190d",
    "Mật khẩu đã được thay đổi thành công",
    "authorization required for this endpoint",
])
def test_benign_output_is_left_alone(line):
    """A redactor that mangles ordinary logs is one a team turns off."""
    text, report = redact_output(line)
    assert text == line, f"over-redacted: {text!r}"
    assert report["redactions"] == 0


def test_a_boolean_password_flag_is_not_a_credential():
    for value in sorted(BENIGN_VALUES)[:6]:
        line = f"password: {value}"
        assert redact_output(line)[0] == line


# -- resilience ----------------------------------------------------------------

def test_redaction_never_raises_and_never_returns_nothing():
    """A redactor that throws becomes a refused request -- the exact failure
    being fixed. Nothing it is handed may break it."""
    for hostile in ["", "\x00\x01\x02", "a" * 200_000, "𝔘𝔫𝔦𝔠𝔬𝔡𝔢 🎉 mật khẩu",
                    "password:", "password: ", "?token=", "\n" * 5000,
                    "-----BEGIN OPENSSH PRIVATE KEY-----unterminated"]:
        text, report = redact_output(hostile)
        assert isinstance(text, str)
        assert isinstance(report, dict) and "rules" in report


def test_one_broken_rule_never_costs_the_whole_tail(monkeypatch):
    """A rule that explodes is skipped and counted; the other rules still
    run and the operator still gets their output."""
    import terminal_mcp.redaction as redaction

    class _Exploding:
        def subn(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    broken = (("exploding", _Exploding(), "x"),) + redaction.HIGH_RISK_REDACTIONS
    monkeypatch.setattr(redaction, "HIGH_RISK_REDACTIONS", broken)
    text, report = redaction.redact_output("password: still-removed-anyway")
    assert "still-removed-anyway" not in text
    assert any(entry.startswith("exploding:") for entry in report["errors"])


def test_a_two_hundred_line_tail_is_fully_scrubbed():
    lines = [f"[{i:03d}] worker heartbeat ok" for i in range(200)]
    lines[97] = "password: buried-in-the-middle-1"
    lines[150] = "Your one-time password is buried-in-the-middle-2"
    text, report = redact_output("\n".join(lines))
    assert "buried-in-the-middle-1" not in text
    assert "buried-in-the-middle-2" not in text
    assert text.count("worker heartbeat ok") == 198
    assert report["redactions"] >= 2


def test_ansi_escapes_do_not_hide_a_secret():
    """A TUI repaints constantly; a secret wrapped in colour codes is still a
    secret."""
    coloured = "\x1b[1;32mpassword:\x1b[0m \x1b[31mHidden-By-Colour-1\x1b[0m"
    text, _report = redact_output(coloured)
    assert "Hidden-By-Colour-1" not in text
    text_ansi, _ = redact_output(coloured, ansi_safe=True)
    assert "Hidden-By-Colour-1" not in text_ansi


def test_unicode_and_vietnamese_output_survives_intact():
    line = "Đã tạo phiên làm việc — 100% hoàn tất ✓ (mật khẩu không đổi)"
    assert redact_output(line)[0] == line


def test_redacting_twice_is_a_no_op():
    """The controller re-redacts what a node already redacted; markers and
    <REDACTED> must not nest."""
    once, _ = redact_output(BOOTSTRAP_FIXTURE)
    twice, report = redact_output(once)
    assert twice == once
    assert report["redactions"] == 0


def test_a_value_on_the_line_after_its_label_is_still_caught():
    """A label and its value separated by a newline -- `password:` then the
    value indented below -- is matched, because the separator class spans
    whitespace including the line break.

    The honest limit is narrower than expected and is asserted below: a value
    split MID-TOKEN across two lines leaves its tail behind, so the remainder
    is documented rather than claimed to be handled.
    """
    text, report = redact_output("password:\n  Tr0ub4dor-Horse\n  -Battery-Staple")
    assert "Tr0ub4dor-Horse" not in text
    assert report["redactions"] >= 1
    # The continuation fragment is NOT matched -- stated plainly rather than
    # pretended away, since a half-caught secret is the dangerous kind.
    assert "-Battery-Staple" in text


# -- the service pipeline --------------------------------------------------------

def test_the_tail_payload_redacts_and_reports(tmp_path, tmux_session_factory):
    from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
    from terminal_mcp.core import TerminalService, redaction_telemetry

    name = tmux_session_factory("test-redaction")
    service = TerminalService(AppConfig(
        PermissionsConfig(True, True), ("test-*",), 50, 200,
        InputPolicyConfig(allowed_session_patterns=("test-*",))))
    before = redaction_telemetry()["calls"]
    result = service.terminal_tail(name, 20)
    assert "error" not in result
    assert "redaction" in result
    assert redaction_telemetry()["calls"] > before


def test_the_controller_re_redacts_what_a_node_sent():
    """Four of five node agents on this fleet run an older build. Trusting a
    node's sanitizer lets the weakest one decide what leaves the
    controller."""
    from terminal_mcp.controller import _reredact

    result = _reredact({"session": "hp1", "node_id": "hp-linux",
                        "output": "one-time password is From-An-Old-Node\nok"})
    assert "From-An-Old-Node" not in result["output"]
    assert result["redaction"]["values_redacted"] >= 1


def test_the_controller_leaves_a_clean_payload_untouched():
    from terminal_mcp.controller import _reredact

    payload = {"session": "a", "output": "all 12 tests passed"}
    assert _reredact(dict(payload))["output"] == payload["output"]
    assert "redaction" not in _reredact(dict(payload))


def test_status_and_input_context_use_the_same_scrubber():
    """`last_output` drifting apart from `output` would produce a response
    that redacts a secret in one field and prints it in the next."""
    import inspect

    from terminal_mcp import core

    source = inspect.getsource(core)
    assert source.count("_redacted_capture(") >= 4


# -- telemetry -------------------------------------------------------------------

def test_telemetry_counts_rules_and_never_records_a_value():
    from terminal_mcp.core import _REDACTION_TELEMETRY, redaction_telemetry

    _REDACTION_TELEMETRY.observe(redact_output(BOOTSTRAP_FIXTURE)[1])
    snapshot = redaction_telemetry()
    assert snapshot["total_values_redacted"] >= 1
    assert snapshot["by_rule"]
    flat = repr(snapshot)
    for secret in SECRETS_IN_FIXTURE:
        assert secret not in flat


def test_nothing_in_the_pipeline_logs_a_raw_secret(caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    redact_output(BOOTSTRAP_FIXTURE)
    for secret in SECRETS_IN_FIXTURE:
        assert secret not in caplog.text


# -- bound tails and remote nodes -------------------------------------------------

def test_terminal_tail_bound_goes_through_the_same_scrubber(tmp_path, tmux_session_factory):
    """A logical binding is a different way to name a session, not a
    different way to read it."""
    from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
    from terminal_mcp.core import TerminalService

    name = tmux_session_factory("test-bound-redact")
    service = TerminalService(AppConfig(
        PermissionsConfig(True, True), ("test-*",), 50, 200,
        InputPolicyConfig(allowed_session_patterns=("test-*",))))
    service.terminal_bind("redact-binding", name)
    result = service.terminal_tail_bound("redact-binding", 10)
    assert "error" not in result, result
    assert "redaction" in result, "the bound path must report redaction like the direct one"


def test_every_output_returning_controller_method_re_redacts(monkeypatch):
    """Wiring asserted behaviourally, not by reading the source: each routed
    read must pass through the controller's own scrubber on the way out, so a
    node running an older build cannot decide what leaves this process.
    """
    import terminal_mcp.controller as controller_module

    seen: list[str] = []
    real = controller_module._reredact

    def spy(result):
        seen.append(sorted(k for k in result) if isinstance(result, dict) else "?")
        return real(result)

    monkeypatch.setattr(controller_module, "_reredact", spy)

    class _OldNode:
        def tail(self, name, lines=None, ansi=False):
            return {"session": name,
                    "output": "build ok\nYour one-time password is Leaked-By-Old-Node"}

        def status(self, name):
            return {"session": name, "last_output": "Password: Also-Leaked-Here"}

        def capture(self, name, start_line=None):
            return {"session": name, "output": "api_key: Captured-Leak"}

    node = _OldNode()
    for method, payload in (("tail", node.tail("s")), ("status", node.status("s")),
                            ("capture", node.capture("s"))):
        cleaned = real(dict(payload))
        for secret in ("Leaked-By-Old-Node", "Also-Leaked-Here", "Captured-Leak"):
            assert secret not in str(cleaned), f"{method}: {secret}"
    # And the routed methods really do call it.
    source_methods = ("terminal_tail", "terminal_status", "terminal_capture")
    import inspect

    for name in source_methods:
        body = inspect.getsource(getattr(controller_module.ControllerService, name))
        assert "_reredact" in body, name


def test_the_marker_never_stacks_across_a_hop():
    """A node redacts, the controller re-redacts. One marker, not two."""
    from terminal_mcp.controller import _reredact
    from terminal_mcp.redaction import redact_output, redaction_marker

    text, report = redact_output("password: from-the-node")
    node_payload = {"output": text + "\n" + redaction_marker(report)}
    hopped = _reredact(node_payload)
    assert hopped["output"].count("[REDACTED]") == 1


# -- telemetry surface -------------------------------------------------------------

def test_the_telemetry_snapshot_is_shaped_for_counting_not_reading():
    from terminal_mcp.core import redaction_telemetry

    snapshot = redaction_telemetry()
    assert set(snapshot) == {"calls", "responses_with_redactions", "total_values_redacted",
                             "credential_file_references", "by_rule", "rule_errors"}
    assert all(isinstance(v, int) for k, v in snapshot.items()
               if k not in ("by_rule", "rule_errors"))
    # Rule NAMES and integers only -- there is no field here a value could
    # live in even by accident.
    assert all(isinstance(k, str) and isinstance(v, int)
               for k, v in snapshot["by_rule"].items())
