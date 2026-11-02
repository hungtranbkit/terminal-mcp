---
title: Kanzo read-only pilot for Repo Task Board
date: 2026-11-02
execution: implementation-ready
issues: [82, 106]
---

# Goal Capsule

Evaluate Kanzo as an optional, disposable view over the existing Repo Kanban without creating another task authority. Produce a disabled-by-default, one-way projection for the BrandAgent and `hungtranbkit/offline-pos` pilot projects, with truthful source and staleness metadata and a localhost-only operating contract.

# Scope and boundaries

- Terminal MCP/Gateway, GitHub issues, durable task receipts, and session data remain canonical.
- The adapter reads the existing `RepoKanbanService` projection; it does not fetch GitHub independently, dispatch work, close issues, mutate queues, or touch live agents.
- Kanzo's database is disposable. Reconciliation replaces the projection atomically and deterministically, so Kanzo-side edits never flow upstream.
- No unauthenticated Kanzo listener may bind a public interface or be placed behind public ingress. The shipped launcher validates loopback binding.
- No production deployment, service restart, firewall change, tunnel change, merge, or GitHub Actions workflow is part of this slice.
- Kanzo is AGPL-3.0. This repository does not modify or redistribute Kanzo; operators download an upstream release and verify GitHub attestation before use.

# Key technical decisions

1. Generate a Kanzo-compatible SQLite database directly from the already-sanitized Repo Kanban service result. This avoids a writable Kanzo API dependency and preserves the existing allowlist, committed-ref, redaction, and session-join boundaries.
2. Store canonical identity in a projection-only `tmcp_projection` table and in human-readable task descriptions. Kanzo's integer task ID cannot represent arbitrary canonical IDs, so deterministic local IDs are derived from project plus canonical task ID while the original is retained verbatim as metadata.
3. Map `NEW→todo`, `RUNNING→doing`, `REVIEW→review`, and verified `DONE→done`. `BLOCKED`, stale, unknown, waiting-input, and failed states remain non-done and receive explicit tags/description fields; idle sessions never imply completion.
4. Write a fresh database in the destination directory, validate it, fsync it, and atomically replace `kanzo.db`. Repeated reconciliation produces one row per canonical task and cannot duplicate cards.
5. Keep startup separate from Terminal MCP. A launcher checks the Kanzo binary/version, requires `127.0.0.1` or `::1`, uses a dedicated home/port, and never changes existing services.

# Implementation units

## U1 — Deterministic projection

Add `terminal_mcp/kanzo_projection.py` with a pure mapping layer and atomic SQLite writer. Include source URL, canonical ID, source status, last synced time, warnings, evidence, sessions, and a content digest. Validate project allowlist, database integrity, duplicate canonical keys, and cross-project isolation.

Tests: `tests/test_kanzo_projection.py` covers two projects, status truthfulness, idle-not-done, evidence handling, deterministic reconciliation, no duplicates, stale/unknown mapping, redaction inheritance, atomic replacement, and cross-project separation.

## U2 — Operator CLI and safe launch contract

Add `scripts/kanzo-readonly-pilot.py` to build a projection from an explicitly supplied sanitized Repo Kanban snapshot, and `scripts/kanzo-pilot-launch.sh` to start an installed upstream Kanzo binary only on loopback with a separate home and port. Refuse non-loopback listeners and unsafe homes. Do not install, download, update, expose, or daemonize Kanzo automatically.

Tests: CLI tests use synthetic two-project snapshots and a fake Kanzo executable; they assert refusal of public binds and stable output.

## U3 — Pilot documentation and evidence

Add `docs/KANZO_READONLY_PILOT.md` with provenance verification, AGPL obligations, config examples for BrandAgent/offline-pos, reconciliation/restart/backup/restore, rollback, and a GO/NO-GO scorecard. Record that Kanzo has no auth and must never be published through Cloudflare/public ingress.

# Verification contract

- Targeted unit/CLI tests pass from a clean isolated worktree.
- Existing Repo Kanban and dashboard tests pass.
- A locally built or attestation-verified Kanzo release opens the generated two-project database on `127.0.0.1` only; desktop and 390px browser smoke capture screenshots, console errors, and mutation-isolation behavior.
- Restart and a second reconciliation keep canonical task count stable; backup/restore reproduces the same canonical keys.
- Report exact tested SHA, commands and exit codes, CPU/RAM where measurable, card coverage, duplicates/orphans, refresh latency, limitations, and GO/NO-GO recommendation.

# Definition of done

The Draft PR contains the disabled one-way adapter, safety launcher, tests, docs, and available local browser evidence. It does not claim production readiness, does not expose Kanzo publicly, and lists any unavailable real BrandAgent/offline-pos data or node-level staging checks as explicit blockers rather than fabricating results.
