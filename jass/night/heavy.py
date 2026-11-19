"""`jass heavy -- <command>`: run one heavy command at a time on this box, under a memory cap.

On a small box shared by several agents, a build or test suite that outgrows memory gets the whole agent
killed, with every background job it had. Wrapped here, the overrun kills only the command: on Linux
with a systemd user manager it runs in its own scope with `MemoryMax`, and everywhere a box-wide lock
makes the next heavy command wait its turn.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys

from jass import paths
from jass.night import probe

MIN_CAP_MB = 2000


def cap_mb(cfg: dict, asked: int | None = None) -> int:
    """The cap in MB: `--mem`, else `night.heavy_mb` of the box config, else a quarter of memory. 0 is no cap."""
    if asked is not None:
        return asked
    configured = cfg.get("night", {}).get("heavy_mb")
    if configured is not None:
        return int(configured)
    total = probe.memory().get("total_mb", 0)
    return max(MIN_CAP_MB, total // 4) if total else 0  # the same default `jass night check` budgets for


def can_scope() -> bool:
    return sys.platform.startswith("linux") and bool(shutil.which("systemd-run")) and bool(os.environ.get("XDG_RUNTIME_DIR"))


def wrap(command: list[str], mb: int, scope: bool) -> list[str]:
    """`command` inside a capped scope. Swap is denied to it, so an overrun ends the command instead of stalling the box."""
    if not scope or mb <= 0:
        return command
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", "-p", f"MemoryMax={mb}M", "-p", "MemorySwapMax=0", "--", *command]


def run(cfg: dict, command: list[str], asked_mb: int | None = None) -> int:
    mb = cap_mb(cfg, asked_mb)
    argv = wrap(command, mb, can_scope())
    lock = paths.Lock(paths.cache_dir() / "heavy.lock")
    if not lock.__enter__():
        print("jass heavy: another heavy command is running on this box; waiting for it.", file=sys.stderr, flush=True)
        lock = paths.Lock(paths.cache_dir() / "heavy.lock", wait=True)
        lock.__enter__()
    try:
        try:
            child = subprocess.Popen(argv)
        except OSError as error:
            print(f"jass heavy: cannot run {command[0]}: {error}", file=sys.stderr)
            return 127
        previous = {sig: signal.signal(sig, lambda number, _frame: child.send_signal(number)) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            code = child.wait()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    finally:
        lock.__exit__(None, None, None)
    if argv is not command and code in (137, -signal.SIGKILL):
        print(f"jass heavy: the command was killed, most likely at its {mb} MB memory cap. Lower its worker count or split it; "
              f"raise the cap with --mem only if the box has room.", file=sys.stderr)
    return code if code >= 0 else 128 - code
