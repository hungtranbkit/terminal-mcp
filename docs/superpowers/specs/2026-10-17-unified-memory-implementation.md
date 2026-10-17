# Approved MVP execution

The user explicitly approved implementation and safe scope on 2026-10-17; no additional spec approval is required. Production remains separately gated.

1. Test-first additive SQLite memory journal, owner namespaces, typed metadata, redaction, role/project authorization, offline expiry, and durable page cursors.
2. Versioned authenticated join/push/pull/search API, controller-only relay, and restart-safe coordinator. Keep legacy fleet exchange separate: unrestricted old metadata APIs never carry new private memory.
3. Explicit approved-root Git/project/service/skill/task/session/note projections, bounded filesystem reads; no transcript or source copy, skill execution, destructive migration, or retention deletion.
4. Wire opt-in node-agent routes plus local operator CLI using existing node bearer authentication. New memory state defaults private; project sharing is explicit configuration.
5. Run regression tests and real HTTP three-node staging including offline/restart/reconnect, pagination, denied principals and secrets; review, commit, push PR, and report exact gates. No production activation.
