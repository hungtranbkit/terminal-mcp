"""Safety gates for the standalone node janitor; no tmux process touched."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

p=Path(__file__).resolve().parents[1]/'deploy/fleet-node-janitor.py'
spec=importlib.util.spec_from_file_location('fleet_node_janitor',p)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class JanitorSafetyTest(unittest.TestCase):
    def test_protected_name(self):
        self.assertEqual(m.safe('tmcp-maint-hp-audit',True)[1],'protected')
        self.assertEqual(m.safe('my-manual-shell',True)[1],'protected')
    def test_attached_rejected(self):
        with patch.object(m,'details',return_value=['bash','1234','1','1','1']),patch.object(m,'unfinished',return_value=False):
            self.assertFalse(m.safe('hp-shell-old',True)[0])
    def test_unfinished_rejected(self):
        with patch.object(m,'details',return_value=['bash','1234','0','1','1']),patch.object(m,'unfinished',return_value=True):
            self.assertFalse(m.safe('hp-shell-old',True)[0])
    def test_live_child_rejected(self):
        with patch.object(m,'details',return_value=['bash','1234','0','1','1']),patch.object(m,'unfinished',return_value=False),patch.object(m,'cmd',return_value=(0,'python3')):
            self.assertEqual(m.safe('hp-shell-old',True)[1],'active-children')
    def test_unknown_agent_rejected(self):
        with patch.object(m,'details',return_value=['claude','1234','0','1','1']),patch.object(m,'unfinished',return_value=False):
            self.assertEqual(m.safe('hp-claude-old',True)[1],'agent-active-or-unknown')

if __name__=='__main__':unittest.main()
