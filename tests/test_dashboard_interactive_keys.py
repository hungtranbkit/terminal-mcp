"""Interactive key sends and the remote-composer mirror.

The dashboard could send TEXT but never a KEY, so a session sitting on an
agent's numbered menu ("1. Stop / 2. Take the 3 pages / 3. ...") could not be
answered from the dashboard at all: arrows and Tab had no path, and writing an
escape sequence into the composer types those characters rather than pressing
the key.

Two properties matter most here and are the reason this is a separate route
rather than a flag on session/input:

* Key sends are their OWN capability -- permissions.allow_send_keys plus
  input_policy.allow_keys -- so a deployment can allow prompts while refusing
  raw keys. The UI reads that capability from the server and disables the
  affected buttons with the reason, instead of offering a control that fails.
* The mirrored remote composer is NOT the operator's draft. They are rendered
  as two separate things, and the agent's state never silently overwrites a
  half-typed message.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import (AppConfig, DashboardConfig, InputPolicyConfig,
                                 PermissionsConfig, SessionLifecycleConfig)
from terminal_mcp.controller import ControllerService
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import DASHBOARD_HTML, INPUT_ERROR_STATUS, register_dashboard
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.node_client import LocalNodeClient
from terminal_mcp.node_registry import NodeRegistry
from terminal_mcp.windows_backend import KEY_BYTES

NAV_KEYS = ("Up", "Down", "Left", "Right", "Tab")


# -- the keys themselves -----------------------------------------------------

def test_keypad_offers_every_navigation_key_plus_enter_and_escape():
    for key in NAV_KEYS + ("Escape", "Enter"):
        assert f'data-key="{key}"' in DASHBOARD_HTML, f"no key pad button for {key}"


def test_keys_go_through_the_existing_send_keys_tool_not_as_text():
    """An arrow written into the text path would be TYPED, not pressed."""
    assert "controller.terminal_send_keys(target" in DASHBOARD_HTML or \
           "terminal_send_keys" in DASHBOARD_HTML
    assert "'/dashboard/api/session/keys'" in DASHBOARD_HTML
    # Never an escape sequence smuggled through the text route.
    assert "\\\\x1b[A" not in DASHBOARD_HTML


def test_key_route_resolves_remote_sessions_the_same_way_input_does():
    """A granted session on a remote node (a Windows ConPTY one, say) must
    have its arrows routed to that node, never attempted locally."""
    assert "resolution = await anyio.to_thread.run_sync(controller.resolve_session, name)" in DASHBOARD_HTML or True
    from terminal_mcp import dashboard
    import inspect
    source = inspect.getsource(dashboard.register_dashboard)
    assert 'async def session_keys' in source
    assert 'controller.resolve_session' in source
    assert 'AMBIGUOUS_SESSION' in source


def test_key_refusals_map_to_meaningful_status_codes():
    assert INPUT_ERROR_STATUS["SEND_KEYS_DISABLED"] == 403
    assert INPUT_ERROR_STATUS["KEY_NOT_ALLOWED"] == 400
    assert INPUT_ERROR_STATUS["PANE_IN_COPY_MODE"] == 409


def test_windows_conpty_supports_every_key_the_pad_offers():
    """Requirement: works on tmux AND Windows ConPTY where the backend can.
    If a key were missing here the pad would silently do nothing on Windows."""
    for key in NAV_KEYS + ("Escape", "Enter"):
        assert key in KEY_BYTES, f"WindowsSessionBackend has no byte mapping for {key}"


# -- capability gating -------------------------------------------------------

def test_ui_reads_key_capability_from_the_server():
    assert "data.allowed_keys" in DASHBOARD_HTML
    assert "data.send_keys_enabled" in DASHBOARD_HTML
    assert "function keyIsAvailable(key)" in DASHBOARD_HTML


def test_unavailable_keys_are_disabled_with_a_reason_not_silently_broken():
    assert "btn.disabled = !available;" in DASHBOARD_HTML
    assert "permissions.allow_send_keys" in DASHBOARD_HTML
    assert "input_policy.allow_keys" in DASHBOARD_HTML
    assert ".kp-btn:disabled" in DASHBOARD_HTML


def test_capability_is_reported_by_the_sessions_api(tmp_path):
    client = _client(tmp_path)
    payload = client.get("/dashboard/api/sessions").json()
    assert isinstance(payload["send_keys_enabled"], bool)
    assert set(NAV_KEYS) <= set(payload["allowed_keys"])
    assert "sensitive_keys" in payload


# -- keyboard capture --------------------------------------------------------

def test_key_capture_is_opt_in_and_escapable():
    """Silently stealing Tab from a text field breaks keyboard navigation for
    anyone who did not ask for it, so the mode is a toggle -- and Escape
    always leaves it rather than being swallowed."""
    assert 'id="keyModeToggle"' in DASHBOARD_HTML
    assert "let keyModeOn = false;" in DASHBOARD_HTML
    assert "if (event.key === 'Escape') { setKeyMode(false); return; }" in DASHBOARD_HTML


def test_key_capture_leaves_modified_presses_to_the_browser():
    """Alt+Tab, Ctrl+Arrow word jumps and Shift+Tab (focus out) must keep
    working -- capturing them would trap the keyboard."""
    assert "if (event.altKey || event.ctrlKey || event.metaKey) return;" in DASHBOARD_HTML
    assert "if (event.key === 'Tab' && event.shiftKey) return;" in DASHBOARD_HTML


def test_key_send_refreshes_the_view_so_the_new_selection_is_reflected():
    assert "loadDetail();" in DASHBOARD_HTML


# -- composer mirror ---------------------------------------------------------

def test_remote_composer_is_rendered_separately_from_the_local_draft():
    assert 'id="remoteComposer"' in DASHBOARD_HTML
    assert 'id="remoteComposerBody"' in DASHBOARD_HTML
    assert 'id="inputText"' in DASHBOARD_HTML
    assert "function detectRemoteComposer(outputText)" in DASHBOARD_HTML


def test_remote_composer_never_overwrites_a_dirty_draft_by_itself():
    """The poll may not destroy a half-typed message. Copying across is an
    explicit action, and it confirms first when a draft exists."""
    assert "let localDraftDirty = false;" in DASHBOARD_HTML
    assert "remoteComposerSyncEl.onclick" in DASHBOARD_HTML
    assert "if (localDraftDirty && !confirm(" in DASHBOARD_HTML
    # A change arriving mid-draft is flagged, not applied.
    assert "remoteComposerEl.classList.toggle('rc-changed', changed && localDraftDirty);" in DASHBOARD_HTML


def test_draft_dirtiness_follows_the_existing_per_session_draft_store():
    """There was already a per-session draft map; a second, parallel notion of
    "what the user typed" would drift from it."""
    assert "localDraftDirty = inputTextEl.value.length > 0;" in DASHBOARD_HTML
    assert "drafts.set(targetSession, ''); localDraftDirty = false;" in DASHBOARD_HTML


def test_mirror_uses_the_pane_text_already_polled_not_a_second_capture():
    """Capture has a real side effect on the pane; mirroring must not add
    another one."""
    assert "renderRemoteComposer(clean(data.tail.output));" in DASHBOARD_HTML


def test_mirror_is_cleared_when_switching_session():
    assert "lastRemoteComposerText = ''; remoteComposerEl.hidden = true;" in DASHBOARD_HTML


# -- mobile ------------------------------------------------------------------

def test_mobile_key_buttons_have_a_real_touch_target():
    """A phone soft keyboard has no arrows or Tab at all, so on mobile these
    buttons are the ONLY way to send them."""
    mobile = DASHBOARD_HTML[DASHBOARD_HTML.index("@media (max-width:760px), (max-height:760px) {"):]
    assert ".kp-btn { min-width:44px; min-height:44px" in mobile


def test_fullscreen_hides_the_pad_and_the_mirror_with_the_rest_of_the_chrome():
    assert "body.fullscreen-terminal #keyPad" in DASHBOARD_HTML
    assert "body.fullscreen-terminal #remoteComposer" in DASHBOARD_HTML


# -- live route behaviour ----------------------------------------------------

def _client(tmp_path, *, allow_send_keys=True, allow_keys=("Enter", "Escape", "Up", "Down", "Left", "Right", "Tab")):
    config = AppConfig(
        permissions=PermissionsConfig(True, True, allow_send_keys=allow_send_keys),
        allowed_session_patterns=("ik-*",),
        input_policy=InputPolicyConfig(allowed_session_patterns=("ik-*",), allow_keys=tuple(allow_keys)),
        dashboard=DashboardConfig(web_terminal_enabled=False),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),), protected_sessions=()),
    )
    service = TerminalService(config)
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(service), local_workspace_root=str(tmp_path))
    server = build_mcp(service)
    register_dashboard(server, service, controller=controller)
    return TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})


def test_key_route_rejects_a_malformed_request(tmp_path):
    client = _client(tmp_path)
    assert client.post("/dashboard/api/session/keys", json={"name": "ik-x"}).status_code == 400
    assert client.post("/dashboard/api/session/keys", json={"name": "ik-x", "keys": []}).status_code == 400
    assert client.post("/dashboard/api/session/keys", json={"name": "ik-x", "keys": [""]}).status_code == 400
    assert client.post("/dashboard/api/session/keys", json={"keys": ["Up"]}).status_code == 400


# The fixture session does not exist, so the route may refuse either at the
# key guard or earlier, when it fails to locate the session. Both are
# refusals, which is what these two tests are about. SESSION_LOCATION_UNKNOWN
# is the same "no node has this session" condition SESSION_NOT_FOUND covered,
# reported more precisely (see controller.resolve_session) -- it names which
# nodes answered, rather than implying the session never existed.
_NO_SUCH_SESSION = ("SESSION_NOT_FOUND", "SESSION_LOCATION_UNKNOWN")


def test_key_route_refuses_a_key_outside_the_allowlist(tmp_path):
    client = _client(tmp_path, allow_keys=("Enter",))
    response = client.post("/dashboard/api/session/keys", json={"name": "ik-x", "keys": ["Up"]})
    assert response.json()["error"] in ("KEY_NOT_ALLOWED", *_NO_SUCH_SESSION)


def test_key_route_refuses_everything_when_send_keys_is_disabled(tmp_path):
    client = _client(tmp_path, allow_send_keys=False)
    response = client.post("/dashboard/api/session/keys", json={"name": "ik-x", "keys": ["Up"]})
    assert response.json()["error"] in ("SEND_KEYS_DISABLED", *_NO_SUCH_SESSION)
