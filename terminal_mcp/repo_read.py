"""Read-only repository introspection -- the engine behind the `repo_*`
MCP tools, so an external agent (ChatGPT, via the MCP surface) can read
Git and file content DIRECTLY instead of asking a Claude session to read
it and paste the result back.

Why this is a separate module, not a widening of `coordinator.
git_repo_evidence`: that function is deliberately METADATA ONLY (branch/
HEAD/porcelain lines) because it feeds a dispatch gate, and its own
node-agent endpoint docstring promises "never file contents, never a
diff". This module is the opposite kind of thing -- it DOES return
content -- so it gets its own allowlist, its own secret-path denial, its
own output caps and its own audit category rather than quietly inheriting
a contract that promised not to do this.

The safety posture, in the order a request meets it:

  1. `enabled` -- an operator gate, like `session_lifecycle.enabled`.
  2. `allowed_roots` -- an absolute-path allowlist. Symlinks are resolved
     BEFORE containment is checked (lifecycle.resolve_cwd's own rule), so
     a symlink inside an allowed root that points outside it is refused.
  3. The repo root itself must also be inside an allowed root -- checking
     only the requested path is not enough, because a repo whose root
     sits ABOVE the allowed root would otherwise become readable in full
     through `repo_tree`/`repo_read` relative paths.
  4. Every path below the repo is re-resolved and re-contained against
     the repo root (PATH_OUTSIDE_REPO), which is what stops `../..`.
  5. Secret paths are denied by NAME/glob before a single byte is read
     (SECRET_PATH_DENIED). The path stays visible in listings -- an
     operator needs to know the file exists -- exactly the posture
     redaction.CREDENTIAL_FILE_NAMES already documents.
  6. Whatever survives is still run through `redaction.redact_output`,
     because a secret in an ordinary file (a token pasted into a README,
     a key in a test fixture) is not caught by any path rule.
  7. Output is capped -- bytes, lines, results, tree entries, timeout --
     and truncation is always REPORTED (`truncated: true`), never silent.

V1 is READ-ONLY by construction, not by convention: every git invocation
goes through `_git`, which refuses any subcommand outside
READ_ONLY_GIT_SUBCOMMANDS. There is no tool here that takes a git
subcommand, a shell string or an argv list from the caller, so "no
arbitrary exec for ChatGPT" is a property of the code, not a promise.
`checkout`/`reset`/`clean`/`commit`/`push`/`fetch`/`pull` are absent from
that set, so even a future careless caller inside this process cannot
reach them through this module.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .redaction import CREDENTIAL_FILE_NAMES, redact_output
from .work_telemetry_runtime import note as _note_signal

# -- error codes ---------------------------------------------------------
# Named constants rather than inline strings: these are part of the MCP
# contract (an external agent branches on them), so they are declared in
# one place and asserted in tests.
REPO_READ_DISABLED = "REPO_READ_DISABLED"
REPO_NOT_ALLOWED = "REPO_NOT_ALLOWED"
NO_ALLOWED_REPO_ROOTS = "NO_ALLOWED_REPO_ROOTS"
PATH_OUTSIDE_REPO = "PATH_OUTSIDE_REPO"
PATH_NOT_FOUND = "PATH_NOT_FOUND"
SECRET_PATH_DENIED = "SECRET_PATH_DENIED"
NOT_A_GIT_REPO = "NOT_A_GIT_REPO"
NOT_A_FILE = "NOT_A_FILE"
BINARY_FILE = "BINARY_FILE"
INVALID_REF = "INVALID_REF"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
GIT_COMMAND_FAILED = "GIT_COMMAND_FAILED"
GIT_UNAVAILABLE = "GIT_UNAVAILABLE"
GIT_AUTH_REQUIRED = "GIT_AUTH_REQUIRED"
# NODE_UNREACHABLE is raised by the routing layer (repo_service.py), not
# here, but belongs to the same contract -- re-exported so the whole set
# has one import site.
NODE_UNREACHABLE = "NODE_UNREACHABLE"

ERROR_CODES: tuple[str, ...] = (
    REPO_READ_DISABLED, REPO_NOT_ALLOWED, NO_ALLOWED_REPO_ROOTS, PATH_OUTSIDE_REPO,
    PATH_NOT_FOUND, SECRET_PATH_DENIED, NOT_A_GIT_REPO, NOT_A_FILE, BINARY_FILE,
    INVALID_REF, INVALID_ARGUMENT, GIT_COMMAND_FAILED, GIT_UNAVAILABLE,
    GIT_AUTH_REQUIRED, NODE_UNREACHABLE,
)

# -- the read-only boundary ---------------------------------------------
# Every git call in this module names one of these. A write/refs-mutating
# subcommand (checkout, reset, clean, commit, push, fetch, pull, merge,
# rebase, stash, apply, am, cherry-pick, tag -d, gc, worktree) is absent
# on purpose: `_git` raises on anything not listed, so V1's read-only
# guarantee cannot be lost to an edit elsewhere in this file.
READ_ONLY_GIT_SUBCOMMANDS = frozenset({
    "status", "rev-parse", "rev-list", "log", "show", "diff", "grep",
    "ls-tree", "ls-files", "remote", "branch", "for-each-ref", "cat-file",
    "ls-remote", "symbolic-ref",
})

# Files whose CONTENT is (or routinely is) a credential. Seeded from
# redaction.CREDENTIAL_FILE_NAMES -- the list this project already
# maintains for exactly this judgement -- and extended with the shapes a
# repository specifically tends to carry.
SECRET_PATH_GLOBS: tuple[str, ...] = (
    *(name for name in CREDENTIAL_FILE_NAMES),
    ".env", ".env.*", "*.env", "env.local", ".envrc",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.ppk",
    "id_*", "*_rsa", "*_ed25519", "*_ecdsa", "*_dsa",
    ".git-credentials", ".npmrc", ".pypirc", ".dockercfg", ".docker/config.json",
    "secrets.yaml", "secrets.yml", "secrets.json", "*.secrets",
    "*secret*.yaml", "*secret*.yml", "*credentials*", "*.kdbx",
    "known_hosts", "authorized_keys",
    # This project's own real secret-bearing files, named so its own repo
    # is safe to expose through its own tools.
    "node-agent.env", "*.token", "*token.txt",
)

# Directories never descended into or read from. `.git` is here for a
# concrete reason, not tidiness: `.git/config` can hold a remote URL with
# an embedded token, and `.git/objects` would let a caller reconstruct
# any file the path rules just denied.
DENIED_DIR_NAMES: tuple[str, ...] = (".git", ".ssh", ".gnupg", ".aws", ".terminal-mcp")

# Skipped when walking a tree so a listing is about the project rather
# than its dependency caches. Not a security boundary -- purely signal.
NOISE_DIR_NAMES: tuple[str, ...] = (
    "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".next", "target",
    ".tox", ".egg-info",
)

# How far a LINE WINDOW will scan to reach its first line. Far above any
# real source file, far below anything that threatens memory or time -- and
# a window beyond it is refused outright rather than answered with the
# wrong lines. Separate from RepoReadPolicy.max_bytes on purpose: that one
# bounds what is RETURNED, this one bounds what is LOOKED THROUGH.
MAX_LINE_SCAN_BYTES = 32_000_000

# A ref/commit-ish the caller supplied. Deliberately strict, and it may
# not start with "-": every git argument this module builds is positional,
# so a value like "--upload-pack=..." must never be able to become a flag.
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^{}@+-]{0,199}$")


@dataclass(frozen=True)
class RepoReadPolicy:
    """The whole boundary, in one value. Constructed from config for the
    real server (see config.RepoReadConfig) and inline in tests, so every
    limit is exercised without touching a config file.

    Defaults are chosen to be useful to an agent reading source while
    still bounded: 256 KB / 2000 lines is a large source file, 200 hits is
    more than a person reads at once, and 15s is well inside any MCP
    client's own timeout."""

    enabled: bool = True
    allowed_roots: tuple[str, ...] = ()
    max_bytes: int = 256_000
    max_lines: int = 2_000
    max_results: int = 200
    max_tree_entries: int = 2_000
    max_tree_depth: int = 6
    max_log_entries: int = 200
    max_diff_bytes: int = 400_000
    timeout_seconds: float = 15.0
    # Additive only: an operator can deny MORE, never less. There is no
    # config key that removes a built-in secret glob, so a config edit
    # cannot open a hole this module closed.
    extra_secret_globs: tuple[str, ...] = ()
    secret_globs: tuple[str, ...] = field(default=SECRET_PATH_GLOBS, compare=False)

    def all_secret_globs(self) -> tuple[str, ...]:
        return (*self.secret_globs, *self.extra_secret_globs)

    def resolved_roots(self) -> list[Path]:
        roots: list[Path] = []
        for root in self.allowed_roots or (str(Path.home()),):
            try:
                roots.append(Path(root).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                continue
        return roots


def _err(code: str, **extra: Any) -> dict[str, Any]:
    return {"error": code, **extra}


def is_secret_path(relative_path: str, policy: RepoReadPolicy) -> bool:
    """True when ANY component of the path matches a denied directory or
    the basename matches a secret glob.

    Matching every component (not just the basename) is what makes
    `config/secrets/db.yaml` and `deploy/.ssh/id_ed25519` denials rather
    than a basename that happens to look innocent."""
    parts = [part for part in Path(relative_path).parts if part not in ("/", ".")]
    if not parts:
        return False
    lowered = [part.casefold() for part in parts]
    for denied in DENIED_DIR_NAMES:
        if denied.casefold() in lowered:
            return True
    globs = policy.all_secret_globs()
    # The basename is what a secret glob is written against; the interior
    # components are checked too so a directory named `credentials` or
    # `.env.d` denies everything beneath it.
    for part in lowered:
        for pattern in globs:
            if fnmatch.fnmatch(part, pattern.casefold()):
                return True
    # A couple of globs are written as paths ("*.docker/config.json"), so
    # match the whole relative path as well.
    whole = "/".join(lowered)
    return any(fnmatch.fnmatch(whole, pattern.casefold()) for pattern in globs
               if "/" in pattern)


@dataclass(frozen=True)
class ResolvedRepo:
    """A request that has already passed the allowlist. Carrying the
    resolved repo root (never the caller's original string) into every
    git call is what keeps the containment check and the execution from
    ever disagreeing about which directory is meant."""

    repo_root: Path
    target: Path
    relative_path: str  # "" when the target IS the repo root

    @property
    def is_root(self) -> bool:
        return self.relative_path == ""


def _contained(candidate: Path, roots: list[Path]) -> bool:
    return any(candidate == root or root in candidate.parents for root in roots)


def _git_toplevel(directory: Path, policy: RepoReadPolicy) -> tuple[Path | None, dict[str, Any] | None]:
    code, out, err = _run_git(directory, "rev-parse", "--show-toplevel", policy=policy)
    if code != 0:
        if code == -1:
            return None, _err(GIT_UNAVAILABLE, detail=err[:300])
        return None, _err(NOT_A_GIT_REPO, path=str(directory), detail=err.strip()[:300])
    top = out.strip()
    if not top:
        return None, _err(NOT_A_GIT_REPO, path=str(directory))
    try:
        return Path(top).resolve(), None
    except (OSError, RuntimeError, ValueError):
        return None, _err(NOT_A_GIT_REPO, path=str(directory))


def resolve_repo(path: str | None, policy: RepoReadPolicy, *,
                 relative: str | None = None) -> tuple[ResolvedRepo | None, dict[str, Any] | None]:
    """`path` (a repo root or anywhere inside one) + optional `relative`
    -> a ResolvedRepo, or (None, error). This is the ONE place the
    allowlist, the repo-root containment rule and the in-repo containment
    rule are applied; every public function below starts here and no git
    call anywhere in this module runs before it has succeeded."""
    if not policy.enabled:
        return None, _err(REPO_READ_DISABLED,
                          detail="repo_read.enabled is false in this server's config")
    roots = policy.resolved_roots()
    if not roots:
        return None, _err(NO_ALLOWED_REPO_ROOTS)
    if not path or not str(path).strip():
        return None, _err(INVALID_ARGUMENT, detail="a repo path is required")
    try:
        candidate = Path(str(path)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None, _err(INVALID_ARGUMENT, path=str(path), detail="unresolvable path")
    if not _contained(candidate, roots):
        return None, _err(REPO_NOT_ALLOWED, path=str(path),
                          allowed_roots=[str(root) for root in roots])
    if not candidate.exists():
        return None, _err(PATH_NOT_FOUND, path=str(path))

    search_from = candidate if candidate.is_dir() else candidate.parent
    repo_root, error = _git_toplevel(search_from, policy)
    if error is not None:
        return None, error
    assert repo_root is not None
    # The repo root gets the SAME allowlist check as the requested path.
    # Without this, an allowed root deep inside a repository would expose
    # the entire repository through relative paths.
    if not _contained(repo_root, roots):
        return None, _err(REPO_NOT_ALLOWED, path=str(repo_root),
                          detail="the repository root is outside every allowed root",
                          allowed_roots=[str(root) for root in roots])

    target = candidate
    if relative is not None and str(relative).strip():
        resolved_target, error = _resolve_in_repo(repo_root, str(relative), policy)
        if error is not None:
            return None, error
        assert resolved_target is not None
        target = resolved_target
    elif candidate != repo_root:
        # A path given directly (not via `relative`) still has to be
        # inside the repo it resolved to -- true by construction here,
        # but asserted rather than assumed.
        if repo_root not in candidate.parents:
            return None, _err(PATH_OUTSIDE_REPO, path=str(path), repo_root=str(repo_root))

    try:
        rel = "" if target == repo_root else str(target.relative_to(repo_root))
    except ValueError:
        return None, _err(PATH_OUTSIDE_REPO, path=str(target), repo_root=str(repo_root))
    if rel and is_secret_path(rel, policy):
        return None, _err(SECRET_PATH_DENIED, path=rel,
                          detail="this path matches a credential/secret rule and is never read")
    return ResolvedRepo(repo_root=repo_root, target=target, relative_path=rel), None


def _resolve_in_repo(repo_root: Path, relative: str,
                     policy: RepoReadPolicy) -> tuple[Path | None, dict[str, Any] | None]:
    """Resolve a caller-supplied path against the repo root and require
    the RESOLVED result to still be inside it. `.resolve()` before the
    check is the whole point: it collapses `../..` and follows symlinks,
    so both escape routes are caught by one containment test.

    The secret-path rule is applied BEFORE `exists()`, so a denied path
    answers SECRET_PATH_DENIED whether or not the file is there. Checking
    existence first would turn this function into an oracle for "does this
    machine have a ~/.ssh/id_ed25519", which is information the caller has
    just been refused."""
    raw = relative.strip()
    if raw.startswith("-"):
        return None, _err(INVALID_ARGUMENT, path=relative,
                          detail="a path may not start with '-'")
    try:
        base = Path(raw)
        target = (base if base.is_absolute() else repo_root / base).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None, _err(INVALID_ARGUMENT, path=relative, detail="unresolvable path")
    if target != repo_root and repo_root not in target.parents:
        return None, _err(PATH_OUTSIDE_REPO, path=relative, repo_root=str(repo_root))
    rel = "" if target == repo_root else str(target.relative_to(repo_root))
    if rel and is_secret_path(rel, policy):
        return None, _err(SECRET_PATH_DENIED, path=rel,
                          detail="this path matches a credential/secret rule and is never read")
    if not target.exists():
        return None, _err(PATH_NOT_FOUND, path=relative)
    return target, None


def _git_env() -> dict[str, str]:
    """Never let a git call block on, or acquire, a credential.

    GIT_TERMINAL_PROMPT/GIT_ASKPASS/SSH_ASKPASS: a command that would
    otherwise wait for a username/passphrase fails fast instead of hanging
    until the timeout -- which is how GIT_AUTH_REQUIRED becomes a clean
    answer rather than a stall. GIT_OPTIONAL_LOCKS=0 keeps a read from
    taking the index lock, so reading a repo can never interfere with a
    session actually working in it."""
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
    })
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def _run_git(cwd: Path, subcommand: str, *args: str, policy: RepoReadPolicy,
             timeout: float | None = None) -> tuple[int, str, str]:
    """The single choke point for every git invocation in this module.

    Returns (returncode, stdout, stderr); returncode -1 means git could
    not be run at all (missing binary, timeout), which callers report as
    GIT_UNAVAILABLE rather than as a repo problem -- the same "could not
    look" vs "looked and it is bad" distinction coordinator.py draws.

    Raises ValueError on a non-read-only subcommand. That is a programming
    error, not a user error, so it is loud: it means someone added a write
    path to a module whose entire contract is that it has none."""
    if subcommand not in READ_ONLY_GIT_SUBCOMMANDS:
        raise ValueError(f"repo_read refuses the non-read-only git subcommand {subcommand!r}")
    command = ["git", "--no-pager", subcommand, *args]
    try:
        result = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True,
                                errors="replace", timeout=timeout or policy.timeout_seconds,
                                check=False, env=_git_env())
    except FileNotFoundError:
        return -1, "", "git is not installed on this host"
    except subprocess.TimeoutExpired:
        return -1, "", f"git {subcommand} timed out after {timeout or policy.timeout_seconds}s"
    except OSError as exc:
        return -1, "", f"git {subcommand} could not be run: {exc}"
    return result.returncode, result.stdout, result.stderr


def _redact(text: str) -> tuple[str, dict[str, Any]]:
    """Content leaves this module redacted, always. `redact_output`
    never raises and reports what it hit by RULE NAME (never a matched
    value), so the report is safe to return to the caller and to log."""
    return redact_output(text or "")


def _safe_ref(ref: str) -> tuple[str | None, dict[str, Any] | None]:
    value = (ref or "").strip()
    if not value:
        return None, _err(INVALID_ARGUMENT, detail="a ref is required")
    if not _SAFE_REF.match(value):
        return None, _err(INVALID_REF, ref=ref,
                          detail="refs are limited to [A-Za-z0-9._/~^{}@+-] and may not start with '-'")
    return value, None


def _clamp(value: Any, default: int, maximum: int, *, minimum: int = 1) -> int:
    try:
        number = int(value) if value is not None else default
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


# -- public reads --------------------------------------------------------

def repo_status(path: str, policy: RepoReadPolicy) -> dict[str, Any]:
    """Branch / HEAD / dirty state / upstream divergence, plus the repo's
    canonical project identity so a caller can correlate this checkout
    with the same project on another node.

    Reuses `coordinator.git_repo_evidence` rather than re-running the same
    four git commands a second way -- that function is already this
    project's one answer to "what state is this repo in", and it already
    fails closed."""
    resolved, error = resolve_repo(path, policy)
    if error is not None:
        return error
    assert resolved is not None
    from .coordinator import RepoEvidenceError, git_repo_evidence

    try:
        evidence = git_repo_evidence(str(resolved.repo_root), timeout=policy.timeout_seconds)
    except RepoEvidenceError as exc:
        return _err(GIT_COMMAND_FAILED, repo_root=str(resolved.repo_root), detail=str(exc)[:500])

    status_lines = list(evidence.status_lines)
    truncated = len(status_lines) > policy.max_results
    if truncated:
        status_lines = status_lines[: policy.max_results]
    identity = _identity_for(resolved.repo_root)
    return {
        "repo_root": str(resolved.repo_root), "branch": evidence.branch, "head": evidence.head,
        "clean": evidence.clean, "status_lines": status_lines,
        "status_lines_truncated": truncated,
        "has_upstream": evidence.has_upstream, "ahead": evidence.ahead, "behind": evidence.behind,
        "diverged": evidence.diverged, "project": identity,
    }


def _identity_for(repo_root: Path) -> dict[str, Any] | None:
    """Project identity, best-effort. A repo whose identity cannot be
    resolved is still perfectly readable -- identity is a correlation
    convenience here, never an access decision -- so a failure degrades
    to None instead of failing the read."""
    try:
        from .project_identity import resolve_project

        identity = resolve_project(str(repo_root))
    except Exception:  # noqa: BLE001 -- identity is additive; never fail a read for it
        return None
    return identity.to_dict() if identity is not None else None


def repo_head(path: str, policy: RepoReadPolicy) -> dict[str, Any]:
    """HEAD's commit id, branch and subject -- the cheapest "where am I"
    call, for a caller that does not need full status."""
    resolved, error = resolve_repo(path, policy)
    if error is not None:
        return error
    assert resolved is not None
    root = resolved.repo_root
    code, out, err = _run_git(root, "log", "-1", "--format=%H%n%h%n%an%n%aI%n%s", policy=policy)
    if code != 0:
        return _err(GIT_UNAVAILABLE if code == -1 else GIT_COMMAND_FAILED,
                    repo_root=str(root), detail=err.strip()[:300])
    lines = out.splitlines()
    while len(lines) < 5:
        lines.append("")
    branch_code, branch_out, _ = _run_git(root, "rev-parse", "--abbrev-ref", "HEAD", policy=policy)
    subject, _ = _redact(lines[4])
    author, _ = _redact(lines[2])
    return {
        "repo_root": str(root), "head": lines[0], "short": lines[1],
        "author": author, "authored_at": lines[3], "subject": subject,
        "branch": branch_out.strip() if branch_code == 0 else None,
        "detached": (branch_out.strip() == "HEAD") if branch_code == 0 else None,
    }


def repo_branches(path: str, policy: RepoReadPolicy, *, limit: int | None = None) -> dict[str, Any]:
    """Local branches with their tip and upstream. Read via for-each-ref
    (stable, parseable output) rather than `branch -v`, whose format is
    explicitly documented as porcelain-unstable."""
    resolved, error = resolve_repo(path, policy)
    if error is not None:
        return error
    assert resolved is not None
    root = resolved.repo_root
    count = _clamp(limit, 100, policy.max_results)
    code, out, err = _run_git(
        root, "for-each-ref", f"--count={count}", "--sort=-committerdate",
        "--format=%(refname:short)%09%(objectname)%09%(upstream:short)%09%(HEAD)",
        "refs/heads", policy=policy)
    if code != 0:
        return _err(GIT_UNAVAILABLE if code == -1 else GIT_COMMAND_FAILED,
                    repo_root=str(root), detail=err.strip()[:300])
    branches = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        while len(parts) < 4:
            parts.append("")
        branches.append({"name": parts[0], "head": parts[1],
                         "upstream": parts[2] or None, "current": parts[3].strip() == "*"})
    return {"repo_root": str(root), "branches": branches, "limit": count}


def repo_remotes(path: str, policy: RepoReadPolicy, *, check_auth: bool = False) -> dict[str, Any]:
    """Configured remotes, with credentials stripped from every URL.

    A remote URL is one of the few places a live token genuinely sits in
    plain config (`https://user:ghp_xxx@github.com/...`), so each URL is
    passed through project_identity.normalise_git_remote -- which already
    drops the credential portion for exactly this reason -- and the raw
    URL is returned only after redaction.

    `check_auth=True` additionally probes READ access with `ls-remote`
    (network, but read-only and bounded, and with every credential prompt
    disabled by `_git_env`). It is opt-in because a local read must never
    depend on, or wait for, the network: with it off this call is pure
    local config, which is what lets a repo stay fully readable while
    remote auth is unavailable. A probe that fails answers
    GIT_AUTH_REQUIRED in `auth`, never as a top-level error -- the
    remotes themselves were read successfully."""
    resolved, error = resolve_repo(path, policy)
    if error is not None:
        return error
    assert resolved is not None
    root = resolved.repo_root
    code, out, err = _run_git(root, "remote", "-v", policy=policy)
    if code != 0:
        return _err(GIT_UNAVAILABLE if code == -1 else GIT_COMMAND_FAILED,
                    repo_root=str(root), detail=err.strip()[:300])
    from .project_identity import normalise_git_remote

    remotes: dict[str, dict[str, Any]] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        name, _, rest = line.partition("\t")
        url = rest.split(" ", 1)[0].strip()
        safe_url, _ = _redact(url)
        entry = remotes.setdefault(name.strip(), {"name": name.strip()})
        entry["url"] = safe_url
        entry["normalised"] = normalise_git_remote(url)
        entry["transport"] = ("ssh" if url.startswith("git@") or url.startswith("ssh://")
                              else "https" if url.startswith("http") else "other")
    result: dict[str, Any] = {"repo_root": str(root), "remotes": list(remotes.values())}
    if not check_auth:
        result["auth"] = {"checked": False,
                          "detail": "pass check_auth=true to probe read access (network)"}
        return result

    probe_target = "origin" if "origin" in remotes else (next(iter(remotes), None))
    if probe_target is None:
        result["auth"] = {"checked": True, "ok": False, "error": INVALID_ARGUMENT,
                          "detail": "no remote is configured"}
        return result
    code, _, err = _run_git(root, "ls-remote", "--exit-code", "--quiet", probe_target, "HEAD",
                            policy=policy, timeout=min(policy.timeout_seconds, 20.0))
    if code == 0:
        result["auth"] = {"checked": True, "ok": True, "remote": probe_target}
    else:
        # The stderr of a failed auth probe can echo a URL that contained
        # a token, so it is redacted like any other content before it is
        # returned or logged.
        detail, _ = _redact(err.strip()[:300])
        result["auth"] = {"checked": True, "ok": False, "remote": probe_target,
                          "error": GIT_AUTH_REQUIRED, "detail": detail}
    return result


def repo_tree(path: str, policy: RepoReadPolicy, *, subpath: str | None = None,
              depth: int | None = None, limit: int | None = None) -> dict[str, Any]:
    """Directory listing, breadth-first, bounded by depth and entry count.

    Walks the filesystem (os.scandir) rather than `git ls-tree` on
    purpose: a caller exploring a working tree needs to see untracked
    files too -- a file a session just created is exactly what it will
    ask about. Denied paths are LISTED with `"denied": true` and never
    descended into or read, because hiding them would make an agent
    conclude the file does not exist."""
    resolved, error = resolve_repo(path, policy, relative=subpath)
    if error is not None:
        return error
    assert resolved is not None
    if not resolved.target.is_dir():
        return _err(INVALID_ARGUMENT, path=subpath or path,
                    detail="repo_tree needs a directory; use repo_read for a file")
    max_depth = _clamp(depth, 2, policy.max_tree_depth)
    max_entries = _clamp(limit, 200, policy.max_tree_entries)
    root = resolved.repo_root
    entries: list[dict[str, Any]] = []
    truncated = False
    queue: list[tuple[Path, int]] = [(resolved.target, 0)]
    while queue:
        directory, level = queue.pop(0)
        try:
            children = sorted(os.scandir(directory), key=lambda entry: (not entry.is_dir(), entry.name))
        except OSError as exc:
            entries.append({"path": _rel(directory, root), "type": "dir", "error": str(exc)[:120]})
            continue
        for child in children:
            if len(entries) >= max_entries:
                truncated = True
                break
            rel = _rel(Path(child.path), root)
            is_dir = child.is_dir(follow_symlinks=False)
            record: dict[str, Any] = {"path": rel, "type": "dir" if is_dir else "file"}
            if child.is_symlink():
                record["symlink"] = True
            if is_secret_path(rel, policy):
                record["denied"] = True
                record["reason"] = SECRET_PATH_DENIED
                entries.append(record)
                continue
            if not is_dir:
                try:
                    record["bytes"] = child.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
            entries.append(record)
            if is_dir and level + 1 <= max_depth and child.name not in NOISE_DIR_NAMES:
                # A symlinked directory is listed but never followed: its
                # target may be perfectly legal while walking INTO it would
                # silently leave the repo, which is the escape
                # PATH_OUTSIDE_REPO exists to prevent.
                if not child.is_symlink():
                    queue.append((Path(child.path), level + 1))
        if len(entries) >= max_entries:
            truncated = True
            break
    return {"repo_root": str(root), "subpath": resolved.relative_path or ".",
            "entries": entries, "count": len(entries), "truncated": truncated,
            "depth": max_depth, "limit": max_entries}


def _rel(target: Path, root: Path) -> str:
    try:
        return str(target.relative_to(root))
    except ValueError:
        return str(target)


def repo_read(path: str, policy: RepoReadPolicy, *, file: str | None = None,
              start_line: int | None = None, end_line: int | None = None,
              max_bytes: int | None = None) -> dict[str, Any]:
    """One file's content, redacted, with either a line window or a byte
    cap. `file` is resolved against the repo root; passing the full path
    as `path` works too.

    A binary file is refused (BINARY_FILE) rather than returned as
    mojibake -- an agent asking for a .png wants to be told so, and
    redaction has no meaning over binary bytes anyway."""
    resolved, error = resolve_repo(path, policy, relative=file)
    if error is not None:
        return error
    assert resolved is not None
    target = resolved.target
    if target.is_dir():
        return _err(NOT_A_FILE, path=resolved.relative_path or str(target),
                    detail="this is a directory; use repo_tree")
    cap = _clamp(max_bytes, policy.max_bytes, policy.max_bytes)
    try:
        size = target.stat().st_size
        with target.open("rb") as probe:
            head = probe.read(8192)
    except OSError as exc:
        return _err(PATH_NOT_FOUND, path=resolved.relative_path, detail=str(exc)[:200])
    if b"\x00" in head:
        return _err(BINARY_FILE, path=resolved.relative_path, bytes=size)

    payload = (_read_window(resolved, policy, cap, size, start_line, end_line)
               if start_line is not None or end_line is not None
               else _read_head(resolved, policy, cap, size))
    # Efficiency telemetry, counted where the file was actually read. A
    # refused read is not a read, and this is a no-op unless a recorder is
    # active for the task being worked.
    if not payload.get("error"):
        _note_signal("files_read", source="repo_read.repo_read")
    return payload


def _read_head(resolved: ResolvedRepo, policy: RepoReadPolicy, cap: int,
               size: int) -> dict[str, Any]:
    """No window asked for: the start of the file, up to the byte and line
    caps, whichever binds first."""
    try:
        with resolved.target.open("rb") as handle:
            raw = handle.read(cap + 1)
    except OSError as exc:
        return _err(PATH_NOT_FOUND, path=resolved.relative_path, detail=str(exc)[:200])
    byte_truncated = len(raw) > cap
    if byte_truncated:
        raw = raw[:cap]
    text = raw.decode("utf-8", errors="replace")
    # A byte-truncated read almost always ends mid-line; dropping that
    # partial tail keeps every returned line a real line, which matters
    # when the caller is going to quote line numbers back.
    if byte_truncated and "\n" in text:
        text = text[: text.rindex("\n") + 1]
    lines = text.splitlines()
    line_truncated = len(lines) > policy.max_lines
    selected = lines[: policy.max_lines]
    # No window was asked for, so "capped" and "there is more" are the same
    # event here: the caller implicitly asked for the whole file.
    incomplete = byte_truncated or line_truncated or size > len(raw)
    return _read_payload(resolved, policy, cap, size, selected, first=1,
                         truncated=incomplete, has_more=incomplete)


def _read_window(resolved: ResolvedRepo, policy: RepoReadPolicy, cap: int, size: int,
                 start_line: int | None, end_line: int | None) -> dict[str, Any]:
    """A requested line window, read by STREAMING lines rather than by
    slicing the first `cap` bytes.

    The byte cap bounds what is RETURNED; it must not bound which lines are
    REACHABLE. Slicing first meant a window late in a large file was
    silently clamped to whatever line the byte cap happened to land on --
    the caller asked for line 5000, got line 4600, and nothing in the
    response said the window had moved. Silently answering about different
    lines than were asked for is the worst failure a file reader can have,
    because every line number the caller quotes afterwards is then wrong.

    Scanning is bounded separately by MAX_LINE_SCAN_BYTES so a pathological
    file still cannot be walked forever, and a window genuinely beyond that
    bound is REFUSED with a clear reason rather than answered wrongly."""
    first = max(1, int(start_line) if start_line is not None else 1)
    if end_line is not None:
        last = max(first, int(end_line))
    else:
        last = first + policy.max_lines - 1
    # Whether the caller's own window had to be narrowed by the line limit --
    # distinct from the file merely continuing past it (see below).
    clamped = last - first + 1 > policy.max_lines
    if clamped:
        last = first + policy.max_lines - 1

    selected: list[str] = []
    scanned = 0
    returned_bytes = 0
    line_number = 0
    more_after = False
    cut_by_cap = False
    try:
        with resolved.target.open("rb") as handle:
            for raw_line in handle:
                scanned += len(raw_line)
                line_number += 1
                if line_number < first:
                    if scanned > MAX_LINE_SCAN_BYTES:
                        return _err(INVALID_ARGUMENT, path=resolved.relative_path,
                                    detail=f"start_line {first} lies beyond the first "
                                           f"{MAX_LINE_SCAN_BYTES} bytes of this file, which is "
                                           f"as far as a line window is scanned",
                                    bytes_total=size, scan_limit=MAX_LINE_SCAN_BYTES)
                    continue
                if line_number > last:
                    # The window was fully satisfied; the file just continues.
                    more_after = True
                    break
                if returned_bytes + len(raw_line) > cap:
                    # The window itself exceeds the byte cap: return the
                    # whole lines that fit and say so.
                    more_after = True
                    cut_by_cap = True
                    break
                returned_bytes += len(raw_line)
                selected.append(raw_line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r"))
    except OSError as exc:
        return _err(PATH_NOT_FOUND, path=resolved.relative_path, detail=str(exc)[:200])

    if not selected:
        return _err(INVALID_ARGUMENT, path=resolved.relative_path,
                    detail=f"start_line {first} is past the end of this file "
                           f"({line_number} lines)",
                    lines_total=line_number, bytes_total=size)
    # `truncated` means "you did NOT get what you asked for" -- the byte cap
    # cut the window short, or the line limit narrowed it. It deliberately
    # does NOT mean "the file continues past your window": a caller that asked
    # for lines 2-2 and got exactly that has not been truncated, and a caller
    # paging on `truncated` would otherwise never stop. "The file continues"
    # is `has_more`, which is the field to page on.
    return _read_payload(resolved, policy, cap, size, selected, first=first,
                         truncated=cut_by_cap or clamped, has_more=more_after)


def _read_payload(resolved: ResolvedRepo, policy: RepoReadPolicy, cap: int, size: int,
                  selected: list[str], *, first: int, truncated: bool,
                  has_more: bool) -> dict[str, Any]:
    redacted, report = _redact("\n".join(selected))
    return {
        "repo_root": str(resolved.repo_root), "path": resolved.relative_path,
        "content": redacted, "start_line": first, "end_line": first + len(selected) - 1,
        "lines_returned": len(selected), "bytes_total": size,
        "truncated": bool(truncated), "has_more": bool(has_more),
        "byte_limit": cap, "line_limit": policy.max_lines,
        "redaction": _redaction_summary(report),
    }


def _redaction_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Only the counts and rule NAMES travel outward. `redact_output`'s
    report never contains a matched value, which is what makes it safe to
    return to the caller and to write to the audit log."""
    return {"redactions": report.get("redactions", 0), "rules": report.get("rules", {}),
            "high_risk": bool(report.get("high_risk"))}


def repo_search(path: str, policy: RepoReadPolicy, *, query: str,
                paths: list[str] | None = None, max_results: int | None = None,
                regex: bool = False, ignore_case: bool = False,
                include_untracked: bool = True) -> dict[str, Any]:
    """Content search across the repo via `git grep`.

    Fixed-string matching is the DEFAULT (`regex=False` -> `-F`): a
    caller-supplied regex is a real catastrophic-backtracking risk, and
    "find this symbol" -- the actual use -- never needs one. `-I` skips
    binary files, `--no-color` keeps the output parseable, and every hit
    whose file is a denied path is dropped before it is returned, so a
    secret cannot be read one grep line at a time.

    Pathspecs are validated and passed after `--`, so a value like
    `--output=/etc/passwd` is a path that does not exist rather than a
    flag."""
    resolved, error = resolve_repo(path, policy)
    if error is not None:
        return error
    assert resolved is not None
    needle = (query or "").strip()
    if not needle:
        return _err(INVALID_ARGUMENT, detail="query is required")
    if len(needle) > 500:
        return _err(INVALID_ARGUMENT, detail="query is limited to 500 characters")
    root = resolved.repo_root
    limit = _clamp(max_results, 100, policy.max_results)

    pathspecs: list[str] = []
    for raw in paths or []:
        spec = str(raw).strip()
        if not spec:
            continue
        if spec.startswith("-"):
            return _err(INVALID_ARGUMENT, path=spec, detail="a pathspec may not start with '-'")
        if ".." in Path(spec).parts:
            return _err(PATH_OUTSIDE_REPO, path=spec, repo_root=str(root))
        pathspecs.append(spec)

    args = ["--no-color", "-n", "-I", "--full-name", f"--max-count={limit}"]
    args.append("-F" if not regex else "-E")
    if ignore_case:
        args.append("-i")
    if include_untracked:
        args.append("--untracked")
    # Exclusions are magic pathspecs, so they belong AFTER `--` next to the
    # positive pathspecs -- placed before it, git would read them as
    # patterns and silently search for the literal text ":!node_modules/".
    exclusions = [f":(exclude){denied}/" for denied in (*NOISE_DIR_NAMES, *DENIED_DIR_NAMES)]
    args.extend(["-e", needle, "--", *(pathspecs or ["."]), *exclusions])
    code, out, err = _run_git(root, "grep", *args, policy=policy)
    if code == -1:
        return _err(GIT_UNAVAILABLE, repo_root=str(root), detail=err.strip()[:300])
    # git grep exits 1 for "no matches" -- an answer, not a failure.
    if code not in (0, 1):
        return _err(GIT_COMMAND_FAILED, repo_root=str(root), detail=err.strip()[:300])

    results: list[dict[str, Any]] = []
    denied_files: set[str] = set()
    truncated = False
    for line in out.splitlines():
        if len(results) >= limit:
            truncated = True
            break
        file_part, _, rest = line.partition(":")
        line_no, _, body = rest.partition(":")
        if not file_part or not line_no.isdigit():
            continue
        if is_secret_path(file_part, policy):
            denied_files.add(file_part)
            continue
        redacted, _ = _redact(body[:1000])
        results.append({"path": file_part, "line": int(line_no), "text": redacted})
    # One search CALL, whatever it found: the cost being measured is the
    # round trip, not the number of lines that came back.
    _note_signal("search_calls", source="repo_read.repo_search")
    return {"repo_root": str(root), "query": needle, "regex": bool(regex),
            "results": results, "count": len(results), "truncated": truncated,
            "limit": limit, "secret_paths_skipped": sorted(denied_files)}


def repo_diff(path: str, policy: RepoReadPolicy, *, base: str | None = None,
              head: str | None = None, staged: bool = False,
              paths: list[str] | None = None, stat_only: bool = False,
              max_bytes: int | None = None) -> dict[str, Any]:
    """A diff: working tree by default, `base..head` when both are given.

    With no base/head this is the uncommitted change (`--cached` when
    `staged=True`), which is what "what is this session doing right now"
    actually means. Every patch is passed through `_filter_patch`, so a
    hunk touching a denied path is dropped rather than leaking a secret
    through a diff -- the one hole a path-only rule would otherwise
    leave wide open."""
    resolved, error = resolve_repo(path, policy)
    if error is not None:
        return error
    assert resolved is not None
    root = resolved.repo_root
    args: list[str] = ["--no-color"]
    if stat_only:
        args.append("--stat")
    if base and head:
        safe_base, error = _safe_ref(base)
        if error is not None:
            return error
        safe_head, error = _safe_ref(head)
        if error is not None:
            return error
        for ref in (safe_base, safe_head):
            code, _, err = _run_git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
                                    policy=policy)
            if code != 0:
                return _err(INVALID_REF, ref=ref, detail=err.strip()[:200] or "unknown revision")
        args.extend([safe_base, safe_head])
    elif base or head:
        return _err(INVALID_ARGUMENT,
                    detail="give BOTH base and head, or neither (which diffs the working tree)")
    elif staged:
        args.append("--cached")

    pathspecs: list[str] = []
    for raw in paths or []:
        spec = str(raw).strip()
        if not spec:
            continue
        if spec.startswith("-"):
            return _err(INVALID_ARGUMENT, path=spec, detail="a pathspec may not start with '-'")
        if ".." in Path(spec).parts:
            return _err(PATH_OUTSIDE_REPO, path=spec, repo_root=str(root))
        pathspecs.append(spec)
    args.append("--")
    args.extend(pathspecs or ["."])

    code, out, err = _run_git(root, "diff", *args, policy=policy)
    if code == -1:
        return _err(GIT_UNAVAILABLE, repo_root=str(root), detail=err.strip()[:300])
    if code not in (0, 1):
        return _err(GIT_COMMAND_FAILED, repo_root=str(root), detail=err.strip()[:300])
    cap = _clamp(max_bytes, policy.max_diff_bytes, policy.max_diff_bytes)
    return _patch_payload(out, policy, root, cap, extra={
        "base": base, "head": head, "staged": bool(staged) and not (base and head),
        "stat_only": bool(stat_only),
    })


def repo_log(path: str, policy: RepoReadPolicy, *, limit: int | None = None,
             file: str | None = None, base: str | None = None,
             head: str | None = None) -> dict[str, Any]:
    """Commit history, newest first, optionally for one path.

    Subject and author are redacted like any other content: a commit
    message is a place secrets really do get pasted, and the log is
    exactly what an agent reads first."""
    resolved, error = resolve_repo(path, policy)
    if error is not None:
        return error
    assert resolved is not None
    root = resolved.repo_root
    count = _clamp(limit, 20, policy.max_log_entries)
    args = [f"--max-count={count}", "--no-color",
            "--format=%H%x09%h%x09%an%x09%aI%x09%s"]
    if base and head:
        safe_base, error = _safe_ref(base)
        if error is not None:
            return error
        safe_head, error = _safe_ref(head)
        if error is not None:
            return error
        args.append(f"{safe_base}..{safe_head}")
    elif base or head:
        return _err(INVALID_ARGUMENT, detail="give BOTH base and head, or neither")

    relative_file = None
    if file and str(file).strip():
        target, error = _resolve_in_repo(root, str(file), policy)
        if error is not None:
            return error
        assert target is not None
        relative_file = _rel(target, root)
        if is_secret_path(relative_file, policy):
            return _err(SECRET_PATH_DENIED, path=relative_file)
    args.append("--")
    if relative_file:
        args.append(relative_file)

    code, out, err = _run_git(root, "log", *args, policy=policy)
    if code != 0:
        return _err(GIT_UNAVAILABLE if code == -1 else GIT_COMMAND_FAILED,
                    repo_root=str(root), detail=err.strip()[:300])
    commits = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        subject, _ = _redact(parts[4])
        author, _ = _redact(parts[2])
        commits.append({"commit": parts[0], "short": parts[1], "author": author,
                        "authored_at": parts[3], "subject": subject})
    return {"repo_root": str(root), "commits": commits, "count": len(commits),
            "limit": count, "path": relative_file}


def repo_show_commit(path: str, policy: RepoReadPolicy, *, commit: str,
                     file: str | None = None, stat_only: bool = False,
                     max_bytes: int | None = None) -> dict[str, Any]:
    """One commit: metadata, message, and its patch (or just `--stat`).

    Same `_filter_patch` treatment as repo_diff -- a commit that touched
    a `.env` shows the file in its stat and omits the hunk."""
    resolved, error = resolve_repo(path, policy)
    if error is not None:
        return error
    assert resolved is not None
    root = resolved.repo_root
    safe_commit, error = _safe_ref(commit)
    if error is not None:
        return error
    assert safe_commit is not None
    code, _, err = _run_git(root, "rev-parse", "--verify", "--quiet", f"{safe_commit}^{{commit}}",
                            policy=policy)
    if code != 0:
        return _err(INVALID_REF, ref=commit, detail=err.strip()[:200] or "unknown revision")

    meta_code, meta_out, meta_err = _run_git(
        root, "show", "--no-color", "--no-patch",
        "--format=%H%n%h%n%an%n%aI%n%cn%n%cI%n%P%n%s%n%b", safe_commit, policy=policy)
    if meta_code != 0:
        return _err(GIT_UNAVAILABLE if meta_code == -1 else GIT_COMMAND_FAILED,
                    repo_root=str(root), detail=meta_err.strip()[:300])
    fields = meta_out.split("\n")
    while len(fields) < 9:
        fields.append("")
    subject, _ = _redact(fields[7])
    body, _ = _redact("\n".join(fields[8:])[:8000])

    relative_file = None
    if file and str(file).strip():
        target, error = _resolve_in_repo(root, str(file), policy)
        if error is not None:
            return error
        assert target is not None
        relative_file = _rel(target, root)
        if is_secret_path(relative_file, policy):
            return _err(SECRET_PATH_DENIED, path=relative_file)

    patch_args = ["--no-color", "--format=", safe_commit]
    if stat_only:
        patch_args.insert(1, "--stat")
    patch_args.append("--")
    if relative_file:
        patch_args.append(relative_file)
    patch_code, patch_out, patch_err = _run_git(root, "show", *patch_args, policy=policy)
    if patch_code != 0:
        return _err(GIT_UNAVAILABLE if patch_code == -1 else GIT_COMMAND_FAILED,
                    repo_root=str(root), detail=patch_err.strip()[:300])
    cap = _clamp(max_bytes, policy.max_diff_bytes, policy.max_diff_bytes)
    payload = _patch_payload(patch_out, policy, root, cap, extra={
        "commit": fields[0], "short": fields[1], "author": _redact(fields[2])[0],
        "authored_at": fields[3], "committer": _redact(fields[4])[0], "committed_at": fields[5],
        "parents": fields[6].split() if fields[6].strip() else [],
        "subject": subject, "body": body, "path": relative_file,
        "stat_only": bool(stat_only),
    })
    return payload


def _patch_payload(patch: str, policy: RepoReadPolicy, root: Path, cap: int,
                   *, extra: dict[str, Any]) -> dict[str, Any]:
    filtered, excluded = _filter_patch(patch, policy)
    truncated = len(filtered.encode("utf-8", errors="replace")) > cap
    if truncated:
        # Cut on a character boundary and then back to the last complete
        # line, so a truncated patch is still a parseable one.
        filtered = filtered.encode("utf-8", errors="replace")[:cap].decode("utf-8", errors="ignore")
        if "\n" in filtered:
            filtered = filtered[: filtered.rindex("\n") + 1]
    redacted, report = _redact(filtered)
    return {"repo_root": str(root), "patch": redacted, "truncated": truncated,
            "byte_limit": cap, "secret_paths_excluded": excluded,
            "redaction": _redaction_summary(report), **extra}


_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")


def _filter_patch(patch: str, policy: RepoReadPolicy) -> tuple[str, list[str]]:
    """Drop whole `diff --git` blocks whose path is denied.

    Splitting on the header rather than trying to exclude paths at the git
    level is deliberate: pathspec exclusion has to be built per-call and
    silently does nothing if a pattern is wrong, whereas this filter is
    applied to whatever git actually produced and is directly testable.
    Both sides (a/ and b/) are checked so a RENAME from an ordinary name
    to a secret one, or the reverse, is still caught."""
    if not patch:
        return patch, []
    kept: list[str] = []
    excluded: list[str] = []
    skipping = False
    for line in patch.splitlines(keepends=True):
        match = _DIFF_HEADER.match(line.rstrip("\n"))
        if match is not None:
            paths = {match.group("a"), match.group("b")}
            skipping = any(is_secret_path(candidate, policy) for candidate in paths)
            if skipping:
                for candidate in sorted(paths):
                    if candidate not in excluded:
                        excluded.append(candidate)
                kept.append(f"diff --git a/{match.group('a')} b/{match.group('b')}\n")
                kept.append(f"[{SECRET_PATH_DENIED}: hunks for this path are never returned]\n")
                continue
        if skipping:
            continue
        kept.append(line)
    return "".join(kept), excluded


# -- the operation table -------------------------------------------------
# ONE declaration of "which read operations exist and what each takes",
# shared by every caller: the MCP tools, the node-agent HTTP endpoint, the
# LocalNodeClient and the routing layer in repo_service.py. Local and
# remote therefore cannot drift -- a param added here is available on both
# sides at once, and an op absent here does not exist anywhere.
#
# This table is also the second half of the read-only guarantee: the node
# endpoint dispatches ONLY through it, so a node cannot be asked to run
# anything but one of these ten functions, regardless of what a caller
# puts in the request.

OPERATIONS: dict[str, dict[str, Any]] = {
    "status": {"func": "repo_status", "params": {}},
    "head": {"func": "repo_head", "params": {}},
    "branches": {"func": "repo_branches", "params": {"limit": "int"}},
    "remotes": {"func": "repo_remotes", "params": {"check_auth": "bool"}},
    "tree": {"func": "repo_tree", "params": {"subpath": "str", "depth": "int", "limit": "int"}},
    "read": {"func": "repo_read",
             "params": {"file": "str", "start_line": "int", "end_line": "int", "max_bytes": "int"}},
    "search": {"func": "repo_search",
               "params": {"query": "str", "paths": "list", "max_results": "int",
                          "regex": "bool", "ignore_case": "bool", "include_untracked": "bool"}},
    "diff": {"func": "repo_diff",
             "params": {"base": "str", "head": "str", "staged": "bool", "paths": "list",
                        "stat_only": "bool", "max_bytes": "int"}},
    "log": {"func": "repo_log",
            "params": {"limit": "int", "file": "str", "base": "str", "head": "str"}},
    "show_commit": {"func": "repo_show_commit",
                    "params": {"commit": "str", "file": "str", "stat_only": "bool",
                               "max_bytes": "int"}},
}

OPERATION_NAMES: tuple[str, ...] = tuple(OPERATIONS)


def coerce_params(op: str, raw: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Turn a query-string/JSON mapping into the typed kwargs one operation
    takes, refusing anything the table does not declare.

    An UNKNOWN parameter is an error rather than something to ignore: a
    caller that misspells `max_results` should be told, not silently
    handed an unbounded-looking result that actually used the default. A
    list value accepts either a real list or a comma-separated string,
    because the same spec has to serve JSON kwargs and an HTTP query
    string."""
    spec = OPERATIONS.get(op)
    if spec is None:
        return None, _err(INVALID_ARGUMENT, op=op,
                          detail=f"unknown repo operation; known: {', '.join(OPERATION_NAMES)}")
    declared: dict[str, str] = spec["params"]
    unknown = [key for key in raw if key not in declared and key not in ("path", "op")]
    if unknown:
        return None, _err(INVALID_ARGUMENT, op=op, unknown_params=sorted(unknown),
                          detail=f"{op} accepts: {', '.join(sorted(declared)) or '(no parameters)'}")
    kwargs: dict[str, Any] = {}
    for key, kind in declared.items():
        if key not in raw or raw[key] is None or raw[key] == "":
            continue
        value = raw[key]
        if kind == "int":
            try:
                kwargs[key] = int(value)
            except (TypeError, ValueError):
                return None, _err(INVALID_ARGUMENT, param=key, detail=f"{key} must be an integer")
        elif kind == "bool":
            if isinstance(value, bool):
                kwargs[key] = value
            else:
                text = str(value).strip().casefold()
                if text not in ("1", "0", "true", "false", "yes", "no"):
                    return None, _err(INVALID_ARGUMENT, param=key,
                                      detail=f"{key} must be a boolean")
                kwargs[key] = text in ("1", "true", "yes")
        elif kind == "list":
            if isinstance(value, (list, tuple)):
                items = [str(item) for item in value]
            else:
                items = [part for part in str(value).split(",") if part.strip()]
            kwargs[key] = items
        else:
            kwargs[key] = str(value)
    return kwargs, None


def run_operation(op: str, path: str, params: dict[str, Any],
                  policy: RepoReadPolicy) -> dict[str, Any]:
    """Dispatch one named read operation. The ONLY entry point the node
    endpoint and the routing layer use, so every caller gets the same
    validation, the same allowlist and the same caps by construction."""
    kwargs, error = coerce_params(op, params or {})
    if error is not None:
        return error
    assert kwargs is not None
    function = globals()[OPERATIONS[op]["func"]]
    result = function(path, policy, **kwargs)
    result.setdefault("op", op)
    return result
