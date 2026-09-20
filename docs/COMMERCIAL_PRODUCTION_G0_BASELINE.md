# G0 Baseline Reconciliation Evidence

Generated: 2026-09-20T06:52:52.052109+00:00

- Branch: docs/commercial-production-plan-2026-09-20
- Candidate HEAD: a0d7374def4b575551f3a9387cfe6c1a22e2b9fd
- origin/main: 8afbfdb3b5ef3de972b852f3a234b43dcbfbbb27
- Divergence (main-only / candidate-only): 0	3
- Tracked commercial worktree clean before this evidence file: yes (this evidence file itself was the only untracked item)

## Candidate-only commits
```
a0d7374 docs: harden commercial production execution contract
848abec docs: add machine-readable commercial production work packages
f10ced9 docs: add Terminal MCP commercial production plan
```

## Main-only commits
```
(none)
```

## G0 interpretation
This docs worktree is isolated from the dirty legacy Dell checkout. No force/reset/clean was used. Live controller evidence captured on hp-linux:
- Terminal MCP version: 0.13.0
- Deployed/source SHA: 8afbfdb3b5ef3de972b852f3a234b43dcbfbbb27
- Controller repository HEAD: same SHA
- Tracked controller source: clean; two untracked operator artifacts exist (TASK-TMCP-SESSION-HEALTH-001.md, start-node-agent.sh) and are not imported by the runtime.
- Controller process remained stable during the observed BACKEND_UNAVAILABLE incidents; the incidents were compact-sidecar/admission latency, not controller restarts.

The commercial branch is based on that exact production baseline. No force/reset/clean was used. Durable queue evidence is reconciled separately in G1; no task is marked complete from chat prose alone.
