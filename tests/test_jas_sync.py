import json
import subprocess

from jashelp import Sandbox, git


class SyncTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.remote = self.base / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.a, self.b = self.base / "a", self.base / "b"
        for home, box in ((self.a, "mac"), (self.b, "linux-box")):
            home.mkdir()
            self.use_home(home)
            self.ok("init", "--box", box, "--zone", "personal", "--brain", str(self.remote), "--apply")
            self.write_config(sources=[])
            self.ok("sync")

    def write_config(self, section="sessions", **values):
        path = self.home / ".config" / "just-a-stack" / "box.json"
        cfg = json.loads(path.read_text())
        cfg[section].update(values)
        path.write_text(json.dumps(cfg))

    def on(self, home, *argv, stdin=""):
        self.use_home(home)
        return self.jas(*argv, stdin=stdin)

    def test_concurrent_log_entries_merge_on_both_boxes(self):
        self.on(self.a, "log", "-t", "From mac", "-p", "example-app", stdin="- a\n")
        self.on(self.a, "sync")
        self.on(self.b, "sync")
        self.on(self.b, "log", "-t", "From linux", "-p", "example-app", stdin="- b\n")
        self.on(self.a, "log", "-t", "Mac again", "-p", "example-app", stdin="- a2\n")
        self.assertEqual(self.on(self.a, "sync")[0], 0)
        code, out, err = self.on(self.b, "sync")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.on(self.a, "sync")[0], 0)
        for home in (self.a, self.b):
            log = (home / "brain" / "Projects" / "example-app" / "Log.md").read_text()
            for title in ("From mac", "From linux", "Mac again"):
                self.assertIn(title, log)
            self.assertNotIn("<<<<<<<", log)
            self.assertTrue((home / "brain" / "Boxes" / "mac.md").exists())
            self.assertTrue((home / "brain" / "Boxes" / "linux-box.md").exists())

    def test_hub_conflict_fails_safe(self):
        hub = "Projects/example-app/example-app.md"
        self.on(self.a, "log", "-t", "Start", "-p", "example-app")
        self.on(self.a, "sync")
        self.on(self.b, "sync")
        (self.a / "brain" / hub).write_text("# example-app\n\nState from mac.\n")
        (self.b / "brain" / hub).write_text("# example-app\n\nState from linux.\n")
        self.assertEqual(self.on(self.a, "sync")[0], 0)
        code, _, err = self.on(self.b, "sync")
        self.assertEqual(code, 1)
        self.assertIn(f"Sync stopped: {hub} changed on another box too", err)
        self.assertIn("nothing was pushed", err)
        self.assertEqual((self.b / "brain" / hub).read_text(), "# example-app\n\nState from linux.\n")
        self.assertIn("State from linux", git(self.b / "brain", "show", f"HEAD:{hub}"))
        self.assertFalse((self.b / "brain" / ".git" / "rebase-merge").exists())
        self.assertIn("State from mac", git(self.remote, "show", f"HEAD:{hub}"))
        self.assertEqual((self.a / "brain" / hub).read_text(), "# example-app\n\nState from mac.\n")

    def test_offline_remote_is_a_warning(self):
        self.remote.rename(self.base / "gone.git")
        self.on(self.a, "log", "-t", "Offline work", "-p", "example-app")
        code, _, err = self.on(self.a, "sync")
        self.assertEqual(code, 0)
        self.assertIn("warning: could not reach the brain remote", err)
        self.assertIn("Offline work", git(self.a / "brain", "show", "HEAD:Projects/example-app/Log.md"))
        self.write_config("sync", max_age_minutes=0)
        code, out, err = self.on(self.a, "recall", "offline")
        self.assertEqual(code, 0)
        self.assertIn("Offline work", out)
        self.assertIn("warning", err)
        self.assertEqual(len(err.strip().splitlines()), 1, err)

    def test_fallback_identity_when_git_has_none(self):
        (self.base / "gitconfig").write_text("")
        self.on(self.a, "log", "-t", "No identity", "-p", "example-app")
        self.assertEqual(self.on(self.a, "sync")[0], 0)
        self.assertEqual(git(self.a / "brain", "log", "-1", "--format=%ae").strip(), "mac@just-a-stack.local")
