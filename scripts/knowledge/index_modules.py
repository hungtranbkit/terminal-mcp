#!/usr/bin/env python3
"""Index this package into the knowledge map -- completely, by one rule.

WHY THIS EXISTS. The 2026-09-14 token-efficiency benchmark measured that the
briefing could not help in 11 of 18 real bugs for a reason that had nothing
to do with retrieval: the map covered 9 modules / 19 paths against ~150
source files, so the file the fix touched was usually not in it at all
(backlog item 27).

THE OVERFITTING PROBLEM, AND THE RULE THAT ANSWERS IT. This indexing is
being done AFTER seeing which cases the benchmark failed. Indexing exactly
the modules those cases needed would be tuning the system to its own test,
and the resulting number would mean nothing. So the rule is completeness,
not selection:

  * EVERY `terminal_mcp/*.py` file is assigned to exactly one module. The
    script refuses to write anything if one is missing or claimed twice.
  * Groupings are by subsystem, taken from the code's own naming (the
    `work_*`, `node_*`, `fleet_*`, `queue_*` families are already there) --
    not from what the benchmark happens to ask about.
  * Summaries are MECHANICAL: each module's summary is the first sentence of
    its largest file's own docstring, plus the public names its files
    define. Nothing is written to match a bug report's wording, and the same
    rule runs over every module including the ones no case touches.
  * Operational artifacts the system genuinely depends on (`config.yaml`,
    `deploy/`, `scripts/agent`) are indexed on the same basis as code.

A summary generated this way is weaker prose than a human would write. That
is the trade deliberately taken: a weaker summary that nobody tuned is worth
more than a strong one that was.

Run: python3 scripts/knowledge/index_modules.py [--dry-run]
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from terminal_mcp.project_knowledge import ProjectKnowledge  # noqa: E402

# Subsystem -> the files it owns. Every .py in the package appears exactly
# once; the script enforces that rather than trusting this table.
GROUPS: dict[str, list[str]] = {
    "session_ops": ["core.py", "tmux.py", "lifecycle.py", "session_backend.py",
                    "models.py", "bindings.py", "killed_sessions.py"],
    "session_state": ["status.py", "adapters.py", "terminal_wall.py"],
    "prompt_submission": ["prompt_transport.py", "submit_flow.py",
                          "submit_watchdog.py", "verifier.py"],
    "session_registry": ["session_registry.py", "session_knowledge.py",
                         "ephemeral_state.py"],
    "work_ui": ["dashboard.py", "dashboard_nav.py", "webterm.py", "webterm_assets.py",
                "windows_webterm.py"],
    "webauth": ["webauth.py", "webauth_cli.py", "webauth_dashboard.py"],
    "auth": ["permissions.py", "grants.py", "cf_access.py", "enrollment.py"],
    "security": ["access_policy.py", "redaction.py", "audit.py", "network_bind.py",
                 "network_middleware.py"],
    "queue": ["queue_engine.py", "queue_store.py", "queue_service.py", "request_governor.py",
              "queue_loop.py", "queue_task_follower.py", "coordinator.py", "task_migration.py",
              "dor_gate.py"],
    "supervisor": ["supervisor.py", "supervisor2.py", "recovery_engine.py",
                   "recovery_loop.py"],
    "orchestration": ["event_bus.py", "event_wiring.py", "outcomes.py", "lease.py",
                      "verify_queue.py", "worker_registry.py"],
    "integration": ["integration_engine.py", "integration_loop.py",
                    "integration_reviewer.py", "integration_service.py",
                    "integration_store.py", "git_worktree.py",
                    "git_isolation_service.py", "release_store.py",
                    "release_service.py"],
    # TMCP-HARNESS-001. The single execution state machine and everything
    # that decides for it. Grouped rather than scattered into queue/planner/
    # pm because the whole point of the feature is that these eleven files
    # hold ONE answer to "what is this task doing" -- splitting them across
    # the subsystems they replaced would index them as more of the same.
    "harness": ["harness_state.py", "harness_schema.py", "harness_store.py",
                "harness_contract.py", "harness_policy.py", "harness_context.py",
                "harness_engine.py", "harness_scheduler.py", "harness_service.py",
                "harness_pilot.py", "harness_migration.py"],
    "pm": ["pm_router.py", "pm_service.py", "pm_store.py", "pm_summary.py", "pm_recovery.py"],
    "backlog": ["backlog_db.py", "backlog_service.py", "backlog_store.py",
                "project_service.py", "project_identity.py"],
    "planner": ["bug_spec.py", "task_classifier.py", "planner_service.py",
                "planner_store.py", "requirement_contract.py", "test_selection.py"],
    "work_runtime": ["work_store.py", "work_service.py", "work_loop.py",
                     "work_eligibility.py", "work_decompose.py"],
    "work_planning": ["work_spec.py", "work_planning.py", "work_inbox.py",
                      "work_reuse.py", "work_writeback.py"],
    "work_telemetry": ["work_telemetry.py", "work_telemetry_runtime.py",
                       "efficiency_benchmark.py", "metrics.py"],
    "knowledge": ["project_knowledge.py", "context_pack.py", "skill_provider.py"],
    "procedures": ["procedures.py"],
    "policy": ["work_policy.py"],
    "fleet": ["fleet_registry.py", "fleet_loop.py", "fleet_projection.py",
              "fleet_service.py", "fleet_sync.py", "scheduler.py"],
    "capabilities": ["capability_probe.py", "agent_availability.py",
                     "launcher_resolution.py", "host_metrics.py"],
    "nodes": ["node_registry.py", "node_models.py", "node_client.py",
              "node_transport.py", "controller.py", "connection_store.py"],
    "node_onboarding": ["node_agent.py", "node_onboarding.py", "node_profile.py",
                        "remote_connect.py", "lan_discovery.py",
                        "bootstrap_protocol.py", "helper_artifact.py",
                        "rescue_gateway.py"],
    "windows": ["windows_backend.py", "windows_agent.py", "windows_onboarding.py",
                "windows_visible_console.py"],
    "http_api": ["server.py", "server_http.py", "health.py", "contract.py",
                 "logging_setup.py", "schema.py", "maintenance.py"],
    "mcp_surface": ["mcp_app.py"],
    "repo_read": ["repo_read.py", "repo_service.py", "repo_tools.py"],
    "observer": ["observer_app.py", "observer_auth.py"],
    "bridge": ["bridge.py"],
    "ai_usage": ["ai_usage_client.py", "ai_usage_index.py", "ai_usage_local.py",
                 "ai_usage_service.py"],
    "deployment": ["deployment_redundancy.py", "deployment_service.py",
                   "tunnel_diagnostics.py", "tunnel_watchdog.py", "doctor.py"],
    "config": ["config.py"],
}

# Operational artifacts that are part of the system but are not Python. Kept
# separate from GROUPS so the completeness check over the package stays
# exact, and indexed on the same basis: the system depends on them and a
# worker has to be able to find them.
EXTRA_PATHS: dict[str, list[str]] = {
    "procedures": ["scripts/agent"],
    "policy": [".projectflow/policies"],
    "deployment": ["deploy"],
    "config": ["config.yaml", "config.example.yaml"],
}

PACKAGE = "terminal_mcp"
MAX_SUMMARY_CHARS = 300
MAX_NAMES = 10


def _public_names(tree: ast.Module) -> list[str]:
    """Top-level names a caller could use. Real vocabulary, from the code."""
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Name) and target.id.isupper()
                        and not target.id.startswith("_")):
                    names.append(target.id)
    return names


def summarise_module(root: Path, files: list[str], *, style: str = "prose") -> str:
    """One rule, run over every module, whichever style is asked for.

    `prose`  the largest file's own first docstring sentence, and nothing
             else. A summary should be what the authors said the module is.
    `names`  that sentence plus the public names its files define -- a
             symbol index rather than a summary. Kept because it was the
             first rule tried and the benchmark measured both; see
             docs/TOKEFF_BENCHMARK.md for what each scored.
    """
    paths = [root / PACKAGE / name for name in files]
    existing = [p for p in paths if p.exists()]
    if not existing:
        return ""
    largest = max(existing, key=lambda p: p.stat().st_size)
    sentence, names = "", []
    for path in sorted(existing, key=lambda p: -p.stat().st_size):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        if path == largest:
            doc = (ast.get_docstring(tree) or "").strip()
            if doc:
                first = doc.split("\n\n", 1)[0].replace("\n", " ").strip()
                sentence = first.split(". ")[0].strip(" .")
        names.extend(_public_names(tree))
    summary = sentence
    if names and style == "names":
        joined = ", ".join(dict.fromkeys(names))[: MAX_SUMMARY_CHARS]
        summary = (f"{sentence}. Defines: {joined}" if sentence
                   else f"Defines: {joined}")
    return summary[:MAX_SUMMARY_CHARS].strip()


def check_completeness(root: Path) -> tuple[list[str], list[str]]:
    """Every package file claimed exactly once, or nothing is written."""
    on_disk = {p.name for p in (root / PACKAGE).glob("*.py") if p.name != "__init__.py"}
    claimed: list[str] = [name for files in GROUPS.values() for name in files]
    missing = sorted(on_disk - set(claimed))
    duplicated = sorted({name for name in claimed if claimed.count(name) > 1})
    return missing, duplicated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--summary-style", choices=("prose", "names"), default="prose",
                        help="what a module summary contains (see summarise_module)")
    args = parser.parse_args()
    root = Path(args.repo)

    missing, duplicated = check_completeness(root)
    if missing or duplicated:
        if missing:
            print(f"UNCLAIMED package files ({len(missing)}): {missing}", file=sys.stderr)
        if duplicated:
            print(f"claimed more than once: {duplicated}", file=sys.stderr)
        print("refusing to write a map that does not cover the package",
              file=sys.stderr)
        return 2

    knowledge = ProjectKnowledge(root)
    written = 0
    for module, files in sorted(GROUPS.items()):
        paths = [f"{PACKAGE}/{name}" for name in files]
        paths.extend(EXTRA_PATHS.get(module, []))
        summary = summarise_module(root, files, style=args.summary_style)
        print(f"{module:20s} {len(paths):2d} paths  {summary[:70]}")
        if not args.dry_run:
            knowledge.record_module(module, paths=paths, summary=summary,
                                    owner="index_modules")
            written += 1
    if not args.dry_run:
        knowledge.mark_indexed(owner="index_modules")
    print(f"\n{len(GROUPS)} modules, "
          f"{sum(len(f) for f in GROUPS.values())} package files"
          f"{'' if args.dry_run else f', {written} written'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
