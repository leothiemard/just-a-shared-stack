"""`jass night watch`: a watchdog for unattended T3 Code threads, with no model in the loop.

Once a minute it reads the state of the watched threads, the network and the memory. A thread whose
run failed is sent a message to continue once the cause has passed; with `--keep-going`, a thread that
ended before the deadline is reminded of its standing mandate. Messages go through T3's own
`t3 thread send`, which needs no token, only the same user account. What it cannot handle (a thread that
waits for the owner, a usage limit, a refused sign-in, a thread that keeps failing) is an alert: one line
on stdout and in the log, and with `--wake` the watchdog exits 3 so that a thread running it in the
background wakes.

What it sent and which alerts it raised are kept in a small state file for the night, so a watch that is
started again neither repeats an alert nor starts the message count from zero.

`Watch.step` holds every decision and touches nothing but the `Box` it is given, so tests drive it
with a fake box and clock.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import shutil
import signal
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
STATE_HOURS = 16  # how long sent messages and raised alerts count: one night


def utc(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%H:%M UTC")


def moment(text: str) -> dt.datetime | None:
    """A timestamp of T3's store or of the state file as an aware datetime."""
    try:
        found = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=dt.timezone.utc)


def load_state(path: Path, now: dt.datetime) -> dict:
    """What earlier runs of the watch sent and alerted on this night.

    A night ends at the deadline of the watch that wrote the state: a watch started again before it carries
    on, one started after it begins a new night with nothing. Entries older than `STATE_HOURS` go in any case."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    deadline = moment(raw.get("until", ""))
    if deadline and deadline <= now:
        raw = {}
    cutoff = now - dt.timedelta(hours=STATE_HOURS)

    def fresh(text) -> bool:
        when = moment(text)
        return bool(when) and when >= cutoff

    state = {"sent": {}, "reminded": {}, "alerts": {}}
    for thread, kinds in (raw.get("sent") or {}).items():
        kept = {kind: [t for t in times if fresh(t)] for kind, times in kinds.items() if kind in ("resume", "remind")}
        if any(kept.values()):
            state["sent"][thread] = kept
    state["reminded"] = {thread: mark for thread, mark in (raw.get("reminded") or {}).items() if fresh(mark.get("at"))}
    state["alerts"] = {key: when for key, when in (raw.get("alerts") or {}).items() if fresh(when)}
    return state


def save_state(path: Path, state: dict) -> None:
    scratch = path.with_suffix(".tmp")
    scratch.write_text(json.dumps(state, indent=1))
    os.replace(scratch, path)


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
                 dry_run: bool = False, t3_resumes: bool = False, state: dict | None = None, keep=None, emit=print) -> None:
        self.box, self.until = box, until
        self.names = {t["id"]: t["title"] or t["id"][:16] for t in threads}
        self.resume, self.keep_going, self.max_nudges, self.dry_run = resume, keep_going, max_nudges, dry_run
        self.peers, self.low_mb, self.need_mb, self.emit = peers or [], low_mb, need_mb, emit
        self.t3_resumes = t3_resumes                           # T3 Code resumes a thread itself after a usage limit resets
        self.state = state if state is not None else {"sent": {}, "reminded": {}, "alerts": {}}
        self.keep = keep or (lambda: None)                     # called when the state changed, to save it
        self.seen: dict[str, tuple[str, dt.datetime]] = {}   # thread -> (state and run, first seen)
        self.tried: dict[str, dt.datetime] = {}                # thread -> last attempt to send, delivered or not
        self.noted: dict[tuple, dt.datetime] = {}              # (thread, run) -> when it began to wait for the box
        self.broken: dt.datetime | None = None                 # since when every poll has failed
        self.flags: dict[str, bool] = {}                       # last value of net, memory, the store, each peer and thread
        self.since: dict[str, dt.datetime] = {}                # when a flag last turned bad
        self.alerts = self.messages = 0                        # raised and sent by this process
        self.settle = dt.timedelta(minutes=3)                  # a failed or stuck thread must stay so this long
        self.idle = dt.timedelta(minutes=10)                   # an ended thread may still start its next turn
        self.gap = dt.timedelta(minutes=10)                    # between two messages to one thread
        self.wind_down = dt.timedelta(minutes=30)              # no reminders this close to the end
        self.patience = dt.timedelta(minutes=30)               # a failed thread that waits this long for the box is an alert
        self.quick = dt.timedelta(minutes=5)                   # a reminded run this short had nothing left to do
        self.limit_grace = dt.timedelta(minutes=15)            # T3's own resume after a limit gets this long first

    def log(self, kind: str, text: str) -> None:
        self.emit(f"{self.box.now().strftime('%Y-%m-%d %H:%M:%SZ')} {kind:6} {text}")

    def alert(self, subject: str, reason: str, text: str) -> None:
        """One line per subject and reason this night, however long it lasts and however often the watch restarts."""
        key = f"{subject}|{reason}"
        if key not in self.state["alerts"]:
            self.state["alerts"][key] = self.box.now().isoformat()
            self.alerts += 1
            self.log("ALERT", f"{self.names.get(subject, subject)}: {text}")
            self.keep()

    def clear(self, subject: str, reason: str) -> None:
        """Forget an alert whose cause is over, so the next time it happens it is raised again."""
        gone = [key for key in self.state["alerts"] if key == f"{subject}|{reason}" or key.startswith(f"{subject}|{reason}#")]
        for key in gone:
            del self.state["alerts"][key]
        if gone:
            self.keep()

    def flag(self, name: str, good: bool, bad_text: str, good_text: str) -> bool:
        """Log a change of one yes/no fact (network, memory, the store, a peer); quiet while it holds."""
        if self.flags.get(name, True) != good:
            self.log("box", good_text if good else bad_text)
            self.since[name] = self.box.now()
        self.flags[name] = good
        return good

    def bad_for(self, name: str) -> dt.timedelta:
        return dt.timedelta(0) if self.flags.get(name, True) else self.box.now() - self.since[name]

    def nudge(self, thread: dict, kind: str, message: str, why: str) -> None:
        """Send one message of `kind` (`resume` or `remind`); each kind has its own count, and only a delivered message counts."""
        now, name = self.box.now(), self.names[thread["id"]]
        times = self.state["sent"].setdefault(thread["id"], {})
        sent = times.setdefault(kind, [])
        earlier = [moment(t) for kind_times in times.values() for t in kind_times] + [self.tried.get(thread["id"])]
        if any(when and now - when < self.gap for when in earlier):
            return
        if len(sent) >= self.max_nudges:
            what = "resumed" if kind == "resume" else "reminded"
            self.alert(thread["id"], f"gave-up-{kind}", f"{thread['state']} again after being {what} {len(sent)} times; it needs a person ({thread['detail']})")
            return
        self.tried[thread["id"]] = now
        if self.dry_run:
            sent.append(now.isoformat())
            self.log("would", f"send to {name}: {why}")
            return
        problem = self.box.send(thread["id"], f"{LEAD} {message}")
        if problem:
            self.alert(thread["id"], f"send#{thread['run']}", f"could not send a message ({problem}); it stays {thread['state']} and the watch tries again every {self.gap.seconds // 60} minutes")
            return
        sent.append(now.isoformat())
        self.messages += 1
        if kind == "remind":
            self.state["reminded"][thread["id"]] = {"run": thread["run"], "at": now.isoformat()}
        self.log("sent", f"{name}: {why}")
        self.keep()

    def poll(self) -> None:
        """One `step` that cannot end the watch: a watchdog that dies at 3 a.m. watches nothing."""
        try:
            self.step()
        except Exception as error:
            now = self.box.now()
            self.broken = self.broken or now
            self.alert("this box", f"error#{type(error).__name__}", f"the watch hit an error and goes on: {error!r}")
            if now - self.broken >= self.settle:
                self.alert("this box", "blind", f"every poll has failed since {utc(self.broken)} ({error!r}); no thread is watched")
        else:
            self.broken = None
            self.clear("this box", "blind")

    def step(self) -> None:
        now = self.box.now()
        online = self.flag("net", self.box.online(), "network DOWN: the provider cannot be reached", "network back")
        available = self.box.available_mb()
        self.flag("memory", available is None or available >= self.low_mb,
                          f"memory low: {available} MB available; agents may be killed", f"memory recovered: {available} MB available")
        for peer in self.peers:
            if self.flag(f"peer {peer}", self.box.reach(peer), f"{peer} stopped answering: asleep, off or off the network; its threads are frozen", f"{peer} answers again"):
                self.clear(peer, "quiet")
            elif self.bad_for(f"peer {peer}") >= self.settle:
                self.alert(peer, "quiet", f"stopped answering at {utc(self.since[f'peer {peer}'])}; its threads are frozen and nothing here can wake it")
        try:
            threads = self.box.threads(list(self.names))
        except (sqlite3.Error, JassError) as error:  # busy, mid-write, moved or changed by a T3 Code update: keep trying
            self.flag("store", False, f"could not read the T3 store ({error}); trying again at each poll", "")
            if self.bad_for("store") >= self.settle:
                self.alert("this box", "store", f"the T3 store cannot be read since {utc(self.since['store'])} ({error}); no thread is watched until it can")
            return
        self.flag("store", True, "", "the T3 store reads again")
        self.clear("this box", "store")
        present = {thread["id"] for thread in threads}
        for thread_id, name in self.names.items():
            self.flag(f"thread {thread_id}", thread_id in present, f"{name} is no longer in the store (archived or deleted); it is not watched", f"{name} is back in the store")
        for thread in threads:
            key = f"{thread['state']}#{thread['run']}"
            if self.seen.get(thread["id"], ("", now))[0] != key:
                self.seen[thread["id"]] = (key, now)
            held = now - self.seen[thread["id"]][1]
            state = thread["state"]
            if state != "blocked":
                self.clear(thread["id"], "blocked")  # its request was answered: the next one is news again
            if state in FINE:
                continue
            if state == "blocked":  # keyed by the requests themselves: a new one is news even in the same run
                self.alert(thread["id"], f"blocked#{thread.get('asks') or thread['run']}", f"{thread['detail']}. Nothing here can answer it")
            elif state == "stopped":
                self.alert(thread["id"], key, f"{thread['detail']}; left alone, a person or another agent stopped it")
            elif state in ("failed", "stuck") and held >= self.settle:
                self.failed(thread, now, online, available)
            elif state == "ended" and held >= self.idle:
                self.ended(thread, now)

    def failed(self, thread: dict, now: dt.datetime, online: bool, available: int | None) -> None:
        kind = "stuck" if thread["state"] == "stuck" else thread["failure"] or "other"
        run = thread["run"]
        if not self.resume:
            self.alert(thread["id"], f"failed#{run}", f"{thread['detail']} ({kind}); not resumed, --no-resume is set")
        elif kind == "auth":
            self.alert(thread["id"], f"auth#{run}", f"{thread['detail']}. The provider refused the sign-in; not resumed, the owner has to sign in again")
        elif kind == "limit":
            reset = moment(thread["reset"]) if thread["reset"] else None
            grace = self.limit_grace if self.t3_resumes else self.settle
            if reset and now >= reset + grace and not online:
                self.wait(thread, "the network")
            elif reset and now >= reset + grace:
                self.nudge(thread, "resume", f"{RESUME[kind]} {CONTINUE}", f"continue after the usage limit reset ({thread['detail']})")
            elif reset:
                who = f"T3 Code resumes it then, and the watch does if it has not started {grace.seconds // 60} minutes later" if self.t3_resumes else "the watch resumes it then"
                self.alert(thread["id"], f"limit#{run}", f"{thread['detail']}. A usage limit: it resets at {utc(reset)}; {who}")
            else:
                self.alert(thread["id"], f"limit#{run}", f"{thread['detail']}. A usage limit with no reset time recorded, so it is not resumed")
        elif not online:
            self.wait(thread, "the network")
        elif kind == "memory" and available is not None and available < max(self.need_mb, self.low_mb):
            self.wait(thread, f"memory ({available} MB available, {max(self.need_mb, self.low_mb)} MB wanted before a killed thread starts again)")
        else:
            self.nudge(thread, "resume", f"{RESUME[kind]} {CONTINUE}", f"continue after a {kind} failure ({thread['detail']})")

    def wait(self, thread: dict, what: str) -> None:
        """A failed thread that is not resumed yet: say so once, and raise an alert when the wait gets long."""
        now = self.box.now()
        if (thread["id"], thread["run"]) not in self.noted:
            self.noted[(thread["id"], thread["run"])] = now
            self.log("wait", f"{self.names[thread['id']]}: {thread['detail']}; not resumed yet, waiting for {what}")
        waited = now - self.noted[(thread["id"], thread["run"])]
        if waited >= self.patience:
            self.alert(thread["id"], f"waiting#{thread['run']}", f"failed and not resumed for {int(waited.total_seconds() // 60)} minutes: still waiting for {what}")

    def ended(self, thread: dict, now: dt.datetime) -> None:
        if now >= self.until - self.wind_down:
            return
        if not self.keep_going:
            self.alert(thread["id"], f"ended#{thread['run']}", f"ended at {thread['since'][11:16] or '?'} UTC with the night running until {utc(self.until)}")
            return
        mark = self.state["reminded"].get(thread["id"])
        reminded, finished = (moment(mark["at"]), moment(thread["since"])) if mark else (None, None)
        if mark and reminded and finished and thread["run"] > mark["run"] and reminded <= finished < reminded + self.quick:
            self.alert(thread["id"], f"finished#{thread['run']}", f"answered the reminder within {self.quick.seconds // 60} minutes and ended: its lane is finished or all that is left needs the owner. No more reminders")
            return
        self.nudge(thread, "remind", KEEP_GOING.format(until=utc(self.until)), f"reminder of the standing mandate ({thread['detail']})")

    def summary(self) -> str:
        return f"{self.messages} automatic message{'s' if self.messages != 1 else ''} sent, {self.alerts} alert{'s' if self.alerts != 1 else ''}"


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


def quiet_close(handle) -> None:
    """Close the log; lines a full disk refused are still in its buffer, and must not turn the exit code into an error."""
    with contextlib.suppress(OSError, ValueError):
        handle.close()


def stop_on(signals) -> dict:
    """Make these signals end the loop like Ctrl-C, so the watch logs its last line and lets the box sleep again."""
    def stop(_number, _frame):
        raise KeyboardInterrupt

    previous = {}
    for number in signals:
        try:
            previous[number] = signal.signal(number, stop)
        except (ValueError, OSError):  # not the main thread
            pass
    return previous


def run(cfg: dict, args) -> int:
    store = t3state.store_path(cfg)
    t3 = shutil.which("t3")
    box = Box(store, t3 or "t3")
    threads = t3state.pick(box.threads(None), args.thread)
    if not t3 and not args.dry_run and (not args.no_resume or args.keep_going):
        raise JassError("`t3` is not on PATH here, so the watch could not message a thread. Start it where `t3` runs "
                        "(under systemd-run, add `--setenv=PATH=\"$PATH\"`), or pass --dry-run.")
    start = box.now()
    until = start + dt.timedelta(hours=args.hours)
    folder = paths.cache_dir() / "night"
    with paths.Lock(folder / "watch.lock") as free:
        if not free:
            raise JassError("A night watch is already running on this box. Stop it first; two would each message the threads.")
        path = Path(args.log).expanduser() if args.log else log_path(start)
        path.parent.mkdir(parents=True, exist_ok=True)
        state_file = paths.private_dir(folder) / "watch-state.json"
        state = load_state(state_file, start)
        state["until"] = until.isoformat()
        with contextlib.ExitStack() as stack:
            handle = open(path, "a")
            stack.callback(quiet_close, handle)
            said = set()

            def careful(what: str, action) -> None:
                """A full disk or a closed terminal must not end the watch: say it once where it can still be said, and go on."""
                try:
                    action()
                except (OSError, ValueError) as error:
                    if what not in said:
                        said.add(what)
                        with contextlib.suppress(OSError, ValueError):
                            print(f"jass night watch: cannot write {what} ({error}); the watch goes on", file=sys.stderr, flush=True)

            def emit(line: str) -> None:
                careful("to the terminal", lambda: print(line, flush=True))
                careful("its log", lambda: (handle.write(line + "\n"), handle.flush()))

            def keep() -> None:
                if not args.dry_run:
                    careful("its state file", lambda: save_state(state_file, state))

            night = cfg.get("night", {})
            cap = int(night.get("heavy_mb") or 3000)  # room one heavy command may take, also where the box sets no cap
            watch = Watch(box, threads, until, resume=not args.no_resume, keep_going=args.keep_going, max_nudges=args.max_messages,
                          peers=args.peer, low_mb=int(night.get("reserve_mb") or 2000), need_mb=cap, dry_run=args.dry_run, t3_resumes=bool(probe.t3(box.base).get("resume_after_limit")),
                          state=state, keep=keep, emit=emit)
            helper = keep_awake(int(args.hours * 3600) + 60) if args.awake else None
            earlier = sum(len(times) for kinds in state["sent"].values() for times in kinds.values())
            watch.log("start", f"watching {len(threads)} thread{'s' if len(threads) != 1 else ''} until {utc(until)}: "
                      + "; ".join(watch.names.values()) + f". Log: {paths.tilde(path)}"
                      + (f". Earlier this night: {earlier} message{'s' if earlier != 1 else ''} sent, {len(state['alerts'])} alert{'s' if len(state['alerts']) != 1 else ''} raised; they are not repeated" if earlier or state["alerts"] else "")
                      + ("" if not args.awake else ". Holding this box awake" if helper else ". COULD NOT hold this box awake: caffeinate or systemd-inhibit is missing or was refused"))
            previous = stop_on([signal.SIGTERM, signal.SIGHUP])
            try:
                while box.now() < until:
                    before = watch.alerts
                    watch.poll()
                    if args.wake and watch.alerts > before:
                        watch.log("stop", f"woke the caller. {watch.summary()}")
                        return 3
                    box.sleep(max(5, args.interval))
            except KeyboardInterrupt:
                watch.log("stop", f"interrupted or told to stop. {watch.summary()}")
                return 130
            finally:
                for number, handler in previous.items():
                    signal.signal(number, handler if handler is not None else signal.SIG_DFL)
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
