"""The real ux-ui-agent-skills kit against the clean and broken fixtures.

Opt-in: needs the pack installed on this node (`terminal-mcp-skill-packs sync`),
node and a browser, and takes about a minute. Run with
TERMINAL_MCP_SKILL_PACK_E2E=1. Skipped otherwise -- a skip here is not a pass.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from terminal_mcp import skill_packs as sp

FIXTURES = Path(__file__).resolve().parent.parent / "examples" / "ux-ui-skill-pack"

pytestmark = pytest.mark.skipif(os.environ.get("TERMINAL_MCP_SKILL_PACK_E2E") != "1",
                                reason="real-kit e2e is opt-in: TERMINAL_MCP_SKILL_PACK_E2E=1")


@pytest.fixture(scope="module")
def ready():
    status = sp.status("ux-ui-agent-skills", verify=True)
    assert status["state"] == sp.READY, status.get("detail")
    return status


def _review(name):
    root = FIXTURES / name
    return sp.review([str(root)], tokens=str(root / "tokens.json"), theme=str(root / "theme.css"),
                     focustrap_open="#open-dialog", component_root=str(root / "spec"), record=False)


def test_real_kit_clean_fixture_passes_every_gate(ready):
    result = _review("clean")
    assert result["verdict"] == sp.PASS, sp.format_review(result)
    assert result["exit_code"] == 0
    assert result["counts"][sp.PASS] == result["catalogue_size"] > 0


def test_real_kit_broken_fixture_fails_with_real_exit_codes(ready):
    result = _review("broken")
    assert result["verdict"] == sp.FAIL and result["exit_code"] == 1
    failed = {name for name, gate in result["gates"].items() if gate["status"] == sp.FAIL}
    # One defect per family planted in the fixture: emoji, hardcodes, a11y, a
    # dangling var(), an incomplete spec, a dead button.
    assert {"check_no_emoji", "lint_hardcodes", "axe_audit", "validate_theme_refs",
            "validate_component_spec", "verify_interactive"} <= failed
    assert all(1 in gate["exit_codes"] for name, gate in result["gates"].items() if name in failed)


def test_real_kit_without_the_modal_trigger_is_incomplete(ready):
    root = FIXTURES / "clean"
    result = sp.review([str(root)], tokens=str(root / "tokens.json"), theme=str(root / "theme.css"),
                       component_root=str(root / "spec"), record=False)
    assert result["verdict"] == sp.INCOMPLETE and result["exit_code"] == 2
    assert result["gates"]["verify_focustrap"]["status"] == sp.NOT_RUN
