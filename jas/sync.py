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
    paths.private_dir(state_path(root).parent)
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
        url = origin_url(root, cfg["brain"]["remote"]) if has_origin else ""
        upstream = fetch(root, url, branch, timeout) if has_origin else None
        state = load_state(root)
        last_export = age_seconds(state.get("last_export"))
        if cfg["sessions"]["export"] and not (quick and last_export is not None and last_export < QUICK_EXPORT_SECONDS):
            result = export.run(cfg, root)
            state["last_export"] = now().isoformat()
            if result["written"]:
                say(f"Exported {len(result['written'])} session digest(s).")
        write_if_changed(card, box_card(cfg, root, budget.measure()["total"]))
        brain.remove_stale_temps(root)
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
            return exchange(root, cfg["box"], cfg["zone"], url, branch, upstream, timeout, say, state, quiet)
        finally:
            save_state(root, state)


def current_branch(root: Path) -> str:
    result = brain.git(root, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    if result.returncode:
        raise JasError(f"Sync stopped; nothing was exported, committed or pushed. The brain {root} is on a detached HEAD. "
                       f"Switch it back to its branch (git switch <branch>), then rerun.")
    return result.stdout.strip()


REMOTE = "refs/jas/remote/"


def repo_id(url: str) -> str:
    """A repository URL as host/path in lower case, whatever its form: https, ssh://, git@host:path, file:// or a path."""
    text = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", url.strip())
    text = re.sub(r":\d*/", "/", re.sub(r"^[^@/]*@", "", text), count=1).replace(":", "/", 1)
    return re.sub(r"\.git$", "", text.rstrip("/")).lower()


def origin_url(root: Path, configured: str) -> str:
    """The URL sync fetches from and pushes to: origin's, when it names the box config's `brain.remote` (or none is set).

    Fetch and push both use this one URL, so a push URL or a repointed origin never sends the brain somewhere unchecked."""
    url = brain.git(root, "remote", "get-url", "origin").stdout.strip()
    if configured and repo_id(url) != repo_id(configured):
        raise JasError(f"Sync stopped; nothing was exported, committed or pushed. The brain's origin is {strip_credentials(url)}, "
                       f"but brain.remote in {paths.tilde(paths.config_file())} is {strip_credentials(configured)}. "
                       f"Point origin back (git remote set-url origin <brain.remote>) or set brain.remote to the new URL, then rerun.")
    return url


def fetch(root: Path, url: str, branch: str, timeout: float) -> str | None:
    """Fetch every remote branch into `refs/jas/remote/`, then name the ref to exchange with, or "" when the remote has no branch.

    The refspec is explicit, so the brain's own fetch config and tracking refs never decide what the remote has.
    None when the remote cannot be reached. Stops when the remote has branches but not this one: sync never creates a branch there."""
    try:
        brain.git(root, "fetch", "-q", "--prune", "--no-tags", url, f"+refs/heads/*:{REMOTE}*", timeout=timeout)
    except JasError as error:
        print(f"warning: could not reach the brain remote; local commits kept, push skipped. {error}", file=sys.stderr)
        return None
    names = [ref[len(REMOTE):] for ref in brain.git(root, "for-each-ref", "--format=%(refname)", REMOTE).stdout.split()]
    if names and branch not in names:
        raise JasError(f"Sync stopped; nothing was exported, committed or pushed. The brain remote has no branch {branch!r} and sync never creates one; "
                       f"it uses {', '.join(names)}. Switch the brain back (git switch {names[0]}), then rerun.")
    return REMOTE + branch if names else ""


def zone_at(root: Path, rev: str) -> str | None:
    shown = brain.git(root, "show", f"{rev}:brain.json", check=False)
    return brain.parse_zone(shown.stdout) if shown.returncode == 0 else None


def exchange(root: Path, box: str, zone: str, url: str, branch: str, upstream: str, timeout: float, say, state: dict, quiet: bool) -> int:
    """Rebase local commits onto the fetched remote branch, push. A push succeeds only while the remote branch is still what was fetched."""
    base = brain.git(root, "rev-parse", upstream).stdout.strip() if upstream else ""
    if base:
        found = zone_at(root, base)
        if found != zone:
            raise JasError(f"The brain remote's {branch} is zone {found!r}; this box is zone {zone!r}. Nothing was pulled or pushed; "
                           f"local commits are kept. Point the brain's origin at a {zone} brain.")
        rebase = brain.git(root, *brain.identity(root, box), "rebase", "--autostash", base, check=False)
        if rebase.returncode:
            conflicts = brain.git(root, "diff", "--name-only", "--diff-filter=U", check=False).stdout.splitlines()
            brain.git(root, "rebase", "--abort", check=False)
            if not quiet:
                files = ", ".join(conflicts) or "unknown files"
                print(
                    f"Sync stopped: {files} changed on another box too. Local commits are kept; nothing was pushed.\n"
                    f"Resolve: cd {root} && git rebase {upstream}, fix the files, git add them, git rebase --continue, then jas sync.",
                    file=sys.stderr,
                )
            return 1
    head = brain.git(root, "rev-parse", "HEAD").stdout.strip()
    if head != base:
        if zone_at(root, head) != zone:
            raise JasError(f"Brain {root} is no longer zone {zone!r} after the rebase; nothing was pushed. Check brain.json.")
        try:
            brain.git(root, "push", "-q", f"--force-with-lease=refs/heads/{branch}:{base}", url, f"{head}:refs/heads/{branch}", timeout=timeout)
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
