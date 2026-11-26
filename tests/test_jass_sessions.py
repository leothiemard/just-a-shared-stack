import datetime as dt
import json

from jasshelp import Sandbox, disk_full

from jass import lore
from jass.sessions import SessionRecord, digest, export, scrub

NOW = dt.datetime.now(dt.timezone.utc)


def stamp(minutes_ago: float) -> str:
    return (NOW - dt.timedelta(minutes=minutes_ago)).isoformat()


def record(id_: str, updated_minutes_ago: float = 120, **extra) -> dict:
    row = {
        "id": id_, "title": f"Session {id_}", "project": "example-app", "cwd": "/work/example-app",
        "started": stamp(600), "updated": stamp(updated_minutes_ago), "provider": "claude", "model": "model-a",
        "prompts": ["Fix the deploy hook"], "turn_finals": ["Looked around.", "Fixed the deploy hook and added a test."],
        "tokens": {"input": 1000, "cached": 200, "output": 300},
    }
    row.update(extra)
    return row


class SessionsTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.store = self.base / "sessions.json"
        self.lore = self.new_box(sources=[f"json:{self.store}"])

    def export(self, rows, *args):
        self.store.write_text(json.dumps(rows))
        return json.loads(self.ok("sessions", "export", "--json", *args))

    def test_no_planted_secret_reaches_a_written_digest(self):
        planted = ["PLANTED_A1", "PLANTED_B2", "PLANTED_C3", "PLANTED_D4", "dXNlcjpwYXNz", "PLANTED_E5", "PLANTED_F6", "PLANTED_G7"]
        prompts = [
            'Set password="line1\nPLANTED_A1" in the config',
            'It still has token = "PLANTED_B2\nand the next line',
            'curl -H "Authorization: Basic dXNlcjpwYXNz" https://example.invalid',
        ]
        finals = [
            'The payload was {\\"password\\": \\"PLANTED_C3\\"}.',
            "Exported secret='it\\'s PLANTED_D4' to the shell.",
            "Proxy-Authorization: Negotiate PLANTED_E5",
            '"client_secret": "PLANTED_F6"',
            "Done; api_key=PLANTED_G7 is rotated.",
        ]
        result = self.export([record("leaky", title="Rotate token=PLANTED_G7", prompts=prompts, turn_finals=finals)])
        self.assertEqual(len(result["written"]), 1)
        for path in (self.lore / "Sessions").rglob("*.md"):
            data = path.read_bytes()
            for secret in planted:
                self.assertNotIn(secret.encode(), data, path.name)

    def test_a_failed_write_leaves_the_digest_and_export_state_whole(self):
        rel = self.export([record("s1")])["written"][0]
        path = self.lore / rel
        state = next((self.home / ".cache" / "jass").glob("sessions-*.json"))
        before = path.read_text(), state.read_text()
        self.store.write_text(json.dumps([record("s1", updated_minutes_ago=100, turn_finals=["A continuation."])]))
        with disk_full(path.parent), self.assertRaises(OSError):
            self.jass("sessions", "export", "--all")
        self.assertEqual(path.read_text(), before[0])
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])
        with disk_full(state.parent), self.assertRaises(OSError):
            self.jass("sessions", "export", "--all")
        self.assertEqual(json.loads(state.read_text())["sessions"], json.loads(before[1])["sessions"])

    def test_digest_from_fixture_store(self):
        rows = [
            record("top", pr_urls=["https://example.invalid/pr/1"], environment_id="env-1", native_ref="claude:abc"),
            record("child", parent_id="top"),
            record("active", updated_minutes_ago=5),
            record("ignored", project="private-thing"),
        ]
        cfg_path = self.home / ".config" / "jass" / "box.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["sessions"]["exclude_projects"] = ["Private-Thing"]
        cfg_path.write_text(json.dumps(cfg))
        result = self.export(rows)
        self.assertEqual(len(result["written"]), 1)
        self.assertEqual(result["skipped"], 3)
        rel = result["written"][0]
        self.assertRegex(rel, r"^Sessions/mac/\d{4}-\d{2}/\d{4}-\d{2}-\d{2}-example-app-[0-9a-f]{8}\.md$")
        text = (self.lore / rel).read_text()
        meta, _ = lore.frontmatter(text)
        self.assertEqual((meta["kind"], meta["id"], meta["box"], meta["zone"], meta["subagents"]), ("session", "top", "mac", "personal", "1"))
        self.assertEqual(meta["tokens"], "{input: 1000, cached: 200, output: 300}")
        self.assertEqual(meta["t3_environment_id"], "env-1")
        self.assertIn("## Asked\n\n- Fix the deploy hook\n", text)
        self.assertIn("## Outcome\n\nFixed the deploy hook and added a test.\n", text)
        self.assertIn("## Turns\n\n1. Looked around.\n", text)
        listed = self.ok("sessions", "list")
        self.assertIn("mac  example-app  Session top  2 turns  1500 tokens", listed)

    def configure(self, **sessions):
        cfg_path = self.home / ".config" / "jass" / "box.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["sessions"].update(sessions)
        cfg_path.write_text(json.dumps(cfg))

    def test_delegated_steps_and_configured_patterns_are_left_out(self):
        self.configure(exclude_title_patterns=["^smoke test"], exclude_prompt_prefixes=["[worker]"])
        rows = [
            record("kept"),
            record("step", delegated_from="kept"),
            record("probe", title="Smoke Test: approval"),
            record("worker", first_prompt="[worker] Delegated worker for a coordinator thread"),
            record("lookalike", first_prompt="Explain the [worker] prefix"),
        ]
        result = self.export(rows)
        self.assertEqual((len(result["written"]), result["skipped"]), (2, 3))

    def test_prune_deletes_only_this_boxs_digests_of_sessions_now_left_out(self):
        rows = [record("kept"), record("step"), record("private", project="private-thing")]
        self.assertEqual(len(self.export(rows)["written"]), 3)
        other = self.lore / "Sessions" / "other-box" / "2026-01" / "step.md"
        other.parent.mkdir(parents=True)
        other.write_text('---\nkind: "session"\nid: "step"\n---\n# From another box\n')
        self.configure(exclude_projects=["private-thing"])
        rows[1]["delegated_from"] = "kept"
        self.assertEqual(self.export(rows)["pruned"], [])  # an ordinary run never deletes
        self.assertEqual(len(list((self.lore / "Sessions" / "mac").rglob("*.md"))), 3)
        preview = self.export(rows, "--prune", "--dry-run")
        self.assertEqual(len(preview["pruned"]), 2)
        self.assertEqual(len(list((self.lore / "Sessions" / "mac").rglob("*.md"))), 3)
        done = self.export(rows, "--prune")
        self.assertEqual(sorted(done["pruned"]), sorted(preview["pruned"]))
        left = list((self.lore / "Sessions" / "mac").rglob("*.md"))
        self.assertEqual(len(left), 1)
        self.assertEqual(lore.frontmatter(left[0].read_text())[0]["id"], "kept")
        self.assertTrue(other.exists())

    def test_an_id_listed_in_the_lore_is_never_exported_and_a_prune_restores_nothing(self):
        rows = [record("kept"), record("removed"), record("gone")]
        written = self.export(rows)["written"]
        self.assertEqual(len(written), 3)
        (self.lore / "Sessions" / "excluded-ids.txt").write_text("# removed by hand\nremoved  # a personal thread\n")
        gone = next(self.lore / rel for rel in written if lore.frontmatter((self.lore / rel).read_text())[0]["id"] == "gone")
        gone.unlink()  # deleted by hand without listing it: only `--all` brings it back
        done = self.export(rows, "--prune")
        self.assertEqual((len(done["pruned"]), done["written"]), (1, []))
        self.assertFalse(gone.exists())
        again = self.export(rows, "--all")
        ids = {lore.frontmatter(p.read_text())[0]["id"] for p in (self.lore / "Sessions" / "mac").rglob("*.md")}
        self.assertEqual((ids, again["skipped"]), ({"kept", "gone"}, 1))

    def test_a_session_is_filed_under_the_project_its_name_is_an_alias_of(self):
        self.ok("log", "-t", "First", "-p", "example-app")
        hub = self.lore / "Projects" / "example-app" / "example-app.md"
        hub.write_text(hub.read_text().replace("---\nproject:", "---\naliases: [renamed-checkout]\nproject:", 1))
        rel = self.export([record("s1", project="renamed-checkout")])["written"][0]
        self.assertIn("-example-app-", rel)
        self.assertEqual(lore.frontmatter((self.lore / rel).read_text())[0]["project"], "example-app")

    def test_the_outcome_is_the_last_reply_of_substance(self):
        result = "Root cause: the hook read a stale variable. " + "Detail. " * 80
        rec = SessionRecord(
            id="x", title="T", project="p", cwd="", started=NOW, updated=NOW,
            prompts=["Fix it", "/compact", "Continue where you left off.", "Fix it", "Now ship it"],
            turn_finals=["Looked around.", result, "Waiting on your review."],
            pr_urls=[f"https://example.invalid/pr/{n}" for n in range(40)],
        )
        text = digest.render(rec, "mac", "personal")
        self.assertIn(f"# T\n\nState as of {NOW.date().isoformat()}. Check before relying on it.\n", text)
        self.assertIn("## Asked\n\n- Fix it\n- Now ship it\n", text)
        self.assertIn("## Outcome\n\nRoot cause: the hook read a stale variable.", text)
        self.assertIn("\n\nLast reply: Waiting on your review.\n", text)
        self.assertIn("## Turns\n\n1. Looked around.\n", text)
        self.assertEqual(text.count("example.invalid/pr/"), digest.MAX_PRS)
        self.assertIn("example.invalid/pr/39", text)
        short = SessionRecord(id="y", title="T", project="p", cwd="", started=NOW, updated=NOW, turn_finals=["Looked.", "Done."])
        self.assertIn("## Outcome\n\nDone.\n\n## Turns\n\n1. Looked.\n", digest.render(short, "mac", "personal"))

    def test_delegated_titles_go_before_turns_when_a_digest_is_too_long(self):
        rec = SessionRecord(
            id="x", title="T", project="p", cwd="", started=NOW, updated=NOW, prompts=["Do it"],
            turn_finals=[f"Turn {n} " + "t" * 190 for n in range(12)] + ["Result. " + "r" * 2400],
            delegated=[{"title": "Child " + "c" * 100, "model": "m", "status": "completed"}] * 12,
        )
        text = digest.render(rec, "mac", "personal")
        self.assertLessEqual(len(text.encode()), digest.MAX_BYTES)
        self.assertNotIn("## Delegated", text)
        self.assertIn("12 delegated task(s).", text)
        self.assertIn("12. Turn 11", text)

    def test_unchanged_sessions_are_skipped_and_continuations_overwrite(self):
        first = self.export([record("s1", updated_minutes_ago=200)])
        self.assertEqual(self.export([record("s1", updated_minutes_ago=200)])["written"], [])
        state = export.state_path(self.lore)
        state.write_text(json.dumps(dict(json.loads(state.read_text()), horizon=stamp(90))))
        continued = record("s1", updated_minutes_ago=60, turn_finals=["a", "b", "Continued and finished."])
        second = self.export([continued])
        self.assertEqual(second["written"], first["written"])
        self.assertIn("Continued and finished.", (self.lore / first["written"][0]).read_text())
        self.assertEqual(len(list((self.lore / "Sessions").rglob("*.md"))), 1)

    def test_sync_before_stop_exports_sessions_that_just_finished(self):
        self.store.write_text(json.dumps([record("s1", updated_minutes_ago=4)]))
        self.assertEqual(self.export([record("s1", updated_minutes_ago=4)])["written"], [])
        self.ok("sync", "--before-stop")
        digests = list((self.lore / "Sessions" / "mac").rglob("*.md"))
        self.assertEqual(len(digests), 1)
        self.assertIn("Fixed the deploy hook", digests[0].read_text())
        self.assertEqual(lore.git(self.lore, "status", "--porcelain").stdout.strip(), "")  # committed
        # The next ordinary export, after the box wakes, rewrites it only once the session changed again.
        self.assertEqual(self.export([record("s1", updated_minutes_ago=4)])["written"], [])
        continued = record("s1", updated_minutes_ago=0.1, turn_finals=["a", "Continued after the wake."])
        self.store.write_text(json.dumps([continued]))
        self.ok("sync", "--before-stop")
        self.assertIn("Continued after the wake.", digests[0].read_text())

    def test_sync_before_stop_waits_for_another_sync_instead_of_skipping(self):
        import threading

        from jass import sync

        self.store.write_text(json.dumps([record("s1", updated_minutes_ago=4)]))
        held = sync.lock(self.lore)
        self.assertTrue(held.__enter__())
        try:
            self.assertIn("skipped", self.ok("sync"))
        finally:
            threading.Timer(0.5, held.__exit__, (None, None, None)).start()
        self.ok("sync", "--before-stop")
        self.assertEqual(len(list((self.lore / "Sessions" / "mac").rglob("*.md"))), 1)

    def test_sync_before_stop_undoes_a_rebase_a_killed_sync_left(self):
        def git(*args):
            return lore.git(self.lore, *args, check=False)

        note = self.lore / "note.md"
        git("switch", "-q", "-c", "other")
        note.write_text("other\n")
        git("add", "note.md")
        git("commit", "-q", "-m", "other")
        git("switch", "-q", "-")
        note.write_text("mine\n")
        git("add", "note.md")
        git("commit", "-q", "-m", "mine")
        mine = git("rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(git("rebase", "other").returncode, 0)  # stops on the conflict, as a killed sync would leave it
        self.store.write_text(json.dumps([record("s1", updated_minutes_ago=4)]))
        self.assertEqual(self.jass("sync")[0], 1)  # an ordinary sync refuses to commit over it
        out = self.ok("sync", "--before-stop")
        self.assertIn("Aborted a rebase", out)
        self.assertEqual(git("rev-parse", "HEAD~1").stdout.strip(), mine)  # local commit kept, export committed on top
        self.assertEqual(len(list((self.lore / "Sessions" / "mac").rglob("*.md"))), 1)

    def test_dry_run_writes_nothing(self):
        result = self.export([record("s1")], "--dry-run")
        self.assertEqual(len(result["written"]), 1)
        self.assertFalse((self.lore / "Sessions").exists())

    def test_unknown_schema_and_missing_store_warn_without_failing(self):
        self.store.write_text(json.dumps({"threads": []}))
        code, _, err = self.jass("sessions", "export")
        self.assertEqual(code, 0)
        self.assertEqual(err.count("warning: json source disabled: unrecognised schema"), 1)
        self.store.unlink()
        code, _, err = self.jass("sync")
        self.assertEqual(code, 0)
        self.assertIn("json source disabled: no file", err)

    def test_digest_limits(self):
        rec = SessionRecord(
            id="x", title="Long", project="p", cwd="", started=NOW, updated=NOW,
            prompts=[f"prompt {n} " + "p" * 900 for n in range(10)],
            turn_finals=[f"turn {n} " + "t" * 400 for n in range(30)] + ["final " + "f" * 4000],
            delegated=[{"title": f"child {n}", "model": "m", "status": "done"} for n in range(20)],
        )
        text = digest.render(rec, "mac", "personal")
        self.assertLessEqual(len(text.encode()), digest.MAX_BYTES)
        self.assertIn("- … 4 more …", text)
        self.assertIn("prompt 0", text)
        self.assertIn("prompt 9", text)
        self.assertNotIn("prompt 5", text)
        self.assertIn("earlier turns …", text)
        self.assertIn("final ", text)

    def test_scrubber(self):
        cases = {
            "key sk-ant-abcdefghijklmnopqrstuvwxyz used": "key [redacted] used",
            "ghp_" + "a" * 36: "[redacted]",
            "github_pat_" + "B" * 40: "[redacted]",
            "xoxb-1234567890-abcdef": "[redacted]",
            "AKIAABCDEFGHIJKLMNOP": "[redacted]",
            "Authorization: Bearer abc.def-123456": "Authorization: [redacted]",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----": "[redacted]",
            "export API_KEY=abc123 next": "export API_KEY=[redacted] next",
            "token=xyz password=hunter2 secret=s": "token=[redacted] password=[redacted] secret=[redacted]",
            "https://user:pa55word@example.invalid/repo": "https://user:[redacted]@example.invalid/repo",
            "plain text about tokens and keys": "plain text about tokens and keys",
        }
        for raw, clean in cases.items():
            self.assertEqual(scrub.text(raw), clean)
        rec = scrub.record(SessionRecord(id="1", title="t", project="p", cwd="", started=NOW, updated=NOW, prompts=["password=abc"], tokens={"input": 3}))
        self.assertEqual(rec.prompts, ["password=[redacted]"])
        self.assertEqual(rec.tokens, {"input": 3})

    def test_secrets_never_reach_the_lore(self):
        self.export([record("s1", prompts=["use token=supersecretvalue please"])])
        text = next((self.lore / "Sessions").rglob("*.md")).read_text()
        self.assertNotIn("supersecretvalue", text)

    def test_stub_sources_report_only_when_listed(self):
        from jass.sessions import sources

        for source in sources({"sources": ["claude", "codex", "nope"]}):
            ok, reason = source.available()
            self.assertFalse(ok)
            self.assertTrue(reason)
            self.assertEqual(list(source.records()), [])
        self.store.write_text("[]")
        _, _, err = self.jass("sessions", "export")
        self.assertNotIn("claude", err)
        self.assertNotIn("codex", err)

    def test_list_shows_peak_context_and_subagents(self):
        self.export([record("s1", peak_context=250000, subagent_count=4)])
        self.assertIn("1500 tokens  peak 250K  4 subagents", self.ok("sessions", "list"))

    def test_a_shorter_fence_line_does_not_end_a_longer_fence_in_a_digest(self):
        rec = SessionRecord(
            id="x", title="T", project="p", cwd="", started=NOW, updated=NOW, prompts=["Go"],
            turn_finals=["first", "````md\n```\n## inside\n````\n## outside\n````\nstill open"],
        )
        text = digest.render(rec, "mac", "personal")
        sections = [t for _, level, t in lore.headings(text.split("\n")) if level == 2]
        self.assertEqual(sections, ["Asked", "Outcome", "Turns"])
        self.assertIn("**outside**", text)
        self.assertIn("````md\n```\n## inside\n````", text)

    def test_assistant_markdown_cannot_open_digest_sections(self):
        rec = SessionRecord(
            id="x", title="T", project="p", cwd="", started=NOW, updated=NOW,
            prompts=["Run this:\n```sh\nmake " + "x" * 700],
            turn_finals=["first", "## Question for you\nShould I?\n```\n# not a heading\n" + "y" * 3000],
        )
        text = digest.render(rec, "mac", "personal")
        sections = [t for _, level, t in lore.headings(text.split("\n")) if level == 2]
        self.assertEqual(sections, ["Asked", "Outcome", "Turns"])
        self.assertIn("**Question for you**\nShould I?", text)
