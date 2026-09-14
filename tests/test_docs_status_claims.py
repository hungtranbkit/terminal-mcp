"""Guardrail: a status claim must not outrun its evidence
(docs/AI_ANALYSIS_GATE.md §10a).

Written because this project's own Analysis Gate feature note was
briefly committed as "unit + dogfood verified" BEFORE any test had been
run. That is the same defect class the gate exists to prevent -- a
confident statement ahead of its evidence -- and a rule nobody checks is
a rule that decays, so it is checked here.

SCOPE, disclosed rather than implied: this enforces the ladder only for
the documents that opted into it -- docs/AI_ANALYSIS_GATE.md and the
§20.6 Phase F implementation note. docs/REQUIREMENTS.md carries many
older VERIFIED notes written under earlier conventions; retroactively
failing the build on them would be exactly the kind of sweeping,
unasked-for change this project's own backward-compatibility posture
rejects. New sections can adopt the ladder by using its vocabulary.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_DOC = REPO_ROOT / "docs" / "AI_ANALYSIS_GATE.md"
REQUIREMENTS = REPO_ROOT / "docs" / "REQUIREMENTS.md"

ALLOWED_STATUSES = ("PLANNED", "IMPLEMENTED — UNVERIFIED", "TESTING", "VERIFIED")

#: What counts as recorded evidence sitting WITH a VERIFIED claim. Each
#: is a thing a reader could go and check: a real count, a real command,
#: a real commit, a real date.
EVIDENCE_MARKERS = (
    re.compile(r"\b\d+\s+(?:passed|tests?\s+pass)", re.I),
    re.compile(r"\bpytest\b", re.I),
    re.compile(r"\bcommit\s+[0-9a-f]{7,40}\b", re.I),
    re.compile(r"\b20\d\d-\d\d-\d\d\b"),
)


def has_recorded_evidence(text: str) -> bool:
    """True when `text` contains something a reader could actually go and
    check. Factored out so the guardrail's own logic is testable directly
    rather than only via a skip when the docs happen to be UNVERIFIED."""
    return any(marker.search(text) for marker in EVIDENCE_MARKERS)


def _status_line(text: str) -> str | None:
    match = re.search(r"^\s*Status:\s*\*\*(.+?)\*\*", text, re.M)
    return match.group(1).strip() if match else None


def test_gate_doc_declares_a_status_from_the_allowed_ladder():
    status = _status_line(GATE_DOC.read_text(encoding="utf-8"))
    assert status is not None, "docs/AI_ANALYSIS_GATE.md must declare a Status"
    assert any(status.startswith(allowed) for allowed in ALLOWED_STATUSES), \
        f"status {status!r} is not on the §10a ladder {ALLOWED_STATUSES}"


def _phase_f_note(text: str) -> str:
    start = text.index("#### Phase F implementation note")
    end = text.find("\n### ", start)
    return text[start:end if end != -1 else len(text)]


def test_phase_f_note_declares_a_status_from_the_allowed_ladder():
    note = _phase_f_note(REQUIREMENTS.read_text(encoding="utf-8"))
    match = re.search(r"\*\*Status:\s*(.+?)\.?\*\*", note)
    assert match, "the Phase F note must declare a Status"
    status = match.group(1).strip()
    assert any(status.startswith(allowed) for allowed in ALLOWED_STATUSES), \
        f"status {status!r} is not on the §10a ladder {ALLOWED_STATUSES}"


@pytest.mark.parametrize("name,getter", [
    ("AI_ANALYSIS_GATE.md", lambda: GATE_DOC.read_text(encoding="utf-8")),
    ("REQUIREMENTS.md §20.6 Phase F", lambda: _phase_f_note(REQUIREMENTS.read_text(encoding="utf-8"))),
])
def test_a_verified_claim_must_carry_recorded_evidence(name, getter):
    """The actual guardrail. If either document is upgraded to VERIFIED,
    real evidence must be written down in the same block -- a test count,
    a pytest invocation, a commit, or a date. Downgrading the claim is
    the correct fix when there is none; adding remembered evidence after
    the fact is not."""
    text = getter()
    status = _status_line(text) or ""
    match = re.search(r"\*\*Status:\s*(.+?)\.?\*\*", text)
    claim = status or (match.group(1).strip() if match else "")
    if "VERIFIED" not in claim or "UNVERIFIED" in claim:
        pytest.skip(f"{name} does not currently claim VERIFIED (status: {claim!r})")
    assert has_recorded_evidence(text), (
        f"{name} claims VERIFIED but records no evidence next to the claim -- "
        "see docs/AI_ANALYSIS_GATE.md §10a"
    )


def test_the_ladder_itself_is_documented():
    """The rule and the test must not drift apart."""
    text = GATE_DOC.read_text(encoding="utf-8")
    assert "## 10a. Status claims are themselves evidence claims" in text
    for status in ALLOWED_STATUSES:
        assert status in text, f"{status!r} is enforced here but not documented in §10a"


# -- the guardrail's own logic, proven directly --------------------------

@pytest.mark.parametrize("text", [
    "**Status: VERIFIED.** Everything works great and we are confident.",
    "**Status: VERIFIED.** Unit and dogfood verified.",
    "**Status: VERIFIED.** All tests green.",
])
def test_evidence_check_rejects_a_bare_confident_claim(text):
    """"All tests green" is a claim, not evidence: no count, no command,
    no commit, no date -- nothing a reader can check."""
    assert has_recorded_evidence(text) is False


@pytest.mark.parametrize("text", [
    "**Status: VERIFIED.** 65 passed in tests/test_analysis_gate.py.",
    "**Status: VERIFIED.** Ran `pytest tests/test_analysis_gate.py`.",
    "**Status: VERIFIED.** commit 1a2b3c4d, dogfood task blocked then dispatched.",
    "**Status: VERIFIED** (2026-09-14): real disposable task driven through engine.tick().",
])
def test_evidence_check_accepts_a_checkable_record(text):
    assert has_recorded_evidence(text) is True
