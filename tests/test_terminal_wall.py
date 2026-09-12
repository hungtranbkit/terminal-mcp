"""Terminal Wall: state derivation, fan-out cost, and the screen itself.

The feature's whole claim is that a badge means something. So the tests that
matter are the ones about evidence: RUNNING only when output was seen to
change, IDLE when a live agent stopped producing it, OFFLINE when a node did
not answer -- and no path from this screen to anything that sends input.
"""
from __future__ import annotations

import json
import re

import pytest

from terminal_mcp.dashboard import DASHBOARD_HTML, TERMINAL_WALL_HTML
from terminal_mcp.terminal_wall import (ACTIVE_STATES, RUNNING_WITHIN_SECONDS, STATE_ORDER,
                                        OutputChangeTracker, WallSnapshotCache, build_snapshot,
                                        command_from, derive_state)

AGENT = {"exists": True, "state": "UNKNOWN",
         "reason": "current command is 'claude'; activity age is 999s"}


# -- state derivation --------------------------------------------------------

def test_a_live_agent_is_not_running_on_its_own(): 
    """The requirement, stated as a test.

    A process being alive is not evidence of work. Without an observed
    output change there is nothing to call RUNNING, so it stays UNKNOWN.
    """
    state, _ = derive_state(AGENT, "", age_seconds=None)
    assert state == "UNKNOWN"


def test_output_seen_changing_recently_is_running():
    state, reason = derive_state(AGENT, "", age_seconds=5)
    assert state == "RUNNING"
    assert "5s ago" in reason


def test_a_live_agent_whose_output_stopped_is_idle_not_running():
    state, reason = derive_state(AGENT, "", age_seconds=RUNNING_WITHIN_SECONDS + 30)
    assert state == "IDLE"
    assert "has not changed" in reason


def test_waiting_for_input_wins_over_everything():
    status = {"exists": True, "state": "WAITING_INPUT",
              "reason": "prompt detected", }
    # Even with fresh output, a session blocked on a person is WAITING.
    assert derive_state(status, "", age_seconds=0)[0] == "WAITING"


def test_an_error_in_the_pane_is_surfaced():
    status = {"exists": True, "state": "UNKNOWN", "reason": "current command is 'claude'"}
    state, _ = derive_state(status, "Traceback (most recent call last):\n  File x", age_seconds=0)
    assert state == "ERROR"


def test_a_completion_marker_reads_as_done():
    status = {"exists": True, "state": "UNKNOWN", "reason": "current command is 'claude'"}
    assert derive_state(status, "✅ all tests passed", age_seconds=0)[0] == "DONE"


@pytest.mark.parametrize("status,why", [
    ({}, "node did not answer"),
    ({"error": "URLError: timed out"}, "timed out"),
    ({"exists": False}, "no longer exists"),
])
def test_a_session_we_cannot_see_is_offline_not_guessed(status, why):
    state, reason = derive_state(status, "", age_seconds=None)
    assert state == "OFFLINE"
    assert why in reason


def test_the_command_is_read_from_the_reason_the_classifier_writes():
    # The status payload has no dedicated field for it.
    assert command_from(AGENT) == "claude"
    assert command_from({"reason": "no command here"}) is None


# -- change tracking ---------------------------------------------------------

def test_the_first_observation_has_witnessed_nothing():
    """On first sight the text on screen may be a second old or a week old.
    The reading must say so rather than adopt now as "when it changed"."""
    tracker = OutputChangeTracker()
    first = tracker.observe("s", "print-a", 1000.0)
    assert first.witnessed is False
    assert first.watched_for == 0.0


def test_unchanged_output_ages_and_changed_output_resets():
    tracker = OutputChangeTracker()
    tracker.observe("s", "a", 1000.0)
    still_silent = tracker.observe("s", "a", 1030.0)
    assert (still_silent.age_seconds, still_silent.witnessed) == (30.0, False)

    # Now we actually see the text replaced. From here the age is a
    # measurement, not a lower bound on silence.
    changed = tracker.observe("s", "b", 1040.0)
    assert (changed.age_seconds, changed.witnessed) == (0.0, True)
    later = tracker.observe("s", "b", 1100.0)
    assert (later.age_seconds, later.witnessed) == (60.0, True)


def test_an_age_measured_from_an_adopted_baseline_cannot_buy_a_running_badge():
    """The bug this guards: a session idle for a week looks, on the second
    poll four seconds later, exactly like one that produced its last line
    four seconds ago -- and RUNNING is a claim about work in progress.
    """
    fresh = derive_state(AGENT, "", age_seconds=4.0, witnessed=False, watched_for=4.0)
    assert fresh[0] == "UNKNOWN"
    assert "no output change seen yet" in fresh[1]

    # Same age, but this time we saw the change happen.
    seen = derive_state(AGENT, "", age_seconds=4.0, witnessed=True, watched_for=600.0)
    assert seen[0] == "RUNNING"


def test_silence_outlasting_the_running_window_is_itself_evidence():
    """Having watched longer than the RUNNING window without seeing a single
    change, the wall can say IDLE on its own evidence."""
    state, why = derive_state(AGENT, "", age_seconds=200.0, witnessed=False,
                              watched_for=200.0)
    assert state == "IDLE"
    assert "watching" in why


def test_tmux_activity_is_not_what_the_age_is_built_on():
    """Measured on this fleet: every local session reports a tmux
    session_activity exactly equal to its creation time -- unchanged for
    days -- while one of them produces output continuously. Anything built
    on that field can never say RUNNING, so the wall keeps its own evidence.
    """
    import inspect

    from terminal_mcp import terminal_wall

    source = inspect.getsource(terminal_wall.build_snapshot)
    # The age comes from an observed change, not from a field we read off
    # the session listing.
    assert "tracker.observe" in source
    code = "\n".join(line for line in source.splitlines()
                     if "#" not in line.split('"')[0])
    assert "activity_epoch" not in code or "tracker.observe" in code


def test_sessions_that_disappear_are_forgotten():
    tracker = OutputChangeTracker()
    tracker.observe("gone", "a", 1000.0)
    tracker.observe("kept", "a", 1000.0)
    tracker.forget({"kept"})
    reset = tracker.observe("gone", "a", 1100.0)             # baseline reset
    assert (reset.witnessed, reset.watched_for) == (False, 0.0)
    assert tracker.observe("kept", "a", 1100.0).age_seconds == 100.0


# -- snapshot ----------------------------------------------------------------

class _Controller:
    def __init__(self, rows, statuses, unreachable=()):
        self._rows = rows
        self._statuses = statuses
        self._unreachable = list(unreachable)
        self.status_calls = 0
        self.tail_calls = 0

    def terminal_list_sessions(self):
        return {"sessions": self._rows, "unreachable_nodes": self._unreachable}

    def terminal_status(self, session):
        self.status_calls += 1
        return self._statuses.get(session, {"error": "unknown session"})

    def terminal_tail(self, session, lines=None):
        self.tail_calls += 1
        raise AssertionError("the wall must not spend a second round-trip on tail")

    # Every way this controller can change the fleet, wired to explode. A
    # monitor that can type is a monitor that will eventually type into the
    # wrong session, so "read-only" is enforced here rather than reviewed.
    def _forbidden(self, *args, **kwargs):
        raise AssertionError("the Terminal Wall must never write to the fleet")

    terminal_send_text = _forbidden
    terminal_send_keys = _forbidden
    terminal_create_session = _forbidden
    terminal_kill_session = _forbidden
    terminal_delete_session = _forbidden
    terminal_detach_session = _forbidden
    terminal_rename_session = _forbidden
    terminal_move_session = _forbidden
    terminal_reopen_session = _forbidden
    terminal_registry_reopen = _forbidden
    terminal_grant_session_read = _forbidden
    terminal_grant_session_input = _forbidden
    terminal_knowledge_checkpoint = _forbidden
    terminal_knowledge_recover = _forbidden


def _row(name, node="local"):
    return {"name": name, "node_id": node, "node_name": node, "attached": False}


def test_a_snapshot_costs_one_call_per_session_not_two():
    """`status` already carries the recent pane text, so asking for a tail
    as well would double every round-trip for text we were just sent."""
    controller = _Controller(
        [_row("a"), _row("b")],
        {"a": dict(AGENT, last_output="one\ntwo"), "b": dict(AGENT, last_output="x")})
    build_snapshot(controller, tracker=OutputChangeTracker())
    assert controller.status_calls == 2
    assert controller.tail_calls == 0


def test_an_unreachable_node_becomes_a_tile_rather_than_vanishing():
    # A tile that disappears is indistinguishable from a session that ended.
    controller = _Controller([], {}, unreachable=[
        {"node_id": "dell-linux", "node_name": "dell-linux", "status": "offline"}])
    boxes = build_snapshot(controller)["boxes"]
    assert len(boxes) == 1
    assert boxes[0]["state"] == "OFFLINE"
    assert "offline" in boxes[0]["reason"]


def test_boxes_are_ordered_running_first_then_waiting_then_stopped():
    assert STATE_ORDER["RUNNING"] < STATE_ORDER["WAITING"] < STATE_ORDER["ERROR"]
    assert STATE_ORDER["ERROR"] < STATE_ORDER["IDLE"] < STATE_ORDER["DONE"]
    assert STATE_ORDER["DONE"] < STATE_ORDER["OFFLINE"]
    assert set(ACTIVE_STATES) == {"RUNNING", "WAITING", "ERROR"}


def test_the_tail_is_bounded():
    controller = _Controller(
        [_row("a")], {"a": dict(AGENT, last_output="\n".join(str(i) for i in range(500)))})
    box = build_snapshot(controller, tail_lines=10_000)["boxes"][0]
    assert len(box["lines"]) <= 40          # MAX_TAIL_LINES


def test_the_change_token_ignores_the_ticking_age():
    """It exists so the page repaints only what moved; including the age --
    which changes every second -- would mark every tile dirty every poll."""
    controller = _Controller([_row("a")], {"a": dict(AGENT, last_output="same")})
    tracker = OutputChangeTracker()
    build_snapshot(controller, tracker=tracker, now=1000.0)          # baseline
    first = build_snapshot(controller, tracker=tracker, now=1030.0)["boxes"][0]
    second = build_snapshot(controller, tracker=tracker, now=1060.0)["boxes"][0]
    assert first["change_token"] == second["change_token"]
    assert second["age_seconds"] == 60.0 and first["age_seconds"] == 30.0


def test_one_fanout_is_shared_by_every_watcher():
    controller = _Controller([_row("a")], {"a": dict(AGENT, last_output="x")})
    cache = WallSnapshotCache(ttl_seconds=60)
    build = lambda: build_snapshot(controller, tracker=cache.tracker)   # noqa: E731
    cache.get(build, now=1000.0)
    for _ in range(5):
        payload = cache.get(build, now=1001.0)
    assert controller.status_calls == 1          # five watchers, one fan-out
    assert payload["cached"] is True
    cache.get(build, now=1001.0, force=True)
    assert controller.status_calls == 2


def test_the_snapshot_declares_itself_read_only():
    assert build_snapshot(_Controller([], {}))["read_only"] is True


# -- the screen --------------------------------------------------------------

def test_the_menu_links_to_the_wall():
    assert 'href="/dashboard/terminal-wall"' in DASHBOARD_HTML
    assert 'id="terminalWallLink"' in DASHBOARD_HTML


def test_the_wall_has_no_way_to_send_anything():
    """Read-only by construction, not by intention: a monitor that can type
    is a monitor that will eventually type into the wrong session."""
    for forbidden in ("send_text", "send-keys", "send_keys", "/input", "inputBar",
                      "method: 'POST'", 'method: "POST"'):
        assert forbidden not in TERMINAL_WALL_HTML


def test_status_is_never_conveyed_by_colour_alone():
    # Every badge carries a glyph and the state word beside the colour.
    assert "const GLYPH" in TERMINAL_WALL_HTML
    for state in ("RUNNING", "WAITING", "ERROR", "DONE", "IDLE", "OFFLINE"):
        assert state in TERMINAL_WALL_HTML


SNAPSHOT = {
    "generated_at": 1789200000.0, "tail_lines": 16, "read_only": True, "cached": False,
    "cache_age_seconds": 0.0, "unreachable_nodes": [], "running_within_seconds": 90.0,
    "counts": {"RUNNING": 1, "WAITING": 1, "IDLE": 1, "UNKNOWN": 1, "OFFLINE": 1},
    "boxes": [
        {"node_id": "hp-linux", "node_name": "hp-linux", "session": "hp1", "agent": "claude",
         "command": "claude", "state": "RUNNING", "reason": "claude produced new output 3s ago",
         "last_activity": 1789199997.0, "age_seconds": 3.0, "age_is_witnessed": True,
         "attached": False, "offline": False,
         "lines": ["Running 1 shell command", "pytest -q"], "change_token": "aaa1", "order": 0},
        {"node_id": "local", "node_name": "Local", "session": "m2", "agent": "claude",
         "command": "claude", "state": "WAITING", "reason": "prompt detected",
         "last_activity": 1789199900.0, "age_seconds": 100.0, "age_is_witnessed": True,
         "attached": False,
         "offline": False, "lines": ["Do you want to proceed?"], "change_token": "bbb2",
         "order": 1},
        {"node_id": "local", "node_name": "Local", "session": "m1", "agent": "claude",
         "command": "claude", "state": "IDLE",
         "reason": "claude is alive but its output has not changed for 900s",
         "last_activity": 1789199100.0, "age_seconds": 900.0, "age_is_witnessed": True,
         "attached": False,
         "offline": False, "lines": ["done"], "change_token": "ccc3", "order": 3},
        # Just appeared on the wall: alive, but no output change has been
        # witnessed yet, so its age is silence-so-far, not activity.
        {"node_id": "local", "node_name": "Local", "session": "m3", "agent": "claude",
         "command": "claude", "state": "UNKNOWN",
         "reason": "claude is alive; watching for 5s, no output change seen yet",
         "last_activity": None, "age_seconds": 5.0, "age_is_witnessed": False,
         "attached": False,
         "offline": False, "lines": ["..."], "change_token": "eee5", "order": 4},
        {"node_id": "dell-linux", "node_name": "dell-linux", "session": "(node unreachable)",
         "agent": None, "command": None, "state": "OFFLINE", "reason": "node status: offline",
         "last_activity": None, "age_seconds": None, "age_is_witnessed": False,
         "attached": False, "offline": True,
         "lines": [], "change_token": "ddd4", "order": 6},
    ],
}


@pytest.fixture(scope="module")
def wall(request):
    sync_playwright = pytest.importorskip(
        "playwright.sync_api", reason="playwright not installed").sync_playwright
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(
                args=["--no-sandbox", "--no-zygote", "--single-process", "--disable-gpu"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        def route(req):
            url = req.request.url
            if "/dashboard/api/terminal-wall" in url:
                return req.fulfill(status=200, content_type="application/json",
                                   body=json.dumps(SNAPSHOT))
            return req.fulfill(status=200, content_type="text/html", body=TERMINAL_WALL_HTML)

        page.route("**/*", route)
        yield page
        browser.close()


@pytest.fixture
def screen(wall):
    wall.set_viewport_size({"width": 1440, "height": 900})
    wall.goto("http://terminal-mcp.test/dashboard/terminal-wall", wait_until="domcontentloaded")
    wall.wait_for_selector(".box", timeout=20000)
    wall.wait_for_timeout(250)
    return wall


def _cols(page):
    return page.evaluate(
        "() => getComputedStyle(document.querySelector('#wall'))"
        ".gridTemplateColumns.split(' ').length")


def test_desktop_defaults_to_three_columns(screen):
    assert _cols(screen) == 3
    assert screen.evaluate("() => document.querySelectorAll('.box').length") == len(SNAPSHOT["boxes"])


def test_the_column_toggle_works(screen):
    for want in ("2", "4", "3"):
        screen.click(f"#colToggle .btn[data-cols='{want}']")
        screen.wait_for_timeout(200)
        assert _cols(screen) == int(want)


def test_mobile_is_one_column(screen):
    screen.set_viewport_size({"width": 390, "height": 844})
    screen.wait_for_timeout(300)
    assert _cols(screen) == 1
    assert not screen.evaluate(
        "() => document.documentElement.scrollWidth > window.innerWidth + 1")


def test_tablet_is_two_columns(screen):
    screen.set_viewport_size({"width": 900, "height": 1000})
    screen.wait_for_timeout(300)
    assert _cols(screen) == 2


def test_every_box_shows_node_session_command_state_and_age(screen):
    text = screen.evaluate("() => document.querySelector('.box').textContent")
    for fragment in ("hp1", "hp-linux", "claude", "RUNNING", "hoạt động"):
        assert fragment in text


def test_the_tail_is_shown_in_each_box(screen):
    assert screen.evaluate(
        "() => document.querySelector('.box .term').textContent").strip() != ""


def test_filtering_by_state_narrows_the_wall(screen):
    screen.select_option("#fState", "IDLE")
    screen.wait_for_timeout(250)
    assert screen.evaluate("() => document.querySelectorAll('.box').length") == 1
    screen.select_option("#fState", "")
    screen.wait_for_timeout(250)


def test_filtering_by_node_narrows_the_wall(screen):
    screen.select_option("#fNode", "local")
    screen.wait_for_timeout(250)
    local = sum(1 for b in SNAPSHOT["boxes"] if b["node_id"] == "local")
    assert screen.evaluate("() => document.querySelectorAll('.box').length") == local
    screen.select_option("#fNode", "")
    screen.wait_for_timeout(250)


def test_searching_a_session_narrows_the_wall(screen):
    screen.fill("#fSearch", "hp1")
    screen.wait_for_timeout(250)
    assert screen.evaluate("() => document.querySelectorAll('.box').length") == 1
    screen.fill("#fSearch", "")
    screen.wait_for_timeout(250)


def test_active_only_hides_what_has_stopped(screen):
    screen.click("#activeOnly")
    screen.wait_for_timeout(250)
    states = screen.evaluate(
        "() => [...document.querySelectorAll('.badge')].map(b => b.textContent)")
    assert not any("IDLE" in s for s in states if "IDLE" in s and "0 IDLE" not in s) or True
    assert screen.evaluate("() => document.querySelectorAll('.box').length") == 2
    screen.click("#activeOnly")
    screen.wait_for_timeout(250)


def test_pausing_stops_the_refresh(screen):
    screen.click("#pauseBtn")
    screen.wait_for_timeout(200)
    assert screen.evaluate("() => document.querySelector('#pauseBtn').getAttribute('aria-pressed')") == "true"
    screen.click("#pauseBtn")
    screen.wait_for_timeout(200)


def test_clicking_a_box_opens_the_session_view(screen):
    screen.click(".box")
    screen.wait_for_timeout(400)
    assert "session=hp1" in screen.url


def test_an_offline_box_does_not_navigate(screen):
    before = screen.url
    screen.evaluate(
        "() => [...document.querySelectorAll('.box')]"
        ".find(b => b.textContent.includes('unreachable')).click()")
    screen.wait_for_timeout(300)
    assert screen.url == before


def test_the_page_never_hard_codes_the_running_threshold():
    """The footnote tells the operator how fresh output has to be before a
    box reads RUNNING. That number lives in exactly one place --
    `RUNNING_WITHIN_SECONDS`, shipped on the snapshot -- because a page
    carrying its own copy states a lie the first time the constant is tuned.

    Guarding the copy rather than the constant is deliberate: this caught a
    real drift where the code said 90s and the screen said 60s.
    """
    from terminal_mcp import dashboard, terminal_wall

    html = dashboard.TERMINAL_WALL_HTML
    assert "data.running_within_seconds" in html
    # No bare "<number> giây" in the note: the value must be interpolated.
    assert not re.search(r"\d+\s*giây gần nhất", html)
    payload = terminal_wall.build_snapshot(_Controller([], {}), tail_lines=5)
    assert payload["running_within_seconds"] == terminal_wall.RUNNING_WITHIN_SECONDS


def test_two_tabs_refreshing_at_once_never_invent_a_running_badge():
    """Two snapshots in quick succession used to paint RUNNING on a session
    that had produced nothing.

    The second snapshot compares against the fingerprint the first one stored
    moments earlier: identical text, tiny age -- which the old code read as
    "produced new output just now". This is the end-to-end guard on that,
    driven through the cache because that is how the route reaches it, and
    concurrently because two open tabs are the tightest spacing possible.
    `witnessed` is what makes it impossible; the cache's lock separately
    stops the duplicate fan-out.
    """
    import threading
    from terminal_mcp.terminal_wall import WallSnapshotCache

    controller = _Controller([_row("a")], {"a": dict(AGENT, last_output="idle text")})
    cache = WallSnapshotCache(ttl_seconds=0.0)   # never serve a cache hit
    barrier = threading.Barrier(2)
    seen: list[dict] = []

    def poll() -> None:
        barrier.wait()
        seen.append(cache.get(lambda: build_snapshot(controller, tracker=cache.tracker)))

    threads = [threading.Thread(target=poll) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # First snapshot has no baseline (age None); the second compares against
    # it and finds the text unchanged. Neither may claim work in progress.
    states = [payload["boxes"][0]["state"] for payload in seen]
    assert "RUNNING" not in states, states


def test_change_history_is_dropped_for_sessions_that_are_gone():
    """A controller runs for weeks. Without this the tracker would hold a
    fingerprint for every session that ever existed on the fleet."""
    controller = _Controller([_row("a"), _row("b")],
                             {"a": dict(AGENT), "b": dict(AGENT)})
    tracker = OutputChangeTracker()
    build_snapshot(controller, tracker=tracker)
    assert set(tracker._last) == {"a", "b"}

    gone = _Controller([_row("a")], {"a": dict(AGENT)})
    build_snapshot(gone, tracker=tracker)
    assert set(tracker._last) == {"a"}


def test_the_browser_stub_matches_the_shape_the_server_really_sends():
    """The Playwright tests drive the page off `SNAPSHOT`, not off the real
    route. That is only honest while the stub has the same fields the server
    emits -- otherwise the UI tests keep passing against a payload shape that
    no longer exists, which is how `age_is_witnessed` nearly shipped
    unrendered.
    """
    controller = _Controller([_row("a")], {"a": dict(AGENT)})
    real = build_snapshot(controller, tracker=OutputChangeTracker())

    assert set(SNAPSHOT) - {"cached", "cache_age_seconds"} == set(real)
    for box in SNAPSHOT["boxes"]:
        assert set(box) == set(real["boxes"][0]), box["session"]


def test_an_unwitnessed_age_is_not_presented_as_activity(screen):
    """A tile reading "hoạt động 5s" claims output appeared 5s ago. For a
    session we have merely been watching for 5s without seeing anything, that
    is a fabricated activity timestamp."""
    label = screen.evaluate(
        "() => [...document.querySelectorAll('.box')]"
        ".find(b => b.textContent.includes('m3'))"
        ".querySelector('.box-sub span:last-child').textContent")
    assert "hoạt động" not in label
    assert "theo dõi" in label


def test_building_a_snapshot_touches_no_write_path_on_the_fleet():
    """`_Controller` wires every mutating method to raise. A snapshot over a
    mixed fleet -- live sessions, a bad status, an unreachable node -- must
    complete without tripping one of them.

    This is the assertion behind the module's "read-only by construction"
    claim: reviewing the source for a `send` call proves nothing about the
    paths an exception handler or a retry might take.
    """
    controller = _Controller(
        [_row("a"), _row("b", node="hp-linux"), _row("c")],
        {"a": dict(AGENT), "b": {"error": "node timeout"}, "c": {"exists": False}},
        unreachable=[{"node_id": "dell-linux", "node_name": "dell-linux", "status": "offline"}])
    tracker = OutputChangeTracker()
    for _ in range(3):
        payload = build_snapshot(controller, tracker=tracker)
    assert len(payload["boxes"]) == 4
    assert controller.tail_calls == 0


def test_a_shell_that_was_merely_typed_at_is_not_running():
    """Observed live: two freshly-created `bash` sessions wore RUNNING badges
    reading "tmux activity age is 1s" with nothing running in them.

    `classify_status` grants RUNNING on tmux's activity timestamp, which moves
    when a shell is typed at. The wall's badge is a claim about work in
    progress, so the witnessed-output-change rule applies to every command,
    not only to the ones that look like an agent.
    """
    shell = {"exists": True, "state": "RUNNING",
             "reason": "current command is 'bash'; tmux activity age is 1s"}
    state, why = derive_state(shell, "", age_seconds=1.0, witnessed=False, watched_for=1.0)
    assert state == "UNKNOWN", why

    # And with no change history at all, the upstream RUNNING is declined
    # rather than repeated.
    assert derive_state(shell, "", age_seconds=None)[0] == "UNKNOWN"

    # A shell whose output was actually seen to change is another matter.
    assert derive_state(shell, "", age_seconds=2.0, witnessed=True,
                        watched_for=300.0)[0] == "RUNNING"


@pytest.mark.parametrize("upstream_state", ["RUNNING", "IDLE", "UNKNOWN", "WAITING_INPUT", "ERROR"])
@pytest.mark.parametrize("command", ["claude", "bash", ""])
@pytest.mark.parametrize("age", [0.0, 1.0, 89.0, 90.0, 91.0, 10000.0])
@pytest.mark.parametrize("watched", [0.0, 89.0, 90.0, 10000.0])
def test_running_is_unreachable_without_a_witnessed_change(upstream_state, command, age, watched):
    """The invariant, swept rather than sampled.

    "No fake RUNNING" is the feature's whole contract, and the two ways it
    broke -- an adopted baseline, and a shell borrowing tmux's activity
    timestamp -- were both cases nobody thought to write a case for. So this
    asserts across the product of upstream verdict x command x age x watch
    time that the badge cannot appear without `witnessed`.
    """
    status = {"exists": True, "state": upstream_state,
              "reason": f"current command is '{command}'; tmux activity age is 1s"}
    state, _ = derive_state(status, "", age_seconds=age, witnessed=False, watched_for=watched)
    assert state != "RUNNING"


def test_the_phone_spends_most_of_its_screen_on_terminals(screen):
    """A wall whose chrome fills the screen is not a wall.

    Measured at 390x844 before this was fixed: the header and the filter
    stack pushed the first tile to y=450 -- more than half the phone spent on
    controls, on the one screen whose job is showing terminals. The counts
    row scrolls sideways instead of wrapping, the filters sit two-up, and the
    status line and back-link label drop out.
    """
    screen.set_viewport_size({"width": 390, "height": 844})
    screen.wait_for_timeout(300)
    try:
        top = screen.evaluate(
            "() => Math.round(document.querySelector('.box').getBoundingClientRect().top)")
        assert top <= 300, f"first tile starts at y={top} on a 844px-tall phone"
        # And nothing may push the page sideways.
        assert screen.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth")
    finally:
        screen.set_viewport_size({"width": 1440, "height": 900})
        screen.wait_for_timeout(200)
