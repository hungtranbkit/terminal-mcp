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
| `scroll` | agent_id, tab_id, [direction, amount] | read-only; constant `window.scrollBy` template (not the upstream wheel), returns `scrolled`, `position`, or `reason: at_edge_or_not_scrollable` |
| `screenshot` | agent_id, tab_id | needs `TERMINAL_MCP_CAMOFOX_SCREENSHOTS_ENABLED=1`; returns a local 0600 PNG path; 12 s fast-fail with a `fallback_call` to `browser_screenshot` |
| `list_tabs` | agent_id | only that agent's tabs |
| `close_tab` | agent_id, tab_id | |
| `close_all` | agent_id | drops the agent's whole browser context |

Every result carries `status` (`OK`/`ERROR`), `engine: camofox`, `action` and
`duration_ms`. Errors carry a stable `error` code and
`fallback: "browser_verify/browser_screenshot remain available"`.

After a scroll or screenshot times out, upstream keeps that tab locked for about
30 s. The provider then answers calls on that tab with `CAMOFOX_TAB_BUSY` and
`retry_after_seconds` (32) at once instead of queueing behind the lock. A failed
screenshot also carries
`fallback_call: {"tool": "browser_screenshot", "url": <last allowed tab URL>}`.

Error codes: `CAMOFOX_DISABLED`, `CAMOFOX_TAB_BUSY`, `CAMOFOX_UNAVAILABLE`, `CAMOFOX_TIMEOUT`,
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
- Another node (Dell, HP): use the operator-run, default-off SSH forward in
  `deploy/camofox/camofox-tunnel.sh`. It listens on 127.0.0.1 only, sets
  `StrictHostKeyChecking=yes`, and is never enabled at boot unless `--enable` is passed:

  ```bash
  deploy/camofox/camofox-tunnel.sh keygen        # prints a forward-only authorized_keys line
  # owner adds that line on m910 (restrict,port-forwarding,permitopen="127.0.0.1:9377",command="/bin/false")
  deploy/camofox/camofox-tunnel.sh install --host mesflow@<m910-tailscale-ip>
  deploy/camofox/camofox-tunnel.sh start && deploy/camofox/camofox-tunnel.sh smoke
  export TERMINAL_MCP_CAMOFOX_BASE_URL=http://127.0.0.1:19377
  # copy ~/.config/camofox/access.key out of band (chmod 600)
  deploy/camofox/camofox-tunnel.sh stop | uninstall
  ```

  The forward adds reachability, not authority: the worker still requires the
  access key.

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
- Upstream v1.18.1 `/scroll` (mouse wheel) hangs for its 30 s handler timeout on
  about 1 in 4 calls (scrollable or not; always with a focused input), so the
  provider does not use it. `scroll` runs a constant `window.scrollBy(dx, dy)`
  template through the worker's evaluate route. The only inputs are two validated
  integers, there are no input events, and it is not an agent action (agents still
  have no evaluate). JS scrolling fires `scroll` events but not `wheel` events;
  a page that only reacts to wheel input will not lazy-load on it.
- Upstream screenshots intermittently time out ("waiting for fonts to load"):
  1 of 5 raw runs on 2026-10-19. The provider fails fast at 12 s, marks the tab busy and
  returns a `browser_screenshot` fallback. Screenshots stay off by default.
- Tabs idle for 3 minutes and sessions idle for 10 minutes are reaped by the worker. The
  browser process stops after 2 idle minutes and relaunches on demand (about 2 s).
