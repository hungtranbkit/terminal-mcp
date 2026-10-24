#!/usr/bin/env python3
"""Prepare a node-owned workspace and scoped CLI project trust.

Called by the Linux Add Node installer. Never trusts arbitrary paths, follows
symlinks, changes system-wide trust, or disables CLI tool approvals.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import tomllib


def atomic_text(path: Path, value: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".workspace-new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def owned_workspace(home: Path) -> Path:
    workspace = home / "workspace"
    if workspace.is_symlink():
        raise ValueError("workspace must not be a symlink")
    workspace.mkdir(mode=0o755, exist_ok=True)
    if not workspace.is_dir() or workspace.stat().st_uid != os.getuid():
        raise ValueError("workspace must be a directory owned by this user")
    if workspace.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("workspace must not be group/world writable")
    if workspace.resolve() != workspace:
        raise ValueError("workspace must be located directly under HOME")
    return workspace


def allow_workspace(config_path: Path, workspace: Path) -> None:
    text = config_path.read_text(encoding="utf-8")
    if not re.search(r"(?m)^session_lifecycle:\s*$", text):
        raise ValueError("session_lifecycle block is missing")
    start = re.search(r"(?m)^session_lifecycle:\s*$", text).end()
    next_top = re.search(r"(?m)^\S", text[start:])
    end = start + next_top.start() if next_top else len(text)
    section = text[start:end]
    match = re.search(r"(?m)^  allowed_cwd_roots:\s*(?:#.*)?$", section)
    item = "    - " + str(workspace) + "\n"
    if match:
        entries = section[match.end():]
        current = re.match(r"(?:\n[ \t]*- [^\n]*)*", entries)
        if any(line.strip() == "- " + str(workspace) for line in entries.splitlines()):
            return
        section = section[:match.end()] + "\n" + item + section[match.end():].lstrip("\n")
    else:
        section = "\n  allowed_cwd_roots:\n" + item + section
    atomic_text(config_path, text[:start] + section + text[end:],
                mode=stat.S_IMODE(config_path.stat().st_mode))


def trust_codex(home: Path, workspace: Path) -> None:
    path = home / ".codex" / "config.toml"
    content = path.read_text(encoding="utf-8") if path.is_file() else ""
    parsed = tomllib.loads(content) if content else {}
    projects = parsed.get("projects", {})
    existing = projects.get(str(workspace))
    if existing is not None:
        if not isinstance(existing, dict) or existing.get("trust_level") != "trusted":
            raise ValueError("existing Codex project trust differs; manual review required")
        return
    suffix = "\n[projects." + json.dumps(str(workspace)) + ']\ntrust_level = "trusted"\n'
    revised = content.rstrip() + "\n" + suffix
    tomllib.loads(revised)
    atomic_text(path, revised,
                mode=stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600)


def trust_claude(home: Path, workspace: Path) -> None:
    path = home / ".claude.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(data, dict):
        raise ValueError("Claude config must be a JSON object")
    projects = data.setdefault("projects", {})
    existing = projects.setdefault(str(workspace), {})
    if not isinstance(existing, dict):
        raise ValueError("existing Claude project trust differs; manual review required")
    if existing.get("hasTrustDialogAccepted") is False:
        raise ValueError("Claude project explicitly untrusted; manual review required")
    if existing.get("hasTrustDialogAccepted") is True:
        return
    existing["hasTrustDialogAccepted"] = True
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                mode=stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    home = Path.home()
    workspace = owned_workspace(home)
    # Never trust project settings without first establishing ownership.
    trust_codex(home, workspace)
    trust_claude(home, workspace)
    allow_workspace(args.config, workspace)
    print("WORKSPACE_READY", workspace, "CODEx_CLAUDE_TRUST_SCOPED")


if __name__ == "__main__":
    main()
