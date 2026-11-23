import json
import tempfile
import unittest
from pathlib import Path

import jasshelp  # noqa: F401  (puts the stack on sys.path)

from jass import board, scale

NOW = 1_800_000_000.0
POOL = [{"id": "bx_1", "box": "boat-w1"}, {"id": "bx_2", "box": "boat-w2"}]


def cfg(**extra):
    s = {"enabled": True, "pool": POOL, "channels": ["fma-boat"], "per_vm": 2, "ttl_minutes": 120, "idle_stop_minutes": 15,
         "spend_warn_usd": 5, "spend_cap_usd": 8, "since": "2026-11-23T00:00:00Z", "max_starts_per_day": 30,
         "start_command": "systemctl --user start jass-board-vm"}
    s.update(extra)
    return {"box": "mac", "board": {"channel": "build"}, "scale": s}


class FakeBoat:
    def __init__(self, states, dollars=None, resume_fails=()):
        self.vms, self.cost, self.resume_fails, self.calls = dict(states), dict(dollars or {}), set(resume_fails), []

    def states(self):
        self.calls.append(("list",))
        return dict(self.vms)

    def dollars(self, vm, since):
        return self.cost.get(vm, 0.0)

    def resume(self, vm, ttl_s):
        self.calls.append(("resume", vm, ttl_s))
        if vm in self.resume_fails:
            return "Your sandbox could not start. Retry in a few minutes."
        self.vms[vm] = "ready"
        return ""

    def extend(self, vm, ttl_s):
        self.calls.append(("extend", vm, ttl_s))
        return ""

    def start_worker(self, vm, command):
        self.calls.append(("worker", vm, command))
        return ""

    def stop(self, vm):
        self.calls.append(("stop", vm))
        self.vms[vm] = "stopped"
        return ""

    def did(self, kind):
        return [c[1] for c in self.calls if c[0] == kind]


class FakeBoard:
    def __init__(self, ready=0, claimed=()):
        self.ready, self.claimed, self.asked = ready, list(claimed), []

    def tasks(self, channel, state):
        self.asked.append((channel, state))
        if state == "ready":
            return [{"message": {"id": f"r{n}", "labels": {}}} for n in range(self.ready)]
        return [{"message": {"id": f"c{n}", "labels": {board.BOX: box} if box else {}}} for n, box in enumerate(self.claimed)]


class TickTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.state = scale.State(self.dir / "state.json")
        self.logs = []

    def tick(self, boat, b, config=None, now=NOW):
        return scale.tick(config or cfg(), boat, b, self.state, now, self.logs.append)

    def test_disabled_does_nothing(self):
        boat = FakeBoat({"bx_1": "stopped"})
        self.assertEqual(self.tick(boat, FakeBoard(ready=3), cfg(enabled=False)), "scale.enabled is false")
        self.assertEqual(boat.calls, [])

    def test_ready_tasks_wake_enough_vms_and_start_their_workers(self):
        boat = FakeBoat({"bx_1": "stopped", "bx_2": "stopped"})
        b = FakeBoard(ready=3)
        self.tick(boat, b)
        self.assertEqual(boat.did("resume"), ["bx_1", "bx_2"])
        self.assertEqual(boat.did("worker"), ["bx_1", "bx_2"])
        self.assertIn(("resume", "bx_1", 7200), boat.calls)
        self.assertEqual({c for c, _ in b.asked}, {"build/fma-boat"})

    def test_one_vm_per_two_tasks_and_running_vms_count(self):
        boat = FakeBoat({"bx_1": "running", "bx_2": "stopped"})
        self.tick(boat, FakeBoard(ready=1, claimed=["boat-w1"]))
        self.assertEqual(boat.did("resume"), [])

    def test_a_vm_still_waking_counts_as_running(self):
        boat = FakeBoat({"bx_1": "provisioning", "bx_2": "stopped"})
        self.tick(boat, FakeBoard(ready=2))
        self.assertEqual(boat.did("resume"), [])

    def test_no_demand_starts_nothing(self):
        boat = FakeBoat({"bx_1": "stopped", "bx_2": "stopped"})
        self.tick(boat, FakeBoard())
        self.assertEqual(boat.did("resume"), [])

    def test_a_vm_holding_a_claimed_task_gets_its_auto_stop_pushed_back(self):
        boat = FakeBoat({"bx_1": "running", "bx_2": "running"})
        self.tick(boat, FakeBoard(claimed=["boat-w2"]))
        self.assertEqual(boat.did("extend"), ["bx_2"])

    def test_spend_cap_stops_the_pool_and_switches_off(self):
        boat = FakeBoat({"bx_1": "running", "bx_2": "stopped"}, dollars={"bx_1": 6.0, "bx_2": 2.5})
        summary = self.tick(boat, FakeBoard(ready=4))
        self.assertIn("cap", summary)
        self.assertEqual(boat.did("stop"), ["bx_1"])
        self.assertEqual(boat.did("resume"), [])
        self.assertTrue(self.state.data["off"])
        boat.calls.clear()
        self.assertTrue(self.tick(boat, FakeBoard(ready=4)).startswith("off"))
        self.assertEqual(boat.calls, [])

    def test_warning_is_logged_once(self):
        boat = FakeBoat({"bx_1": "stopped", "bx_2": "stopped"}, dollars={"bx_1": 5.5})
        self.tick(boat, FakeBoard())
        self.tick(boat, FakeBoard())
        self.assertEqual(sum("warning" in line for line in self.logs), 1)

    def test_daily_start_cap(self):
        boat = FakeBoat({"bx_1": "stopped", "bx_2": "stopped"})
        self.tick(boat, FakeBoard(ready=4), cfg(max_starts_per_day=1))
        self.assertEqual(boat.did("resume"), ["bx_1"])
        self.assertTrue(any("max_starts_per_day" in line for line in self.logs))

    def test_failed_resume_is_counted_and_retried_next_tick(self):
        boat = FakeBoat({"bx_1": "stopped", "bx_2": "stopped"}, resume_fails={"bx_1"})
        self.tick(boat, FakeBoard(ready=1))
        self.assertEqual(boat.did("worker"), [])
        boat.resume_fails.clear()
        self.tick(boat, FakeBoard(ready=1))
        self.assertEqual(boat.did("worker"), ["bx_1"])
        self.assertEqual(self.state.starts_today("2027-01-15"), 2)

    def test_a_vm_running_without_demand_is_stopped_after_the_idle_limit(self):
        boat = FakeBoat({"bx_1": "running", "bx_2": "stopped"})
        self.tick(boat, FakeBoard(), now=NOW)
        self.tick(boat, FakeBoard(), now=NOW + 600)
        self.assertEqual(boat.did("stop"), [])
        self.tick(boat, FakeBoard(), now=NOW + 900)
        self.assertEqual(boat.did("stop"), ["bx_1"])

    def test_demand_resets_the_idle_clock(self):
        boat = FakeBoat({"bx_1": "running", "bx_2": "stopped"})
        self.tick(boat, FakeBoard(), now=NOW)
        self.tick(boat, FakeBoard(ready=1), now=NOW + 600)
        self.tick(boat, FakeBoard(), now=NOW + 900)
        self.assertEqual(boat.did("stop"), [])

    def test_missing_pool_or_channels_is_an_error(self):
        with self.assertRaises(scale.ScaleError):
            self.tick(FakeBoat({}), FakeBoard(), cfg(pool=[]))
        with self.assertRaises(scale.ScaleError):
            self.tick(FakeBoat({}), FakeBoard(), cfg(channels=[]))

    def test_pool_entries_may_be_plain_ids(self):
        self.assertEqual(scale.settings({"scale": {"pool": ["bx_9"]}})["pool"], [{"id": "bx_9", "box": ""}])


class StateTests(unittest.TestCase):
    def test_starts_reset_each_day_and_state_survives_a_reload(self):
        path = Path(tempfile.mkdtemp()) / "state.json"
        s = scale.State(path)
        s.count_start("2026-11-23")
        s.count_start("2026-11-23")
        s.save()
        again = scale.State(path)
        self.assertEqual(again.starts_today("2026-11-23"), 2)
        again.count_start("2026-11-24")
        self.assertEqual(again.starts_today("2026-11-24"), 1)
        self.assertEqual(again.starts_today("2026-11-23"), 0)
        self.assertEqual(json.loads(path.read_text())["off"], False)


class BoatTests(unittest.TestCase):
    def test_resume_reads_the_last_event_line(self):
        from unittest import mock
        import subprocess

        events = '{"event":"accepted","id":"bx_1"}\n{"event":"state","state":"starting"}\n{"event":"ready","id":"bx_1","state":"ready"}\n'
        with mock.patch("jass.scale.subprocess.run", return_value=subprocess.CompletedProcess([], 0, events, "")):
            self.assertEqual(scale.Boat().resume("bx_1", 7200), "")
        failed = '{"event":"accepted"}\n{"code":"machine_not_running","error":"Sandbox bx_1 is in error","status":409}\n'
        with mock.patch("jass.scale.subprocess.run", return_value=subprocess.CompletedProcess([], 0, failed, "")):
            self.assertEqual(scale.Boat().resume("bx_1", 7200), "Sandbox bx_1 is in error")

    def test_worker_starts_through_a_login_shell(self):
        from unittest import mock
        import subprocess

        with mock.patch("jass.scale.subprocess.run", return_value=subprocess.CompletedProcess([], 0, '{"pid":1}', "")) as run:
            self.assertEqual(scale.Boat("boat").start_worker("bx_1", "systemctl --user start jass-board-vm"), "")
        self.assertEqual(run.call_args[0][0], ["boat", "exec", "--detach", "--json", "bx_1", "--", "bash", "-lc", "systemctl --user start jass-board-vm"])


class CommandTests(unittest.TestCase):
    def test_off_and_on_and_lock(self):
        import os
        from types import SimpleNamespace
        from unittest import mock

        home = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"JASS_HOME": home}):
            args = SimpleNamespace(off=True, on=False, status=False, as_profile="lead", verbose=False)
            self.assertEqual(scale.run(cfg(), args), 0)
            state = scale.State(scale.scale_dir() / "state.json")
            self.assertTrue(state.data["off"])
            args.off, args.on = False, True
            scale.run(cfg(), args)
            self.assertFalse(scale.State(scale.scale_dir() / "state.json").data["off"])
            # A tick that finds the lock held by another tick does nothing.
            from jass import paths
            with paths.Lock(scale.scale_dir() / "lock"):
                with mock.patch("jass.scale.tick") as tick:
                    args.on = False
                    self.assertEqual(scale.run(cfg(), args), 0)
                    tick.assert_not_called()


if __name__ == "__main__":
    unittest.main()
