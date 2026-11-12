"""`jas sync`: export sessions, refresh the box card, commit, pull with rebase, push."""

from __future__ import annotations

import datetime as dt
import json
import platform
import re
import sys
from pathlib import Path

from jas import JasError, brain, budget, config, paths
from jas.sessions import export, sources

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
    paths.write_atomic(state_path(root), json.dumps(state, indent=2) + "\n")


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


def lock(root: Path) -> paths.Lock:
    return paths.Lock(paths.cache_dir() / f"sync-{paths.brain_key(root)}.lock")


def strip_credentials(url: str) -> str:
    return re.sub(r"://[^/@\s]+@", "://", url)


def box_card(cfg: dict, root: Path, total: int) -> str:
    stack = config.stack_path(cfg)
    rev = brain.git(stack, "rev-parse", "--short", "HEAD", check=False).stdout.strip() or "unknown"
    projects = set()
    for source in sources(cfg["sessions"]):
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
        paths.write_atomic(path, text)


def run(cfg: dict, quick: bool = False, timeout: float = 60, quiet: bool = False) -> int:
    root = brain.open_brain(cfg)

    def say(message: str) -> None:
        if not quiet:
            print(message)

    with lock(root) as held:
        if not held:
            say("Another jas sync is running for this brain; skipped.")
            return 0
        problem = brain.unfinished(root)
        if problem:
            raise JasError(f"Sync stopped; nothing was exported, committed or pushed. {problem}")
        branch = current_branch(root)
        card = brain.within(root, root / "Boxes" / f"{cfg['box']}.md")
        has_origin = "origin" in brain.git(root, "remote", check=False).stdout.split()
        upstream = fetch(root, branch, timeout) if has_origin else None
        state = load_state(root)
        last_export = age_seconds(state.get("last_export"))
        if cfg["sessions"]["export"] and not (quick and last_export is not None and last_export < QUICK_EXPORT_SECONDS):
            result = export.run(cfg, root)
            state["last_export"] = now().isoformat()
            if result["written"]:
                say(f"Exported {len(result['written'])} session digest(s).")
        write_if_changed(card, box_card(cfg, root, budget.measure()["total"]))
        stamp = now().strftime("%Y-%m-%dT%H:%M:%SZ")
        if brain.commit_all(root, cfg["box"], f"brain: {cfg['box']} {stamp}"):
            say("Committed local changes.")
        try:
            if not has_origin:
                state["last_sync"] = now().isoformat()
                say("Local-only brain (no remote); nothing to push.")
                return 0
            if upstream is None:
                return 0
            return exchange(root, cfg["box"], cfg["zone"], branch, upstream, timeout, say, state, quiet)
        finally:
            save_state(root, state)


def current_branch(root: Path) -> str:
    result = brain.git(root, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    if result.returncode:
        raise JasError(f"Sync stopped; nothing was exported, committed or pushed. The brain {root} is on a detached HEAD. "
                       f"Switch it back to its branch (git switch <branch>), then rerun.")
    return result.stdout.strip()


def fetch(root: Path, branch: str, timeout: float) -> str | None:
    """Fetch, then name the remote branch to exchange with: `origin/<branch>`, or "" when the remote has no branch yet.

    None when the remote cannot be reached. Stops when the remote has branches but not this one: sync never creates a branch there."""
    try:
        brain.git(root, "fetch", "-q", "--prune", "origin", timeout=timeout)
    except JasError as error:
        print(f"warning: could not reach the brain remote; local commits kept, push skipped. {error}", file=sys.stderr)
        return None
    refs = brain.git(root, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/").stdout.split()
    names = [ref[len("refs/remotes/origin/"):] for ref in refs if ref != "refs/remotes/origin/HEAD"]
    if names and branch not in names:
        raise JasError(f"Sync stopped; nothing was exported, committed or pushed. The brain remote has no branch {branch!r} and sync never creates one; "
                       f"it uses {', '.join(names)}. Switch the brain back (git switch {names[0]}), then rerun.")
    return f"origin/{branch}" if names else ""


def exchange(root: Path, box: str, zone: str, branch: str, upstream: str, timeout: float, say, state: dict, quiet: bool) -> int:
    """Rebase local commits onto the fetched remote branch, push. Never forces; a conflict leaves everything local."""
    if upstream:
        shown = brain.git(root, "show", f"{upstream}:brain.json", check=False)
        found = brain.parse_zone(shown.stdout) if shown.returncode == 0 else None
        if found != zone:
            raise JasError(f"The brain remote ({upstream}) is zone {found!r}; this box is zone {zone!r}. Nothing was pulled or pushed; "
                           f"local commits are kept. Point the brain's origin at a {zone} brain.")
        rebase = brain.git(root, *brain.identity(root, box), "rebase", "--autostash", upstream, check=False)
        if rebase.returncode:
            conflicts = brain.git(root, "diff", "--name-only", "--diff-filter=U", check=False).stdout.splitlines()
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
        if brain.zone_of(root) != zone:
            raise JasError(f"Brain {root} is no longer zone {zone!r} after the rebase; nothing was pushed. Check brain.json.")
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
    if "origin" not in brain.git(root, "remote", check=False).stdout.split():
        return
    age = age_seconds(load_state(root).get("last_sync"))
    if age is not None and age < cfg["sync"]["max_age_minutes"] * 60:
        return
    try:
        if run(cfg, quick=True, timeout=8, quiet=True):
            print("warning: brain sync before recall failed; showing local results. Run `jas sync` for details.", file=sys.stderr)
    except JasError as error:
        print(f"warning: brain sync before recall failed: {error}", file=sys.stderr)
