# -*- coding: utf-8 -*-
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from brief_marker import SYNTH_MARKER  # noqa: E402
from finalize_daily import (_sync_confirmed, artifact_manifest,
                            finalize_day)  # noqa: E402


class FinalizeDailyTest(unittest.TestCase):
    def make_root(self, td):
        root = Path(td)
        for directory in ("briefs", "config", "data/state/claim_inbox"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        (root / "briefs/2026-08-05.md").write_text(
            f"# brief\n\n{SYNTH_MARKER}\n", encoding="utf-8")
        (root / "config/claims.yaml").write_text("claims: []\n", encoding="utf-8")
        (root / "config/accounts.yaml").write_text("seed: []\n", encoding="utf-8")
        return root

    def runner(self, root, calls, *, fail_backup=False, fail_dossier=False,
               mutate_on_backup=False, fail_receipt_sync=False):
        def run(cmd, timeout):
            script = Path(cmd[1]).name
            calls.append(script)
            if script == "build_report_dossiers.py":
                path = root / "reports/dossiers/index.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("dossiers\n", encoding="utf-8")
            elif script == "build_monthly_claim_review.py":
                path = root / "reports/monthly/2026-07-claim-review.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("monthly\n", encoding="utf-8")
            elif script == "backup_private.py" and mutate_on_backup:
                (root / "config/claims.yaml").write_text(
                    "claims: []\n# changed\n", encoding="utf-8")
            is_receipt_sync = "--receipt-sync-only" in cmd
            code = 1 if ((script == "backup_private.py" and
                          (fail_backup or (fail_receipt_sync and is_receipt_sync)))
                         or (script == "build_report_dossiers.py" and fail_dossier)) else 0
            return subprocess.CompletedProcess(cmd, code, stdout="ok", stderr="")
        return run

    def test_finalize_orders_views_before_backup_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            calls = []
            first = finalize_day(root, "2026-08-05",
                                 run_command=self.runner(root, calls),
                                 today=date(2026, 8, 5))
            self.assertEqual(first["status"], "complete")
            self.assertEqual(calls, ["build_analysis_context.py",
                                     "build_story_clusters.py",
                                     "build_source_independence.py",
                                     "build_source_health.py",
                                     "build_story_lineage.py",
                                     "build_report_dossiers.py",
                                     "build_monthly_claim_review.py",
                                     "backup_private.py",
                                     "backup_private.py"])
            receipt = json.loads((root / "data/state/daily_runs/2026-08-05.json")
                                 .read_text(encoding="utf-8"))
            self.assertEqual(receipt["artifact_fingerprint"],
                             first["artifact_fingerprint"])
            receipt_path = root / "data/state/daily_runs/2026-08-05.json"
            marker_path = root / "data/state/daily_runs/2026-08-05.sync.json"
            self.assertTrue(_sync_confirmed(receipt_path, marker_path))

            calls.clear()
            second = finalize_day(root, "2026-08-05",
                                  run_command=self.runner(root, calls),
                                  today=date(2026, 8, 5))
            self.assertTrue(second["skipped"])
            self.assertNotIn("backup_private.py", calls)

            calls.clear()
            warned = finalize_day(
                root, "2026-08-05",
                run_command=self.runner(root, calls, fail_dossier=True),
                today=date(2026, 8, 5))
            self.assertEqual(warned["status"], "complete_with_warnings")
            self.assertIn("backup_private.py", calls)

    def test_backup_failure_and_inflight_change_never_mark_complete(self):
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            failed = finalize_day(
                root, "2026-08-05",
                run_command=self.runner(root, [], fail_backup=True))
            self.assertEqual(failed["status"], "backup_failed")

        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            changed = finalize_day(
                root, "2026-08-05",
                run_command=self.runner(root, [], mutate_on_backup=True))
            self.assertEqual(changed["status"], "changed_during_backup")

    def test_complete_receipt_must_also_reach_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            result = finalize_day(
                root, "2026-08-05",
                run_command=self.runner(root, [], fail_receipt_sync=True))
            self.assertEqual(result["status"], "receipt_backup_failed")
            self.assertFalse((root / "data/state/daily_runs/2026-08-05.sync.json").exists())

    def test_receipt_sync_exception_is_retryable(self):
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)

            def timeout_runner(cmd, timeout):
                if "--receipt-sync-only" in cmd:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

            failed = finalize_day(root, "2026-08-05", run_command=timeout_runner)
            self.assertEqual(failed["status"], "receipt_backup_failed")
            persisted = json.loads(
                (root / "data/state/daily_runs/2026-08-05.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "receipt_backup_failed")

            calls = []
            retried = finalize_day(root, "2026-08-05",
                                   run_command=self.runner(root, calls))
            self.assertEqual(retried["status"], "complete")
            self.assertEqual(calls.count("backup_private.py"), 2)

    def test_historical_complete_day_skips_before_touching_current_context(self):
        from datetime import date
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            finalize_day(root, "2026-08-05", today=date(2026, 8, 5),
                         run_command=self.runner(root, []))
            calls = []
            skipped = finalize_day(root, "2026-08-05", today=date(2026, 8, 6),
                                   run_command=self.runner(root, calls))
            self.assertTrue(skipped["skipped"])
            self.assertEqual(calls, [])

            commands = []
            def record_commands(cmd, timeout):
                commands.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
            finalize_day(root, "2026-08-05", today=date(2026, 8, 6), force=True,
                         run_command=record_commands)
            context_cmd = next(cmd for cmd in commands
                               if Path(cmd[1]).name == "build_analysis_context.py")
            self.assertIn("--output", context_cmd)
            self.assertIn("analysis_context", " ".join(context_cmd))

    def test_manifest_covers_private_scope_but_excludes_volatile_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = self.make_root(td)
            (root / "playbooks").mkdir()
            (root / "playbooks/private.md").write_text("private\n", encoding="utf-8")
            (root / "data/state/locks").mkdir(parents=True)
            (root / "data/state/locks/x.lock").write_text("pid=1\n", encoding="utf-8")
            (root / "data/state/http_cache/bodies").mkdir(parents=True)
            (root / "data/state/http_cache/bodies/full-response.bin").write_bytes(
                b"third-party full response")
            (root / "data/state/orchestrator_log.jsonl").write_text("live\n", encoding="utf-8")
            (root / "data/state/daily_runs").mkdir(parents=True)
            (root / "data/state/daily_runs/2026-08-05.json").write_text(
                "{}\n", encoding="utf-8")
            (root / "data/state/daily_runs/2026-08-05.sync.json").write_text(
                "{}\n", encoding="utf-8")
            manifest, _ = artifact_manifest(root, "2026-08-05")
            self.assertIn("playbooks/private.md", manifest)
            self.assertNotIn("data/state/locks/x.lock", manifest)
            self.assertNotIn("data/state/http_cache/bodies/full-response.bin", manifest)
            self.assertNotIn("data/state/orchestrator_log.jsonl", manifest)
            self.assertNotIn("data/state/daily_runs/2026-08-05.json", manifest)
            self.assertNotIn("data/state/daily_runs/2026-08-05.sync.json", manifest)


if __name__ == "__main__":
    unittest.main()
