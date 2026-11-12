import datetime as dt
import json

from jashelp import Sandbox

from jas import brain
from jas.sessions import SessionRecord, digest, export, scrub

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
        self.brain = self.new_box(sources=[f"json:{self.store}"])

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
        for path in (self.brain / "Sessions").rglob("*.md"):
            data = path.read_bytes()
            for secret in planted:
                self.assertNotIn(secret.encode(), data, path.name)

    def test_digest_from_fixture_store(self):
        rows = [
            record("top", pr_urls=["https://example.invalid/pr/1"], environment_id="env-1", native_ref="claude:abc"),
            record("child", parent_id="top"),
            record("active", updated_minutes_ago=5),
            record("ignored", project="private-thing"),
        ]
        cfg_path = self.home / ".config" / "just-a-stack" / "box.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["sessions"]["exclude_projects"] = ["Private-Thing"]
        cfg_path.write_text(json.dumps(cfg))
        result = self.export(rows)
        self.assertEqual(len(result["written"]), 1)
        self.assertEqual(result["skipped"], 3)
        rel = result["written"][0]
        self.assertRegex(rel, r"^Sessions/mac/\d{4}-\d{2}/\d{4}-\d{2}-\d{2}-example-app-[0-9a-f]{8}\.md$")
        text = (self.brain / rel).read_text()
        meta, _ = brain.frontmatter(text)
        self.assertEqual((meta["kind"], meta["id"], meta["box"], meta["zone"], meta["subagents"]), ("session", "top", "mac", "personal", "1"))
        self.assertEqual(meta["tokens"], "{input: 1000, cached: 200, output: 300}")
        self.assertEqual(meta["t3_environment_id"], "env-1")
        self.assertIn("## Asked\n\n- Fix the deploy hook\n", text)
        self.assertIn("## Outcome\n\nFixed the deploy hook and added a test.\n", text)
        self.assertIn("## Turns\n\n1. Looked around.\n", text)
        listed = self.ok("sessions", "list")
        self.assertIn("mac  example-app  Session top  2 turns  1500 tokens", listed)

    def test_unchanged_sessions_are_skipped_and_continuations_overwrite(self):
        first = self.export([record("s1", updated_minutes_ago=200)])
        self.assertEqual(self.export([record("s1", updated_minutes_ago=200)])["written"], [])
        state = export.state_path(self.brain)
        state.write_text(json.dumps(dict(json.loads(state.read_text()), horizon=stamp(90))))
        continued = record("s1", updated_minutes_ago=60, turn_finals=["a", "b", "Continued and finished."])
        second = self.export([continued])
        self.assertEqual(second["written"], first["written"])
        self.assertIn("Continued and finished.", (self.brain / first["written"][0]).read_text())
        self.assertEqual(len(list((self.brain / "Sessions").rglob("*.md"))), 1)

    def test_dry_run_writes_nothing(self):
        result = self.export([record("s1")], "--dry-run")
        self.assertEqual(len(result["written"]), 1)
        self.assertFalse((self.brain / "Sessions").exists())

    def test_unknown_schema_and_missing_store_warn_without_failing(self):
        self.store.write_text(json.dumps({"threads": []}))
        code, _, err = self.jas("sessions", "export")
        self.assertEqual(code, 0)
        self.assertEqual(err.count("warning: json source disabled: unrecognised schema"), 1)
        self.store.unlink()
        code, _, err = self.jas("sync")
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

    def test_secrets_never_reach_the_brain(self):
        self.export([record("s1", prompts=["use token=supersecretvalue please"])])
        text = next((self.brain / "Sessions").rglob("*.md")).read_text()
        self.assertNotIn("supersecretvalue", text)

    def test_stub_sources_report_only_when_listed(self):
        from jas.sessions import sources

        for source in sources({"sources": ["claude", "codex", "nope"]}):
            ok, reason = source.available()
            self.assertFalse(ok)
            self.assertTrue(reason)
            self.assertEqual(list(source.records()), [])
        self.store.write_text("[]")
        _, _, err = self.jas("sessions", "export")
        self.assertNotIn("claude", err)
        self.assertNotIn("codex", err)

    def test_list_shows_peak_context_and_subagents(self):
        self.export([record("s1", peak_context=250000, subagent_count=4)])
        self.assertIn("1500 tokens  peak 250K  4 subagents", self.ok("sessions", "list"))

    def test_assistant_markdown_cannot_open_digest_sections(self):
        rec = SessionRecord(
            id="x", title="T", project="p", cwd="", started=NOW, updated=NOW,
            prompts=["Run this:\n```sh\nmake " + "x" * 700],
            turn_finals=["first", "## Question for you\nShould I?\n```\n# not a heading\n" + "y" * 3000],
        )
        text = digest.render(rec, "mac", "personal")
        sections = [t for _, level, t in brain.headings(text.split("\n")) if level == 2]
        self.assertEqual(sections, ["Asked", "Outcome", "Turns"])
        self.assertIn("**Question for you**\nShould I?", text)
