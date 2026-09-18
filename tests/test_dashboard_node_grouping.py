"""Session lists grouped by node, on both surfaces that list sessions.

Before this, a session's node appeared only as a small per-row badge that was
deliberately hidden for the local node -- so on a fleet the list read as one
flat pile with no indication of where anything actually ran, and the local
node was indistinguishable from "no node information at all".

Two things are worth pinning here beyond the rendering itself:

* ONE grouping implementation. Both pages had already grown their own
  independent node-badge code; a second parallel grouping implementation
  would drift the same way. NODE_GROUP_JS is injected into both.
* No hardcoded node identity. Node ids on this deployment happen to be
  "local"/"dell-5530"/"macbook", and none of them may appear as a literal in
  the grouping logic -- it reads node_id/node_name off the API like any other
  field.

Structural assertions against the rendered HTML/JS, matching this suite's own
convention (see test_dashboard_tab_ux_fixes.py's docstring for why this
project has no browser-automation dependency).
"""
from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import (AppConfig, DashboardConfig, InputPolicyConfig,
                                 PermissionsConfig, SessionLifecycleConfig)
from terminal_mcp.controller import ControllerService
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import DASHBOARD_HTML, NODE_GROUP_JS, SESSIONS_ADMIN_HTML, register_dashboard
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.node_client import LocalNodeClient
from terminal_mcp.node_registry import NodeRegistry

PAGES = {"DASHBOARD_HTML": DASHBOARD_HTML, "SESSIONS_ADMIN_HTML": SESSIONS_ADMIN_HTML}


# -- one shared implementation ----------------------------------------------

@pytest.mark.parametrize("page_name", sorted(PAGES))
def test_shared_grouping_module_is_injected_into_every_session_list(page_name):
    page = PAGES[page_name]
    assert "function buildNodeGroups(rows, nodes, options)" in page
    assert "function makeNodeCollapseStore(pageKey)" in page
    assert "function nodeStatusLabel(status)" in page
    # The marker itself must be consumed, never shipped to a browser.
    assert "__NODE_GROUP_JS__" not in page


def test_grouping_derives_nodes_from_session_rows_when_nodes_payload_missing():
    assert "function mergeNodeSources(rows, nodes)" in NODE_GROUP_JS
    assert "for (const node of mergeNodeSources(rows, nodes))" in NODE_GROUP_JS
    assert "row.node_id || 'local'" in NODE_GROUP_JS


def test_nodes_page_falls_back_from_stale_selected_node_and_merges_sessions():
    from terminal_mcp.dashboard import NODES_ADMIN_HTML
    assert "api('/dashboard/api/sessions')" in NODES_ADMIN_HTML
    assert "if (!selectedNodeId || !known.has(selectedNodeId))" in NODES_ADMIN_HTML
    assert "nodesCache.push({ id, display_name: row.node_name || id" in NODES_ADMIN_HTML


def test_grouping_logic_is_defined_exactly_once_per_page():
    """A second copy pasted into one page is the drift this module exists to
    prevent -- it would pass every other assertion here while silently
    diverging from the other surface."""
    for page in PAGES.values():
        assert page.count("function buildNodeGroups(") == 1
        assert page.count("function makeNodeCollapseStore(") == 1


def test_node_identity_is_never_hardcoded_in_the_grouping_logic():
    for literal in ("'dell-5530'", '"dell-5530"', "'macbook'", '"macbook"',
                    "'dell-linux'", '"dell-linux"', "'m910'", '"m910"'):
        assert literal not in NODE_GROUP_JS, f"grouping logic hardcodes {literal}"
    # "local" appears only as the fallback for a row with no node_id at all --
    # never as a branch that treats the local node as a special case.
    assert NODE_GROUP_JS.count("'local'") == 1
    assert "row.node_id || 'local'" in NODE_GROUP_JS


# -- ordering ----------------------------------------------------------------

def test_nodes_sort_online_then_recent_then_offline():
    assert "const NODE_STATUS_RANK = { online: 0, degraded: 1, offline: 2 };" in NODE_GROUP_JS
    assert "nodeStatusRank(a.status) - nodeStatusRank(b.status)" in NODE_GROUP_JS


def test_unknown_node_status_never_sorts_ahead_of_a_known_online_node():
    """A node the registry has not vouched for must not be presented as more
    available than one it has."""
    assert "return rank === undefined ? NODE_STATUS_RANK.offline : rank;" in NODE_GROUP_JS


def test_degraded_is_surfaced_to_the_operator_as_recent():
    """`degraded` is the registry's word for "heartbeat is stale but not yet
    offline". "recent" is what that actually tells an operator."""
    assert "NODE_STATUS_LABEL = { online: 'online', degraded: 'recent', offline: 'offline' }" in NODE_GROUP_JS


def test_sessions_within_a_node_sort_attention_first_then_activity():
    assert "(b.state === 'WAITING_INPUT') - (a.state === 'WAITING_INPUT')" in NODE_GROUP_JS
    assert "sessionActivityValue(b) - sessionActivityValue(a)" in NODE_GROUP_JS
    # Name last, so the order is stable between polls rather than reshuffling
    # whenever two sessions share a timestamp.
    assert "String(a.name).localeCompare(String(b.name))" in NODE_GROUP_JS


# -- collapse state ----------------------------------------------------------

def test_collapse_state_is_persisted_per_page_and_per_node():
    assert "const storageKey = 'tmNodeCollapse:' + pageKey;" in NODE_GROUP_JS
    assert "makeNodeCollapseStore('dashboard')" in DASHBOARD_HTML
    assert "makeNodeCollapseStore('sessions')" in SESSIONS_ADMIN_HTML


def test_collapse_state_survives_unreadable_localstorage():
    """Safari private mode throws on access. A storage failure must cost the
    remembered collapse state, never the list itself."""
    assert "catch (error) { state = {}; }" in NODE_GROUP_JS
    assert "catch (error) { /* non-fatal */ }" in NODE_GROUP_JS


def test_the_node_holding_the_current_session_is_revealed():
    """A collapsed group must never be able to hide the session being viewed."""
    assert "reveal(nodeId)" in NODE_GROUP_JS
    assert "if (owner) nodeCollapse.reveal(owner.id);" in DASHBOARD_HTML


# -- filtering vs. the empty-node rule ---------------------------------------

def test_online_node_with_no_sessions_still_gets_a_group():
    assert "includeEmptyOnline" in NODE_GROUP_JS
    assert "node.status !== 'online'" in NODE_GROUP_JS
    assert "Node online, chưa có session." in DASHBOARD_HTML
    assert "Node đang online, chưa có session nào." in SESSIONS_ADMIN_HTML


def test_filtering_hides_groups_with_no_matches_on_both_pages():
    # Main dashboard: filter box drives the same grouping call.
    assert "{ includeEmptyOnline: !query }" in DASHBOARD_HTML
    assert 'id="sessionFilter"' in DASHBOARD_HTML
    # Sessions admin: its existing search box + "chỉ hiện session đã khoá".
    assert "{ includeEmptyOnline: !filtering }" in SESSIONS_ADMIN_HTML
    assert "const filtering = Boolean(query) || onlyGrantableEl.checked;" in SESSIONS_ADMIN_HTML


def test_filter_does_not_drop_tabs_for_filtered_out_sessions():
    """A session hidden by the filter still exists; tearing its tab down would
    lose the element identity that keeps clicks from being dropped mid-poll."""
    assert "const currentNames = new Set(rows.map(row => row.name));" in DASHBOARD_HTML


# -- headers -----------------------------------------------------------------

@pytest.mark.parametrize("page_name", sorted(PAGES))
def test_group_header_shows_name_status_and_count(page_name):
    page = PAGES[page_name]
    assert "node-group-name" in page
    assert "node-group-status" in page
    assert "node-group-count" in page
    assert "nodeStatusLabel(group.status)" in page


@pytest.mark.parametrize("page_name", sorted(PAGES))
def test_group_header_is_a_real_button_with_expanded_state(page_name):
    """Collapse must be reachable by keyboard and announced, not a div that
    only responds to a mouse."""
    page = PAGES[page_name]
    assert "node-group-toggle" in page
    assert "aria-expanded" in page


def test_node_id_is_shown_when_it_differs_from_the_display_name():
    """The id is what every API call and CLI command actually takes."""
    assert "group.id === group.name ? '' : group.id" in DASHBOARD_HTML
    assert "if (group.id !== group.name) idEl.textContent = group.id;" in SESSIONS_ADMIN_HTML


# -- mobile ------------------------------------------------------------------

def test_mobile_group_header_is_compact_with_a_real_tap_target():
    mobile = DASHBOARD_HTML[DASHBOARD_HTML.index("@media (max-width:760px), (max-height:760px) {"):]
    assert ".node-group-toggle { padding:7px 10px; min-height:40px" in mobile
    assert ".node-group-id { display:none }" in mobile


def test_mobile_filter_input_holds_the_ios_zoom_threshold():
    """iOS Safari zooms the whole page for any input under 16px, which breaks
    the fixed app shell -- the same floor the output search input already
    holds."""
    mobile = DASHBOARD_HTML[DASHBOARD_HTML.index("@media (max-width:760px), (max-height:760px) {"):]
    assert ".tabbar-filter input { font-size:16px" in mobile


def test_fullscreen_still_hides_the_whole_strip():
    """Grouping must not have cost the existing fullscreen behaviour."""
    assert "body.fullscreen-terminal .tabbar" in DASHBOARD_HTML


# -- API contract the grouping depends on ------------------------------------

def _client(tmp_path):
    config = AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("ng-*",),
        input_policy=InputPolicyConfig(allowed_session_patterns=("ng-*",)),
        dashboard=DashboardConfig(web_terminal_enabled=False),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),), protected_sessions=()),
    )
    service = TerminalService(config)
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(service), local_workspace_root=str(tmp_path))
    server = build_mcp(service)
    register_dashboard(server, service, controller=controller)
    return TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})


def test_nodes_api_exposes_the_fields_group_headers_render(tmp_path):
    """Grouping reads id/display_name/status off this route -- never derives
    a label from the id, and never assumes a node it has not seen."""
    client = _client(tmp_path)
    response = client.get("/dashboard/api/nodes")
    assert response.status_code == 200
    nodes = response.json()["nodes"]
    assert nodes, "the local node must always be registered"
    for node in nodes:
        assert set(("id", "display_name", "status")) <= set(node)
        assert node["status"] in ("online", "degraded", "offline")


def test_both_session_list_pages_render(tmp_path):
    """Smoke: a template that fails to build serves a 500, and every
    assertion above would still pass against the unrendered constant."""
    client = _client(tmp_path)
    for path in ("/dashboard", "/dashboard/sessions"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "buildNodeGroups" in response.text, path
        assert "__NODE_GROUP_JS__" not in response.text, path


# -- optional access locks (default-open model) ------------------------------

def test_the_access_column_shows_effective_state_with_two_optional_locks():
    """Access is open by default, so this column answers "what is true now?"
    and offers two switches to turn it OFF. It used to report the static name
    allowlist beside an effective state that could disagree with it -- the
    contradiction that made a perfectly usable session look forbidden."""
    assert "access-locks" in SESSIONS_ADMIN_HTML
    assert "lock-toggle" in SESSIONS_ADMIN_HTML
    assert "const readOn = row.effective_read !== false;" in SESSIONS_ADMIN_HTML
    assert "makeLock('Xem'" in SESSIONS_ADMIN_HTML
    assert "makeLock('Gửi'" in SESSIONS_ADMIN_HTML


def test_a_lock_defaults_to_on_and_reverts_if_the_server_refuses():
    """The checkbox reflects EFFECTIVE access, so it starts ON. If the server
    refuses the change it snaps back -- the UI must never show a lock state
    the server did not actually apply."""
    assert "box.checked = on;" in SESSIONS_ADMIN_HTML
    assert "if (!ok) box.checked = on;" in SESSIONS_ADMIN_HTML


def test_locking_a_remote_session_is_qualified_to_its_home_node():
    """A session on another node is locked on the node that OWNS its grants,
    not against the local store."""
    assert "const name = (row.node_id && row.node_id !== 'local') ? `${row.node_id}/${row.name}` : row.name;" \
        in SESSIONS_ADMIN_HTML


def test_bulk_by_node_reuses_the_existing_selection_bar():
    """Bulk-by-node without a second mechanism: the group header selects that
    node's sessions into the bulk bar that already knows how to apply a
    preset."""
    assert "node-select-all" in SESSIONS_ADMIN_HTML
    assert "Chọn cả node" in SESSIONS_ADMIN_HTML
    assert "event.stopPropagation();" in SESSIONS_ADMIN_HTML   # must not toggle collapse


def test_every_row_can_have_its_access_changed():
    """`grantable` used to mean "outside the whitelist". Under default-open
    that reads false for every ACCESSIBLE session, which would have hidden the
    controls from exactly the rows an operator wants to lock."""
    for page in (DASHBOARD_HTML, SESSIONS_ADMIN_HTML):
        assert "function grantable(row) { return true; }" in page
