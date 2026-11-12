import subprocess

from jashelp import Sandbox, git


class UnfinishedGitTests(Sandbox):
    """`jas sync` never commits over a rebase, merge or cherry-pick that is still in progress."""

    def conflict(self, root, operation):
        (root / "note.md").write_text("base\n")
        git(root, "add", "note.md")
        git(root, "commit", "-q", "-m", "base")
        git(root, "checkout", "-q", "-b", "topic")
        (root / "note.md").write_text("topic\n")
        git(root, "commit", "-q", "-am", "topic")
        git(root, "checkout", "-q", "main")
        (root / "note.md").write_text("main\n")
        git(root, "commit", "-q", "-am", "main")
        command = {"rebase": ["rebase", "main", "topic"], "merge": ["merge", "topic"], "cherry-pick": ["cherry-pick", "topic"]}[operation]
        result = subprocess.run(["git", "-C", str(root), *command], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def stops(self, operation):
        root = self.new_box()
        self.conflict(root, operation)
        head = git(root, "rev-parse", "HEAD")
        status = git(root, "status", "--porcelain")
        code, _, err = self.jas("sync")
        self.assertEqual(code, 1)
        self.assertIn(f"git {operation} is in progress", err)
        self.assertIn(f"git {operation} --continue", err)
        self.assertIn(f"git {operation} --abort", err)
        self.assertEqual(git(root, "rev-parse", "HEAD"), head)
        self.assertEqual(git(root, "status", "--porcelain"), status)
        self.assertFalse((root / "Boxes").exists())
        code, out, _ = self.jas("doctor")
        self.assertEqual(code, 1)
        self.assertIn(f"git {operation} is in progress", out)

    def test_rebase_in_progress(self):
        self.stops("rebase")

    def test_merge_in_progress(self):
        self.stops("merge")

    def test_cherry_pick_in_progress(self):
        self.stops("cherry-pick")

    def test_sync_stops_on_unmerged_index_entries(self):
        root = self.new_box()
        self.conflict(root, "merge")
        git(root, "merge", "--quit")
        code, _, err = self.jas("sync")
        self.assertEqual(code, 1)
        self.assertIn("unmerged", err)
        self.assertNotIn("<<<<<<<", git(root, "show", "HEAD:note.md"))
