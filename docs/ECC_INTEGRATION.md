# Everything Claude Code (ECC) project integration

Audited against [`affaan-m/ECC`](https://github.com/affaan-m/ECC) at commit
`76aaa77df6adc5b889e689007bf99b9a4f74af34` (`VERSION` 2.2.3) on
2026-10-19. ECC is optional; it does not replace Terminal MCP's task queue,
readiness checks, verification gates, or dispatch policy.

## Project-local install only

Terminal MCP's helper copies selected `SKILL.md` packages into the current
project's Claude and/or Codex skill discovery roots. It does not run the ECC
installer, copy hook payloads, edit settings/configuration, or write to
`~/.claude`, `~/.codex`, or `CODEX_HOME`. Existing destination skill names
block the whole plan; the helper does not overwrite them. No selected skill is
installed by default in a project.

Review an ECC checkout before using it. The helper's preflight confirms the
source skill directories exist and are contained in the checkout, rejects
symlinked payloads and destination collisions, and checks that the project
root exists. It prints the project-local paths first. Dry-run is the default;
`--apply` is required to copy.

```sh
# Obtain/review ECC source separately; this helper does not clone or execute it.
git clone https://github.com/affaan-m/ECC.git /path/to/ecc

# Preview the default workflow subset, for both Claude Code and Codex.
terminal-mcp-ecc-setup --source /path/to/ecc --project . --target both --dry-run

# After reviewing the exact skills and destinations, explicitly apply.
terminal-mcp-ecc-setup --source /path/to/ecc --project . --target both --apply

# Or choose a host and an explicit skill set.
terminal-mcp-ecc-setup --source /path/to/ecc --project . --target claude \
  --skills tdd-workflow,security-review --dry-run
```

Claude skill files go under `.claude/skills/<skill>/`; Codex skill files go
under `.agents/skills/<skill>/`. These paths are scoped to the project. The
helper accepts only lowercase alphanumeric/hyphen/underscore skill ids and
copies only those named skill directories. It never copies ECC hooks or other
harness configuration. Review and run skills according to your normal agent
permission settings.

## Workflow provider

`EccProvider` is read-only. A project opts into it by selecting `provider:
"ecc"` in `WorkflowPolicy` and enabling individual stages. Missing skills
fall back to the existing workflow. Codex invocations use `$skill-name` and
Claude Code uses `/skill-name`. Skill output is evidence only; it cannot mark
a task complete. The optional node profile entry reports local availability
without making ECC a node requirement.

## Verification boundaries

This repository tests the setup planner and copier with temporary project and
source trees. It does not vendor ECC, run ECC hooks, install dependencies, or
deploy any production changes. ECC source version is reviewed by the operator
when they select a checkout; inspect its current [installation guide](https://github.com/affaan-m/ECC#readme)
before use because upstream changes over time.
