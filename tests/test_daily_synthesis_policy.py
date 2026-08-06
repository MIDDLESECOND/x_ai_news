# -*- coding: utf-8 -*-
import unittest
import sys
import tempfile
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
PROMPT = ROOT / "playbooks" / "daily-brief-synthesis.md"
ORCHESTRATOR = ROOT / "scripts" / "daily_orchestrator.py"
BACKUP = ROOT / "scripts" / "backup_private.py"
FINALIZER = ROOT / "scripts" / "finalize_daily.py"
ANALYSIS = ROOT / "scripts" / "build_analysis_context.py"
DOSSIERS = ROOT / "scripts" / "build_report_dossiers.py"
MONTHLY = ROOT / "scripts" / "build_monthly_claim_review.py"
STORY_CLUSTERS = ROOT / "scripts" / "build_story_clusters.py"
SOURCE_INDEPENDENCE = ROOT / "scripts" / "build_source_independence.py"
SOURCE_HEALTH = ROOT / "scripts" / "build_source_health.py"
STORY_LINEAGE = ROOT / "scripts" / "build_story_lineage.py"
sys.path.insert(0, str(ROOT / "scripts"))

from synthesis_lease import acquire_lease, release_lease  # noqa: E402
import daily_orchestrator as orchestrator  # noqa: E402


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
        self.assertIn("scripts/finalize_daily.py", text)
        self.assertIn("source_item_id", text)
        self.assertIn("snapshot_hash", text)
        self.assertIn("support|counter|neutral|confounder", text)
        self.assertIn("本管线覆盖的厂商源未发现", text)
        self.assertIn("事故影响时窗内", text)
        self.assertIn("关键数字必须在正文就近链接", text)
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
        self.assertIn("run_finalization(day, brief)", text)
        backup = BACKUP.read_text(encoding="utf-8")
        self.assertIn('ap.add_argument("--finalize-date", required=True)', backup)
        self.assertIn('ap.add_argument("--artifact-fingerprint", required=True)', backup)
        self.assertIn("产物指纹已变化，拒绝备份", backup)
        self.assertIn("verify_target_snapshot", backup)
        self.assertIn('"--receipt-sync-only"', backup)
        self.assertIn("private-backup.lock", backup)
        finalizer = FINALIZER.read_text(encoding="utf-8")
        self.assertIn('"--finalize-date", day', finalizer)
        self.assertIn('"--artifact-fingerprint", fingerprint', finalizer)
        self.assertIn('"--receipt-sync-only"', finalizer)
        for script in ("build_story_clusters.py", "build_source_independence.py",
                       "build_source_health.py", "build_story_lineage.py"):
            self.assertIn(script, finalizer)

    def test_every_derived_writer_has_an_independent_lock(self):
        for path, lock_name in (
            (ANALYSIS, "analysis-context.lock"),
            (DOSSIERS, "report-dossiers.lock"),
            (MONTHLY, "monthly-review-"),
            (STORY_CLUSTERS, "story-clusters.lock"),
            (SOURCE_INDEPENDENCE, "source-independence.lock"),
            (SOURCE_HEALTH, "source-health.lock"),
            (STORY_LINEAGE, "story-lineage.lock"),
        ):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("exclusive_lock", text)
                self.assertIn(lock_name, text)

    def test_prompt_and_fallback_share_synthesis_lease(self):
        prompt = self.read_private_prompt()
        orchestrator = ORCHESTRATOR.read_text(encoding="utf-8")
        self.assertIn("scripts/synthesis_lease.py acquire", prompt)
        self.assertIn("scripts/synthesis_lease.py release", prompt)
        self.assertIn('ROOT, day, "orchestrator", stale_after=APP_LEASE_STALE_AFTER',
                      orchestrator)
        self.assertIn("release_lease(ROOT, day, \"orchestrator\")", orchestrator)
        synthesis_start = orchestrator.index("synthesis_result = run(")
        finalize = orchestrator.index(
            "run_finalization(day, brief, lease_held=True)", synthesis_start)
        release = orchestrator.index('release_lease(ROOT, day, "orchestrator")',
                                     synthesis_start)
        self.assertLess(finalize, release)

    def test_finalization_does_not_bypass_an_app_lease(self):
        with (patch.object(orchestrator, "brief_synthesized", return_value=True),
              patch.object(orchestrator, "acquire_lease", return_value=None),
              patch.object(orchestrator, "log"),
              patch.object(orchestrator, "run") as run):
            result = orchestrator.run_finalization(
                "2026-08-05", Path("brief.md"))
        self.assertIsNone(result)
        run.assert_not_called()

    def test_finalization_owns_and_releases_lease_outside_fallback(self):
        completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with (patch.object(orchestrator, "brief_synthesized", return_value=True),
              patch.object(orchestrator, "acquire_lease", return_value="token") as acquire,
              patch.object(orchestrator, "release_lease") as release,
              patch.object(orchestrator, "log"),
              patch.object(orchestrator, "run", return_value=completed) as run):
            result = orchestrator.run_finalization(
                "2026-08-05", Path("brief.md"))
        self.assertIs(result, completed)
        acquire.assert_called_once_with(
            orchestrator.ROOT, "2026-08-05", "orchestrator-finalize",
            stale_after=orchestrator.APP_LEASE_STALE_AFTER)
        release.assert_called_once_with(
            orchestrator.ROOT, "2026-08-05", "orchestrator-finalize")
        run.assert_called_once()

    def test_fallback_finalization_reuses_held_lease(self):
        completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with (patch.object(orchestrator, "brief_synthesized", return_value=True),
              patch.object(orchestrator, "acquire_lease") as acquire,
              patch.object(orchestrator, "release_lease") as release,
              patch.object(orchestrator, "log"),
              patch.object(orchestrator, "run", return_value=completed)):
            result = orchestrator.run_finalization(
                "2026-08-05", Path("brief.md"), lease_held=True)
        self.assertIs(result, completed)
        acquire.assert_not_called()
        release.assert_not_called()

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
