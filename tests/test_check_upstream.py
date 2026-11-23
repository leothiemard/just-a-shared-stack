import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_upstream", ROOT / "scripts" / "check_upstream.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class UpstreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="upstream test ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.upstream = self.base / "upstream"
        self.upstream.mkdir()
        self.git("init", "-b", "main", "--template=")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.write("skills/example/SKILL.md", "original instructions\n")
        self.write("skills/example/old.txt", "old resource\n")
        self.write("other/SKILL.md", "unrelated\n")
        self.baseline = self.commit()
        self.output = self.base / "output"
        (self.output / "diffs").mkdir(parents=True)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.upstream, capture_output=True, text=True, check=True).stdout.strip()

    def write(self, name, content):
        path = self.upstream / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self):
        self.git("add", "-A")
        self.git("-c", "commit.gpgsign=false", "commit", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def item(self, ident="example", baseline=None, source_path="skills/example"):
        return {"id": ident, "path": source_path, "reviewed_commit": baseline or self.baseline, "local_paths": ["skills/personal"]}

    def config(self, items=None, ref="HEAD"):
        return {"git_url": str(self.upstream), "ref": ref, "items": items or [self.item()]}

    def check(self, config=None):
        return checker.check_source("fixture", config or self.config(), self.output)

    def test_unrelated_commits_do_not_report_a_skill_update(self):
        self.write("other/SKILL.md", "changed elsewhere\n")
        head = self.commit()
        result = self.check()
        self.assertEqual(result["latest_commit"], head)
        self.assertEqual(result["items"][0]["status"], "unchanged")
        self.assertEqual(list((self.output / "diffs").iterdir()), [])

    def test_modified_added_and_deleted_supporting_files_have_full_diff(self):
        self.write("skills/example/SKILL.md", "improved upstream instructions\n")
        self.write("skills/example/references/new.md", "new supporting guidance\n")
        (self.upstream / "skills/example/old.txt").unlink()
        self.commit()
        item = self.check()["items"][0]
        self.assertEqual(item["status"], "changed")
        self.assertEqual({row["status"] for row in item["changes"]}, {"A", "M", "D"})
        diff = (self.output / item["patch"]).read_text()
        self.assertIn("-original instructions", diff)
        self.assertIn("+improved upstream instructions", diff)
        self.assertIn("+new supporting guidance", diff)

    def test_moved_scope_is_flagged_as_removed(self):
        (self.upstream / "skills/example").rename(self.upstream / "skills/renamed")
        self.commit()
        item = self.check()["items"][0]
        self.assertEqual(item["status"], "removed")
        self.assertTrue(all(row["status"] == "D" for row in item["changes"]))

    def test_independent_review_baselines_are_respected(self):
        self.write("skills/example/SKILL.md", "new instructions\n")
        latest = self.commit()
        result = self.check(self.config([self.item("unreviewed"), self.item("reviewed", baseline=latest)]))
        self.assertEqual([row["status"] for row in result["items"]], ["changed", "unchanged"])
        self.assertEqual(result["items"][0]["reviewed_commit"], self.baseline)

    def test_bad_baseline_does_not_hide_other_results(self):
        result = self.check(self.config([self.item("bad", baseline="a" * 40), self.item("good")]))
        self.assertEqual([row["status"] for row in result["items"]], ["error", "unchanged"])

    def test_missing_baseline_path_is_an_error(self):
        item = self.check(self.config([self.item(source_path="nonexistent")]))["items"][0]
        self.assertEqual(item["status"], "error")
        self.assertIn("absent at its reviewed commit", item["error"])

    def test_unavailable_head_is_an_error(self):
        result = self.check(self.config(ref="missing-branch"))
        self.assertIsNone(result["latest_commit"])
        self.assertEqual(result["items"][0]["status"], "error")

    def test_report_preserves_local_files_and_baselines(self):
        personal = self.base / "personal repo"
        (personal / "skills/personal").mkdir(parents=True)
        (personal / "skills/personal/SKILL.md").write_text("adapted personal text\n")
        (personal / "sources.json").write_text(json.dumps({"reviewed": self.baseline}))
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in personal.rglob("*") if p.is_file()}
        self.write("skills/example/SKILL.md", "upstream edit\n")
        self.commit()
        destination = self.base / "new report"
        with patch.object(checker, "load_sources", return_value=({"fixture": self.config()}, ["unconfigured"])):
            report = checker.run_check(personal, destination)
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in personal.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(report["sources"][0]["items"][0]["status"], "changed")
        self.assertEqual(json.loads((destination / "report.json").read_text()), report)
        markdown = (destination / "report.md").read_text()
        self.assertIn("diffs/fixture-example.patch", markdown)
        self.assertIn("unconfigured", markdown)
        self.assertIn("1 changed", markdown)

    def test_existing_reports_and_source_directories_are_protected(self):
        personal = self.base / "personal"
        (personal / "skills").mkdir(parents=True)
        with patch.object(checker, "load_sources", return_value=({"fixture": self.config()}, [])):
            with self.assertRaises(FileExistsError):
                checker.run_check(personal, self.output)
            with self.assertRaises(ValueError):
                checker.run_check(personal, personal / "skills/new-report")
        self.assertFalse((personal / "skills/new-report").exists())

    def test_manifest_validation_and_source_selection(self):
        sources, omitted = checker.load_sources(ROOT, ["mattpocock"])
        self.assertEqual(set(sources), {"mattpocock"})
        self.assertIn("pstack", omitted)
        sanity_sources, _ = checker.load_sources(ROOT, ["sanity-agent-toolkit"])
        self.assertEqual(set(sanity_sources), {"sanity-agent-toolkit"})
        self.assertEqual(len(sanity_sources["sanity-agent-toolkit"]["items"]), 5)
        with self.assertRaises(ValueError):
            checker.load_sources(ROOT, ["tailwind-agent-skills"])


if __name__ == "__main__":
    unittest.main()
