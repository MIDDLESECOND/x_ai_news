# -*- coding: utf-8 -*-
import unittest
import sys
import tempfile
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT = ROOT / "playbooks" / "daily-brief-synthesis.md"
ORCHESTRATOR = ROOT / "scripts" / "daily_orchestrator.py"
sys.path.insert(0, str(ROOT / "scripts"))

from synthesis_lease import acquire_lease, release_lease  # noqa: E402


class DailySynthesisPolicyTest(unittest.TestCase):
    def read_private_prompt(self):
        if not PROMPT.exists():
            self.skipTest("private synthesis playbook is not present in this checkout")
        return PROMPT.read_text(encoding="utf-8")

    def test_prompt_uses_bounded_context_and_validated_writers(self):
        text = self.read_private_prompt()
        self.assertIn("current_analysis_context.json", text)
        self.assertIn("scripts/apply_triage.py", text)
        self.assertIn("scripts/signal_inbox.py", text)
        self.assertIn("scripts/build_report_dossiers.py", text)
        self.assertIn("不得直接编辑 `config/claims.yaml`", text)
        self.assertIn("不得自动改 status", text)

    def test_orchestrator_refuses_stale_context(self):
        text = ORCHESTRATOR.read_text(encoding="utf-8")
        self.assertIn('result = run([sys.executable, "scripts/build_analysis_context.py", "--date", day]', text)
        self.assertIn("拒绝使用旧上下文", text)
        self.assertIn('ROOT / "playbooks" / "daily-brief-synthesis.md"', text)
        self.assertIn("current = datetime.now()", text)
        self.assertIn("APP_LEASE_STALE_AFTER = 30 * 60", text)
        self.assertIn("stale_after=APP_LEASE_STALE_AFTER", text)
        self.assertIn('synthesis_env.pop("ANTHROPIC_API_KEY", None)', text)
        self.assertIn("timeout=2400, env=synthesis_env", text)
        self.assertIn('"--tools", "Read,Write,Edit,Bash"', text)
        self.assertIn('"--disallowedTools", "PowerShell", "WebFetch", "WebSearch"', text)
        self.assertIn('ap.add_argument("--synthesis-only"', text)
        self.assertIn("run_synthesis(day, brief, build_context(day))", text)

    def test_prompt_and_fallback_share_synthesis_lease(self):
        prompt = self.read_private_prompt()
        orchestrator = ORCHESTRATOR.read_text(encoding="utf-8")
        self.assertIn("scripts/synthesis_lease.py acquire", prompt)
        self.assertIn("scripts/synthesis_lease.py release", prompt)
        self.assertIn('ROOT, day, "orchestrator", stale_after=APP_LEASE_STALE_AFTER',
                      orchestrator)
        self.assertIn("release_lease(ROOT, day, \"orchestrator\")", orchestrator)

    def test_synthesis_lease_rejects_a_second_owner(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertIsNotNone(acquire_lease(root, "2026-08-04", "app"))
            self.assertIsNone(acquire_lease(root, "2026-08-04", "orchestrator"))
            self.assertFalse(release_lease(root, "2026-08-04", "orchestrator"))
            self.assertTrue(release_lease(root, "2026-08-04", "app"))
            self.assertIsNotNone(acquire_lease(root, "2026-08-04", "orchestrator"))

    def test_synthesis_lease_can_reclaim_an_abandoned_app_lease(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertIsNotNone(acquire_lease(root, "2026-08-04", "app"))
            lease = root / "data/state/locks/synthesis-2026-08-04.lease"
            abandoned_at = time.time() - 31 * 60
            os.utime(lease, (abandoned_at, abandoned_at))

            self.assertIsNotNone(acquire_lease(
                root, "2026-08-04", "orchestrator", stale_after=30 * 60))
            stale_receipt = root / "data/state/locks/synthesis-2026-08-04-app.token"
            self.assertFalse(stale_receipt.exists())
            self.assertFalse(release_lease(root, "2026-08-04", "app"))
            self.assertTrue(release_lease(root, "2026-08-04", "orchestrator"))


if __name__ == "__main__":
    unittest.main()
