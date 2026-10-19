# Camofox ops readiness: post-merge, 2026-10-19

Scope: shared Camofox REST worker on m910, the optional Terminal MCP provider
(`camofox_browser`), and the optional PropertyIQ render adapter. Status words
below only describe what was run and observed.

## Merged code

| Repo | PR | Merge commit on main | Verified |
| --- | --- | --- | --- |
| hungtranbkit/terminal-mcp | #73 | `b97046d8b7a94b97f50c23996c97795234cf9aca` | on `main`; tree identical to reviewed head `e1443fb` |
| hungtranbkit/property-intelligence-vn | #23 | `74dc5728f5290580c710d079afd1d6202fd23451` | on `main`; tree identical to reviewed head `309acba` |

Follow-up (this branch, separate PR): deterministic scroll, busy fast-fail,
screenshot fallback, SSH tunnel bootstrap. See "Mitigations".

## Worker on m910 (observed 2026-10-19 06:42 +07)

| Item | State |
| --- | --- |
| Unit | `camofox-browser.service` (systemd --user) **active/running** since 03:13, 0 restarts, **disabled at boot** |
| Version | jo-inc/camofox-browser v1.18.1, `39c82094…` (only local change: `camofox.config.json` plugins youtube/persistence/vnc off); Camoufox 152.0.4-beta.30, bin sha256 `955c053a…` |
| Listener | `127.0.0.1:9377` only (no other address, no tunnel/proxy/VNC) |
| Auth | access key file 600, env file 600, dir 700; unauthenticated → 401, wrong key → 401, `/stop` → 403; `CAMOFOX_API_KEY`/`CAMOFOX_ADMIN_KEY` unset; `NODE_ENV=production` |
| Telemetry | `CAMOFOX_CRASH_REPORT_ENABLED=false`, `SENTRY_DSN` empty, Prometheus off |
| Caps | 2 sessions × 2 tabs, 4 tabs global, 1 concurrent per user; navigate 20 s, handler 30 s; cgroup MemoryHigh 1100M / Max 1400M / swap 256M, CPU 150%, tasks 512 |
| Memory | unit 552 MB with browser up (peak 818 MB); node idles ~140 MB after 2 min browser idle stop; cgroup events high=0 max=0 oom=0 |
| Host | RAM available 1.3 GB of 15 GB, **swap 4095/4095 MB used** (pre-existing), disk 61 GB free, load ~2 |
| Screenshots | work through the worker; provider flag `TERMINAL_MCP_CAMOFOX_SCREENSHOTS_ENABLED` defaults off |

## Tests run after merge (merged main code unless noted)

| Test | Result |
| --- | --- |
| Raw REST pilot `deploy/camofox/pilot_e2e.py` (health, missing key, wrong key, /stop, create example.com, snapshot, screenshot, list title, close, health-after) × 3 consecutive | **3/3 PASS**, 10/10 checks each; create 5.5 s cold, 0.6–0.8 s warm |
| `camofox_browser` through `build_mcp()` in an isolated local process, `TERMINAL_MCP_CAMOFOX_ENABLED=1`, allowlist `example.com` | tool listed beside `browser_verify`; status/create/snapshot/screenshot/list/close OK; metadata URL, non-allowlisted host, mutation without opt-in, off-allowlist click all refused as designed. Scroll on merged main: 10 s timeout (upstream wheel), and the following click waited 26 s on the upstream tab lock (fixed by the follow-up) |
| Default `browser_verify` + `browser_screenshot` (Playwright) on merged main | **PASS** (3/3 assertions) / OK |
| PropertyIQ `test_camofox_render.py` with `PI_CAMOFOX_LIVE=1` on merged main | **25 passed** (incl. JS-only loopback fixture + example.com) |
| Dell → m910 canary (short-lived SSH forward, temp key copy, merged-main provider, isolated process) | status/create/snapshot/list/close OK; private IP refused; tunnel, key copy and worktree removed afterwards (verified: no listener, no files) |
| Follow-up branch: unit `tests/test_camofox_provider.py` + `test_efficiency_benchmark.py` | 94 passed, 2 live skipped |
| Follow-up branch: live tests × 5 | **5/5 PASS**, ~1.5 s per test (previously up to 30+ s) |
| Follow-up branch: stress on a long loopback page (30 scrolls incl. after focusing an input, 10 screenshots) | 29 moved + 1 at-edge, 0 errors, worst 119 ms; screenshots 10/10, worst 79 ms |
| Follow-up branch: `camofox-tunnel.sh` lifecycle on Dell (install → disabled/inactive, start, smoke, provider canary, uninstall) | install did not enable/start; listener 127.0.0.1:19377 only; smoke health ok + unauthenticated 401; canary OK (scroll at-edge 136 ms); teardown left nothing |
| Full suite (Dell), follow-up branch | 1 failed, 9786 passed, 18 skipped; the failure (`test_transports.py::test_http_real_node_tools_full_lifecycle_round_trip`) also fails on main (earlier Dell run of main: 1 failed, the same test) |

Evidence: `docs/evidence/camofox-postmerge-20261019/` (no secrets; checked).

## Mitigations of upstream defects (follow-up PR)

- **Scroll:** upstream `/scroll` (mouse wheel) hung for 30 s on ~1 in 4 calls on
  scrollable pages, and always on non-scrollable pages or with a focused input. The
  provider now uses a constant `window.scrollBy` template (two validated integers,
  no input events). Agents still have no evaluate action.
- **Tab lock after a hang:** calls on that tab fail fast with `CAMOFOX_TAB_BUSY` +
  `retry_after_seconds` for 32 s. close/list are unaffected.
- **Screenshots:** 12 s client timeout. On failure the tab is marked busy and a
  `fallback_call` to `browser_screenshot` with the last allowed URL is returned.
- No CAPTCHA, login or access-policy circumvention was added or attempted.

## What is accessible from where

| Node | Today | How |
| --- | --- | --- |
| m910 (worker host) | raw REST, provider and PropertyIQ adapter work when their flags are set in a process | `http://127.0.0.1:9377` + key file |
| Dell (runs `terminal-mcp-http`) | **not enabled**. The live server runs another branch; no flag set, no tunnel installed | canary proven; install with `camofox-tunnel.sh` |
| HP (controller) | **not set up**. tcp/22 reachable, but this session has no authorized key and hopping via Dell is not authorized | owner runs `camofox-tunnel.sh` on HP |
| Public internet | **no exposure** | – |

The provider flag (`TERMINAL_MCP_CAMOFOX_ENABLED`) is off on every running Terminal
MCP server. The PropertyIQ adapter (`PI_CAMOFOX_RENDER_ENABLED`) is off, and no
real source has a render policy. No crawler, parser, AI path or production job was changed.

## Unresolved / blockers

1. **m910 memory:** swap fully used and ~1.3 GB RAM available. The worker adds
   ~140 MB idle and up to ~820 MB active. Boot-enable stays off.
2. **Persistent tunnel identity:** the canary used Dell's existing general SSH key
   to m910. A permanent forward should use `camofox-tunnel.sh keygen` and the
   forward-only `authorized_keys` line (owner adds it on m910).
3. **HP access** for installing the tunnel or provider env: owner action.
4. **Dell live server** runs a non-main branch. Enabling the provider there means
   deploying main (or the follow-up) to that server first: owner decision.
5. **Upstream:** v1.18.1 wheel and screenshot hangs remain upstream (mitigated in the
   provider, not fixed). Re-test before upgrading upstream; keep the version pinned.

## To enable at boot / for production

- [ ] m910 headroom: ≥ 2 GB RAM available sustained and swap not saturated (or a
  move to a host with headroom). Then `systemctl --user enable camofox-browser`
  (and `loginctl enable-linger mesflow` if it must start without a login session;
  check first).
- [ ] Merge the follow-up PR; deploy main to the target Terminal MCP server;
  confirm the full suite there.
- [ ] Forward-only key authorized on m910; `camofox-tunnel.sh install/start/smoke` on
  the server node; key copied out of band, chmod 600.
- [ ] Canary: set `TERMINAL_MCP_CAMOFOX_ENABLED=1` + an explicit
  `TERMINAL_MCP_CAMOFOX_ALLOWED_DOMAINS` on one server instance only; run the live
  tests; keep screenshots off unless needed.
- [ ] PropertyIQ: owner-reviewed render policy with an explicit rights basis before
  any real source; native parser/sync remains default.

## Rollback

Provider: unset the flag and restart (the tool disappears). Tunnel:
`camofox-tunnel.sh uninstall`. Worker:
`systemctl --user disable --now camofox-browser` (full steps:
`deploy/camofox/README.md`). PropertyIQ: inert unless enabled; revert `74dc572`
if needed.
