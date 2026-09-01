#!/usr/bin/env python3
"""Minimal target that immediately shows a menu/approval-style prompt and
waits for a line of input -- models a Claude/Codex approval dialog
("Allow this command to run? approve or deny? [y/n]") for the
TARGET_AWAITING_APPROVAL regression coverage in test_send_reliability.py.
A real program, real stdin/stdout, launched via `exec -a codex`/`exec -a
claude` so tmux's own #{pane_current_command} genuinely reports the
adapter name under test -- not a stub of tmux/adapter behavior.
"""
import sys

print("Allow this command to run?")
print("approve or deny? [y/n] ", end="", flush=True)
answer = sys.stdin.readline()
print(f"APPROVED={answer.strip()}", flush=True)
sys.stdin.readline()  # keep the pane alive so a stray Enter (if the bug
                      # regresses) has something to be swallowed by/echoed
                      # into, rather than the process exiting immediately
