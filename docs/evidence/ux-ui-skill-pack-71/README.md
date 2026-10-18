# Issue #71 evidence — shared UX/UI skill pack (2026-10-18)

Pin: `ux-ui-agent-skills` v2.9.8, commit `62092bd83bc3df0bcb9a2b3b643305ab11c547da`,
release `2.9.8-62092bd83bc3`. Every line below is copied from a real run.

## Install (`terminal-mcp-skill-packs sync`)

| Node | Result |
| --- | --- |
| Dell `dell-Latitude-5511` (live node) | `READY`, 640 files, tree hash verified, playwright 1.63.0, `/usr/bin/google-chrome`; router `claude=INSTALLED, codex=INSTALLED`; MCP registrations: none |
| m910 `mesflow-ThinkCentre-M910q` (live node) | `READY`, playwright 1.63.0; router `claude=INSTALLED, codex=INSTALLED` |

## Gates on the fixtures (`ux-ui-gates review <dir> --tokens --theme --component-root --focustrap-open '#open-dialog'`)

| Node | clean | broken |
| --- | --- | --- |
| Dell | `PASS: 23/23 gates passed, 0 failed, 0 blocked, 0 errored, 0 not run`, exit 0 | `FAIL: 10/23 gates passed, 13 failed, 0 blocked, 0 errored, 0 not run`, exit 1 |
| m910 (user session via `systemd-run --user`) | `PASS: 23/23 gates passed ...`, exit 0 | `FAIL: 10/23 gates passed, 13 failed ...`, exit 1 |
| m910 inside a restricted user namespace (Chrome's setuid sandbox unusable) | `INCOMPLETE: 7/23 gates passed, 0 failed, 16 blocked, 0 errored, 0 not run`, exit 2 | `FAIL: 2/23 gates passed, 5 failed, 16 blocked ...`, exit 1 |
| Dell, clean without `--focustrap-open` | `INCOMPLETE: 22/23 ... 1 not run`, exit 2 | |

The third row is the fail-closed case found live: before commit `5957a0a` the same
crash was reported as `FAIL: 7/23 ... 15 failed, 1 errored` on the CLEAN page.

Screenshots (`ux-ui-gates screenshot`, Dell): `clean-1280.png`, `clean-375.png`,
`broken-1280.png`, `broken-375.png` in this directory.

## Upstream self-test (`ux-ui-gates selfcheck`, the README's "52 gates"), Dell

- Read-only kit, no devDependencies (before `selfcheck` existed): `RESULT: 46/52`, exit 1.
  Three of the six failures were the environment (EACCES writing `kit/dist`; esbuild/react missing).
- `selfcheck` (writable copy, `npm ci --ignore-scripts` from the kit lockfile): `RESULT: 49/52`, exit 1, 13m.
  The three remaining are upstream's own examples on this Chrome:
  - Component harnesses (misc.html): `"65" 2.92:1 (need 3)` in default/hover/focus
  - Responsive under 1.25x font: `data-display.html @280px overflow +2px`
  - Industry templates: `checkout.html` inert buttons, `@280px overflow +8px`

Upstream advertises 52/52; this node measures 49/52. Reported as measured.

## Live agent smoke, Dell (scratch git repos under /tmp)

| Agent | Task | Pack used | Outcome |
| --- | --- | --- | --- |
| Claude Code 2.1.302 (`claude -p`) | review `./site` (broken fixture) | `Skill ux-ui-kit`, then `ux-ui-gates review` | reported `FAIL: 10/23 gates passed, 12 failed, ... 1 not run`, exit 1; re-ran with a focus-trap trigger |
| Claude Code | write `cron_next.py` | no (skill name appears only in the init skill list) | done |
| Codex CLI 0.162.0 (`codex exec`) | review `./site` | read `ux-ui-kit` router + `KIT/AGENTS.md`, ran `ux-ui-gates review` | reported `FAIL: 10/23 gates passed, 11 failed, ... 2 not run`, exit 1 |
| Codex | write `cron_next.py` | no (0 mentions in the event stream) | done |

## Tests

- `tests/test_skill_packs.py`: 49 passed (fake kit; no network/node/browser).
- `tests/test_skill_packs_e2e.py` with `TERMINAL_MCP_SKILL_PACK_E2E=1` on Dell: 3 passed (126 s).
