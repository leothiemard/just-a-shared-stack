"""`jas sync`: export sessions, refresh the box card, commit, pull with rebase, push."""

from __future__ import annotations

import datetime as dt
import json
import os
import platform
import re
import sys
import time
from pathlib import Path

from jas import JasError, brain, budget, config, paths
from jas.sessions import export, sources

LOCK_STALE_SECONDS = 600
QUICK_EXPORT_SECONDS = 6 * 3600


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def state_path(root: Path) -> Path:
    return paths.cache_dir() / f"state-{paths.brain_key(root)}.json"


def load_state(root: Path) -> dict:
    try:
        return json.loads(state_path(root).read_text())
    except (OSError, ValueError):
        return {}


def save_state(root: Path, state: dict) -> None:
    state_path(root).parent.mkdir(parents=True, exist_ok=True)
    state_path(root).write_text(json.dumps(state, indent=2) + "\n")


def age_seconds(stamp: str | None) -> float | None:
    if not stamp:
        return None
    return (now() - dt.datetime.fromisoformat(stamp)).total_seconds()


def synced_label(root: Path) -> str:
    age = age_seconds(load_state(root).get("last_sync"))
    if age is None:
        return "never synced"
    minutes = int(age // 60)
    return f"synced {minutes} min ago" if minutes < 120 else f"synced {minutes // 60} h ago" if minutes < 2880 else f"synced {minutes // 1440} days ago"


class Lock:
    def __init__(self, root: Path) -> None:
        self.path = paths.cache_dir() / f"sync-{paths.brain_key(root)}.lock"
        self.held = False

    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                self.held = True
                return True
            except FileExistsError:
                if time.time() - self.path.stat().st_mtime < LOCK_STALE_SECONDS:
                    return False
                self.path.unlink()
        return False

    def __exit__(self, *exc) -> None:
        if self.held:
            self.path.unlink()


def strip_credentials(url: str) -> str:
    return re.sub(r"://[^/@\s]+@", "://", url)


def box_card(cfg: dict, root: Path, total: int) -> str:
    stack = config.stack_path(cfg)
    rev = brain.git(stack, "rev-parse", "--short", "HEAD", check=False).stdout.strip() or "unknown"
    projects = set()
    for source in sources(cfg["sessions"]["sources"]):
        if source.available()[0]:
            projects.update(source.projects())
    rows = []
    for name, path in sorted(projects):
        where = Path(path)
        remote = brain.git(where, "remote", "get-url", "origin", check=False).stdout.strip() if where.is_dir() else ""
        rows.append(f"| {name} | {paths.tilde(where)} | {strip_credentials(remote)} |")
    lines = [
        "---", "kind: box", f"box: {cfg['box']}", f"zone: {cfg['zone']}", f"last_seen: {dt.date.today().isoformat()}", "---",
        f"# {cfg['box']}", "",
        f"- OS: {platform.system()} {platform.release()} {platform.machine()}",
        f"- Stack revision: {rev}",
        f"- Always-loaded context: {total} bytes (~{total // 4} tokens)",
        "", "## Projects", "",
    ]
    lines += ["| Project | Path | Remote |", "| --- | --- | --- |", *rows] if rows else ["_No projects seen by the session sources yet._"]
    return "\n".join(lines) + "\n"


def write_if_changed(path: Path, text: str) -> None:
    if not path.exists() or path.read_text() != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def run(cfg: dict, quick: bool = False, timeout: float = 60, quiet: bool = False) -> int:
    root = brain.open_brain(cfg)

    def say(message: str) -> None:
        if not quiet:
            print(message)

    with Lock(root) as held:
        if not held:
            say("Another jas sync is running for this brain; skipped.")
            return 0
        state = load_state(root)
        last_export = age_seconds(state.get("last_export"))
        if cfg["sessions"]["export"] and not (quick and last_export is not None and last_export < QUICK_EXPORT_SECONDS):
            result = export.run(cfg, root)
            state["last_export"] = now().isoformat()
            if result["written"]:
                say(f"Exported {len(result['written'])} session digest(s).")
        write_if_changed(root / "Boxes" / f"{cfg['box']}.md", box_card(cfg, root, budget.measure()["total"]))
        stamp = now().strftime("%Y-%m-%dT%H:%M:%SZ")
        if brain.commit_all(root, cfg["box"], f"brain: {cfg['box']} {stamp}"):
            say("Committed local changes.")
        code = exchange(root, cfg["box"], timeout, say, state, quiet)
        save_state(root, state)
        return code


def exchange(root: Path, box: str, timeout: float, say, state: dict, quiet: bool) -> int:
    """Fetch, rebase local commits onto the remote, push. Never forces; a conflict leaves everything local."""
    if "origin" not in brain.git(root, "remote", check=False).stdout.split():
        state["last_sync"] = now().isoformat()
        say("Local-only brain (no remote); nothing to push.")
        return 0
    branch = brain.git(root, "symbolic-ref", "--short", "HEAD").stdout.strip()
    try:
        brain.git(root, "fetch", "-q", "origin", timeout=timeout)
    except JasError as error:
        print(f"warning: could not reach the brain remote; local commits kept, push skipped. {error}", file=sys.stderr)
        return 0
    upstream = f"origin/{branch}"
    if brain.git(root, "rev-parse", "--verify", "-q", upstream, check=False).returncode == 0:
        rebase = brain.git(root, *brain.identity(root, box), "rebase", "--autostash", upstream, check=False)
        if rebase.returncode:
            conflicts = brain.git(root, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
            brain.git(root, "rebase", "--abort", check=False)
            if not quiet:
                files = ", ".join(conflicts) or "unknown files"
                print(
                    f"Sync stopped: {files} changed on another box too. Local commits are kept; nothing was pushed.\n"
                    f"Resolve: cd {root} && git pull --rebase, fix the files, git add them, git rebase --continue, then jas sync.",
                    file=sys.stderr,
                )
            return 1
        ahead = int(brain.git(root, "rev-list", "--count", f"{upstream}..HEAD").stdout.strip() or 0)
    else:
        ahead = 1
    if ahead:
        try:
            brain.git(root, "push", "-q", "-u", "origin", branch, timeout=timeout)
        except JasError as error:
            print(f"warning: push failed; local commits kept. {error}", file=sys.stderr)
            return 0
        say(f"Pushed {branch}.")
    state["last_sync"] = now().isoformat()
    say("Brain in sync.")
    return 0


def refresh_if_stale(cfg: dict, root: Path) -> None:
    """The quick sync before a recall: only with a remote, only when stale, never an error."""
    if not cfg["brain"].get("remote") and "origin" not in brain.git(root, "remote", check=False).stdout.split():
        return
    age = age_seconds(load_state(root).get("last_sync"))
    if age is not None and age < cfg["sync"]["max_age_minutes"] * 60:
        return
    try:
        if run(cfg, quick=True, timeout=8, quiet=True):
            print("warning: brain sync before recall failed; showing local results. Run `jas sync` for details.", file=sys.stderr)
    except JasError as error:
        print(f"warning: brain sync before recall failed: {error}", file=sys.stderr)
