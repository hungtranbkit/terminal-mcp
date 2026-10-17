# Terminal Dashboard Node Join Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Deliver an allowlist-authorized, responsive current-machine node join flow with safe helper pairing, explicit first install, metadata-only heartbeat inventory, retry, idempotency, and revocation.

**Architecture:** Require an explicit verified-email role map whenever Cloudflare Access JWT verification is enabled; retain existing auth behavior when Cloudflare Access is not configured. Reuse the existing enrollment store and one-time handles; add a pinned-origin loopback bridge in the existing Go helper, plus honest installer states in the Dashboard. Reuse agent heartbeat/service installers and add a closed metadata inventory contract.

**Tech Stack:** Python/Starlette, PyYAML config loader, embedded dashboard HTML/JS, SQLite enrollment and node stores, Go helper, systemd/launchd/Windows SCM installers, pytest and Go tests.

**Spec:** `docs/superpowers/specs/2026-10-17-terminal-dashboard-node-join-design.md`

## Global Constraints

- Access JWT verification remains RS256 with configured issuer and audience and enforced token lifetime.
- A missing/empty allowlist and unknown identities are denied when Access is configured; role assignment uses only verified JWT email.
- Loopback listeners bind to localhost, validate exact pinned Dashboard origin and loopback Host, use short-lived one-use nonces, and never log pairing credentials.
- First-time service installation must be launched by the user and use native OS permissions; never claim success before native confirmation.
- Inventory is limited to platform/version, capability names, project identifiers, skill names, and health metadata.
- Reuse the enrollment/token/revocation stores and avoid arbitrary shell or filesystem APIs.
- Run requested tests and update `PROJECT_CONTEXT.md`; merge/deploy only with green applicable tests and production Cloudflare credentials.

## Review Focus

- Missing, expired, wrong-audience or non-allowlisted Access identity: `tests/test_cf_access.py` and new role-policy tests.
- Loopback DNS rebinding, untrusted Origin/Host and CORS preflight: helper protocol tests.
- Pairing replay/expiry and transient retry: enrollment API tests.
- Duplicate machine identity and request idempotency: enrollment store tests.
- Inventory containing prompt, env, secret, or filesystem data: node-agent serialization tests.

---

### Task 1: Verified Access role allowlist

**Files:** `terminal_mcp/config.py`, `terminal_mcp/access_policy.py`, `terminal_mcp/dashboard.py`, `config.example.yaml`; tests `tests/test_access_policy.py`, `tests/test_cf_access.py`, new dashboard auth tests.

**Interfaces:** Add immutable `DashboardConfig.access_email_roles: tuple[tuple[str, str], ...]`; add `resolve_access_role(identity, role_map) -> str | None`; `_cloudflare_access_guard` requires this map whenever Access JWT verification is configured and returns 403 for missing or unlisted roles; join/revoke routes require `operator` or `owner`.

- [x] Add tests for normalized email, each role, unknown email, malformed role, and missing verified identity.
- [x] Run the focused tests and confirm new policy tests fail before implementation.
- [x] Parse and validate the YAML map; preserve self-hosted behavior only when Cloudflare Access itself is not configured.
- [x] Enforce allowlist on authenticated Dashboard requests and enforce role requirements on join/revoke endpoints.
- [x] Run `pytest -q tests/test_access_policy.py tests/test_cf_access.py` plus the new route tests.

### Task 2: Idempotent enrollment identity and revocation

**Files:** `terminal_mcp/enrollment.py`, `terminal_mcp/node_onboarding.py`, `terminal_mcp/dashboard.py`; tests `tests/test_enrollment.py`, `tests/test_dashboard_nodes_connect.py` (or the actual enrollment API test module).

**Interfaces:** Add optional stable `machine_id` and `idempotency_key` to enrollment creation/redeem records; repeated requests with matching actor/key return the same pending enrollment; conflicting machine/node ownership returns a stable conflict; revoke invalidates pending handles and existing node credentials.

- [x] Add failing store/API tests for exact retry, conflicting duplicate, replay, revoke-before-redeem, and revoke-after-join.
- [x] Run the focused tests and confirm expected failures.
- [x] Implement schema migration and atomic/idempotent store operations; keep secret values hashed and excluded from list/status models.
- [x] Wire audited dashboard routes to the existing token revocation path and return safe retryable error codes.
- [x] Run enrollment, credential, and route tests.

### Task 3: Loopback helper challenge and pairing

**Files:** `helper/cmd/terminal-mcp-bootstrap/main.go`, new platform-specific loopback listener files under `helper/cmd/terminal-mcp-bootstrap/`, `helper/internal/proto/`; tests in helper package.

**Interfaces:** `GET /v1/challenge` returns helper/platform/version/nonce/expiry; `POST /v1/pair` accepts `{nonce, handle}` once. The browser Origin header is bound to the challenge and server-side handle; the listener binds only `127.0.0.1:47562`, validates loopback Host and secure origin, supports the exact CORS/PNA preflight, and bounds challenge lifetime and request body. Successful redemption persists the Dashboard origin in helper config. Pair calls existing handle redemption and installer/update path.

- [x] Add Go tests for allowed origin/Host, denied remote/alternate Host, replay, expiry, malformed body, and nonce consumption.
- [x] Run `go test ./...` in `helper` to establish the expected red tests.
- [x] Implement loopback protocol and connect pairing to existing one-time redemption without logging credentials.
- [x] Ensure Windows continues through SCM/UAC and first-run behavior remains explicit; add OS-gated service-manager behavior for supported platforms or route users to the existing explicit native installer.
- [x] Run helper tests and cross-compile/build checks for supported OS targets.

### Task 4: Metadata-only inventory heartbeat

**Files:** `terminal_mcp/node_agent.py`, `terminal_mcp/windows_agent.py`, `terminal_mcp/node_registry.py`, `terminal_mcp/dashboard.py`; tests `tests/test_node_agent.py`, `tests/test_windows_agent.py`, `tests/test_node_registry.py`.

**Interfaces:** `collect_public_inventory() -> dict[str, list[str] | str]` returns only the spec's metadata allowlist; heartbeat stores/returns the sanitized inventory under a versioned field; HTTPS/WSS destinations remain pinned to controller configuration.

- [x] Add tests proving permitted inventory is serialized and prompts, environment values, file contents, tokens, and paths are absent.
- [x] Run focused tests and confirm the inventory contract failures.
- [x] Implement schema-versioned, allowlisted inventory collection and sanitization on Linux/macOS and Windows agents.
- [x] Preserve outbound TLS, bearer auth, bounded retry and existing health reporting.
- [x] Run node-agent, registry, heartbeat relay, and Windows-agent tests.

### Task 5: Responsive Join this machine Dashboard

**Files:** `terminal_mcp/dashboard.py`, `terminal_mcp/helper_artifact.py`, `deploy/install-node-agent.sh`, `deploy/install-node-agent-macos.sh`, `deploy/install-node-agent.ps1`; tests `tests/test_dashboard_mobile_portrait.py`, new dashboard join tests, helper installer tests.

**Interfaces:** Add a dashboard join-state endpoint and UI flow: detect installed helper by loopback challenge; otherwise offer the manifest-verified Windows helper or a paired Linux/macOS user-run shell installer with explicit consent; after challenge, create one-time pairing handle, submit to helper, poll enrollment progress, and render verified ready/retry/revoke states.

- [x] Add route/UI contract tests for mobile and desktop, helper absent, helper present, permission pending, retry, ready and revoke states.
- [x] Run targeted tests and verify they fail before implementation.
- [x] Add the responsive UI and APIs; preserve the existing manual onboarding fallback and never equate download with install completion.
- [x] Ran HTML/route contract tests. Live browser QA was attempted but unavailable: browser gateway surfaces were absent, Chrome DevTools could not connect to loopback CDP, and local Playwright Chrome crashed before opening a page.
- [x] Run the relevant dashboard and onboarding suites.

### Task 6: Cloudflare deployment guidance, handoff, and final verification

**Files:** `deploy/cloudflare/` reviewed config/runbook, `PROJECT_CONTEXT.md`, relevant docs, all files above.

- [x] Add a production checklist for Google IdP, Access application/audience, email policy and allowlist, bootstrap ingress, rollback, and verification; do not write production values/secrets.
- [x] Run focused tests across auth, enrollment, helper, agents, and Dashboard; run `git diff --check`.
- [x] Run UI browser QA at phone and desktop widths in an authorized test tab when available; inspect console/network and close only task-created tabs.
- [x] Update `PROJECT_CONTEXT.md` with architecture, changed files, actual tests, branch/commit, deployment status, and blockers.
- [x] Commit the implementation on `feat/p0-dashboard-node-join-20261017`.
- [x] Checked production credential availability and public endpoints. No Cloudflare API/Wrangler credentials or verified tunnel account credentials were available. Google IdP/Access policy and live tunnel config remain unverified; merge/deploy are blocked by the required production credential/configuration gate.
