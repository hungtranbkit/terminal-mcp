"""Local CLI for managing the dashboard's password-login accounts
(terminal-mcp-webauth). Never accepts a password as a command-line
argument or environment variable -- always prompted via getpass so it
never lands in shell history, `ps`, or a log. This is the operator-facing
counterpart to webauth.py's WebAuthStore; it never runs the HTTP server
and touches only webauth.db.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from .webauth import WebAuthStore, default_webauth_db_path


def _prompt_new_password() -> str | None:
    first = getpass.getpass("New password: ")
    if len(first) < 12:
        print("Password must be at least 12 characters.", file=sys.stderr)
        return None
    second = getpass.getpass("Confirm password: ")
    if first != second:
        print("Passwords did not match.", file=sys.stderr)
        return None
    return first


def cmd_set_password(args: argparse.Namespace) -> int:
    from .server_http import bootstrap_secret_path, delete_bootstrap_secret_if_matches  # local: avoids pulling in the HTTP server for every CLI invocation

    store = WebAuthStore(args.db)
    password = _prompt_new_password()
    if password is None:
        return 1
    if not store.set_password(args.username, password):
        print(f"No such user: {args.username!r}. Use create-user first.", file=sys.stderr)
        return 1
    print(f"Password updated for {args.username!r}. Every existing session for this user was invalidated.")
    # Scoped to THIS store's own path (args.db) -- never the global/
    # production default -- so `--db /tmp/other.db set-password admin`
    # can never touch production's own bootstrap secret file.
    if delete_bootstrap_secret_if_matches(args.username, store.path):
        print(f"Removed one-time bootstrap secret file: {bootstrap_secret_path(store.path)}")
    return 0


def cmd_create_user(args: argparse.Namespace) -> int:
    store = WebAuthStore(args.db)
    password = _prompt_new_password()
    if password is None:
        return 1
    store.create_or_replace_user(args.username, password, must_change_password=False)
    print(f"Created/updated user {args.username!r}.")
    return 0


def cmd_list_users(args: argparse.Namespace) -> int:
    store = WebAuthStore(args.db)
    if not store.has_any_user():
        print("No local password-login users exist yet.")
        return 0
    # Deliberately minimal: WebAuthStore has no list-all method (never
    # needed for anything else) -- read the one thing this command needs
    # directly rather than growing the store's own API for a CLI-only case.
    import sqlite3

    connection = sqlite3.connect(store.path)
    try:
        rows = connection.execute(
            "SELECT username, must_change_password, created_at FROM webauth_users ORDER BY username"
        ).fetchall()
    finally:
        connection.close()
    for username, must_change, created_at in rows:
        flag = " (must change password on next login)" if must_change else ""
        print(f"{username}  created {created_at}{flag}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="terminal-mcp-webauth", description=__doc__)
    parser.add_argument("--db", type=Path, default=default_webauth_db_path(),
                        help="Path to webauth.db (default: the same one the running service uses)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_set = sub.add_parser("set-password", help="Change an existing user's password (prompts via getpass)")
    p_set.add_argument("username")
    p_set.set_defaults(func=cmd_set_password)

    p_create = sub.add_parser("create-user", help="Create a new local user (prompts via getpass)")
    p_create.add_argument("username")
    p_create.set_defaults(func=cmd_create_user)

    p_list = sub.add_parser("list-users", help="List local users (no password material)")
    p_list.set_defaults(func=cmd_list_users)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
