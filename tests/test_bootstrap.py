import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bootstrap.sh"
URL = "https://github.com/example/just-a-shared-stack.git"
SHELLS = [shell for shell in ("sh", "dash") if shutil.which(shell)]


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="bootstrap ")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.ran = self.base / "ran"

    def checkout(self, origin, cli=True):
        stack = Path(tempfile.mkdtemp(prefix="stack ", dir=self.base))
        subprocess.run(["git", "init", "-q", str(stack)], check=True)
        subprocess.run(["git", "-C", str(stack), "remote", "add", "origin", origin], check=True)
        (stack / "bin").mkdir()
        (stack / "bin" / "jass").write_text(f"#!/bin/sh\necho \"$@\" > '{self.ran}'\n")
        (stack / "bin" / "jass").chmod(0o755)
        if cli:
            (stack / "jass").mkdir()
            (stack / "jass" / "cli.py").write_text("")
        return stack

    def bootstrap(self, shell, stack):
        env = dict(os.environ, HOME=str(self.base), JASS_STACK_DIR=str(stack), JASS_STACK_URL=URL, GIT_CONFIG_NOSYSTEM="1",
                   GIT_CONFIG_GLOBAL=os.devnull)
        return subprocess.run([shell, str(SCRIPT), "--box", "test"], capture_output=True, text=True, env=env)

    def test_the_script_parses(self):
        for shell in SHELLS:
            with self.subTest(shell):
                self.assertEqual(subprocess.run([shell, "-n", str(SCRIPT)]).returncode, 0)

    def test_a_checkout_of_the_same_repository_is_used_in_any_url_form(self):
        for origin in ("https://github.com/example/just-a-shared-stack", "git@github.com:Example/just-a-shared-stack.git",
                       "ssh://git@GitHub.com/example/Just-A-Shared-Stack.git", "https://user@github.com/example/just-a-shared-stack.git/"):
            for shell in SHELLS:
                with self.subTest(origin=origin, shell=shell):
                    stack = self.checkout(origin)
                    result = self.bootstrap(shell, stack)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.ran.read_text(), "init --box test\n")
                    shutil.rmtree(stack)
                    self.ran.unlink()

    def test_an_unrelated_folder_is_never_run(self):
        cases = {
            "another repository": ("https://github.com/example/other-tool.git", True),
            "another owner": ("git@github.com:someone/just-a-shared-stack.git", True),
            "no jass package": (URL, False),
        }
        for name, (origin, cli) in cases.items():
            for shell in SHELLS:
                with self.subTest(name, shell=shell):
                    stack = self.checkout(origin, cli)
                    result = self.bootstrap(shell, stack)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("JASS_STACK_DIR", result.stderr)
                    self.assertFalse(self.ran.exists())
                    shutil.rmtree(stack)

    def test_a_folder_inside_another_checkout_is_never_run(self):
        stack = self.checkout(URL)
        inner = stack / "jass"
        (inner / "bin").mkdir()
        (inner / "bin" / "jass").write_text(f"#!/bin/sh\necho \"$@\" > '{self.ran}'\n")
        (inner / "bin" / "jass").chmod(0o755)
        (inner / "jass").mkdir()
        (inner / "jass" / "cli.py").write_text("")
        for shell in SHELLS:
            with self.subTest(shell):
                result = self.bootstrap(shell, inner)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("JASS_STACK_DIR", result.stderr)
                self.assertFalse(self.ran.exists())
