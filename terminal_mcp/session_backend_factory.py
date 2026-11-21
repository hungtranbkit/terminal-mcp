"""Conservative backend rollout selection for *new/local* sessions.

TMUX_ONLY is the fail-closed default.  DUAL_READ and HERDR_CANARY retain
tmux as the control backend; their additive observation/canary paths are
opt-in callers.  HERDR_DEFAULT selects hc only on an explicit node allowlist.
"""
from __future__ import annotations

import os
import math
import shutil
from enum import Enum
from pathlib import Path
from typing import Mapping

from .herdr_core_backend import HerdrCoreBackend
from .session_backend import SessionBackend
from .tmux import TmuxClient


class BackendMode(str, Enum):
    TMUX_ONLY = "TMUX_ONLY"
    DUAL_READ = "DUAL_READ"
    HERDR_CANARY = "HERDR_CANARY"
    HERDR_DEFAULT = "HERDR_DEFAULT"


def backend_mode(env: Mapping[str, str] | None = None) -> BackendMode:
    source = os.environ if env is None else env
    raw = source.get("TERMINAL_MCP_SESSION_MODE", "TMUX_ONLY").strip().upper()
    try:
        return BackendMode(raw)
    except ValueError:
        return BackendMode.TMUX_ONLY


def build_session_backend(*, node_id: str, env: Mapping[str, str] | None = None) -> SessionBackend:
    source = os.environ if env is None else env
    mode = backend_mode(source)
    nodes = {item.strip() for item in source.get("TERMINAL_MCP_HERDR_NODES", "").split(",") if item.strip()}
    if mode is BackendMode.HERDR_DEFAULT and node_id in nodes:
        binary = source.get("TERMINAL_MCP_HC_BIN", "hc")
        binary_path = Path(binary).expanduser() if os.sep in binary else None
        available = binary_path.is_file() and os.access(binary_path, os.X_OK) if binary_path else shutil.which(binary) is not None
        try:
            timeout_seconds = float(source.get("TERMINAL_MCP_HC_TIMEOUT", "5"))
        except ValueError:
            timeout_seconds = 0
        if not available or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            return TmuxClient()
        return HerdrCoreBackend(
            binary=binary,
            timeout_seconds=timeout_seconds,
            session=source.get("TERMINAL_MCP_HC_SESSION") or None,
        )
    return TmuxClient()
