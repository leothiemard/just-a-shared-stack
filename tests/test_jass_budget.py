import json
import re

from jasshelp import Sandbox, git

from jass import budget


def skill(root, name, description):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\nbody\n")


class BudgetTests(Sandbox):
    def test_counts_from_fixture_home(self):
        agents = self.home / ".agents"
        agents.mkdir()
        (agents / "AGENTS.md").write_text("x" * 2500)
        claude = self.home / ".claude"
        claude.mkdir()
        (claude / "CLAUDE.md").symlink_to(agents / "AGENTS.md")
        skill(agents / "skills", "alpha", "a" * 10)
        skill(agents / "skills", "beta", "b" * 600)
        folded = claude / "skills" / "gamma"
        folded.mkdir(parents=True)
        (folded / "SKILL.md").write_text("---\nname: gamma\ndescription: >\n  one two\n  three\n---\n")
        plugin = self.base / "plugin-cache" / "tool"
        skill(plugin / "skills", "p1", "p" * 20)
        (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"tool@market": True, "off@market": False}}))
        (claude / "plugins").mkdir()
        (claude / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": {"tool@market": [{"installPath": str(plugin)}]}}))
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"docs": {}, "search": {}}}))
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "config.toml").write_text('[mcp_servers.linear]\ncommand = "x"\n[mcp_servers."web"]\n[other]\n')

        report = budget.measure()
        rows = {(i["group"], i["name"]): (i["count"], i["bytes"]) for i in report["items"]}
        self.assertEqual(rows[("global file", "~/.agents/AGENTS.md")], (1, 2500))
        self.assertNotIn(("global file", "~/.claude/CLAUDE.md"), rows)
        self.assertEqual(rows[("skill root", "~/.agents/skills")], (2, 5 + 10 + 4 + 600))
        self.assertEqual(rows[("skill root", "~/.claude/skills")], (1, 5 + len("one two") + 1 + len("three") + 1))
        self.assertEqual(rows[("plugin", "tool@market")], (1, 2 + 20))
        self.assertEqual(report["total"], sum(b for _, b in rows.values()))
        self.assertEqual(report["mcp"], {"claude": ["docs", "search"], "codex": ["linear", "web"]})
        self.assertEqual(len(report["warnings"]), 2)
        self.assertIn("total always loaded", budget.table(report))

    def test_project_rows_for_the_current_repo(self):
        repo = self.base / "example-app"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "AGENTS.md").write_text("a" * 9000)
        (repo / "CLAUDE.md").symlink_to(repo / "AGENTS.md")
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(repo.resolve()))
        memory = self.home / ".claude" / "projects" / slug / "memory" / "MEMORY.md"
        memory.parent.mkdir(parents=True)
        memory.write_text("m" * 100)
        report = budget.measure(repo)
        rows = {i["name"]: i["bytes"] for i in report["items"] if i["group"] == "project"}
        self.assertEqual(rows, {"example-app/AGENTS.md": 9000, f"auto-memory ~/.claude/projects/{slug}/memory/MEMORY.md": 100})
        self.assertEqual(report["with_project"], report["total"] + 9100)
        self.assertEqual(sum("example-app/AGENTS.md is 9000 bytes" in w for w in report["warnings"]), 1)
        self.assertIn("total in this project", budget.table(report))
        self.assertIsNone(budget.measure(self.base)["with_project"])
