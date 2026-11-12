import json
import os
import re
from pathlib import Path

from jashelp import Sandbox, disk_full, git, tree

from jas import rules

INIT = ["init", "--box", "mac", "--zone", "personal", "--new-brain"]


class InitTests(Sandbox):
    def manifest(self, out: str) -> str:
        return re.search(r"^Restore manifest: (.+)$", out, re.M).group(1)

    def test_preview_changes_nothing(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "CLAUDE.md").write_text("mine\n")
        before = tree(self.home)
        out = self.ok(*INIT)
        self.assertIn("Preview only", out)
        self.assertIn("add      brain", out)
        self.assertEqual(tree(self.home), before)

    def test_apply_is_idempotent(self):
        self.ok(*INIT, "--apply")
        self.assertTrue((self.home / ".agents" / "skills" / "brain").is_symlink())
        self.assertTrue((self.home / ".claude" / "skills" / "brain").is_symlink())
        self.assertTrue((self.home / ".local" / "bin" / "jas").is_symlink())
        self.assertTrue((self.home / "brain" / "brain.json").is_file())
        before = tree(self.home)
        out = self.ok(*INIT, "--apply")
        self.assertIn("Nothing to do.", out)
        self.assertNotIn("Restore manifest", out)
        self.assertEqual(tree(self.home), before)

    def test_restore_returns_previous_state(self):
        claude = self.home / ".claude"
        claude.mkdir()
        (claude / "CLAUDE.md").write_text("# Box-local rules\n\nKeep me.\n")
        (claude / "settings.json").write_text(json.dumps({"model": "x", "env": {"A": "1"}}))
        before = tree(self.home)
        out = self.ok(*INIT, "--apply")
        settings = json.loads((claude / "settings.json").read_text())
        self.assertEqual(settings["attribution"], {"commit": "", "pr": ""})
        self.assertEqual(settings["env"], {"A": "1"})
        self.ok("restore", self.manifest(out))
        after = tree(self.home)
        for rel in list(after):
            if rel.startswith(("brain", ".config/just-a-stack/backups", ".agents/skill-backups", ".claude/skill-backups")):
                after.pop(rel)
        self.assertEqual(after, {**before, ".config": "/", ".config/just-a-stack": "/", ".agents": "/"})

    def test_restore_refuses_when_a_file_changed_since(self):
        out = self.ok(*INIT, "--apply")
        canonical = self.home / ".agents" / "AGENTS.md"
        canonical.write_text(canonical.read_text() + "edited later\n")
        code, _, err = self.jas("restore", self.manifest(out))
        self.assertEqual(code, 1)
        self.assertIn("changed since this transaction", err)
        self.assertTrue((self.home / ".claude" / "CLAUDE.md").is_symlink())

    def test_block_replaced_in_place_and_outside_text_kept_byte_for_byte(self):
        canonical = self.home / ".agents" / "AGENTS.md"
        canonical.parent.mkdir(parents=True)
        head, tail = "# Local head\r\nline  \n\n", "\n\ntail without newline"
        canonical.write_bytes((head + rules.BEGIN + "\nold rules\n" + rules.END + tail).encode())
        self.ok(*INIT, "--apply")
        text = canonical.read_bytes().decode()
        self.assertTrue(text.startswith(head))
        self.assertTrue(text.endswith(tail))
        self.assertIn("Shared knowledge (brain)", text)
        self.assertNotIn("old rules", text)

    def test_block_inserted_at_top_when_missing(self):
        text = "existing\n\nnotes"
        new = rules.with_block(text, rules.BEGIN + "\nx\n" + rules.END, Path("f"))
        self.assertTrue(new.startswith(rules.BEGIN))
        self.assertTrue(new.endswith("\n" + text))

    def test_claude_and_codex_files(self):
        claude, codex = self.home / ".claude" / "CLAUDE.md", self.home / ".codex" / "AGENTS.md"
        claude.parent.mkdir()
        claude.write_text("my claude rules\n")
        self.ok(*INIT, "--apply")
        self.assertFalse(claude.is_symlink())
        self.assertTrue(claude.read_text().endswith("\nmy claude rules\n"))
        self.assertIn(rules.BEGIN, claude.read_text())
        self.assertTrue(codex.is_symlink())
        self.assertEqual(codex.resolve(), (self.home / ".agents" / "AGENTS.md").resolve())

    def test_existing_symlink_to_canonical_is_kept(self):
        canonical = self.home / ".agents" / "AGENTS.md"
        canonical.parent.mkdir(parents=True)
        canonical.write_text("")
        codex = self.home / ".codex" / "AGENTS.md"
        codex.parent.mkdir()
        codex.symlink_to(canonical)
        out = self.ok(*INIT)
        self.assertRegex(out, r"keep\s+rules\s+~/.codex/AGENTS.md")

    def test_duplicate_skill_across_layers_fails(self):
        layer = self.base / "company"
        (layer / "skills" / "brain").mkdir(parents=True)
        (layer / "skills" / "brain" / "SKILL.md").write_text("---\nname: brain\n---\n")
        (layer / "skills.json").write_text(json.dumps({"skills": ["brain"]}))
        code, _, err = self.jas(*INIT, "--layer", str(layer), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("Skill 'brain' is in both", err)
        self.assertFalse((self.home / ".agents").exists())

    def test_layer_rules_are_appended_to_the_block(self):
        layer = self.base / "company"
        (layer / "skills" / "work-flow").mkdir(parents=True)
        (layer / "skills" / "work-flow" / "SKILL.md").write_text("---\nname: work-flow\n---\n")
        (layer / "skills.json").write_text(json.dumps({"skills": ["work-flow"]}))
        (layer / "rules").mkdir()
        (layer / "rules" / "global.md").write_text("## Company rule\n")
        self.ok(*INIT, "--layer", str(layer), "--apply")
        text = (self.home / ".agents" / "AGENTS.md").read_text()
        self.assertLess(text.index("No agent attribution"), text.index("## Company rule"), text)
        self.assertTrue((self.home / ".agents" / "skills" / "work-flow").is_symlink())

    def test_conflicting_skill_needs_replace(self):
        existing = self.home / ".agents" / "skills" / "brain"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("someone else's")
        code, out, err = self.jas(*INIT, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("conflict", out)
        self.assertFalse((self.home / ".config" / "just-a-stack" / "box.json").exists())
        self.ok(*INIT, "--apply", "--replace")
        self.assertTrue(existing.is_symlink())

    def test_existing_brain_with_other_zone_is_refused(self):
        self.ok(*INIT, "--apply")
        code, _, err = self.jas("init", "--zone", "work")
        self.assertEqual(code, 1)
        self.assertIn("zone 'personal'", err)

    def test_import_notes_never_overwrites_or_touches_source(self):
        vault = self.base / "vault"
        (vault / "Projects" / "example-app").mkdir(parents=True)
        (vault / "Projects" / "example-app" / "example-app.md").write_text("# imported hub\n")
        (vault / "Projects" / "example-app" / ".hidden").write_text("x")
        (vault / "Inbox.md").write_text("loose note\n")
        (vault / "README.md").write_text("vault readme\n")
        before = tree(vault)
        self.ok(*INIT, "--import-notes", str(vault), "--apply")
        brain = self.home / "brain"
        self.assertEqual((brain / "Projects" / "example-app" / "example-app.md").read_text(), "# imported hub\n")
        self.assertEqual((brain / "Inbox.md").read_text(), "loose note\n")
        self.assertNotEqual((brain / "README.md").read_text(), "vault readme\n")
        self.assertFalse((brain / "Projects" / "example-app" / ".hidden").exists())
        self.assertEqual(tree(vault), before)
        self.assertEqual(git(brain, "status", "--porcelain"), "")

    def vault(self):
        vault = self.base / "vault"
        (vault / "Projects" / "example-app").mkdir(parents=True)
        (vault / "Projects" / "example-app" / "New.md").write_text("imported\n")
        (vault / "Inbox.md").write_text("loose note\n")
        return vault

    def test_import_aborts_when_a_destination_folder_leads_outside_the_brain(self):
        self.ok(*INIT, "--apply")
        vault, outside = self.vault(), self.base / "outside"
        outside.mkdir()
        brain = self.home / "brain"
        (brain / "Projects").mkdir()
        (brain / "Projects" / "example-app").symlink_to(outside, target_is_directory=True)
        before = tree(vault), tree(outside), git(brain, "rev-parse", "HEAD")
        code, _, err = self.jas(*INIT, "--import-notes", str(vault), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("outside the brain", err)
        self.assertEqual((tree(vault), tree(outside), git(brain, "rev-parse", "HEAD")), before)
        self.assertFalse((brain / "Inbox.md").exists())

    def test_import_skips_a_dangling_destination_link(self):
        self.ok(*INIT, "--apply")
        vault = self.vault()
        brain = self.home / "brain"
        (brain / "Inbox.md").symlink_to(vault / "missing.md")
        before = tree(vault)
        out = self.ok(*INIT, "--import-notes", str(vault), "--apply")
        self.assertIn("1 files to copy, 1 already present", out)
        self.assertEqual(tree(vault), before)
        self.assertEqual((brain / "Projects" / "example-app" / "New.md").read_text(), "imported\n")

    def test_failed_clone_changes_nothing_outside_the_brain(self):
        before = tree(self.home)
        code, _, err = self.jas("init", "--box", "mac", "--brain", str(self.base / "absent.git"), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("brain", err)
        self.assertNotIn("jas restore", err)
        self.assertEqual(tree(self.home), before)

    def test_restore_undoes_an_init_that_failed_midway(self):
        (self.home / ".codex").write_text("a file where a folder belongs\n")
        before = tree(self.home)
        code, _, err = self.jas(*INIT, "--apply")
        self.assertEqual(code, 1)
        self.assertTrue((self.home / ".agents" / "AGENTS.md").exists())
        manifest = re.search(r"jas restore (.+manifest\.json)", err).group(1)
        self.ok("restore", manifest)
        after = {rel: v for rel, v in tree(self.home).items() if not rel.startswith(("brain", ".config/just-a-stack/backups", ".agents/skill-backups", ".claude/skill-backups"))}
        self.assertEqual(after, {**before, ".config": "/", ".config/just-a-stack": "/", ".agents": "/", ".claude": "/"})

    def canonical_with(self, text):
        canonical = self.home / ".agents" / "AGENTS.md"
        canonical.parent.mkdir(parents=True)
        canonical.write_text(text)
        return canonical

    def test_restore_works_after_a_write_that_failed_partway(self):
        canonical = self.canonical_with("ORIGINAL\n")
        with disk_full(canonical.parent):
            code, _, err = self.jas(*INIT, "--apply")
        self.assertEqual(code, 1)
        self.assertEqual(canonical.read_text(), "ORIGINAL\n")
        self.assertEqual([p.name for p in canonical.parent.iterdir() if p.is_file()], ["AGENTS.md"])
        self.ok("restore", re.search(r"jas restore (.+manifest\.json)", err).group(1))
        self.assertEqual(canonical.read_text(), "ORIGINAL\n")

    def test_a_linked_rules_file_is_written_through_and_stays_a_link(self):
        real = self.base / "dotfiles" / "CLAUDE.md"
        real.parent.mkdir()
        real.write_text("# Mine\n")
        link = self.home / ".claude" / "CLAUDE.md"
        link.parent.mkdir()
        link.symlink_to(real)
        out = self.ok(*INIT, "--apply")
        self.assertTrue(link.is_symlink())
        self.assertIn(rules.BEGIN, real.read_text())
        self.assertTrue(real.read_text().endswith("# Mine\n"))
        self.ok("restore", self.manifest(out))
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(), "# Mine\n")

    def test_markers_in_a_code_fence_are_examples_not_the_block(self):
        example = "```md\n" + rules.BEGIN + "\nexample rules\n" + rules.END + "\n```\n"
        canonical = self.canonical_with("# Local\n\n" + example + "\nKeep this.\n" + rules.BEGIN + "\nold\n" + rules.END + "\ntail\n")
        self.ok(*INIT, "--apply")
        text = canonical.read_text()
        self.assertIn(example + "\nKeep this.\n" + rules.BEGIN + "\n", text)
        self.assertNotIn("\nold\n", text)
        self.assertTrue(text.endswith(rules.END + "\ntail\n"))

    def test_a_fence_closes_only_on_its_own_kind_and_length(self):
        for opener, inner in (("````md", "```"), ("```", "~~~")):
            with self.subTest(opener):
                example = f"{opener}\n{inner}\n" + rules.BEGIN + "\nKEEP\n" + rules.END + "\n" + opener[:4].rstrip("md") + "\n"
                canonical = self.home / ".agents" / "AGENTS.md"
                canonical.parent.mkdir(parents=True, exist_ok=True)
                canonical.write_text("# Local\n\n" + example)
                self.ok(*INIT, "--apply")
                text = canonical.read_text()
                self.assertIn(example, text)
                self.assertEqual(text.count(rules.BEGIN), 2)

    def test_a_fence_opened_in_a_list_item_holds_its_indented_examples(self):
        example = "- ```markdown\n  " + rules.BEGIN + "\n  example rules\n  " + rules.END + "\n  ```\n"
        canonical = self.canonical_with("# Local\n\n" + example + "\n" + rules.BEGIN + "\nold\n" + rules.END + "\ntail\n")
        self.ok(*INIT, "--apply")
        text = canonical.read_text()
        self.assertIn(example + "\n" + rules.BEGIN + "\n", text)
        self.assertNotIn("\nold\n", text)
        self.assertTrue(text.endswith(rules.END + "\ntail\n"))

    def test_indented_code_neither_opens_a_fence_nor_holds_the_block(self):
        canonical = self.home / ".agents" / "AGENTS.md"
        canonical.parent.mkdir(parents=True)
        for local in ("# Local\n\n    ```\n    indented code\n\n",
                      "# Local\n\n    " + rules.BEGIN + "\n    example\n    " + rules.END + "\n\n"):
            with self.subTest(local):
                canonical.write_text(local + rules.BEGIN + "\nold\n" + rules.END + "\ntail\n")
                self.ok(*INIT, "--apply")
                text = canonical.read_text()
                self.assertTrue(text.startswith(local + rules.BEGIN + "\n"), text)
                self.assertNotIn("\nold\n", text)

    def test_a_fence_in_a_nested_list_item_closes_on_its_own_closer(self):
        example = "1. Step\n   - Detail\n     ```\n     code\n     ```\n"
        canonical = self.canonical_with("# Local\n\n" + example + "\n" + rules.BEGIN + "\nold\n" + rules.END + "\ntail\n")
        self.ok(*INIT, "--apply")
        text = canonical.read_text()
        self.assertEqual(text.count(rules.BEGIN), 1, text)
        self.assertNotIn("\nold\n", text)

    def test_ambiguous_markers_are_a_conflict_and_the_file_is_kept(self):
        cases = {
            "only fenced markers": "```\n" + rules.BEGIN + "\n```\nlocal\n" + rules.END + "\n",
            "two ends": rules.BEGIN + "\nold\n" + rules.END + "\nlocal\n" + rules.END + "\n",
            "end before begin": rules.END + "\nlocal\n" + rules.BEGIN + "\n",
            "two blocks": (rules.BEGIN + "\nold\n" + rules.END + "\n") * 2,
        }
        canonical = self.canonical_with("")
        for name, text in cases.items():
            with self.subTest(name):
                canonical.write_text(text)
                code, out, err = self.jas(*INIT, "--apply")
                self.assertEqual(code, 1)
                self.assertRegex(out, r"conflict\s+rules\s+~/.agents/AGENTS.md\s+.*marker")
                self.assertEqual(canonical.read_text(), text)

    def test_existing_repo_without_brain_json_is_not_adopted(self):
        repo = self.base / "project"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "notes.md").write_text("tracked\n")
        git(repo, "add", "notes.md")
        git(repo, "commit", "-q", "-m", "start")
        (repo / ".env").write_text("UNRELATED=1\n")
        before = tree(repo)
        for extra in ([], ["--new-brain"]):
            with self.subTest(extra):
                code, out, err = self.jas("init", "--box", "mac", "--brain-path", str(repo), *extra, "--apply")
                self.assertEqual(code, 1)
                self.assertIn("brain.json", out + err)
                self.assertEqual(tree(repo), before)

    def test_cloned_repo_without_brain_json_is_refused(self):
        source = self.base / "project"
        source.mkdir()
        git(source, "init", "-q")
        (source / "notes.md").write_text("tracked\n")
        git(source, "add", "notes.md")
        git(source, "commit", "-q", "-m", "start")
        code, _, err = self.jas("init", "--box", "mac", "--brain", str(source), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("brain.json", err)
        self.assertNotIn("brain.json", git(source, "ls-tree", "-r", "--name-only", "HEAD"))
        self.assertFalse((self.home / "brain" / "brain.json").exists())

    def test_skeleton_fill_commits_only_the_skeleton(self):
        self.ok(*INIT, "--apply")
        brain = self.home / "brain"
        git(brain, "rm", "-q", "README.md")
        git(brain, "commit", "-q", "-m", "drop readme")
        (brain / "scratch.txt").write_text("not mine to commit\n")
        self.ok(*INIT, "--apply")
        self.assertIn("README.md", git(brain, "show", "--name-only", "--format=", "HEAD"))
        self.assertEqual(git(brain, "status", "--porcelain"), "?? scratch.txt\n")

    def test_import_skips_source_links_that_lead_outside_the_source(self):
        vault, outside = self.vault(), self.base / "outside"
        outside.mkdir()
        (outside / "private.md").write_text("private\n")
        (vault / "Projects" / "example-app" / "Linked.md").symlink_to(outside / "private.md")
        (vault / "Linked.md").symlink_to(outside / "private.md")
        self.ok(*INIT, "--import-notes", str(vault), "--apply")
        brain = self.home / "brain"
        self.assertTrue((brain / "Projects" / "example-app" / "New.md").exists())
        self.assertFalse(os.path.lexists(brain / "Projects" / "example-app" / "Linked.md"))
        self.assertFalse(os.path.lexists(brain / "Linked.md"))

    def test_skeleton_fill_never_writes_through_a_link(self):
        self.ok(*INIT, "--apply")
        brain, outside = self.home / "brain", self.base / "outside.md"
        git(brain, "rm", "-q", "README.md")
        git(brain, "commit", "-q", "-m", "drop readme")
        (brain / "README.md").symlink_to(outside)
        self.jas(*INIT, "--apply")
        self.assertFalse(outside.exists())
