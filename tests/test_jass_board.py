import datetime as dt
import threading
import unittest

import jasshelp  # noqa: F401  (puts the stack on sys.path)

from jass import board

CFG = {"box": "mac", "board": {"channel": "build", "projects": {"demo": "/tmp/demo"}, "worker": "", "base": "",
                               "lease_minutes": 3, "poll_seconds": 10, "settle_seconds": 30, "board_poll_seconds": 60,
                               "answer_hours": 1, "stall_minutes": 15, "max_misses": 3}}


BASE = 1_800_000_000.0


def iso(seconds):
    return dt.datetime.fromtimestamp(BASE + seconds, dt.timezone.utc).isoformat()


class FakeClock:
    """`t` counts seconds from the start of a test; `now` is a real epoch so `updatedAt` can be compared."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return BASE + self.t

    def sleep(self, seconds, stop=None):
        self.t += seconds


class FakeBoard:
    def __init__(self, task, replies=()):
        self.task, self.replies, self.actions, self.lost_at = task, list(replies), [], None
        self.clock = None

    def read(self, task_id):
        return {"message": dict(self.task), "replies": list(self.replies)}

    def settle(self, action, task_id, *args):
        if self.lost_at is not None and self.clock.t >= self.lost_at:
            return "claim_lost"
        self.actions.append((action, *args))
        return ""

    def renew(self, task_id, lease):
        return self.settle("renew", task_id)

    def reply(self, task_id, text):
        self.actions.append(("reply", text))
        return ""

    def label(self, task_id, labels=None, unset=()):
        self.actions.append(("label", dict(labels or {}), tuple(unset)))
        return ""

    def done(self):
        return [a for a in self.actions if a[0] in ("complete", "fail", "block", "release")]


class FakeT3:
    """`timeline(t)` gives the thread's fields at clock time t."""

    def __init__(self, clock, timeline, answer="Did it.\n\nDetails.", status="completed", known=()):
        self.clock, self.timeline, self.answer, self.status = clock, timeline, answer, status
        self.started, self.sent, self.known = [], [], set(known)

    def start(self, project, title, brief, worktree="", options=None):
        self.started.append({"project": project, "title": title, "brief": brief, "worktree": worktree, "options": options})
        self.known.add("mcp:new")
        return "mcp:new"

    def show(self, thread, messages=0):
        if thread not in self.known:
            return None
        fields = {"threadId": thread, "status": "running", "activeRunId": None, "latestRunId": "r1", "pendingRequestCount": 0,
                  "pendingBackgroundTasks": [], "blockedOnThreadWait": False, "link": "https://t3/x", "updatedAt": iso(self.clock.t)}
        fields.update(self.timeline(self.clock.t))
        if "quiet_since" in fields:
            fields["updatedAt"] = iso(fields.pop("quiet_since"))
        if not fields["activeRunId"] and not fields["pendingRequestCount"] and fields["status"] == "running":
            fields["status"] = self.status
        out = {"thread": fields}
        if messages:
            out["messages"] = [{"role": "assistant", "text": self.answer}]
        return out

    def send(self, thread, text):
        self.sent.append(text)
        return True


def task(labels=None, text="Bump the lockfile.\nScope: package-lock.json."):
    return {"id": "msg_1", "channel": "build/demo", "text": text, "labels": dict({"board.task": "claimed"}, **(labels or {}))}


def job(t, timeline, replies=(), **t3kw):
    clock = FakeClock()
    b = FakeBoard(t, replies)
    b.clock = clock
    t3 = FakeT3(clock, timeline, **t3kw)
    logs = []
    j = board.Job(t, CFG, b, t3, clock, logs.append, threading.Event(), base=lambda project, base: "origin/main")
    return j, b, t3, clock, logs


def running_until(end, **extra):
    return lambda t: dict({"activeRunId": "r1"} if t < end else {}, **extra)


class HelperTests(unittest.TestCase):
    def test_first_line_strips_markdown_and_skips_blanks(self):
        self.assertEqual(board.first_line("\n\n## **Bumped** `next` to 16.4.4\nmore"), "Bumped next to 16.4.4")

    def test_first_json_skips_banner(self):
        self.assertEqual(board.first_json("\nVercel CLI 66\n{\"a\": 1}"), {"a": 1})
        self.assertIsNone(board.first_json("no json"))

    def test_thread_state(self):
        self.assertEqual(board.thread_state({"pendingRequestCount": 1, "activeRunId": "r"}), "question")
        self.assertEqual(board.thread_state({"pendingBackgroundTasks": [{"taskId": "b"}]}), "busy")
        self.assertEqual(board.thread_state({"blockedOnThreadWait": True}), "busy")
        self.assertEqual(board.thread_state({"activeRunId": None}), "idle")

    def test_worker_profile_must_start_with_worker(self):
        with self.assertRaises(board.BoardError):
            board.settings({"box": "mac", "board": {"worker": "fx"}})
        self.assertEqual(board.settings({"box": "ps5"})["worker"], "worker-ps5")


class JobTests(unittest.TestCase):
    def test_completes_with_the_first_line_once_idle_long_enough(self):
        j, b, t3, clock, _ = job(task(), running_until(100))
        self.assertEqual(j.run(), "completed")
        self.assertEqual(b.done(), [("complete", "Did it.")])
        self.assertGreaterEqual(clock.t, 130)
        started = t3.started[0]
        self.assertEqual(started["project"], "/tmp/demo")
        self.assertEqual(started["worktree"], "origin/main")
        self.assertEqual(started["title"], "[board] Bump the lockfile.")
        self.assertIn("Bump the lockfile.", started["brief"])
        self.assertIn("msg_1", started["brief"])
        labels = [a[1] for a in b.actions if a[0] == "label"][0]
        self.assertEqual(labels, {board.THREAD: "mcp:new", board.BOX: "mac"})
        self.assertTrue(any(a[0] == "reply" and "Did it." in a[1] for a in b.actions))

    def test_background_command_after_turn_end_holds_the_task(self):
        # The turn ends at 20 s, but a background command runs until 200 s, then a second run reports.
        def timeline(t):
            if t < 20:
                return {"activeRunId": "r1"}
            if t < 200:
                return {"status": "completed", "pendingBackgroundTasks": [{"taskId": "bg"}]}
            if t < 230:
                return {"activeRunId": "r2", "latestRunId": "r2"}
            return {"latestRunId": "r2"}
        j, b, _, clock, _ = job(task(), timeline)
        self.assertEqual(j.run(), "completed")
        self.assertGreaterEqual(clock.t, 260)

    def test_short_gap_between_runs_does_not_settle(self):
        # Idle for 20 s (less than settle_seconds) between two runs.
        def timeline(t):
            if t < 50:
                return {"activeRunId": "r1"}
            if t < 70:
                return {}
            if t < 150:
                return {"activeRunId": "r2", "latestRunId": "r2"}
            return {"latestRunId": "r2"}
        j, b, _, clock, _ = job(task(), timeline)
        self.assertEqual(j.run(), "completed")
        self.assertGreaterEqual(clock.t, 180)

    def test_question_labels_and_waits_then_completes_after_answer(self):
        def timeline(t):
            if t < 30:
                return {"activeRunId": "r1"}
            if t < 600:
                return {"activeRunId": "r1", "pendingRequestCount": 1}
            if t < 650:
                return {"activeRunId": "r1"}
            return {}
        j, b, _, _, _ = job(task(), timeline)
        self.assertEqual(j.run(), "completed")
        labels = [a for a in b.actions if a[0] == "label"]
        self.assertIn(("label", {board.WAITING: "answer"}, ()), labels)
        self.assertIn(("label", {}, (board.WAITING,)), labels)
        self.assertTrue(any(a[0] == "reply" and "waiting for an answer" in a[1] for a in b.actions))
        self.assertEqual(b.done(), [("complete", "Did it.")])

    def test_question_unanswered_for_answer_hours_blocks(self):
        j, b, _, _, _ = job(task(), lambda t: {"activeRunId": "r1", "pendingRequestCount": 1})
        self.assertEqual(j.run(), "blocked")
        self.assertEqual(b.done()[0][0], "block")
        self.assertIn("needs_input", b.done()[0])

    def test_silent_run_is_flagged_then_blocked(self):
        # A secret card: the run stays open, nothing counts as a question, and the thread stops changing at 20 s.
        j, b, _, _, _ = job(task(), lambda t: {"activeRunId": "r1", "quiet_since": min(t, 20)})
        self.assertEqual(j.run(), "blocked")
        self.assertIn(("label", {board.WAITING: "check"}, ()), [a for a in b.actions if a[0] == "label"])
        self.assertTrue(any(a[0] == "reply" and "no progress" in a[1] for a in b.actions))
        self.assertIn("made no progress", b.done()[0][1])

    def test_quiet_background_work_is_not_a_stall(self):
        def timeline(t):
            if t < 2400:
                return {"activeRunId": "r1", "pendingBackgroundTasks": [{"taskId": "bg"}], "quiet_since": 0}
            return {}
        j, b, _, _, _ = job(task(), timeline)
        self.assertEqual(j.run(), "completed")
        self.assertFalse(any(a[0] == "label" and board.WAITING in a[1] for a in b.actions))

    def test_lease_is_renewed_during_long_work(self):
        j, b, _, _, _ = job(task(), running_until(1000))
        j.run()
        renews = [a for a in b.actions if a[0] == "renew"]
        self.assertGreaterEqual(len(renews), 1000 // 60 - 1)

    def test_lost_claim_stops_without_settling(self):
        j, b, _, _, _ = job(task(), running_until(10_000))
        b.lost_at = 300
        self.assertEqual(j.run(), "lost (claim_lost)")
        self.assertEqual(b.done(), [])

    def test_failed_first_line_fails_the_task(self):
        j, b, _, _, _ = job(task(), running_until(20), answer="FAILED: the tests need a database.")
        self.assertEqual(j.run(), "failed")
        self.assertEqual(b.done(), [("fail", "FAILED: the tests need a database.")])

    def test_thread_that_ends_in_error_fails_the_task(self):
        j, b, _, _, _ = job(task(), running_until(20), status="failed", answer="")
        self.assertEqual(j.run(), "failed")
        self.assertEqual(b.done()[0][0], "fail")
        self.assertIn("T3 thread failed", b.done()[0][1])

    def test_attaches_to_its_own_thread_after_a_restart(self):
        t = task({board.THREAD: "mcp:old", board.BOX: "mac"})
        j, b, t3, _, _ = job(t, running_until(40), known=("mcp:old",))
        self.assertEqual(j.run(), "completed")
        self.assertEqual(t3.started, [])
        self.assertFalse(any(a[0] == "reply" and a[1].startswith("Working in") for a in b.actions))

    def test_thread_of_another_box_starts_a_new_one_with_a_note(self):
        t = task({board.THREAD: "mcp:old", board.BOX: "ps5"})
        j, _, t3, _, _ = job(t, running_until(40), known=("mcp:old",))
        j.run()
        self.assertEqual(len(t3.started), 1)
        self.assertIn("mcp:old on box ps5", t3.started[0]["brief"])

    def test_earlier_replies_go_into_the_brief_and_new_ones_are_forwarded(self):
        lead = {"id": "msg_1_2", "text": "Use npm 11.", "by": {"profile": "lead"}, "author": {"name": "lead", "via": {"name": "leo"}}}
        mine = {"id": "msg_1_3", "text": "Working in…", "by": {"profile": "worker-mac"}}
        j, b, t3, _, _ = job(task(), running_until(400), replies=[lead, mine])
        late = {"id": "msg_1_4", "text": "Also update the docs.", "by": {"profile": None}, "author": {"name": "leo"}}

        original = b.read

        def read(task_id):
            data = original(task_id)
            if j.clock.t > 100:
                data["replies"].append(late)
            return data

        b.read = read
        self.assertEqual(j.run(), "completed")
        self.assertIn("Use npm 11.", t3.started[0]["brief"])
        self.assertEqual(len(t3.sent), 1)
        self.assertIn("Also update the docs.", t3.sent[0])
        self.assertIn("from leo", t3.sent[0])

    def test_reply_posted_while_idle_gets_a_turn_before_settling(self):
        j, b, t3, clock, _ = job(task(), running_until(20))
        late = {"id": "msg_1_9", "text": "One more thing.", "by": {"profile": None}, "author": {"name": "leo"}}
        original = b.read
        b.read = lambda task_id: dict(original(task_id), replies=[late] if clock.t > 30 else [])
        self.assertEqual(j.run(), "completed")
        self.assertEqual(len(t3.sent), 1)

    def test_unreadable_thread_releases_the_task(self):
        j, b, t3, _, _ = job(task(), lambda t: {})
        t3.known = set()
        t3.start = lambda *a, **k: "mcp:gone"
        self.assertEqual(j.run(), "released")
        self.assertEqual(b.done(), [("release",)])

    def test_start_failure_fails_the_task(self):
        j, b, t3, _, _ = job(task(), lambda t: {})

        def boom(*a, **k):
            raise board.BoardError("t3 thread start failed (exit 1): no project")

        t3.start = boom
        self.assertEqual(j.run(), "failed")
        self.assertEqual(b.done()[0][0], "fail")

    def test_stop_releases_and_leaves_the_thread(self):
        j, b, _, _, _ = job(task(), running_until(10_000))
        j.stop.set()
        self.assertEqual(j.run(), "released")
        self.assertEqual(b.done(), [("release",)])

    def test_task_labels_choose_model_and_effort(self):
        t = task({"provider": "claudeAgent", "model": "claude-opus-5-5", "effort": "high"})
        j, _, t3, _, _ = job(t, running_until(20))
        j.run()
        self.assertEqual(t3.started[0]["options"], {"provider": "claudeAgent", "model": "claude-opus-5-5", "options": ["effort=high"]})

    def test_unknown_project_fails_the_task(self):
        t = dict(task(), channel="build/other")
        j, b, _, _, _ = job(t, running_until(20))
        self.assertEqual(j.run(), "failed")
        self.assertIn("board.projects", b.done()[0][1])


class ChannelTests(unittest.TestCase):
    def test_channels_follow_board_projects(self):
        s = board.settings(CFG)
        self.assertEqual(board.channels(s, None), ["build/demo"])
        with self.assertRaises(board.BoardError):
            board.channels(s, ["nope"])
        with self.assertRaises(board.BoardError):
            board.channels(board.settings({"box": "mac", "board": {"projects": {}}}), None)


if __name__ == "__main__":
    unittest.main()


class CliTests(unittest.TestCase):
    def test_add_takes_the_brief_after_options(self):
        from jass import cli

        args = cli.parse(["board", "add", "shop", "--needs", "msg_1", "--for", "worker-ps5", "Review the fix."])
        self.assertEqual((args.project, args.text, args.needs, args.for_profile), ("shop", "Review the fix.", ["msg_1"], "worker-ps5"))
        args = cli.parse(["board", "add", "shop", "Fix it.", "--base", "none"])
        self.assertEqual((args.text, args.base), ("Fix it.", "none"))

    def test_other_extra_arguments_are_still_refused(self):
        from jass import cli

        with self.assertRaises(SystemExit):
            cli.parse(["board", "add", "shop", "one", "--base", "none", "two"])
        with self.assertRaises(SystemExit):
            cli.parse(["board", "status", "--bogus"])


class WaitTests(unittest.TestCase):
    def test_stop_ends_a_long_wait_at_once(self):
        import subprocess
        import time
        from unittest import mock

        real = subprocess.Popen
        stop = threading.Event()
        threading.Timer(0.5, stop.set).start()
        with mock.patch("jass.board.subprocess.Popen", side_effect=lambda cmd, **kw: real(["sleep", "30"], **kw)):
            started = time.monotonic()
            self.assertEqual(board.Board("worker-mac").wait_ready("build/", 300, stop), "")
        self.assertLess(time.monotonic() - started, 5)


class OpenTests(unittest.TestCase):
    URL = "https://board.example.test"

    def run_open(self, argv, env, whoami=(0, {"board": URL}, "")):
        from unittest import mock

        from jass import cli

        args = cli.parse(["board", "open", *argv])
        with mock.patch.object(board.Board, "run", return_value=whoami) as run, \
                mock.patch("jass.board.subprocess.call", return_value=0) as call:
            code = board.open_board(CFG, args, env=env)
        return code, run, call

    def test_runs_the_pinned_inspector_on_the_board_from_whoami(self):
        code, run, call = self.run_open([], env={})
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args, ("whoami",))
        command = call.call_args.args[0]
        self.assertEqual(command[:5], ["npx", "--yes", board.INSPECT_PACKAGE, "inspect", "--url"])
        self.assertEqual(command[5], self.URL)
        self.assertRegex(board.INSPECT_PACKAGE, r"^@vercel/board@\d+\.\d+\.\d+$")
        self.assertNotIn("--no-open", command)
        self.assertNotIn("--allow-host", command)

    def test_port_and_no_open_pass_through(self):
        _, _, call = self.run_open(["--port", "4800", "--no-open"], env={})
        command = call.call_args.args[0]
        self.assertEqual(command[-3:], ["--port", "4800", "--no-open"])

    def test_over_ssh_it_opens_no_browser_and_uses_a_fixed_port(self):
        import contextlib
        import io

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _, _, call = self.run_open([], env={"SSH_CONNECTION": "1 2 3 4"})
        command = call.call_args.args[0]
        self.assertIn("--no-open", command)
        self.assertEqual(command[command.index("--port") + 1], str(board.INSPECT_PORT))
        self.assertIn(f"ssh -N -L {board.INSPECT_PORT}:127.0.0.1:{board.INSPECT_PORT}", err.getvalue())
        self.assertNotIn("--allow-host", command)

    def test_no_board_from_whoami_says_to_log_in(self):
        with self.assertRaisesRegex(board.BoardError, "vercel login"):
            self.run_open([], env={}, whoami=(1, None, "Error: not logged in"))

    def test_open_needs_no_box_config(self):
        from unittest import mock

        from jass import cli

        args = cli.parse(["board", "open", "--no-open"])
        with mock.patch("jass.cli.config.load", side_effect=AssertionError("loaded")), \
                mock.patch("jass.board.open_board", return_value=0) as opened:
            self.assertEqual(cli.cmd_board(args), 0)
        self.assertEqual(opened.call_args.args[0], {})
