# Browser gateway (Phase 1) — TMCP-BROWSER-GATEWAY-001

Terminal MCP stays the **only** control plane for ChatGPT. This feature adds a
browser to that control plane without adding a second one: four declarative
tools, no raw Python, no raw shell, no pile of low-level CDP verbs.

```
ChatGPT / Claude / Codex
        │  (MCP)
        ▼
Terminal MCP  ──►  browser gateway  ──►  node (Phase 1: the local node)
                    browser_plan.py        │
                    (validate + policy)    ▼
                                      Browser Use / Browser Harness CLI
                                           │  (CDP, loopback only)
                                           ▼
                                      managed Chrome (dedicated profile)
```

The managed browser is **dedicated**. It has its own user-data-dir and its own
debugging port and is launched by the gateway. It never attaches to whatever
Chrome the operator happens to be running — on dell-linux another project
drives a logged-in profile on `:9333`, and "found a browser" would mean driving
someone's real session.

## Tools

| Tool | What it does |
| --- | --- |
| `terminal_browser_status` | Capability/version/health; with `job_id`, the result of an earlier PENDING job |
| `terminal_browser_verify` | Run one declarative plan; returns `PASS \| FAIL \| PENDING \| ERROR` |
| `terminal_browser_screenshot` | Capture one page at one viewport (default 1348x768) |
| `terminal_browser_stop` | Release the managed browser |

### Reaching it from ChatGPT

ChatGPT is served by `chatgpt_sidecar`, whose catalog is a single tool
(`terminal_turn`) so that one orchestration step is one "Called tool" row. The
browser is therefore also an **action** on that tool, routed to the very same
functions the standalone tools are registered from:

```
terminal_turn(action="verify", url="https://…", viewport={"width":1348,"height":768},
              steps=[{"op":"assert_text","selector":"h1","contains":"…"}])
terminal_turn(action="screenshot", url="https://…")
terminal_turn(action="browser_status", job_id="…")   # alias: "browser"
terminal_turn(action="browser_stop")
```

Aliases: `verify` → `browser_verify`, `screenshot` → `browser_screenshot`,
`browser` → `browser_status`. A build with no gateway wired answers
`ACTION_UNAVAILABLE` rather than silently doing nothing.

There is deliberately **no** `browser_exec`, `browser_js` or `browser_cdp`.
`tests/test_browser_gateway.py::test_the_browser_surface_is_exactly_four_declarative_tools`
pins this; adding a fifth tool should be a security review, not a convenience.

### A 1348x768 verification

```json
{
  "url": "https://example.com/",
  "viewport": {"width": 1348, "height": 768},
  "allow_mutations": true,
  "screenshot": "on_failure",
  "steps": [
    {"op": "assert_text", "selector": "h1", "contains": "Example Domain"},
    {"op": "fill",  "selector": "#qty", "value": "12.5"},
    {"op": "click", "selector": "#apply"},
    {"op": "assert_value", "selector": "#qty", "equals": "12.5"},
    {"op": "assert_text",  "selector": "#total", "equals": "25.00"},
    {"op": "assert_url",   "contains": "/cart"}
  ]
}
```

Returns, compactly:

```json
{"status": "PASS", "job_id": "…", "node": "dell-linux", "summary": "6/6 checks passed",
 "mutations": ["fill", "click"], "elapsed_ms": 4120, "artifact": "…/verify-….png"}
```

### Decimal-quantity regression

The shape worth copying: a decimal that silently becomes an integer between the
input and the computed total is invisible to an HTTP check and obvious to a DOM
assertion.

```json
{"steps": [
  {"op": "fill", "selector": "#qty", "value": "0.25"},
  {"op": "click", "selector": "#apply"},
  {"op": "assert_value", "selector": "#qty",   "equals": "0.25"},
  {"op": "assert_text",  "selector": "#total", "equals": "0.50"}
]}
```

`assert_value` reads the input back; `assert_text` reads what the page
computed. A rounding bug fails one of them, never both.

## The vocabulary

`navigate`, `click`, `fill`, `press`, `wait`, `assert_text`, `assert_value`,
`assert_visible`, `assert_url`. Everything is bounded: ≤40 steps, ≤256-char
selectors, ≤4096-char values, ≤30s per wait, ≤300s per plan, viewport within
320x240 … 3840x2160.

- **At least one `assert_*` is required.** A plan that asserts nothing would
  report PASS forever.
- **`click`/`fill`/`press` need `allow_mutations: true`** and are echoed back in
  the result. A chat client should not click a button on a logged-in page as a
  side effect of what reads like a health check.
- **`press` keys are an allowlist** (`Enter`, `Tab`, `Escape`, arrows, …).

## Security

| Rule | Where |
| --- | --- |
| http/https only; `file:`/`chrome:`/`javascript:`/`data:`/`blob:`/`about:`/`view-source:` rejected by name | `browser_plan.validate_url` |
| SSRF: private, loopback, link-local and unique-local blocked by default; public names that *resolve* into those ranges blocked too | same |
| Cloud metadata (`169.254.169.254`, `*.internal`) blocked even when allowlisted | same |
| Local dev targets reachable only via an explicit operator allowlist | `TERMINAL_MCP_BROWSER_ALLOW_HOSTS` |
| Caller data never becomes code — the plan is JSON on disk, read by a static executor; selectors cross into JS as `json.dumps` literals | `browser_exec.py` |
| Screenshots can be *named* but never *placed*; the path is re-checked for containment | `browser_gateway.artifact_path` |
| No secrets in results/logs — every free-text field is redacted and bounded; filled values are never echoed | `browser_gateway._clip` |
| Child processes get a minimal env (no node tokens, no API keys) | `browser_runner._child_env` |
| Recording **off** unless an operator sets `TERMINAL_MCP_BROWSER_ALLOW_RECORDING=1` | `browser_runner.recording_enabled` |

## Latency contract

Sync calls are bounded: 30s default, 45s hard ceiling. A longer plan is not
failed and not abandoned — it returns `PENDING` with a resume handle:

```json
{"status": "PENDING", "job_id": "ab12…",
 "resume": {"job_id": "ab12…", "tool": "terminal_browser_status"}}
```

## Multi-node

Capability is **probed, never declared** (`capability_probe.py`, capability name
`browser-harness`), like every other node capability.

- Explicit `node` wins; otherwise a named `session` pins the plan to its own
  node; auto-selection happens only when neither was given.
- A named node that cannot run the plan returns a typed `BROWSER_UNAVAILABLE`
  with a `reason` (`node_capability_missing`, `remote_execution_unsupported`,
  `no_eligible_node`) — never a silent hop to a different host.

**Phase 1 executes on the local node.** Remote execution needs a browser
endpoint on the node agent; that is Phase 2 and deliberately not a fleet/RPC
redesign here.

## Install / upgrade

```bash
scripts/provision-browser-harness.sh          # install or upgrade
scripts/provision-browser-harness.sh --check  # report only
```

Installs the official `browser-use` distribution (Browser Use CLI 3.x is
Browser Harness-backed) into an isolated uv venv on Python 3.12, at
`~/.local/share/terminal-mcp/browser-harness`, and writes `manifest.json` with
the resolved version. The heavy browser stack deliberately stays out of the
terminal-mcp service venv and off `PATH`; the gateway resolves the venv itself.

The Browser Use **WebUI is not installed** — it is a second control plane.

Verified on dell-linux: `browser-use 0.13.10`, `browser-harness 0.1.13`,
Python 3.12.8.

## Doctor / troubleshooting

```bash
terminal_browser_status()                        # from the MCP client
~/.local/share/terminal-mcp/browser-harness/venv/bin/browser-harness doctor --json
~/.local/share/terminal-mcp/browser-harness/venv/bin/browser-harness recordings
```

**Readiness is not liveness.** A browser whose renderer cannot start still binds
the debugging port, still answers `/json/version`, and still returns a result
for `Page.navigate` — and then every assertion hangs. The gateway therefore
probes readiness by *evaluating an expression*
(`LocalBrowserRunner.renderer_ready`) before it trusts a browser.

### Known host defect on dell-linux (why the docker fallback exists)

A shell-launched Chrome on this box answers CDP but its renderer never
executes: `Runtime.evaluate` hangs, `--dump-dom` hangs. Reproduced identically
with `--headless=new`, under Xvfb, with `--single-process`, with the system
Chrome **and** with a user-owned Playwright Chromium; no AppArmor denials are
logged for it. The same Chromium inside
`mcr.microsoft.com/playwright:v1.63.0-noble` on `--network host` evaluates
fine, which is what the fallback uses.

`TERMINAL_MCP_BROWSER_LAUNCH` pins the strategy:

| Value | Behaviour |
| --- | --- |
| `auto` (default) | reuse a *ready* browser, else host, else docker |
| `host` | host browser only |
| `docker` | container only (what dell-linux ends up using) |
| `external` | attach to a browser an operator already runs; never launches one |

Debugging port collisions are the other trap: a stale browser holding the port
makes every relaunch fail to bind while `/json/version` keeps answering, so it
looks alive and is not. `ensure_browser` stops a not-ready browser before
retrying rather than driving it.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TERMINAL_MCP_BROWSER_HOME` | `~/.local/share/terminal-mcp/browser-harness` | provisioned venv, profile, manifest |
| `TERMINAL_MCP_BROWSER_ARTIFACT_DIR` | `$XDG_STATE_HOME/terminal-mcp/browser-artifacts` | screenshots |
| `TERMINAL_MCP_BROWSER_CDP_PORT` | `9444` | managed browser's loopback CDP port |
| `TERMINAL_MCP_BROWSER_ALLOW_HOSTS` | *(empty)* | `host` / `host:port`, comma-separated |
| `TERMINAL_MCP_BROWSER_ALLOW_PRIVATE` | off | allow all private ranges (blunt; prefer the allowlist) |
| `TERMINAL_MCP_BROWSER_ALLOW_RECORDING` | off | permit Browser Harness recording |
| `TERMINAL_MCP_BROWSER_LAUNCH` | `auto` | launch strategy |
| `TERMINAL_MCP_BROWSER_DOCKER_IMAGE` | `mcr.microsoft.com/playwright:v1.63.0-noble` | fallback image |
| `TERMINAL_MCP_BROWSER_CHROME` | *(probed)* | explicit browser binary |
| `TERMINAL_MCP_BROWSER_CHROME_FLAGS` | *(empty)* | extra flags for the host strategy |

## Privacy

Recording is off by default and the gateway never enables it for a caller. The
managed profile is dedicated and disposable — no operator profile, no cookies,
no logged-in session is reused. Screenshots stay in the gateway's own artifact
directory. Filled values are never echoed back, and `secret: true` marks a
field as sensitive for the caller's own audit trail.

## Tests

```bash
pytest tests/test_browser_gateway.py            # 99 tests, no browser needed
pytest -m browser_smoke tests/test_browser_smoke.py   # real Chrome
```

The smoke suite serves its own page on loopback, asserts the page measured
itself at 1348x768, exercises fill/click/DOM assertions and a screenshot,
proves a wrong assertion really FAILs, and loads a credential-free NovaRetail
preview read-only if one is running.

## Rollback

The feature is additive and inert when unused:

1. `terminal_browser_stop()` — release the managed browser.
2. Remove `browser-harness` from the node (`rm -rf ~/.local/share/terminal-mcp/browser-harness`).
   `terminal_browser_status` then answers `DEGRADED` and every plan returns a
   typed error; nothing else in Terminal MCP is affected.
3. To remove the tools entirely, revert the `feat/browser-gateway-phase1` merge.
