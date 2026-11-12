"""Every home-relative path. `JAS_HOME` replaces the real home for tests and sandboxes."""

from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path

STACK = Path(__file__).resolve().parents[1]


def home() -> Path:
    return Path(os.environ.get("JAS_HOME") or os.path.expanduser("~"))


def expand(path: str | Path) -> Path:
    text = str(path)
    if text == "~" or text.startswith("~/"):
        return home() / text[2:]
    return Path(text)


def tilde(path: Path) -> str:
    """Show a path relative to the home as `~/...` when it lives there."""
    try:
        return "~/" + str(Path(path).relative_to(home()))
    except ValueError:
        return str(path)


def config_dir() -> Path:
    return home() / ".config" / "just-a-stack"


def config_file() -> Path:
    return config_dir() / "box.json"


def cache_dir() -> Path:
    return home() / ".cache" / "just-a-stack"


def brain_key(brain: Path) -> str:
    return hashlib.sha1(str(brain.resolve()).encode()).hexdigest()[:12]


class Lock:
    """An advisory lock on `path` for the `with` block. The kernel drops it when the process exits, so a crash never leaves it held."""

    def __init__(self, path: Path, wait: bool = False) -> None:
        self.path, self.wait, self.fd = path, wait, None

    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if self.wait else fcntl.LOCK_NB))
        except BlockingIOError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def skill_roots() -> list[Path]:
    return [home() / ".agents" / "skills", home() / ".claude" / "skills"]


def canonical_rules() -> Path:
    return home() / ".agents" / "AGENTS.md"


def linked_rules() -> list[Path]:
    return [home() / ".claude" / "CLAUDE.md", home() / ".codex" / "AGENTS.md"]


def bin_dir() -> Path:
    return home() / ".local" / "bin"
