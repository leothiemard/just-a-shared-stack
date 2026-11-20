"""`jass` command line. Exit codes: 0 ok, 1 failed check or operation, 2 usage error."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from jass import JassError, config, lore, paths

HUB_MAX_BYTES = 3500


def stdin_text() -> str:
    return "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()


def cmd_init(args) -> int:
    from jass import doctor, init

    init.run_init(args)
    return doctor.run() if args.apply else 0


def cmd_update(args) -> int:
    from jass import doctor, init

    init.run_update(args)
    return doctor.run()


def cmd_doctor(args) -> int:
    from jass import doctor

    return doctor.run(args.json)


def cmd_restore(args) -> int:
    from jass import init

    init.restore(Path(args.manifest).expanduser().absolute())
    print("Restored the state before that transaction. The lore and the stack checkout are untouched.")
    return 0


def cmd_path(args) -> int:
    cfg = config.load()
    if args.what == "config":
        print(paths.config_file())
    elif args.what == "stack":
        print(config.stack_path(cfg))
    else:
        root = lore.open_lore(cfg)
        print(root if args.what == "lore" else root / "Projects" / lore.project_key(root))
    return 0


def cmd_hub(args) -> int:
    root = lore.open_lore(config.load())
    hub = lore.within(root, lore.hub_path(root, lore.project_key(root, args.project)))
    if hub.is_file():
        rel = hub.relative_to(root).as_posix()
        print(lore.clip(hub.read_text(), HUB_MAX_BYTES, f"jass show {rel}"), end="")
    return 0


def cmd_recall(args) -> int:
    from jass import index, sync

    cfg = config.load()
    root = lore.open_lore(cfg)
    if not args.no_sync:
        sync.refresh_if_stale(cfg, root)
    rev = lore.git(root, "rev-parse", "--short", "HEAD", check=False).stdout.strip() or "uncommitted"
    since = index.parse_since(args.since) if args.since else ""
    with contextlib.closing(index.connect(root)) as conn:
        print(index.recall(
            conn, root, " ".join(args.query), limit=args.limit, budget=args.budget, status=f"lore {rev} · {sync.synced_label(root)}",
            as_json=args.json, project=args.project or "", box=args.box or "", kinds=args.kind, since=since,
        ), end="")
    return 0


def cmd_show(args) -> int:
    from jass import index

    root = lore.open_lore(config.load())
    print(index.show(root, args.ref, args.max_bytes), end="")
    return 0


def cmd_log(args) -> int:
    cfg = config.load()
    root = lore.open_lore(cfg)
    log = lore.add_log(root, lore.project_key(root, args.project), args.title, stdin_text(), cfg["box"])
    print(f"Logged to {log.relative_to(root)}. Run `jass sync` to share it.")
    return 0


def cmd_lesson(args) -> int:
    cfg = config.load()
    root = lore.open_lore(cfg)
    project = lore.project_key(root, args.project)
    path, near = lore.add_lesson(root, args.topic, args.claim, stdin_text(), {"box": cfg["box"], "project": project, "thread": args.thread})
    for claim in near:
        print(f"warning: similar lesson already in {path.relative_to(root)}: {claim}", file=sys.stderr)
    print(f"Saved lesson to {path.relative_to(root)}. Run `jass sync` to share it.")
    return 0


def cmd_sessions(args) -> int:
    from jass import index
    from jass.sessions import export

    cfg = config.load()
    root = lore.open_lore(cfg)
    since = index.parse_since(args.since) if args.since else ""
    if args.action == "export":
        result = export.run(cfg, root, everything=args.all, since=since, dry_run=args.dry_run, prune=args.prune)
        if args.json:
            print(json.dumps(result, indent=2))
            return 0
        verb = "Would write" if args.dry_run else "Wrote"
        for rel in result["written"]:
            print(f"{verb} {rel}")
        print(f"{verb} {len(result['written'])} digest(s); {result['unchanged']} unchanged, {result['skipped']} skipped (active, subagent, delegated, excluded or too old).")
        if args.prune:
            for rel in result["pruned"]:
                print(f"{'Would delete' if args.dry_run else 'Deleted'} {rel}")
            print(f"{'Would delete' if args.dry_run else 'Deleted'} {len(result['pruned'])} digest(s) of sessions that are no longer exported.")
        return 0
    if args.action == "stats":
        groups = export.stats(export.listing(root, since), args.by)
        print(json.dumps(groups, indent=2) if args.json else export.stats_table(groups, args.by).rstrip("\n"))
        return 0
    rows = export.listing(root, since, args.box or "", args.project or "")
    if args.json:
        print(json.dumps(rows, indent=2))
    for row in [] if args.json else rows:
        extra = (f"  peak {row['peak_context'] // 1000}K" if row["peak_context"] else "") + (f"  {row['subagents']} subagents" if row["subagents"] else "")
        print(f"{row['date']}  {row['box']}  {row['project']}  {row['title']}  {row['turns']} turns  {row['tokens']} tokens{extra}")
    return 0


def cmd_sync(args) -> int:
    from jass import sync

    return sync.run(config.load(), quick=args.quick, first_push=args.first_push)


def cmd_night(args) -> int:
    import sqlite3

    from jass.night import check, probe, t3state, watch

    cfg = config.load()
    if args.action == "watch":
        return watch.run(cfg, args)
    if args.action == "status":
        print(watch.status(cfg, args.thread), end="")
        return 0
    threads, problem, store = None, "", t3state.store_path(cfg)
    agents = args.agents
    try:
        threads = t3state.snapshot(store)
        agents = t3state.going(store) if agents is None else agents
    except (JassError, sqlite3.Error) as error:
        threads, problem = None, str(error)
    facts = probe.collect()
    facts["t3"] = probe.t3(store.parent.parent)
    rows = check.report(cfg, facts, threads, problem, hours=args.hours, agents=agents or 1, needs=args.need, peers=args.peer)
    if args.json:
        print(json.dumps([{"status": s, "check": n, "detail": d} for s, n, d in rows], indent=2))
    else:
        width = max(len(name) for _, name, _ in rows)
        for status, name, detail in rows:
            print(f"{status:4}  {name:{width}}  {detail}")
        fails, warns = (sum(1 for row in rows if row[0] == level) for level in ("fail", "warn"))
        print(f"\n{fails} to fix before you leave, {warns} to know about. Checked this box only; run it on every box that hosts work tonight.")
    return 1 if any(status == "fail" for status, _, _ in rows) else 0


def cmd_heavy(args) -> int:
    from jass.night import heavy

    command = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not command:
        raise JassError("Give the command to run: jass heavy -- npm run check")
    return heavy.run(config.load(), command, args.mem)


def cmd_board(args) -> int:
    from jass import board

    cfg = {} if args.action == "open" else config.load()  # open needs only `vercel login`, not a box config
    return {"work": board.work, "add": board.add, "status": board.status, "open": board.open_board}[args.action](cfg, args)


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="jass", description="Just a Shared Stack: box setup, the shared lore, recall and sync.")
    sub = top.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    def command(name: str, func, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    p = command("init", cmd_init, "Set up this box: config, skills, CLI, global rules, lore. Preview unless --apply.")
    p.add_argument("--box", help="box name, [a-z0-9-]+ (default: from the hostname)")
    p.add_argument("--zone", help="trust zone, e.g. personal or work (default: personal)")
    p.add_argument("--lore-path", help="where the lore lives (default: ~/lore)")
    source = p.add_mutually_exclusive_group()
    source.add_argument("--lore", metavar="URL", help="clone this lore repo, or add it as the remote of an existing one")
    source.add_argument("--new-lore", action="store_true", help="create a new local lore repo")
    p.add_argument("--import-notes", metavar="DIR", help="copy DIR/Projects and top-level .md files into the lore, never overwriting")
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
    p.add_argument("what", nargs="?", default="lore", choices=["lore", "stack", "config", "project"])

    p = command("hub", cmd_hub, "Print the project's hub note (silent when there is none).")
    p.add_argument("project", nargs="?", help="project name (default: the current repo or directory)")

    p = command("recall", cmd_recall, "Ranked, budgeted search over the lore.")
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

    p = command("sessions", cmd_sessions, "Export, list or summarise session digests.")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True
    e = actions.add_parser("export", help="write digests for finished sessions on this box")
    e.add_argument("--all", action="store_true", help="rewrite every digest, not only changed sessions")
    e.add_argument("--since")
    e.add_argument("--prune", action="store_true", help="also delete this box's digests of subagent, delegated and excluded sessions")
    e.add_argument("--dry-run", action="store_true")
    e.add_argument("--json", action="store_true")
    ls = actions.add_parser("list", help="one line per digest")
    ls.add_argument("--since")
    ls.add_argument("--box")
    ls.add_argument("--project", "-p")
    ls.add_argument("--json", action="store_true")
    st = actions.add_parser("stats", help="sessions, turns, tokens and long-context share per week, box or project")
    st.add_argument("--since")
    st.add_argument("--by", choices=["week", "box", "project"], default="week")
    st.add_argument("--json", action="store_true")

    p = command("night", cmd_night, "Unattended agent work: pre-flight check, watchdog and thread states. Reads only; changes no system setting.")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True
    c = actions.add_parser("check", help="pre-flight for this box: sleep, power, memory for the planned agents, network, disk, threads that wait for you. Exit 1 on a fail")
    c.add_argument("--hours", type=float, default=8, help="how long the work runs unattended (default 8)")
    c.add_argument("--agents", type=int, help="agents that will run on this box at once, delegates included (default: those with a run going now)")
    c.add_argument("--need", action="append", default=[], metavar="CMD", help="a command that must exit 0, proving something the night needs (a sign-in, a tool); repeatable")
    c.add_argument("--peer", action="append", default=[], metavar="HOST[:PORT]", help="another machine that must answer; repeatable")
    c.add_argument("--json", action="store_true")
    w = actions.add_parser("watch", help="watchdog: resume threads whose run failed, report what needs a person. Runs in the foreground until --hours pass")
    w.add_argument("--thread", action="append", required=True, metavar="NAME", help="a thread to watch: its id, the start of its id, or a piece of its title; repeatable")
    w.add_argument("--hours", type=float, default=8, help="how long to watch (default 8)")
    w.add_argument("--interval", type=int, default=60, help="seconds between polls (default 60)")
    w.add_argument("--no-resume", action="store_true", help="report failed runs instead of sending them a message to continue")
    w.add_argument("--keep-going", action="store_true", help="remind a thread that ended before the end of its standing mandate")
    w.add_argument("--max-messages", type=int, default=4, help="messages of each kind (to continue, reminders) a thread gets in one night before it becomes an alert (default 4)")
    w.add_argument("--peer", action="append", default=[], metavar="HOST[:PORT]", help="another machine to watch: an alert when it stops answering for three minutes; repeatable")
    w.add_argument("--awake", action="store_true", help="hold this box awake while the watch runs (caffeinate on macOS, systemd-inhibit on Linux)")
    w.add_argument("--wake", action="store_true", help="exit 3 at the first alert, so a thread running this in the background wakes")
    w.add_argument("--dry-run", action="store_true", help="log what would be sent; send nothing")
    w.add_argument("--log", help="log file (default: a new file under ~/.cache/jass/night)")
    s = actions.add_parser("status", help="one line per thread: working, delegated, background, blocked, failed, stuck, stopped or ended")
    s.add_argument("--thread", action="append", default=[], metavar="NAME", help="default: threads active in the last 24 hours")

    p = command("heavy", cmd_heavy, "Run one heavy command at a time on this box, under a memory cap, so an overrun kills the command and not the agent.")
    p.add_argument("--mem", type=int, metavar="MB", help="memory cap (default: night.heavy_mb of the box config, else a quarter of memory; 0 for none)")
    p.add_argument("cmd", nargs=argparse.REMAINDER, metavar="-- command", help="the command and its arguments")

    p = command("board", cmd_board, "Vercel Board tasks run as T3 Code threads: queue a task, work the queue on this box, list what is open, show the board in a browser.")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True
    w = actions.add_parser("work", help="claim ready tasks for this box's projects and run each as a T3 thread, settling it when the thread is idle. Runs in the foreground")
    w.add_argument("--project", action="append", metavar="KEY", help="only this project of board.projects; repeatable")
    w.add_argument("--workers", type=int, help="tasks this box works at once (default: board.workers, 1)")
    w.add_argument("--once", action="store_true", help="work one task, or none when nothing is ready, then exit")
    w.add_argument("--wait-minutes", type=float, default=5, help="how long one wait for a ready task lasts before the queue is checked again; a crashed worker's task, whose lease ran out, is picked up at the next check (default 5)")
    w.add_argument("--log", help="log file (default: ~/.cache/jass/board/work-<date>.log)")
    a = actions.add_parser("add", help="queue a task for a project; prints its id")
    a.add_argument("project", help="project key, as in board.projects")
    a.add_argument("text", nargs="?", help="the brief, written for an agent with no memory: what to do, scope, and how to prove it is done (- or nothing: stdin)")
    a.add_argument("--base", help="ref the worktree starts from, or none (default: board.base)")
    a.add_argument("--model", help="model slug for the thread (needs --provider)")
    a.add_argument("--provider", help="provider instance id for the thread")
    a.add_argument("--effort", help="reasoning effort option for the thread")
    a.add_argument("--needs", action="append", metavar="ID", help="a task that must complete first; repeatable")
    a.add_argument("--label", action="append", metavar="K=V", help="extra label; repeatable")
    a.add_argument("--for", dest="for_profile", metavar="PROFILE", help="only this worker profile may claim it")
    a.add_argument("--as", dest="as_profile", default="lead", metavar="PROFILE", help="board profile that owns the task (default lead)")
    s = actions.add_parser("status", help="open tasks with their state and T3 thread")
    s.add_argument("project", nargs="?", help="project key (default: all)")
    s.add_argument("--all", action="store_true", help="include completed, failed and canceled tasks")
    s.add_argument("--as", dest="as_profile", default="lead", metavar="PROFILE")
    s.add_argument("--json", action="store_true")
    o = actions.add_parser("open", help="show the board in a browser, as you, through @vercel/board inspect on 127.0.0.1; runs until Ctrl-C")
    o.add_argument("--port", type=int, help="port to listen on (default: a free one; 4747 over SSH)")
    o.add_argument("--no-open", action="store_true", help="print the address instead of opening a browser")

    p = command("sync", cmd_sync, "Export sessions, refresh the box card, commit, pull and push the lore.")
    p.add_argument("--quick", action="store_true", help="skip the session export when the last one was under 6 hours ago")
    p.add_argument("--first-push", action="store_true", help="publish this lore to an empty remote; refused once the remote has a branch")
    return top


def parse(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line. `jass board add` also takes its brief after options (`add shop --needs 12 "brief"`),
    which argparse alone refuses for an optional positional."""
    top = parser()
    args, extra = top.parse_known_args(argv)
    if extra:
        if (getattr(args, "command", None), getattr(args, "action", None)) == ("board", "add") and args.text is None \
                and len(extra) == 1 and not extra[0].startswith("-"):
            args.text = extra[0]
        else:
            top.error(f"unrecognized arguments: {' '.join(extra)}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    try:
        return args.func(args)
    except JassError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
