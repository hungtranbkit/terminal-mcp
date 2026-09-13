# Debug Map

## Work UI (`dashboard.py`)
Entry points: `load()`, `loadContext()`, the per-page `$('#...').onclick`
handlers. Search keywords: `WORK_HTML`, `renderWorks`, `createCard`.

**Past fix:** a JS block landed in `TERMINAL_WALL_HTML` because
`$('#refreshBtn').onclick` is NOT unique across templates. Anchor edits on a
string that exists in exactly one template (`$('#liveBtn').onclick` for Work)
and assert the other templates stayed clean afterwards.

**Past fix:** browser tests timed out on `.card` because the hidden
`#createCard` matched first. Wait on `#works .card`.

## Queue completion
Search keywords: `COMPLETION_MARKER_RE`, `worker_output_after_prompt`.

**Past fix (critical):** a worker's echo of the completion instruction was
accepted as evidence of completion, so a `sleep` worker reached COMPLETED. The
fix anchors on the instruction SENTENCE and skips our own marker by shape. An
earlier attempt using `attempt_count + 1` was silently inert at verify time.

## Terminal Wall states
Search keywords: `derive_state`, `witnessed`, `RUNNING_WITHIN_SECONDS`.

**Past fix:** RUNNING was reported from an unchanged fingerprint. RUNNING now
requires a WITNESSED output change within the window, for every command. tmux
`session_activity` is dead on this fleet; the wall keeps its own history.

## Auth status
Search keywords: `auth_status_for_node`, `UNKNOWN_STALE`.

**Past fix:** AUTHENTICATED was reported from a 6.8-hour-old cache.

## Fleet peers
**Past fix:** peers failed with ECONNRESET rather than 404 because agents reset
mid-body on a 363-object export. `probe()` sends an empty batch first.

## MCP tunnel split-brain (2026-09-13)

Search keywords: `tunnel-client`, `tunnel_id`, `client_instance_id`,
`terminal-mcp-tunnel`, `ExecCondition`.

**Symptom:** the session list flickered between full and empty, and
`terminal_send_text` returned SESSION_NOT_FOUND immediately after
`terminal_input_context` had found that same session.

**Cause:** TWO `tunnel-client` processes served ONE OpenAI tunnel id
(m910 and dell-linux). Requests alternated between two controllers with
different views: m910 saw the whole fleet, dell-linux's fallback
controller knew only itself. Not a cache bug inside one process -- two
hosts answering one tunnel.

**How to recognise it again:** compare `client_instance_id` in the
tunnel-client logs on each host. One tunnel id must have exactly one
instance. `tunnel_restart_count` climbing in the watchdog log while the
unit keeps stopping is the other tell.

**Fix:** `ExecCondition=` guard on dell-linux's tunnel unit
(`terminal-mcp-tunnel-guard condition`) so the fallback only takes the
tunnel when m910 is unreachable, plus a 60s `enforce` timer that stops a
duplicate if a boot race started one anyway. ExecCondition rather than
ExecStartPre: a non-zero exit SKIPS the unit instead of failing it, so
`Restart=always` does not fight the guard.
