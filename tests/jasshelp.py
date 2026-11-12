"""Shared sandbox for jass tests: a temp home through JASS_HOME and a hermetic git config."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jass import cli  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def tree(root: Path) -> dict[str, str]:
    """Every path under `root` with its link target or file content, for before/after comparisons."""
    out = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            out[rel] = "-> " + os.readlink(path)
        elif path.is_file():
            out[rel] = path.read_bytes().decode(errors="replace")
        else:
            out[rel] = "/"
    return out


class PartialWrite:
    """A file whose first write lands one byte and then fails, as on a full disk."""

    def __init__(self, file) -> None:
        self.file = file

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.file.close()

    def write(self, data):
        self.file.write(data[:1])
        self.file.flush()
        raise OSError(28, "No space left on device")

    def __getattr__(self, name):
        return getattr(self.file, name)


@contextlib.contextmanager
def disk_full(folder: Path):
    """Every file opened for writing directly inside `folder` fails after its first byte."""
    real_open = io.open

    def failing_open(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        writing = any(flag in mode for flag in "wxa")
        return PartialWrite(handle) if writing and Path(file).parent == folder else handle

    with mock.patch("io.open", failing_open), mock.patch("builtins.open", failing_open):
        yield


class Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="jass test ")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        gitconfig = self.base / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Fixture\n\temail = fixture@example.invalid\n[init]\n\tdefaultBranch = main\n")
        env = mock.patch.dict(os.environ, {"JASS_HOME": str(self.home), "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"})
        env.start()
        self.addCleanup(env.stop)

    def use_home(self, home: Path) -> None:
        os.environ["JASS_HOME"] = str(home)
        self.home = home

    def jass(self, *argv: str, stdin: str = "") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            try:
                code = cli.main(list(argv))
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv: str, stdin: str = "") -> str:
        code, out, err = self.jass(*argv, stdin=stdin)
        self.assertEqual(code, 0, f"jass {' '.join(argv)} failed:\n{out}\n{err}")
        return out

    def new_box(self, box: str = "mac", zone: str = "personal", sources: list[str] | None = None) -> Path:
        """A configured box with a new local lore, written directly (no skill links)."""
        lore = self.home / "lore"
        cfg = {"box": box, "zone": zone, "lore": {"path": str(lore), "remote": ""},
               "sessions": {"sources": sources or [], "idle_minutes": 30}}
        config = self.home / ".config" / "jass" / "box.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps(cfg))
        from jass import lore as lore_module

        lore_module.create(lore, zone, box)
        return lore
