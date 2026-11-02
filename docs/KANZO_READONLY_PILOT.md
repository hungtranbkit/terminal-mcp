# Kanzo read-only pilot

This pilot evaluates [Kanzo](https://github.com/emanuele-em/kanzo) as an optional UI over Repo Task Board #82 and ProjectFlow #106. It is a **one-way disposable projection**, not a queue, dispatcher, task registry, or completion authority.

## Hard safety boundary

- Kanzo has no authentication. Bind it to `127.0.0.1` or `::1` only. Never put it on Cloudflare/public ingress, a public IP, or an untrusted LAN.
- Terminal MCP/Gateway, GitHub, durable task receipts, and sessions remain canonical. The adapter has no mutation path back to them.
- Kanzo-side card edits are local notes only and are overwritten by the next reconciliation. They never dispatch work, close issues, or mark canonical tasks done.
- Use a dedicated home and port. Do not point the pilot at an existing Kanzo home.
- `DONE` maps to Kanzo `done` only when evidence is present and no QA-failure/missing-evidence warning exists. Idle sessions never imply done.
- The launcher disables Kanzo's HTTP MCP endpoint. No live BrandAgent or Offline POS agent should connect to this pilot.

## Upstream provenance and license

Kanzo v0.3.0 is AGPL-3.0. This integration does not modify or redistribute Kanzo. If an operator modifies Kanzo and offers it over a network, the AGPL source-availability obligations apply; consult counsel for the deployment's exact obligations.

Download the asset for the node architecture from the upstream release and verify both the published digest and GitHub artifact attestation before extraction:

```bash
gh release download v0.3.0 --repo emanuele-em/kanzo --pattern 'kanzo-x86_64-unknown-linux-musl.tar.xz*' --dir /tmp/kanzo-v0.3.0
cd /tmp/kanzo-v0.3.0
sha256sum -c kanzo-x86_64-unknown-linux-musl.tar.xz.sha256
gh attestation verify kanzo-x86_64-unknown-linux-musl.tar.xz --repo emanuele-em/kanzo
```

The v0.3.0 GitHub release metadata reports SHA-256 `912469c510ee3d704a2f5d59bf631c8f60c320fbe82f50f72868019c141dd428` for that Linux x86_64 archive. Do not use an unverified installer pipe.

## Projection input

Use `snapshot_from_repo_kanban()` in `terminal_mcp.kanzo_projection` inside an authenticated/offline operator process to select the allowlisted `brandagent` and `offline-pos` Repo Kanban entries. The projection layer must not read repositories, GitHub, queues, or the session database directly.

For replayable staging tests, the CLI accepts an explicit sanitized snapshot containing the `projects` returned by Repo Kanban plus each project's `cards` response:

```bash
uv run python scripts/kanzo-readonly-pilot.py project \
  --snapshot /run/user/$(id -u)/tmcp-kanzo-snapshot.json \
  --home /home/operator/.local/state/tmcp-kanzo-pilot \
  --synced-at 2026-11-02T12:00:00Z
```

The output includes a digest and counts. `tmcp-projection.json` records `authority: terminal-mcp`, `direction: one-way`, and `disposable: true`. Re-running with the same source is idempotent; changed input atomically replaces `kanzo.db`.

Status mapping:

| Canonical state | Kanzo column | Truth-preserving tag |
|---|---|---|
| `NEW` | `todo` | owner |
| `RUNNING` | `doing` | owner |
| `REVIEW`, `QA_PENDING` | `review` | `waiting-qa` |
| verified `DONE` | `done` | `verified` |
| unverified `DONE` | `review` | `qa-pending` |
| `BLOCKED`, `WAITING_INPUT`, `FAILED`, `STALE`, `UNKNOWN` | `todo` | explicit source state |

## Isolated launch

After projection and binary verification:

```bash
scripts/kanzo-pilot-launch.sh \
  --binary /opt/kanzo-v0.3.0/kanzo \
  --home /home/operator/.local/state/tmcp-kanzo-pilot \
  --listen 127.0.0.1 \
  --port 14242
```

The launcher refuses non-loopback addresses, broad/shared homes, invalid ports, a missing projection database, or a non-executable binary. It does not install a service, alter a firewall, create a tunnel, or touch Terminal MCP processes.

## Backup, restore, and rollback

Stop only the isolated Kanzo pilot process, then copy the dedicated home with mode-preserving tools. Restore into a new dedicated directory and compare the manifest digest and `tmcp_projection.canonical_key` rows before launch. A reconciliation from the canonical snapshot is the preferred restore.

Rollback is removal of the isolated process and dedicated pilot home. No canonical data changes, services, queues, auth settings, firewall rules, or tunnels need rollback because this slice never mutates them.

## Acceptance evidence and recommendation

Record for each run:

- exact Terminal MCP and Kanzo SHAs/versions and successful attestation;
- named project/card coverage, orphan count, duplicate count, and source links;
- blocked/stale/waiting-QA/verified mapping checks, including idle != done;
- reconciliation and restart latency, CPU and RSS;
- desktop 1366×768 and mobile 390×844 screenshots, console errors, overflow, keyboard/focus, contrast, and tap targets;
- attempted Kanzo mutation followed by reconciliation, proving no upstream effect;
- listener addresses from `ss -ltnp`, proving loopback only.

Current recommendation is **NO-GO for production** until a real authorized two-project staging run meets those checks. Even then, Kanzo should remain an optional view: its unauthenticated writable UI and four-column state model lose fidelity compared with the authenticated Repo Task Board unless the one-way metadata convention is maintained.
