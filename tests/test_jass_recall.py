import datetime as dt
import json
import os
import time

from jasshelp import Sandbox

TODAY = dt.date.today().isoformat()


class RecallTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.lore = self.new_box()
        self.write("Lessons/vercel.md", f"---\ntopic: vercel\nupdated: {TODAY}\n---\n# vercel\n\n## Preview deploys ignore env vars added after the build starts\nRedeploy after changing variables.\n<!-- {TODAY} · box mac · project example-app -->\n")
        self.write(f"Sessions/linux-box/{TODAY[:7]}/{TODAY}-example-app-1234abcd.md", f"---\nkind: session\nbox: linux-box\nproject: example-app\nupdated: {TODAY}T10:00:00Z\n---\n# Fix preview deploys\n\n## Outcome\n\nPreview deploys ignore env vars added after the build starts; redeployed.\n")
        self.write("Projects/example-app/Log.md", "# log\n\n## 2025-01-02 — Old deploy work\n- deploys were slow\n<!-- box mac -->\n")

    def write(self, rel: str, text: str) -> None:
        path = self.lore / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def recall(self, *args: str) -> dict:
        return json.loads(self.ok("recall", *args, "--no-sync", "--json"))

    def test_lesson_outranks_session_for_the_same_terms(self):
        hits = self.recall("preview deploys env vars")["hits"]
        self.assertEqual([h["kind"] for h in hits[:2]], ["lesson", "session"])
        self.assertEqual(hits[1]["box"], "linux-box")

    def test_text_output_and_show(self):
        out = self.ok("recall", "preview", "deploys", "--no-sync")
        self.assertRegex(out.splitlines()[0], r"^\d+ of \d+ hits · ~\d+ tokens · lore \w+ · never synced$")
        self.assertIn("[1] Lessons/vercel.md § Preview deploys ignore env vars added after the build starts   lesson", out)
        self.assertIn("Expand with: jass show 1", out)
        shown = self.ok("show", "1")
        self.assertIn("Redeploy after changing variables.", shown)
        self.assertNotIn("# vercel\n", shown)
        section = self.ok("show", "Projects/example-app/Log.md#2025-01-02 — Old deploy work")
        self.assertIn("deploys were slow", section)

    def test_filters(self):
        self.assertEqual({h["kind"] for h in self.recall("deploys", "--kind", "log")["hits"]}, {"log"})
        self.assertEqual({h["box"] for h in self.recall("deploys", "--box", "linux-box")["hits"]}, {"linux-box"})
        self.assertNotIn("log", {h["kind"] for h in self.recall("deploys", "--since", "5w")["hits"]})
        self.write("Projects/infra/Deploys.md", "# Deploys\n\nUndated deploys checklist.\n")
        self.write("Projects/infra/Old.md", "---\nupdated: 2024-01-01\n---\n# Old\n\nOld deploys checklist.\n")
        recent = {h["path"] for h in self.recall("deploys", "checklist", "--since", "5w")["hits"]}
        self.assertIn("Projects/infra/Deploys.md", recent)
        self.assertNotIn("Projects/infra/Old.md", recent)
        self.assertEqual(self.recall("deploys", "--project", "other-app")["total"], 0)
        self.assertEqual(len(self.recall("deploys", "--project", "EXAMPLE-APP")["hits"]), 3)

    def test_budget_caps_output_but_keeps_one_hit(self):
        for n in range(8):
            self.write(f"Projects/example-app/Topic{n}.md", f"# Topic {n}\n\n## Deploy note {n}\n" + "deploy pipeline detail " * 30 + "\n")
        wide = self.recall("deploy", "--limit", "20", "--budget", "5000")["hits"]
        narrow = self.recall("deploy", "--limit", "20", "--budget", "200")["hits"]
        tiny = self.recall("deploy", "--limit", "20", "--budget", "1")["hits"]
        self.assertGreater(len(wide), len(narrow))
        self.assertEqual(len(tiny), 1)
        text = self.ok("recall", "deploy", "--limit", "20", "--budget", "200", "--no-sync")
        self.assertLess(len(text.encode()) / 4, 260)

    def test_one_file_fills_at_most_two_hits(self):
        self.write("Projects/example-app/Deploys.md", "# Deploys\n" + "".join(f"\n## Deploy step {n}\nDeploy detail.\n" for n in range(6)))
        paths = [h["path"] for h in self.recall("deploy", "--limit", "10")["hits"]]
        self.assertEqual(paths.count("Projects/example-app/Deploys.md"), 2)
        self.assertGreater(len(paths), 2)

    def test_session_hits_are_labelled_with_the_session_title(self):
        out = self.ok("recall", "redeployed", "--no-sync")
        self.assertIn("-1234abcd.md § Fix preview deploys › Outcome   session", out)

    def test_zero_hits(self):
        code, out, _ = self.jass("recall", "kubernetes", "--no-sync")
        self.assertEqual((code, out), (0, "No hits for 'kubernetes'.\n"))

    def test_incremental_reindex_sees_edits_and_deletions(self):
        self.assertEqual(self.recall("zeppelin")["total"], 0)
        self.write("Lessons/airship.md", "# airship\n\n## Zeppelin mooring needs a mast\nUse a mast.\n")
        self.assertEqual(self.recall("zeppelin")["total"], 1)
        path = self.lore / "Lessons/airship.md"
        path.write_text("# airship\n\n## Blimps float\nHelium.\n")
        os.utime(path, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
        self.assertEqual(self.recall("zeppelin")["total"], 0)
        self.assertEqual(self.recall("helium")["total"], 1)
        path.unlink()
        self.assertEqual(self.recall("helium")["total"], 0)

    def test_hub_is_capped_and_silent_when_missing(self):
        self.assertEqual(self.ok("hub", "nothing-here"), "")
        self.write("Projects/big-app/big-app.md", "# big-app\n\n" + "line of hub text\n" * 400)
        out = self.ok("hub", "Big_App")
        self.assertLess(len(out.encode()), 3700)
        self.assertIn("[truncated:", out)
        self.assertIn("jass show Projects/big-app/big-app.md", out)

    def test_zone_guard_refuses_recall_and_sync(self):
        config = self.home / ".config" / "jass" / "box.json"
        cfg = json.loads(config.read_text())
        config.write_text(json.dumps(dict(cfg, zone="work")))
        for argv in (["recall", "deploys", "--no-sync"], ["sync"], ["log", "-t", "x"]):
            code, _, err = self.jass(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("is zone 'personal'; this box is zone 'work'", err)

    def test_budget_bounds_the_whole_output(self):
        for i in range(8):
            self.write(f"note{i}.md", "# Recall fixture\n" + "budgetterm relevant context details " * 12)
        for budget in range(1, 400, 7):
            out = self.ok("recall", "budgetterm", "--no-sync", "--budget", str(budget))
            shown = int(out.split()[0])
            self.assertGreaterEqual(shown, 1)
            if shown > 1:
                self.assertLessEqual(len(out.encode()), budget * 4, out)
            tokens = int(out.splitlines()[0].split("~")[1].split()[0])
            self.assertEqual(tokens, -(-len(out.encode()) // 4))
