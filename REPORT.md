# Session Recovery + Self-Host/Federation readiness

Audited 2026-09-11 against the running fleet. Every row below is something
that was checked on the machine, not inferred from configuration intent.
Where a thing could not be checked, it says so rather than guessing.

## Scoring

Eight criteria per node. A node scores a point per criterion it actually
meets; "unknown" never counts as met.

| # | Criterion |
| --- | --- |
| C1 | node-agent (or controller) process running and healthy |
| C2 | autostart configured (systemd / launchd / Scheduled Task) |
| C3 | starts with **no interactive login** (survives a bare reboot) |
| C4 | reachable over the Tailscale overlay |
| C5 | can run its **own** controller (self-host, not just an agent) |
| C6 | durable local state: session registry + grants + leases |
| C7 | admin reachable from the controller (SSH) for rollout |
| C8 | federation peer API / cluster view |

## Per-node result

| Node | C1 | C2 | C3 | C4 | C5 | C6 | C7 | C8 | Score | Verdict |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | --- | --- |
| **local / M910** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | n/a | ❌ | 7/7 | **PASS** (federation missing) |
| **dell-linux** | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ✅ | ❌ | 6.5/8 | **PARTIAL** |
| **hp-linux** | ✅ | ❓ | ❓ | ✅ | ❓ | ❓ | ❌ | ❌ | 2/8 | **PARTIAL — unauditable** |
| **dell-5530 (Win)** | ✅ | ✅ | ❌ | ❌ | ❌ | ❓ | ✅ | ❌ | 3/8 | **PARTIAL** |
| **macbook** | ✅ | ✅ | ❌ | ✅ | ❌ | ❓ | ✅ | ❌ | 4/8 | **PARTIAL** |

**Fleet self-host/federation readiness: ~45%.** Session-recovery core is
substantially higher (see below); federation is 0%.

### local / M910 — PASS
`terminal-mcp-http`, `terminal-mcp-tunnel`, `cloudflared-…-dashboard` all
active+enabled, `Linger=yes`, Tailscale `100.117.214.87`.
`/health/live`, `/health/ready`, `/version` all answer; version reports a
clean SHA. Restarting the controller with live sessions leaves tmux
byte-identical (name + created epoch) and all 11 grants intact — verified
again this session.

### dell-linux — PARTIAL
`terminal-node-agent` active+enabled, `Linger=yes`, `KillMode=process`
present, controller-url already on the tailnet (`100.117.214.87:8766`),
9 live tmux sessions, all 19 state DBs present locally.

⚠️ **Version skew, and it is real.** Its checkout is `ee375c1`, a commit that
does not exist on M910. It forked at `eb9f51d` and carries one local docs
commit, while M910 has seven it does not — including the whitelist removal and
every recovery fix below. This is exactly why dell-linux rows still report the
old contradictory `allowed=false` next to `effective_read=true`. It runs the
agent but cannot currently host a controller of the same generation.

### hp-linux — PARTIAL, and mostly unauditable
Agent answers `/v1/health` (`node_id=hp-linux`, agent 0.12.0) with 4 readable
sessions, on the tailnet at `100.67.53.117`.

❌ **BLOCKED: no SSH access.** The controller's key is not authorised there
(`Permission denied (publickey)` for `mesflow`/`hp`/`dell`). Autostart, linger,
local state and version are therefore **unknown, not assumed**. Nothing can be
rolled out to this node until that is fixed.

### dell-5530 (Windows) — PARTIAL
Scheduled Task `TerminalMcpNodeAgent-dell-5530` is Running with **both** a
Logon and a Boot trigger, restart bounded (3 × 1 min).

* ❌ **C3**: principal is `LogonType=Interactive`, `RunLevel=Limited` — the
  boot trigger will not actually start the agent until that user logs in. The
  repo's own `configure-windows-node-stability.ps1` already knows the fix
  (S4U); this task was not created with it.
* ❌ **C4**: **Tailscale is not installed.** This node cannot join the peer
  mesh at all, and its controller-url is still the LAN address
  `192.168.1.109:8766`, so it depends on both hosts being on the same subnet.
* `StartWhenAvailable=False`, so a missed boot trigger is not caught up.

Its three ConPTY sessions (`win1`/`win2`/`wtest`) remain the reason nothing
here may be restarted without a maintenance window.

### macbook — PARTIAL
LaunchAgent `com.terminal-mcp.node-agent.macbook.plist` with `RunAtLoad=true`
and `KeepAlive`, reachable on LAN and tailnet.

❌ **C3**: a LaunchAgent runs at **user login**, not at boot. After a cold
reboot the node stays down until someone logs into the desktop. A LaunchDaemon
(or `loginwindow` auto-login) is the fix; neither is configured.

## Session Recovery — what exists, what was fixed

Most of this was already built and is good: `recovery_engine.py` (lease-locked,
generation-counted, policy-gated), `recovery_loop.py`, and a
`session_records` schema that already persists node_id, stable_session_id,
conversation_id, cwd/repo_root/git_branch/worktree, agent_type, launch command,
grants, binding names, recovery_state/attempts/generation, per-session
`auto_recovery_enabled`, and `killed_at`/`deleted_at`/`offline_at` tombstones.
`drop_events` is the event log. None of that needed rebuilding.

Three real defects found and fixed this session:

| Fix | Commit | What was wrong |
| --- | --- | --- |
| Tombstones honoured | `8f9d03a` | `RECOVERABLE_STATUSES` includes KILLED so a human can press Reopen — but `reconcile_node` walks the same set, so the background pass would undo an operator's deliberate stop. KILLED is now `RECOVERY_TOMBSTONED` to the engine; `force=True` still reopens. |
| SOFT_RECONNECT tier | `783be94` | A node-agent restart does not kill tmux, so a session marked MISSING is usually still alive when the node returns. The engine respawned it, hit `SESSION_ALREADY_EXISTS`, and recorded a FAILED recovery for a healthy session while burning an attempt. It now reconciles the record instead, before the attempt budget. |
| Staleness bound | `ccdec63` | Enabling auto-recovery would have spawned **136 real processes** — measured, not estimated. |

### The recovery tiers now

| Tier | State | Trigger |
| --- | --- | --- |
| SOFT_RECONNECT | `RECONNECTED` | runtime session still live on that node → record reconciled, nothing spawned |
| AGENT_RESUME | `RESUMED_OK` | gone, but a real `conversation_id` is on record → reopened with `--resume`, verified before success is reported |
| TASK_RECOVERY | `RECOVERY_DEGRADED` | gone, no resume id → honest metadata-only recreate in the right cwd/branch |
| refused | `RECOVERY_TOMBSTONED` / `RECOVERY_STALE` / `RECOVERY_BLOCKED` | intentional stop, too old, or policy/attempt budget |

One caveat worth stating plainly: the liveness probe must not use
`controller.resolve_session` — that answers from a TTL'd location cache and
reports a just-killed session as alive. The first version of SOFT_RECONNECT
did exactly that and silently skipped a real recovery; the existing live MCP
round-trip test caught it. It now takes a fresh fleet listing, once per
reconcile pass.

## Why auto-recovery is still OFF here

`auto_recovery.enabled` is `False` on this deployment and should stay that way
for now. The registry holds 207 MISSING records; 136 satisfied "recoverable".
The staleness bound cuts that to ~2, but 2 is not 0, and both are disposable
test sessions from today. The real fix is a registry that knows which sessions
were disposable — it currently keeps a durable row for every session that ever
existed, including everything pytest creates. Per-session opt-in
(`auto_recovery_enabled`) already works and is the safe way to use this today.

## Federation — 0%

There is no federation code: no peer protocol, no capability handshake, no
cluster index, no `cluster_status`/`route` API, no second controller. Verified
by inspection, not assumed.

**Failure matrix if M910 goes offline — everything stops.** Every node is an
agent pointed at one controller; the MCP surface, the dashboard, routing,
session discovery and recovery all live there. dell-linux is the only node
with both a full checkout and complete local state, so it is the only
realistic second controller today — and it is on a divergent commit.

## What must happen next, in order

1. **Unblock hp-linux** — authorise the controller key so the node can be
   audited and rolled out to at all. (Needs a credential/an action on that
   host; not something to do silently.)
2. **Converge versions** — dell-linux is on a commit M910 has never seen.
   Nothing federated should be built while nodes run different generations of
   the protocol.
3. **Windows autostart + tailnet** — switch the Scheduled Task principal to
   S4U, install Tailscale, repoint controller-url to the tailnet address. All
   three need a maintenance window because of `win1`/`win2`/`wtest`.
4. **macOS boot-start** — LaunchDaemon or auto-login, otherwise the node is
   down after every cold boot.
5. **Registry disposability signal**, then global auto-recovery can be turned
   on safely.
6. **Federation V1** only after 1–3. Building a peer protocol across nodes
   that cannot be reached, cannot be updated, and do not share a version is
   how the version skew above becomes permanent.


---

## Historical Claude ghost-composer investigation

The following is the original branch report. Current integration retains canonical adapter acknowledgment, staged-editor handling, and Codex watchdog protections; composer release alone is not confirmation.

# P0 — Claude submit/Enter: root cause, fix, evidence

**Branch:** `fix/p0-claude-submit-ghost-composer` (based on `main` @ `eb9f51d`)
**Date:** 2026-09-11 · **Node:** `hp-linux` · **Claude Code:** 2.1.267 · **tmux:** 3.6

---

## 1. Root cause

**Claude Code renders a dim ghost suggestion inside an EMPTY composer, and
`tmux capture-pane` strips the dim attribute — so an empty composer is
byte-identical to one holding a pending prompt.**

On the wire, a ghost suggestion and a real draft differ only by SGR 2 (faint):

```
ghost (composer EMPTY):  ESC[39m ❯ \xa0 ESC[2m yes, publish the report ESC[0m
draft (composer FULL):   ESC[39m ❯ \xa0 REAL_TYPED_DRAFT
```

`capture-pane -p` without `-e` strips every SGR run, so both become the
identical plain string `❯ <text>`.

### The failure loop, verified end to end

1. Claude finishes a turn → composer empty → Claude draws a dim ghost
   suggestion into it (typically a proposed reply to its own last question).
2. An operator/orchestrator reads the pane, sees `❯ yes, publish the report`,
   and concludes the prompt is stuck unsubmitted.
3. It calls `terminal_send_keys(["Enter"])`. The Enter reaches a **genuinely
   empty** composer, so Claude correctly does nothing and emits **zero bytes**.
4. The pane is byte-identical across the verification window → the verifier
   reports `DELIVERY_UNKNOWN` / `SUBMIT_UNCONFIRMED` — which *reads* like
   "Enter was swallowed" and sends the caller back to step 2.

The real audit row from the live incident (`~/.local/state/terminal-mcp/audit.db`, id 13):

```
send_keys | hp1 | ["Enter"] | SENT_UNCONFIRMED
"the pane looked identical to its pre-send state throughout the verification window"
```

### Why "Enter is being swallowed" was the wrong hypothesis

Every layer below the misread was measured and found correct:

| Check | Result |
| --- | --- |
| `tmux send-keys Enter` / `C-m` / literal `\r` / `-H 0d` | all deliver the identical single byte `0x0d` (raw pty sniffer) |
| Pane tty mode on the stuck session | raw (`-icanon -echo`), identical to a healthy one |
| `TIOCINQ` on the stuck pane's tty | **0** — Claude had *read* the Enter byte |
| Claude process state | alive, `utime` advancing (event loop running) |
| `pane_in_mode` (copy-mode) | 0 |
| Kitty keyboard / bracketed paste / modifyOtherKeys negotiation | identical on stuck and healthy sessions (`CSI>5u`, `?2004h`, `>4;2m`) |
| Replaying the pane's own output log through a VT emulator | produced an **empty** composer — the visible text was never in Claude's render stream as draft content |

**The decisive probe** (minimal and reversible, on the live stuck pane `hp1`):

```
BEFORE:   ❯ yes, publish the report
send 'X'  → ❯ X                        ← composer was EMPTY; X went to an empty buffer
send BSp  → ❯ yes, publish the report  ← ghost text redrawn
```

So the prompt was never pending. There was nothing to submit, and the Enter
was correctly a no-op. The bug was entirely in **classification**, and in the
codebase treating stripped pane text as composer state.

### The same blindness inside the code

`core._extract_composer_text()` took the last non-empty line, stripped a
leading `"> "`, and handed the result to `adapters.submit_ack_evidence` as the
text an acceptance had to echo. Against a real Claude session that returned the
**ghost suggestion**, so a bare Enter was verified against a string that had
never been submitted and never would be. `_sent_text_echoed` and
`_codex_draft_in_composer` had the same exposure.

---

## 2. The fix

### `terminal_mcp/composer.py` (new)

An ANSI-aware, pure-function composer reader. Returns one of three states —
deliberately three-valued, because *"I can see there is nothing to submit"* and
*"I cannot tell"* are different facts and collapsing them **is** the bug:

| state | meaning |
| --- | --- |
| `DRAFT` | real, non-dim pending text — there **is** something to submit |
| `EMPTY` | blank, or holding only a dim ghost suggestion |
| `UNKNOWN` | no composer marker, or a capture with no attribute information |

It also distinguishes the **live** composer from a submitted prompt's
identical-looking `❯ text` echo in scrollback: a live composer is either inside
a box (a `───` rule row directly above it — the real Claude/Codex shape) or has
nothing non-blank below it. Ghost text is never exposed as `draft_text`.

### New delivery states (`adapters.py`)

| state | meaning | correct caller action |
| --- | --- | --- |
| `NOT_ACTIVATED` | Positively established from an ANSI composer read: **nothing to submit**. | Find out why the text never arrived. **Never** press Enter again. |
| `ACTIVATION_UNCERTAIN` | A **real draft** was there, exactly one Enter went out, no positive evidence in the grace window. | Genuinely ambiguous — never resend the text. |
| `DELIVERY_UNKNOWN` | *(unchanged)* Enter went out, evidence inconclusive, composer unreadable. | Honest fallback. |

`submit_status` keeps its exact legacy vocabulary (all three → `SUBMIT_UNCONFIRMED`),
so **no existing caller changes behaviour**. The new precision lives in
`delivery_state` / `submit_reason` / `evidence`.

### Behaviour changes

- **`terminal_send_keys(["Enter"])` is composer-gated.** If the composer
  positively reads `EMPTY` — confirmed by **two independent reads** a settle
  window apart, so a mid-redraw frame cannot wrongly withhold a legitimate key —
  and the target is not showing a menu/approval widget, **no keystroke is sent
  at all**; the result is `NOT_ACTIVATED` with the ghost text quoted back.
- **Composer transition is the primary submit evidence.** A draft *leaving* the
  composer is direct causal proof; a pane diff is not. `OUTPUT_CHANGED` alone is
  never an ACK for a composer-based agent, and a composer that positively still
  holds this attempt's draft now **vetoes** a confirmation the older heuristics
  would have granted.
- **Per-agent `SubmitPolicy`.** "Claude and unknown agents are single-submit" is
  now a structural invariant of the adapter instead of four
  `adapter.name == "codex"` string checks in `core.py`. Config can narrow an
  agent's Enter budget, never widen it past what its policy allows.
  `UNKNOWN_AGENT_SUBMIT_POLICY is CLAUDE_SUBMIT_POLICY`.
- **Codex is untouched.** Bounded, evidence-gated Enter retries and the
  Escape+Enter recovery behave exactly as before (regression-tested).
- **Bounded, configurable, deterministic timing** — no loops, no blind retries:
  `submit.<agent>.settle_ms` (default 80, the historical value) and
  `submit.<agent>.composer_grace_ms` (default 3000). Each is one deadline and
  one poll interval, and neither can ever cause an extra keystroke.

### Backend parity

`ansi_capture_supported` is now declared explicitly on the backend
(`True` for tmux, `False` for Windows ConPTY) rather than inferred from a
capture — inference is wrong in exactly the case that matters, because the
moment a real draft replaces the ghost the row carries no SGR either. The
remote Linux node path and the local tmux path are the *same* `TerminalService`
(`node_agent` wraps one instance; `LocalNodeClient` forwards straight into it),
so they share these semantics by construction — asserted by a test, not prose.
On Windows the composer reads `UNKNOWN`, the Enter is still delivered, and the
verdict degrades to the pre-existing `DELIVERY_UNKNOWN` — never a false
`NOT_ACTIVATED`, never a false `SUBMIT_CONFIRMED`.

---

## 3. Files changed

| File | Change |
| --- | --- |
| `terminal_mcp/composer.py` | **new** — ANSI-aware composer reader, ghost/draft/unknown classification |
| `terminal_mcp/adapters.py` | `NOT_ACTIVATED`/`ACTIVATION_UNCERTAIN` states; `SubmitPolicy` per agent |
| `terminal_mcp/core.py` | composer evidence in both send paths; the bare-Enter gate; policy-gated retry/recovery; ghost-aware `_extract_composer_text`; configurable settle/grace |
| `terminal_mcp/config.py` | `SubmitProfile.settle_ms`, `SubmitProfile.composer_grace_ms` (validated, bounded) |
| `terminal_mcp/session_backend.py` | `ansi_capture_supported` on the Protocol |
| `terminal_mcp/tmux.py` | declares `ansi_capture_supported = True` |
| `terminal_mcp/windows_backend.py` | declares `ansi_capture_supported = False` |
| `tests/fixtures/claude_ghost_composer.py` | **new** — real pty fixture reproducing the dim-ghost composer box |
| `tests/test_submit_composer_evidence.py` | **new** — 21 regression tests |
| `tests/test_adapters.py` | delivery-state vocabulary updated for the two new states |
| `docs/prompt-submission.md` | new "P0 (2026-09-11) — composer evidence" section |

---

## 4. Tests

`tests/test_submit_composer_evidence.py` — 21 tests, all green. The fixture's
ghost row was verified byte-identical to the real live `hp1` pane.

Required coverage:

| Required case | Test |
| --- | --- |
| (a) prompt + Enter really clears composer → confirmed | `test_a_real_submit_clears_the_composer_and_confirms` |
| (b) Enter swallowed, draft remains → not confirmed, no second Enter | `test_b_swallowed_enter_is_uncertain_and_never_retried` |
| (c) redraw/output noise → no false positive | `test_c_redraw_noise_is_never_mistaken_for_an_ack` |
| (d) menu/autocomplete state → no wrong selection submitted | `test_e_open_menu_with_empty_text_composer_is_not_submitted_into` |
| (e) Codex still bounded-retries | `test_f_codex_keeps_its_bounded_evidence_gated_enter_retry` |
| (f) remote-node ≡ local tmux; Windows via deterministic mock | `test_remote_node_path_shares_the_identical_submit_semantics`, `test_windows_conpty_backend_degrades_to_unknown_never_to_not_activated`, `test_local_tmux_and_node_paths_use_one_composer_reader` |
| The live incident itself | `test_d_bare_enter_into_a_ghost_composer_sends_no_key_at_all` |

Every integration test asserts the number of Enter bytes **actually delivered
to the pty** (the fixture logs each read byte) — the single-submit policy is
only meaningfully tested by counting real keystrokes, not by reading a status
field.

Also re-run green: `test_send_reliability.py`, `test_adapters.py`,
`test_adapters_real_cli.py` (real disposable Codex/Claude CLIs),
`test_submit_profiles.py`, `test_submit_config_wiring.py`,
`test_verified_submit_watchdog.py`, `test_controller.py`, plus the full suite.

---

## 5. Dogfood evidence

Real disposable Claude Code session (`test-dogfood-enter`), driven through the
real MCP surface (`build_mcp` → `ControllerService` → `TerminalService`), with
`XDG_STATE_HOME` redirected so the live node's databases were never touched.

**One prompt injection, one Enter, real composer transition, real response:**

```
STEP 2  terminal_send_text("Reply exactly ENTER_OK", press_enter=True)
        delivery_state      = 'SUBMIT_CONFIRMED'
        evidence            = ['COMPOSER_RELEASED']
        enter_count         = 1          attempts = 1
        composer_before     = 'DRAFT'    composer_draft_before = 'Reply exactly ENTER_OK'
        composer_after      = 'EMPTY'
        submit_latency_ms   = 215.8

STEP 3  ENTER_OK in output: True
        history row: '❯ Reply exactly ENTER_OK'   composer row: '❯ '
```

**The live bug, now correctly classified — and no key sent:**

```
STEP 4  terminal_send_keys(["Enter"]) at an empty/ghost composer
        sent = False        enter_sent = False
        delivery_state = 'NOT_ACTIVATED'
        evidence       = ['COMPOSER_EMPTY']
        submit_reason  = 'no Enter was sent: the composer is empty, so there is
                          nothing to submit. Send the prompt text itself with
                          terminal_send_text.'
```

Audit trail — exactly one injection, no resend:

```
send_text | test-dogfood-enter | 'Reply exactly ENTER_OK' | press_enter=1 | SENT
send_keys | test-dogfood-enter | ["Enter"]                | press_enter=0 | SENT_UNCONFIRMED (no key sent)
```

**Read-only verdict on the two actual failing live panes** (no keystroke sent):

```
hp1: composer=EMPTY  ghost='yes, publish the report'
     -> new code WITHHOLDS the Enter -> NOT_ACTIVATED
hp2: composer=EMPTY  ghost='docker builder prune + gỡ image 2.24.8 đi'
     -> new code WITHHOLDS the Enter -> NOT_ACTIVATED
```

A false negative found *by* this dogfood was fixed before finishing: the first
run read the composer the instant the text bytes were written, raced Claude's
render, and reported `NOT_ACTIVATED` for a send that actually succeeded. The
pre-Enter read is now a bounded readiness poll performed **after** the settle
window, and `NOT_ACTIVATED` on a text send is only reached at the very end,
when the text is nowhere on the pane at all.

---

## 6. Remaining risk

1. **Windows/ConPTY cannot detect ghost text.** pyte does not model SGR 2, so
   `read_composer` returns `UNKNOWN` there and the verdict degrades to
   `DELIVERY_UNKNOWN` — safe, but the original misread is still possible for a
   Windows-node operator reading the pane. Fixing it means teaching the pyte
   screen to track SGR 2 per cell. Deliberately out of scope here.
2. **Ghost detection depends on Claude Code continuing to use SGR 2.** If a
   future release renders its suggestion differently, ghost rows would read as
   `DRAFT` again. The regression tests pin the byte-exact captured rows, so this
   fails loudly rather than silently.
3. **Composer-box heuristic.** Distinguishing the live composer from a scrollback
   echo relies on the `───` rule row above it (real Claude/Codex shape). A CLI
   that renders neither a box nor a trailing prompt reads `UNKNOWN` and falls
   back to the pre-existing pane-diff path — no worse than before.
4. **Latency.** A send whose composer becomes unreadable after Enter can now use
   up to two grace windows (~6s worst case for Claude) instead of one. Bounded,
   configurable, and it only ever delays an *honest* verdict.
5. **Not deployed.** The live `hp-linux` node is still running the old code (see
   §7). No process was restarted and no live session was touched.

### Separate defect found during the investigation (not fixed here)

`TmuxClient.ensure_output_capture` pipes the pane through
`stdbuf -oL cat >> <log>`. **Line buffering flushes on `\n`** — but an Ink TUI
redraws with `\r` + cursor moves and emits almost no newlines, so the Session
Knowledge Store's raw log can lag by up to a full stdio buffer (observed: `hp1`'s
log frozen at 22:49 while the pane kept updating). The method's own docstring
claims `stdbuf -oL` "forces line-buffered output", which is true but does not
achieve what it was reasoning about. `stdbuf -o0` would. Out of scope for this
lane — filed here so it is not lost.

---

## 7. Rollback / merge

Work was done in a **separate clone**, on a branch, and **not** applied to
`~/workspace/terminal-mcp`. That live tree is not a git repository and holds
~143 lines of another session's unpushed `docs/` work, so nothing there was
touched (task constraint 9). No controller, node-agent, or live session was
restarted; no live pane received a keystroke except the two reversible probe
keys on `hp1` (`X` then `BSpace`, net zero).

**Merge**

The branch lives in a durable, separate clone at
`~/workspace/terminal-mcp-p0-submit-fix` (origin points at
`hungtranbkit/terminal-mcp`; nothing has been pushed).

```bash
# review
cd ~/workspace/terminal-mcp-p0-submit-fix
git log --oneline -1                 # f207ec6, based on eb9f51d
git diff eb9f51d --stat

# merge into your own clone of the upstream repo
cd <your clone of hungtranbkit/terminal-mcp>
git fetch ~/workspace/terminal-mcp-p0-submit-fix fix/p0-claude-submit-ghost-composer
git merge --no-ff FETCH_HEAD

# or push the branch yourself (deliberately NOT done here)
cd ~/workspace/terminal-mcp-p0-submit-fix
git push origin fix/p0-claude-submit-ghost-composer
```

The branch is based on `eb9f51d` (current `main`) and touches only the files in
§3 — no overlap with the unpushed `docs/CONTROLLER_RUNBOOK.md` /
`docs/multi-node.md` work.

**Deploying to the live `hp-linux` node** (only when the operator chooses):
copy the changed `terminal_mcp/*.py` into `~/workspace/terminal-mcp/`, then
`systemctl restart terminal-node-agent`. The restart drops no tmux session
(sessions are owned by the tmux server, not the agent), but it is a real
restart — hence not done here.

**Rollback:** revert the merge commit, or restore the seven changed
`terminal_mcp/*.py` files. `composer.py` is additive and inert once nothing
imports it. No schema, database, config-file or wire-format change was made —
`submit.<agent>.settle_ms` / `composer_grace_ms` are new optional keys with
defaults equal to the previous hardcoded values, so an unmodified `config.yaml`
behaves exactly as before.
