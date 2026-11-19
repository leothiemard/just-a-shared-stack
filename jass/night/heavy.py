"""`jass heavy -- <command>`: run one heavy command at a time on this box, under a memory cap.

On a small box shared by several agents, a build or test suite that outgrows memory gets the whole agent
killed, with every background job it had. Wrapped here, the overrun kills only the command: on Linux
with a systemd user manager it runs in its own scope (its own cgroup) with `MemoryMax`, and everywhere a
box-wide lock makes the next heavy command wait its turn.

The lock is held by this wrapper, not by the command: if the wrapper is killed, the lock is free even
when the command lives on. Handing the lock to the command would let one leftover server block every
later heavy command, which is worse.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from jass import paths
from jass.night import probe

MIN_CAP_MB = 2000
INSIDE = "JASS_HEAVY"  # set for the command, so a nested `jass heavy` runs straight through


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
    """Whether a systemd user manager can be asked for a scope from here."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    return (sys.platform.startswith("linux") and bool(shutil.which("systemd-run")) and bool(runtime)
            and any((Path(runtime) / name).exists() for name in ("bus", "systemd/private")))


def wrap(command: list[str], mb: int, unit: str) -> list[str]:
    """`command` inside the capped scope `unit`. Swap is denied to it, so an overrun ends the command instead of stalling the box."""
    if not unit or mb <= 0:
        return command
    return ["systemd-run", "--user", "--scope", "--quiet", f"--unit={unit}", "-p", f"MemoryMax={mb}M", "-p", "MemorySwapMax=0", "--", *command]


def hit_cap(unit: str) -> bool:
    """Whether the kernel killed a process of the scope at its cap. systemd keeps such a scope, as failed, until it is reset."""
    for _ in range(5):
        shown = probe.run("systemctl", "--user", "show", f"{unit}.scope", "-p", "Result", "-p", "ActiveState")
        if "Result=oom-kill" in shown:
            probe.run("systemctl", "--user", "reset-failed", f"{unit}.scope")
            return True
        if not shown or "ActiveState=inactive" in shown:
            return False
        time.sleep(0.2)  # the scope is still closing; its result may not be recorded yet
    return False


def run(cfg: dict, command: list[str], asked_mb: int | None = None) -> int:
    if not shutil.which(command[0]):
        print(f"jass heavy: cannot run {command[0]}: not found or not executable", file=sys.stderr)
        return 127
    if os.environ.get(INSIDE):  # a script under `jass heavy` that calls it again would wait on its own lock for ever
        return subprocess.call(command)
    mb = cap_mb(cfg, asked_mb)
    unit = f"jass-heavy-{os.getpid()}-{int(time.time())}" if can_scope() else ""
    argv = wrap(command, mb, unit)
    lock = paths.Lock(paths.cache_dir() / "heavy.lock")
    if not lock.__enter__():
        print("jass heavy: another heavy command is running on this box; waiting for it.", file=sys.stderr, flush=True)
        lock = paths.Lock(paths.cache_dir() / "heavy.lock", wait=True)
        lock.__enter__()
    try:
        try:
            child = subprocess.Popen(argv, env={**os.environ, INSIDE: "1"})
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
    code = code if code >= 0 else 128 - code
    if argv is not command and code != 0 and hit_cap(unit):
        print(f"jass heavy: a process of this command was killed at the {mb} MB memory cap (exit {code}), so a failure reported above may be only that. "
              f"Lower the worker count or split the command; raise the cap with --mem only if the box has room.", file=sys.stderr)
    return code
