"""Durable orchestration for source-backed Archify generation."""

from __future__ import annotations

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .archify_policy import ArchifyPolicyError, ArchifyProjectPolicy
from .archify_runtime import ArchifyRuntimeError
from .archify_source import ArchifyAuthor, ArchifyEvidenceError, DIAGRAM_TYPES, SourceInspector
from .archify_store import ArchifyStore
from .config import ArchifyConfig


class ArchifyServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ArchifyService:
    def __init__(self, config: ArchifyConfig, policy: ArchifyProjectPolicy, runtime: Any,
                 store: ArchifyStore, *, artifact_root: str | Path) -> None:
        self.config = config
        self.policy = policy
        self.runtime = runtime
        self.store = store
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.artifact_root, 0o700)
        except OSError:
            pass
        self._executor = ThreadPoolExecutor(max_workers=config.workers,
                                            thread_name_prefix="terminal-mcp-archify")
        self._lock = threading.RLock()
        self._submitted: set[str] = set()
        self._started = False
        self._closed = False

    def status(self) -> dict[str, Any]:
        runtime = self.runtime.status()
        return {
            "enabled": self.config.enabled,
            "ready": bool(self.config.enabled and runtime.get("ready")),
            "runtime": runtime,
            "limits": {
                "max_files": self.config.max_files,
                "max_source_bytes": self.config.max_source_bytes,
                "max_prompt_chars": self.config.max_prompt_chars,
                "timeout_seconds": self.config.timeout_seconds,
                "history_limit": self.config.history_limit,
            },
        }

    def projects(self) -> list[dict[str, str]]:
        return [project.as_dict() for project in self.policy.discover_projects()]

    def start(self) -> None:
        with self._lock:
            if self._started or self._closed:
                return
            self._started = True
        self.store.recover_running()
        for job in self.store.queued():
            self._submit(job["id"])

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)

    def create_job(self, project: str, diagram_type: str, prompt: str = "") -> dict[str, Any]:
        if not self.config.enabled:
            raise ArchifyServiceError("ARCHIFY_DISABLED", "Archify is disabled by configuration.")
        runtime = self.runtime.status()
        if not runtime.get("ready"):
            raise ArchifyServiceError("ARCHIFY_UNAVAILABLE", runtime.get("message", "Archify is unavailable."))
        if diagram_type not in DIAGRAM_TYPES:
            raise ArchifyServiceError("INVALID_DIAGRAM_TYPE", "Choose a supported Archify diagram type.")
        if not isinstance(prompt, str):
            raise ArchifyServiceError("INVALID_REQUEST", "Prompt must be text.")
        prompt = prompt.strip()
        if len(prompt) > self.config.max_prompt_chars:
            raise ArchifyServiceError("PROMPT_TOO_LONG", "Prompt exceeds the configured limit.")
        try:
            selected = self.policy.resolve_project(project)
        except ArchifyPolicyError as exc:
            raise ArchifyServiceError(exc.code, exc.message) from exc
        job = self.store.create(selected.path, selected.name, diagram_type, prompt)
        self._submit(job["id"])
        return self.store.get(job["id"])

    def list_jobs(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        return self.store.list(limit=min(limit or self.config.history_limit, self.config.history_limit))

    def get_job(self, job_id: str) -> dict[str, Any]:
        try:
            return self.store.get(job_id)
        except KeyError as exc:
            raise ArchifyServiceError("JOB_NOT_FOUND", "Archify job was not found.") from exc

    def html_path(self, job_id: str) -> Path:
        job = self.get_job(job_id)
        relative = job.get("html_path")
        if job["status"] != "completed" or not relative:
            raise ArchifyServiceError("ARTIFACT_MISSING", "Completed HTML artifact is unavailable.")
        expected_root = (self.artifact_root / job_id).resolve()
        try:
            artifact = (self.artifact_root / relative).resolve(strict=True)
        except OSError as exc:
            raise ArchifyServiceError("ARTIFACT_MISSING", "HTML artifact is missing.") from exc
        if not artifact.is_relative_to(expected_root) or not artifact.is_file() or artifact.is_symlink():
            raise ArchifyServiceError("ARTIFACT_MISSING", "HTML artifact path is invalid.")
        return artifact

    def _submit(self, job_id: str) -> None:
        with self._lock:
            if self._closed or job_id in self._submitted:
                return
            self._submitted.add(job_id)
        future = self._executor.submit(self._execute, job_id)
        future.add_done_callback(lambda _future: self._discard_submission(job_id))

    def _discard_submission(self, job_id: str) -> None:
        with self._lock:
            self._submitted.discard(job_id)

    @staticmethod
    def _atomic_json(path: Path, value: Any) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def _execute(self, job_id: str) -> None:
        if not self.store.mark_running(job_id):
            return
        job = self.store.get(job_id)
        job_root = (self.artifact_root / job_id).resolve()
        try:
            try:
                selected = self.policy.resolve_project(job["project_path"])
            except ArchifyPolicyError as exc:
                raise ArchifyServiceError(exc.code, exc.message) from exc
            if not job_root.is_relative_to(self.artifact_root):
                raise ArchifyServiceError("ARTIFACT_MISSING", "Job artifact path escaped its root.")
            job_root.mkdir(parents=True, exist_ok=False, mode=0o700)
            inspection = SourceInspector(
                max_files=self.config.max_files, max_bytes=self.config.max_source_bytes,
            ).inspect(selected.path)
            ir = ArchifyAuthor().build(job["diagram_type"], inspection, job["prompt"])
            ir_path = job_root / "diagram.json"
            metadata_path = job_root / "metadata.json"
            output_part = job_root / "diagram.part.html"
            output_path = job_root / "diagram.html"
            self._atomic_json(ir_path, ir)
            metadata = {"job_id": job_id, "project": selected.path,
                        "diagram_type": job["diagram_type"], "prompt": job["prompt"],
                        "inspection": inspection.as_dict()}
            self._atomic_json(metadata_path, metadata)
            self.runtime.render(job["diagram_type"], ir_path, output_part,
                                repo_root=selected.path)
            if not output_part.is_file() or not output_part.resolve().is_relative_to(job_root):
                raise ArchifyServiceError("ARTIFACT_MISSING", "Renderer output is missing or invalid.")
            os.replace(output_part, output_path)
            self.store.complete(
                job_id, ir_path=f"{job_id}/diagram.json",
                metadata_path=f"{job_id}/metadata.json", html_path=f"{job_id}/diagram.html",
            )
        except (ArchifyRuntimeError, ArchifyEvidenceError, ArchifyServiceError) as exc:
            self.store.fail(job_id, code=getattr(exc, "code", "GENERATION_FAILED"),
                            message=getattr(exc, "message", str(exc)),
                            detail=getattr(exc, "detail", ""))
        except Exception as exc:  # noqa: BLE001 -- worker failures must become durable state
            self.store.fail(job_id, code="GENERATION_FAILED",
                            message=f"Generation failed: {type(exc).__name__}.")
