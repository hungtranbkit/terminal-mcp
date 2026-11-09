# Node-scoped SSH bootstrap — design

## Intent
Let an authenticated Terminal MCP controller establish a managed, private SSH path to an already registered node, so an operator can verify access and open a browser terminal without visiting each headless machine.

## Scope and safety
- The dashboard and MCP accept a registered `node_id`; the controller resolves its address, platform and current authenticated node client. Callers cannot supply an IP, username, shell command for bootstrap, or `authorized_keys` path.
- A fixed, authenticated node-agent operation reports SSH preflight and adds/removes one controller-managed public key. The node agent never exposes general shell execution through this setup API. Existing user keys and config are preserved. No automatic privilege escalation, sudoers change, public firewall rule, or blanket auth relaxation.
- The controller stores a per-node Ed25519 private key under a 0700 directory with 0600 file permissions; the node sees only the public key. Profiles contain non-secret address/user/port, host-key fingerprint, timestamps, and last result. Audit/log data excludes keys and bearer tokens.
- Host keys are returned by the authenticated node channel, fingerprinted, and pinned through the existing `remote_connect.HostKeyStore`/known-hosts machinery. A changed key is a hard mismatch.
- Existing WebTerminal routes, authentication/origin checks, and `pump_websocket` remain the browser terminal. SSH-backed attach reuses this route; browser close detaches its viewer and does not stop the remote shell/job. Linux/macOS use a stable tmux attach where available. Windows remains explicitly unsupported for detached SSH terminals unless the existing node-side terminal backend can provide the same lifetime guarantee.
- `sshd` installation, service enablement, ACL/firewall changes require existing admin capability. Without it, return `needs_admin` and a short platform-specific remediation. Do not treat ONLINE heartbeat as admin authorization or SSH success.

## Approaches considered
1. Reuse the existing manual SSH bootstrap and ask for passwords/private keys. Rejected: it targets unregistered machines and cannot recover a linked node without credentials.
2. Add arbitrary command execution to node-agent HTTP. Rejected: it turns the authenticated node API into a generic shell and bypasses the requested fixed-operation boundary.
3. Add a node-scoped fixed SSH-management operation to the authenticated node transport, persist a managed controller key/profile, and reuse SSH host-key/PTY/webterminal code. Chosen: smallest fit with existing node identity, auth, key-pinning, audit, and browser terminal components.

## Data flow
1. User selects a node in Nodes UI or `terminal_turn` passes `node_id`.
2. Controller requires admin/CSRF for setup/revoke and read permission for status/verify; it resolves `NodeRegistry` and `client_for(node_id)`.
3. Node agent returns platform-specific preflight over its bearer-authenticated channel.
4. Controller creates/loads its node-specific keypair and sends only the public key to the fixed setup operation. The node validates key shape, writes only the tagged managed entry atomically, and returns its SSH host public key from the local sshd configuration.
5. Controller pins that exact authenticated host key, then performs a real bounded `BatchMode=yes`, strict-host-key SSH identity check with a nonce. Profile is `verified` only on matching hostname/user/nonce/exit code.
6. MCP can verify/run an explicit bounded command. Browser opens an SSH-backed terminal through the existing WebSocket route; disconnect detaches the viewer.
7. Revoke removes only this feature's key entry, deletes the controller private key/profile after node confirmation, and refuses on host-key mismatch or unreachable node.

## Known constraints
- Existing deployed node agents may lack the new fixed endpoint; report `unsupported` and ship the package. Do not bypass a 404 or authentication rejection.
- A heartbeat is not a proof of SSH readiness. Per-node setup, SSH verification, browser terminal, and Windows validation are reported separately.
- Offline nodes remain pending/unverified. No fleet-wide deployment or restart is inferred from source changes.
