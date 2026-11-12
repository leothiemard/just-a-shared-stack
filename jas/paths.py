"""Every home-relative path. `JAS_HOME` replaces the real home for tests and sandboxes."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import uuid
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


def write_atomic(path: Path, data: str | bytes) -> None:
    """Replace the file at `path` (through a symlink, never the link itself) so it holds either the old or the new content.

    The new content is written to a `0600` file and gets the old file's mode (or the umask default) only once it is complete."""
    target = path.resolve() if path.is_symlink() else path
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(temp, "xb", opener=lambda name, flags: os.open(name, flags, 0o600)) as handle:
            handle.write(data.encode() if isinstance(data, str) else data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o666 & ~umask())
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def umask() -> int:
    current = os.umask(0o077)
    os.umask(current)
    return current


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
