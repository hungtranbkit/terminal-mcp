# Windows detached session hosts

How a Windows node's Claude/Codex/PowerShell sessions are made to survive a
node-agent restart or update, why the sessions running on dell-5530 today cannot
be migrated into it in place, and the rollout that loses none of them anyway.

---

## 1. The problem, and the evidence for it

Before this design, a Windows session's ConPTY child was spawned by
`WindowsSessionBackend` **inside the node-agent process**, and the only registry
of sessions was that backend's in-memory `dict`. Two consequences, both measured
rather than assumed (see "Windows node-agent restart safety (Phase 0)" in
`docs/REQUIREMENTS.md`):

1. **A session does not survive its agent.** Confirmed live against real Windows
   processes, by two independent routes — a hard `taskkill /F` and the graceful
   `POST /v1/internal/shutdown` — that a session's ConPTY child, and its sibling
   `conhost.exe`, die with the agent. The Phase 0 conclusion was explicit: *any*
   node-agent restart on dell-5530 ends its sessions' real OS processes,
   unconditionally, regardless of how the restart is performed.
2. **Nothing on disk describes a session**, so even a cooperative handover had
   nothing to hand over.

So every node-agent update was an outage for every session on that node. That
finding was scoped to *the current architecture* — which is what this replaces.

The fix is not a gentler way to stop the agent. It is to stop the session being
the agent's child at all.

---

## 2. Architecture

```
    node agent  (control plane, restartable at will)
        |   reads  meta.json, out.log
        |   writes in.log, ctl.json
        v
    session host  (one detached process per session, owns the PTY)
        |   ConPTY
        v
    powershell.exe | claude.exe | codex
```

The agent holds no PTY. It holds file offsets. Restarting it therefore tears
nothing down, and after restart it learns what exists by reading a directory.

| File | Module | Role |
| --- | --- | --- |
| `terminal_mcp/windows_session_host.py` | host + shared state format | The detached host process, the atomic metadata, the spools, liveness/PID-reuse rules, `discover()` |
| `terminal_mcp/windows_detached.py` | agent side | `HostProcessProxy` (a `PtyProcessLike` over the spool), the spawn factory, `adopt_sessions()` |
| `terminal_mcp/windows_backend.py` | integration | Optional `session_process_factory`, `adopt_detached_sessions()` |
| `terminal_mcp/windows_agent.py` | wiring | `--detached-sessions` (**off by default**), `--session-state-root`, adoption at startup |

Per session, under `<state root>/<name>/`:

| File | Written by | Purpose |
| --- | --- | --- |
| `meta.json` | host | Source of truth: host pid, child pid, generation, cwd, argv, geometry. Atomic. Its mtime is the heartbeat. |
| `out.log` | host | Append-only output spool, rotated at 8 MB keeping the recent tail |
| `in.log` | agent | Append-only input spool, read by offset |
| `ctl.json` | agent | Control requests (resize, shutdown), consumed exactly once |
| `host.log` | host | Host diagnostics only — never session output |

### Why a file spool and not a socket or named-pipe RPC

A live control connection has to be re-established after a restart: a handshake,
a protocol, version skew between an old host and a new agent, and a decision
about output produced while nobody was connected. An append-only spool has none
of that. Output written while the agent was down is simply still there; history
survives the agent by construction rather than by buffering; and "reconnect" is
`open()`. The input channel is a spool for the same reason plus one more: a POSIX
FIFO does not exist on Windows and a Windows named pipe is a different API, so a
pipe-based input path would mean two platform-specific implementations of the one
code path that carries an operator's keystrokes — and the Windows half could only
ever be tested on a Windows host, which this project's CI does not have.

**One known rough edge in rotation.** When `out.log` rotates, a reader whose
offset is now past the end of the shrunken file resets to 0 and re-reads the
retained tail, so the agent's in-memory scrollback shows that tail twice. The
alternative — a rotation the reader can detect precisely — needs a generation
counter in the spool and a protocol to interpret it; duplicated history at an 8 MB
boundary is the cheaper failure than either losing output or adding that protocol.
No output is lost.

### Why an adapter and not a new backend

`WindowsSessionBackend` talks to a session's process through exactly one narrow
shape — `PtyProcessLike` (`pid` / `isalive` / `read` / `write` / `setwinsize` /
`terminate`). `HostProcessProxy` implements that shape against the spool, so the
reader thread, the pyte VT parser, the history buffer, the resize path, the
desktop-viewer path and the kill path all keep working untouched. Teaching the
backend about hosts, spools and offsets would have meant changing every one of
those, each load-bearing on a live node today.

Two details in the proxy are not arbitrary:

- **`pid` is the child's pid, not the host's.** The backend feeds this pid to
  `_win32_foreground_command`, which walks the descendant tree to answer "what is
  this session running right now". The host's pid there would make every session
  report the host wrapper instead of `claude.exe`. Host liveness is reported
  through `isalive()` instead, so `_is_alive`'s child-pid-then-`isalive`
  composition still checks both.
- **`read()` sleeps briefly on an empty spool.** `_reader_loop` treats an empty
  read from a live process as "nothing yet, poll again", so a proxy that returned
  `""` instantly would spin that thread at 100% CPU. pywinpty's own `read` blocks
  with a timeout; this keeps the same shape.

### Session identity survives adoption, so permissions do too

`SessionInfo.session_id` is `win:<name>:<created_epoch>` and `pane_id` is
`pid:<child pid>`. Session grants (`session_grants`, keyed by name) and
send-bindings can pin those values, so adoption deliberately reuses the
**original** `created_at` from `meta.json` rather than stamping the moment the
entry is rebuilt. Stamping a fresh time — the obvious thing to do, since the
registry entry really is being created now — would change `session_id` on every
restart, and every pinned grant and binding would quietly stop matching: the
operator would lose read or input capability on a session that is plainly still
running. The child pid is unchanged by definition, so `pane_id` is stable for
free. A test pins both.

### Windows process ownership

`spawn_host` uses exactly `DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB |
CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` (`0x09000208`), asserted by test.

- `DETACHED_PROCESS` — no inherited console; without it the host joins the
  agent's console and dies with it.
- `CREATE_BREAKAWAY_FROM_JOB` — **the one that actually matters.** A Scheduled
  Task's process tree is commonly placed in a job object with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; without breakaway the OS kills the host
  when the agent's job closes, no matter how detached it otherwise looks. It
  fails with `ERROR_ACCESS_DENIED` if the job forbids breakaway
  (`JOB_OBJECT_LIMIT_BREAKAWAY_OK` unset), which is why `spawn_host` raises
  `BreakawayDenied` rather than silently retrying without the flag — a host
  spawned without it looks identical and then dies with the agent, which is the
  exact bug being fixed.
- `CREATE_NEW_PROCESS_GROUP` — Ctrl-C to the agent's group does not reach it.
- No handle inheritance (`close_fds`), and stdio goes to `host.log`, never to an
  inherited pipe: an inherited pipe handle keeps a dead agent's objects
  referenced and reintroduces exactly the coupling this removes.

### Liveness, and why a live PID is not enough

`host_state()` returns `ALIVE`, `GONE` or `PID_REUSED`:

| Condition | Verdict |
| --- | --- |
| recorded host pid not running | `GONE` |
| pid running, heartbeat (metadata mtime) fresher than 30 s | `ALIVE` |
| pid running, heartbeat missing or older than 30 s | `PID_REUSED` |

Windows recycles PIDs aggressively. A stale record whose pid happens to be live
again would make an agent adopt a session whose "host" is an unrelated process,
then write an operator's keystrokes into a spool nobody reads **while reporting
the session healthy**. The heartbeat is what distinguishes the two, and the
ambiguous case is resolved conservatively: a live pid with no fresh beat is
treated as reuse, never as a session.

On POSIX, liveness also rejects zombies. `os.kill(pid, 0)` succeeds on an
already-exited-but-unreaped process, so without that check a host killed a moment
ago reads as `ALIVE` for as long as its parent has not called `wait()`. Windows
has no zombie state, so this is what makes liveness mean the same thing on both
platforms rather than a test-only convenience.

---

## 3. Requirement → implementation → test

| # | Requirement | Implementation | Test |
| --- | --- | --- | --- |
| 1 | Restart cannot terminate sessions | detached host, `WINDOWS_DETACH_FLAGS` | `test_sessions_survive_the_agent_being_killed_and_are_re_adopted`, `test_the_child_process_itself_outlives_the_agent`, `test_a_detached_child_survives_its_spawner_being_killed` |
| 2 | Rediscover/reconnect under the same ids | `discover()` → `adopt_sessions()` → `_register_adopted` | same flagship test; `test_the_backend_creates_detached_sessions_and_a_new_backend_adopts_them` |
| 3 | stdout/history and input continue | offset spools; adoption replays from offset 0 | flagship test (pre-restart history + post-restart input), `test_history_and_input_continue_across_a_reader_restart` |
| 4 | Orphan cleanup explicit and safe | `cleanup_session_dir` / `terminate_host`, never on a timer | `test_cleanup_removes_an_orphan_only_when_asked`, `test_the_destructive_primitives_are_never_self_invoked` |
| 5 | No duplicate session on restart | adoption is a pure read keyed by directory name; duplicate check re-done inside the registry lock | `test_adopting_twice_does_not_duplicate_a_session`, `test_new_session_still_refuses_a_name_that_was_adopted` |
| 6 | Crash/reboot documented | §4 below | `test_a_crashed_host_is_reported_as_an_orphan_and_never_adopted`, `test_a_crashed_session_is_not_reported_alive_by_its_proxy` |
| 7 | Creation flags / job ownership | §2 | `test_the_detach_flags_are_exactly_the_four_that_matter`, `test_create_breakaway_from_job_is_present`, `test_spawn_passes_detach_flags_on_windows_and_new_session_on_posix` |
| 8 | Atomic metadata, stale PID protection | temp+fsync+`os.replace`; heartbeat + `PID_REUSED` | `test_metadata_is_written_via_rename_not_truncation`, `test_a_live_pid_with_a_stale_heartbeat_is_treated_as_pid_reuse`, `test_a_stale_record_whose_pid_is_live_again_is_not_adopted` |
| 9 | Linux unchanged, API compatible | second optional factory; tmux untouched | `test_the_linux_backend_path_is_untouched_by_all_of_this`; existing `test_windows_backend.py` (86 tests) unchanged and passing |
| 10 | Test coverage | 52 unit + 19 integration | `tests/test_windows_session_host.py`, `tests/test_windows_detached_sessions.py` |

### What is proven, and what is not

The child processes in these tests are real, the pty is real, the agent that gets
SIGKILLed is a real separate process, and the code exercised is the production
code path — but the platform is Linux, because this project's dev and CI hosts
are Linux and the live Windows node must not be restarted.

**Not measured on Windows:** ConPTY behaviour and the Win32 creation flags. Those
are asserted structurally (the exact flag combination) and reviewed against the
Win32 contract. Every claim about survival on a real Windows host is therefore
**designed and reviewed, not measured**, and step 4 of the rollout below is the
step that measures it — on a disposable session, before anything valuable depends
on it.

---

## 4. Crash and reboot behaviour

| Event | Sessions | What the agent reports | Recovery |
| --- | --- | --- | --- |
| Agent restart / update | **survive** | `ADOPTED` for each | automatic, at startup |
| Agent crash (killed, OOM) | **survive** | `ADOPTED` | automatic |
| Host process crash | lost — the pty died with it | `ORPHAN_HOST_GONE` | state dir kept for inspection; removal is explicit |
| Child exits normally (user types `exit`) | ended | host exits too; `ORPHAN_HOST_GONE` | explicit cleanup |
| Machine reboot | **lost** — no process survives a reboot | `ORPHAN_HOST_GONE` for every session | explicit cleanup; sessions must be recreated |
| Host killed, pid later reused | lost | `ORPHAN_PID_REUSED` — never adopted | explicit cleanup after the operator confirms |

Two deliberate properties:

- **A reboot is not recoverable and is not pretended to be.** Nothing in this
  design survives loss of the machine; `out.log` survives, so the conversation
  transcript is readable after a reboot even though the session is gone.
- **An orphan is never auto-deleted.** A `PID_REUSED` verdict can also be a live
  host that was merely slow to heartbeat, and deleting that destroys a real
  session's history. Adoption reports; an operator removes.

When the host exits it terminates its child. A host that exited leaving a live
ConPTY behind would leave a process no agent could ever reach again — an orphan
with no state directory pointing at it.

---

## 5. Why dell-5530's current sessions cannot be adopted in place

The task asked for a bridge if one is feasible, and an explicit proof if not.
**It is not feasible.** The proof:

**(a) The pty handle is not reachable from another process.** A 0.12.0 session's
ConPTY is represented by an `HPCON` plus its input/output pipe handles, all in
the running agent's own handle table. Windows provides no API to enumerate or
re-open an existing pseudoconsole from outside the process that created it —
there is no name, no path and no registry for it. `DuplicateHandle` can move a
handle between processes, but only with a *cooperating source*: code inside the
holding process must call it and pass the result out. 0.12.0 contains no such
code and no endpoint that could be asked to run it. Adding one requires
replacing the agent — which is the thing being ruled out.

**(b) Even a duplicated handle would not keep the session alive.** The child's
console is attached to a pseudoconsole owned by the old agent. When that process
exits, its handles close, ConPTY signals the client and `conhost.exe` tears the
console down. This is not inference: Phase 0 measured exactly this outcome on
this machine, via a hard kill *and* via the graceful shutdown path, and got the
same result both times.

**(c) The old agent must exit.** An update replaces the code the process is
running, and the port (8790) admits one listener. There is no version of
"upgrade the agent" in which the 0.12.0 process keeps running its own sessions
under the new code.

**(a) ∧ (b) ∧ (c) ⇒ every path that ends with the 0.12.0 agent gone also ends
with its current sessions gone.** No in-place migration, bridge or adoption
exists. The detached architecture applies to sessions **created after** it is
enabled; it cannot retroactively rescue sessions that were created as children.

This is precisely why the rollout below never asks the live sessions to survive a
restart.

---

## 6. Zero-loss rollout for dell-5530

Two options. **Option B loses nothing and waits for nothing**, and is the
recommended one if the live sessions' work cannot be paused.

Before either: enumerate what is actually live *now* (`terminal_status` per
session on that node) rather than trusting any name list written earlier,
including this document's.

### Option A — staged restart at a chosen moment (simplest)

1. **Deploy files only.** Copy the new code to the node. Do **not** restart.
   `--detached-sessions` defaults to off, so the running 0.12.0 agent's behaviour
   is unchanged by the presence of the files. *(This is the deploy that was
   already approved and is safe to repeat.)*
2. **Capture the live sessions' state.** For each: `terminal_capture` /
   `terminal_tail` the full scrollback to a file outside the node, and note each
   session's `cwd`, command and — for Claude sessions — its `--session-id`, which
   is what allows `--resume` to continue the same conversation afterwards.
3. **Quiesce.** Let each session reach a point where nothing is mid-run. Nothing
   in this step is automatic; it is a human confirming each session is idle.
4. **Prove the new path on a disposable session first.** With the node still
   serving the old agent, start a *second*, isolated agent on another port with
   `--detached-sessions` and its own `--session-state-root`, create a throwaway
   session, restart that second agent, and confirm the session survives and is
   re-adopted. This is the step that converts "designed and reviewed" into
   "measured on this Windows host". Do not proceed if it fails.
5. **Restart once, with the flag on.** Restart the real agent via `POST
   /v1/internal/shutdown` (never `schtasks /end` — Phase 0 proved it
   non-deterministic) with `--detached-sessions` added to the Scheduled Task's
   arguments. The old sessions end here; this is the unavoidable cost proven in
   §5.
6. **Recreate.** Recreate each session with its recorded cwd/command, resuming
   Claude sessions with the recorded `--session-id` via `--resume`.
7. **Verify, then confirm the property.** Confirm all sessions are up, then
   restart the agent a second time and confirm they all come back `ADOPTED`.
   Until that second restart, the property is claimed but not demonstrated on
   this node.

### Option B — side-by-side, zero loss, no waiting (recommended)

The two agents are independent processes with independent ports and independent
state, so both can run at once:

1. Deploy files only, as in A.1.
2. Start a **second** agent on the same machine: a new port, a distinct
   `--node-id` (e.g. `dell-5530-detached`), `--detached-sessions`, and its own
   `--session-state-root`. Register it as an additional node.
3. Prove survival there (A.4) on a throwaway session.
4. Create **new** work on the new node. The 0.12.0 agent keeps serving its four
   existing sessions, untouched, for as long as they are needed — nothing is
   killed and nothing is waited for.
5. Retire the old agent only when its sessions are genuinely finished, then
   re-point the `dell-5530` node id at the new agent.

Cost: two agents on one machine for a while, and two node ids in the fleet until
step 5. Benefit: not one live session is sacrificed, and the new path is proven
in production before anything valuable runs on it.

### Not yet done

No production restart has been performed, and the live dell-5530 agent has not
been touched. `--detached-sessions` is off by default specifically so that
shipping this code cannot change that.

---

## 7. Operator notes

**Enabling.** Add `--detached-sessions` to the agent's arguments. Optionally
`--session-state-root <dir>`; the default is
`<first allowed cwd root>/.terminal-mcp/win-sessions`.

**Inspecting without the agent.** The state directory is plain files: `meta.json`
for what a session is, `out.log` for its transcript, `host.log` for host
diagnostics. All readable while the agent is down, which is the point.

**Cleaning up orphans.** Adoption logs a verdict per session at startup. For
anything reported `ORPHAN_HOST_GONE` or `ORPHAN_PID_REUSED`: confirm the host pid
in `meta.json` is not running, keep `out.log` if the transcript still matters,
then remove the directory. Nothing removes it for you, by design.

**One asymmetry to know about.** Running hosts on POSIX (only tests do) leaves a
zombie per exited host, because `spawn_host` does not keep the `Popen` object to
reap. On Windows there is no such state, and that is where this runs in
production.
