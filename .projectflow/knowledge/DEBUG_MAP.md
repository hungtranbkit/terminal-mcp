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
