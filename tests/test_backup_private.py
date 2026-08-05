# -*- coding: utf-8 -*-
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from backup_private import perform_backup  # noqa: E402
from backup_scope import snapshot_manifest  # noqa: E402


def run_git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


class BackupPrivateTest(unittest.TestCase):
    def make_repos(self, td):
        base = Path(td)
        source = base / "source"
        backup = base / "backup"
        for path in (source / "config", source / "reports",
                     source / "data/state/daily_runs", backup):
            path.mkdir(parents=True, exist_ok=True)
        (source / "config/claims.yaml").write_text("claims: []\n", encoding="utf-8")
        (source / "config/accounts.yaml").write_text("seed: []\n", encoding="utf-8")
        (source / "reports/current.md").write_text("current\n", encoding="utf-8")
        (source / "data/state/daily_runs/2026-08-05.json").write_text(
            '{"status":"complete"}\n', encoding="utf-8")
        run_git(backup, "init")
        run_git(backup, "config", "user.email", "test@example.com")
        run_git(backup, "config", "user.name", "Test")
        (backup / "README.md").write_text("backup\n", encoding="utf-8")
        run_git(backup, "add", "README.md")
        run_git(backup, "commit", "-m", "init")
        return source, backup

    def args(self, source, *, receipt_sync_only=False):
        _, fingerprint = snapshot_manifest(source, "2026-08-05")
        return SimpleNamespace(finalize_date="2026-08-05",
                               artifact_fingerprint=fingerprint,
                               receipt_sync_only=receipt_sync_only)

    def test_backup_tracks_ignored_files_and_prunes_deleted_source_files(self):
        with tempfile.TemporaryDirectory() as td:
            source, backup = self.make_repos(td)
            (backup / ".git/info/exclude").write_text("reports/current.md\n",
                                                      encoding="utf-8")
            stale = backup / "reports/stale.md"
            stale.parent.mkdir(parents=True)
            stale.write_text("stale\n", encoding="utf-8")
            run_git(backup, "add", "-f", "reports/stale.md")
            run_git(backup, "commit", "-m", "stale")

            perform_backup(self.args(source), root=source,
                           backup_repo=backup, push=False)
            self.assertFalse(stale.exists())
            run_git(backup, "ls-files", "--error-unmatch", "reports/current.md")
            with self.assertRaises(subprocess.CalledProcessError):
                run_git(backup, "ls-files", "--error-unmatch", "reports/stale.md")

            (source / "reports/current.md").unlink()
            perform_backup(self.args(source), root=source,
                           backup_repo=backup, push=False)
            self.assertFalse((backup / "reports/current.md").exists())
            with self.assertRaises(subprocess.CalledProcessError):
                run_git(backup, "ls-files", "--error-unmatch", "reports/current.md")

    def test_no_change_run_still_verifies_existing_commit(self):
        with tempfile.TemporaryDirectory() as td:
            source, backup = self.make_repos(td)
            args = self.args(source)
            perform_backup(args, root=source, backup_repo=backup, push=False)
            head = run_git(backup, "rev-parse", "HEAD").stdout.strip()
            perform_backup(args, root=source, backup_repo=backup, push=False)
            self.assertEqual(run_git(backup, "rev-parse", "HEAD").stdout.strip(), head)

    def test_no_change_retry_pushes_a_previously_unpushed_commit(self):
        with tempfile.TemporaryDirectory() as td:
            source, backup = self.make_repos(td)
            remote = Path(td) / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                           capture_output=True)
            run_git(backup, "remote", "add", "origin", str(remote))
            run_git(backup, "push", "-u", "origin", "master")

            perform_backup(self.args(source), root=source,
                           backup_repo=backup, push=False)
            local_head = run_git(backup, "rev-parse", "HEAD").stdout.strip()
            remote_before = run_git(backup, "rev-parse", "origin/master").stdout.strip()
            self.assertNotEqual(local_head, remote_before)

            with patch("backup_private.GhAccount", side_effect=lambda: nullcontext()):
                perform_backup(self.args(source), root=source,
                               backup_repo=backup, push=True)
            self.assertEqual(run_git(backup, "rev-parse", "origin/master").stdout.strip(),
                             local_head)


if __name__ == "__main__":
    unittest.main()
