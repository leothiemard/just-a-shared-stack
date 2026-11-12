"""`jas doctor`: read-only checks of this box plus the context budget table."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from jas import JasError, brain, budget, config, index, paths, rules, sync
from jas.init import installer
from jas.sessions import sources

HUB_MAX = 3000


def checks(cfg: dict) -> list[tuple[str, str, str]]:
    out = [("ok", "config", paths.tilde(paths.config_file()))]

    def add(ok: bool, name: str, detail: str, level: str = "fail") -> None:
        out.append(("ok" if ok else level, name, detail))

    stack = config.stack_path(cfg)
    add(brain.is_repo(stack), "stack", f"{paths.tilde(stack)}" + ("" if brain.is_repo(stack) else " is not a git checkout; clone it again"))
    repos = [stack, *config.layer_paths(cfg)]
    for root in paths.skill_roots():
        for repo in repos:
            try:
                rows = installer.plan(repo, root)
            except (OSError, ValueError, KeyError) as error:
                add(False, "skills", f"{repo}: {error}")
                continue
            missing = [t.name for t, _, state in rows if state != "linked"]
            detail = f"not linked: {', '.join(missing)}; run jas update --apply" if missing else f"{len(rows)} linked"
            add(not missing, "skills", f"{paths.tilde(root)} ← {repo.name}: {detail}")
    found = shutil.which("jas")
    on_path = bool(found) and Path(found).resolve() == (stack / "bin" / "jas").resolve()
    add(on_path, "cli", "jas on PATH" if on_path else f"jas not on PATH; add: export PATH=\"$HOME/.local/bin:$PATH\"", "warn")
    try:
        block = rules.render(repos)
        canonical = paths.canonical_rules()
        for path in [canonical, *paths.linked_rules()]:
            if path != canonical and path.is_symlink() and path.resolve() == canonical.resolve():
                add(True, "rules", f"{paths.tilde(path)} → {paths.tilde(canonical)}")
            elif not path.is_file():
                add(False, "rules", f"{paths.tilde(path)} missing; run jas update --apply", "fail" if path == canonical else "warn")
            else:
                try:
                    same = rules.current(path.read_bytes().decode(), path) == block
                except JasError as error:
                    add(False, "rules", str(error))
                    continue
                add(same, "rules", f"{paths.tilde(path)}: managed block " + ("current" if same else "missing or stale; run jas update --apply"))
    except (OSError, JasError) as error:
        add(False, "rules", str(error))
    out += brain_checks(cfg)
    add(index.fts5_available(), "fts5", "SQLite FTS5 available" if index.fts5_available() else "this Python's sqlite3 lacks FTS5; use a python.org or Homebrew Python")
    for source in sources(cfg["sessions"]) if cfg["sessions"]["export"] else []:
        ok, reason = source.available()
        add(ok, "sessions", f"{source.name} source " + ("available" if ok else f"disabled: {reason}"), "warn")
    return out


def brain_checks(cfg: dict) -> list[tuple[str, str, str]]:
    try:
        root = brain.open_brain(cfg)
    except JasError as error:
        return [("fail", "brain", str(error))]
    out = [("ok", "brain", f"{paths.tilde(root)} (zone {cfg['zone']})")]

    def git(*args: str):
        return brain.git(root, *args, check=False)

    problem = brain.unfinished(root)
    if problem:
        out.append(("fail", "brain git", problem))
    dirty = git("status", "--porcelain").stdout.strip()
    out.append(("warn" if dirty else "ok", "brain git", "uncommitted changes; jas sync commits them" if dirty else "clean"))
    branch = git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    counts = git("rev-list", "--left-right", "--count", f"{sync.REMOTE}{branch}...HEAD")
    if counts.returncode == 0:
        behind, ahead = counts.stdout.split()
        out.append(("warn" if int(ahead) else "ok", "brain git", f"{ahead} ahead, {behind} behind the remote (as of the last fetch)"))
    out.append(("ok", "brain sync", sync.synced_label(root)))
    has_identity = bool(git("config", "user.email").stdout.strip())
    out.append(("ok" if has_identity else "warn", "git identity", "set" if has_identity else f"none; brain commits use {cfg['box']}@just-a-stack.local"))
    for probe, line in (("Projects/x/Log.md", "Log.md merge=union"), ("Lessons/x.md", "Lessons/*.md merge=union")):
        union = "merge: union" in git("check-attr", "merge", "--", probe).stdout
        out.append(("ok" if union else "warn", "merge driver", line if union else f"add `{line}` to the brain's .gitattributes (rerun `jas init --apply` to add it)"))
    ignored = git("check-ignore", "-q", "--no-index", f"x{paths.TEMP}0").returncode == 0
    out.append(("ok" if ignored else "warn", "temp files", f"*{paths.TEMP}* ignored" if ignored else
                f"add `*{paths.TEMP}*` to the brain's .gitignore (rerun `jas init --apply` to add it)"))
    big = sorted(p for p in (root / "Projects").glob("*/*.md") if p.stem == p.parent.name and brain.inside(root, p) and p.stat().st_size > HUB_MAX)
    for hub in big:
        out.append(("warn", "hub size", f"{hub.relative_to(root)} is {hub.stat().st_size} bytes (target {HUB_MAX}); move detail to topic notes"))
    return out


def run(as_json: bool = False) -> int:
    try:
        cfg = config.load()
        results = checks(cfg)
    except JasError as error:
        results = [("fail", "config", str(error))]
    report = budget.measure(Path.cwd())
    failed = any(status == "fail" for status, _, _ in results)
    if as_json:
        print(json.dumps({"checks": [{"status": s, "check": n, "detail": d} for s, n, d in results], "budget": report}, indent=2))
    else:
        width = max(len(n) for _, n, _ in results)
        for status, name, detail in results:
            print(f"{status:4}  {name:{width}}  {detail}")
        print("\nContext budget (measured)\n" + budget.table(report), end="")
    return 1 if failed else 0
