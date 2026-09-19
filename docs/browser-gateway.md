# Browser gateway (TMCP-BROWSER-GATEWAY-001)

Phase 1 verifies web UIs with local Playwright and Chromium. It uses deterministic
assertions and task steps; it needs no LLM, Browser Use service, or model API key.
The gateway is disabled by default.

## Architecture

The MCP server registers `browser_verify`, `browser_run_task`, `browser_status`,
`browser_screenshot` and `browser_stop`. The compact `terminal_turn` surface
dispatches the same five action names to the same handlers. The ChatGPT sidecar
still publishes only `terminal_turn`, so that surface is never weaker than the
full one.

`browser_stop` releases browser work still in flight. Each run already closes its
own browser, so on an idle gateway it reports `IDLE` and touches nothing; it
exists for what the per-call deadline cannot cover -- a worker whose caller went
away, or a Chromium that outlived its job. It takes no arguments and only ever
touches worker process groups this gateway started.

Each verification, task, or screenshot starts a Python worker with a fresh
Chromium context on the MCP server's host. A local dev URL therefore refers to
that host, not the caller's computer or a remote terminal node.

| Component | Responsibility |
| --- | --- |
| `browser_gateway.py` | Configuration, job construction, process deadline, result shaping, artifacts and recent runs |
| `browser_worker.py` | Playwright launch, request interception, actions and observations |
| `browser_network.py` | Per-worker loopback proxy, DNS validation and connections to validated numeric addresses |
| `browser_safety.py` | URL policy, shared redaction rules and output bounds |
| `browser_script.py` | Assertion/task grammar and deterministic evaluation |

The parent sends one JSON job through stdin; the worker emits one JSON result
on stdout and diagnostics on stderr. The parent judges observations and enforces
a wall-clock deadline independently of Playwright's per-operation timeout. It
cleans up the worker process group, including browser descendants, on completion
or failure. Combined stdout/stderr is limited to 256 KiB.

## Installation

From the checkout, using the Python environment that runs the MCP server:

```bash
python -m pip install -e '.[browser]'
python -m playwright install chromium
```

On Linux hosts missing Chromium system libraries, the operator can install them
with `python -m playwright install-deps chromium` (system privileges may be
needed). Install browser binaries for the server's service account. Playwright
is optional and imported only in the worker; ordinary server operation does not
require it. The declared extra is `playwright>=1.48,<2`.

`browser_status` without a probe reports configuration and package availability
without launching Chromium. Package availability does not prove that Chromium
can launch. An explicitly requested `probe: true` launches and closes Chromium;
it does not verify an application page.

## Configuration

Add a top-level `browser` section to the server's `config.yaml`. This example
enables local dev servers while retaining the other conservative defaults:

```yaml
browser:
  enabled: true
  allow_loopback: true
  allow_private_networks: false
  headless: true
  executable: ""                  # Playwright's bundled Chromium
  viewport_width: 1280
  viewport_height: 800
  navigation_timeout_seconds: 20
  hard_timeout_seconds: 25
  allow_url_patterns: []
  deny_url_patterns: []
  screenshots_enabled: false
  artifact_dir: ""
  keep_artifacts: 50
  ignore_https_errors: false
  browser_args: []
```

Every setting has a `TERMINAL_MCP_BROWSER_<UPPERCASE_KEY>` environment override,
applied when the gateway is constructed. Restart the server after changing
configuration or its environment. Booleans accept `true`, `false`, `1`, or `0`;
list settings require JSON arrays, for example:

```bash
export TERMINAL_MCP_BROWSER_ENABLED=1
export TERMINAL_MCP_BROWSER_ALLOW_LOOPBACK=true
export TERMINAL_MCP_BROWSER_DENY_URL_PATTERNS='["/admin/delete"]'
```

The hard timeout must exceed the navigation timeout. Configuration accepts
navigation budgets of 1–120 seconds and hard budgets of 2–300 seconds. A call's
`timeout_seconds` can narrow these budgets but cannot enlarge them. Viewport
dimensions are bounded to 200–4096 pixels. Browser timeouts are independent of
terminal waiting timeouts.

## Mutations are opt-in

`click`, `fill` and `press` write to the page. Because this surface is reachable
from a chat client, a task containing any of them is refused with
`BROWSER_MUTATION_NOT_ALLOWED` unless the caller passes `allow_mutations=true`;
the refusal happens before the browser is launched, and it names the step kinds
it refused. "Check that the cart page renders" must not be able to press the
button that empties it.

Read-only steps -- `open`/`goto`, `wait`, `scroll`, and every `assert` -- need no
authorization. An authorized run echoes `"mutations": [...]` back in its result,
so the audit trail lives in the same record the chat already keeps.

```json
{"status": "ERROR", "error": "BROWSER_MUTATION_NOT_ALLOWED",
 "mutating_steps": ["click", "fill"]}
```

## Security and artifacts

Only HTTP and HTTPS targets are accepted. URL credentials, ambiguous URLs,
metadata endpoints and link-local destinations are refused; metadata and
link-local blocks cannot be overridden. Loopback and private networks each
require their own explicit setting. Allow patterns do not grant either range
permission. Public destinations remain eligible when the allow list is empty.

Allow/deny patterns are case-insensitive substrings of the complete URL, not
hostname rules, globs or regular expressions. Deny wins. A substring is not a
strict origin allowlist: it can also match a path or query. Request interception
checks navigations and subresources, while the proxy resolves and validates
addresses before connecting to a numeric address to avoid a second DNS lookup.
Use host-level network restrictions when a strict egress boundary is required.

Contexts block service workers and WebSockets and disable accepted downloads.
The worker disables QUIC and non-proxied WebRTC UDP. Operator browser arguments
are restricted to `--disable-dev-shm-usage`, `--disable-gpu`, and `--no-sandbox`;
remote debugging flags, extensions and arbitrary profile arguments are not
passed through. `--no-sandbox` weakens Chromium isolation and should only be used
where the deployment requires it. TLS verification is enabled unless the
operator sets `ignore_https_errors`.

Text results use the project's redaction rules, strip URL credentials, redact
query values and enforce length/count limits. These rules do not make arbitrary
page content safe to publish. Screenshots contain raw, unredacted pixels and
require `screenshots_enabled: true`. They return a local PNG path, never inline
image bytes or an uploaded image. A screenshot request on a verification/task
does not override the operator setting.

The default artifact directory is
`$XDG_STATE_HOME/terminal-mcp/browser-artifacts`, falling back to
`~/.local/state/terminal-mcp/browser-artifacts`. New directories request mode
0700; existing directory permissions are not repaired. Retention removes older
PNG files from that directory by modification time after successful runs, keeping
`keep_artifacts` files (default 50). Use a dedicated directory: unrelated PNGs in
it are also subject to pruning. A returned path is local to the server and may
later be pruned; `keep_artifacts: 0` removes even the latest capture.

## Examples and result contract

These are JSON argument objects for `terminal_turn`:

```json
{"action":"browser_status"}
```

```json
{"action":"browser_verify","target":"http://127.0.0.1:3000/","args":{"wait_for":"#ready","assertions":["status is: 200","text contains: Ready","selector exists: #ready","no errors"]}}
```

```json
{"action":"browser_run_task","target":"http://127.0.0.1:3000/","text":"fill #search with widgets; click #submit; wait for #results; assert selector text contains: #results :: widgets","args":{"timeout_seconds":15}}
```

After the operator enables screenshots:

```json
{"action":"browser_screenshot","target":"http://127.0.0.1:3000/","args":{"full_page":true}}
```

Direct tool calls use `url` instead of `target` and `task` instead of `text`, with
the contents of `args` passed as named arguments. Compact calls wrap the handler
response in `result`. Unsupported argument keys return `UNKNOWN_ARGS` with
`unknown` and `allowed` lists; a non-object `args` returns `INVALID_ARGS`.
For example, `browser_status` accepts only the `probe` argument.

Verification returns `PASS`, `FAIL`, or `ERROR`, per-check outcomes and bounded
evidence: final URL, HTTP status, title, console/page/network errors and optional
screenshot path. Without assertions, a successful load with status below 400
(or no reported HTTP status) can return `PASS`; this is not evidence of UI
correctness. Add explicit assertions, including `no errors` when appropriate.
Task results use `OK` or `FAILED`, or `ERROR` for gateway/worker failures.
Common errors include `BROWSER_GATEWAY_DISABLED`,
`BROWSER_DEPENDENCY_UNAVAILABLE`, `BROWSER_TIMEOUT`, URL policy errors and
`SCREENSHOTS_DISABLED`.

Assertions support text contains/not contains, title is/contains, URL contains,
HTTP status, selector exists/missing, selector text is/contains, and no
console/page/network errors. Selector text uses `SELECTOR :: EXPECTED`; plain
assertion text means `text contains`.

Tasks accept explicit open/goto, click, fill, press, wait for selector, timed
wait, scroll to bottom, assert and screenshot steps. Separate steps with
semicolons, newlines or `then`; these separators also split quoted values.
Unrecognized task prose is rejected. Actions can change the target application;
use appropriate test data. All assertions and screenshots observe the final
page, even if placed earlier in the task. Execution stops on the first failed
action.

## Phase 1 limitations

- Local Chromium only; no remote CDP endpoint, node routing or Browser Use engine.
  Process-group cleanup uses POSIX facilities; native Windows is not supported
  by this implementation.
- No persistent login/profile/storage state. `session_id` is only an echoed label;
  every call starts a clean context. Login and subsequent actions must occur in
  the same task if required.
- No arbitrary JavaScript tool, autonomous planning, visual assertion engine,
  file upload workflow or download artifacts. WebSocket-dependent applications
  may not work under the network restrictions.
- At most 25 task steps and 50 assertions. Observations are truncated (including
  worker body text at 8000 characters and selector text at 400); they are not a
  complete page dump. Worker error collections cap at 25 per category, so counts
  are not an unlimited audit of page activity.
- Recent-run history is in memory, not a durable audit log. Screenshots are
  best-effort; inspect the returned screenshot field before assuming one exists.

## Phase 2 adapter notes

Phase 2 is not implemented. The engine identifier (`playwright-chromium`) and
worker JSON job/result boundary are the current adapter seams, not a configurable
engine registry. A future Browser Use or remote-browser adapter should retain
the four public tool contracts and the compact dispatch surface.

Keep deterministic assertion evaluation in the parent. An agentic planner could
propose task actions, but its narrative must not become a verification verdict.
Each adapter must preserve URL/DNS policy, deadlines, process/resource cleanup,
bounded and scrubbed observations, screenshot opt-in and explicit state-lifetime
semantics. Model credentials, remote connection authorization, session storage,
additional costs and new network capabilities require explicit configuration
and adapter tests before rollout.


### Inline screenshots in normal ChatGPT

On the compact ChatGPT surface, an explicit `terminal_turn` call with
`action="browser_screenshot"` keeps the normal compact JSON metadata and also
returns the validated PNG as MCP `ImageContent`, so clients that support MCP
images can render the browser view inline. This does **not** add another public
tool: the catalog remains `terminal_turn` only.

The sidecar reads images only from
`$TERMINAL_MCP_BROWSER_ARTIFACT_DIR` (default
`~/.local/state/terminal-mcp/browser-artifacts`), rejects symlink escapes,
non-PNG files and files larger than 8 MiB, and never places base64 image data in
text. Screenshots are still opt-in at the Browser Gateway level with
`screenshots_enabled: true`; raw pixels are not redacted.
