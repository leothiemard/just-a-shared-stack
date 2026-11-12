import datetime as dt
import json
import os
import subprocess
from pathlib import Path

from jasshelp import Sandbox, git, tree

from jass import sync


class UnfinishedGitTests(Sandbox):
    """`jass sync` never commits over a rebase, merge or cherry-pick that is still in progress."""

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
        code, _, err = self.jass("sync")
        self.assertEqual(code, 1)
        self.assertIn(f"git {operation} is in progress", err)
        self.assertIn(f"git {operation} --continue", err)
        self.assertIn(f"git {operation} --abort", err)
        self.assertEqual(git(root, "rev-parse", "HEAD"), head)
        self.assertEqual(git(root, "status", "--porcelain"), status)
        self.assertFalse((root / "Boxes").exists())
        code, out, _ = self.jass("doctor")
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
        code, _, err = self.jass("sync")
        self.assertEqual(code, 1)
        self.assertIn("unmerged", err)
        self.assertNotIn("<<<<<<<", git(root, "show", "HEAD:note.md"))


class ContainmentTests(Sandbox):
    """No brain reader or writer follows a symlink out of the brain."""

    def setUp(self):
        super().setUp()
        self.store = self.base / "sessions.json"
        self.store.write_text("[]")
        self.root = self.new_box(sources=[f"json:{self.store}"])
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "example.md").write_text("# Outside\n\nunikeyword private text\n")
        (self.outside / "Log.md").write_text("outside log\n")

    def link(self, rel, target, folder=False):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target, target_is_directory=folder)

    def refused(self, *argv, stdin=""):
        before = tree(self.outside)
        code, out, err = self.jass(*argv, stdin=stdin)
        self.assertEqual(code, 1, out)
        self.assertIn("outside the brain", err)
        self.assertEqual(tree(self.outside), before)
        return out

    def test_project_folder_link(self):
        self.link("Projects/example", self.outside, folder=True)
        self.assertEqual(self.refused("hub", "example"), "")
        self.refused("log", "-p", "example", "-t", "Entry", stdin="- body\n")
        self.refused("show", "Projects/example/example.md")
        self.assertIn("No hits", self.ok("recall", "unikeyword", "--no-sync"))

    def test_dangling_log_link(self):
        (self.root / "Projects" / "dangling").mkdir(parents=True)
        self.link("Projects/dangling/Log.md", self.outside / "new.md")
        self.refused("log", "-p", "dangling", "-t", "Entry")
        self.assertFalse((self.outside / "new.md").exists())

    def test_lessons_folder_link(self):
        self.link("Lessons", self.outside, folder=True)
        self.refused("lesson", "vercel", "Preview deploys ignore late env vars", "-p", "example-app")

    def test_file_link(self):
        self.link("leak.md", self.outside / "example.md")
        self.assertIn("No hits", self.ok("recall", "unikeyword", "--no-sync"))
        self.refused("show", "leak.md")

    def test_a_box_card_link_stops_sync_before_export(self):
        updated = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
        self.store.write_text(json.dumps([{"id": "s1", "title": "T", "project": "example-app", "cwd": "/work", "started": updated, "updated": updated,
                                           "prompts": ["Do it"], "turn_finals": ["Done."]}]))
        self.link("Boxes", self.outside, folder=True)
        self.refused("sync")
        self.assertFalse((self.root / "Sessions").exists())

    def test_one_digest_folder_link_stops_the_whole_export(self):
        now = dt.datetime.now(dt.timezone.utc)
        rows = [{"id": sid, "title": "T", "project": "example-app", "cwd": "/work", "started": started.isoformat(),
                 "updated": (now - dt.timedelta(hours=2)).isoformat(), "prompts": ["Do it"], "turn_finals": ["Done."]}
                for sid, started in (("old", now - dt.timedelta(days=70)), ("new", now - dt.timedelta(hours=3)))]
        self.store.write_text(json.dumps(rows))
        self.link(f"Sessions/mac/{rows[1]['started'][:7]}", self.outside, folder=True)
        self.refused("sessions", "export")
        self.assertEqual([p for p in (self.root / "Sessions" / "mac").rglob("*.md")], [])

    def test_a_brain_json_that_leads_outside_is_no_brain(self):
        (self.outside / "brain.json").write_text('{"version": 1, "zone": "personal"}\n')
        (self.root / "brain.json").unlink()
        self.link("brain.json", self.outside / "brain.json")
        for argv in (("recall", "x", "--no-sync"), ("sync",), ("hub", "example")):
            with self.subTest(argv):
                code, _, err = self.jass(*argv)
                self.assertEqual(code, 1)
                self.assertIn("No valid brain", err)
        code, out, _ = self.jass("init", "--box", "mac", "--zone", "personal", "--new-brain")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"conflict\s+brain\s+.*is not a brain")

    def test_session_and_box_folder_links(self):
        (self.outside / "2026-01").mkdir()
        (self.outside / "2026-01" / "digest.md").write_text("---\nkind: session\nbox: other\nupdated: 2026-01-02\n---\n# x\n")
        self.link("Sessions/other", self.outside, folder=True)
        self.assertEqual(json.loads(self.ok("sessions", "list", "--json")), [])
        self.link("Sessions/mac", self.outside, folder=True)
        updated = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
        self.store.write_text(json.dumps([{"id": "s1", "title": "T", "project": "example-app", "cwd": "/work", "started": updated, "updated": updated,
                                           "prompts": ["Do it"], "turn_finals": ["Done."]}]))
        self.refused("sessions", "export")
        self.store.write_text("[]")
        self.link("Boxes", self.outside, folder=True)
        self.refused("sync")


class CaseInsensitiveContainmentTests(Sandbox):
    def test_another_spelling_of_the_brain_is_inside_it(self):
        root = self.new_box()
        if not (self.base / str(root.relative_to(self.base)).upper()).exists():
            self.skipTest("case-sensitive filesystem")
        (root / "Projects" / "real").mkdir(parents=True)
        (root / "Projects" / "alias").symlink_to(Path(str(root).upper()) / "Projects" / "real", target_is_directory=True)
        self.ok("log", "-p", "alias", "-t", "Entry", stdin="- body\n")
        self.assertIn("Entry", (root / "Projects" / "real" / "Log.md").read_text())


class SyncLockTests(Sandbox):
    def test_a_held_lock_is_never_granted_twice_however_old(self):
        root = self.new_box()
        with sync.lock(root) as held:
            self.assertTrue(held)
            os.utime(sync.lock(root).path, (0, 0))
            code, out, _ = self.jass("sync")
            self.assertEqual(code, 0)
            self.assertIn("Another jass sync is running", out)
            self.assertFalse((root / "Boxes").exists())
        self.assertTrue(sync.lock(root).path.exists())
        self.assertIn("Local-only brain", self.ok("sync"))
