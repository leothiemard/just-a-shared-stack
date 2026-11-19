import contextlib
import datetime as dt
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

import t3fixture
from jasshelp import Sandbox

from jass import JassError
from jass.night import heavy, t3state, watch

START = dt.datetime(2026, 1, 10, 23, 0, tzinfo=dt.timezone.utc)
OOM = {"class": "transport_error", "code": "oom_kill", "message": "Killed: out of memory."}
DNS = {"class": "provider_error", "code": "api_error", "message": "API Error: Can't reach the API server — check your internet or DNS (EAI_AGAIN)"}
LIMIT = {"class": "usage_limit", "code": "usage_limit", "message": "You've hit your session limit", "resetAt": "2026-01-11T01:00:00.000Z"}
AUTH = {"class": "provider_error", "code": "api_error_403", "message": "Failed to authenticate. API Error: 403"}
REAL_WHICH = shutil.which


def with_t3(name, *args, **kwargs):
    """`shutil.which` on a box where `t3` is installed, whatever this box has."""
    return "/opt/t3/bin/t3" if name == "t3" else REAL_WHICH(name, *args, **kwargs)


class FakeBox(watch.Box):
    """The store is real (a fixture); the clock, network, memory and the sending are scripted."""

    def __init__(self, store):
        super().__init__(store)
        self.clock, self.net, self.mem, self.up, self.sent, self.refuse = START, True, 9000, {}, [], ""

    def now(self):
        return self.clock

    def online(self):
        return self.net

    def available_mb(self):
        return self.mem

    def reach(self, peer):
        return self.up.get(peer, True)

    def send(self, thread_id, message):
        if not self.refuse:
            self.sent.append((thread_id, message))
        return self.refuse


class StateTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.path = self.base / "t3" / "userdata" / "statev2.sqlite"
        self.path.parent.mkdir(parents=True)
        self.store = t3fixture.Store(self.path)

    def lane(self, name, status="completed", title=""):
        self.store.thread(name, title=title or f"Lane {name}")
        return self.store.turn(name, "Go", "Done.", status=status)

    def states(self):
        return {t["id"]: t["state"] for t in t3state.snapshot(self.path)}

    def test_every_state(self):
        self.lane("ended")
        self.lane("working", "running")
        self.lane("stopped", "interrupted")
        self.lane("failed", "failed")
        self.store.item("failed", "error", "failed", failure=OOM)
        self.lane("stuck")
        self.store.turn("stuck", "Queued and never started", "", status="queued")
        self.lane("blocked", "running")
        self.store.request("blocked", "user_input")
        self.lane("answered")
        self.store.request("answered", "command", status="resolved")
        self.lane("secret", "running")
        self.store.item("secret", "secret_request", "completed", secretStatus="pending")
        self.lane("delegated")
        self.store.thread("child", parent="delegated")
        self.store.subagent("delegated", "child", "app_owned", status="running")
        self.lane("background")
        self.store.background("background", "command", "monitor")
        self.assertEqual(self.states(), {name: name for name in ("ended", "working", "stopped", "failed", "stuck", "blocked", "delegated", "background")}
                         | {"answered": "ended", "secret": "blocked"})
        found = {t["id"]: t for t in t3state.snapshot(self.path)}
        self.assertEqual((found["failed"]["failure"], found["failed"]["detail"]), ("memory", "run 1 failed: Killed: out of memory."))
        self.assertEqual(found["blocked"]["detail"], "waits for the owner: user input")
        self.assertEqual(found["background"]["detail"], "waits on command, monitor")
        self.assertEqual(t3state.going(self.path), 3)

    def test_a_queued_run_behind_a_running_one_is_work(self):
        self.lane("busy", "running")
        self.store.turn("busy", "Next", "", status="queued")
        self.lane("paused", "waiting")  # T3 counts a waiting run as active
        self.assertEqual(self.states(), {"busy": "working", "paused": "working"})

    def test_the_last_run_that_ran_decides_not_an_older_queued_one(self):
        self.lane("held", "failed")                      # T3 holds the queue after a failure:
        self.store.turn("held", "Queued", "", status="queued")
        self.assertEqual(self.states()["held"], "failed")  # the failure is what to act on, and it says why
        self.store.turn("held", "Continue", "Done.")     # a message from the CLI ran as a new run
        self.assertEqual(self.states()["held"], "ended")   # the held run is passed over, not stuck for ever
        self.lane("halted", "interrupted")
        self.store.turn("halted", "Queued", "", status="queued")
        self.assertEqual(self.states()["halted"], "stopped")  # a person stopped it: never nudged as stuck
        self.lane("parent")
        self.store.turn("parent", "Queued", "", status="queued")
        self.store.thread("child", parent="parent")
        self.store.subagent("parent", "child", "app_owned", status="running")
        self.assertEqual(self.states()["parent"], "delegated")
        self.lane("killed", "failed")
        self.store.background("killed", "command")       # listed still, but it died with the run
        self.assertEqual(self.states()["killed"], "failed")

    def test_children_archived_and_deleted_threads_are_left_out(self):
        self.lane("top")
        self.store.thread("child", parent="top")
        self.store.thread("gone", deleted=True)
        self.store.thread("fork", parent="top", relationship="fork")  # a fork is a thread of its own
        self.assertEqual(set(self.states()), {"top", "fork"})

    def test_a_link_to_open_blocks_and_a_failure_is_read_from_its_own_run(self):
        self.lane("link")
        self.store.item("link", "link_request", "waiting")
        self.lane("opened")
        self.store.item("opened", "link_request", "completed")
        self.lane("twice", "failed")
        self.store.item("twice", "error", "failed", failure=LIMIT)
        self.store.turn("twice", "Continue", "", status="failed")  # failed again, with no error item of its own
        found = {t["id"]: t for t in t3state.snapshot(self.path)}
        self.assertEqual((found["link"]["state"], found["link"]["detail"]), ("blocked", "waits for the owner: link"))
        self.assertEqual(found["opened"]["state"], "ended")
        self.assertEqual((found["twice"]["failure"], found["twice"]["reset"]), ("other", ""))  # not the earlier run's usage limit
        self.assertEqual(found["link"]["provider"], "claudeAgent")
        self.assertEqual([t["id"] for t in t3state.snapshot(self.path, ["nothing"])], [])

    def test_failure_kinds(self):
        self.assertEqual([t3state.failure_kind(f) for f in (OOM, DNS, LIMIT, {"class": "transport_error", "code": "unknown"}, {"class": "provider_error", "message": "403"}, AUTH,
                                                            {"class": "provider_error", "message": "exit 1 at line 4031"}, {})],
                         ["memory", "network", "limit", "network", "auth", "auth", "other", "other"])

    def test_pick_by_id_prefix_or_title(self):
        self.lane("lane-alpha-1", title="Fuzz the wire")
        self.lane("lane-beta-2", title="Cost benchmark")
        threads = t3state.snapshot(self.path)
        self.assertEqual([t["id"] for t in t3state.pick(threads, ["fuzz", "lane-beta", "lane-alpha-1"])], ["lane-alpha-1", "lane-beta-2"])
        for name in ("lane", "nothing like it"):
            with self.assertRaises(JassError):
                t3state.pick(threads, [name])

    def test_a_changed_store_is_refused_and_a_missing_one_named(self):
        self.store.drop_column(f"{t3fixture.T}runs", "requested_at")
        with self.assertRaisesRegex(JassError, "requested_at"):
            t3state.snapshot(self.path)
        with self.assertRaisesRegex(JassError, "No T3 Code store"):
            t3state.snapshot(self.base / "nowhere.sqlite")

    def test_the_store_is_never_written(self):
        self.lane("a", "failed")
        before = self.path.read_bytes()
        t3state.snapshot(self.path)
        t3state.going(self.path)
        self.assertEqual(self.path.read_bytes(), before)


class WatchTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.path = self.base / "t3" / "userdata" / "statev2.sqlite"
        self.path.parent.mkdir(parents=True)
        self.store = t3fixture.Store(self.path)
        self.box = FakeBox(self.path)
        self.lines = []

    def lane(self, name, status="completed", failure=None):
        self.store.thread(name, title=f"Lane {name}")
        run = self.store.turn(name, "Go", "Done.", status=status)
        if failure:
            self.store.item(name, "error", "failed", failure=failure)
        return run

    def watch(self, *names, hours=8, **options):
        threads = [t for t in t3state.snapshot(self.path) if t["id"] in names]
        return watch.Watch(self.box, threads, START + dt.timedelta(hours=hours), emit=self.lines.append, **options)

    def tick(self, w, minutes=1, times=1):
        for _ in range(times):
            self.box.clock += dt.timedelta(minutes=minutes)
            w.step()

    def logged(self, kind):
        return [line for line in self.lines if f" {kind:6} " in line]

    def test_a_failed_run_is_resumed_once_it_has_stayed_failed(self):
        self.lane("a", "failed", DNS)
        w = self.watch("a")
        self.tick(w, times=2)
        self.assertEqual(self.box.sent, [])  # may still be T3's own retry
        self.tick(w, times=2)
        self.assertEqual(len(self.box.sent), 1)
        thread, message = self.box.sent[0]
        self.assertEqual(thread, "a")
        self.assertIn("could not be reached", message)
        self.assertIn("Continue from where you stopped", message)
        self.assertTrue(message.startswith("Automatic message from the night watchdog"))
        self.tick(w, times=5)
        self.assertEqual(len(self.box.sent), 1)  # not again within ten minutes
        self.assertEqual(w.alerts, 0)

    def test_it_waits_for_the_network_and_logs_the_outage_once(self):
        self.lane("a", "failed", DNS)
        self.box.net = False
        w = self.watch("a")
        self.tick(w, times=8)
        self.assertEqual((self.box.sent, len(self.logged("box"))), ([], 1))
        self.box.net = True
        self.tick(w)
        self.assertEqual(len(self.box.sent), 1)
        self.assertIn("network back", self.logged("box")[1])

    def test_a_memory_kill_is_resumed_only_when_there_is_room(self):
        self.lane("a", "failed", OOM)
        self.box.mem = 1500
        w = self.watch("a", need_mb=3000)
        self.tick(w, times=6)
        self.assertEqual(self.box.sent, [])
        self.assertIn("memory low: 1500 MB", self.logged("box")[0])
        self.box.mem = 2500
        self.tick(w, times=3)
        self.assertEqual(self.box.sent, [])  # no longer low, but less than a heavy command needs
        self.assertEqual(len(self.logged("wait")), 1)  # said once, not at every poll
        self.assertIn("waiting for memory (1500 MB available, 3000 MB wanted", self.logged("wait")[0])
        self.tick(w, times=25)
        self.assertEqual((self.box.sent, w.alerts), ([], 1))  # a long wait is not silent
        self.assertIn("failed and not resumed for 30 minutes", self.logged("ALERT")[0])
        self.box.mem = 6000
        self.tick(w)
        self.assertIn("jass heavy", self.box.sent[0][1])

    def test_a_thread_that_keeps_failing_becomes_an_alert(self):
        run = self.lane("a", "failed", OOM)
        w = self.watch("a", max_nudges=2)
        self.tick(w, minutes=11, times=4)
        self.assertEqual((len(self.box.sent), w.alerts), (2, 1))
        self.assertIn("after being resumed 2 times", self.logged("ALERT")[0])
        self.tick(w, minutes=11, times=3)
        self.assertEqual(w.alerts, 1)
        self.store.set_run(run, "completed")  # it recovered and now ends before the deadline
        self.tick(w, minutes=11, times=2)
        self.assertEqual(w.alerts, 2)

    def test_a_usage_limit_waits_for_its_reset(self):
        self.lane("a", "failed", LIMIT)
        w = self.watch("a")
        self.tick(w, times=10)
        self.assertEqual((self.box.sent, w.alerts), ([], 1))
        self.assertIn("resets at 01:00 UTC", self.logged("ALERT")[0])
        self.box.clock = dt.datetime(2026, 1, 11, 1, 5, tzinfo=dt.timezone.utc)
        w.step()
        self.assertIn("limit has reset", self.box.sent[0][1])

    def test_t3_gets_time_to_resume_after_a_limit_itself(self):
        self.lane("a", "failed", LIMIT)
        w = self.watch("a", t3_resumes=True)
        self.tick(w, times=5)
        self.assertIn("T3 Code resumes it then", self.logged("ALERT")[0])
        self.box.clock = dt.datetime(2026, 1, 11, 1, 5, tzinfo=dt.timezone.utc)
        w.step()
        self.assertEqual(self.box.sent, [])  # T3's own resume comes first
        self.box.clock = dt.datetime(2026, 1, 11, 1, 16, tzinfo=dt.timezone.utc)
        w.step()
        self.assertEqual(len(self.box.sent), 1)

    def test_reminders_and_resumes_are_counted_apart(self):
        run = self.lane("a")
        w = self.watch("a", keep_going=True, max_nudges=2)
        self.tick(w, minutes=11, times=4)
        self.assertEqual((len(self.box.sent), w.alerts), (2, 1))  # reminded twice, then an alert
        self.assertIn("after being reminded 2 times", self.logged("ALERT")[0])
        self.store.set_run(run, "failed")
        self.store.item("a", "error", "failed", failure=DNS)
        self.tick(w, minutes=11, times=2)
        self.assertEqual(len(self.box.sent), 3)  # the failure is still resumed
        self.assertIn("could not be reached", self.box.sent[2][1])

    def test_a_restarted_watch_keeps_its_count_and_repeats_no_alert(self):
        self.lane("fails", "failed", OOM)
        self.lane("halted", "interrupted")
        state = {"sent": {}, "reminded": {}, "alerts": {}}
        saves = []
        for _ in range(3):  # a watcher thread starts the watch again after every alert
            w = self.watch("fails", "halted", max_nudges=2, state=state, keep=lambda: saves.append(1))
            self.tick(w, minutes=11, times=4)
        self.assertEqual(len(self.box.sent), 2)
        self.assertEqual([line.split(": ", 1)[0].split()[-2:] for line in self.logged("ALERT")], [["Lane", "halted"], ["Lane", "fails"]])
        self.assertEqual(w.alerts, 0)  # the third watch had nothing new to say, so --wake does not fire again
        self.assertTrue(saves)
        path = self.base / "state.json"
        watch.save_state(path, state)
        self.assertEqual(watch.load_state(path, self.box.clock), state)
        self.assertEqual(watch.load_state(path, self.box.clock + dt.timedelta(hours=20)), {"sent": {}, "reminded": {}, "alerts": {}})  # another night
        path.write_text("not json")
        self.assertEqual(watch.load_state(path, self.box.clock), {"sent": {}, "reminded": {}, "alerts": {}})

    def test_a_thread_that_answers_the_reminder_at_once_is_not_reminded_again(self):
        self.lane("a")
        w = self.watch("a", keep_going=True)
        self.tick(w, times=11)
        self.assertEqual(len(self.box.sent), 1)
        done = (self.box.clock + dt.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.store.set_run(self.store.turn("a", None, "Everything left needs the owner."), "completed", completed_at=done)
        self.tick(w, minutes=11, times=4)
        self.assertEqual((len(self.box.sent), w.alerts), (1, 1))
        self.assertIn("answered the reminder within 5 minutes", self.logged("ALERT")[0])

    def test_a_refused_sign_in_is_an_alert_not_a_message(self):
        self.lane("a", "failed", AUTH)
        w = self.watch("a")
        self.tick(w, times=20)
        self.assertEqual((self.box.sent, w.alerts), ([], 1))
        self.assertIn("refused the sign-in", self.logged("ALERT")[0])

    def test_an_unreadable_store_is_logged_then_an_alert_and_the_watch_goes_on(self):
        self.lane("a", "failed", DNS)
        w = self.watch("a")
        with mock.patch.object(self.box, "threads", side_effect=JassError("T3 Code's store changed shape")):
            self.tick(w, times=2)
            self.assertEqual((len(self.logged("box")), w.alerts), (1, 0))
            self.tick(w, times=3)
            self.assertEqual(w.alerts, 1)
            self.assertIn("this box: the T3 store cannot be read since", self.logged("ALERT")[0])
        self.tick(w, times=4)
        self.assertIn("the T3 store reads again", self.logged("box")[1])
        self.assertEqual(len(self.box.sent), 1)

    def test_a_watched_thread_that_leaves_the_store_is_logged(self):
        self.lane("a", "running")
        w = self.watch("a")
        self.tick(w)
        self.store.archive("a")
        self.tick(w, times=3)
        self.assertEqual(len(self.logged("box")), 1)
        self.assertIn("Lane a is no longer in the store", self.logged("box")[0])

    def test_a_stuck_queue_gets_a_message(self):
        self.lane("a")
        self.store.turn("a", "Queued", "", status="queued")
        w = self.watch("a")
        self.tick(w, times=4)
        self.assertIn("queued message of yours never started", self.box.sent[0][1])

    def test_blocked_and_stopped_threads_are_alerts_and_get_no_message(self):
        self.lane("asks", "running")
        self.store.request("asks", "user_input")
        self.lane("halted", "interrupted")
        w = self.watch("asks", "halted")
        self.tick(w, times=20)
        self.assertEqual((self.box.sent, w.alerts), ([], 2))
        self.assertIn("Lane asks: waits for the owner: user input", self.logged("ALERT")[0])

    def test_working_delegated_and_background_threads_are_left_alone(self):
        self.lane("busy", "running")
        self.lane("delegated")
        self.store.thread("child", parent="delegated")
        self.store.subagent("delegated", "child", "app_owned", status="running")
        self.lane("waiting")
        self.store.background("waiting", "command")
        w = self.watch("busy", "delegated", "waiting", keep_going=True)
        self.tick(w, minutes=20, times=6)
        self.assertEqual((self.box.sent, w.alerts, self.lines), ([], 0, []))

    def test_an_early_end_is_an_alert_or_with_keep_going_a_reminder(self):
        self.lane("a")
        w = self.watch("a")
        self.tick(w, times=9)
        self.assertEqual(w.alerts, 0)
        self.tick(w, times=2)
        self.assertEqual((w.alerts, self.box.sent), (1, []))
        self.assertIn("with the night running until 07:00 UTC", self.logged("ALERT")[0])

        self.box.clock, self.lines = START, []
        w = self.watch("a", keep_going=True)
        self.tick(w, times=11)
        self.assertEqual(w.alerts, 0)
        self.assertIn("standing mandate", self.box.sent[0][1])
        self.assertIn("until 07:00 UTC", self.box.sent[0][1])

    def test_no_reminder_in_the_last_half_hour(self):
        self.lane("a")
        w = self.watch("a", hours=0.5, keep_going=True)
        self.tick(w, times=25)
        self.assertEqual((self.box.sent, w.alerts), ([], 0))

    def test_dry_run_sends_nothing_and_a_refused_send_is_an_alert(self):
        self.lane("a", "failed", DNS)
        w = self.watch("a", dry_run=True)
        self.tick(w, times=4)
        self.assertEqual(self.box.sent, [])
        self.assertIn("continue after a network failure", self.logged("would")[0])

        self.box.refuse = "t3: server not running"
        w = self.watch("a")
        self.tick(w, minutes=11, times=6)
        self.assertEqual((self.box.sent, w.alerts), ([], 1))  # one alert, and no message used up
        self.assertIn("server not running", self.logged("ALERT")[0])
        self.box.refuse = ""
        self.tick(w, minutes=11)
        self.assertEqual(len(self.box.sent), 1)

    def test_no_resume_reports_instead(self):
        self.lane("a", "failed", OOM)
        w = self.watch("a", resume=False)
        self.tick(w, times=5)
        self.assertEqual((self.box.sent, w.alerts), ([], 1))

    def test_a_peer_going_quiet_is_logged_on_each_change(self):
        self.lane("a", "running")
        w = self.watch("a", peers=["laptop"])
        self.tick(w, times=2)
        self.box.up["laptop"] = False
        self.tick(w, times=3)
        self.box.up["laptop"] = True
        self.tick(w)
        self.assertEqual([line.split("    ")[-1] for line in self.logged("box")],
                         ["laptop stopped answering: asleep, off or off the network; its threads are frozen", "laptop answers again"])
        self.assertEqual(w.alerts, 0)  # a blip of two minutes
        self.box.up["laptop"] = False
        self.tick(w, times=6)
        self.assertEqual(w.alerts, 1)  # a box that stays quiet wakes the watcher, once
        self.assertIn("laptop: stopped answering at 23:07 UTC", self.logged("ALERT")[0])

    def test_the_real_send_goes_through_t3_thread_send(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs.get("input")))
            return mock.Mock(returncode=0, stdout="", stderr="")

        box = watch.Box(self.path)
        with mock.patch.object(watch.shutil, "which", return_value="/bin/t3"), mock.patch.object(watch.subprocess, "run", fake_run):
            self.assertEqual(box.send("mcp:launch:abc", "hello"), "")
        self.assertEqual(calls, [(["t3", "thread", "send", "--base-dir", str(self.base / "t3"), "mcp:launch:abc", "-"], "hello")])
        with mock.patch.object(watch.shutil, "which", return_value=None):
            self.assertIn("not on PATH", box.send("x", "hello"))
        with mock.patch.object(watch.shutil, "which", return_value="/opt/t3/bin/t3"), mock.patch.object(watch.subprocess, "run", fake_run):
            watch.Box(self.path, "/opt/t3/bin/t3").send("x", "hello")  # the path found at the start is the one used
        self.assertEqual(calls[-1][0][0], "/opt/t3/bin/t3")


class CommandTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.path = self.base / "t3" / "userdata" / "statev2.sqlite"
        self.path.parent.mkdir(parents=True)
        self.store = t3fixture.Store(self.path)
        self.new_box(sources=["t3"])
        config = self.home / ".config" / "jass" / "box.json"
        cfg = json.loads(config.read_text())
        cfg["sessions"]["t3_store"] = str(self.path)
        cfg["night"] = {"heavy_mb": 0}
        config.write_text(json.dumps(cfg))
        self.store.thread("a", title="Lane A")
        self.store.turn("a", "Go", "", status="running")
        self.store.thread("b", title="Lane B")
        self.store.turn("b", "Go", "Done.", status="failed")
        patcher = mock.patch.object(watch.shutil, "which", with_t3)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_status_lists_recent_threads(self):
        with mock.patch.object(watch.probe, "online", return_value=True), mock.patch.object(watch.probe, "memory", return_value={"available_mb": 5000}):
            out = self.ok("night", "status")
        self.assertRegex(out, r"failed +Lane B")
        self.assertRegex(out, r"working +Lane A")
        self.assertIn("box: network up, 5000 MB available", out)

    def test_check_reports_and_exits_1_on_a_fail(self):
        facts = {"power": {"platform": "mac", "idle_minutes": 10, "prevented_by": [], "sleep_disabled": False, "on_ac": True, "lid_closed": False},
                 "memory": {"total_mb": 32000, "available_mb": 20000}, "temp": {}, "home": {"path": "/h", "free_mb": 90000, "used_mb": 1},
                 "network": {"interface": "en5", "wireless": False, "signal_dbm": None, "probes": 9, "failed": []}}
        with mock.patch("jass.night.probe.collect", return_value=facts), mock.patch("jass.night.probe.t3", return_value={"found": False}):
            code, out, _ = self.jass("night", "check", "--hours", "6", "--json")
            rows = {(row["check"], row["status"]) for row in json.loads(out)}
            self.assertEqual(code, 1)
            self.assertIn(("sleep", "fail"), rows)
            self.assertIn(("threads", "warn"), rows)   # Lane B stopped on a failed run
            self.assertIn(("memory", "ok"), rows)      # one agent going, counted from the store
            facts["power"]["prevented_by"] = ["caffeinate"]
            code, out, _ = self.jass("night", "check", "--need", "true")
            self.assertEqual(code, 0)
            self.assertIn("0 to fix before you leave", out)

    def test_check_reports_a_store_it_cannot_read_instead_of_failing(self):
        import sqlite3

        with mock.patch("jass.night.probe.collect", return_value={}), mock.patch("jass.night.probe.t3", return_value={"found": False}), \
                mock.patch.object(t3state, "snapshot", side_effect=sqlite3.OperationalError("database is locked")):
            code, out, _ = self.jass("night", "check")
        self.assertIn("T3 Code threads not checked: database is locked", out)

    def test_watch_refuses_an_unknown_thread_and_a_second_instance(self):
        code, _, err = self.jass("night", "watch", "--thread", "nothing")
        self.assertEqual(code, 1)
        self.assertIn("matches 0 threads", err)
        from jass import paths

        with paths.Lock(paths.cache_dir() / "night" / "watch.lock") as free:
            self.assertTrue(free)
            code, _, err = self.jass("night", "watch", "--thread", "Lane A")
        self.assertEqual(code, 1)
        self.assertIn("already running", err)

    def test_watch_runs_until_the_deadline_and_writes_its_log(self):
        log = self.base / "watch.log"
        with mock.patch.object(watch.Box, "online", return_value=True), mock.patch.object(watch.Box, "available_mb", return_value=9000), \
                mock.patch.object(watch.Box, "sleep", lambda self, seconds: None), \
                mock.patch.object(watch.Box, "now", side_effect=[START + dt.timedelta(minutes=n) for n in range(40)]):
            code, out, _ = self.jass("night", "watch", "--thread", "Lane A", "--hours", "0.1", "--log", str(log))
        self.assertEqual(code, 0)
        self.assertIn("watching 1 thread until 23:06 UTC: Lane A", out)
        self.assertIn("the night is over. 0 automatic messages sent, 0 alerts", out)
        self.assertEqual(log.read_text(), out)

    def test_wake_exits_3_at_the_first_alert(self):
        self.store.request("a", "user_input")
        with mock.patch.object(watch.Box, "online", return_value=True), mock.patch.object(watch.Box, "available_mb", return_value=9000), \
                mock.patch.object(watch.Box, "sleep", lambda self, seconds: None):
            code, out, _ = self.jass("night", "watch", "--thread", "Lane A", "--wake", "--log", str(self.base / "w.log"))
        self.assertEqual(code, 3)
        self.assertIn("ALERT  Lane A: waits for the owner: user input", out)
        with mock.patch.object(watch.Box, "online", return_value=True), mock.patch.object(watch.Box, "available_mb", return_value=9000), \
                mock.patch.object(watch.Box, "sleep", lambda self, seconds: None), \
                mock.patch.object(watch.Box, "now", side_effect=[dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=n) for n in range(40)]):
            code, out, _ = self.jass("night", "watch", "--thread", "Lane A", "--wake", "--hours", "0.1", "--log", str(self.base / "w.log"))
        self.assertEqual(code, 0)  # started again: the same alert does not wake the caller a second time
        self.assertIn("Earlier this night: 0 messages sent, 1 alert raised; they are not repeated", out)
        self.assertNotIn("ALERT", out)

    def test_watch_refuses_to_start_where_it_could_not_send(self):
        with mock.patch.object(watch.shutil, "which", return_value=None):
            code, _, err = self.jass("night", "watch", "--thread", "Lane A")
            self.assertEqual(code, 1)
            self.assertIn("`t3` is not on PATH", err)
            with mock.patch.object(watch.Box, "sleep", lambda self, seconds: None), mock.patch.object(watch.probe, "online", return_value=True):
                code, _, _ = self.jass("night", "watch", "--thread", "Lane A", "--dry-run", "--hours", "0.001", "--log", str(self.base / "w.log"))
            self.assertEqual(code, 0)

    def test_a_signal_or_an_error_in_a_poll_does_not_end_the_watch_silently(self):
        polls = []

        def step(self_):
            polls.append(1)
            if len(polls) == 1:
                raise RuntimeError("boom")
            os.kill(os.getpid(), signal.SIGTERM)

        with mock.patch.object(watch.Watch, "step", step), mock.patch.object(watch.Box, "sleep", lambda self, seconds: None):
            code, out, _ = self.jass("night", "watch", "--thread", "Lane A", "--log", str(self.base / "w.log"))
        self.assertEqual(code, 130)
        self.assertIn("ALERT  this box: the watch hit an error and goes on: RuntimeError('boom')", out)
        self.assertIn("stop   interrupted or told to stop", out)
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)


class HeavyTests(Sandbox):
    def test_wrap_adds_a_capped_scope_only_when_it_can(self):
        command = ["npm", "run", "check"]
        self.assertEqual(heavy.wrap(command, 3000, "jass-heavy-1"),
                         ["systemd-run", "--user", "--scope", "--quiet", "--unit=jass-heavy-1", "-p", "MemoryMax=3000M", "-p", "MemorySwapMax=0", "--", *command])
        self.assertIs(heavy.wrap(command, 3000, ""), command)
        self.assertIs(heavy.wrap(command, 0, "jass-heavy-1"), command)

    def test_the_scope_says_whether_a_process_was_killed_at_the_cap(self):
        calls = []

        def shown(*argv, **_):
            calls.append(argv[2])
            return "Result=oom-kill\nActiveState=failed\n"

        with mock.patch.object(heavy.probe, "run", shown):
            self.assertTrue(heavy.hit_cap("jass-heavy-1"))
        self.assertEqual(calls, ["show", "reset-failed"])  # the failed scope is cleared away
        with mock.patch.object(heavy.probe, "run", return_value="Result=success\nActiveState=inactive\n"):
            self.assertFalse(heavy.hit_cap("jass-heavy-1"))
        with mock.patch.object(heavy.probe, "run", return_value=""):
            self.assertFalse(heavy.hit_cap("jass-heavy-1"))

    @unittest.skipUnless(heavy.can_scope(), "needs a systemd user manager")
    def test_a_worker_killed_at_the_cap_is_named_even_when_the_runner_reports_its_own_failure(self):
        hog = f"{sys.executable} -c 'x = bytearray(300 * 1024 * 1024)'"
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = heavy.run({}, ["sh", "-c", f"{hog} 2>/dev/null; exit 1"], 100)
        self.assertNotEqual(code, 0)
        self.assertIn("killed at the 100 MB memory cap", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(heavy.run({}, ["sh", "-c", "exit 5"], 100), 5)
        self.assertEqual(err.getvalue(), "")

    def test_cap_comes_from_the_flag_then_the_config_then_a_quarter_of_memory(self):
        with mock.patch.object(heavy.probe, "memory", return_value={"total_mb": 14436}):
            self.assertEqual(heavy.cap_mb({"night": {"heavy_mb": 2500}}, 900), 900)
            self.assertEqual(heavy.cap_mb({"night": {"heavy_mb": 2500}}), 2500)
            self.assertEqual(heavy.cap_mb({"night": {"heavy_mb": 0}}), 0)
            self.assertEqual(heavy.cap_mb({"night": {"heavy_mb": None}}), 3609)
        with mock.patch.object(heavy.probe, "memory", return_value={"total_mb": 4000}):
            self.assertEqual(heavy.cap_mb({}), 2000)

    def test_runs_the_command_and_returns_its_exit_code(self):
        self.new_box()
        with mock.patch.object(heavy, "can_scope", return_value=False), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(heavy.run({}, [sys.executable, "-c", "raise SystemExit(7)"], 0), 7)
            self.assertEqual(heavy.run({}, ["/nonexistent/command"], 0), 127)
        self.assertIn("cannot run /nonexistent/command", err.getvalue())
        code, _, err = self.jass("heavy")
        self.assertEqual(code, 1)
        self.assertIn("jass heavy -- npm run check", err)

    def test_a_heavy_command_inside_a_heavy_command_runs_straight_through(self):
        self.new_box()
        from jass import paths

        nested = [sys.executable, "-c", "import os, sys; sys.path.insert(0, sys.argv[1]); from jass.night import heavy; "
                  "raise SystemExit(heavy.run({'night': {'heavy_mb': 0}}, [sys.executable, '-c', 'raise SystemExit(9)']))", str(paths.STACK)]
        self.assertEqual(heavy.run({"night": {"heavy_mb": 0}}, nested), 9)  # the inner one does not wait for the lock its parent holds

    def test_a_second_heavy_command_waits_for_the_first(self):
        from jass import paths

        marker = self.base / "order.txt"
        script = "import sys, time; open(sys.argv[1], 'a').write(sys.argv[2] + ' start\\n'); time.sleep(float(sys.argv[3])); open(sys.argv[1], 'a').write(sys.argv[2] + ' end\\n')"
        env = dict(os.environ)
        launcher = [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); from jass.night import heavy; "
                    "raise SystemExit(heavy.run({'night': {'heavy_mb': 0}}, sys.argv[2:]))", str(paths.STACK)]
        first = subprocess.Popen([*launcher, sys.executable, "-c", script, str(marker), "first", "1.0"], env=env)
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.05)
        second = subprocess.run([*launcher, sys.executable, "-c", script, str(marker), "second", "0"], env=env, capture_output=True, text=True)
        first.wait()
        self.assertEqual(marker.read_text().split("\n")[:4], ["first start", "first end", "second start", "second end"])
        self.assertIn("another heavy command is running", second.stderr)


if __name__ == "__main__":
    unittest.main()
