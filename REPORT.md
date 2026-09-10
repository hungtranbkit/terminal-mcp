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
