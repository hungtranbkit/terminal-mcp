"""What an agent is actually sent -- and, more importantly, what it is not.

THE EXPENSIVE PART OF AN AUTONOMOUS RUN IS NOT THE REASONING

It is the bookkeeping. A full project context resent on every revision. A
Planner invoked for a task whose acceptance criteria were already written
down. A fresh Evaluator session spun up to run a command whose exit status is
not a matter of opinion. A PM model woken on a timer to ask whether anything
changed. None of those are reasoning; all of them are billed as if they were.

This module is the half of the cost policy that shapes prompts. The other
half -- what may be skipped entirely -- is harness_policy (`planner_required`,
`evaluator_required`, `may_reuse_builder`, `budget_action`). Together the
rule is: an LLM is invoked to reason, to write code, or to judge evidence,
and for nothing else.

THE THREE MECHANISMS, AND WHY EACH ONE IS SHAPED AS IT IS

1. BOUNDED PACKS. A context pack has a byte ceiling, and files are admitted
   against it in a declared order until it is full. It never "includes the
   repository"; a pack that grows with the repository is a pack whose cost is
   set by something nobody decided. What does not fit is listed by path, so
   the agent knows the file exists and can open it -- a named omission is
   cheap and an unnoticed one is a bug.

2. CONTENT-ADDRESSED REUSE. The cache key is the hash of everything that went
   INTO the pack: the module list, each file's own content hash, the skill
   versions and the rules version. Invalidation is therefore automatic and
   there is nothing to expire: change a file and the key changes, so the old
   entry is simply never asked for again. A time-based cache would have to
   choose between serving a stale pack and paying to rebuild an identical one.

3. DELTA REVISIONS. This is the single biggest saving in the system. After a
   failed evaluation, the Builder is sent the failed criteria and the
   already-passing list -- not the contract again, not the pack again, not the
   evaluator's prose. A revision that re-reads everything re-does everything,
   which is how a targeted fix becomes a rewrite and how iteration three costs
   more than iteration one. The delta is only legal when the session is the
   SAME session that built the previous attempt: a replacement session has no
   history for a delta to be a delta of, and sending it one produces an agent
   confidently editing files it has never read.

TOKEN ESTIMATES ARE ESTIMATES, AND SAY SO

`estimate_tokens` is characters/4. It is not a tokenizer and does not pretend
to be one: it is used for budget ladders and for comparing two prompts built
by this same function, both of which only need the ratio to be right. Every
number this module produces is labelled `_estimate` for that reason -- a
field named `tokens` would be read as a bill.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import harness_policy as policy
from .harness_contract import EvaluationResult, ExecutionContract

#: Characters per token. Deliberately a single named constant rather than a
#: sprinkling of `// 4`, so the one place to fix it is obvious if a real
#: tokenizer is ever wired in.
CHARS_PER_TOKEN = 4

#: Ceilings for one assembled pack. A pack is a briefing, not an archive.
DEFAULT_PACK_BYTES = 48_000
DEFAULT_FILE_BYTES = 6_000
DEFAULT_MAX_FILES = 24

#: The version of the prompt/rule shaping in this module. It is part of every
#: cache key, so changing how a pack is rendered invalidates every cached pack
#: automatically instead of silently serving prompts built by the old shape.
CONTEXT_RULES_VERSION = "1"


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class SkillRef:
    """One bounded piece of injected know-how."""

    id: str
    version: str
    body: str
    tags: tuple[str, ...] = ()

    @property
    def bytes(self) -> int:
        return len(self.body.encode("utf-8"))

    def stamp(self) -> str:
        return f"{self.id}@{self.version}"


def select_skills(available: Sequence[SkillRef], *, tags: Sequence[str] = (),
                  max_skills: int = policy.MAX_INJECTED_SKILLS,
                  max_bytes: int = policy.MAX_SKILL_BYTES
                  ) -> tuple[tuple[SkillRef, ...], tuple[str, ...]]:
    """(selected, rejected_ids). Deterministic, bounded, and it explains itself.

    Relevance is tag overlap, ties broken by id so the same inputs always
    produce the same selection -- a non-deterministic selection would give two
    otherwise identical runs two different cache keys and quietly halve the
    hit rate.
    """
    wanted = {t.lower() for t in tags}
    scored = sorted(
        available,
        key=lambda s: (-len({t.lower() for t in s.tags} & wanted), s.id))
    selected: list[SkillRef] = []
    rejected: list[str] = []
    spent = 0
    for skill in scored:
        if len(selected) >= max_skills or spent + skill.bytes > max_bytes:
            rejected.append(skill.id)
            continue
        if wanted and not ({t.lower() for t in skill.tags} & wanted):
            rejected.append(skill.id)
            continue
        selected.append(skill)
        spent += skill.bytes
    return tuple(selected), tuple(rejected)


@dataclass(frozen=True)
class PackFile:
    path: str
    content_hash: str
    body: str
    truncated: bool = False

    @property
    def bytes(self) -> int:
        return len(self.body.encode("utf-8"))


@dataclass(frozen=True)
class ContextPack:
    """A bounded briefing about a set of modules, identified by its inputs."""

    cache_key: str
    project_id: str | None
    modules: tuple[str, ...]
    files: tuple[PackFile, ...] = ()
    omitted: tuple[str, ...] = ()
    skills: tuple[SkillRef, ...] = ()
    rules: tuple[str, ...] = ()

    @property
    def bytes(self) -> int:
        return len(self.render().encode("utf-8"))

    @property
    def tokens_estimate(self) -> int:
        return estimate_tokens(self.render())

    def render(self) -> str:
        parts: list[str] = []
        if self.modules:
            parts.append("# Modules in scope\n" + "\n".join(f"- {m}" for m in self.modules))
        if self.rules:
            parts.append("# Rules\n" + "\n".join(f"- {r}" for r in self.rules))
        for skill in self.skills:
            parts.append(f"# Skill {skill.stamp()}\n{skill.body}")
        for entry in self.files:
            suffix = "  (truncated)" if entry.truncated else ""
            parts.append(f"# File {entry.path}{suffix}\n{entry.body}")
        if self.omitted:
            # Named, not hidden: the agent can open what it needs, and a
            # missing file is never a silent gap in what it believes it saw.
            parts.append("# Present but not included (open if needed)\n"
                         + "\n".join(f"- {p}" for p in self.omitted))
        return "\n\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cache_key": self.cache_key, "project_id": self.project_id,
            "modules": list(self.modules),
            "files": [{"path": f.path, "content_hash": f.content_hash,
                       "bytes": f.bytes, "truncated": f.truncated}
                      for f in self.files],
            "omitted": list(self.omitted),
            "skills": [s.stamp() for s in self.skills],
            "rules": list(self.rules),
            "bytes": self.bytes,
            "tokens_estimate": self.tokens_estimate,
        }


def pack_cache_key(*, project_id: str | None, modules: Sequence[str],
                   file_hashes: Sequence[tuple[str, str]],
                   skill_stamps: Sequence[str],
                   rules_version: str = CONTEXT_RULES_VERSION,
                   pack_bytes: int = DEFAULT_PACK_BYTES) -> str:
    """The hash of everything that went into the pack. See mechanism 2 above."""
    blob = json.dumps({
        "project": project_id or "",
        "modules": sorted(modules),
        "files": sorted([list(pair) for pair in file_hashes]),
        "skills": sorted(skill_stamps),
        "rules_version": rules_version,
        "pack_bytes": pack_bytes,
    }, sort_keys=True, separators=(",", ":"))
    return "pack:" + _sha(blob)[:32]


@dataclass(frozen=True)
class Prompt:
    """What is actually sent, plus the accounting that proves what it cost."""

    role: str
    text: str
    delta: bool = False
    pack_key: str | None = None
    cache_hit: bool = False
    skills: tuple[str, ...] = ()

    @property
    def tokens_estimate(self) -> int:
        return estimate_tokens(self.text)

    @property
    def bytes(self) -> int:
        return len(self.text.encode("utf-8"))

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "delta": self.delta, "pack_key": self.pack_key,
                "cache_hit": self.cache_hit, "skills": list(self.skills),
                "bytes": self.bytes, "tokens_estimate": self.tokens_estimate}


class ContextAssembler:
    """Builds every prompt the engine sends, and records what each one cost.

    It holds the HarnessStore only to read and write the shared context cache
    and to bump efficiency counters. It never advances a run and never decides
    anything about a stage -- that is the engine's job, and keeping the two
    apart is what lets the cost accounting be tested without a state machine.
    """

    def __init__(self, store: Any, *, repo_root: str | Path | None = None,
                 pack_bytes: int = DEFAULT_PACK_BYTES,
                 file_bytes: int = DEFAULT_FILE_BYTES,
                 max_files: int = DEFAULT_MAX_FILES,
                 skills: Sequence[SkillRef] = ()) -> None:
        self.store = store
        self.repo_root = Path(repo_root) if repo_root else None
        self.pack_bytes = pack_bytes
        self.file_bytes = file_bytes
        self.max_files = max_files
        self.skills = tuple(skills)

    # -- packs ---------------------------------------------------------------
    def _read(self, relative: str) -> tuple[str, str, bool] | None:
        """(body, content_hash, truncated) or None if unreadable.

        Unreadable is not an error: a contract may name a path the Builder is
        about to CREATE, and refusing to assemble a pack because a file does
        not exist yet would make every greenfield task unrunnable.
        """
        if self.repo_root is None:
            return None
        target = (self.repo_root / relative).resolve()
        try:
            # The pack must not be able to read outside the repository, no
            # matter what a contract's affected_areas says.
            target.relative_to(self.repo_root.resolve())
        except ValueError:
            return None
        if not target.is_file():
            return None
        try:
            raw = target.read_bytes()
        except OSError:
            return None
        content_hash = _sha(raw)[:16]
        text = raw.decode("utf-8", errors="replace")
        if len(raw) > self.file_bytes:
            head = text[: self.file_bytes]
            return head, content_hash, True
        return text, content_hash, False

    def _candidate_paths(self, modules: Sequence[str]) -> list[str]:
        """Module names -> concrete repository paths, in a stable order.

        A module may be a file or a directory. A directory contributes its
        files sorted by path, so two runs over the same tree admit the same
        files in the same order and therefore share a cache key.
        """
        if self.repo_root is None:
            return []
        out: list[str] = []
        root = self.repo_root.resolve()
        for module in modules:
            target = (self.repo_root / module)
            if target.is_file():
                out.append(module)
                continue
            if not target.is_dir():
                continue
            for path in sorted(target.rglob("*")):
                if not path.is_file():
                    continue
                if any(part.startswith(".") or part in ("node_modules", "__pycache__",
                                                        "dist", "build")
                       for part in path.parts):
                    continue
                try:
                    out.append(str(path.resolve().relative_to(root)))
                except ValueError:
                    continue
        # Stable and deduplicated.
        seen: set[str] = set()
        ordered: list[str] = []
        for path in out:
            if path not in seen:
                seen.add(path)
                ordered.append(path)
        return ordered

    def build_pack(self, *, project_id: str | None, modules: Sequence[str],
                   tags: Sequence[str] = (), run_id: str | None = None,
                   ) -> tuple[ContextPack, bool]:
        """(pack, cache_hit).

        The key is computed from the file hashes BEFORE the pack is rendered,
        which is what makes a hit cost one read of the file list rather than a
        full assembly. A design that rendered first and hashed the render
        would pay the expensive half on every hit.
        """
        modules = tuple(dict.fromkeys(str(m).strip() for m in modules if str(m).strip()))
        selected_skills, _rejected = select_skills(self.skills, tags=tags)
        candidates = self._candidate_paths(modules)

        read: list[tuple[str, str, str, bool]] = []
        for relative in candidates[: self.max_files * 4]:
            found = self._read(relative)
            if found is None:
                continue
            body, content_hash, truncated = found
            read.append((relative, content_hash, body, truncated))

        key = pack_cache_key(
            project_id=project_id, modules=modules,
            file_hashes=[(r[0], r[1]) for r in read],
            skill_stamps=[s.stamp() for s in selected_skills],
            pack_bytes=self.pack_bytes)

        cached = self.store.cache_get(key) if self.store is not None else None
        if cached:
            payload = cached.get("payload") or {}
            if run_id is not None:
                self.store.bump_efficiency(run_id, reused_context_hits=1)
            # project_id is passed back in rather than read from the payload:
            # it identifies the ASKER, not the content, so it is deliberately
            # not part of the cached body. Without this a pack served from
            # cache would report a different project than the identical pack
            # served on a miss.
            return self._pack_from_payload(key, payload, selected_skills,
                                           project_id=project_id), True

        files: list[PackFile] = []
        omitted: list[str] = []
        spent = 0
        for relative, content_hash, body, truncated in read:
            size = len(body.encode("utf-8"))
            if len(files) >= self.max_files or spent + size > self.pack_bytes:
                omitted.append(relative)
                continue
            files.append(PackFile(path=relative, content_hash=content_hash,
                                  body=body, truncated=truncated))
            spent += size
        for relative in candidates[len(read):]:
            if relative not in omitted:
                omitted.append(relative)

        pack = ContextPack(cache_key=key, project_id=project_id, modules=modules,
                           files=tuple(files), omitted=tuple(omitted),
                           skills=selected_skills, rules=PACK_RULES)
        if self.store is not None:
            self.store.cache_put(key, self._payload(pack), project_id=project_id,
                                 modules=modules)
            if run_id is not None:
                self.store.bump_efficiency(
                    run_id, context_cache_misses=1, context_bytes=pack.bytes,
                    skills_injected=len(selected_skills),
                    skill_bytes=sum(s.bytes for s in selected_skills))
        return pack, False

    @staticmethod
    def _payload(pack: ContextPack) -> dict[str, Any]:
        return {
            "modules": list(pack.modules),
            "omitted": list(pack.omitted),
            "rules": list(pack.rules),
            "files": [{"path": f.path, "content_hash": f.content_hash,
                       "body": f.body, "truncated": f.truncated} for f in pack.files],
        }

    @staticmethod
    def _pack_from_payload(key: str, payload: dict[str, Any],
                           skills: Sequence[SkillRef],
                           project_id: str | None = None) -> ContextPack:
        return ContextPack(
            cache_key=key, project_id=project_id,
            modules=tuple(payload.get("modules") or ()),
            files=tuple(PackFile(path=f["path"], content_hash=f["content_hash"],
                                 body=f["body"], truncated=bool(f.get("truncated")))
                        for f in payload.get("files") or ()),
            omitted=tuple(payload.get("omitted") or ()),
            skills=tuple(skills),
            rules=tuple(payload.get("rules") or PACK_RULES))

    # -- prompts -------------------------------------------------------------
    def planner_prompt(self, *, task_title: str, task_prompt: str,
                       acceptance: Sequence[str] = (), checks: Sequence[str] = (),
                       pack: ContextPack | None = None,
                       cache_hit: bool = False) -> Prompt:
        sections = [
            "You are the Planner. Produce an ExecutionContract and nothing else.",
            PLANNER_CONTRACT_SHAPE,
            f"# Task\n{task_title}\n\n{task_prompt}",
        ]
        if acceptance:
            sections.append("# Acceptance already declared by the task\n"
                            + "\n".join(f"- {a}" for a in acceptance))
        if checks:
            sections.append("# Checks already declared by the task\n"
                            + "\n".join(f"- {c}" for c in checks))
        if pack is not None:
            sections.append(pack.render())
        return Prompt(role=policy.PLANNER, text="\n\n".join(sections),
                      pack_key=pack.cache_key if pack else None, cache_hit=cache_hit,
                      skills=tuple(s.stamp() for s in (pack.skills if pack else ())))

    def builder_prompt(self, *, contract: ExecutionContract, iteration: int,
                       pack: ContextPack | None = None, cache_hit: bool = False,
                       checkpoint: dict[str, Any] | None = None) -> Prompt:
        """The FULL prompt. Sent on iteration 1, and on any iteration whose
        Builder session is not the one that built the previous attempt."""
        sections = [
            "You are the Builder. Satisfy the contract below, then stop.",
            _render_contract(contract),
            BUILDER_RULES,
        ]
        if checkpoint:
            sections.append(_render_checkpoint(checkpoint))
        if pack is not None:
            sections.append(pack.render())
        return Prompt(role=policy.BUILDER, text="\n\n".join(sections), delta=False,
                      pack_key=pack.cache_key if pack else None, cache_hit=cache_hit,
                      skills=tuple(s.stamp() for s in (pack.skills if pack else ())))

    def revision_prompt(self, *, evaluation: EvaluationResult,
                        contract: ExecutionContract, iteration: int) -> Prompt:
        """DELTA ONLY. The failed criteria, the passing list, nothing else.

        Legal only when the receiving session is the same one that produced
        the attempt this evaluation judged -- the engine enforces that, and a
        replacement session gets `builder_prompt` with a checkpoint instead.
        """
        feedback = evaluation.feedback()
        failed = feedback["fix_only"]
        lines = [f"Revision {iteration}. The contract has not changed "
                 f"(hash {contract.content_hash[:12]}).",
                 "Fix ONLY the criteria below. Do not revisit anything else; "
                 "the rest already passed and re-editing it costs another "
                 "evaluation round.",
                 "# Failing criteria"]
        for entry in failed:
            lines.append(f"- [{entry['kind']}] {entry['text']}\n"
                         f"  evidence from the evaluator: {entry['evidence']}")
        if feedback["already_passing"]:
            lines.append("# Already passing -- leave alone\n"
                         + "\n".join(f"- {cid}" for cid in feedback["already_passing"]))
        for command, label in ((contract.test_command, "test"),
                               (contract.build_command, "build")):
            if command:
                lines.append(f"# Re-run the {label} check when done\n{command}")
        return Prompt(role=policy.BUILDER, text="\n\n".join(lines), delta=True)

    def adjacent_task_prompt(self, *, contract: ExecutionContract,
                             previous_task_id: str,
                             pack_key: str | None = None) -> Prompt:
        """The NEXT contract, handed to a session that already has the lane loaded.

        This is the cross-task cousin of `revision_prompt`, and it saves the
        same thing for a different reason. A revision omits the context
        because the session just built against it; this omits the context
        because the session built the task NEXT DOOR -- same lane, same
        modules, same toolchain, already read. What is genuinely new is the
        contract, and the contract is small.

        It is marked `delta` because that is what it is: everything the
        session does not already have. Sending the pack again here would be
        paying a second time for files that have not changed since the
        session read them, which is the single most common way a "fresh start
        per task" design becomes expensive.
        """
        lines = [
            f"Next task in this lane. You already have this lane's modules and "
            f"toolchain loaded from {previous_task_id}; nothing about them has "
            f"changed, so they are not repeated below.",
            _render_contract(contract),
            "Work only inside this contract's scope. If it needs files you have "
            "not opened in this session, open them -- do not assume the previous "
            "task's files are the same ones.",
        ]
        return Prompt(role=policy.BUILDER, text="\n\n".join(lines), delta=True,
                      pack_key=pack_key, cache_hit=True)

    def evaluator_prompt(self, *, contract: ExecutionContract, iteration: int,
                         result_commit: str | None = None,
                         diff_summary: str | None = None,
                         check_output: str | None = None) -> Prompt:
        """The Evaluator is sent the contract and the EVIDENCE, never the
        Builder's reasoning. It is judging the result, and a transcript of how
        the result was reached is both expensive and prejudicial."""
        sections = [
            "You are the Evaluator. Judge the result against the contract.",
            EVALUATOR_VERDICT_SHAPE,
            _render_contract(contract, criteria_ids=True),
        ]
        if result_commit:
            sections.append(f"# Result commit\n{result_commit}")
        if diff_summary:
            sections.append(f"# Diff\n{diff_summary}")
        if check_output:
            sections.append(f"# Check output\n{check_output}")
        return Prompt(role=policy.EVALUATOR, text="\n\n".join(sections))

    # -- accounting ----------------------------------------------------------
    def charge(self, run_id: str, prompt: Prompt, *,
               completion_estimate: int = 0) -> None:
        """Record one LLM call and what it is estimated to have cost.

        The engine calls this and only this; nothing else increments
        `llm_calls`, so the count cannot drift from the number of prompts
        actually built.
        """
        if self.store is None:
            return
        self.store.bump_efficiency(
            run_id,
            llm_calls=1,
            prompt_tokens_estimate=prompt.tokens_estimate + completion_estimate,
            delta_prompts=1 if prompt.delta else 0,
            full_prompts=0 if prompt.delta else 1,
        )


#: The rules every pack carries. Short on purpose: they are paid for on every
#: single prompt, so anything that is not load-bearing is a recurring cost.
PACK_RULES: tuple[str, ...] = (
    "Stay inside the contract's scope. Out-of-scope work is a redefine request, not a commit.",
    "Do not edit files outside the affected areas without saying why.",
    "Run the contract's required checks before reporting done.",
    "If the contract cannot be satisfied as written, say so; do not widen it.",
)

PLANNER_CONTRACT_SHAPE = """\
Answer with a JSON object only:
{"scope": str, "out_of_scope": [str], "affected_areas": [path],
 "functional_acceptance": [str], "visual_acceptance": [str],
 "performance_acceptance": [str], "security_acceptance": [str],
 "required_checks": [shell command], "manual_checks": [str],
 "test_command": str|null, "build_command": str|null, "dev_command": str|null}
Every acceptance criterion must be decidable from evidence a command or a
screenshot can produce. A criterion nobody can check is not a criterion."""

BUILDER_RULES = """\
Report done only when every required check has actually been run and passed.
If you are interrupted, leave the worktree committed and consistent: a
replacement builder continues from your commit, in your worktree, on your
branch."""

EVALUATOR_VERDICT_SHAPE = """\
Answer with a JSON object only:
{"result": "pass"|"fail"|"blocked"|"needs_redefine", "summary": str,
 "criteria": [{"criterion_id": str, "result": "pass"|"fail"|"blocked",
               "evidence": str, "artifact": path|null}]}
One entry per declared criterion, each with its own evidence. "looks good" is
not evidence and will be rejected as a malformed verdict."""


def _render_contract(contract: ExecutionContract, *, criteria_ids: bool = False) -> str:
    lines = [f"# Contract v{contract.version} (hash {contract.content_hash[:12]})",
             f"## Scope\n{contract.scope}"]
    if contract.out_of_scope:
        lines.append("## Out of scope\n" + "\n".join(f"- {s}" for s in contract.out_of_scope))
    if contract.affected_areas:
        lines.append("## Affected areas\n" + "\n".join(f"- {s}" for s in contract.affected_areas))
    accepted = ["## Acceptance"]
    for kind, text in contract.criteria():
        from .harness_contract import criterion_id
        prefix = f"[{criterion_id(kind, text)}] " if criteria_ids else f"[{kind}] "
        accepted.append(f"- {prefix}{text}")
    lines.append("\n".join(accepted))
    if contract.required_checks:
        lines.append("## Required checks\n" + "\n".join(f"- {c}" for c in contract.required_checks))
    if contract.manual_checks:
        lines.append("## Manual checks\n" + "\n".join(f"- {c}" for c in contract.manual_checks))
    return "\n\n".join(lines)


def _render_checkpoint(checkpoint: dict[str, Any]) -> str:
    """What a REPLACEMENT builder is told. Place first, progress second.

    The branch, worktree and commit come before the remaining work because a
    replacement that starts work before it knows where it is, is a
    replacement that starts a second worktree.
    """
    lines = ["# You are continuing an interrupted build -- not starting one",
             f"- worktree: {checkpoint.get('worktree_path')}",
             f"- branch: {checkpoint.get('branch')}",
             f"- last commit: {checkpoint.get('commit_sha')}",
             f"- iteration: {checkpoint.get('iteration')}"]
    done = checkpoint.get("checks") or []
    if done:
        lines.append("## Checks already run\n" + "\n".join(f"- {c}" for c in done))
    remaining = checkpoint.get("remaining") or []
    if remaining:
        lines.append("## Remaining\n" + "\n".join(f"- {r}" for r in remaining))
    if checkpoint.get("note"):
        lines.append(f"## Note from the previous builder\n{checkpoint['note']}")
    return "\n".join(lines)


# =============================================================================
# THE BASELINE THIS IS MEASURED AGAINST
# =============================================================================

@dataclass(frozen=True)
class NaiveBaseline:
    """What the same work costs with no cost policy at all.

    This is the comparison the pilot reports against, and it is computed from
    the SAME assembler and the SAME contract rather than quoted from memory,
    so the two sides of the comparison cannot drift apart.

    The naive model is the pipeline this feature replaces, stated exactly:
      * a Planner call for every task, whatever its definition already says
      * a full-context Builder call for every iteration, including revisions
      * a fresh full-context Evaluator call for every iteration
      * a PM/status model call on a fixed poll interval for the run's duration

    It is an ACCOUNTING model, not a measurement of a run that happened. It
    answers "how many calls and how much prompt would the old shape have
    required for this exact work", which is the honest question -- running the
    old pipeline again on the same tasks to get a measured number would cost
    exactly the money this feature exists to not spend.
    """

    llm_calls: int
    prompt_tokens_estimate: int
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"llm_calls": self.llm_calls,
                "prompt_tokens_estimate": self.prompt_tokens_estimate,
                "detail": dict(self.detail)}


#: How often the old continuous-PM loop woke a model to ask whether anything
#: had changed, and what that question cost in prompt. Both are the observed
#: shape of the pm/scheduler loops this feature removes.
NAIVE_PM_POLL_SECONDS = 60
NAIVE_PM_PROMPT_TOKENS = 1_200


def naive_baseline(*, full_prompt_tokens: int, iterations: int,
                   run_seconds: float = 0.0,
                   planner_tokens: int | None = None,
                   evaluator_tokens: int | None = None) -> NaiveBaseline:
    planner = planner_tokens if planner_tokens is not None else full_prompt_tokens
    evaluator = evaluator_tokens if evaluator_tokens is not None else full_prompt_tokens
    builder_total = full_prompt_tokens * max(1, iterations)
    evaluator_total = evaluator * max(1, iterations)
    polls = int(max(0.0, run_seconds) // NAIVE_PM_POLL_SECONDS)
    pm_total = polls * NAIVE_PM_PROMPT_TOKENS
    return NaiveBaseline(
        llm_calls=1 + max(1, iterations) * 2 + polls,
        prompt_tokens_estimate=planner + builder_total + evaluator_total + pm_total,
        detail={
            "planner_calls": 1, "planner_tokens": planner,
            "builder_calls": max(1, iterations), "builder_tokens": builder_total,
            "evaluator_calls": max(1, iterations), "evaluator_tokens": evaluator_total,
            "pm_poll_calls": polls, "pm_poll_tokens": pm_total,
        })


def naive_parallel_baseline(*, tasks: int, full_prompt_tokens: int,
                            iterations_per_task: int = 1,
                            wall_clock_seconds: float = 0.0,
                            coordination_calls_per_task: int = 1) -> NaiveBaseline:
    """The "just run everything at once" shape, costed.

    Parallel execution does not reduce the number of LLM calls -- it makes
    them happen at the same time. What it DOES change is the two things this
    design saves on, and it changes both for the worse:

      * no session is reused across tasks, so every task pays a full context
        load rather than inheriting a lane's warm session;
      * concurrent work needs coordinating, and the coordinator in the shape
        being replaced was a model, asked once per task at minimum and on a
        timer beyond that.

    So the parallel baseline is the sequential one plus a coordination call
    per task, with no cache credit anywhere. The honest comparison is against
    THIS rather than against a hypothetical perfect parallel scheduler,
    because this is the shape that actually existed.
    """
    sequential = naive_baseline(full_prompt_tokens=full_prompt_tokens,
                                iterations=iterations_per_task,
                                run_seconds=0.0)
    per_task_calls = sequential.llm_calls + coordination_calls_per_task
    per_task_tokens = (sequential.prompt_tokens_estimate
                       + coordination_calls_per_task * NAIVE_PM_PROMPT_TOKENS)
    polls = int(max(0.0, wall_clock_seconds) // NAIVE_PM_POLL_SECONDS)
    return NaiveBaseline(
        llm_calls=tasks * per_task_calls + polls,
        prompt_tokens_estimate=tasks * per_task_tokens + polls * NAIVE_PM_PROMPT_TOKENS,
        detail={
            "tasks": tasks,
            "per_task_calls": per_task_calls,
            "per_task_tokens": per_task_tokens,
            "planner_calls": tasks,
            "builder_calls": tasks * iterations_per_task,
            "evaluator_calls": tasks * iterations_per_task,
            "coordination_calls": tasks * coordination_calls_per_task,
            "pm_poll_calls": polls,
            "context_loads": tasks * iterations_per_task,
            "cache_reuse": 0,
        })
