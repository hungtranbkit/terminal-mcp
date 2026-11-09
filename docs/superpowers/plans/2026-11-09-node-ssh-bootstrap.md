# Node SSH Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Keep this feature branch isolated from other PR/worktrees.

**Goal:** Add private, node-scoped managed SSH from authenticated Terminal MCP to an existing linked node, with real verification and browser terminal reuse.

**Architecture:** A dedicated SSH profile/key store in the controller, a fixed authenticated `/v1/ssh/*` operation in node-agent, matching `NodeClient` methods, controller-mediated MCP/dashboard routes keyed only by `node_id`, existing `remote_connect` host-key pinning and existing `webterm` pump/guards. No arbitrary shell operation is added to the node-agent.

**Safety:** Admin-only setup/revoke via existing dashboard mutation guard; status/verify through read guard. The node agent modifies only a uniquely tagged public-key line. It does not install packages, change service/firewall policy, or elevate. Missing admin capability is `needs_admin`; unsupported platform/old agent, offline and host-key/auth errors remain distinct. Keep current tmux/project services intact.

## File map
- `terminal_mcp/node_ssh.py` (new): key/profile persistence, node-address resolution, strict OpenSSH probe/verify, SSH PTY process.
- `terminal_mcp/node_ssh_agent.py` (new): fixed platform preflight/key add/revoke, atomic authorized-key edits, local locking and redacted errors.
- `terminal_mcp/node_agent.py`, `terminal_mcp/node_client.py`, `terminal_mcp/controller.py`: authenticated, allowlisted remote operation and local parity.
- `terminal_mcp/mcp_app.py`, `terminal_mcp/compact_tools.py`: `node_ssh_status/setup/verify/revoke/connect` actions scoped to `node_id`.
- `terminal_mcp/dashboard.py`, `terminal_mcp/webterm.py`: compact Vietnamese node controls and SSH-backed existing WebSocket/PTY path with auth/origin checks and detach behavior.
- `tests/test_node_ssh.py`, `tests/test_node_ssh_agent.py`, MCP/dashboard tests: safety, platform errors, routing and browser guards.
- `docs/multi-node.md`, `README.md`, `PROJECT_CONTEXT.md`: operator flow and verified support matrix.

## Tasks (TDD)
1. **Managed key/profile store:** Complete. Profiles use per-node 0700 directories, 0600 private key/public key/pin/profile files, node ID validation, strict host pinning, and exact confirmed-key revoke.
2. **Node preflight/key operation:** Complete for Linux/macOS where a non-root node-agent user can safely edit its own authorized_keys and sshd policy/listener/host key are verified. Windows returns `needs_admin` until a real Windows ACL/service helper exists. No installation, elevation, sshd restart, firewall, or policy changes are attempted.
3. **Authenticated node transport:** Complete in source via a fixed `manage_ssh` operation on authenticated `/v1/ssh/manage`, strict request allowlist, local/remote NodeClient implementations, bounded timeout, and old-agent `unsupported` mapping.
4. **Controller SSH profile/verification:** Complete in source. Target is resolved from registered node endpoint; one dedicated identity, isolated OpenSSH config, strict pinned host key, BatchMode, nonce/hostname/whoami proof, timeout/error mapping, persistent verification timestamp. Owner's existing HP→Dell canary remains separate and untouched.
5. **MCP actions:** Complete in source through `terminal_turn` action `node_ssh`; only `node_id`, allowlisted operation, and bounded structured command arguments are accepted. Secrets are never serialized in result fields.
6. **Dashboard/WebSocket:** Complete in source through guarded status/setup/verify/revoke routes and existing authenticated terminal WebSocket/PTY path. Local browser QA rendered controls; local-only endpoint correctly remained disabled. The only console error was the test app's missing favicon.
7. **Docs/regression:** Complete locally. Focused SSH/MCP/node-agent/webterm suite: 156 passed; dashboard: 136 passed; token rotation: 33 passed; compileall and `git diff --check` passed. No full suite run.
8. **Canary/rollout:** Blocked by actual runtime capability: the connected public Terminal MCP version is 0.13.0 and responds `INVALID_ACTION` for `node_ssh`; no supported in-band package rollout operation is exposed. Do not restart or mutate services outside a verified release path. Dell/HP owner-reported SSH canary evidence is documented but not claimed as product-feature acceptance. Other node live setup, browser SSH terminal, Windows and offline nodes remain unverified. Next: push this branch/open PR, release through supported package/service deployment, then run the staged live matrix and browser SSH attach test.

## Review focus
- Old node-agent returns 404 without being mislabeled as offline.
- Key rollback never removes any user-owned `authorized_keys` line or overwrites config.
- Host-key pin comes from authenticated node data, and a mismatch blocks setup/connect.
- Removing a browser WebSocket never kills tmux/remote jobs.
- Windows service/account/admin-key ACL handling is proven on Windows; Linux fakes do not count.
