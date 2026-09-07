#!/usr/bin/env python3
"""Reproduces, as a real program (not a send_keys simulation), the exact
live-production pane shape found in a real, attended Claude Code session
(`window2`, 2026-09-07) that caused a real false positive: an ordinary,
idle composer whose own typed prompt text happens to contain the word
"Permission" (this project's own subject matter -- a permissions/roles
feature), plus Claude Code's own normal context-usage status line. NOT
an approval/menu prompt -- a normal send here must be ALLOWED, unlike
waiting_prompt.py/menu_prompt.py's own real approval-prompt fixtures.
"""
import sys

print("│ > Làm Role/Permission step 2 custom role web đi")
print()
print("  new task? /clear to save 891k tokens", flush=True)
answer = sys.stdin.readline()
print(f"RECEIVED={answer.strip()}", flush=True)
sys.stdin.readline()  # keep the pane alive
