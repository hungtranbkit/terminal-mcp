# Camofox browser provider (TMCP-CAMOFOX-001, pilot)

An **optional**, stateful browser for agents, backed by a shared
[Camofox](https://github.com/jo-inc/camofox-browser) REST worker. It sits beside
the Phase-1 browser gateway (`docs/browser-gateway.md`) and does not replace it:
`browser_verify`, `browser_screenshot`, `browser_run_task`, `browser_status` and
`browser_stop` remain the default browser and their signatures are unchanged.

| | Phase-1 gateway (default) | Camofox provider (opt-in) |
| --- | --- | --- |
| Engine | local Playwright Chromium, fresh per call | shared Camofox (Firefox) worker on m910 |
| State | none | per-agent tabs until closed or expired |
| Tool | `browser_*` (5 tools + compact actions) | one tool, `camofox_browser` |
| Default | registered (disabled until configured) | **not registered** unless `TERMINAL_MCP_CAMOFOX_ENABLED=1` |

## Tool

`camofox_browser(action, agent_id, url, tab_id, ref, selector, text, submit,
direction, amount, offset, allow_mutations)`

| action | needs | notes |
| --- | --- | --- |
| `status` | – | health, auth check, config view, fallback hint |
| `create_tab` | agent_id, url | URL policy + allowlist, ≤ `max_tabs_per_agent` |
| `navigate` | agent_id, tab_id, url | same URL policy |
| `snapshot` | agent_id, tab_id, [offset] | accessibility text with refs (`e12`), scrubbed, ≤ `max_snapshot_chars` |
| `click` | agent_id, tab_id, ref or selector, **allow_mutations** | |
| `type` | agent_id, tab_id, ref or selector, text, [submit], **allow_mutations** | text is not echoed back |
| `scroll` | agent_id, tab_id, [direction, amount] | read-only |
| `screenshot` | agent_id, tab_id | needs `TERMINAL_MCP_CAMOFOX_SCREENSHOTS_ENABLED=1`; returns a local 0600 PNG path |
| `list_tabs` | agent_id | only that agent's tabs |
| `close_tab` | agent_id, tab_id | |
| `close_all` | agent_id | drops the agent's whole browser context |

Every result carries `status` (`OK`/`ERROR`), `engine: camofox`, `action` and
`duration_ms`. Errors carry a stable `error` code and
`fallback: "browser_verify/browser_screenshot remain available"`.

Error codes: `CAMOFOX_DISABLED`, `CAMOFOX_UNAVAILABLE`, `CAMOFOX_TIMEOUT`,
`CAMOFOX_AUTH_FAILED`, `CAMOFOX_KEY_UNAVAILABLE`, `CAMOFOX_TAB_NOT_FOUND`
(404/410; also returned for another agent's tab), `CAMOFOX_LIMIT`,
`CAMOFOX_TAB_LIMIT`, `CAMOFOX_BAD_REQUEST`, `CAMOFOX_BAD_RESPONSE`,
`CAMOFOX_UPSTREAM_ERROR`, `CAMOFOX_DOMAIN_NOT_ALLOWED`,
`CAMOFOX_LANDED_OFF_POLICY`, `CAMOFOX_MUTATION_NOT_ALLOWED`,
`SCREENSHOTS_DISABLED`, plus the gateway's `URL_*` policy codes.

## Configuration (environment; restart the MCP server after changes)

| Variable | Default | |
| --- | --- | --- |
| `TERMINAL_MCP_CAMOFOX_ENABLED` | `0` | registers the tool |
| `TERMINAL_MCP_CAMOFOX_BASE_URL` | `http://127.0.0.1:9377` | must be loopback or a private/Tailscale IP; public hosts are refused |
| `TERMINAL_MCP_CAMOFOX_ACCESS_KEY_FILE` | `~/.config/camofox/access.key` | must be chmod 600 |
| `TERMINAL_MCP_CAMOFOX_ALLOWED_DOMAINS` | `example.com` | comma list; exact host or subdomain |
| `TERMINAL_MCP_CAMOFOX_ALLOW_LOOPBACK_TARGETS` | `0` | |
| `TERMINAL_MCP_CAMOFOX_ALLOW_PRIVATE_TARGETS` | `0` | |
| `TERMINAL_MCP_CAMOFOX_REQUEST_TIMEOUT_SECONDS` | `30` | 2–120 |
| `TERMINAL_MCP_CAMOFOX_RETRIES` | `1` | 0–3, GET/DELETE only |
| `TERMINAL_MCP_CAMOFOX_MAX_TABS_PER_AGENT` | `2` | 1–4 |
| `TERMINAL_MCP_CAMOFOX_MAX_SNAPSHOT_CHARS` | `8000` | 500–50000 |
| `TERMINAL_MCP_CAMOFOX_SCREENSHOTS_ENABLED` | `0` | raw pixels are not redacted |
| `TERMINAL_MCP_CAMOFOX_ARTIFACT_DIR` | `$XDG_STATE_HOME/terminal-mcp/browser-artifacts` | |
| `TERMINAL_MCP_CAMOFOX_USER_PREFIX` | `tmcp` | Camofox userId = `<prefix>-<agent_id>` |

An invalid configuration disables the provider and logs a warning; the server and
the default browser keep working.

## Connectivity

The worker listens on **127.0.0.1:9377 on m910 only**. There is no public tunnel,
proxy or open port.

- Terminal MCP processes on m910: use the default base URL.
- Another node: forward the port over SSH and point the provider at the local end:

  ```bash
  ssh -N -L 127.0.0.1:19377:127.0.0.1:9377 mesflow@<m910-tailscale-ip>
  export TERMINAL_MCP_CAMOFOX_BASE_URL=http://127.0.0.1:19377
  ```

  The access key file must be copied to that node out of band (chmod 600).
  Do not rebind the worker to a non-loopback address.

## Security model

- URL gate: `browser_safety.validate_url` (http/https only, metadata and link-local
  hard block, DNS-resolved loopback/private refusal), then a strict hostname
  allowlist. The worker resolves DNS again, and subresource requests inside the page
  are not filtered by this provider. The allowlist plus the post-navigation check
  (below) bound navigation, not every byte the page loads. Use host egress rules
  if a strict boundary is required.
- Post-navigation check: the tab URL after create/navigate/click/type/snapshot is
  re-validated. A tab that redirected or was clicked off-policy is closed
  (`CAMOFOX_LANDED_OFF_POLICY`).
- Not exposed: `evaluate`, cookie import, uploads, downloads, `extract`, traces,
  `/stop`. On the worker, `CAMOFOX_API_KEY` and `CAMOFOX_ADMIN_KEY` are unset and
  `NODE_ENV=production`, so loopback cookie import is refused and `/stop` answers 403.
- Per-agent isolation is per Camofox userId (separate browser context). The worker
  enforces tab ownership. `agent_id` is supplied by the caller, so this separates
  cooperating agents; it is not an authentication boundary between mutually
  untrusted MCP clients.
- The access key never appears in results, logs or errors (tested).
- No CAPTCHA solving, login automation or private-access features are provided or
  intended. Stop when a page presents a challenge.

## Operations

Worker install, unit, limits and rollback: `deploy/camofox/README.md`.

```bash
systemctl --user status camofox-browser       # on m910
curl -s http://127.0.0.1:9377/health          # no key needed
python3 -I deploy/camofox/pilot_e2e.py        # raw REST E2E (reads key from file)
CAMOFOX_LIVE=1 python -m pytest tests/test_camofox_provider.py -k live
```

**Rollback of the provider:** unset `TERMINAL_MCP_CAMOFOX_ENABLED` (or set `0`) and
restart the MCP server. The tool disappears and nothing else changes. Reverting
the code is a single commit revert (new module, one import and a 5-line
registration block in `mcp_app.py`).

## Limits (pilot)

- The full MCP surface only. The compact `terminal_turn` surface and the ChatGPT
  sidecar are unchanged and do not expose Camofox.
- The worker on m910 is capped at 2 sessions × 2 tabs, 4 tabs global, 1 concurrent
  request per user, 1.4 GB cgroup memory and 150% CPU. Expect `CAMOFOX_LIMIT`
  under contention.
- Upstream v1.18.1 `scroll` (mouse wheel) hangs until its 30 s handler timeout when
  the page cannot scroll (e.g. example.com) or after an input has focus; the result
  is `CAMOFOX_UPSTREAM_ERROR` with a `hint`. Scroll before typing. In the pilot it
  also hung intermittently on a long page after another tab's screenshot timed out.
- Upstream screenshots intermittently time out after 30 s ("waiting for fonts to
  load"): 1 of 5 consecutive raw pilot runs on example.com (evidence). Screenshots
  stay off by default; treat `CAMOFOX_UPSTREAM_ERROR` as retry-later or fall back
  to `browser_screenshot`.
- Tabs idle for 3 minutes and sessions idle for 10 minutes are reaped by the worker. The
  browser process stops after 2 idle minutes and relaunches on demand (about 2 s).
