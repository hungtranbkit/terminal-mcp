"""The real AgentRunner: prompts into actual sessions, answers back as files.

WHY THE ANSWER MAY NOT COME OFF THE PANE

The obvious design is to ask the agent to print its JSON and read it back with
`terminal_tail`. It does not work here, and the reason is specific and
load-bearing: Claude Code's Ink TUI repaints in place and holds tmux's
`history_size` at 0. There is no scrollback. A JSON document longer than the
visible pane is not truncated in some recoverable way -- the beginning of it
never existed as far as tmux is concerned. Add line wrapping at the pane width
and ANSI repaint artefacts and the result is a parser that works on a 40-line
verdict and silently mangles a 120-line one, which is the worst possible
failure mode for a system whose entire argument is that its record can be
trusted.

So the two channels are split by what each is good at:

* The PANE carries the prompt in and one short line out -- the existing
  nonce-bound completion marker, which is a single line by construction and
  survives a repaint.
* A FILE carries the content. The harness names an absolute path, the agent
  writes JSON there, and the harness reads it off disk. Disk does not wrap,
  does not repaint, and does not forget.

The marker is still what says "done", because a file can be half-written; the
marker is the agent's statement that it finished writing. Both are required,
and the nonce binds them to this dispatch rather than to an earlier one
scrolled back into view.

WHY A DISPATCH IS DURABLE AND ASYNCHRONOUS

`run()` does not wait for the agent. It sends, records a `harness_dispatches`
row, and returns `pending`. Resolution happens on a later `step()` through
`poll()`. Nothing sleeps and nothing loops, here or in the engine.

That is not an optimisation, it is the only correct shape. An agent takes
minutes; a blocking call holds a thread across a controller restart it cannot
survive, and on restart the in-memory fact "a builder is running" is gone --
so the recovery is to send the prompt again, to an agent that is still
working. That is the duplicate-dispatch failure `dispatch_idempotency_key`
was introduced to stop on the queue side, and it would have been reintroduced
here. The row makes the in-flight dispatch a fact that outlives the process,
and the idempotency key is derived from (run, iteration, role, attempt) so a
replay reaches core.py's own `idempotent_sends` store and gets the original
result back instead of sending twice.

NOTHING HERE POLLS A MODEL

`poll()` reads `terminal_status` and, when the pane has gone quiet, one
bounded `terminal_tail`. Both are local pane reads -- the same ones
`queue_engine._check_completion` already makes. No model is asked whether the
work is done, ever.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from . import delivery_gate
from . import harness_policy as policy
from . import session_resource
from .harness_contract import iso_now
from .queue_engine import SESSION_UNREACHABLE_ERRORS, worker_output_after_prompt
from .status import parse_completion_marker, verify_completion_marker

#: How long a freshly SPAWNED session is given before the first prompt goes
#: into it. A CLI that has just started is still drawing its welcome screen
#: and is not listening: the prompt lands in a composer that is not ready to
#: submit it, the send is reported confirmed, and the work never starts.
#: Observed live on Claude Code v2.1.278 -- the whole multi-line prompt sat
#: unsubmitted in the input box while the dispatch read as accepted.
SPAWN_SETTLE_SECONDS = 20.0

#: How long a pane may sit quiet with no marker and no artifact before the
#: dispatch is called stalled. Generous: a Builder genuinely thinking, or
#: waiting on a slow test, looks exactly like a stalled one from outside, and
#: the cost of being wrong in the impatient direction is killing working work.
DEFAULT_STALL_SECONDS = 1_800.0

#: Pane states that mean the agent is still working.
BUSY_STATES = frozenset({"RUNNING", "BUSY", "WORKING"})
#: Pane states that mean a human is being asked something. Not stalled, not
#: done -- and NOT something to send another prompt into.
HUMAN_STATES = frozenset({"WAITING_INPUT", "WAITING_APPROVAL", "INPUT_REQUIRED", "PAGER"})

#: Runtimes this adapter knows how to drive. Both are already first-class in
#: adapters.py; the list is here so an unknown runtime is refused loudly
#: rather than dispatched into and silently mishandled.
KNOWN_RUNTIMES: tuple[str, ...] = ("claude", "codex")


def dispatch_idempotency_key(run_id: str, iteration: int, role: str, attempt: int) -> str:
    """Stable across restarts. The same tuple always produces the same key."""
    return f"harness:{run_id}:{iteration}:{role}:{attempt}"


def artifact_relative_path(run_id: str, iteration: int, role: str, attempt: int) -> str:
    return os.path.join(run_id, str(iteration), f"{role}.{attempt}.json")


class SessionOps(Protocol):
    """Exactly the slice queue_engine already defines, plus session creation.

    Deliberately the same shape, so a real ControllerService satisfies it with
    no adapter code -- and so this runner cannot reach for a capability the
    queue's own dispatcher does not already have.
    """

    def terminal_status(self, session: str) -> dict[str, Any]: ...
    def terminal_tail(self, session: str, lines: int | None = None) -> dict[str, Any]: ...
    def terminal_send_text(self, session: str, text: str, press_enter: bool = False,
                           dry_run: bool = False, **kwargs: Any) -> dict[str, Any]: ...
    def terminal_list_sessions(self) -> dict[str, Any]: ...
    def terminal_create_session(self, name: str, agent_type: str = "shell",
                                cwd: str | None = None, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SessionPick:
    """Which runtime this role is going to, and why."""

    session: str
    node_id: str | None = None
    runtime: str | None = None
    reused: bool = False
    context_percent: float | None = None
    reason: str = ""
    spawned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"session": self.session, "node_id": self.node_id,
                "runtime": self.runtime, "reused": self.reused,
                "spawned": self.spawned, "context_percent": self.context_percent,
                "reason": self.reason}


class NoSessionAvailable(RuntimeError):
    """Nothing compatible could be reused and nothing could be spawned.

    An infrastructure failure, never a product one: the work is fine and the
    fleet is full or unreachable. The engine resumes rather than iterating.
    """


class SessionBroker:
    """Chooses, reuses or spawns the runtime a role runs in.

    THE TWO RULES THAT ARE NOT NEGOTIABLE

    1. A CRITICAL Evaluator gets a session that has never held any part of
       this run. The whole value it is being paid for is that it did not watch
       the Builder reason; handing it the Builder's pane buys the saving by
       destroying the thing being bought. `independent=True` excludes the
       run's own sessions AND anything holding an open dispatch for it, and
       will spawn rather than compromise.

    2. A Builder session is reused only while it is genuinely healthy: the
       pane is readable, input is allowed, it is not already holding another
       run's dispatch, and its context is under the ceiling shared with
       harness_policy. A session at 90% is about to be replaced; handing it
       one more task means doing the work twice.
    """

    def __init__(self, ops: SessionOps, store: Any, *, router: Any = None,
                 default_runtime: str = "claude",
                 reuse_ceiling: float = policy.SESSION_REUSE_CONTEXT_CEILING,
                 session_prefix: str = "harness",
                 spawn_node: str = "auto") -> None:
        self.ops = ops
        self.store = store
        self.router = router
        self.default_runtime = default_runtime
        self.reuse_ceiling = reuse_ceiling
        self.session_prefix = session_prefix
        self.spawn_node = spawn_node

    # -- health --------------------------------------------------------------
    def context_percent(self, session: str) -> float | None:
        """Occupancy from the pane's own resource footer, or None.

        None means "nobody measured", which `harness_policy.context_action`
        reads as CONTINUE -- acting on a number nobody measured is worse than
        acting on none.
        """
        try:
            capture = self.ops.terminal_tail(session, 60)
        except Exception:  # noqa: BLE001 -- a health read must never raise into a dispatch
            return None
        if capture.get("error"):
            return None
        return session_resource.parse_session_resources(
            str(capture.get("output") or "")).context_percent

    def health(self, session: str) -> dict[str, Any]:
        try:
            status = self.ops.terminal_status(session)
        except Exception as exc:  # noqa: BLE001
            return {"alive": False, "reason": f"status raised: {exc}"}
        error = status.get("error")
        if error:
            return {"alive": False, "reason": str(error),
                    "unreachable": error in SESSION_UNREACHABLE_ERRORS}
        # A SESSION THAT IS GONE IS NOT AN ERROR, AND SAYS SO EXPLICITLY.
        #
        # `terminal_status` answers a killed session with error=None,
        # state="UNKNOWN" and exists=False -- being asked about a session
        # that does not exist is a legitimate question with a definite
        # answer, not a failure. Checking only `error` therefore read a
        # killed session as ALIVE, and a Builder whose tmux session was
        # destroyed sat in `awaiting_session` until the stall timeout
        # instead of failing over immediately. Found by killing a real
        # session and watching the engine not notice.
        #
        # `exists` is checked rather than `state == "UNKNOWN"` because
        # UNKNOWN is also what a live pane reports when its activity cannot
        # be classified (see status.py) -- treating that as death would
        # abandon sessions that are working.
        if status.get("exists") is False:
            return {"alive": False, "unreachable": True,
                    "reason": str(status.get("reason") or "session does not exist")}
        state = str(status.get("state") or "").upper()
        return {"alive": True, "state": state,
                "busy": state in BUSY_STATES,
                "awaiting_human": state in HUMAN_STATES or bool(status.get("input_required")),
                "node_id": status.get("node_id")}

    def owns(self, session: str | None) -> bool:
        """Did the harness create this session?

        THE HARNESS ADOPTS NOTHING IT DID NOT CREATE.

        An agent TUI reports `RUNNING` whether it is generating a reply or
        sitting at an empty prompt -- confirmed live on Claude Code v2.1.278,
        where a freshly booted, idle session read as RUNNING. So pane state
        cannot tell "busy" from "ready", which means it also cannot tell
        "nobody is using this" from "somebody is typing in it right now".

        Guessing wrong in that direction means dropping a machine-generated
        prompt into a human's attended session. So the rule is ownership, not
        inference: a session whose name this broker minted is fair game, and
        every other session on the fleet is somebody else's. It costs a spawn
        occasionally and it cannot interrupt anyone.
        """
        return bool(session) and str(session).startswith(f"{self.session_prefix}-")

    def healthy_for_reuse(self, session: str) -> tuple[bool, str, float | None]:
        """Reusable means: ours, readable, not asking a human, and has room.

        Deliberately NOT "not busy". See `owns` -- pane state cannot express
        it for an agent TUI. Exclusivity comes from `sessions_in_use`, which
        is the harness's own durable record of what it has dispatched into
        and is authoritative rather than inferred.
        """
        if not self.owns(session):
            return False, "not a harness-owned session", None
        health = self.health(session)
        if not health.get("alive"):
            return False, f"session unreadable: {health.get('reason')}", None
        if health.get("awaiting_human"):
            return False, "session is waiting on a human", None
        percent = self.context_percent(session)
        if percent is not None and percent >= self.reuse_ceiling:
            return False, f"context {percent}% is at or above the {self.reuse_ceiling}% ceiling", percent
        return True, "healthy and has room", percent

    # -- selection -----------------------------------------------------------
    def _candidates(self) -> list[dict[str, Any]]:
        """Existing sessions, from sources that already exist.

        The router's own candidate list is preferred because it is already
        assembled and cached from the fleet listing, the session registry and
        the queue -- re-probing here would be a second, differently-cached
        opinion about the same fleet.
        """
        if self.router is not None:
            try:
                return [{"session": c.session, "node_id": c.node_id,
                         "runtime": c.runtime, "state": c.state,
                         "input_allowed": c.input_allowed,
                         "node_online": c.node_online,
                         "worktree_path": c.worktree_path}
                        for c in self.router.candidates()]
            except Exception:  # noqa: BLE001 -- fall through to the plain listing
                pass
        try:
            listing = self.ops.terminal_list_sessions()
        except Exception:  # noqa: BLE001
            return []
        rows = listing.get("sessions") or []
        out = []
        for row in rows:
            if isinstance(row, str):
                out.append({"session": row})
            elif isinstance(row, Mapping):
                out.append({"session": row.get("name") or row.get("session"),
                            "node_id": row.get("node_id"),
                            "runtime": row.get("agent_type") or row.get("runtime"),
                            "state": row.get("state"),
                            "input_allowed": row.get("input_allowed", True),
                            "node_online": row.get("node_online", True)})
        return [row for row in out if row.get("session")]

    @staticmethod
    def _runtime_matches(candidate_runtime: Any, wanted: str) -> bool:
        if not candidate_runtime:
            return False
        return str(candidate_runtime).lower().startswith(wanted.lower())

    def pick(self, *, run: Any, role: str, runtime: str | None = None,
             independent: bool = False, worktree_path: str | None = None,
             allow_spawn: bool = True,
             exclude: Sequence[str] = ()) -> SessionPick:
        wanted = (runtime or self.default_runtime).lower()
        if wanted not in KNOWN_RUNTIMES:
            raise NoSessionAvailable(
                f"unknown runtime {wanted!r}; this adapter drives {', '.join(KNOWN_RUNTIMES)}")

        blocked = set(exclude)
        blocked |= self.store.sessions_in_use(exclude_run=run.id)
        if independent:
            # Every session this run has already used, whatever the role.
            blocked |= {d["session_id"] for d in self.store.dispatches(run.id)
                        if d.get("session_id")}
            for name in (run.builder_session_id, run.evaluator_session_id):
                if name:
                    blocked.add(name)

        if not independent and role == policy.BUILDER and run.builder_session_id \
                and run.builder_session_id not in blocked:
            ok, why, percent = self.healthy_for_reuse(run.builder_session_id)
            if ok:
                return SessionPick(session=run.builder_session_id,
                                   node_id=run.node_id, runtime=wanted, reused=True,
                                   context_percent=percent,
                                   reason=f"builder session reused: {why}")

        for candidate in self._candidates():
            name = candidate["session"]
            if name in blocked or not self.owns(name):
                continue
            if candidate.get("node_online") is False:
                continue
            if candidate.get("input_allowed") is False:
                continue
            if not self._runtime_matches(candidate.get("runtime"), wanted):
                continue
            ok, why, percent = self.healthy_for_reuse(name)
            if not ok:
                continue
            return SessionPick(session=name, node_id=candidate.get("node_id"),
                               runtime=wanted, reused=not independent,
                               context_percent=percent,
                               reason=f"compatible idle {wanted} session: {why}")

        if not allow_spawn:
            raise NoSessionAvailable(
                f"no compatible {wanted} session for {role} and spawning is disabled")
        return self.spawn(run=run, role=role, runtime=wanted, worktree_path=worktree_path)

    def _spawn_kwargs(self) -> dict[str, Any]:
        """Only the arguments THIS ops object actually accepts.

        Both a multi-node ControllerService and a single-host TerminalService
        are valid SessionOps, and they differ: the controller places a session
        on a node, the local service has only the one host and no `node`
        parameter at all. Passing it regardless raises TypeError, and catching
        TypeError around a call that does real work would swallow a genuine
        bug in the callee. Asking the signature is exact.
        """
        try:
            accepted = inspect.signature(self.ops.terminal_create_session).parameters
        except (TypeError, ValueError):
            return {}
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in accepted.values()):
            return {"node": self.spawn_node, "requested_by": "harness"}
        return {name: value for name, value in
                (("node", self.spawn_node), ("requested_by", "harness"))
                if name in accepted}

    def spawn(self, *, run: Any, role: str, runtime: str,
              worktree_path: str | None = None) -> SessionPick:
        name = f"{self.session_prefix}-{role}-{run.task_id}-{secrets.token_hex(3)}".lower()
        response = self.ops.terminal_create_session(
            name, agent_type=runtime, cwd=worktree_path or run.worktree_path,
            **self._spawn_kwargs())
        if response.get("error"):
            raise NoSessionAvailable(
                f"could not spawn a {runtime} session for {role}: {response['error']}")
        return SessionPick(session=response.get("session") or name,
                           node_id=response.get("node_id"), runtime=runtime,
                           reused=False, spawned=True,
                           reason=f"spawned a fresh {runtime} session for {role}")


# ---------------------------------------------------------------------------
# the prompt wrapper
# ---------------------------------------------------------------------------

#: The one sentence that makes the marker findable and attributable. Reused
#: verbatim from the queue's own protocol so `_is_our_own_template` can still
#: tell our dispatched template apart from the agent's reply.
from .queue_engine import COMPLETION_INSTRUCTION_SENTENCE  # noqa: E402


def build_agent_text(*, prompt_text: str, role: str, dispatch_id: str, attempt: int,
                     nonce: str, artifact_path: str, run_id: str,
                     iteration: int) -> str:
    """The role's prompt, verbatim, plus the two-channel contract.

    The prompt itself is included UNMODIFIED and first: nothing here rewrites
    or reinterprets what the engine decided to ask. What is appended is only
    where to put the answer and how to say it is finished -- and it is
    appended rather than prepended because standing mechanics should not be
    the first thing a model reads when there is a task to do.
    """
    return (
        f"{prompt_text}\n\n"
        f"---\n"
        f"## How to return your answer (Terminal MCP Harness)\n"
        f"Write your answer as a single JSON object to this exact absolute path:\n"
        f"  {artifact_path}\n"
        f"Create the parent directory if it does not exist. Write the FILE, not the\n"
        f"terminal -- this pane has no scrollback and long output is lost, so anything\n"
        f"printed here cannot be read back.\n"
        f'Include this exact key in the JSON object: "harness_nonce": "{nonce}"\n'
        f"Write the file completely, then add that key last, then print the line below.\n"
        f"Run: {run_id} iteration {iteration} role {role}.\n\n"
        f"{COMPLETION_INSTRUCTION_SENTENCE}\n"
        f"###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 "
        f"task_id={dispatch_id} attempt={attempt} nonce={nonce} "
        f"status=completion_candidate "
        f"summary_sha256={hashlib.sha256(dispatch_id.encode()).hexdigest()[:16]}###\n"
    )


# ---------------------------------------------------------------------------
# the runner
# ---------------------------------------------------------------------------

def _age_seconds(stamp: str | None) -> float:
    if not stamp:
        return 0.0
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds())


#: Reused rather than reimplemented. `terminal_status` speaks pane states
#: ("RUNNING"), delivery_gate speaks adapter target states ("running"), and a
#: local helper that conflated the two silently produced "not accepted" for
#: every send -- which is exactly the sort of quiet, plausible wrongness the
#: acceptance gate exists to prevent, arriving via the gate itself.
from .queue_engine import _target_state_from_status  # noqa: E402


class TerminalAgentRunner:
    """A real AgentRunner over real sessions. No mock, no second queue.

    It owns no state of its own. Everything it needs to survive a restart is
    in `harness_dispatches`, everything about the run is in `harness_runs`,
    and the sessions belong to the controller. Losing this object loses
    nothing: a new one reads the open dispatch row and carries on observing
    the agent that is still working.
    """

    def __init__(self, ops: SessionOps, store: Any, *, artifacts_root: str | Path,
                 broker: SessionBroker | None = None, router: Any = None,
                 runtime_for_role: Mapping[str, str] | None = None,
                 default_runtime: str = "claude",
                 stall_seconds: float = DEFAULT_STALL_SECONDS,
                 allow_spawn: bool = True,
                 spawn_settle_seconds: float = SPAWN_SETTLE_SECONDS,
                 #: ON by default, unlike the queue's advisory setting. The
                 #: queue can afford advisory because a task that silently
                 #: fails to start is noticed by an operator watching a board.
                 #: A harness dispatch has no such observer: it would sit in
                 #: BUILDING until the stall timeout, having spent a session
                 #: and told nobody. The gate already knows how to say
                 #: "the prompt is still in the composer" -- it just has to
                 #: be asked.
                 require_acceptance: bool = True,
                 #: Registers workspace trust for a worktree the Harness
                 #: itself created, before a session is opened in it. None
                 #: keeps the previous behaviour exactly: the spawn happens,
                 #: Claude Code asks its question, and `_send_when_ready`
                 #: names it PERMISSION_REQUIRED. See harness_trust for why
                 #: this cannot be pointed at a directory a person owns.
                 trust: Any = None) -> None:
        self.ops = ops
        self.store = store
        self.trust = trust
        self.artifacts_root = Path(artifacts_root)
        self.broker = broker or SessionBroker(ops, store, router=router,
                                              default_runtime=default_runtime)
        self.runtime_for_role = dict(runtime_for_role or {})
        self.default_runtime = default_runtime
        self.stall_seconds = stall_seconds
        self.allow_spawn = allow_spawn
        self.spawn_settle_seconds = spawn_settle_seconds
        self.require_acceptance = require_acceptance

    # -- the AgentRunner protocol -------------------------------------------
    def run(self, *, role: str, prompt: Any, run: Any, tier: str,
            session_id: str | None, iteration: int) -> Any:
        """Dispatch, or resolve what is already dispatched. Never blocks.

        Called again for a stage that already has work in flight, this does
        NOT send a second prompt -- it polls the open dispatch. That is the
        whole reason the dispatch is a durable row: whoever is driving ticks
        may step the same stage any number of times, and only the first one
        may send.
        """
        from .harness_engine import AgentResult

        existing = self.store.latest_dispatch(run.id, iteration=iteration, role=role)
        if existing is not None:
            if existing["state"] == "awaiting_session":
                return self._send_when_ready(existing, prompt, run, role, iteration)
            if existing["state"] in self.store.OPEN_DISPATCH_STATES:
                return self.poll(existing)
            if existing["state"] == "completed":
                return self._result_from_artifact(existing)
        attempt = int(existing["attempt"]) + 1 if existing else 1

        # A CRITICAL Evaluator is the only role the engine asks for while
        # demanding independence -- see harness_policy.INDEPENDENT_EVALUATOR.
        independent = role == policy.EVALUATOR
        runtime = self.runtime_for_role.get(role, self.default_runtime)
        # BEFORE the session opens the directory, not after it has asked.
        # Trust is keyed by exact path and read when Claude Code starts, so
        # a grant that lands afterwards helps nobody -- the session is
        # already sitting on the question. A refusal is deliberately NOT an
        # error here: the run proceeds, the session asks, and the existing
        # PERMISSION_REQUIRED path reports it. Refusing to dispatch on a
        # refused grant would turn a recoverable human question into a hard
        # stop for every worktree this module does not recognise.
        self._register_trust(run)
        try:
            pick = self.broker.pick(run=run, role=role, runtime=runtime,
                                    independent=independent,
                                    worktree_path=run.worktree_path,
                                    allow_spawn=self.allow_spawn)
        except NoSessionAvailable as exc:
            # The fleet, not the work. Resume, do not iterate.
            return AgentResult(infra_failure=True, error=str(exc), agent=f"harness:{role}")

        nonce = secrets.token_hex(12)
        key = dispatch_idempotency_key(run.id, iteration, role, attempt)
        artifact = self.artifacts_root / artifact_relative_path(
            run.id, iteration, role, attempt)
        dispatch, created = self.store.open_dispatch(
            run_id=run.id, task_id=run.task_id, iteration=iteration, role=role,
            attempt=attempt, idempotency_key=key, nonce=nonce,
            session_id=pick.session, node_id=pick.node_id, runtime=pick.runtime,
            worktree_path=run.worktree_path, branch=run.branch,
            correlation_id=key, artifact_path=str(artifact),
            session_reused=pick.reused,
            prompt_bytes=getattr(prompt, "bytes", 0),
            prompt_tokens_estimate=getattr(prompt, "tokens_estimate", 0))
        if not created:
            # Another stepper opened it between our read and our write.
            return self.poll(dispatch)

        try:
            artifact.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.store.update_dispatch(dispatch["id"], state="failed",
                                       error=f"artifact directory: {exc}")
            return AgentResult(infra_failure=True, error=str(exc))

        if pick.spawned:
            # A CLI that has just been started is not listening yet. The
            # dispatch is recorded with its session bound and NOTHING sent;
            # the next step sends, by which time the pane has settled. This
            # costs one tick and removes an entire class of silent failure.
            self.store.update_dispatch(dispatch["id"], state="awaiting_session")
            return AgentResult(pending=True, dispatch_id=dispatch["id"],
                               session_id=pick.session, session_reused=False,
                               agent=f"{pick.runtime}:{pick.session}")

        text = build_agent_text(
            prompt_text=getattr(prompt, "text", str(prompt)), role=role,
            dispatch_id=dispatch["id"], attempt=attempt, nonce=dispatch["nonce"],
            artifact_path=str(artifact), run_id=run.id, iteration=iteration)
        return self._send(dispatch, pick, text)

    def _register_trust(self, run: Any) -> Any:
        """Record workspace trust for this run's worktree, if it has one.

        Idempotent and cheap on the common path: a worktree already trusted
        costs one config read and writes nothing. Never raises -- a trust
        service that is down must not take the run with it, because the
        outcome without it is the behaviour that already existed.
        """
        if self.trust is None or not getattr(run, "worktree_path", None):
            return None
        try:
            return self.trust.register(run.worktree_path, run_id=run.id,
                                       source="harness-runner")
        except Exception:  # noqa: BLE001 -- reported by the trust audit, never fatal
            return None

    def _send_when_ready(self, dispatch: Mapping[str, Any], prompt: Any, run: Any,
                         role: str, iteration: int) -> Any:
        """A spawned session, revisited. Send once it has settled."""
        from .harness_engine import AgentResult

        session = dispatch["session_id"]
        health = self.broker.health(session)
        if not health.get("alive"):
            self.store.update_dispatch(dispatch["id"], state="failed",
                                       error=f"spawned session never came up: "
                                             f"{health.get('reason')}")
            return AgentResult(infra_failure=True, session_id=session,
                               error=f"spawned session never came up: {health.get('reason')}")
        age = _age_seconds(dispatch.get("created_at"))
        if health.get("awaiting_human") and age >= self.spawn_settle_seconds:
            # A SETUP BLOCKER, NOT A STALL.
            #
            # A spawned session asking a human something before it has been
            # given any work is not slow -- it is waiting for a person, and no
            # amount of further polling will change that. Observed live:
            # Claude Code asks "Is this a project you trust?" the first time
            # it opens a directory, and a fresh git worktree is always a new
            # directory. Waiting it out burns the stall timeout and reports
            # the wrong cause; retrying spawns another session that asks the
            # same question.
            #
            # So it is named for what it is. The engine turns this into a
            # PERMISSION_REQUIRED decision, which is on the closed human list
            # precisely because deterministic code cannot answer it.
            reason = (f"the spawned session is waiting on a human before any work "
                      f"was sent (most likely a workspace-trust prompt for "
                      f"{dispatch.get('worktree_path') or 'its working directory'})")
            self.store.update_dispatch(dispatch["id"], state="failed", error=reason,
                                       last_observed_at=iso_now())
            return AgentResult(infra_failure=True, session_id=session, error=reason,
                               needs_permission=True)
        if age < self.spawn_settle_seconds or health.get("awaiting_human"):
            self.store.update_dispatch(dispatch["id"], last_observed_at=iso_now())
            return AgentResult(pending=True, dispatch_id=dispatch["id"],
                               session_id=session)
        text = build_agent_text(
            prompt_text=getattr(prompt, "text", str(prompt)), role=role,
            dispatch_id=dispatch["id"], attempt=int(dispatch["attempt"]),
            nonce=dispatch["nonce"], artifact_path=str(dispatch["artifact_path"]),
            run_id=run.id, iteration=iteration)
        pick = SessionPick(session=session, node_id=dispatch.get("node_id"),
                           runtime=dispatch.get("runtime"), reused=False,
                           reason="spawned session has settled")
        return self._send(dispatch, pick, text)

    def _send(self, dispatch: Mapping[str, Any], pick: SessionPick, text: str) -> Any:
        from .harness_engine import AgentResult

        try:
            response = self.ops.terminal_send_text(
                pick.session, text, press_enter=True,
                idempotency_key=dispatch["idempotency_key"])
        except Exception as exc:  # noqa: BLE001 -- a transport raise is infrastructure
            self.store.update_dispatch(dispatch["id"], state="failed", error=str(exc))
            return AgentResult(infra_failure=True, error=f"send raised: {exc}",
                               session_id=pick.session)

        verdict = self._delivery_verdict(pick.session, response, text)
        self.store.update_dispatch(
            dispatch["id"], delivery_state=str(response.get("delivery_state") or ""),
            delivery_verdict=verdict.to_dict() if hasattr(verdict, "to_dict") else str(verdict),
            context_percent=pick.context_percent, last_observed_at=iso_now())

        if response.get("error"):
            self.store.update_dispatch(dispatch["id"], state="failed",
                                       error=str(response["error"]))
            return AgentResult(infra_failure=True, error=str(response["error"]),
                               session_id=pick.session)
        if verdict.kind == delivery_gate.REFUSED:
            # The pane refused the text. Nothing is running, so this is an
            # infrastructure failure and the same attempt may be retried --
            # the idempotency key is unchanged, so a send that did somehow
            # land will return its original result rather than duplicate.
            self.store.update_dispatch(
                dispatch["id"], state="failed",
                error=f"delivery refused: {verdict.activation}")
            return AgentResult(infra_failure=True, session_id=pick.session,
                               error=f"delivery refused: {verdict.activation}")
        if verdict.kind == delivery_gate.UNCERTAIN or \
                response.get("delivery_state") == "DELIVERY_UNKNOWN":
            # Genuinely unknown. Held open and resolved by observation on a
            # later step -- never resent blindly, which is the queue's own
            # DISPATCH_UNCERTAIN posture and for the same reason.
            self.store.update_dispatch(dispatch["id"], state="dispatching",
                                       last_observed_at=iso_now())
            return AgentResult(pending=True, dispatch_id=dispatch["id"],
                               session_id=pick.session, session_reused=pick.reused,
                               context_percent=pick.context_percent,
                               newly_dispatched=True,
                               agent=f"{pick.runtime}:{pick.session}")
        if verdict.kind == delivery_gate.NOT_ACCEPTED:
            # The submit is CONFIRMED and the target did not take it -- most
            # often the prompt sitting unsubmitted in a composer. Held open,
            # never resent: resending a confirmed submit duplicates it. The
            # stall timeout eventually escalates this as infrastructure,
            # which is what it is.
            self.store.update_dispatch(dispatch["id"], state="dispatching",
                                       error=f"not accepted: {verdict.acceptance}",
                                       last_observed_at=iso_now())
            return AgentResult(pending=True, dispatch_id=dispatch["id"],
                               session_id=pick.session, session_reused=pick.reused,
                               newly_dispatched=True,
                               agent=f"{pick.runtime}:{pick.session}")
        self.store.update_dispatch(dispatch["id"], state="accepted",
                                   accepted_at=iso_now(), last_observed_at=iso_now())
        return AgentResult(pending=True, dispatch_id=dispatch["id"],
                           session_id=pick.session, session_reused=pick.reused,
                           context_percent=pick.context_percent,
                           newly_dispatched=True,
                           agent=f"{pick.runtime}:{pick.session}")

    def _delivery_verdict(self, session: str, response: Mapping[str, Any], sent_text: str):
        """One extra read, never a loop. Same shape as queue_engine's."""
        after_lines = None
        target_state = None
        if self.require_acceptance:
            try:
                status = self.ops.terminal_status(session)
                if not status.get("error"):
                    target_state = _target_state_from_status(status)
                    tail = status.get("last_output")
                    if isinstance(tail, str):
                        after_lines = tail.splitlines()
            except Exception:  # noqa: BLE001 -- unobservable must not crash a dispatch
                after_lines = None
        return delivery_gate.evaluate(
            dict(response), before_lines=response.get("pre_submit_lines"),
            after_lines=after_lines, target_state=target_state, sent_text=sent_text,
            require_acceptance=self.require_acceptance)

    # -- observation ---------------------------------------------------------
    def poll(self, dispatch: Mapping[str, Any]) -> Any:
        """Is it done? Answered from pane state and one bounded tail.

        No model is consulted. The order matters: a dead session is checked
        before a quiet one, because an unreachable pane and a finished pane
        both look like "not RUNNING" and only one of them means the work is
        still recoverable where it stands.
        """
        from .harness_engine import AgentResult

        session = dispatch.get("session_id")
        dispatch_id = dispatch["id"]
        if not session:
            self.store.update_dispatch(dispatch_id, state="failed",
                                       error="dispatch has no session")
            return AgentResult(infra_failure=True, error="dispatch has no session")

        health = self.broker.health(session)
        if not health.get("alive"):
            reason = str(health.get("reason"))
            self.store.update_dispatch(dispatch_id, state="failed", error=reason,
                                       last_observed_at=iso_now())
            return AgentResult(infra_failure=True, error=f"session gone: {reason}",
                               session_id=session)

        self.store.update_dispatch(dispatch_id, last_observed_at=iso_now())
        if health.get("awaiting_human"):
            # Not stalled and not done. Reported as infrastructure so the run
            # checkpoints and resumes rather than burning a product iteration
            # on an agent that is sitting at a prompt.
            return AgentResult(infra_failure=True, session_id=session,
                               error="the session is waiting on a human")

        finished = self._read_completion(dispatch, session)
        if finished is not None:
            return finished
        if health.get("busy"):
            return AgentResult(pending=True, dispatch_id=dispatch_id,
                               session_id=session)

        age = _age_seconds(dispatch.get("last_observed_at") or dispatch.get("created_at"))
        if age >= self.stall_seconds:
            self.store.update_dispatch(
                dispatch_id, state="stalled",
                error=f"quiet for {int(age)}s with no completion marker")
            return AgentResult(infra_failure=True, session_id=session,
                               error=f"builder stalled: quiet for {int(age)}s")
        return AgentResult(pending=True, dispatch_id=dispatch_id, session_id=session)

    def _read_completion(self, dispatch: Mapping[str, Any], session: str) -> Any:
        """Is the answer really there, and is it really for THIS dispatch?

        TWO SIGNALS, AND THE FILE IS THE STRONGER ONE

        The obvious completion signal is the pane marker, and on a normal CLI
        it is enough. Claude Code is not a normal CLI: it repaints in place
        with no scrollback, so a marker printed and then repainted over is
        simply not there the next time the pane is read. Waiting for a signal
        that may have already been erased is how a finished run sits in
        BUILDING forever.

        So the nonce is ALSO written into the artifact, and a file carrying
        this dispatch's nonce is accepted on its own. That is not a weaker
        check -- it is a stronger one. The nonce is unguessable and unique to
        this attempt, so a file containing it cannot be a leftover from an
        earlier attempt, cannot be our own prompt echoed back, and cannot be
        anything but a deliberate answer to exactly this request. The pane
        marker stays as the fallback for agents that write the file without
        the key.
        """
        from .harness_engine import AgentResult

        payload, artifact_error = self._load_artifact(dispatch.get("artifact_path"))
        if artifact_error is None and isinstance(payload, dict) \
                and str(payload.get("harness_nonce") or "") == dispatch["nonce"]:
            return self._complete(dispatch, session, payload)

        try:
            capture = self.ops.terminal_tail(session, 200)
        except Exception:  # noqa: BLE001
            return None
        if capture.get("error"):
            return None
        # Only what the WORKER wrote: our own dispatched template contains a
        # fully valid marker, and reading that back is how a session that did
        # nothing gets recorded as finished.
        output = worker_output_after_prompt(str(capture.get("output") or ""))
        marker = parse_completion_marker(output)
        if not verify_completion_marker(marker, task_id=dispatch["id"],
                                        attempt=int(dispatch["attempt"]),
                                        nonce=dispatch["nonce"], nonce_consumed=False):
            return None

        error = artifact_error
        if error is not None:
            # It said it was done and there is nothing to read. That is an
            # infrastructure failure of the exchange, not a wrong answer about
            # the code -- so it resumes rather than spending an iteration.
            self.store.update_dispatch(dispatch["id"], state="failed", error=error)
            return AgentResult(infra_failure=True, session_id=session, error=error)
        return self._complete(dispatch, session, payload)

    def _complete(self, dispatch: Mapping[str, Any], session: str, payload: Any) -> Any:
        from .harness_engine import AgentResult

        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        self.store.update_dispatch(
            dispatch["id"], state="completed", completed_at=iso_now(),
            artifact_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16])
        return AgentResult(
            payload=payload, session_id=session, agent=str(dispatch.get("runtime") or ""),
            commit=payload.get("commit") if isinstance(payload, dict) else None,
            context_percent=self.broker.context_percent(session),
            session_reused=bool(dispatch.get("session_reused")),
            dispatch_id=dispatch["id"],
            artifact_path=dispatch.get("artifact_path"))

    def _result_from_artifact(self, dispatch: Mapping[str, Any]) -> Any:
        from .harness_engine import AgentResult

        payload, error = self._load_artifact(dispatch.get("artifact_path"))
        if error is not None:
            return AgentResult(infra_failure=True, error=error,
                               session_id=dispatch.get("session_id"))
        return AgentResult(payload=payload, session_id=dispatch.get("session_id"),
                           agent=str(dispatch.get("runtime") or ""),
                           dispatch_id=dispatch["id"],
                           session_reused=bool(dispatch.get("session_reused")),
                           artifact_path=dispatch.get("artifact_path"))

    @staticmethod
    def _load_artifact(path: str | None) -> tuple[Any, str | None]:
        """(payload, error). A missing or unparseable file is a named gap."""
        if not path:
            return None, "dispatch declared no artifact path"
        target = Path(path)
        if not target.is_file():
            return None, f"agent reported done but wrote no artifact at {path}"
        try:
            raw = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return None, f"artifact unreadable: {exc}"
        text = raw.strip()
        if not text:
            return None, f"artifact at {path} is empty"
        try:
            return json.loads(text), None
        except ValueError:
            # A model that wrapped its JSON in a fence is common enough to be
            # worth recovering from -- and recovering is strictly better than
            # failing a whole iteration over three backticks.
            fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
            if fenced:
                try:
                    return json.loads(fenced.group(1)), None
                except ValueError:
                    pass
            return None, f"artifact at {path} is not valid JSON"
