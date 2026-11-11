import datetime as dt
import os

from jashelp import Sandbox, git

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
            f"---\ntopic: vercel\nupdated: {TODAY}\n---\n# vercel\n\n## Preview deploys ignore env vars added after the build starts\n"
            f"Redeploy after changing variables.\n<!-- {TODAY} · box mac · project example-app · thread 5232d102 -->\n",
        )
        code, _, err = self.jas("lesson", "vercel", "preview deploys ignore env vars added after the build starts")
        self.assertEqual(code, 1)
        self.assertIn("already has the lesson", err)
        code, _, err = self.jas("lesson", "vercel", "Preview deploys ignore env vars added after build start", "-p", "x")
        self.assertEqual(code, 0)
        self.assertIn("warning: similar lesson", err)
        self.assertEqual(path.read_text().count("\n## "), 2)

    def test_lesson_body_is_at_most_four_lines(self):
        code, _, err = self.jas("lesson", "git", "Claim", "-p", "x", stdin="1\n2\n3\n4\n5\n")
        self.assertEqual(code, 1)
        self.assertIn("more than four lines", err)
        self.assertFalse((self.brain / "Lessons" / "git.md").exists())
