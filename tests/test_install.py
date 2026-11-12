import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install.py"
spec = importlib.util.spec_from_file_location("install", SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skill install ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "source repo"
        self.repo.mkdir()
        (self.repo / "skills.json").write_text(json.dumps({"skills": ["alpha", "beta"]}))
        for name in ("alpha", "beta"):
            folder = self.repo / "skills" / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(name)
        self.root = self.base / "agent root" / "skills"

    def test_default_is_read_only(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Preview only", result.stdout)
        self.assertFalse(self.root.parent.exists())

    def test_default_root_follows_jas_home(self):
        env = dict(os.environ, JAS_HOME=str(self.base / "sandbox home"))
        result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.base / "sandbox home" / ".agents" / "skills"), result.stdout)

    def test_conflict_preflight_prevents_partial_install(self):
        self.root.mkdir(parents=True)
        (self.root / "beta").mkdir()
        with self.assertRaises(ValueError):
            installer.install(self.repo, self.root)
        self.assertFalse((self.root / "alpha").exists())
        self.assertFalse((self.root.parent / "skill-backups").exists())

    def test_replacement_idempotency_and_restore_preserve_other_skills(self):
        self.root.mkdir(parents=True)
        original = self.root / "alpha"
        original.mkdir()
        (original / "personal.txt").write_text("keep me")
        work = self.root / "company-workflow"
        work.mkdir()
        (work / "SKILL.md").write_text("local only")
        manifest = installer.install(self.repo, self.root, replace=True)
        self.assertTrue(original.is_symlink())
        self.assertEqual((original / "SKILL.md").read_text(), "alpha")
        self.assertIsNone(installer.install(self.repo, self.root, replace=True))
        installer.restore(manifest)
        self.assertFalse(original.is_symlink())
        self.assertEqual((original / "personal.txt").read_text(), "keep me")
        self.assertFalse((self.root / "beta").exists())
        self.assertEqual((work / "SKILL.md").read_text(), "local only")

    def test_restore_refuses_changed_link_without_partial_changes(self):
        manifest = installer.install(self.repo, self.root)
        target = self.root / "alpha"
        target.unlink()
        target.mkdir()
        (target / "new-work").write_text("keep")
        with self.assertRaises(ValueError):
            installer.restore(manifest)
        self.assertTrue((self.root / "beta").is_symlink())
        self.assertEqual((target / "new-work").read_text(), "keep")

    def test_install_failure_rolls_back_links_and_originals(self):
        self.root.mkdir(parents=True)
        original = self.root / "alpha"
        original.mkdir()
        (original / "keep").write_text("original")
        real_symlink = Path.symlink_to

        def fail_second(path, target, **kwargs):
            if path.name == "beta":
                raise OSError("injected failure")
            return real_symlink(path, target, **kwargs)

        with patch.object(Path, "symlink_to", fail_second):
            with self.assertRaises(OSError):
                installer.install(self.repo, self.root, replace=True)
        self.assertFalse(original.is_symlink())
        self.assertEqual((original / "keep").read_text(), "original")
        self.assertFalse((self.root / "beta").exists())

    def test_dangling_symlink_is_preserved(self):
        self.root.mkdir(parents=True)
        target = self.root / "alpha"
        target.symlink_to("missing-target")
        manifest = installer.install(self.repo, self.root, replace=True)
        installer.restore(manifest)
        self.assertEqual(target.readlink(), Path("missing-target"))

    def test_cannot_install_over_source(self):
        with self.assertRaises(ValueError):
            installer.install(self.repo, self.repo / "skills", replace=True)


if __name__ == "__main__":
    unittest.main()
