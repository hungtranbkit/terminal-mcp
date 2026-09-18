# Supervisor / watch / queue: one lifecycle

What drives what, which switches gate it, and the four defects that made parts of
it silently inert.

---

## 1. The symptom this started from

Live controller, read from `supervisor.db`:

```
supervisor loop_running=true  poll_interval=20s
watch_count=10  enabled_watch_count=0  stalled_count=3
```

Ten watches, none running, and a status surface reporting 3 problems out of 9.

| target | source | disabled_reason | iterations |
| --- | --- | --- | --- |
| `wtest`, `win2` | manual | `target_missing` | 1 |
| `hp1`, `hp2`, `hp3-work`, `hp-work` | manual | `target_missing` | 1 |
| `terminal-mcp-main` | manual | `max_iterations_exceeded` | 223 |
| `mcp-work` | manual | `max_iterations_exceeded` | 121 |
| `gatefix2-work` | manual | `max_iterations_exceeded` | 20 |
| `mesflow-dell` | manual | `manual_unwatch` | 1 |

Six of those sessions were alive the whole time, on the hp and Windows nodes.
Each was declared missing on its **first** poll. One row — `mesflow-dell` — is a
person's decision and must stay off.

---

## 2. Who runs what

There is **one** background thread per concern, and no new ones were added:

| Loop | Drives | Gate |
| --- | --- | --- |
| `SupervisorLoop` | `SupervisorService.run_once` → reconcile, then poll each enabled watch | `supervisor.enabled` |
| `QueueLoop` | the event drain, then `QueueEngine.tick` per opted-in lane | `queue.enabled` |
| `RecoveryLoop` | node/session reconciliation | `auto_recovery.enabled` |
| `IntegrationLoop` | handoff claims | `integration.loop` |

The event drain is a **step inside `QueueLoop`**, not a thread. Two schedulers
acting on the same lanes would leave the lane's claim lease arbitrating races on
every cycle; there is one scheduler.

```
run_once:  _sync_config_watches -> reconcile_watches -> poll each enabled watch
cycle:     drain_once -> refresh heartbeat -> tick each opted-in lane
```

`reconcile_watches` runs *before* polling deliberately, so a watch restored in a
pass is also polled in that pass — recovery costs one cycle, not two.

---

## 3. Watch lifetime

A manual watch stays logically enabled until someone unwatches it. Iteration
counters bound a *run*, never the watch.

```
             poll ceiling / repeated failure / target gone / node down
  enabled  ------------------------------------------------------------>  disabled
     ^                                                                       |
     |          reconcile: recoverable reason AND target alive AND            |
     +--------- backoff elapsed AND attempts < cap  <-----------------------+
                                  (never for a deliberate exclusion)
```

**Recoverable** — brought back automatically, with exponential backoff and a
bounded attempt count: `max_iterations_exceeded`, `target_missing`,
`same_failure_limit_exceeded`, `access_denied_or_error`, `node_unreachable`,
`node_not_found`, `ambiguous_target`.

**Deliberate** — never resurrected by any code path: `manual_unwatch`,
`autonomous_completion_blocked_no_verifier`, `autonomous_verification_failed`. A
person said no; that stands until a person says otherwise.

An **unrecognised** status error maps to a *recoverable* reason on purpose. A
watch must never go permanently blind because the fleet grew an error code the
table has not met.

Re-enabling also resets `iteration_count`, or a watch restored at
`iteration_count >= max_iterations` disables itself again on its next quiet poll
and the fix looks like it did nothing.

---

## 4. Node-aware routing, and why local comes first

`_status_for` is the single point where a watch's target is observed. Both
polling and reconciliation use it. When they disagreed, a watch could be disabled
by a poll that asked the whole fleet and never revived by a probe that only asked
the local node — disabled forever by two functions that were each individually
correct.

```
binding            -> local terminal_status_bound   (bindings are node-scoped)
"node/session"     -> fleet only                    (local tmux has no slashes)
bare name          -> LOCAL first, then fleet
```

The order is load-bearing. Routing bare names through the controller
unconditionally makes every local watch depend on the local node being registered
and ONLINE; a stale heartbeat then answers `SESSION_NOT_FOUND` for a session
running right here. That regression broke
`test_supervisor_tools_registered_and_functional` during this work, and would
have disabled the three local watches that still worked in order to fix the six
remote ones. `queue_loop.py` documents the same hazard and injects a heartbeat
refresher for it.

The existing ten rows need **no re-keying**: bare names resolve through the
controller as they are.

Fleet errors now carry their own reasons, so reconciliation can tell a node
outage from a revoked grant:

| controller error | disable reason |
| --- | --- |
| `NODE_UNREACHABLE` / `NODE_OFFLINE` | `node_unreachable` |
| `NODE_NOT_FOUND` | `node_not_found` |
| `SESSION_NOT_FOUND` | `target_missing` |
| `AMBIGUOUS_SESSION` | `ambiguous_target` |
| anything else | `access_denied_or_error` |

---

## 5. `supervisor_status` counts

Every disabled watch now falls into exactly one bucket, and a test asserts that
`recoverable + intentional == disabled`:

```
watch_count, enabled_watch_count, disabled_watch_count,
recoverable_disabled_count, intentionally_excluded_count,
disabled_reasons{watch_key: reason}, stalled_count, state_counts
```

`stalled_count` keeps its original meaning (the two ceiling reasons) for existing
consumers; it is no longer the only view, which is what made six invisible
watches look like three problems.

---

## 6. The event drain

`event_wiring.py` published every queue transition onto the bus and nothing ever
claimed one. The bus was write-only: events accumulated forever, a task whose
`depends_on` edge had just been satisfied waited for whatever cycle happened to
notice it, and the dead-letter machinery could not fire because nothing had ever
claimed an event to fail.

`QueueEventDrain.drain_once()` claims a bounded batch, acts, then acks or fails
each. Three gates, all of which must be open:

1. `queue.enabled` — the loop itself
2. `queue.drain_enabled` — **default False**; enabling auto-dispatch must not
   silently also start consuming the bus
3. `queue_lanes.auto_dispatch_enabled` — re-checked here, not redundant: an event
   names its lane directly, so without it the drain would be a way into a lane
   that never opted in

Actionable types are the ones that can change what a lane should do next
(`TASK_COMPLETED/FAILED/BLOCKED`, `WORKER_DONE`, `VERIFY_PASS/FAIL/BLOCKED`,
`LEASE_RELEASED/EXPIRED`, `TASK_HANDOFF`). `TASK_CREATED/READY/CLAIMED/STARTED`
describe the queue's own forward progress and are consumed without action —
ticking a lane because it just started something is wasted work at best and a
second actor poking a mid-transition lane at worst.

Safety properties:

- **At-least-once**: a drain that dies after acting and before acking sees the
  event again. `tick()` makes one transition per call and reconciles stale claims
  first, so a repeat is a no-op or the next legitimate step.
- **One tick per lane per pass**: ten completions in a lane are one tick.
- **Bounded batch**: a large backlog cannot stall lane dispatch.
- **`fail()` not `ack()` on error**, or attempts and dead-lettering never engage.
- **An unreadable bus never stops dispatch** — dispatch is the loop's real job.
- **Concurrency**: the bus's `claim_next` (BEGIN IMMEDIATE + lease) partitions
  events across drains; a local lock only stops one instance re-entering itself.

---

## 7. Supervisor v2: advancing only on proved submission

`execute_send` already had claim-once, CAS `approved -> sent` before the send, a
durable idempotency key, lease validation, stale-decision and identity-mismatch
holds, and restart reconciliation of `observing` actions. What it got wrong was
reading the transport's answer: it checked `submit_status == "SUBMIT_UNCONFIRMED"`
— a denylist — so two results were treated as success.

- `TEXT_SENT` — text reached the composer, Enter's effect never established.
  `to_legacy_submit_status` deliberately preserves this spelling rather than
  folding it into the unconfirmed bucket, so a `!=` check reads it as a win.
- **no delivery field at all** — a shape the check had not met.

Both advanced the action to `observing` and incremented the auto-action count: an
autonomous chain marching on after a prompt that may never have been submitted.

Now `adapters.is_submission_confirmed`, a positive allowlist beside
`DELIVERY_STATES`, so there is one definition of "confirmed". A state added to
that vocabulary without thinking about autonomy defaults to *not proved*. The
`stop_reason` keeps its existing spelling (`submit_unconfirmed`) because
dashboards and runbooks key on it, and the specific state is already durable in
`send_result`.

This consumes the tmux transport's own verdict rather than reimplementing
acceptance detection. `prompt_transport.SubmissionReceipt` is explicitly a target
shape for a future non-tmux transport, not today's source of truth, so
`adapters.py` is the seam.

---

## 8. Wiring order

`build_mcp()` defaulted `controller` *after* constructing `RecoveryEngine` and
`RecoveryLoop` with it. `server.py` builds the stdio surface by calling
`build_mcp()` bare, so on that surface both were constructed with
`controller=None` — present in the tool list, holding nothing to route with.
Auto-recovery and `reconcile_node` were silently non-functional there.

The controller is now defaulted first. A wiring-order defect produces no error
and no log line; the feature simply does nothing, so it has a test that asks what
the built objects actually hold.

---

## 9. What still needs a real node

Everything above is proved locally, against real tmux sessions and a real
EventBus, with a **simulated** fleet. Not yet proved:

- the six production watches actually recovering once the controller runs this
  code — that needs a controller restart, which is a production rollout
- the drain's behaviour against a real multi-node bus under load, with
  `queue.drain_enabled` turned on
- whether `max_iterations` / backoff constants are right for real workers

Defaults are chosen so that shipping this changes nothing until someone opts in:
`queue.drain_enabled=False`, `queue.enabled=False`, `supervisor.v2_enabled` and
per-lane `auto_dispatch_enabled` unchanged.
