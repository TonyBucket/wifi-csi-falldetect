import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Add project root to sys.path so we can import scripts.code_sync
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scripts.code_sync import (
    check_remote_reachable,
    get_current_branch,
    get_dirty_files,
    inspect_status,
    run_git,
)


class TestCodeSync(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_code_sync_")
        self.base_path = Path(self.temp_dir)
        self.bare_dir = self.base_path / "bare.git"
        self.clone_a = self.base_path / "clone_a"
        self.clone_b = self.base_path / "clone_b"

        # 1. Create bare repo
        run_git(["init", "--bare", str(self.bare_dir)])

        # 2. Init clone_a, create initial commit, push to bare
        run_git(["init", "-b", "main", str(self.clone_a)])
        run_git(["config", "user.name", "Test A"], cwd=self.clone_a)
        run_git(["config", "user.email", "testA@example.com"], cwd=self.clone_a)

        (self.clone_a / "hello.txt").write_text("initial content\n")
        run_git(["add", "hello.txt"], cwd=self.clone_a)
        run_git(["commit", "-m", "Initial commit"], cwd=self.clone_a)
        run_git(["remote", "add", "origin", str(self.bare_dir)], cwd=self.clone_a)
        run_git(["push", "-u", "origin", "main"], cwd=self.clone_a)

        # Set default branch in bare
        run_git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=self.bare_dir)

        # 3. Clone to clone_b
        run_git(["clone", str(self.bare_dir), str(self.clone_b)])
        run_git(["config", "user.name", "Test B"], cwd=self.clone_b)
        run_git(["config", "user.email", "testB@example.com"], cwd=self.clone_b)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_clean_status(self):
        st = inspect_status(remote="origin", cwd=self.clone_a)
        self.assertTrue(st["is_clean"])
        self.assertEqual(st["state"], "CLEAN")
        self.assertEqual(st["ahead"], 0)
        self.assertEqual(st["behind"], 0)
        self.assertEqual(st["branch"], "main")

    def test_dirty_status(self):
        (self.clone_a / "hello.txt").write_text("modified content\n")
        st = inspect_status(remote="origin", cwd=self.clone_a)
        self.assertFalse(st["is_clean"])
        self.assertEqual(st["state"], "DIRTY")
        self.assertEqual(len(st["dirty_files"]), 1)

    def test_publish_dirty_without_auto_commit_fails(self):
        (self.clone_a / "hello.txt").write_text("dirty edit\n")
        cmd = [sys.executable, str(ROOT_DIR / "scripts" / "code_sync.py"), "--repo", str(self.clone_a), "publish"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Working tree is dirty", proc.stderr)

    def test_publish_dirty_with_auto_commit_succeeds(self):
        (self.clone_a / "hello.txt").write_text("auto commit edit\n")
        cmd = [
            sys.executable,
            str(ROOT_DIR / "scripts" / "code_sync.py"),
            "--repo",
            str(self.clone_a),
            "publish",
            "--auto-commit",
            "-m",
            "Auto commit test",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Error: {proc.stderr}")
        self.assertIn("Publish successful", proc.stdout)

        # Check clean afterwards
        st = inspect_status(remote="origin", cwd=self.clone_a)
        self.assertTrue(st["is_clean"])

    def test_reconcile_dirty_refused(self):
        (self.clone_b / "hello.txt").write_text("dirty in b\n")
        cmd = [sys.executable, str(ROOT_DIR / "scripts" / "code_sync.py"), "--repo", str(self.clone_b), "reconcile"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Cannot reconcile with dirty working tree", proc.stderr)

    def test_two_way_independent_merge(self):
        # A modifies file_a.txt, commits and publishes
        (self.clone_a / "file_a.txt").write_text("file A content\n")
        run_git(["add", "file_a.txt"], cwd=self.clone_a)
        run_git(["commit", "-m", "Commit from A"], cwd=self.clone_a)
        run_git(["push", "origin", "main"], cwd=self.clone_a)

        # B modifies file_b.txt and commits locally
        (self.clone_b / "file_b.txt").write_text("file B content\n")
        run_git(["add", "file_b.txt"], cwd=self.clone_b)
        run_git(["commit", "-m", "Commit from B"], cwd=self.clone_b)

        # B reconciles (fetches from origin, merges A's changes)
        cmd = [sys.executable, str(ROOT_DIR / "scripts" / "code_sync.py"), "--repo", str(self.clone_b), "reconcile"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Reconcile failed: {proc.stderr}")
        self.assertTrue((self.clone_b / "file_a.txt").exists())
        self.assertTrue((self.clone_b / "file_b.txt").exists())

    def test_conflict_preserves_state_and_reports_files(self):
        # A edits line 1 and pushes
        (self.clone_a / "hello.txt").write_text("line modified by A\n")
        run_git(["add", "hello.txt"], cwd=self.clone_a)
        run_git(["commit", "-m", "Edit from A"], cwd=self.clone_a)
        run_git(["push", "origin", "main"], cwd=self.clone_a)

        # B edits line 1 differently and commits locally
        (self.clone_b / "hello.txt").write_text("line modified by B\n")
        run_git(["add", "hello.txt"], cwd=self.clone_b)
        run_git(["commit", "-m", "Edit from B"], cwd=self.clone_b)

        # B reconciles -> should detect conflict!
        cmd = [sys.executable, str(ROOT_DIR / "scripts" / "code_sync.py"), "--repo", str(self.clone_b), "reconcile"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("MERGE CONFLICT DETECTED!", proc.stderr)
        self.assertIn("hello.txt", proc.stderr)

        # Verify conflict markers exist in hello.txt
        content = (self.clone_b / "hello.txt").read_text()
        self.assertIn("<<<<<<<", content)
        self.assertIn(">>>>>>>", content)

    def test_offline_handling(self):
        # Create a repo pointing to non-existent remote URL
        offline_repo = self.base_path / "offline_repo"
        run_git(["init", "-b", "main", str(offline_repo)])
        run_git(["config", "user.name", "Offline User"], cwd=offline_repo)
        run_git(["config", "user.email", "offline@example.com"], cwd=offline_repo)
        (offline_repo / "test.txt").write_text("content\n")
        run_git(["add", "."], cwd=offline_repo)
        run_git(["commit", "-m", "init"], cwd=offline_repo)
        run_git(["remote", "add", "origin", "ssh://nonexistent-host-9999/repo.git"], cwd=offline_repo)

        st = inspect_status(remote="origin", cwd=offline_repo)
        self.assertEqual(st["state"], "OFFLINE")
        self.assertFalse(st["remote_reachable"])

        # Publish should report unreachable remote
        cmd = [sys.executable, str(ROOT_DIR / "scripts" / "code_sync.py"), "--repo", str(offline_repo), "publish"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unreachable (offline)", proc.stderr)


if __name__ == "__main__":
    unittest.main()
