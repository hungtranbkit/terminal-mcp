"""What a project IS, and the smallest team that could build it.

DETERMINISTIC, METADATA-FIRST, NO MODEL CALL

The same reasoning as task_profile.py: a team proposal that needs an LLM is
slower, irreproducible, and unavailable exactly when the model endpoint is the
thing that is down. So the profile is computed from what the caller already
supplied plus what the repository already contains, in a fixed order, with
every conclusion carrying the evidence that produced it.

A caller who wants a model in the loop can still have one -- they read the
plan, change it, and pass it back. The base path never requires it.

RIGHT-SIZING IS THE POINT

The failure mode this exists to avoid is proposing the same eight-agent
organisation for a one-page utility. Modules are detected, not assumed; a role
is proposed only when a module justifies it; and the count is bounded by the
complexity band. A small project gets two or three agents because that is
genuinely all it needs, and every agent carries the reason it exists so an
operator can delete the ones they disagree with.

DUPLICATE RESPONSIBILITY IS A BUG

Two agents that would answer the same question are worse than one: work goes
to whichever the matcher happened to score higher, and neither accumulates the
context. Each role owns a disjoint set of capabilities, and the generator
never emits the same role twice.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

# -- complexity bands ----------------------------------------------------
SMALL = "small"
MEDIUM = "medium"
LARGE = "large"

#: (minimum agents, maximum agents) per band. The task's own sizing rule,
#: stated once so the generator cannot drift from it.
TEAM_SIZE: dict[str, tuple[int, int]] = {SMALL: (2, 3), MEDIUM: (3, 5), LARGE: (5, 8)}

#: Modules a description or a repository can reveal. The vocabulary is
#: deliberately small: each one has to justify a distinct agent role, and a
#: module nobody would staff separately is noise.
MODULE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("frontend", re.compile(
        r"(?i)\b(web ?app|website|frontend|front-end|ui|ux|dashboard|map|page|react|vue|svelte"
        r"|next\.?js|tailwind|giao di[eệ]n|trang web)\b")),
    ("backend", re.compile(
        r"(?i)\b(backend|back-end|api|server|endpoint|rest|graphql|database|db|postgres|mysql"
        r"|sqlite|auth|m[aá]y ch[uủ]|c[oơ] s[oở] d[uữ] li[eệ]u)\b")),
    ("ai", re.compile(
        r"(?i)\b(ai|ml|machine learning|vision|detect\w*|inference|model|llm|ocr|classif\w*"
        r"|analysis|analytics|camera stream|tr[ií] tu[eệ] nh[aâ]n t[aạ]o|nh[aậ]n di[eệ]n)\b")),
    ("streaming", re.compile(
        r"(?i)\b(stream\w*|rtsp|hls|webrtc|realtime|real-time|websocket|live feed|camera feed)\b")),
    ("mobile", re.compile(r"(?i)\b(mobile|ios|android|react native|flutter|app di [dđ][oộ]ng)\b")),
    ("data", re.compile(
        r"(?i)\b(etl|pipeline|ingest\w*|warehouse|report\w*|dataset|scraping|crawler)\b")),
    ("deploy", re.compile(
        r"(?i)\b(deploy\w*|release|ci/?cd|docker|kubernetes|k8s|terraform|infra\w*|hosting"
        r"|tri[eể]n khai|ph[aá]t h[aà]nh)\b")),
    ("qa", re.compile(r"(?i)\b(test\w*|qa|quality|review|audit|ki[eể]m th[uử]|r[aà] so[aá]t)\b")),
)

#: Repository fingerprints -> (stack label, implied module). Only files that
#: are unambiguous; a heuristic that fires on a stray README mention would make
#: the detected stack worse than no stack at all.
STACK_FILES: tuple[tuple[str, str, str | None], ...] = (
    ("package.json", "node", "frontend"),
    ("pnpm-lock.yaml", "node", None),
    ("yarn.lock", "node", None),
    ("tsconfig.json", "typescript", None),
    ("next.config.js", "nextjs", "frontend"),
    ("next.config.mjs", "nextjs", "frontend"),
    ("vite.config.ts", "vite", "frontend"),
    ("tailwind.config.js", "tailwind", "frontend"),
    ("requirements.txt", "python", "backend"),
    ("pyproject.toml", "python", "backend"),
    ("manage.py", "django", "backend"),
    ("go.mod", "go", "backend"),
    ("Cargo.toml", "rust", "backend"),
    ("pom.xml", "java", "backend"),
    ("build.gradle", "java", "backend"),
    ("Gemfile", "ruby", "backend"),
    ("composer.json", "php", "backend"),
    ("Dockerfile", "docker", "deploy"),
    ("docker-compose.yml", "docker", "deploy"),
    ("Makefile", "make", None),
    ("terraform.tf", "terraform", "deploy"),
)

#: Directories whose presence implies a module. Checked shallowly.
STACK_DIRS: tuple[tuple[str, str, str | None], ...] = (
    (".github/workflows", "github-actions", "deploy"),
    ("k8s", "kubernetes", "deploy"),
    ("charts", "helm", "deploy"),
    ("migrations", "migrations", "backend"),
    ("tests", "tests", "qa"),
    ("test", "tests", "qa"),
    ("notebooks", "notebooks", "ai"),
    ("models", "models", "ai"),
    ("mobile", "mobile", "mobile"),
    ("ios", "ios", "mobile"),
    ("android", "android", "mobile"),
)

#: How many entries of the repo root we will look at. A project directory with
#: a hundred thousand entries must not turn a plan into a filesystem walk.
MAX_REPO_ENTRIES = 400


# -- built-in skill templates --------------------------------------------
#
# PROMPT TEXT, NEVER CODE. Generating executable content from a description is
# the line this feature does not cross: these are standing instructions an
# agent reads, so the worst case of a wrong one is a badly-briefed agent, not
# an arbitrary program running on the fleet.
BUILTIN_SKILLS: dict[str, dict[str, str]] = {
    "core-engineering": {
        "name": "Core Engineering",
        "summary": "own the project's shape, decide what to build next, keep changes coherent",
        "body": (
            "You own this project's overall shape.\n\n"
            "- Read the existing code before proposing a change; the repository is the source "
            "of truth and any plan disagrees with it at its own risk.\n"
            "- Prefer extending something that exists to adding something beside it. Two "
            "implementations of one idea cost every future change, not just this one.\n"
            "- State assumptions explicitly when the request is ambiguous, then proceed; do "
            "not stall on a question you can answer by reading.\n"
            "- Keep each change small enough to review and describe in one sentence."
        ),
    },
    "frontend-ui": {
        "name": "Frontend & UI",
        "summary": "build and maintain the user interface, layout, and client-side behaviour",
        "body": (
            "You own the user interface.\n\n"
            "- Match the existing component structure, naming and styling conventions; a "
            "screen that looks foreign to the rest of the app is a defect.\n"
            "- Make it work at phone width. Check the real rendered result, not the markup.\n"
            "- Never introduce a second design system or a competing state container.\n"
            "- Escape anything that came from a user or an API before it reaches the DOM."
        ),
    },
    "backend-api": {
        "name": "Backend & API",
        "summary": "own server endpoints, data model, storage and migrations",
        "body": (
            "You own the server side.\n\n"
            "- Schema changes go through the project's existing migration mechanism, in order, "
            "tracked. Never an ad-hoc ALTER on a live database.\n"
            "- Validate every input at the boundary; an endpoint that trusts its caller is the "
            "vulnerability, whatever the client does.\n"
            "- Keep the API's error vocabulary stable -- callers depend on the shape of a "
            "failure as much as on a success.\n"
            "- Make writes idempotent where a retry is possible, which is nearly everywhere."
        ),
    },
    "ai-vision": {
        "name": "AI & Vision",
        "summary": "model integration, inference pipelines, detection and analysis quality",
        "body": (
            "You own the model-facing work.\n\n"
            "- Record which model and which version produced a result. An unattributed "
            "inference cannot be reproduced or compared.\n"
            "- Measure before tuning: state the current accuracy or latency, then the target.\n"
            "- Handle the empty, corrupt and timed-out input explicitly; those are the normal "
            "cases in a streaming pipeline, not the exceptions.\n"
            "- Never let a model's confidence be reported as certainty downstream."
        ),
    },
    "streaming-media": {
        "name": "Streaming & Media",
        "summary": "live feeds, transport, buffering and reconnection behaviour",
        "body": (
            "You own the live media path.\n\n"
            "- Assume every stream drops. Reconnection, backoff and a visible degraded state "
            "are part of the feature, not a follow-up.\n"
            "- Bound every buffer. An unbounded queue in a media path is an outage waiting for "
            "a slow consumer.\n"
            "- Report the real observed frame rate and latency rather than the configured one."
        ),
    },
    "data-pipeline": {
        "name": "Data Pipeline",
        "summary": "ingestion, transformation, storage and reporting correctness",
        "body": (
            "You own how data arrives and what it becomes.\n\n"
            "- Make every stage re-runnable. A pipeline that cannot be replayed cannot be "
            "fixed after a bad day.\n"
            "- Count what you dropped and why; silent loss is the failure nobody notices.\n"
            "- Keep raw input separable from derived output, so a transformation bug is "
            "recoverable."
        ),
    },
    "mobile-app": {
        "name": "Mobile",
        "summary": "mobile client behaviour, offline handling and platform constraints",
        "body": (
            "You own the mobile client.\n\n"
            "- Assume an intermittent network and a cold start; both are the common case.\n"
            "- Respect platform conventions over cross-platform uniformity where they "
            "conflict.\n"
            "- Keep the battery and data cost of background work explicit."
        ),
    },
    "project-planning": {
        "name": "Project Planning",
        "summary": "own the backlog, priorities and the plan across the whole project",
        "body": (
            "You coordinate this project. You do not implement it.\n\n"
            "- Keep one ordered backlog. A priority that is not relative to something else "
            "is not a priority.\n"
            "- Decompose work until each item has an owner and an acceptance criterion.\n"
            "- Track dependencies and blockers explicitly; a blocked item with no named "
            "blocker is an item nobody is unblocking.\n"
            "- Report status as what is done, what is in flight and what is at risk -- never "
            "as a percentage you cannot defend.\n"
            "- You do not write production code and you do not approve your own project's "
            "review, QA or release gates. Those belong to the specialists."
        ),
    },
    "task-decomposition": {
        "name": "Task Decomposition",
        "summary": "split goals into assignable tasks with clear boundaries",
        "body": (
            "You turn goals into tasks.\n\n"
            "- One task, one owner, one acceptance criterion. If two people must both finish "
            "it, it is two tasks.\n"
            "- Say which tasks can run in parallel and which must wait; that is the plan.\n"
            "- Size to something reviewable. A task nobody can review in one sitting will be "
            "approved without being read."
        ),
    },
    "dependency-management": {
        "name": "Dependency Management",
        "summary": "track what blocks what, and act when something stalls",
        "body": (
            "You keep the work moving.\n\n"
            "- Record the dependency when you create the task, not when it blocks.\n"
            "- A task that has not moved needs a reason, an owner and a next action -- all "
            "three, or it is not being managed.\n"
            "- When a runtime dies, the work is not lost: reassign it and say what was "
            "already completed."
        ),
    },
    "phase-gates": {
        "name": "Phase Gates",
        "summary": "collect the evidence a phase is complete and recommend transitions",
        "body": (
            "You decide when to RECOMMEND a phase transition, and collect the evidence for "
            "it.\n\n"
            "- A gate is satisfied by evidence, not by elapsed time or by everyone feeling "
            "ready.\n"
            "- Recommend the transition and record what satisfied the gate; the independent "
            "approval for review, QA and release is not yours to give.\n"
            "- Going backwards is a legitimate recommendation. A failed review returning to "
            "BUILD is the process working."
        ),
    },
    "handoff-coordination": {
        "name": "Handoff Coordination",
        "summary": "make sure the next phase receives what the last one produced",
        "body": (
            "You own the seam between phases.\n\n"
            "- A handoff is artifacts plus open questions. Passing on the artifacts alone "
            "makes the next phase rediscover the questions.\n"
            "- Name what was decided and what was deliberately deferred.\n"
            "- If the outgoing phase produced nothing durable, say so rather than letting the "
            "next phase assume context that does not exist."
        ),
    },
    "status-reporting": {
        "name": "Status & Risk Reporting",
        "summary": "report real progress, risks and next actions",
        "body": (
            "You report what is actually happening.\n\n"
            "- Lead with what changed since last time, then risks, then next actions.\n"
            "- A risk needs a trigger and a response, or it is an anxiety.\n"
            "- Report failures and stalls as plainly as progress; a status that only contains "
            "good news trains its reader to stop believing it."
        ),
    },
    "intake-discovery": {
        "name": "Intake & Discovery",
        "summary": "turn a request into a written goal, scope and constraints",
        "body": (
            "You turn a request into something buildable.\n\n"
            "- Write down the goal, what is explicitly out of scope, and the constraints. "
            "An unwritten constraint is one the build will violate.\n"
            "- Name the unknowns instead of guessing past them; an open question recorded is "
            "cheaper than a wrong assumption discovered in review.\n"
            "- Do not design the solution here. Deciding how before knowing what is the most "
            "expensive mistake available at this stage."
        ),
    },
    "architecture-analysis": {
        "name": "Architecture & Analysis",
        "summary": "read the existing system, choose the shape, name the risks",
        "body": (
            "You decide the shape before anyone writes code.\n\n"
            "- Read what exists first. A design that ignores the current system is a rewrite "
            "proposal wearing a feature's name.\n"
            "- Prefer extending an existing mechanism to adding a parallel one, and say so "
            "explicitly when you do not.\n"
            "- State the two or three risks most likely to sink this, and what would retire "
            "each one."
        ),
    },
    "task-planning": {
        "name": "Task Planning",
        "summary": "break the work into tasks with acceptance criteria",
        "body": (
            "You break the work down.\n\n"
            "- Every task gets an acceptance criterion someone else can check. 'Implement the "
            "API' is not a task; 'POST /cameras returns 201 and persists' is.\n"
            "- Order by dependency, and say which tasks can run in parallel.\n"
            "- You do not decide whether the work is done later -- that is QA's call, and "
            "writing the criteria is already your influence on it."
        ),
    },
    "code-review": {
        "name": "Code Review",
        "summary": "read a change you did not write and find what is wrong with it",
        "body": (
            "You read changes you did not write.\n\n"
            "- Look for the bug, not the style. A correctness defect missed because the "
            "review spent its attention on naming is the failure mode here.\n"
            "- Check the error path and the empty case; those are what the author tested "
            "least.\n"
            "- Say plainly when you did not understand a section rather than approving it."
        ),
    },
    "operate-support": {
        "name": "Operate & Support",
        "summary": "keep it running; triage incidents; report real behaviour",
        "body": (
            "You own the thing now that it is live.\n\n"
            "- Report what the system is actually doing, from its own logs and metrics, not "
            "what it was designed to do.\n"
            "- Triage before fixing: scope, blast radius, and whether a rollback is faster.\n"
            "- An incident with no written cause will happen again."
        ),
    },
    "qa-verification": {
        "name": "QA & Verification",
        "summary": "prove a change works; review for correctness before it ships",
        "body": (
            "You decide whether a change is actually done.\n\n"
            "- A claim of completion needs evidence: the command that ran, and its output. "
            "'Should work' is not a result.\n"
            "- Test the failure path, not only the happy one. The happy path is the part that "
            "was already tried during development.\n"
            "- Report what you did NOT verify as plainly as what you did.\n"
            "- A failing test is information; deleting it is not a fix."
        ),
    },
    "release-deploy": {
        "name": "Release & Deploy",
        "summary": "build, deploy and verify a release; own rollback",
        "body": (
            "You own getting it out and proving it landed.\n\n"
            "- Verify the deployed artifact is the one you built -- check the live version or "
            "commit, never assume the deploy succeeded because the command exited zero.\n"
            "- Know the rollback before you start, and say what it is.\n"
            "- Deploying is not the same as done; confirm the thing works where it now runs.\n"
            "- Never deploy on top of an unverified change to get it out faster."
        ),
    },
}

#: -- PHASES ---------------------------------------------------------------
#:
#: A project is not a standing organisation; it is a pipeline. Keeping every
#: role alive from day one means paying for a QA agent while nobody has
#: written anything, and -- worse -- it means the build agents are already
#: present and idle when the planning phase should own the decisions. So the
#: team is derived from the CURRENT PHASE, and agents are materialised as the
#: project reaches the phase that needs them.
INTAKE = "INTAKE"
ANALYSIS = "ANALYSIS"
PLANNING = "PLANNING"
BUILD = "BUILD"
REVIEW = "REVIEW"
TEST = "TEST"
RELEASE = "RELEASE"
OPERATE = "OPERATE"

PHASES: tuple[str, ...] = (INTAKE, ANALYSIS, PLANNING, BUILD, REVIEW, TEST, RELEASE, OPERATE)

#: The ordinary forward path. A project may also be sent BACKWARD (a failed
#: review returns to BUILD), which is why this is a sequence and not a
#: one-way ratchet -- see `next_phase` and the store's transition record.
PHASE_ORDER: dict[str, int] = {phase: index for index, phase in enumerate(PHASES)}

#: What each phase must produce before it is allowed to advance. Deliberately
#: prose, not a checker: the gate is evidence a human or an agent supplies,
#: and a machine that invented its own pass/fail here would be asserting the
#: work was done rather than recording that somebody said so.
PHASE_GATE: dict[str, str] = {
    INTAKE: "the goal, scope and constraints are written down",
    ANALYSIS: "the stack, modules and main risks are identified",
    PLANNING: "the work is broken into tasks with acceptance criteria",
    BUILD: "the implementation is complete and committed",
    REVIEW: "the change has been read by someone who did not write it",
    TEST: "acceptance criteria are verified with evidence",
    RELEASE: "the artifact is deployed and the deployment is confirmed live",
    OPERATE: "the project is running; this phase does not advance on its own",
}

#: role -> (title, base skills, the capabilities this role OWNS). Capability
#: sets are disjoint on purpose -- see the module docstring on duplicates.
ROLE_CATALOGUE: dict[str, dict[str, Any]] = {
    # The one role that spans the whole project. See CROSS_PHASE_ROLES.
    "pm": {"title": "Project Manager",
           "skills": ("project-planning", "task-decomposition", "dependency-management",
                      "phase-gates", "handoff-coordination", "status-reporting"),
           "capabilities": ("coordination", "backlog", "dependencies", "handoff",
                            "status", "risk", "orchestration")},
    # Pipeline roles -- each owns one phase's thinking.
    "analyst": {"title": "Analyst", "skills": ("intake-discovery",),
                "capabilities": ("intake", "discovery", "requirements")},
    "architect": {"title": "Architect", "skills": ("architecture-analysis",),
                  "capabilities": ("architecture", "analysis", "design")},
    "planner": {"title": "Planner", "skills": ("task-planning",),
                "capabilities": ("planning", "breakdown", "estimation")},
    # Build roles -- each owns one module of the thing being made.
    "core": {"title": "Core", "skills": ("core-engineering",),
             "capabilities": ("build", "refactor", "general")},
    "ui": {"title": "Frontend", "skills": ("frontend-ui",),
           "capabilities": ("frontend", "ui", "design-impl")},
    "backend": {"title": "Backend", "skills": ("backend-api",),
                "capabilities": ("backend", "api", "database", "auth")},
    "ai": {"title": "AI", "skills": ("ai-vision",),
           "capabilities": ("ai", "ml", "vision", "inference")},
    "streaming": {"title": "Streaming", "skills": ("streaming-media",),
                  "capabilities": ("streaming", "media", "realtime")},
    "data": {"title": "Data", "skills": ("data-pipeline",),
             "capabilities": ("data", "etl", "reporting")},
    "mobile": {"title": "Mobile", "skills": ("mobile-app",),
               "capabilities": ("mobile", "ios", "android")},
    # Assurance roles -- deliberately NOT build roles. See SEPARATION_OF_DUTIES.
    "reviewer": {"title": "Reviewer", "skills": ("code-review",),
                 "capabilities": ("review", "readthrough")},
    "qa": {"title": "QA", "skills": ("qa-verification",),
           "capabilities": ("qa", "test", "verification", "acceptance")},
    "release": {"title": "Release", "skills": ("release-deploy",),
                "capabilities": ("deploy", "release", "ci", "rollback")},
    "support": {"title": "Support", "skills": ("operate-support",),
                "capabilities": ("operate", "support", "incident")},
}

#: The canonical roles each phase activates, before module filtering and
#: before collapsing. BUILD is expanded per detected module.
#: The PM is NOT listed here: it is added to every phase by roles_for_phase,
#: because a cross-phase role that had to be repeated in eight rows would
#: eventually be missing from one of them.
PHASE_ROLES: dict[str, tuple[str, ...]] = {
    INTAKE: ("analyst",),
    ANALYSIS: ("architect",),
    PLANNING: ("planner",),
    BUILD: ("core",),          # plus one role per detected module
    REVIEW: ("reviewer",),
    TEST: ("qa",),
    RELEASE: ("release",),
    OPERATE: ("support",),
}

#: module -> the BUILD role that owns it.
MODULE_ROLE: dict[str, str] = {
    "frontend": "ui", "backend": "backend", "ai": "ai", "streaming": "streaming",
    "data": "data", "mobile": "mobile",
}

#: Roles that persist for the WHOLE project rather than one phase.
#:
#: The PM is the only one. Everything else is staffed for the phase that needs
#: it and goes dormant afterwards; the PM is what makes that survivable, since
#: somebody has to hold the backlog, the dependencies and the handoffs across
#: a team that keeps changing shape. It is a coordinator, not an implementer:
#: it owns no module, writes no production code, and cannot supply an
#: independent approval (see ASSURANCE_ROLES).
CROSS_PHASE_ROLES: frozenset[str] = frozenset({"pm"})

#: The only roles that may satisfy a REVIEW/TEST/RELEASE gate. Deliberately
#: excludes both the build roles (they wrote it) and the PM (it planned it and
#: asked for it to be done).
ASSURANCE_ROLES: frozenset[str] = frozenset({"reviewer", "qa", "release"})

#: Roles that WRITE the thing. An agent holding one of these must not also be
#: the independent approval in REVIEW/TEST/RELEASE -- see SEPARATION_OF_DUTIES.
BUILD_ROLES: frozenset[str] = frozenset(
    {"core", "ui", "backend", "ai", "streaming", "data", "mobile"})

#: Phases whose whole purpose is that somebody OTHER than the author looks.
APPROVAL_PHASES: frozenset[str] = frozenset({REVIEW, TEST, RELEASE})

SEPARATION_OF_DUTIES = """A build role may not supply the approval in REVIEW, TEST or RELEASE.

The point of those phases is a second pair of eyes. An agent that approves its
own work provides the ceremony and none of the value, and it fails in the
specific way that matters: the mistake an author cannot see is exactly the one
review exists to catch. The planner is likewise kept off final QA, so the
person who decided what "done" means is not also the one declaring it reached.

Collapsing may merge planning into build on a small project, and may merge
review into QA. It may never merge a build role into an assurance role, in
either direction. A project that genuinely wants self-approval sets
`policy.allow_self_approval`, which is recorded on the project rather than
inferred from its size."""

#: How roles merge as a project gets smaller. A one-person project still gets
#: an independent QA agent -- that is the line collapsing does not cross.
ROLE_COLLAPSE: dict[str, dict[str, str]] = {
    # `pm` appears in no collapse table at any size: a small project still
    # gets its coordinator, it just gets fewer specialists to coordinate.
    SMALL: {"analyst": "core", "architect": "core", "planner": "core",
            "ui": "core", "backend": "core", "ai": "core", "streaming": "core",
            "data": "core", "mobile": "core",
            "reviewer": "qa", "release": "qa", "support": "core"},
    MEDIUM: {"analyst": "planner", "architect": "planner",
             "reviewer": "qa", "support": "core"},
    LARGE: {},
}

#: Ordered by how early a pipeline needs them; used when a band's ceiling
#: forces a cut. A phase's OWN role is never cut -- see plan_phase_team.
ROLE_PRIORITY: tuple[str, ...] = (
    "pm", "analyst", "architect", "planner", "core", "backend", "ui", "ai", "streaming",
    "data", "mobile", "reviewer", "qa", "release", "support")

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value: str, *, fallback: str = "project") -> str:
    slug = _SLUG_STRIP.sub("-", str(value or "").strip().lower()).strip("-")
    return (slug or fallback)[:48]


@dataclass(frozen=True)
class ProjectProfile:
    project_id: str
    name: str
    description: str = ""
    repo_root: str | None = None
    domain: str = "software"
    stack: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    complexity: str = SMALL
    signals: dict[str, Any] = field(default_factory=dict)
    """How each conclusion was reached: which words, which files. Shown in the
    wizard so a surprising plan can be traced to its input."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id, "name": self.name, "description": self.description,
            "repo_root": self.repo_root, "domain": self.domain, "stack": list(self.stack),
            "modules": list(self.modules), "capabilities": list(self.capabilities),
            "complexity": self.complexity, "signals": self.signals,
        }


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    role: str
    name: str
    description: str
    reason: str
    runtime: str | None = None
    max_sessions: int = 1
    base_skills: tuple[str, ...] = ()
    task_skills: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    phases: tuple[str, ...] = ()
    """Which phases this agent is active in. An agent is materialised when the
    project first reaches one of them, and goes DORMANT when it leaves."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id, "role": self.role, "name": self.name,
            "description": self.description, "reason": self.reason, "runtime": self.runtime,
            "max_sessions": self.max_sessions, "base_skills": list(self.base_skills),
            "task_skills": list(self.task_skills), "capabilities": list(self.capabilities),
            "phases": list(self.phases),
            "is_build_role": self.role in BUILD_ROLES,
            "cross_phase": self.role in CROSS_PHASE_ROLES,
            "can_approve": self.role in ASSURANCE_ROLES,
        }


@dataclass(frozen=True)
class TeamPlan:
    project_id: str
    profile: ProjectProfile
    agents: tuple[AgentSpec, ...] = ()
    notes: tuple[str, ...] = ()
    phase: str = INTAKE
    """The phase `agents` is the team FOR. Bootstrap materialises only this."""
    upcoming: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    """phase -> the agents that phase will need, each flagged with whether an
    earlier phase already created it. Shown, never created in advance."""

    @property
    def size(self) -> int:
        return len(self.agents)

    def to_dict(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "profile": self.profile.to_dict(),
                "phase": self.phase,
                "agents": [agent.to_dict() for agent in self.agents],
                "upcoming": self.upcoming,
                "phases": list(PHASES),
                "phase_gate": PHASE_GATE.get(self.phase),
                "next_phase": next_phase(self.phase),
                "size": self.size, "notes": list(self.notes),
                "size_band": list(TEAM_SIZE[self.profile.complexity])}


def _scan_repo(repo_root: str | None) -> tuple[tuple[str, ...], tuple[str, ...], list[str]]:
    """(stack, modules implied, evidence). Shallow and bounded.

    Read-only and never raises: a plan must still be produced for a path that
    does not exist, is unreadable, or is not a repository at all."""
    if not repo_root or not os.path.isdir(repo_root):
        return (), (), []
    root = Path(repo_root)
    stack: list[str] = []
    modules: list[str] = []
    evidence: list[str] = []
    try:
        names = {entry.name for _, entry in zip(range(MAX_REPO_ENTRIES), root.iterdir())}
    except OSError:
        return (), (), []
    for filename, label, module in STACK_FILES:
        if filename in names and (root / filename).is_file():
            if label not in stack:
                stack.append(label)
                evidence.append(f"{filename} -> {label}")
            if module and module not in modules:
                modules.append(module)
    for dirname, label, module in STACK_DIRS:
        if (root / dirname).is_dir():
            if label not in stack:
                stack.append(label)
                evidence.append(f"{dirname}/ -> {label}")
            if module and module not in modules:
                modules.append(module)
    return tuple(stack), tuple(modules), evidence


def _complexity(modules: Sequence[str], description: str, requested: str | None) -> tuple[str, str]:
    """The band, and why. An explicit request always wins -- the caller knows
    things the description does not say."""
    if requested in TEAM_SIZE:
        return requested, f"caller requested the {requested} profile"
    count = len(modules)
    if count >= 5:
        return LARGE, f"{count} distinct modules detected"
    if count >= 3:
        return MEDIUM, f"{count} distinct modules detected"
    if count <= 1 and len(description) < 80:
        return SMALL, "one module and a short description"
    return SMALL, f"{count} module(s) detected"


def analyze(name: str, *, description: str = "", repo_root: str | None = None,
            project_id: str | None = None, stack: Sequence[str] = (),
            modules: Sequence[str] = (), capabilities: Sequence[str] = (),
            complexity: str | None = None) -> ProjectProfile:
    """Describe the project. Caller-supplied values always beat detection."""
    text = f"{name} {description}"
    detected_modules: list[str] = []
    matched_words: dict[str, str] = {}
    for module, pattern in MODULE_PATTERNS:
        match = pattern.search(text)
        if match:
            detected_modules.append(module)
            matched_words[module] = match.group(0)

    repo_stack, repo_modules, repo_evidence = _scan_repo(repo_root)
    for module in repo_modules:
        if module not in detected_modules:
            detected_modules.append(module)

    for module in modules:
        if module not in detected_modules:
            detected_modules.append(str(module))

    merged_stack = list(dict.fromkeys([*repo_stack, *(str(item) for item in stack)]))
    band, band_reason = _complexity(detected_modules, description, complexity)

    owned: list[str] = []
    for module in detected_modules:
        role = MODULE_ROLE.get(module)
        if role:
            owned.extend(ROLE_CATALOGUE[role]["capabilities"])
    owned.extend(str(item) for item in capabilities)

    return ProjectProfile(
        project_id=project_id or slugify(name),
        name=name, description=description, repo_root=repo_root,
        domain="software",
        stack=tuple(merged_stack),
        modules=tuple(dict.fromkeys(detected_modules)),
        capabilities=tuple(dict.fromkeys(owned)),
        complexity=band,
        signals={"description_matches": matched_words, "repo_evidence": repo_evidence,
                 "complexity_reason": band_reason,
                 "repo_scanned": bool(repo_root and os.path.isdir(repo_root))},
    )


def collapse_role(role: str, complexity: str) -> str:
    """The role that actually does `role`'s work at this project size.

    Never collapses a build role into an assurance role or the reverse -- see
    SEPARATION_OF_DUTIES. The tables encode that; this function only applies
    them, and asserts the invariant so a future edit to the table cannot break
    it silently."""
    merged = ROLE_COLLAPSE.get(complexity, {}).get(role, role)
    if role in CROSS_PHASE_ROLES and merged != role:
        raise ValueError(
            f"collapse table would merge cross-phase role {role!r} into {merged!r}; the "
            f"project manager persists at every size")
    if role in BUILD_ROLES and merged in ASSURANCE_ROLES:
        raise ValueError(
            f"collapse table would merge build role {role!r} into assurance role "
            f"{merged!r}, which defeats separation of duties")
    if role in ASSURANCE_ROLES and merged in BUILD_ROLES:
        raise ValueError(
            f"collapse table would merge assurance role {role!r} into build role "
            f"{merged!r}, which defeats separation of duties")
    return merged


def can_approve(role: str | None, phase: str) -> bool:
    """May an agent in `role` satisfy `phase`'s gate?

    Only in an approval phase does this restrict anything; elsewhere every
    role is free to do its own work. See SEPARATION_OF_DUTIES."""
    if phase not in APPROVAL_PHASES:
        return True
    return role in ASSURANCE_ROLES


def roles_for_phase(profile: ProjectProfile, phase: str) -> list[tuple[str, str]]:
    """(role, reason) pairs this phase needs, module-filtered and collapsed.

    BUILD is the only phase whose team depends on the project's modules; every
    other phase has one job and one role to do it."""
    if phase not in PHASE_ROLES:
        return []
    wanted: list[tuple[str, str]] = [
        ("pm", "the project manager coordinates every phase and holds the backlog, "
               "dependencies and handoffs across a team that changes shape")]
    for role in PHASE_ROLES[phase]:
        wanted.append((role, f"{phase} needs a {ROLE_CATALOGUE[role]['title'].lower()}"))
    if phase == BUILD:
        for module in profile.modules:
            role = MODULE_ROLE.get(module)
            if role:
                wanted.append((role, f"the project has a {module} module"))

    collapsed: list[tuple[str, str]] = []
    seen: set[str] = set()
    for role, reason in wanted:
        merged = collapse_role(role, profile.complexity)
        if merged in seen:
            # Two canonical roles landed on the same agent at this size. Say
            # so in the reason rather than dropping the second silently.
            for index, (existing, existing_reason) in enumerate(collapsed):
                if existing == merged and role != merged:
                    collapsed[index] = (existing, f"{existing_reason}; also covers {role}")
            continue
        seen.add(merged)
        note = reason if merged == role else f"{reason} (collapsed into {merged} at this size)"
        collapsed.append((merged, note))
    return collapsed


def _spec(profile: ProjectProfile, role: str, reason: str, *, phase: str,
          runtime: str | None) -> AgentSpec:
    entry = ROLE_CATALOGUE[role]
    return AgentSpec(
        agent_id=f"{profile.project_id}-{role}",
        role=role,
        name=f"{profile.name} {entry['title']}",
        description=f"{entry['title']} agent for {profile.name}",
        reason=reason,
        runtime=runtime,
        max_sessions=1,
        base_skills=tuple(entry["skills"]),
        capabilities=tuple(entry["capabilities"]),
        # A cross-phase role is scoped to the whole pipeline, so it is never
        # made dormant by a transition.
        phases=tuple(PHASES) if role in CROSS_PHASE_ROLES else (phase,),
    )


def plan_phase_team(profile: ProjectProfile, phase: str, *, runtime: str | None = None,
                    max_agents: int | None = None) -> tuple[AgentSpec, ...]:
    """The agents that should be ACTIVE during one phase.

    The phase's own role is never cut by the ceiling: a BUILD phase capped to
    two agents still builds, it just builds with fewer specialists."""
    low, high = TEAM_SIZE[profile.complexity]
    ceiling = min(high, max_agents) if max_agents else high
    roles = roles_for_phase(profile, phase)
    # The PM and the phase's own owner are never cut: a team capped to two
    # still needs a coordinator and somebody doing the phase's actual work.
    protected = [item for item in roles if item[0] in CROSS_PHASE_ROLES][:1]
    remainder = [item for item in roles if item[0] not in CROSS_PHASE_ROLES]
    if remainder:
        protected.append(remainder[0])
        remainder = remainder[1:]
    if len(protected) + len(remainder) > ceiling:
        remainder = sorted(remainder, key=lambda item: ROLE_PRIORITY.index(item[0])
                           if item[0] in ROLE_PRIORITY else len(ROLE_PRIORITY))
        remainder = remainder[:max(0, ceiling - len(protected))]
    roles = [*protected, *remainder]
    return tuple(_spec(profile, role, reason, phase=phase, runtime=runtime)
                 for role, reason in roles)


def plan_team(profile: ProjectProfile, *, runtime: str | None = None,
              max_agents: int | None = None,
              phase: str = INTAKE) -> TeamPlan:
    """The whole pipeline: who is active now, and who each later phase will need.

    Bootstrap materialises only `phase`'s team. The rest of the plan is shown
    so an operator can see where the project is going without those agents
    existing yet -- an agent created for a phase three steps away is an idle
    identity accumulating nothing."""
    active = plan_phase_team(profile, phase, runtime=runtime, max_agents=max_agents)
    upcoming: dict[str, list[dict[str, Any]]] = {}
    seen_roles = {spec.role for spec in active}
    for later in PHASES:
        if PHASE_ORDER[later] <= PHASE_ORDER.get(phase, 0):
            continue
        specs = plan_phase_team(profile, later, runtime=runtime, max_agents=max_agents)
        upcoming[later] = [
            {**spec.to_dict(), "already_exists": spec.role in seen_roles} for spec in specs]
        seen_roles.update(spec.role for spec in specs)

    notes = [profile.signals.get("complexity_reason", "")]
    collapsed = {role: target for role, target in ROLE_COLLAPSE.get(profile.complexity, {}).items()}
    if collapsed:
        notes.append(f"{profile.complexity} project: roles collapsed ("
                     + ", ".join(f"{role}->{target}" for role, target in sorted(collapsed.items()))
                     + ")")
    notes.append("build roles never supply REVIEW/TEST/RELEASE approval unless "
                 "policy.allow_self_approval is set")
    return TeamPlan(project_id=profile.project_id, profile=profile, agents=active,
                    phase=phase, upcoming=upcoming,
                    notes=tuple(note for note in notes if note))


def next_phase(phase: str) -> str | None:
    index = PHASE_ORDER.get(phase)
    if index is None or index + 1 >= len(PHASES):
        return None
    return PHASES[index + 1]


def analyze_and_plan(name: str, **kwargs: Any) -> TeamPlan:
    plan_kwargs = {key: kwargs.pop(key) for key in ("runtime", "max_agents", "phase")
                   if key in kwargs}
    return plan_team(analyze(name, **kwargs), **plan_kwargs)
