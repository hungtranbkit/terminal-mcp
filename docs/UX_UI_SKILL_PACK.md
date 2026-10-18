# Shared UX/UI skill pack (ux-ui-agent-skills)

Issue #71. One pinned, read-only copy of
**github.com/plugin87/ux-ui-agent-skills** per node, shared by every Claude
Code and Codex session on that node, instead of copying the kit into each
repository with `npx ux-ui-agent-skills init`.

Code: `terminal_mcp/skill_packs.py` (stdlib only). Pin:
`deploy/skill-packs/ux-ui-agent-skills.json`. CLI: `terminal-mcp-skill-packs`.

## What was pinned, and what it actually contains

Audited 2026-10-18 from the tag itself, not from its README:

| | |
| --- | --- |
| Tag / version | `v2.9.8` (latest upstream tag at audit), `package.json` 2.9.8 |
| Commit | `62092bd83bc3df0bcb9a2b3b643305ab11c547da` (2026-10-10) |
| Tree | 640 files, sha256 `7debe021…b44c` over path + content (`tree_digest`) |
| Licence | MIT, `LICENSE` sha256 `c8f16848…1f81`, Copyright (c) 2026 Thientan Soparat. Kept verbatim inside the installed tree. |
| Claude skills | 25 directories with a `SKILL.md` under `.claude/skills` (plus one test fixture that is not a skill) |
| Design systems | 138 (`design-systems/library`) |
| MCP gates | 23 in the kit's own `mcp/gates.mjs` catalogue, 16 of which open a browser |
| "52 objective gates" | the README's number is the **checks in `scripts/accuracy_report.mjs`**, the kit's self-test over its *own* examples. They are not 52 gates you can point at a project page. Terminal MCP runs them as `selfcheck` (below) and reports the real N/52. |
| Runtime deps | none; `devDependencies` are playwright, esbuild, react, react-dom |

The pin is refused if the tag moved (resolves to another commit), if the tree
hash or licence hash differ, or if `package.json` names another version.

## Layout on a node

```
~/.local/share/terminal-mcp/skill-packs/        ($TERMINAL_MCP_SKILL_PACK_HOME)
  bin/ux-ui-gates                                launcher (works after the checkout changes)
  bin/tmcp_skill_packs.py                        standalone copy of skill_packs.py
  manifests/ux-ui-agent-skills.json
  ux-ui-agent-skills/
    releases/2.9.8-62092bd83bc3/kit/             pinned upstream tree, read-only
    releases/2.9.8-62092bd83bc3/node_modules/    playwright (installed --ignore-scripts)
    releases/2.9.8-62092bd83bc3/PROVENANCE.json  repo, tag, commit, tree hash, licence
    current -> releases/...                      what sessions use
    previous -> releases/...                     rollback target
    state.json                                   enabled, history, last gate, last error
~/.claude/skills/ux-ui-kit/SKILL.md              router skill (Claude Code)
~/.codex/skills/ux-ui-kit/SKILL.md               router skill (Codex)
```

Each router directory carries `.terminal-mcp-skill-pack.json`. A `ux-ui-kit`
directory without that marker was not written by Terminal MCP: it is reported
as `CONFLICT` and never overwritten or removed.

## What is NOT changed

- No project file: nothing is copied into a repository, and no `AGENTS.md`,
  `CLAUDE.md` or user preference file is written or edited.
- The upstream plugin's hooks (SessionStart / PostToolUse / Stop) are not
  installed. Hooks fire in every session, UI or not.
- No MCP server is registered by default. `--claude-mcp` (runs
  `claude mcp add --scope user`, only when no server of that name exists) and
  `--codex-mcp` (adds a marker-delimited block to `~/.codex/config.toml`,
  backing it up first, refusing if the table already exists) are opt-in,
  because a registered server is loaded into every session.

## Lazy activation

Both hosts list a user-level skill by its one-line description and read the
body only when a task matches. The router's description says: use only for UI,
frontend or visual-design work. A backend task pays one description line and
nothing else; the 25 kit skills are read one at a time from the pinned tree,
only when the router sends the agent there.

`terminal-mcp-skill-packs activation "<task>"` (and the `task` argument of the
`terminal_skill_pack_status` MCP tool) shows whether a task would be pointed at
the pack.

### Claude Code vs Codex

| | Claude Code | Codex |
| --- | --- | --- |
| Discovery | `~/.claude/skills/ux-ui-kit` | `$CODEX_HOME/skills/ux-ui-kit` (default `~/.codex`) |
| Kit skills | read on demand from `KIT/.claude/skills/<name>/SKILL.md` | same files, read as instructions |
| Doctrine | `KIT/CLAUDE.md`, `KIT/.claude/rules` | `KIT/AGENTS.md` |
| Upstream hooks | not installed | do not exist for Codex |
| Gates | `ux-ui-gates` CLI; MCP opt-in | `ux-ui-gates` CLI; MCP opt-in |

Upstream's Claude plugin (hooks and 25 slash skills registered as a plugin) is
**not** automatic for Codex, and this integration does not pretend it is.

## Brand and project isolation

Precedence, highest first, stated in the router skill: the user's instruction;
the project's AGENTS.md / CLAUDE.md and UI rules; the project's own tokens,
theme, components and brand; then the kit. The kit's tokens and 138 design
systems are references. Gates run against the **project's** files, with the
project's tokens (`--tokens`) and theme (`--theme`); without them the
token/theme gates are `NOT_RUN`, never silently pointed at the kit's own
tokens.

## Gates: real exit codes, fail closed

```
ux-ui-gates review <files-or-dirs> --tokens colors.json --theme theme.css \
    [--focustrap-open '#trigger'] [--component-root dir] [--dark] [--json]
ux-ui-gates gate <gate_name> <path> [args]
ux-ui-gates list
ux-ui-gates selfcheck            # upstream accuracy_report.mjs, the "52"
ux-ui-gates screenshot page.html out.png
```

Each run is `PASS` (exit 0 and the gate looked), `FAIL` (exit 1), `BLOCKED`
(exit 2, or no browser/runtime), `ERROR` (crash/timeout) or `NOT_RUN` (input
missing, skipped, or a gate that printed its usage). `DS_REQUIRE_BROWSER=1` is
set, so a render gate without a browser fails instead of skipping. A review
is `PASS` only when every catalogued gate ran and passed. CLI exit: 0 PASS,
1 FAIL, 2 INCOMPLETE.

Fixtures: `examples/ux-ui-skill-pack/clean` must review PASS 23/23 with
`--focustrap-open '#open-dialog'`, and `examples/ux-ui-skill-pack/broken` must
FAIL. `tests/test_skill_packs.py::test_real_kit_*` runs them for real when
`TERMINAL_MCP_SKILL_PACK_E2E=1` and the pack is installed.

## Operating it

```
terminal-mcp-skill-packs sync [--no-browser] [--claude-mcp] [--codex-mcp] [--force]
terminal-mcp-skill-packs status          # exit 0 READY/DEGRADED/DISABLED, 1 otherwise
terminal-mcp-skill-packs rollback        # swap current/previous; pins until sync --force
terminal-mcp-skill-packs disable|enable  # remove/restore router skills; releases kept
```

- **New node / join:** `deploy/install-node-agent.sh` and
  `install-node-agent-macos.sh` run `sync --best-effort` after `pip install`.
  A failure is logged to `/tmp/terminal-mcp-skill-packs-sync.log` and never
  blocks the join. Opt out with `TERMINAL_MCP_SKIP_SKILL_PACKS=1`.
- **Existing node:** run `sync` once from any checkout at or after this
  change. New sessions see the router skill at their next start; nothing
  restarts.
- **Upgrade:** audit the new tag, update the manifest (tag, commit,
  `tree_sha256`, `tree_files`, licence hash), merge, `sync`. The old release
  becomes `previous`.
- **Rollback:** `rollback`, then `sync --force` to return to the manifest.
- **States:** `READY`, `DEGRADED` (verified, but node/browser missing: render
  gates will BLOCK), `DRIFT` (current is not the pin, e.g. after rollback),
  `TAMPERED` (tree hash changed; `sync` reinstalls), `MISSING`, `DISABLED`.

## Admin visibility

- Per node: `terminal-mcp-skill-packs status` — version, provenance, licence,
  verification, node/browser runtime, router per host, agent compatibility,
  history, last gate result, last error, last selfcheck.
- MCP: `terminal_skill_pack_status` on the Terminal MCP server (this node).
- Fleet: `deploy/node-profile.yaml` declares the pack under `skill_packs`
  (provider `terminal-mcp-pack`), so `terminal-mcp-node-doctor` and
  `terminal_fleet_environment` report it per node as PASS / DRIFT / MISSING /
  SKIPPED with the last gate verdict.
