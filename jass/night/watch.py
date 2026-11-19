"""`jass night watch`: a watchdog for unattended T3 Code threads, with no model in the loop.

Once a minute it reads the state of the watched threads, the network and the memory. A thread whose
run failed is sent a message to continue once the cause has passed; with `--keep-going`, a thread that
ended before the deadline is reminded of its standing mandate. Messages go through T3's own
`t3 thread send`, which needs no token, only the same user account. What it cannot handle (a thread that
waits for the owner, a usage limit, a thread that keeps failing) is an alert: one line on stdout and
in the log, and with `--wake` the watchdog exits 3 so that a thread running it in the background wakes.

`Watch.step` holds every decision and touches nothing but the `Box` it is given, so tests drive it
with a fake box and clock.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from jass import JassError, paths
from jass.night import probe, t3state

LEAD = "Automatic message from the night watchdog (`jass night watch`); no person is reading."
RESUME = {
    "network": "Your last run failed because the provider could not be reached. The network is back.",
    "memory": "Your last run was killed because this machine ran out of memory. Background commands it had started died with it. "
              "From now on run every heavy command (build, test suite, install, sweep) through `jass heavy -- <command>`, one at a time, with low worker counts.",
    "other": "Your last run failed.",
    "stuck": "A queued message of yours never started after the run before it died.",
    "limit": "Your last run stopped at a usage limit, and the limit has reset.",
}
CONTINUE = "Continue from where you stopped, with the same task and rules. Read your report file and `git status` first, and redo nothing that is already saved."
KEEP_GOING = ("Your turn ended and nothing is running for you, but your brief runs until {until}. Apply your standing mandate: write the next list of the "
              "most valuable things left in your lane and work down it. Set no timer and wait for no clock time. If every remaining item needs the owner, "
              "say so in one line and stop.")
FINE = ("working", "delegated", "background")


def utc(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%H:%M UTC")


class Box:
    """The watchdog's view of the machine. Everything that reads or acts goes through here."""

    def __init__(self, store: Path, t3: str = "t3") -> None:
        self.store, self.t3 = store, t3
        self.base = store.parent.parent  # <base>/userdata/statev2.sqlite

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def threads(self, ids: list[str]) -> list[dict]:
        return t3state.snapshot(self.store, ids)

    def send(self, thread_id: str, message: str) -> str:
        """Send `message` to the thread through `t3 thread send`. Returns an empty string, or why it failed."""
        if not shutil.which(self.t3):
            return f"`{self.t3}` is not on PATH"
        try:
            done = subprocess.run([self.t3, "thread", "send", "--base-dir", str(self.base), thread_id, "-"],
                                  input=message, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            return str(error)
        return "" if done.returncode == 0 else (done.stderr or done.stdout).strip()[:200] or f"exit {done.returncode}"

    def online(self) -> bool:
        return probe.online()

    def available_mb(self) -> int | None:
        return probe.memory().get("available_mb")

    def reach(self, peer: str) -> bool:
        return probe.reach(peer)


class Watch:
    def __init__(self, box: Box, threads: list[dict], until: dt.datetime, *, resume: bool = True, keep_going: bool = False,
                 max_nudges: int = 4, peers: list[str] | None = None, low_mb: int = 2000, need_mb: int = 3000,
                 dry_run: bool = False, emit=print) -> None:
        self.box, self.until = box, until
        self.names = {t["id"]: t["title"] or t["id"][:16] for t in threads}
        self.resume, self.keep_going, self.max_nudges, self.dry_run = resume, keep_going, max_nudges, dry_run
        self.peers, self.low_mb, self.need_mb, self.emit = peers or [], low_mb, need_mb, emit
        self.seen: dict[str, tuple[str, dt.datetime]] = {}   # thread -> (state and run, first seen)
        self.nudged: dict[str, list[dt.datetime]] = {}        # thread -> when messages were sent
        self.alerted: set[tuple[str, str]] = set()
        self.flags: dict[str, bool] = {}                       # last value of net, memory and each peer
        self.alerts = 0
        self.settle = dt.timedelta(minutes=3)                  # a failed or stuck thread must stay so this long
        self.idle = dt.timedelta(minutes=10)                   # an ended thread may still start its next turn
        self.gap = dt.timedelta(minutes=10)                    # between two messages to one thread
        self.wind_down = dt.timedelta(minutes=30)              # no reminders this close to the end

    def log(self, kind: str, text: str) -> None:
        self.emit(f"{self.box.now().strftime('%Y-%m-%d %H:%M:%SZ')} {kind:6} {text}")

    def alert(self, thread_id: str, reason: str, text: str) -> None:
        """One line per thread and reason, however long the reason lasts."""
        if (thread_id, reason) not in self.alerted:
            self.alerted.add((thread_id, reason))
            self.alerts += 1
            self.log("ALERT", f"{self.names.get(thread_id, thread_id)}: {text}")

    def flag(self, name: str, good: bool, bad_text: str, good_text: str) -> bool:
        """Log a change of one yes/no fact (network, memory, a peer); quiet while it holds."""
        if self.flags.get(name, True) != good:
            self.log("box", good_text if good else bad_text)
        self.flags[name] = good
        return good

    def nudge(self, thread: dict, message: str, why: str) -> None:
        sent = self.nudged.setdefault(thread["id"], [])
        now = self.box.now()
        if sent and now - sent[-1] < self.gap:
            return
        if len(sent) >= self.max_nudges:
            self.alert(thread["id"], "gave-up", f"{thread['state']} again after {len(sent)} automatic messages; it needs a person ({thread['detail']})")
            return
        sent.append(now)
        if self.dry_run:
            self.log("would", f"send to {self.names[thread['id']]}: {why}")
            return
        problem = self.box.send(thread["id"], f"{LEAD} {message}")
        if problem:
            self.alert(thread["id"], "send", f"could not send a message ({problem}); it stays {thread['state']}")
        else:
            self.log("sent", f"{self.names[thread['id']]}: {why}")

    def step(self) -> None:
        now = self.box.now()
        online = self.flag("net", self.box.online(), "network DOWN: the provider cannot be reached", "network back")
        available = self.box.available_mb()
        roomy = self.flag("memory", available is None or available >= self.low_mb,
                          f"memory low: {available} MB available; agents may be killed", f"memory recovered: {available} MB available")
        for peer in self.peers:
            self.flag(f"peer {peer}", self.box.reach(peer), f"{peer} stopped answering: asleep, off or off the network; its threads are frozen", f"{peer} answers again")
        try:
            threads = self.box.threads(list(self.names))
        except sqlite3.Error as error:  # the store is busy or mid-write: try again at the next poll
            self.log("box", f"could not read the T3 store this time ({error})")
            return
        for thread in threads:
            key = f"{thread['state']}#{thread['run']}"
            if self.seen.get(thread["id"], ("", now))[0] != key:
                self.seen[thread["id"]] = (key, now)
            held = now - self.seen[thread["id"]][1]
            state = thread["state"]
            if state in FINE:
                continue
            if state == "blocked":
                self.alert(thread["id"], key, f"{thread['detail']}. Nothing here can answer it")
            elif state == "stopped":
                self.alert(thread["id"], key, f"{thread['detail']}; left alone, a person or another agent stopped it")
            elif state in ("failed", "stuck") and held >= self.settle:
                self.failed(thread, now, online, roomy and (available is None or available >= self.need_mb))
            elif state == "ended" and held >= self.idle:
                self.ended(thread, now)

    def failed(self, thread: dict, now: dt.datetime, online: bool, room: bool) -> None:
        kind = "stuck" if thread["state"] == "stuck" else thread["failure"] or "other"
        if not self.resume:
            self.alert(thread["id"], f"failed#{thread['run']}", f"{thread['detail']} ({kind}); not resumed, --no-resume is set")
        elif kind == "limit" and not (thread["reset"] and thread["reset"][:16] < (now - self.settle).strftime("%Y-%m-%dT%H:%M")):
            when = f"resets at {thread['reset'][11:16]} UTC and is resumed then" if thread["reset"] else "no reset time recorded, so it is not resumed"
            self.alert(thread["id"], "limit", f"{thread['detail']}. A usage limit: {when}")
        elif not online:
            return  # wait for the network; the change is logged once
        elif kind == "memory" and not room:
            return  # resuming into a full box gets it killed again
        else:
            self.nudge(thread, f"{RESUME[kind]} {CONTINUE}", f"continue after a {kind} failure ({thread['detail']})")

    def ended(self, thread: dict, now: dt.datetime) -> None:
        if now >= self.until - self.wind_down:
            return
        if self.keep_going:
            self.nudge(thread, KEEP_GOING.format(until=utc(self.until)), f"reminder of the standing mandate ({thread['detail']})")
        else:
            self.alert(thread["id"], f"ended#{thread['run']}", f"ended at {thread['since'][11:16] or '?'} UTC with the night running until {utc(self.until)}")

    def summary(self) -> str:
        sent = sum(len(times) for times in self.nudged.values())
        return f"{sent} automatic message{'s' if sent != 1 else ''} sent, {self.alerts} alert{'s' if self.alerts != 1 else ''}"


def keep_awake(seconds: int):
    """Hold this box awake for as long as this process lives. Returns the helper process, or None."""
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        argv = ["caffeinate", "-ims", "-w", str(os.getpid())]
    elif shutil.which("systemd-inhibit"):
        argv = ["systemd-inhibit", "--what=idle:sleep", "--who=jass night watch", "--why=agents are working unattended", "sleep", str(seconds)]
    else:
        return None
    try:
        helper = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        helper.wait(timeout=1)  # a helper that ends at once was refused, as systemd-inhibit is without a polkit rule
    except subprocess.TimeoutExpired:
        return helper
    except OSError:
        pass
    return None


def log_path(now: dt.datetime) -> Path:
    return paths.private_dir(paths.cache_dir() / "night") / f"watch-{now.strftime('%Y%m%d-%H%M')}.log"


def run(cfg: dict, args) -> int:
    box = Box(t3state.store_path(cfg))
    threads = t3state.pick(box.threads(None), args.thread)
    start = box.now()
    until = start + dt.timedelta(hours=args.hours)
    with paths.Lock(paths.cache_dir() / "night" / "watch.lock") as free:
        if not free:
            raise JassError("A night watch is already running on this box. Stop it first; two would each message the threads.")
        path = Path(args.log).expanduser() if args.log else log_path(start)
        with open(path, "a") as handle:
            def emit(line: str) -> None:
                print(line, flush=True)
                handle.write(line + "\n")
                handle.flush()

            cap = int(cfg.get("night", {}).get("heavy_mb") or 3000)
            watch = Watch(box, threads, until, resume=not args.no_resume, keep_going=args.keep_going, max_nudges=args.max_messages,
                          peers=args.peer, need_mb=cap, dry_run=args.dry_run, emit=emit)
            helper = keep_awake(int(args.hours * 3600) + 60) if args.awake else None
            watch.log("start", f"watching {len(threads)} thread{'s' if len(threads) != 1 else ''} until {utc(until)}: "
                      + "; ".join(watch.names.values()) + f". Log: {paths.tilde(path)}"
                      + ("" if not args.awake else ". Holding this box awake" if helper else ". COULD NOT hold this box awake: caffeinate or systemd-inhibit is missing or was refused"))
            try:
                while box.now() < until:
                    before = watch.alerts
                    watch.step()
                    if args.wake and watch.alerts > before:
                        watch.log("stop", f"woke the caller. {watch.summary()}")
                        return 3
                    box.sleep(max(5, args.interval))
            except KeyboardInterrupt:
                watch.log("stop", f"interrupted. {watch.summary()}")
                return 130
            finally:
                if helper:
                    helper.terminate()
            watch.log("stop", f"the night is over. {watch.summary()}")
    return 0


def status(cfg: dict, wanted: list[str], hours: float = 24) -> str:
    """One line per thread, for a quick look or a watcher's tick."""
    box = Box(t3state.store_path(cfg))
    threads = box.threads(None)
    if wanted:
        threads = t3state.pick(threads, wanted)
    else:
        cutoff = (box.now() - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")
        threads = [t for t in threads if t["state"] in (*FINE, "blocked", "stuck") or t["since"][:16] >= cutoff]
    lines = []
    for thread in sorted(threads, key=lambda t: t["since"], reverse=True):
        lines.append(f"{thread['state']:10} {thread['title'][:44]:44} {thread['since'][5:16].replace('T', ' ')}  {thread['detail']}")
    available = box.available_mb()
    lines.append(f"box: network {'up' if box.online() else 'DOWN'}" + (f", {available} MB available" if available is not None else ""))
    return "\n".join(lines) + "\n"
