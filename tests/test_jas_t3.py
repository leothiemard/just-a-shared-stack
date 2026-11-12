import datetime as dt
import hashlib
import json
from unittest import mock

import t3fixture
from jashelp import Sandbox

from jas import brain
from jas.sessions import export, t3


class T3SourceTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.store_path = self.base / "t3 data" / "statev2.sqlite"
        self.store_path.parent.mkdir()
        self.store = t3fixture.standard(self.store_path)
        self.brain = self.new_box(sources=["t3"])
        self.set_sessions(t3_store=str(self.store_path))

    def set_sessions(self, **values):
        path = self.home / ".config" / "just-a-stack" / "box.json"
        cfg = json.loads(path.read_text())
        cfg["sessions"].update(values)
        path.write_text(json.dumps(cfg))

    def rewind_horizon(self, minutes):
        """Pretend the last export ran earlier, as if time had passed since."""
        path = export.state_path(self.brain)
        state = json.loads(path.read_text())
        state["horizon"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)).isoformat()
        path.write_text(json.dumps(state))

    def export(self, *args):
        return json.loads(self.ok("sessions", "export", "--json", *args))

    def digests(self) -> dict:
        out = {}
        for path in (self.brain / "Sessions").rglob("*.md"):
            text = path.read_text()
            out[brain.frontmatter(text)[0]["id"]] = text
        return out

    def test_exports_finished_top_level_threads_only(self):
        result = self.export()
        self.assertEqual(set(self.digests()), {"top", "agent-launched", "secret", "long", "scratchy"})
        self.assertEqual(len(result["written"]), 5)
        self.assertEqual(result["skipped"], 1)  # the active thread; children, deleted and failed-only never leave the source

    def test_digest_fields_and_folded_children(self):
        self.export()
        text = self.digests()["top"]
        meta, _ = brain.frontmatter(text)
        self.assertEqual(meta["project"], "example-app")
        self.assertEqual((meta["turns"], meta["subagents"], meta["peak_context"]), ("3", "3", "230000"))
        self.assertEqual(meta["tokens"], "{input: 300, cached: 150, output: 30}")
        self.assertEqual(json.loads(meta["prs"]), ["https://example.invalid/pr/1", "https://example.invalid/pr/2"])
        self.assertEqual((meta["native_ref"], meta["t3_environment_id"]), ("claude:native-session-1", "env-fixture"))
        self.assertEqual((meta["branch"], meta["created_by"], meta["model"]), ("fix/deploy-hook", "user", "model-a"))
        self.assertIn("## Asked\n\n- Fix the deploy hook please\n- Ship it\n\n## Outcome\n\nFixed the deploy hook and opened PR 1.\n", text)
        self.assertNotIn("Child result", text)
        self.assertNotIn("checks passed", text)
        self.assertNotIn("Working on it.", text)
        self.assertIn("1. Looked at the hook; the env is missing.\n2. Child finished.\n", text)
        self.assertIn("## Delegated\n\n- Review the hook fix · model-b · completed\n", text)
        self.assertNotIn("Explore the repo", text)

    def test_agent_launched_thread_keeps_its_brief(self):
        self.export()
        text = self.digests()["agent-launched"]
        self.assertIn("- Brief from another thread: rename the event\n- Also check the archive\n", text)
        self.assertIn("created_by: \"agent\"", text)

    def test_caps_on_long_threads(self):
        self.export()
        text = self.digests()["long"]
        self.assertIn("- Prompt number 4\n- … 4 more …\n- Prompt number 14\n", text)
        self.assertIn("- … 7 earlier turns …\n8. Answer for turn 7\n", text)
        self.assertIn("## Outcome\n\nAnswer for turn 19\n", text)
        self.assertIn("turns: 20", text)

    def test_scratch_workspace_and_secrets(self):
        self.export()
        found = self.digests()
        self.assertEqual(brain.frontmatter(found["scratchy"])[0]["project"], "scratch")
        self.assertNotIn("sk-livesecretvalue", found["secret"])
        self.assertIn("API_KEY=[redacted]", found["secret"])

    def test_idle_filter_and_continuation_overwrite(self):
        self.export()
        self.rewind_horizon(120)
        self.store.touch("active", 60)
        self.store.turn("top", "One more thing", "Also fixed the docs.", at=50)
        self.store.touch("top", 45)
        self.assertEqual(len(self.export()["written"]), 2)
        self.assertIn("Also fixed the docs.", self.digests()["top"])
        self.assertIn("active", self.digests())
        self.assertEqual(len(list((self.brain / "Sessions").rglob("*.md"))), 6)

    def test_incremental_runs_load_only_changed_threads(self):
        self.export()

        def loaded(*args):
            with mock.patch.object(t3, "load", wraps=t3.load) as load:
                result = self.export(*args)
            return result, {call.args[1] for call in load.call_args_list}

        result, ids = loaded()
        self.assertEqual((result["written"], ids), ([], {"active"}))
        self.rewind_horizon(50)
        self.store.touch("secret", 40)
        result, ids = loaded()
        self.assertEqual((len(result["written"]), ids), (1, {"secret", "active"}))
        self.assertEqual(loaded("--all")[1], {"top", "agent-launched", "secret", "long", "scratchy", "active"})

    def test_store_is_never_written(self):
        before = (hashlib.sha256(self.store_path.read_bytes()).hexdigest(), self.store_path.stat().st_mtime_ns)
        self.export()
        self.ok("sync")
        after = (hashlib.sha256(self.store_path.read_bytes()).hexdigest(), self.store_path.stat().st_mtime_ns)
        self.assertEqual(before, after)
        self.assertEqual(sorted(p.name for p in self.store_path.parent.iterdir()), ["environment-id", "statev2.sqlite"])

    def test_schema_drift_disables_the_source_with_one_message(self):
        self.store.drop_column(f"{t3fixture.T}runs", "status")
        code, out, err = self.jas("sessions", "export")
        self.assertEqual(code, 0)
        self.assertEqual(err.count("t3 source disabled:"), 1, err)
        self.assertIn(f"column {t3fixture.T}runs.status missing", err)
        self.assertFalse((self.brain / "Sessions").exists())
        self.assertEqual(self.jas("sync")[0], 0)
        _, doctor, _ = self.jas("doctor")
        self.assertIn(f"t3 source disabled: schema changed", doctor)

    def test_a_disabled_source_does_not_advance_the_horizon(self):
        self.store_path.rename(self.base / "moved.sqlite")
        self.export()
        (self.base / "moved.sqlite").rename(self.store_path)
        self.assertEqual(len(self.export()["written"]), 5)

    def test_missing_store_and_default_path(self):
        self.set_sessions(t3_store="")
        source = t3.T3Source()
        self.assertEqual(source.store, self.home / ".t3" / "userdata" / "statev2.sqlite")
        ok, reason = source.available()
        self.assertFalse(ok)
        self.assertIn("no store at ~/.t3/userdata/statev2.sqlite", reason)

    def test_projects_for_the_box_card(self):
        self.assertEqual(t3.T3Source(str(self.store_path)).projects(),
                         [("Example App", "/work/example-app"), ("No project", "/home/someone/.t3/scratch")])
        self.ok("sync")
        card = (self.brain / "Boxes" / "mac.md").read_text()
        self.assertIn("| Example App | /work/example-app |", card)
        self.assertNotIn("old-thing", card)

    def test_sessions_stats(self):
        self.export()
        groups = json.loads(self.ok("sessions", "stats", "--by", "project", "--json"))
        app = next(g for g in groups if g["group"] == "example-app")
        self.assertEqual((app["sessions"], app["turns"], app["input"], app["subagents"], app["long"], app["long_share"]), (4, 26, 2600, 3, 1, 0.25))
        table = self.ok("sessions", "stats", "--by", "week")
        self.assertRegex(table.splitlines()[1], r"^\d{4}-W\d{2} ")
        self.assertRegex(table.splitlines()[-1], r"^total\s+5\s")
        self.assertIn(">200K", table.splitlines()[0])
