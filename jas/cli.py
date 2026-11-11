"""`jas` command line. Exit codes: 0 ok, 1 failed check or operation, 2 usage error."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from jas import JasError, brain, config, paths

HUB_MAX_BYTES = 3500


def stdin_text() -> str:
    return "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()


def cmd_init(args) -> int:
    from jas import doctor, init

    init.run_init(args)
    return doctor.run() if args.apply else 0


def cmd_update(args) -> int:
    from jas import doctor, init

    init.run_update(args)
    return doctor.run()


def cmd_doctor(args) -> int:
    from jas import doctor

    return doctor.run(args.json)


def cmd_restore(args) -> int:
    from jas import init

    init.restore(Path(args.manifest).expanduser().absolute())
    print("Restored the state before that transaction. The brain and the stack checkout are untouched.")
    return 0


def cmd_path(args) -> int:
    cfg = config.load()
    if args.what == "config":
        print(paths.config_file())
    elif args.what == "stack":
        print(config.stack_path(cfg))
    else:
        root = brain.open_brain(cfg)
        print(root if args.what == "brain" else root / "Projects" / brain.project_key(root))
    return 0


def cmd_hub(args) -> int:
    root = brain.open_brain(config.load())
    hub = brain.hub_path(root, brain.project_key(root, args.project))
    if hub.is_file():
        rel = hub.relative_to(root).as_posix()
        print(brain.clip(hub.read_text(), HUB_MAX_BYTES, f"jas show {rel}"), end="")
    return 0


def cmd_recall(args) -> int:
    from jas import index, sync

    cfg = config.load()
    root = brain.open_brain(cfg)
    if not args.no_sync:
        sync.refresh_if_stale(cfg, root)
    rev = brain.git(root, "rev-parse", "--short", "HEAD", check=False).stdout.strip() or "uncommitted"
    since = index.parse_since(args.since) if args.since else ""
    with contextlib.closing(index.connect(root)) as conn:
        print(index.recall(
            conn, root, " ".join(args.query), limit=args.limit, budget=args.budget, status=f"brain {rev} · {sync.synced_label(root)}",
            as_json=args.json, project=args.project or "", box=args.box or "", kinds=args.kind, since=since,
        ), end="")
    return 0


def cmd_show(args) -> int:
    from jas import index

    root = brain.open_brain(config.load())
    print(index.show(root, args.ref, args.max_bytes), end="")
    return 0


def cmd_log(args) -> int:
    cfg = config.load()
    root = brain.open_brain(cfg)
    log = brain.add_log(root, brain.project_key(root, args.project), args.title, stdin_text(), cfg["box"])
    print(f"Logged to {log.relative_to(root)}. Run `jas sync` to share it.")
    return 0


def cmd_lesson(args) -> int:
    cfg = config.load()
    root = brain.open_brain(cfg)
    project = brain.project_key(root, args.project)
    path, near = brain.add_lesson(root, args.topic, args.claim, stdin_text(), {"box": cfg["box"], "project": project, "thread": args.thread})
    for claim in near:
        print(f"warning: similar lesson already in {path.relative_to(root)}: {claim}", file=sys.stderr)
    print(f"Saved lesson to {path.relative_to(root)}. Run `jas sync` to share it.")
    return 0


def cmd_sessions(args) -> int:
    from jas import index
    from jas.sessions import export

    cfg = config.load()
    root = brain.open_brain(cfg)
    since = index.parse_since(args.since) if args.since else ""
    if args.action == "export":
        result = export.run(cfg, root, everything=args.all, since=since, dry_run=args.dry_run)
        if args.json:
            print(json.dumps(result, indent=2))
            return 0
        verb = "Would write" if args.dry_run else "Wrote"
        for rel in result["written"]:
            print(f"{verb} {rel}")
        print(f"{verb} {len(result['written'])} digest(s); {result['unchanged']} unchanged, {result['skipped']} skipped (active, subagent, excluded or too old).")
        return 0
    rows = export.listing(root, since, args.box or "", args.project or "")
    if args.json:
        print(json.dumps(rows, indent=2))
    for row in [] if args.json else rows:
        print(f"{row['date']}  {row['box']}  {row['project']}  {row['title']}  {row['turns']} turns  {row['tokens']} tokens")
    return 0


def cmd_sync(args) -> int:
    from jas import sync

    return sync.run(config.load(), quick=args.quick)


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="jas", description="just-a-stack: box setup, the shared brain, recall and sync.")
    sub = top.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    def command(name: str, func, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    p = command("init", cmd_init, "Set up this box: config, skills, CLI, global rules, brain. Preview unless --apply.")
    p.add_argument("--box", help="box name, [a-z0-9-]+ (default: from the hostname)")
    p.add_argument("--zone", help="trust zone, e.g. personal or work (default: personal)")
    p.add_argument("--brain-path", help="where the brain lives (default: ~/brain)")
    source = p.add_mutually_exclusive_group()
    source.add_argument("--brain", metavar="URL", help="clone this brain repo, or add it as the remote of an existing one")
    source.add_argument("--new-brain", action="store_true", help="create a new local brain")
    p.add_argument("--import-notes", metavar="DIR", help="copy DIR/Projects and top-level .md files into the brain, never overwriting")
    p.add_argument("--layer", action="append", metavar="DIR", help="extra stack directory (skills.json, skills/, rules/global.md)")
    p.add_argument("--replace", action="store_true", help="back up and replace conflicting skills or links")
    p.add_argument("--apply", action="store_true", help="make the changes")

    p = command("update", cmd_update, "Pull the stack and layers, refresh skills, CLI and rules, then run doctor. Preview unless --apply.")
    p.add_argument("--replace", action="store_true", help="back up and replace conflicting skills or links")
    p.add_argument("--apply", action="store_true", help="pull and make the changes")

    p = command("doctor", cmd_doctor, "Check this box and print the context budget. Exit 1 on failures.")
    p.add_argument("--json", action="store_true")

    p = command("restore", cmd_restore, "Undo one init or update transaction.")
    p.add_argument("manifest", help="manifest path printed by --apply")

    p = command("path", cmd_path, "Print a path.")
    p.add_argument("what", nargs="?", default="brain", choices=["brain", "stack", "config", "project"])

    p = command("hub", cmd_hub, "Print the project's hub note (silent when there is none).")
    p.add_argument("project", nargs="?", help="project name (default: the current repo or directory)")

    p = command("recall", cmd_recall, "Ranked, budgeted search over the brain.")
    p.add_argument("query", nargs="+")
    p.add_argument("--project", "-p")
    p.add_argument("--box")
    p.add_argument("--kind", action="append", choices=["lesson", "hub", "note", "review", "log", "box", "session", "other"])
    p.add_argument("--since", help="5w, 10d, 3m, 1y or a date like 2026-10-01")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--budget", type=int, default=800, help="output budget in tokens (bytes/4)")
    p.add_argument("--no-sync", action="store_true", help="skip the freshness sync")
    p.add_argument("--json", action="store_true")

    p = command("show", cmd_show, "Print one recall hit, a path#heading section, or a file.")
    p.add_argument("ref", help="hit number from the last recall, path#heading, or path")
    p.add_argument("--max-bytes", type=int, default=6000)

    p = command("log", cmd_log, "Add a dated entry at the top of the project's Log.md (body on stdin).")
    p.add_argument("-t", "--title", required=True)
    p.add_argument("-p", "--project")

    p = command("lesson", cmd_lesson, "Append a cross-project lesson (optional body on stdin, at most four lines).")
    p.add_argument("topic", help="tool or theme, e.g. vercel")
    p.add_argument("claim", help="the lesson phrased as a one-line claim")
    p.add_argument("-p", "--project")
    p.add_argument("--thread", help="thread or session id for provenance")

    p = command("sessions", cmd_sessions, "Export or list session digests.")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True
    e = actions.add_parser("export", help="write digests for finished sessions on this box")
    e.add_argument("--all", action="store_true", help="rewrite every digest, not only changed sessions")
    e.add_argument("--since")
    e.add_argument("--dry-run", action="store_true")
    e.add_argument("--json", action="store_true")
    ls = actions.add_parser("list", help="one line per digest")
    ls.add_argument("--since")
    ls.add_argument("--box")
    ls.add_argument("--project", "-p")
    ls.add_argument("--json", action="store_true")

    p = command("sync", cmd_sync, "Export sessions, refresh the box card, commit, pull and push the brain.")
    p.add_argument("--quick", action="store_true", help="skip the session export when the last one was under 6 hours ago")
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except JasError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
