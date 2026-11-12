import datetime as dt
import os

from jasshelp import Sandbox, disk_full, git

TODAY = dt.date.today().isoformat()


class NotesTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.brain = self.new_box()

    def test_log_creates_project_with_hub_skeleton(self):
        self.ok("log", "-t", "First  entry", "-p", "example-app", stdin="- did a thing\n")
        folder = self.brain / "Projects" / "example-app"
        hub = (folder / "example-app.md").read_text()
        self.assertTrue(hub.startswith("---\nproject: example-app\n"))
        self.assertIn("[[Log]]", hub)
        self.assertIn(f"## {TODAY} — First entry\n- did a thing\n<!-- box mac -->\n", (folder / "Log.md").read_text())

    def test_log_entry_goes_on_top_of_existing_entries(self):
        log = self.brain / "Projects" / "Example App" / "Log.md"
        log.parent.mkdir(parents=True)
        log.write_text("---\nproject: Example App\n---\n# Log\n\nIntro line.\n\n## 2026-01-01 — Older\n- old\n")
        self.ok("log", "-t", "Newer", "-p", "example-app", stdin="- new\n")
        self.assertEqual(
            log.read_text(),
            f"---\nproject: Example App\n---\n# Log\n\nIntro line.\n\n## {TODAY} — Newer\n- new\n<!-- box mac -->\n\n## 2026-01-01 — Older\n- old\n",
        )
        self.assertFalse((self.brain / "Projects" / "example-app").exists())

    def test_a_heading_inside_a_longer_fence_is_not_an_entry(self):
        log = self.brain / "Projects" / "example-app" / "Log.md"
        log.parent.mkdir(parents=True)
        intro = "# Log\n\n````md\n```\n## 2026-01-02 — Example inside a fence\n````\n\n"
        log.write_text(intro + "## 2026-01-01 — Older\n- old\n")
        self.ok("log", "-t", "Newer", "-p", "example-app", stdin="- new\n")
        self.assertTrue(log.read_text().startswith(intro + f"## {TODAY} — Newer\n"), log.read_text())

    def test_a_failed_write_leaves_the_log_and_lessons_whole(self):
        self.ok("log", "-t", "First", "-p", "example-app", stdin="- kept\n")
        self.ok("lesson", "vercel", "Preview deploys ignore late env vars", "-p", "example-app")
        log = self.brain / "Projects" / "example-app" / "Log.md"
        lessons = self.brain / "Lessons" / "vercel.md"
        before = log.read_text(), lessons.read_text()
        with disk_full(log.parent), self.assertRaises(OSError):
            self.jass("log", "-t", "Second", "-p", "example-app", stdin="- new\n")
        with disk_full(lessons.parent), self.assertRaises(OSError):
            self.jass("lesson", "vercel", "Edge config needs a redeploy", "-p", "example-app")
        self.assertEqual((log.read_text(), lessons.read_text()), before)
        self.assertEqual(sorted(p.name for p in log.parent.iterdir()), ["Log.md", "example-app.md"])
        self.assertEqual([p.name for p in lessons.parent.iterdir()], ["vercel.md"])

    def test_a_private_log_is_never_readable_while_it_is_replaced(self):
        from unittest import mock

        self.ok("log", "-t", "First", "-p", "example-app", stdin="- kept\n")
        log = self.brain / "Projects" / "example-app" / "Log.md"
        os.chmod(log, 0o600)
        modes, real_fsync = [], os.fsync

        def recording_fsync(fd):
            modes.append(os.fstat(fd).st_mode & 0o777)
            return real_fsync(fd)

        old = os.umask(0o022)
        try:
            with mock.patch("os.fsync", recording_fsync):
                self.ok("log", "-t", "Second", "-p", "example-app", stdin="- new\n")
                self.ok("log", "-t", "New project", "-p", "other-app", stdin="- new\n")
        finally:
            os.umask(old)
        self.assertEqual(set(modes), {0o600})
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.brain / "Projects" / "other-app" / "Log.md").stat().st_mode & 0o777, 0o644)

    def test_an_empty_log_title_creates_nothing(self):
        code, _, err = self.jass("log", "-t", "  ", "-p", "new-project")
        self.assertEqual(code, 1)
        self.assertIn("Log title is empty", err)
        self.assertFalse((self.brain / "Projects" / "new-project").exists())

    def test_log_project_defaults_to_the_repo_of_the_current_directory(self):
        work = self.base / "example-app"
        (work / "sub").mkdir(parents=True)
        git(work, "init", "-q")
        cwd = os.getcwd()
        os.chdir(work / "sub")
        self.addCleanup(os.chdir, cwd)
        self.ok("log", "-t", "From cwd")
        self.assertTrue((self.brain / "Projects" / "example-app" / "Log.md").exists())

    def test_lesson_format_duplicates_and_near_duplicates(self):
        self.ok("lesson", "Vercel", "Preview deploys ignore env vars added after the build starts", "-p", "example-app", "--thread", "5232d102", stdin="Redeploy after changing variables.\n")
        path = self.brain / "Lessons" / "vercel.md"
        self.assertEqual(
            path.read_text(),
            f"---\ntopic: vercel\n---\n# vercel\n\n## Preview deploys ignore env vars added after the build starts\n"
            f"Redeploy after changing variables.\n<!-- {TODAY} · box mac · project example-app · thread 5232d102 -->\n",
        )
        code, _, err = self.jass("lesson", "vercel", "preview deploys ignore env vars added after the build starts")
        self.assertEqual(code, 1)
        self.assertIn("already has the lesson", err)
        code, _, err = self.jass("lesson", "vercel", "Preview deploys ignore env vars added after build start", "-p", "x")
        self.assertEqual(code, 0)
        self.assertIn("warning: similar lesson", err)
        self.assertEqual(path.read_text().count("\n## "), 2)

    def test_lesson_body_is_at_most_four_lines(self):
        code, _, err = self.jass("lesson", "git", "Claim", "-p", "x", stdin="1\n2\n3\n4\n5\n")
        self.assertEqual(code, 1)
        self.assertIn("more than four lines", err)
        self.assertFalse((self.brain / "Lessons" / "git.md").exists())

    def test_old_brains_gain_the_lessons_merge_driver(self):
        from jass import brain

        attributes = self.brain / ".gitattributes"
        attributes.write_text("Log.md merge=union\n*.png binary\n")
        missing = brain.skeleton_missing(self.brain, "personal")
        self.assertEqual(missing[".gitattributes"], "Log.md merge=union\n*.png binary\n* text=auto eol=lf\nLessons/*.md merge=union\n")
        brain.write_skeleton(self.brain, "personal")
        self.assertNotIn(".gitattributes", brain.skeleton_missing(self.brain, "personal"))
        self.assertIn("merge: union", git(self.brain, "check-attr", "merge", "--", "Lessons/vercel.md"))

    def test_temp_files_left_by_a_kill_are_never_committed(self):
        import time

        folder = self.brain / "Projects" / "example-app"
        folder.mkdir(parents=True)
        stale, fresh = folder / ".Log.md.jass-tmp-0123abcd", folder / ".Log.md.jass-tmp-4567cdef"
        stale.write_text("half a private entry\n")
        fresh.write_text("a write in progress\n")
        hour_ago = time.time() - 3600
        os.utime(stale, (hour_ago, hour_ago))
        self.ok("sync")
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertNotIn("jass-tmp", git(self.brain, "ls-files"))

    def test_cleanup_and_ignore_spare_files_that_only_look_like_temp_files(self):
        import time

        folder = self.brain / "Projects" / "example-app"
        folder.mkdir(parents=True)
        tracked, forced = folder / "report.jass-tmp-notes.md", folder / ".Log.md.jass-tmp-89abcdef"
        tracked.write_text("a real report\n")
        forced.write_text("committed on purpose\n")
        git(self.brain, "add", "-f", str(tracked), str(forced))
        git(self.brain, "commit", "-qm", "documents")
        short, link, draft = folder / ".file.jass-tmp-1234", folder / ".hub.md.jass-tmp-0123abcd", folder / "draft.jass-tmp-v2.md"
        short.write_text("not jass's\n")
        draft.write_text("a new note\n")
        link.symlink_to(tracked)
        hour_ago = time.time() - 3600
        for path in (tracked, forced, short, draft, link):
            os.utime(path, (hour_ago, hour_ago), follow_symlinks=False)
        self.ok("sync")
        for path in (tracked, forced, short, draft):
            self.assertTrue(path.exists(), path.name)
        self.assertTrue(link.is_symlink())
        files = git(self.brain, "ls-files").splitlines()
        self.assertIn("Projects/example-app/report.jass-tmp-notes.md", files)
        self.assertIn("Projects/example-app/draft.jass-tmp-v2.md", files)
        self.assertEqual(git(self.brain, "status", "--porcelain", "--", str(tracked), str(forced)), "")

    def test_old_brains_trade_the_broad_temp_file_ignore_line_for_the_exact_one(self):
        ignore = self.brain / ".gitignore"
        ignore.write_text(".DS_Store\n*.jass-tmp-*\n")
        git(self.brain, "commit", "-qam", "old ignore file")
        self.assertIn("warn  temp files", self.jass("doctor")[1])
        self.ok("init", "--apply")
        lines = ignore.read_text().splitlines()
        self.assertEqual(lines[0], ".DS_Store")
        self.assertNotIn("*.jass-tmp-*", lines)
        self.assertIn(".*.jass-tmp-" + "[0-9a-f]" * 8, lines)
        self.assertEqual(git(self.brain, "status", "--porcelain"), "")
        self.assertIn("ok    temp files", self.jass("doctor")[1])
