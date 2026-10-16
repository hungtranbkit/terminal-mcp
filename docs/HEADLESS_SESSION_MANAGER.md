# Headless Session Manager — incident repair, 2026-10-16

## Scope and evidence

Actual HP controller and ChatGPT sidecar source was
`~/.cache/terminal-mcp/issue52-deploy`, commit
`8249c35ef298d539fbaf1e3eff0952c4eacb7bd3`, selected by systemd drop-ins.
The workspace main checkout (`1b8e7a3`) was not the deployed source.
Changes are based on the deployed commit, on an isolated branch.

A. `dt_9d78e880cad54157` was persisted as DISPATCH_UNCERTAIN with
NO_EXECUTION_EVIDENCE, one dispatch, no submit/execution ACK, zero recovery
attempts. The inspected Dell pane contained a clipped prompt suffix in the
composer. Matching required the entire dispatch needle; a clipped needle
was interpreted as absent. The acceptance watchdog had no terminal deadline.
This proves inadequate observation and unbounded uncertainty, **not** that an
Enter packet was never sent. Dell's deployed node was also older than HP's
issue52 deployment. No live prompt was resent or blindly submitted.

B. Interactive Codex startup can stop on hook review. Workspace trust and
hook-content trust are different. No evidence proves that an unchanged hook's
trust hash was lost. Headless launches disable hooks for this invocation using
supported CLI flags, preserving all trust files and approval policy. Claude
uses `--settings '{"disableAllHooks":true}'`; managed administrator policy
retains its documented precedence. No trust bypass or dangerous permission
flag is used.

C. Public task lookup queried only the queue store even for `dt_` IDs.
It now routes durable identities to the correct store without triggering work.
A dispatch with no evidence after 300 seconds is BLOCKED but retains ownership:
`completed_at=null`. New work cannot replace it. Legacy completion additionally
requires accepted execution and an observed idle state. Bare-name ownership
lookups conservatively detect qualified records, without conflating two
explicitly qualified nodes.

D. The external session-guard timer imported a stale checkout and could delete
old shell panes without checking task ownership; capture failure did not stop
its deletion. Its replacement only checkpoints and releases proven completed
headless runs. It never deletes a tmux pane or working directory. Controller
internal deletion now also invokes the durable ownership guard. A shared
admission lock counts both tmux and headless reservations and prevents name
collisions. BLOCKED/unknown work continues to occupy a slot.

E. `SENSITIVE_SESSION_NOT_CREATABLE` comes from the hard name floor in
`permissions.py`/`lifecycle.py`: case-insensitive substrings root, ssh,
password, secret, database. Existing grants cannot override it. The exact
rejected incident name was not available, so its matched substring is not
asserted. This protection remains unchanged. There is no new admin bypass.

## Transport and lifecycle

Opt-in: `TERMINAL_MCP_HEADLESS_ENABLED=1`; Linux user systemd is required.
The existing lifecycle/read/input permissions, cwd allowlist, explicit denials
and sensitive-name floor apply. Initial version requires default read AND
input authorization for new headless identities; old tmux grants are never
inherited. Remote node routing is explicitly unsupported on this transport.

Call the existing `terminal_turn` MCP tool:

```json
{"action":"headless_start","target":"example-task","agent_type":"codex","working_directory":"/home/kimex/workspace/example","text":"Run the requested checks and explain the result.","idempotency_key":"unique-business-request-v1","node":"local"}
```

`agent_type` supports `codex` and `claude`. Save returned `hd_` task/session ID.
Use `headless_status` (or `task_status`), `headless_checkpoint`,
`headless_cleanup`, and `headless_cancel` with `task_id`.
No arbitrary argv, shell command launcher, hook approval or permission override
is accepted from the client.

Lifecycle: durable READY intent → transactional DISPATCH claim → delivery
ACK → execution ACK/RUNNING → COMPLETED/BLOCKED/CANCELLED → checkpoint → RELEASED.
A repeated key returns the same task; a changed payload returns conflict.
Only the transaction's creator launches. A worker flock plus conditional claim
prevents duplicate execution across controller restarts and concurrent callers.
No automatic redispatch occurs after uncertain launch or worker loss.

Workers run in individual `tmcp-hd_*.service` user units with 2 GiB RAM,
200% CPU, NoNewPrivileges and restrictive umask. This also avoids inheriting
an interactive session's nested namespace/seccomp restrictions. A real test
showed inherited subprocess execution failed with bwrap namespace denial;
a fresh user unit retained Codex sandboxing and successfully wrote its artifact.
Neither host sysctls nor AppArmor policy were modified.

Codex uses `exec --json --disable hooks --sandbox workspace-write` and
`approval_policy="never"`; Claude uses print/stream-json and `dontAsk`.
Provider/auth configuration is reused; keys are never copied into argv or logs.
Structured final outcome must explicitly say completed. ACKs, structured
turn completion and zero process exit are also required. A successful CLI exit
with a business blocker remains BLOCKED. A single printed marker cannot finish
a headless task.

Timeouts distinguish delivery ACK (60 s), execution/no progress (300 s) and
total runtime (3600 s). The worker terminates only its own process group.
Controller reconciliation detects lost workers but holds their work, never
replays it. Cancellation is explicit and cleanup still requires checkpoint;
unknown process exit cannot be assumed cancelled.

State defaults to `$XDG_STATE_HOME/terminal-mcp/headless` (otherwise
`~/.local/state/terminal-mcp/headless`), or operator-configured
`TERMINAL_MCP_HEADLESS_STATE_DIR`. Directory 0700, database 0600, prompt stdin
file removed after launch/finally. Audit contains state/event metadata, not raw
CLI stderr or prompts. Results/checkpoints are private and readable only through
authorized APIs. Cleanup releases bookkeeping; it never removes work files.

## Verification

Red reproductions: clipped composer indefinitely uncertain; incorrect direct
ID lookup; absent headless lifecycle. Regression coverage includes marker-only
completion, business refusal despite zero CLI exit, nonresponsive agent,
restart/lost-worker hold, concurrent idempotency, cancellation, hook flags,
permission floor, checkpoint tampering, and cross-transport ownership.

`docs/evidence/headless-codex-10.json`: ten consecutive real Codex jobs, each
writes a unique artifact checked on disk, reconstructs the manager, repeats
the same key, checkpoints and releases. All ten passed. Earlier failed probes
were not counted: one inherited sandbox failure, one missing systemd PATH.
A separate Claude run read the synthetic artifact using its Read tool,
returned a structured successful result, checkpointed and released.

Broad suite stopped at 5 failures in test_backlog_brief.py after 693 passes.
The same backlog tests fail on production base8249c35 (9 failures/1 pass).
Do not describe the entire repository suite as green.

## Deployment and rollback

Only deploy after targeted tests pass. Preserve previous systemd drop-ins.
Create a pinned checkout and add a later drop-in for each of
`terminal-mcp-http.service` and `terminal-mcp-chatgpt-v1.service` setting
WorkingDirectory, PYTHONPATH and TERMINAL_MCP_HEADLESS_ENABLED=1.
Keep KillMode=process so existing tmux sessions survive controller restarts.
Install `deploy/headless/session-guard-override.conf` with the same pinned
checkout path in `session-guard.service.d/99-headless-safe.conf`.

```sh
systemctl --user daemon-reload
systemctl --user restart terminal-mcp-http.service terminal-mcp-chatgpt-v1.service
systemctl --user is-active terminal-mcp-http.service terminal-mcp-chatgpt-v1.service
```

Rollback: remove only the new `99-zzz-headless.conf` controller/sidecar drop-ins,
then daemon-reload/restart those two services. Existing issue52 drop-ins restore
8249c35. Keep the safe session-guard override while investigating: restoring the
old age-only cleanup reintroduces unsafe deletion. Headless user workers and
SQLite remain intact; never delete their state to roll back code.

Operators can run the safe sweep with the pinned checkout's PYTHONPATH and
`python -m terminal_mcp.headless_cleanup`; inspect units using
`systemctl --user list-units 'tmcp-hd_*'`. The actual deployment record and
live MCP receipts must accompany the final release report.

References: https://developers.openai.com/codex/noninteractive and
https://code.claude.com/docs/en/headless and https://code.claude.com/docs/en/hooks.

## HP release verification

Initial code deployment: `59fecd9`, pinned at
`/home/kimex/.cache/terminal-mcp/headless-59fecd9`. Targeted release suite:
178 passed. Both controller services are active. All original 9 tmux session
identities survived deployment and the subsequent restart test.

ChatGPT's actual Terminal MCP connector created
`hd_9ce08a4348ac4108bd870e98fd7c0b0d`. Both controllers restarted while this
worker was RUNNING. The worker completed and wrote exact verified artifact
bytes. Repeating the same request returned the same ID. Durable audit has
one WORKER_CLAIM and one CLI_SPAWNED. Checkpoint and cleanup were then called
through the connector and returned RELEASED. See
`docs/evidence/headless-release.json` for sanitized receipts/counts.

The periodic safe cleanup override is installed and its oneshot was tested.
The old Dell task is now truthfully BLOCKED/DISPATCH_ACK_TIMEOUT with ownership
retained, one dispatch and no replay. New headless workers are local to HP;
remote Dell headless deployment was not silently implied or performed.

Changed implementation files: `headless.py`, `headless_cleanup.py`,
`headless_outcome.schema.json`, `session_admission.py`, `task_lookup.py`,
`direct_task.py`, `core.py`, `compact_tools.py`, `mcp_app.py`, `server_http.py`
(all under `terminal_mcp/`), and `pyproject.toml`. Tests:
`tests/test_headless.py`, `tests/test_task_lookup.py`,
`tests/test_issue52_supervised_dispatch.py`. Operator assets:
`scripts/headless_acceptance.py`, `deploy/headless/session-guard-override.conf`,
this guide and the JSON evidence files.


## Final acceptance boundary

Final HP code SHA: `7ce39a61892c10dc4664a757e65be7873eaef218`, deployed
from `/home/kimex/.cache/terminal-mcp/headless-7ce39a6` on both services.
Headless discovery was additionally verified through the live ChatGPT MCP
connector after that deployment. The worktree is clean and branch is pushed.

**Overall incident investigation remains BLOCKED on Dell evidence.** The
original submission receipt `17e92858a5714398b39ab11df2bfbbeb` is not in HP's
prompt-submission store; it belongs to the remote node. Read-only SSH attempts
using both the configured Dell hostname and registered node address, including
the existing SSH agent, returned `Permission denied (publickey,password)`.
No account, SSH config, trust policy or credentials were changed to bypass this.

Required next access: SSH as `dell` using an authorized HP identity, or an
operator-provided read-only export of that submission's metadata (ack_state,
enter_count, attempts, execution_started, terminal_state, stop_reason) and
corresponding input audit (timestamps, keys, press_enter, result, reason).
Do not export prompt text, API keys or unrelated logs. With that evidence,
finish the Enter-path root-cause audit and determine a checkpointed recovery
for the existing Dell session. Headless deployment to Dell is not claimed.
HP headless acceptance and its 10/10 results remain valid independently.
