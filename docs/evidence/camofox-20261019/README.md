# Camofox pilot evidence, m910, 2026-10-19

## Provisioning

- Upstream `jo-inc/camofox-browser` v1.18.1, commit `39c82094013480b373df6600d44c7f036f58356e`, MIT.
- `npm ci --omit=dev --ignore-scripts`: 173 packages. Camoufox `152.0.4-beta.30`,
  `camoufox-bin` sha256 `955c053a91494bcdd4ac4b9df03f25fdb99d985a0feb8f814b39289a2b54cdaa`.
- Host before install: 16 cores, disk 65 GB free, RAM ~3 GB available, swap ~99% used.
- Docker unusable (account not in the `docker` group, no sudo), so the worker runs as a systemd --user
  unit with cgroup limits (`deploy/camofox/camofox-browser.service`).
- `ss -ltn`: `127.0.0.1:9377` only. Key at `~/.config/camofox/access.key` (600) and an env file (600).
  Crash reports off, no proxy, no VNC, `CAMOFOX_API_KEY`/`CAMOFOX_ADMIN_KEY` unset,
  `NODE_ENV=production`; youtube, persistence and vnc plugins disabled in `camofox.config.json`.

## Raw REST pilot: `deploy/camofox/pilot_e2e.py`

`pilot_e2e_raw_rest.jsonl`: health 200, missing key → 401, wrong key → 401, `/stop` → 403,
`POST /tabs` example.com 200, snapshot contains the page text, tab title "Example Domain",
screenshot PNG 85 KB (`camofox-example.png`), close 200, activeTabs back to 0.

Note: the live example.com (2026) has "Example Domain" only in `<title>`. The body is
a paragraph ("This domain is for use in documentation examples…"), which is what
the accessibility snapshot shows.

Repeatability, 5 consecutive raw runs: 4 PASS, and 1 screenshot timed out after 30 s
upstream ("waiting for fonts to load"). Create, snapshot, auth and close were 5/5.

## Terminal MCP tool through `build_mcp()` (`mcp_tool_smoke.jsonl`)

Flag on: `camofox_browser` is listed next to `browser_verify`. Results: status OK (auth OK),
create_tab OK, snapshot OK, click without `allow_mutations` → `CAMOFOX_MUTATION_NOT_ALLOWED`,
screenshot OK, list_tabs only shows this agent's tabs, metadata URL → `URL_METADATA_BLOCKED`,
iana.org → `CAMOFOX_DOMAIN_NOT_ALLOWED`, click on "Learn more" (navigates to iana.org) →
`CAMOFOX_LANDED_OFF_POLICY` with the tab closed, close_tab/close_all OK, list empty.
Scroll on example.com → upstream 30 s wheel timeout (the page cannot scroll): a known upstream limitation.

## Tests

- `tests/test_camofox_provider.py`: 42 unit passed, 2 live skipped by default.
- `CAMOFOX_LIVE=1`, 3 consecutive runs: 2/2 passed each time (`pytest_live.txt`), including cross-agent
  isolation (`other-agent` gets `CAMOFOX_TAB_NOT_FOUND`) and type on a loopback fixture.
  Scroll/screenshot accept a classified upstream error because of the flakiness above.
- Existing default browser on this branch (run via `systemd-run --user` because Chrome cannot start in
  the agent namespace): `browser_verify https://example.com` PASS (3/3 assertions),
  `browser_screenshot` OK.
- Full suite: see `full_suite.txt`.

## Resources after the runs

Unit memory 420–514 MB with the browser up, peak 779 MB, 0 restarts, 0 cgroup OOM/high events.
The node process idles at ~145 MB RSS after the browser's 2-minute idle stop.
Host still ~1.7 GB available, swap full (pre-existing).
