"""`jass board`: work Vercel Board tasks as T3 Code threads.

The board is the queue and T3 Code is where the work runs and where the owner watches it. A worker on
each box claims a ready task from a `build/<project>` channel, starts one T3 thread for it (in a new git
worktree when the project is a repository), and holds the claim while the thread works. It settles the
task only when the thread is truly idle: no run going, no question waiting for an answer, no background
command or delegate still working, and that for `settle_seconds` in a row. `t3 thread start --wait`
alone is not enough: it returns as soon as a turn ends, background work included, and it exits 2 the
moment the thread asks a question.

While the thread works, the worker:

- renews the claim, so a crashed worker's task returns to the queue when its lease runs out;
- labels the task `t3.waiting=answer` and says so in the task's board thread while T3 waits for an
  answer, and blocks the task only after `answer_hours`;
- forwards replies that people (anyone but a `worker-` profile) post in the task's board thread into the
  T3 thread, so the owner can steer from either side.

The thread's id and box are labels on the task (`t3.thread`, `t3.box`). When the same box claims the
task again (after a crash, a release, an unblock or a reopen), the worker attaches to that thread instead
of starting a new one.

The agent in the thread never touches the board: its brief asks it to end with a one-line result, or a
first line starting `FAILED:`. The worker posts the final answer as a reply and completes or fails the
task with that line.

`Job` holds every decision and touches nothing but the `Board`, `T3` and `Clock` it is given, so tests
drive it with fakes.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from jass import JassError, paths

THREAD, BOX, SEEN, WAITING = "t3.thread", "t3.box", "t3.seen", "t3.waiting"
FINISHED = ("completed", "failed", "canceled")
WORKER_PREFIX = "worker-"
REPLY_MAX = 9000
RESULT_MAX = 240
TITLE_MAX = 72

BRIEF = """You are working board task {task} (channel `{channel}`), as an ordinary T3 Code thread. A worker on this box (`jass board work`) started this thread and watches it; the owner follows it here in T3 Code.

## The task

{text}
{earlier}
## How this thread is handled

- Do the task in this thread, inside this {where}. Commit on this branch when the task changes code; open a pull request only when the task asks for one.
- The worker marks the task done once this thread is idle: no run going, no question waiting and no background command or delegate still working. So finish background work before your final answer, or end your turn and let its completion wake you.
- When you need a decision from the owner, ask it with your multiple-choice question tool (never a secret or sign-in card) and wait. The task stays held while you wait.
- Your final answer is posted to the board. Make its first line the result in one sentence, then add the detail. Start that first line with `FAILED:` only when the task's goal was not reached in the end; trouble you got past along the way is not a failure.
- Do not post to the board yourself and do not run `vercel board` commands; the worker does that.
"""

CONTINUE = "Automatic message from the board worker: your last run did not finish. Continue the board task from where you stopped; check `git status` and redo nothing that is already saved."
FORWARD = "Message from {who} in the task's board thread, forwarded by the board worker:\n\n{text}"


class BoardError(JassError):
    pass


def settings(cfg: dict) -> dict:
    board = dict(cfg.get("board") or {})
    board.setdefault("worker", "")
    board["worker"] = board["worker"] or f"{WORKER_PREFIX}{cfg.get('box') or 'box'}"
    if not board["worker"].startswith(WORKER_PREFIX):
        raise BoardError(f"board.worker {board['worker']!r} must start with {WORKER_PREFIX!r}, so workers never forward each other's replies.")
    return board


def first_json(text: str):
    """The JSON object a CLI printed, skipping anything before it (a blank line, a banner)."""
    start = (text or "").find("{")
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except ValueError:
        return None


def first_line(text: str, limit: int = RESULT_MAX) -> str:
    for line in (text or "").splitlines():
        line = re.sub(r"^[#>*\-\s]+|[*_`]+", "", line).strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return ""


def clip(text: str, limit: int = REPLY_MAX) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "\n\n[cut: the full answer is in the T3 thread]"


def is_worker(message: dict) -> bool:
    return str(((message.get("by") or {}).get("profile")) or "").startswith(WORKER_PREFIX)


def author(message: dict) -> str:
    who = message.get("author") or {}
    name = who.get("name") or (message.get("by") or {}).get("profile") or "someone"
    via = (who.get("via") or {}).get("name")
    return f"{name} via {via}" if via and via != name else name


# --- ports ---------------------------------------------------------------------------------------


class Clock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float, stop: threading.Event | None = None) -> None:
        if stop:
            stop.wait(seconds)
        else:
            time.sleep(seconds)


class Board:
    """`vercel board`, always as one profile, always with `--json`."""

    def __init__(self, profile: str, timeout: int = 120):
        self.profile, self.timeout = profile, timeout

    def run(self, *args: str, timeout: int | None = None) -> tuple[int, dict | None, str]:
        command = ["vercel", "board", *args, "--as", self.profile, "--json"]
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            return 124, None, "timed out"
        except OSError as error:
            raise BoardError(f"Cannot run the Vercel CLI: {error}. Install it with `npm install --global vercel@latest`.")
        data = first_json(done.stdout)
        return done.returncode, data, (done.stdout + done.stderr).strip()

    @staticmethod
    def reason(data: dict | None, raw: str) -> str:
        if isinstance(data, dict) and data.get("reason"):
            return str(data["reason"])
        return raw.splitlines()[-1][:200] if raw else "no output"

    def claim(self, channel: str, lease: str) -> dict | None:
        rc, data, raw = self.run("tasks", "claim", "--ready", channel, "--lease", lease)
        if rc == 0 and data and data.get("task"):
            return data["task"]
        if self.reason(data, raw) in ("nothing_ready", "task_claimed", "channel_not_found"):
            return None
        raise BoardError(f"claim in {channel} failed: {self.reason(data, raw)}")

    def read(self, task_id: str) -> dict:
        rc, data, raw = self.run("read", task_id)
        if rc != 0 or not data or "message" not in data:
            raise BoardError(f"read {task_id} failed: {self.reason(data, raw)}")
        return data

    def settle(self, action: str, task_id: str, *args: str) -> str:
        """Run a `tasks` action on a held task; '' when it worked, else the reason (such as claim_lost)."""
        rc, data, raw = self.run("tasks", action, task_id, *args)
        return "" if rc == 0 else self.reason(data, raw)

    def renew(self, task_id: str, lease: str) -> str:
        return self.settle("renew", task_id, "--lease", lease)

    def reply(self, task_id: str, text: str) -> str:
        rc, data, raw = self.run("reply", task_id, text)
        return "" if rc == 0 else self.reason(data, raw)

    def label(self, task_id: str, labels: dict | None = None, unset: tuple[str, ...] = ()) -> str:
        args = [f"{key}={value}" for key, value in (labels or {}).items()]
        for key in unset:
            args += ["--unset", key]
        rc, data, raw = self.run("label", task_id, *args)
        return "" if rc == 0 else self.reason(data, raw)

    def wait_ready(self, prefix: str, timeout_s: int) -> str:
        """Block until a task under `prefix` turns ready, or the timeout; '' when it woke or timed out normally."""
        rc, data, raw = self.run("wait", prefix, "--label", "board.task=ready", "--timeout", f"{timeout_s}s", timeout=timeout_s + 30)
        if rc == 0 or "Nothing matched" in raw or self.reason(data, raw) == "timeout":
            return ""
        return self.reason(data, raw)

    def ensure_channel(self, channel: str) -> str:
        rc, data, raw = self.run("channels", "create", channel)
        return "" if rc == 0 else self.reason(data, raw)

    def create(self, channel: str, text: str, labels: dict, needs: list[str], for_profile: str | None) -> dict:
        args = ["tasks", "create", channel, "-" if text == "-" else text]
        for key, value in labels.items():
            args += ["--label", f"{key}={value}"]
        for need in needs:
            args += ["--needs", need]
        if for_profile:
            args += ["--for", for_profile]
        rc, data, raw = self.run(*args)
        if rc != 0 or not data or not data.get("task"):
            raise BoardError(f"create in {channel} failed: {self.reason(data, raw)}")
        return data["task"]

    def tasks(self, channel: str, state: str) -> list[dict]:
        rc, data, raw = self.run("tasks", channel, "--state", state)
        if rc != 0 and self.reason(data, raw) == "channel_not_found":
            return []
        if rc != 0 or data is None:
            raise BoardError(f"listing tasks in {channel} failed: {self.reason(data, raw)}")
        return data.get("tasks") or []


class T3:
    """The `t3` CLI of the T3 Code server running on this box."""

    def __init__(self, timeout: int = 120):
        self.timeout = timeout

    def run(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(["t3", *args], input=stdin, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, 124, "", "timed out")
        except OSError as error:
            raise BoardError(f"Cannot run t3: {error}. Is T3 Code installed on this box?")

    def start(self, project: str, title: str, brief: str, worktree: str = "", options: dict | None = None) -> str:
        args = ["thread", "start", "--title", title, "--json"]
        if worktree:
            args += ["--worktree", worktree]
        options = options or {}
        if options.get("provider") and options.get("model"):
            args += ["--provider", options["provider"], "--model", options["model"]]
            for option in options.get("options") or []:
                args += ["--option", option]
        done = self.run(*args, project, "-", stdin=brief)
        data = first_json(done.stdout) or {}
        thread = data.get("threadId") or (data.get("thread") or {}).get("threadId")
        if not thread:
            found = re.search(r"^Thread (\S+)", done.stdout + "\n" + done.stderr, re.M)
            thread = found.group(1) if found else ""
        if done.returncode != 0 or not thread:
            detail = (done.stderr or done.stdout).strip().splitlines()
            raise BoardError(f"t3 thread start failed (exit {done.returncode}): {detail[-1] if detail else 'no output'}")
        return thread

    def show(self, thread: str, messages: int = 0) -> dict | None:
        done = self.run("thread", "show", thread, "--messages", str(messages), "--json")
        data = first_json(done.stdout) if done.returncode == 0 else None
        return data if data and data.get("thread") else None

    def send(self, thread: str, text: str) -> bool:
        return self.run("thread", "send", thread, "-", stdin=text).returncode == 0


# --- one task ------------------------------------------------------------------------------------


def epoch(text) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def thread_state(thread: dict, now: float | None = None, stall_s: float = 0) -> str:
    """`question`, `stalled`, `busy` or `idle`, from `t3 thread show --json`.

    `stalled` is a run that has not changed the thread for `stall_s`: a card T3 does not count as a
    question (a secret or sign-in request), an approval, or a hung tool. Background work and delegates
    can be quiet for long, so only an active run with no pending background work counts."""
    if thread.get("pendingRequestCount"):
        return "question"
    if thread.get("activeRunId") and not thread.get("pendingBackgroundTasks") and not thread.get("blockedOnThreadWait") and now and stall_s:
        changed = epoch(thread.get("updatedAt"))
        if changed and now - changed >= stall_s:
            return "stalled"
    if thread.get("activeRunId") or thread.get("pendingBackgroundTasks") or thread.get("blockedOnThreadWait"):
        return "busy"
    return "idle"


def git_base(project: Path, base: str) -> str:
    """The ref a task's worktree starts from: '' when the project is no git repository or the base is `none`."""
    if base == "none" or not (project / ".git").exists():
        return ""
    subprocess.run(["git", "-C", str(project), "fetch", "--quiet", "origin"], capture_output=True, timeout=120)
    if base:
        return base
    head = subprocess.run(["git", "-C", str(project), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], capture_output=True, text=True)
    return head.stdout.strip() if head.returncode == 0 and head.stdout.strip() else "HEAD"


class Job:
    """One claimed task, from claim to settle. `run` returns how it ended."""

    def __init__(self, task: dict, cfg: dict, board: Board, t3: T3, clock: Clock, log, stop: threading.Event | None = None, base=git_base):
        self.task, self.cfg, self.board, self.t3, self.clock, self.log = task, cfg, board, t3, clock, log
        self.stop = stop or threading.Event()
        self.base = base
        self.s = settings(cfg)
        self.id = task["id"]
        self.labels = dict(task.get("labels") or {})
        self.lease = f"{int(self.s.get('lease_minutes', 10))}m"
        self.lease_s = int(self.s.get("lease_minutes", 10)) * 60
        self.thread = ""
        self.link = ""

    # helpers
    def note(self, text: str) -> None:
        problem = self.board.reply(self.id, text)
        if problem:
            self.log(f"{self.id}: reply failed: {problem}")

    def project(self) -> tuple[str, Path]:
        prefix = self.s.get("channel", "build").rstrip("/") + "/"
        channel = self.task.get("channel", "")
        key = channel[len(prefix):] if channel.startswith(prefix) else ""
        path = (self.s.get("projects") or {}).get(key)
        if not path:
            raise BoardError(f"no path for project {key!r} on box {self.cfg.get('box')}: add it to board.projects in the box config")
        return key, paths.expand(path)

    def options(self) -> dict:
        provider = self.labels.get("provider") or self.s.get("provider") or ""
        model = self.labels.get("model") or self.s.get("model") or ""
        options = list(self.s.get("options") or [])
        if self.labels.get("effort"):
            options = [o for o in options if not o.startswith("effort=")] + [f"effort={self.labels['effort']}"]
        return {"provider": provider, "model": model, "options": options}

    def human_replies(self, data: dict) -> list[dict]:
        return [m for m in data.get("replies_list", []) if not is_worker(m) and not m.get("deleted")]

    def read(self) -> dict:
        data = self.board.read(self.id)
        replies = data.get("replies")
        data["replies_list"] = replies if isinstance(replies, list) else []
        return data

    # the two ways in
    def start(self, data: dict) -> str:
        key, project = self.project()
        text = (data.get("message") or self.task).get("text", "")
        earlier = self.human_replies(data)
        previous = ""
        if self.labels.get(THREAD) and self.labels.get(BOX) != self.cfg.get("box"):
            previous = f"\nAn earlier attempt ran in T3 thread {self.labels[THREAD]} on box {self.labels.get(BOX)}; its work may be on a branch there.\n"
        earlier_text = ""
        if earlier or previous:
            earlier_text = previous + "".join(f"\n### Reply from {author(m)}\n\n{m.get('text', '')}\n" for m in earlier)
        base = self.base(project, self.labels.get("base", self.s.get("base", "")))
        brief = BRIEF.format(task=self.id, channel=self.task.get("channel", ""), text=text, earlier=earlier_text,
                             where="git worktree" if base else "project folder")
        title = "[board] " + first_line(text, TITLE_MAX)
        thread = self.t3.start(str(project), title, brief, base, self.options())
        labels = {THREAD: thread, BOX: self.cfg.get("box", "")}
        if earlier:
            labels[SEEN] = earlier[-1]["id"]
        self.board.label(self.id, labels, unset=(WAITING,))
        self.labels.update(labels)
        return thread

    # main loop
    def run(self) -> str:
        try:
            data = self.read()
        except BoardError as error:
            self.log(f"{self.id}: {error}; releasing")
            self.board.settle("release", self.id)
            return "released"
        attach = self.labels.get(THREAD) and self.labels.get(BOX) == self.cfg.get("box") and self.t3.show(self.labels[THREAD]) is not None
        try:
            if attach:
                self.thread = self.labels[THREAD]
                self.log(f"{self.id}: attaching to T3 thread {self.thread}")
                if self.labels.get(WAITING):
                    # An earlier worker's flag; the watch sets it again if the thread still waits.
                    self.board.label(self.id, unset=(WAITING,))
            else:
                self.thread = self.start(data)
                self.log(f"{self.id}: started T3 thread {self.thread}")
        except BoardError as error:
            self.log(f"{self.id}: {error}")
            problem = self.board.settle("fail", self.id, first_line(str(error)))
            return "failed" if not problem else f"lost ({problem})"
        shown = self.t3.show(self.thread) or {}
        self.link = (shown.get("thread") or {}).get("link", "")
        if not attach:
            self.note(f"Working in T3 thread `{self.thread}` on box {self.cfg.get('box')}.\n{self.link}".strip())
        elif (shown.get("thread") or {}).get("status") not in ("completed", "running") and thread_state(shown.get("thread") or {}) == "idle":
            self.t3.send(self.thread, CONTINUE)
        return self.watch()

    def watch(self) -> str:
        poll = float(self.s.get("poll_seconds", 15))
        settle_s = float(self.s.get("settle_seconds", 45))
        answer_s = float(self.s.get("answer_hours", 24)) * 3600
        board_every = float(self.s.get("board_poll_seconds", 60))
        stall_s = float(self.s.get("stall_minutes", 15)) * 60
        renewed = last_board = self.clock.now()
        idle_since, idle_run, asked_since, asked_kind, misses = None, None, None, "", 0
        seen = self.labels.get(SEEN, "")
        while True:
            if self.stop.is_set():
                self.log(f"{self.id}: stopping; releasing the task, T3 thread {self.thread} keeps going")
                self.board.settle("release", self.id)
                return "released"
            now = self.clock.now()
            if now - renewed >= self.lease_s / 3:
                problem = self.board.renew(self.id, self.lease)
                if problem:
                    self.log(f"{self.id}: renew failed ({problem}); leaving T3 thread {self.thread} as it is")
                    return f"lost ({problem})"
                renewed = now
            if now - last_board >= board_every:
                last_board = now
                seen, _ = self.forward(seen)
            shown = self.t3.show(self.thread)
            if shown is None:
                misses += 1
                if misses >= int(self.s.get("max_misses", 8)):
                    self.log(f"{self.id}: cannot read T3 thread {self.thread}; releasing")
                    self.note(f"The worker could not read T3 thread `{self.thread}` and released the task.")
                    self.board.settle("release", self.id)
                    return "released"
                self.clock.sleep(poll, self.stop)
                continue
            misses = 0
            thread = shown["thread"]
            state = thread_state(thread, now, stall_s)
            if state in ("question", "stalled"):
                idle_since = None
                if asked_since is None or asked_kind != state:
                    asked_since = asked_since or now
                    asked_kind = state
                    if state == "question":
                        self.board.label(self.id, {WAITING: "answer"})
                        self.note(f"T3 thread `{self.thread}` is waiting for an answer from the owner. Answer it in T3 Code.\n{self.link}".strip())
                        self.log(f"{self.id}: waiting for an answer in T3")
                    else:
                        self.board.label(self.id, {WAITING: "check"})
                        self.note(f"T3 thread `{self.thread}` has shown no progress for {stall_s / 60:g} minutes while its run is open. "
                                  f"It may wait for something T3 does not report as a question (a secret, a sign-in, an approval). Check it in T3 Code.\n{self.link}".strip())
                        self.log(f"{self.id}: no progress for {stall_s / 60:g} min; flagged for a check in T3")
                elif now - asked_since >= answer_s:
                    what = "waits for an answer" if state == "question" else "has made no progress"
                    problem = self.board.settle("block", self.id, f"T3 thread {self.thread} {what}", "--kind", "needs_input")
                    self.log(f"{self.id}: blocked after {answer_s / 3600:g} h: thread {what}")
                    return "blocked" if not problem else f"lost ({problem})"
            else:
                if asked_since is not None:
                    asked_since, asked_kind = None, ""
                    self.board.label(self.id, unset=(WAITING,))
                if state == "busy":
                    idle_since = None
                else:
                    run = thread.get("latestRunId")
                    if idle_since is None or run != idle_run:
                        idle_since, idle_run = now, run
                    elif now - idle_since >= settle_s:
                        # A reply posted at the last moment gets its turn before the task is settled.
                        seen, sent = self.forward(seen)
                        if sent:
                            idle_since = None
                        else:
                            return self.finish(thread)
            self.clock.sleep(poll, self.stop)

    def forward(self, seen: str) -> tuple[str, int]:
        """Send people's new board replies into the thread.

        `seen` is the id of the last reply already handled, in the brief or forwarded; '' means none
        existed when the thread started, so every reply is new. Returns the new `seen` and how many
        replies were sent."""
        try:
            data = self.read()
        except BoardError as error:
            self.log(f"{self.id}: {error}")
            return seen, 0
        replies = self.human_replies(data)
        ids = [m["id"] for m in replies]
        if not seen:
            new = replies
        elif seen in ids:
            new = replies[ids.index(seen) + 1:]
        else:
            new = []  # the reply we last handled is gone; never send the others twice
        sent = 0
        for message in new:
            if self.t3.send(self.thread, FORWARD.format(who=author(message), text=message.get("text", ""))):
                self.log(f"{self.id}: forwarded a board reply from {author(message)}")
                sent += 1
        if new:
            seen = new[-1]["id"]
            self.board.label(self.id, {SEEN: seen})
            self.labels[SEEN] = seen
        return seen, sent

    def finish(self, thread: dict) -> str:
        shown = self.t3.show(self.thread, messages=1) or {}
        answers = [m for m in shown.get("messages") or [] if m.get("role") == "assistant"]
        answer = answers[-1].get("text", "") if answers else ""
        status = thread.get("status", "")
        line = first_line(answer)
        failed = status != "completed" or not line or line.upper().startswith("FAILED")
        header = f"Result from T3 thread `{self.thread}` ({status}):"
        self.note(clip(f"{header}\n\n{answer}" if answer else f"{header} no final answer."))
        if failed:
            reason = line if line.upper().startswith("FAILED") else (f"T3 thread {status}" + (f": {line}" if line else ", no final answer"))
            problem = self.board.settle("fail", self.id, first_line(reason))
            self.log(f"{self.id}: failed: {reason}")
            return "failed" if not problem else f"lost ({problem})"
        problem = self.board.settle("complete", self.id, line)
        self.log(f"{self.id}: completed: {line}")
        return "completed" if not problem else f"lost ({problem})"


# --- the worker loop -----------------------------------------------------------------------------


class Logger:
    def __init__(self, path: Path | None):
        self.path, self.lock = path, threading.Lock()

    def __call__(self, text: str) -> None:
        line = f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {text}"
        with self.lock:
            print(line, flush=True)
            if self.path:
                with self.path.open("a") as handle:
                    handle.write(line + "\n")


def channels(s: dict, only: list[str] | None) -> list[str]:
    prefix = s.get("channel", "build").rstrip("/")
    keys = list((s.get("projects") or {}).keys())
    if only:
        unknown = [k for k in only if k not in keys]
        if unknown:
            raise BoardError(f"no path on this box for {', '.join(unknown)}: add it to board.projects in the box config")
        keys = only
    if not keys:
        raise BoardError("board.projects is empty in the box config: map each project key to its folder on this box")
    return [f"{prefix}/{key}" for key in keys]


def worker_loop(cfg: dict, profile: str, chans: list[str], once: bool, stop: threading.Event, log, wait_s: int) -> int:
    s = settings(cfg)
    board, t3, clock = Board(profile), T3(), Clock()
    lease = f"{int(s.get('lease_minutes', 10))}m"
    prefix = s.get("channel", "build").rstrip("/") + "/"
    done = 0
    while not stop.is_set():
        task = None
        try:
            for channel in chans:
                task = board.claim(channel, lease)
                if task:
                    break
        except BoardError as error:
            log(f"[{profile}] {error}; retrying in a minute")
            clock.sleep(60, stop)
            continue
        if not task:
            if once:
                log(f"[{profile}] nothing ready")
                return 0 if done else 1
            problem = board.wait_ready(prefix, wait_s)
            if problem:
                log(f"[{profile}] waiting for tasks failed ({problem}); checking again in a minute")
                clock.sleep(60, stop)
            continue
        log(f"[{profile}] claimed {task['id']} in {task.get('channel')}: {first_line(task.get('text', ''), 80)}")
        try:
            outcome = Job(task, cfg, board, t3, clock, lambda text: log(f"[{profile}] {text}"), stop).run()
        except BoardError as error:
            log(f"[{profile}] {task['id']}: {error}")
            outcome = "error"
        log(f"[{profile}] {task['id']}: {outcome}")
        done += 1
        if once:
            return 0 if outcome == "completed" else 1
    return 0


def preflight() -> None:
    """Fail early, with the fix, when this box's CLIs cannot run the worker."""
    for command, fix in ((["vercel", "board", "tasks", "claim", "--help"], "update the Vercel CLI: npm install --global vercel@latest (board tasks arrived in 66)"),
                         (["t3", "thread", "show", "--help"], "install or update T3 Code: t3 update")):
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BoardError(f"`{' '.join(command[:2])}` is not usable ({error}); {fix}")
        if done.returncode != 0 or "unknown_command" in done.stdout or "Unknown command" in done.stderr:
            raise BoardError(f"`{' '.join(command[:-1])}` is missing on this box; {fix}")


def work(cfg: dict, args) -> int:
    s = settings(cfg)
    chans = channels(s, args.project)
    count = max(1, int(args.workers or s.get("workers", 1)))
    log_dir = paths.private_dir(paths.cache_dir() / "board")
    log = Logger(Path(args.log) if args.log else log_dir / f"work-{dt.date.today().isoformat()}.log")
    stop = threading.Event()

    def halt(signum, frame):
        log(f"signal {signum}: finishing up")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, halt)
    preflight()
    profiles = [s["worker"]] if count == 1 else [f"{s['worker']}-{n}" for n in range(1, count + 1)]
    for channel in chans:
        problem = Board(profiles[0]).ensure_channel(channel)
        if problem:
            raise BoardError(f"cannot create channel {channel}: {problem}. Check `vercel board whoami`.")
    log(f"board worker on {cfg.get('box')}: {', '.join(profiles)} on {', '.join(chans)}")
    results: list[int] = []
    threads = [threading.Thread(target=lambda p=p: results.append(worker_loop(cfg, p, chans, args.once, stop, log, int(args.wait_minutes * 60))), daemon=True) for p in profiles]
    for thread in threads:
        thread.start()
    while any(t.is_alive() for t in threads):
        for thread in threads:
            thread.join(0.5)
    log("board worker stopped")
    return max(results or [0])


def add(cfg: dict, args) -> int:
    s = settings(cfg)
    prefix = s.get("channel", "build").rstrip("/")
    text = sys.stdin.read() if args.text in (None, "-") else args.text
    if not text.strip():
        raise BoardError("Give the task's brief as an argument or on stdin.")
    labels = {}
    for item in args.label or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise BoardError(f"label {item!r} must be key=value")
        labels[key] = value
    for key in ("base", "model", "provider", "effort"):
        if getattr(args, key, None):
            labels[key] = getattr(args, key)
    task = Board(args.as_profile).create(f"{prefix}/{args.project}", text, labels, args.needs or [], args.for_profile)
    print(task["id"])
    return 0


def status(cfg: dict, args) -> int:
    s = settings(cfg)
    prefix = s.get("channel", "build").rstrip("/")
    channel = f"{prefix}/{args.project}" if args.project else f"{prefix}/"
    tasks = Board(args.as_profile).tasks(channel, "all")
    rows = []
    for item in tasks:
        message = item.get("message", item)
        task = message.get("task") or {}
        state = task.get("state", "")
        if not args.all and state in FINISHED:
            continue
        labels = message.get("labels") or {}
        if labels.get(WAITING) == "answer":
            state += " (waiting for an answer in T3)"
        elif labels.get(WAITING):
            state += " (no progress: check its T3 thread)"
        detail = task.get("result") or task.get("reason") or (task.get("blocked") or {}).get("reason") or ""
        thread = f"{labels.get(THREAD)} on {labels.get(BOX)}" if labels.get(THREAD) else ""
        rows.append({"id": message.get("id"), "channel": message.get("channel"), "state": state, "task": first_line(message.get("text", ""), 70),
                     "thread": thread, "detail": first_line(str(detail), 100)})
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No open tasks." if not args.all else "No tasks.")
        return 0
    for row in rows:
        print(f"{row['state']:<12} {row['channel']:<20} {row['task']}")
        print(f"{'':12} {row['id']}" + (f"  · T3 {row['thread']}" if row["thread"] else ""))
        if row["detail"]:
            print(f"{'':12} {row['detail']}")
    return 0
