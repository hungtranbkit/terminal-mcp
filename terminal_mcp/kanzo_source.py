"""Read-only Terminal MCP source adapter for the disposable Kanzo view.

The adapter consumes existing read tools.  It never opens the queue database,
constructs a queue service, or calls a mutation-capable Terminal MCP tool.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .project_identity import normalise_git_remote

_MAX_SOURCE_BYTES = 2_000_000
_MAX_PROJECTS = 20
_MAX_TASKS_PER_SESSION = 1_000


class SourceError(ValueError):
    """Live source data is missing, ambiguous, or violates the allowlist."""


class ReadSource(Protocol):
    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class LiveProject:
    id: str
    name: str
    remote: str
    node_id: str
    session_name: str


def load_live_projects(path: Path | str) -> list[LiveProject]:
    """Load the narrow allowlist that binds projects to real sessions."""
    try:
        raw = Path(path).read_bytes()
        if len(raw) > _MAX_SOURCE_BYTES:
            raise SourceError("live source config is too large")
        payload = json.loads(raw)
    except SourceError:
        raise
    except (OSError, ValueError) as exc:
        raise SourceError("live source config is not valid JSON") from exc
    rows = payload.get("projects") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise SourceError("live source config requires a non-empty projects list")
    if len(rows) > _MAX_PROJECTS:
        raise SourceError("live source config contains too many projects")
    required = {"id", "name", "remote", "node_id", "session_name"}
    projects: list[LiveProject] = []
    identities: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != required:
            raise SourceError(
                "each live project must contain exactly id, name, remote, node_id, session_name"
            )
        if not all(isinstance(row[key], str) and row[key].strip() for key in required):
            raise SourceError("live project identifiers must be non-empty strings")
        project = LiveProject(**row)
        identity = (project.node_id, project.session_name)
        if identity in identities:
            raise SourceError("duplicate live session identity")
        identities.add(identity)
        projects.append(project)
    return projects


_TASK_GROUPS = (
    "queued", "running", "blocked_rework", "waiting_dependency", "recent"
)
_READ_TOOLS = frozenset({
    "terminal_registry_search", "terminal_batch_inspect", "terminal_session_tasks"
})


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpMcpReadSource:
    """Small Streamable HTTP client restricted to loopback read tools."""

    def __init__(self, url: str, *, timeout: float = 10.0) -> None:
        parsed = urlsplit(url)
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
        except ValueError as exc:
            raise SourceError("Terminal MCP source must use a literal loopback address") from exc
        if (
            parsed.scheme != "http"
            or not address.is_loopback
            or parsed.path != "/mcp"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise SourceError("Terminal MCP source must be a plain loopback /mcp URL")
        self.url = url
        self.timeout = timeout
        self._opener = build_opener(_RejectRedirects)
        self._request_id = 0
        self._session_id: str | None = None
        response, headers = self._post("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "tmcp-kanzo-readonly", "version": "1"},
        }, include_id=True)
        if "result" not in response:
            raise SourceError("Terminal MCP initialize failed")
        self._session_id = headers.get("Mcp-Session-Id")
        if not self._session_id:
            raise SourceError("Terminal MCP did not return a session id")
        self._post("notifications/initialized", {}, include_id=False)

    def _post(
        self, method: str, params: dict[str, Any], *, include_id: bool
    ) -> tuple[dict[str, Any], Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        if include_id:
            self._request_id += 1
            payload["id"] = self._request_id
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        request = Request(
            self.url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.geturl() != self.url:
                    raise SourceError("Terminal MCP source redirected away from loopback endpoint")
                raw = response.read(_MAX_SOURCE_BYTES + 1)
                if len(raw) > _MAX_SOURCE_BYTES:
                    raise SourceError("Terminal MCP response is too large")
                decoded = json.loads(raw) if raw else {}
                return decoded, response.headers
        except (HTTPError, URLError, OSError, ValueError) as exc:
            raise SourceError(f"Terminal MCP read failed: {type(exc).__name__}") from exc

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool not in _READ_TOOLS:
            raise SourceError(f"Terminal MCP tool is not an approved read tool: {tool}")
        response, _ = self._post("tools/call", {
            "name": tool, "arguments": arguments,
        }, include_id=True)
        if response.get("error") or response.get("result", {}).get("isError"):
            raise SourceError(f"Terminal MCP read tool failed: {tool}")
        content = response.get("result", {}).get("content", [])
        if not content or not isinstance(content[0], dict):
            raise SourceError(f"Terminal MCP read tool returned no content: {tool}")
        try:
            value = json.loads(content[0].get("text", ""))
        except (TypeError, ValueError) as exc:
            raise SourceError(f"Terminal MCP read tool returned invalid JSON: {tool}") from exc
        if not isinstance(value, dict):
            raise SourceError(f"Terminal MCP read tool returned invalid payload: {tool}")
        return value


def _matching_record(payload: dict[str, Any], project: LiveProject) -> dict[str, Any]:
    records = [
        row for row in payload.get("records", [])
        if isinstance(row, dict)
        and row.get("node_id") == project.node_id
        and row.get("session_name") == project.session_name
    ]
    if len(records) != 1:
        raise SourceError(
            f"source identity mismatch for {project.node_id}/{project.session_name}"
        )
    record = records[0]
    if not record.get("stable_session_id"):
        raise SourceError(f"source session has no stable identity: {project.session_name}")
    if normalise_git_remote(record.get("git_remote")) != normalise_git_remote(
        project.remote
    ):
        raise SourceError(
            f"source identity mismatch for {project.node_id}/{project.session_name}"
        )
    return record


def _task_cards(
    payload: dict[str, Any], record: dict[str, Any], state: str
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in _TASK_GROUPS:
        rows = payload.get(group) or []
        if not isinstance(rows, list):
            raise SourceError(f"terminal_session_tasks.{group} must be a list")
        if len(rows) > _MAX_TASKS_PER_SESSION:
            raise SourceError("terminal_session_tasks returned too many tasks")
        for task in rows:
            if not isinstance(task, dict) or not task.get("id"):
                raise SourceError("terminal_session_tasks returned a task without an id")
            if task.get("node_id") != record["node_id"]:
                raise SourceError("canonical task has no matching node identity")
            task_id = str(task["id"])
            if task_id in seen:
                continue
            seen.add(task_id)
            evidence = str(task.get("verification_evidence") or "")
            linked: dict[str, list[dict[str, Any]]] = {}
            for kind in ("issues", "prs"):
                values = task.get(kind) or []
                if not isinstance(values, list) or not all(
                    isinstance(value, dict) for value in values
                ):
                    raise SourceError(f"canonical task {kind} must be a list of objects")
                linked[kind] = values
            card = {
                "id": f"queue:{task_id}",
                "title": str(task.get("title") or "Canonical queue task"),
                "status": str(task.get("status") or "UNKNOWN").upper(),
                "owner": str(record.get("agent_type") or "Unknown"),
                "last_updated": str(task.get("updated_at") or record.get("last_seen_at") or ""),
                "issues": linked["issues"],
                "prs": linked["prs"],
                "evidence": evidence,
                "next": "",
                "warnings": [],
                "sessions": [{
                    "node_id": record["node_id"],
                    "session_name": record["session_name"],
                    "state": state,
                }],
            }
            if card["status"] == "COMPLETED":
                card["status"] = "DONE"
                if not evidence:
                    card["warnings"].append(
                        "Canonical task is completed but has no verification evidence"
                    )
            cards.append(card)
    return cards


def _observation_card(record: dict[str, Any], state: str) -> dict[str, Any]:
    name = str(record["session_name"])
    return {
        "id": f"session:{record['stable_session_id']}",
        "title": f"Session observation: {name}",
        "status": "UNKNOWN",
        "owner": str(record.get("agent_type") or "Unknown"),
        "last_updated": str(record.get("last_seen_at") or ""),
        "issues": [],
        "prs": [],
        "evidence": "",
        "next": "Link a canonical Terminal MCP task or GitHub work item",
        "warnings": [
            f"No canonical queue task linked; session state is {state or 'UNKNOWN'}"
        ],
        "sessions": [{
            "node_id": record["node_id"],
            "session_name": name,
            "state": state or "UNKNOWN",
        }],
    }


def snapshot_from_terminal_mcp(
    source: ReadSource, projects: list[LiveProject] | tuple[LiveProject, ...]
) -> dict[str, Any]:
    """Build a sanitized snapshot from explicitly identified live sessions."""
    if not projects:
        raise SourceError("at least one live project is required")
    records: dict[tuple[str, str], dict[str, Any]] = {}
    targets: list[str] = []
    for project in projects:
        result = source.call("terminal_registry_search", {
            "query": project.session_name,
            "node_id": project.node_id,
            "scope": "fleet",
            "include_offline": True,
            "limit": 10,
        })
        record = _matching_record(result, project)
        records[(project.node_id, project.session_name)] = record
        targets.append(f"{project.node_id}/{project.session_name}")

    inspected = source.call("terminal_batch_inspect", {
        "targets": targets, "compact": True, "tail_lines": 1,
    })
    inspected_targets = inspected.get("targets")
    if not isinstance(inspected_targets, list):
        raise SourceError("terminal_batch_inspect.targets must be a list")
    states = {
        (str(item.get("node")), str(item.get("session"))): str(item.get("state") or "UNKNOWN")
        for item in inspected_targets
        if isinstance(item, dict)
    }

    output: list[dict[str, Any]] = []
    for project in projects:
        key = (project.node_id, project.session_name)
        record = records[key]
        state = states.get(key, "UNKNOWN")
        tasks = source.call(
            "terminal_session_tasks", {"session": project.session_name}
        )
        cards = _task_cards(tasks, record, state)
        if not cards:
            cards = [_observation_card(record, state)]
        output.append({
            "id": project.id,
            "name": project.name,
            "remote": project.remote,
            "cards": cards,
        })
    return {"projects": output}
