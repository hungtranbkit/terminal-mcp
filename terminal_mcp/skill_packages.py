"""Loading `skills/<id>/SKILL.md` off disk without opening a traversal hole.

THE SINK THIS GUARDS

A caller supplies a skill id; the id is joined onto a root; the result is
read. That is the classic path-traversal shape, and getting it wrong means a
tool call that says `register_skill("../../../../etc/shadow")` reads whatever
the service can read. Three independent rules, so no single mistake is
sufficient:

  1. THE ID IS A SLUG. `agent_registry.valid_slug` allows lowercase letters,
     digits, `-` and `_` only. `..`, `/`, `\\`, a NUL and an absolute path are
     all rejected before a path is constructed at all. This is the rule that
     actually holds; the next two exist because defence that depends on one
     check is not defence.

  2. CONTAINMENT IS RE-CHECKED AFTER RESOLUTION. The root and the candidate
     are both fully resolved -- symlinks included -- and the candidate must
     still be inside the root afterwards. Checking before resolution is the
     standard bug: a symlink inside an approved root pointing at `/etc` passes
     a textual prefix test and fails this one.

  3. THE ROOT MUST BE APPROVED. Roots come from configuration, never from the
     caller, so a request cannot widen its own search path.

READS ARE BOUNDED

A skill body is a prompt. `MAX_SKILL_BYTES` caps it, and the file is read
through a bounded read rather than `read_text()`, so a pathological file
cannot exhaust memory before anyone looks at its size. A file over the limit
is refused by name; it is never silently truncated, because half a prompt is
a different prompt.

NOTHING HERE IS IMPLICIT

No call scans a directory as a side effect of doing something else, and
nothing is auto-registered at startup. `discover` exists so an operator can
SEE what is available, and it still reads only the front matter it needs.
Registration is always a separate, explicit act naming one skill.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .agent_registry import AgentRegistryError, valid_slug

#: The file that makes a directory a skill package. Matches the convention
#: gstack and Claude Code skills already use (see skill_provider.py), so an
#: existing tree can be registered without being restructured.
SKILL_FILE = "SKILL.md"

#: A skill body is a prompt, not a payload. Generous for prose, far below
#: anything that could be used to exhaust memory.
MAX_SKILL_BYTES = 256 * 1024

#: How many packages `discover` will report. A listing is for a human.
MAX_DISCOVERED = 500

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FIELD = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")


class SkillPackageError(AgentRegistryError):
    """A refusal with a reason the caller can act on."""


@dataclass(frozen=True)
class SkillPackage:
    skill_id: str
    version: str
    name: str
    summary: str
    body: str
    path: str
    content_sha: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"skill_id": self.skill_id, "version": self.version, "name": self.name,
                "summary": self.summary, "path": self.path, "content_sha": self.content_sha,
                "metadata": self.metadata, "body_chars": len(self.body)}


def _approved_roots(roots: Sequence[str | Path]) -> list[Path]:
    """Configured roots, resolved. A root that does not exist is dropped rather
    than raising: an operator listing three roots on a host that has two should
    get the two, not an error."""
    resolved: list[Path] = []
    for root in roots or ():
        try:
            candidate = Path(root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if candidate.is_dir():
            resolved.append(candidate)
    return resolved


def _contained(path: Path, root: Path) -> bool:
    """Is `path` inside `root` once BOTH are fully resolved?

    `Path.is_relative_to` on unresolved paths is a textual test and a symlink
    walks straight through it. Resolution first is what makes this a real
    containment check."""
    try:
        return path == root or root in path.parents
    except (OSError, RuntimeError):
        return False


def resolve_package_dir(skill_id: str, roots: Sequence[str | Path]) -> Path:
    """The directory for `skill_id`, proven to live inside an approved root.

    Raises rather than returning None: every caller needs the reason, and an
    empty return here would be indistinguishable from "not installed" for what
    is often an attempted traversal."""
    if not valid_slug(skill_id):
        # Rule 1, and the one that does the real work. Note this fires for
        # "../x", "a/b", "/etc/passwd" and "x\x00y" alike.
        raise SkillPackageError(
            f"invalid skill id {skill_id!r}: lowercase letters, digits, '-' and '_' only, "
            f"1-64 characters -- path separators and '..' are never accepted")
    approved = _approved_roots(roots)
    if not approved:
        raise SkillPackageError(
            "no approved skill roots are configured or none of them exist; set "
            "agents.skill_roots in config.yaml")
    tried: list[str] = []
    for root in approved:
        candidate = root / skill_id
        tried.append(str(candidate))
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        # Rule 2: containment AFTER resolution, so a symlink out of the root
        # is caught even though the unresolved path looked fine.
        if not _contained(resolved, root):
            raise SkillPackageError(
                f"skill {skill_id!r} resolves to {resolved} which is outside its approved root "
                f"{root}; refusing to read it")
        if (resolved / SKILL_FILE).is_file():
            return resolved
    raise SkillPackageError(
        f"no {SKILL_FILE} found for skill {skill_id!r}; looked in: {', '.join(tried)}")


def _read_bounded(path: Path) -> str:
    """Read at most MAX_SKILL_BYTES + 1 bytes, then refuse if it was too big.

    Reading one byte past the limit is how "exactly at the limit" stays legal
    while "over it" is still detectable without reading the whole file."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_SKILL_BYTES + 1)
    except OSError as exc:
        raise SkillPackageError(f"cannot read {path}: {exc}") from exc
    if len(raw) > MAX_SKILL_BYTES:
        raise SkillPackageError(
            f"{path} is larger than the {MAX_SKILL_BYTES} byte limit for a skill body; "
            f"refusing to load it rather than truncating a prompt")
    return raw.decode("utf-8", "replace")


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """A tiny `key: value` front-matter reader, and the body after it.

    Deliberately not YAML: this parses a handful of flat string fields off the
    top of a prompt file, and pulling in a YAML parser here would mean a skill
    file could construct arbitrary objects. Unknown fields are kept as strings
    in metadata rather than dropped."""
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        field_match = _FIELD.match(line)
        if field_match:
            fields[field_match.group(1).lower()] = field_match.group(2).strip().strip("'\"")
    return fields, text[match.end():]


def load_package(skill_id: str, roots: Sequence[str | Path], *,
                 version: str | None = None) -> SkillPackage:
    """Load exactly one skill package. Explicit, rooted, bounded.

    The version comes from the file's front matter when it declares one, from
    the caller when it does not, and otherwise from the content hash -- never
    from a timestamp, so registering the same unchanged file twice is
    idempotent rather than producing a new version every call."""
    directory = resolve_package_dir(skill_id, roots)
    path = directory / SKILL_FILE
    text = _read_bounded(path)
    fields, body = parse_front_matter(text)
    content_sha = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    resolved_version = version or fields.get("version") or f"sha-{content_sha[:12]}"
    summary = fields.get("description") or fields.get("summary") or ""
    return SkillPackage(
        skill_id=skill_id, version=str(resolved_version),
        name=fields.get("name") or skill_id, summary=summary,
        body=body.strip() or text.strip(), path=str(path), content_sha=content_sha,
        metadata={key: value for key, value in fields.items()
                  if key not in ("name", "version", "description", "summary")})


def discover(roots: Sequence[str | Path], *, limit: int = MAX_DISCOVERED) -> list[dict[str, Any]]:
    """What is installed and loadable, as a report. Registers nothing.

    A directory whose name is not a valid slug is reported as skipped rather
    than omitted: "why is my skill not listed" needs an answer, and silence is
    not one."""
    found: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for root in _approved_roots(roots):
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            if len(found) >= limit:
                break
            if not child.is_dir():
                continue
            if not valid_slug(child.name):
                skipped.append({"name": child.name, "reason": "name is not a valid skill id"})
                continue
            if not (child / SKILL_FILE).is_file():
                continue
            try:
                package = load_package(child.name, [root])
            except SkillPackageError as exc:
                skipped.append({"name": child.name, "reason": str(exc)})
                continue
            found.append({**package.to_dict(), "root": str(root)})
    return [*found, *({"skipped": entry} for entry in skipped)]


def default_skill_roots() -> tuple[str, ...]:
    """Where skills live when config says nothing.

    The repository's own `skills/` directory plus the per-user Claude skills
    home -- the two places this fleet already keeps them (see
    skill_provider.py). Never `/` and never the caller's cwd."""
    roots = [str(Path.cwd() / "skills")]
    claude_home = os.environ.get("CLAUDE_SKILLS_HOME")
    roots.append(claude_home if claude_home else str(Path.home() / ".claude" / "skills"))
    return tuple(roots)
