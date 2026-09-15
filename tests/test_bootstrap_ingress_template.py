"""The public bootstrap allowlist, checked as a file rather than trusted.

This template is not applied by anything in this repository -- turning it on
is a deliberate human act. What the repo CAN do is make the allowlist
reviewable: a diff that adds a path here is visible, and a route that should
never be public fails this suite loudly rather than quietly appearing on a
hostname nobody re-read.

The rule these encode: a machine being onboarded has no Cloudflare Access
session and cannot get one, so the three enrollment routes, the one
node-authenticated heartbeat it needs afterwards -- and only those, plus
liveness -- are what a public hostname may carry.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parent.parent / "deploy" / "cloudflare" / \
    "bootstrap-ingress.template.yml"

# Every path the template allows, as a regex the tunnel would apply.
ALLOWED_PATTERNS = (
    r"^/dashboard/api/enroll/(consume|redeem|progress)$",
    r"^/(w|enroll/windows-setup\.ps1)$",
    r"^/dashboard/api/nodes/[A-Za-z0-9_-]{1,64}/agent-bundle$",
    r"^/dashboard/api/nodes/[A-Za-z0-9_-]{1,64}/heartbeat$",
    r"^/health/(live|ready)$",
)

# Paths that must never match. Not an exhaustive list of everything private
# -- an exhaustive list is impossible -- which is why the catch-all 404 is
# tested separately: it is what makes a route added tomorrow private by
# default.
MUST_NOT_MATCH = (
    "/mcp",
    "/mcp/",
    "/dashboard",
    "/dashboard/",
    "/dashboard/nodes",
    "/dashboard/api/nodes/onboard/helper",
    "/dashboard/api/nodes/onboard/helper/windows-x64",
    "/dashboard/api/session/input",
    "/dashboard/api/work/workers",
    "/terminal",
    "/webterm",
    "/supervisor",
    "/login",
    "/app",
    "/health/metrics",
    "/version",
    # Admitting ONE route under /dashboard/api/nodes/ is what makes the
    # rest of that namespace worth spelling out. Every one of these is a
    # sibling of the heartbeat path and none may come along with it.
    "/dashboard/api/nodes",
    "/dashboard/api/nodes/",
    "/dashboard/api/nodes/win-work",
    "/dashboard/api/nodes/win-work/token/refresh",
    "/dashboard/api/nodes/win-work/agent-bundle/../token/refresh",
    "/dashboard/api/nodes/win-work/agent-bundlex",
    "/dashboard/api/nodes/win-work/deregister",
    "/dashboard/api/nodes/win-work/sessions",
    "/dashboard/api/nodes/onboard/enrollments",
    # The operator's own enrollment-minting surface, as opposed to the
    # machine's enrollment-redeeming one.
    "/dashboard/api/nodes/onboard/enrollments/abc/handle",
    # Admitting /w and /enroll/windows-setup.ps1 is what makes their
    # neighbours worth naming. Neither path may become a prefix.
    "/enroll",
    "/enroll/",
    "/enroll/consume",
    "/enroll/windows-setup.ps1.bak",
    "/enroll/../dashboard/api/session/input",
    "/w/",
    "/ws",
    "/well-known",
)


@pytest.fixture(scope="module")
def template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_the_template_exists_and_is_repo_managed(template):
    assert "NOT ACTIVATED" in template
    assert "${BOOTSTRAP_HOSTNAME}" in template


def test_the_template_activates_nothing_by_itself(template):
    """No DNS, no reload, no credential. Checking this in must not turn it
    on."""
    for forbidden in ("cloudflared tunnel run", "systemctl", "dns create",
                      "credentials-file:", "tunnel:"):
        assert forbidden not in template.replace("cloudflared tunnel ingress validate", ""), \
            forbidden


@pytest.mark.parametrize("path", [
    "/w",
    "/enroll/windows-setup.ps1",
    "/dashboard/api/enroll/consume",
    "/dashboard/api/enroll/redeem",
    "/dashboard/api/enroll/progress",
    "/dashboard/api/nodes/win-work/heartbeat",
    "/dashboard/api/nodes/win-work/agent-bundle",
    "/dashboard/api/nodes/w/heartbeat",
    "/dashboard/api/nodes/WIN_work-01/heartbeat",
    "/health/live",
    "/health/ready",
])
def test_the_machine_routes_are_allowed(path):
    assert any(re.match(pattern, path) for pattern in ALLOWED_PATTERNS), path


def test_the_heartbeat_route_is_in_the_template_by_its_exact_path(template):
    """The path and verb are the ones the controller already serves. A
    pattern that merely looks right is how an ingress silently forwards
    nothing."""
    assert "^/dashboard/api/nodes/[A-Za-z0-9_-]{1,64}/heartbeat$" in template


def test_the_heartbeat_pattern_cannot_reach_a_sibling_node_route():
    """The node id class carries no '/' and no '.', so it cannot be used to
    spell a deeper path or a traversal out of the one route admitted."""
    pattern = r"^/dashboard/api/nodes/[A-Za-z0-9_-]{1,64}/heartbeat$"
    for path in ("/dashboard/api/nodes/a/token/refresh",
                 "/dashboard/api/nodes/a/heartbeat/../token/refresh",
                 "/dashboard/api/nodes/../session/input/heartbeat",
                 "/dashboard/api/nodes/a/b/heartbeat",
                 "/dashboard/api/nodes/a/heartbeatx",
                 "/dashboard/api/nodes//heartbeat",
                 "/dashboard/api/nodes/%2e%2e/heartbeat"):
        assert not re.match(pattern, path), path
    # And an id longer than the registry accepts is not admitted either.
    assert not re.match(pattern, "/dashboard/api/nodes/%s/heartbeat" % ("a" * 65))


@pytest.mark.parametrize("path", MUST_NOT_MATCH)
def test_no_operator_or_admin_route_is_exposed(path):
    """The list this feature turns on: /mcp, the Dashboard, terminal content,
    supervisor control and the operator's own auth flow stay private."""
    for pattern in ALLOWED_PATTERNS:
        assert not re.match(pattern, path), f"{path} matched {pattern}"


def test_the_helper_download_is_not_public(template):
    """It stays behind Cloudflare Access on the Dashboard hostname.
    Publishing an unauthenticated binary is a different decision."""
    assert "onboard/helper" not in template.split("TO APPLY")[0].replace(
        "# The helper download is NOT on this hostname", "")
    for pattern in ALLOWED_PATTERNS:
        assert not re.match(pattern, "/dashboard/api/nodes/onboard/helper/windows-x64")


def test_the_patterns_are_anchored_at_both_ends():
    """An unanchored pattern is how /health/live also matches
    /health/liveness-and-everything-else."""
    for pattern in ALLOWED_PATTERNS:
        assert pattern.startswith("^"), pattern
        assert pattern.endswith("$"), pattern


def test_a_path_prefix_cannot_smuggle_a_private_route():
    for path in ("/dashboard/api/enroll/consume/../session/input",
                 "/dashboard/api/enroll/consumex",
                 "/health/liveness"):
        for pattern in ALLOWED_PATTERNS:
            assert not re.match(pattern, path), f"{path} matched {pattern}"


def test_the_template_ends_in_a_catch_all_404(template):
    """The rule that makes the allowlist hold for routes added later: a path
    that matches nothing is refused at the edge, not forwarded."""
    body = template.split("ingress:", 1)[1]
    entries = [line for line in body.splitlines() if line.strip().startswith("- hostname:")]
    assert len(entries) == 6
    assert "http_status:404" in body
    # And the 404 is LAST -- a catch-all above a real rule swallows it.
    assert body.rindex("http_status:404") > body.rindex("path:")


def test_every_allowed_path_in_the_template_is_one_this_suite_knows_about(template):
    """Stops a path being added to the template without being added to the
    MUST_NOT_MATCH reasoning above it."""
    declared = re.findall(r"path:\s*(\S+)", template)
    assert set(declared) == set(ALLOWED_PATTERNS), declared


# ---------------------------------------------------------------------------
# The setup script: the one thing both install paths fetch from here
# ---------------------------------------------------------------------------


def test_the_setup_script_is_reachable_under_both_paths_it_is_served_on(template):
    """The helper GETs /enroll/windows-setup.ps1; the Win + R command GETs
    /w. They are the same bytes from the same route, and BOTH had to be
    allowed -- allowing one would have fixed exactly half the flow."""
    assert r"^/(w|enroll/windows-setup\.ps1)$" in template
    pattern = r"^/(w|enroll/windows-setup\.ps1)$"
    for path in ("/w", "/enroll/windows-setup.ps1"):
        assert re.match(pattern, path), path


def test_the_setup_script_rule_is_not_a_prefix_door():
    """/enroll/... must not become a way to reach anything else added under
    that prefix later."""
    pattern = r"^/(w|enroll/windows-setup\.ps1)$"
    for path in ("/enroll/", "/enroll/consume", "/enroll/windows-setup.ps1.bak",
                 "/enroll/windows-setup.ps1/../../mcp", "/w/", "/ws",
                 "/enroll/windows-setupXps1"):
        assert not re.match(pattern, path), path


def test_the_dot_in_the_script_name_is_escaped():
    """An unescaped '.' would also match /enroll/windows-setupZps1."""
    pattern = r"^/(w|enroll/windows-setup\.ps1)$"
    assert not re.match(pattern, "/enroll/windows-setupZps1")


def test_the_version_query_string_does_not_need_its_own_rule(template):
    """cloudflared matches on PATH, so ?v=1.0.0 rides the same rule -- and
    the rule must not have been widened to try to accommodate it."""
    assert "?v=" not in template
    assert "v=" not in template.split("ingress:", 1)[1]
