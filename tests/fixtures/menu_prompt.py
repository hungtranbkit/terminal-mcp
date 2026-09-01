#!/usr/bin/env python3
"""Reproduces, as a real program (not a send_keys simulation), the exact
live-production shape found in a real, attended Claude Code session
(mesflow) that test_send_reliability.py's
test_claude_send_refused_for_a_multi_choice_selection_menu regresses:
Claude Code's own AskUserQuestion-style interactive multi-choice menu --
numbered options, arrow-key/Tab navigation, Enter accepts the highlighted
choice. Distinct from waiting_prompt.py's plain y/n approval shape."""
import sys

print("1. Keep option A")
print("2. Try option B")
print()
print("Enter to select · Tab/Arrow keys to navigate · Esc to cancel", flush=True)
answer = sys.stdin.readline()
print(f"CHOSE={answer.strip()}", flush=True)
sys.stdin.readline()
