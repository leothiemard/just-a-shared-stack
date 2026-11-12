import json
import subprocess

from jashelp import Sandbox, git


class SyncTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.remote = self.base / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.a, self.b = self.base / "a", self.base / "b"
        for home, box, first in ((self.a, "mac", ["--first-push"]), (self.b, "linux-box", [])):
            home.mkdir()
            self.use_home(home)
            self.ok("init", "--box", box, "--zone", "personal", "--brain", str(self.remote), "--apply")
            self.write_config(sources=[])
            self.ok("sync", *first)

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

    def test_concurrent_lessons_on_one_topic_merge(self):
        self.on(self.a, "lesson", "vercel", "Preview deploys ignore late env vars", "-p", "example-app")
        self.on(self.a, "sync")
        self.on(self.b, "sync")
        self.on(self.a, "lesson", "vercel", "Edge config needs a redeploy", "-p", "example-app")
        self.on(self.b, "lesson", "vercel", "Build cache survives branch renames", "-p", "other-app")
        self.assertEqual(self.on(self.a, "sync")[0], 0)
        code, _, err = self.on(self.b, "sync")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.on(self.a, "sync")[0], 0)
        for home in (self.a, self.b):
            text = (home / "brain" / "Lessons" / "vercel.md").read_text()
            self.assertEqual(text.count("\n## "), 3, text)
            self.assertEqual(text.count("topic: vercel"), 1)
            self.assertNotIn("<<<<<<<", text)

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

    def test_remote_of_another_zone_is_neither_pulled_nor_pushed(self):
        other = self.b / "brain"
        (other / "brain.json").write_text('{"version": 1, "zone": "work"}\n')
        git(other, "commit", "-qam", "change zone")
        git(other, "push", "-q")
        remote_head = git(self.remote, "rev-parse", "HEAD")
        self.on(self.a, "log", "-t", "Personal entry", "-p", "example-app", stdin="- personal\n")
        code, _, err = self.on(self.a, "sync")
        self.assertEqual(code, 1)
        self.assertIn("zone 'work'", err)
        self.assertEqual(git(self.remote, "rev-parse", "HEAD"), remote_head)
        self.assertIn('"personal"', (self.a / "brain" / "brain.json").read_text())
        self.assertIn("Personal entry", git(self.a / "brain", "show", "HEAD:Projects/example-app/Log.md"))

    def test_detached_head_stops_before_anything_is_committed(self):
        root = self.a / "brain"
        git(root, "checkout", "-q", "--detach")
        self.on(self.a, "log", "-t", "Detached entry", "-p", "example-app", stdin="- x\n")
        head, status = git(root, "rev-parse", "HEAD"), git(root, "status", "--porcelain")
        code, _, err = self.on(self.a, "sync")
        self.assertEqual(code, 1)
        self.assertIn("detached", err)
        self.assertEqual(git(root, "rev-parse", "HEAD"), head)
        self.assertEqual(git(root, "status", "--porcelain"), status)

    def test_a_branch_the_remote_lacks_is_never_pushed(self):
        other = self.b / "brain"
        (other / "brain.json").write_text('{"version": 1, "zone": "work"}\n')
        git(other, "commit", "-qam", "change zone")
        git(other, "push", "-q")
        root = self.a / "brain"
        git(root, "checkout", "-q", "-b", "personal-notes")
        self.on(self.a, "log", "-t", "Personal notes", "-p", "example-app", stdin="- private\n")
        head = git(root, "rev-parse", "HEAD")
        code, _, err = self.on(self.a, "sync")
        self.assertEqual(code, 1)
        self.assertIn("personal-notes", err)
        self.assertIn("main", err)
        self.assertEqual(git(self.remote, "branch", "--format=%(refname:short)").split(), ["main"])
        self.assertEqual(git(root, "rev-parse", "HEAD"), head)

    def test_a_new_branch_of_the_same_zone_is_not_pushed_either(self):
        root = self.a / "brain"
        git(root, "checkout", "-q", "-b", "side")
        code, _, err = self.on(self.a, "sync")
        self.assertEqual(code, 1)
        self.assertIn("git switch main", err)
        self.assertEqual(git(self.remote, "branch", "--format=%(refname:short)").split(), ["main"])

    def work_remote_on_trunk(self, work=None):
        """A bare work-zone remote whose only branch is `trunk`; `work`, when given, is an existing empty one."""
        if work is None:
            work = self.base / "work.git"
            subprocess.run(["git", "init", "-q", "--bare", "-b", "trunk", str(work)], check=True)
        seed = self.base / f"seed-{work.name}"
        subprocess.run(["git", "clone", "-q", str(work), str(seed)], check=True, capture_output=True)
        git(seed, "checkout", "-q", "-b", "trunk")
        (seed / "brain.json").write_text('{"version": 1, "zone": "work"}\n')
        git(seed, "add", "brain.json")
        git(seed, "commit", "-qm", "work brain")
        git(seed, "push", "-q", "origin", "trunk")
        return work

    def test_a_remote_without_tracking_refs_is_not_taken_for_empty(self):
        work = self.work_remote_on_trunk()
        root = self.a / "brain"
        git(root, "remote", "set-url", "origin", str(work))
        git(root, "config", "--unset-all", "remote.origin.fetch")
        self.use_home(self.a)
        self.write_config("brain", remote=str(work))
        for ref in git(root, "for-each-ref", "--format=%(refname)", "refs/remotes/").split():
            git(root, "update-ref", "-d", ref)
        self.on(self.a, "log", "-t", "Private entry", "-p", "example-app", stdin="- private\n")
        code, _, err = self.on(self.a, "sync")
        self.assertEqual(code, 1, err)
        self.assertIn("trunk", err)
        self.assertEqual(git(work, "branch", "--format=%(refname:short)").split(), ["trunk"])

    def test_sync_works_without_tracking_refs(self):
        for root in (self.a / "brain", self.b / "brain"):
            git(root, "config", "--unset-all", "remote.origin.fetch")
        self.on(self.a, "log", "-t", "From mac", "-p", "example-app", stdin="- a\n")
        self.assertEqual(self.on(self.a, "sync")[0], 0)
        code, _, err = self.on(self.b, "sync")
        self.assertEqual(code, 0, err)
        self.assertIn("From mac", (self.b / "brain" / "Projects" / "example-app" / "Log.md").read_text())

    def test_a_remote_that_moved_after_the_fetch_is_not_overwritten(self):
        from unittest import mock
        from jas import sync

        self.on(self.b, "log", "-t", "From linux", "-p", "example-app", stdin="- b\n")
        self.on(self.a, "log", "-t", "From mac", "-p", "example-app", stdin="- a\n")
        real = sync.exchange

        def moved(root, *args, **kwargs):
            self.on(self.b, "sync")
            self.use_home(self.a)
            return real(root, *args, **kwargs)

        with mock.patch.object(sync, "exchange", moved):
            code, _, err = self.on(self.a, "sync")
        self.assertIn("push failed", err)
        self.assertIn("From linux", git(self.remote, "show", "main:Projects/example-app/Log.md"))

    def empty_remote(self, name):
        path = self.base / name
        subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
        return path

    def test_a_push_url_never_sends_the_brain_elsewhere(self):
        elsewhere = self.empty_remote("elsewhere.git")
        git(self.a / "brain", "config", "remote.origin.pushurl", str(elsewhere))
        self.on(self.a, "log", "-t", "Private entry", "-p", "example-app", stdin="- private\n")
        code, _, err = self.on(self.a, "sync")
        self.assertEqual(code, 0, err)
        self.assertEqual(git(elsewhere, "for-each-ref"), "")
        self.assertIn("Private entry", git(self.remote, "show", "main:Projects/example-app/Log.md"))

    def test_an_origin_other_than_the_configured_remote_is_refused(self):
        elsewhere = self.empty_remote("elsewhere.git")
        git(self.a / "brain", "remote", "set-url", "origin", str(elsewhere))
        self.on(self.a, "log", "-t", "Private entry", "-p", "example-app", stdin="- private\n")
        code, _, err = self.on(self.a, "sync")
        self.assertEqual(code, 1, err)
        self.assertIn("brain.remote", err)
        self.assertEqual(git(elsewhere, "for-each-ref"), "")

    def test_the_configured_remote_matches_in_another_url_form(self):
        root = self.a / "brain"
        git(root, "remote", "set-url", "origin", f"file://{self.remote}/")
        self.on(self.a, "log", "-t", "From mac", "-p", "example-app", stdin="- a\n")
        code, _, err = self.on(self.a, "sync")
        self.assertEqual(code, 0, err)
        self.assertIn("From mac", git(self.remote, "show", "main:Projects/example-app/Log.md"))

    def test_a_remote_name_in_box_config_is_never_resolved(self):
        work, empty = self.work_remote_on_trunk(), self.empty_remote("empty.git")
        root = self.a / "brain"
        git(root, "remote", "set-url", "origin", "alias")
        git(root, "config", "remote.alias.url", str(empty))
        git(root, "config", "remote.alias.pushurl", str(work))
        self.use_home(self.a)
        self.write_config("brain", remote="alias")
        self.on(self.a, "log", "-t", "Private entry", "-p", "example-app", stdin="- private\n")
        code, _, err = self.on(self.a, "sync")
        self.assertEqual(code, 1, err)
        self.assertIn("full URL", err)
        self.assertEqual(git(work, "branch", "--format=%(refname:short)").split(), ["trunk"])

    def test_an_scp_url_that_is_also_a_remote_name_is_refused(self):
        work = self.work_remote_on_trunk()
        root, location = self.a / "brain", "brain@host.invalid:brain.git"
        git(root, "remote", "set-url", "origin", location)
        git(root, "config", f"remote.{location}.url", str(work))
        self.use_home(self.a)
        self.write_config("brain", remote=location)
        code, _, err = self.on(self.a, "sync")
        self.assertEqual(code, 1, err)
        self.assertIn("full URL", err)
        self.assertEqual(git(work, "branch", "--format=%(refname:short)").split(), ["trunk"])

    def test_a_push_rewrite_in_git_config_is_refused(self):
        elsewhere = self.empty_remote("elsewhere.git")
        git(self.a / "brain", "config", f"url.{elsewhere}.pushInsteadOf", str(self.remote))
        self.on(self.a, "log", "-t", "Private entry", "-p", "example-app", stdin="- private\n")
        code, _, err = self.on(self.a, "sync")
        self.assertEqual(code, 1, err)
        self.assertIn("pushInsteadOf", err)
        self.assertEqual(git(elsewhere, "for-each-ref"), "")

    def point_a_at(self, remote):
        git(self.a / "brain", "remote", "set-url", "origin", str(remote))
        self.use_home(self.a)
        self.write_config("brain", remote=str(remote))

    def branches(self, remote):
        return git(remote, "branch", "--format=%(refname:short)").split()

    def test_plain_sync_never_publishes_to_an_empty_remote(self):
        from unittest import mock
        from jas import sync

        empty = self.empty_remote("fresh.git")
        self.point_a_at(empty)
        self.on(self.a, "log", "-t", "Private entry", "-p", "example-app", stdin="- private\n")
        real = sync.fetch

        def then_another_writer(*args, **kwargs):
            found = real(*args, **kwargs)
            self.work_remote_on_trunk(empty)
            return found

        with mock.patch.object(sync, "fetch", then_another_writer):
            code, out, err = self.on(self.a, "sync")
        self.assertEqual(code, 0, err)
        self.assertIn("jas sync --first-push", out)
        self.assertEqual(self.branches(empty), ["trunk"])
        self.assertIn("Private entry", git(self.a / "brain", "show", "HEAD:Projects/example-app/Log.md"))

    def test_first_push_publishes_an_empty_remote_once(self):
        empty = self.empty_remote("fresh.git")
        self.point_a_at(empty)
        code, out, err = self.on(self.a, "sync", "--first-push")
        self.assertEqual(code, 0, err)
        self.assertEqual(git(empty, "rev-parse", "main"), git(self.a / "brain", "rev-parse", "HEAD"))
        code, _, err = self.on(self.a, "sync", "--first-push")
        self.assertEqual(code, 1)
        self.assertIn("not empty", err)

    def test_first_push_stops_when_the_remote_gains_a_branch_before_the_push(self):
        from unittest import mock
        from jas import sync

        empty = self.empty_remote("fresh.git")
        self.point_a_at(empty)
        real, calls = sync.fetch, []

        def then_another_writer(*args, **kwargs):
            found = real(*args, **kwargs)
            if not calls:
                git(self.a / "brain", "push", "-q", str(empty), "HEAD:refs/heads/main")
            calls.append(found)
            return found

        before = git(self.a / "brain", "rev-parse", "HEAD")
        self.on(self.a, "log", "-t", "Private entry", "-p", "example-app", stdin="- private\n")
        with mock.patch.object(sync, "fetch", then_another_writer):
            code, _, err = self.on(self.a, "sync", "--first-push")
        self.assertEqual(code, 1, err)
        self.assertIn("not empty", err)
        self.assertEqual(git(empty, "rev-parse", "main"), before)

    def test_init_against_an_empty_remote_names_the_first_push(self):
        empty = self.empty_remote("fresh.git")
        home = self.base / "c"
        home.mkdir()
        self.use_home(home)
        out = self.ok("init", "--box", "pi", "--zone", "personal", "--brain", str(empty), "--apply")
        self.assertIn("jas sync --first-push", out)
