import unittest
from unittest import mock

import jasshelp  # noqa: F401  (puts the repository on sys.path)

from jass.night import check, probe

MEMINFO = """MemTotal:       14782932 kB
MemFree:         2253004 kB
MemAvailable:   10758032 kB
SwapTotal:       7391228 kB
SwapFree:        5027324 kB
"""
SWAPS = """Filename				Type		Size		Used		Priority
/dev/zram0                              partition	7391228		2363904		100
"""
WIRELESS = """Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE
 face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22
wlp64s0f7: 0000   29.  -81.  -256        0      0      0     27      0        0
"""
PMSET = """System-wide power settings:
 SleepDisabled		0
Currently in use:
 standby              1
 Sleep On Power Button 1
 disksleep            10
 sleep                1 (sleep prevented by powerd, caffeinate)
 displaysleep         2
"""
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               100000.
Pages active:                             400000.
Pages inactive:                           200000.
Pages speculative:                         20000.
"""


def statuses(rows, name):
    return [status for status, check_name, _ in rows if check_name == name]


def detail(rows, name):
    return " ".join(text for _, check_name, text in rows if check_name == name)


class ParserTests(unittest.TestCase):
    def test_linux_memory_and_swap(self):
        self.assertEqual(probe.parse_meminfo(MEMINFO), {"total_mb": 14436, "available_mb": 10505, "swap_total_mb": 7217, "swap_free_mb": 4909})
        self.assertEqual(probe.parse_swaps(SWAPS), [{"name": "/dev/zram0", "zram": True, "size_mb": 7217, "used_mb": 2308}])
        self.assertEqual(probe.parse_pressure("some avg10=0.00 avg60=12.50 avg300=3.00 total=1\nfull avg10=0 avg60=4.00 avg300=0 total=1\n"), 12.5)
        self.assertIsNone(probe.parse_pressure(""))

    def test_mac_memory(self):
        self.assertEqual(probe.parse_vm_stat(VM_STAT, 36864), {"total_mb": 36864, "available_mb": 5000})
        self.assertEqual(probe.parse_vm_stat("", 36864), {})

    def test_routes_and_wireless(self):
        self.assertEqual(probe.parse_route("1.1.1.1 via 192.168.1.1 dev wlp64s0f7 src 192.168.1.5 uid 1000"), "wlp64s0f7")
        self.assertEqual(probe.parse_route("   route to: default\n  interface: en0\n      flags: <UP>\n"), "en0")
        self.assertEqual(probe.parse_wireless(WIRELESS), {"wlp64s0f7": -81})
        ports = "Hardware Port: Ethernet\nDevice: en5\n\nHardware Port: Wi-Fi\nDevice: en0\nEthernet Address: 00:00\n"
        self.assertEqual(probe.parse_hardware_ports(ports), {"en0"})

    def test_pmset(self):
        self.assertEqual(probe.parse_pmset(PMSET), {"idle_minutes": 1, "prevented_by": ["powerd", "caffeinate"], "sleep_disabled": False})
        self.assertEqual(probe.parse_pmset(" sleep                0\n"), {"idle_minutes": 0, "prevented_by": []})
        self.assertEqual(probe.parse_batt("Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t64%; discharging; 3:10 remaining\n"),
                         {"on_ac": False, "charge": 64})
        self.assertEqual(probe.parse_batt("Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t100%; charged;\n"), {"on_ac": True, "charge": 100})

    def test_mount_of_picks_the_deepest_mount(self):
        mounts = "/dev/sda1 / ext4 rw 0 0\ntmpfs /tmp tmpfs rw 0 0\n"
        self.assertEqual(probe.mount_of(probe.Path("/tmp/claude/x"), mounts), ("/tmp", "tmpfs"))
        self.assertEqual(probe.mount_of(probe.Path("/tmpfiles"), mounts), ("/", "ext4"))


class SleepTests(unittest.TestCase):
    def mac(self, **facts):
        return check.sleep_rows(dict({"platform": "mac", "idle_minutes": 10, "prevented_by": [], "sleep_disabled": False,
                                      "on_ac": True, "lid_closed": False}, **facts), 8)

    def test_a_mac_that_will_idle_sleep_fails_with_the_command(self):
        rows = self.mac()
        self.assertEqual(statuses(rows, "sleep"), ["fail"])
        self.assertIn("caffeinate -ims -t 28800", detail(rows, "sleep"))

    def test_a_transient_holder_is_not_enough(self):
        rows = self.mac(prevented_by=["powerd", "coreaudiod"])
        self.assertEqual(statuses(rows, "sleep"), ["fail"])
        self.assertIn("only powerd, coreaudiod", detail(rows, "sleep"))

    def test_caffeinate_or_no_idle_sleep_passes(self):
        self.assertEqual(statuses(self.mac(prevented_by=["powerd", "caffeinate"]), "sleep"), ["ok"])
        self.assertEqual(statuses(self.mac(idle_minutes=0), "sleep"), ["ok"])
        self.assertEqual(statuses(self.mac(sleep_disabled=True), "sleep"), ["ok"])

    def test_battery_and_closed_lid_fail_even_with_caffeinate(self):
        rows = self.mac(prevented_by=["caffeinate"], on_ac=False, charge=40, lid_closed=True)
        self.assertEqual((statuses(rows, "sleep"), statuses(rows, "power"), statuses(rows, "lid")), (["ok"], ["fail"], ["fail"]))
        self.assertIn("40%", detail(rows, "power"))

    def test_linux(self):
        self.assertEqual(statuses(check.sleep_rows({"platform": "linux", "sleep_masked": False, "idle_action": "ignore", "on_ac": None}, 8), "sleep"), ["ok"])
        self.assertEqual(statuses(check.sleep_rows({"platform": "linux", "sleep_masked": True, "idle_action": "suspend"}, 8), "sleep"), ["ok"])
        rows = check.sleep_rows({"platform": "linux", "sleep_masked": False, "idle_action": "suspend"}, 2)
        self.assertEqual(statuses(rows, "sleep"), ["fail"])
        self.assertIn("systemd-inhibit --what=sleep:idle sleep 7200", detail(rows, "sleep"))
        self.assertEqual(statuses(check.sleep_rows({"platform": "linux", "sleep_masked": False, "idle_action": None}, 8), "sleep"), ["warn"])
        self.assertEqual(statuses(check.sleep_rows({}, 8), "sleep"), ["warn"])


class SessionTests(unittest.TestCase):
    def test_recent_window_server_crashes_are_found_and_warned_about(self):
        import os
        import tempfile
        import time

        with tempfile.TemporaryDirectory() as folder:
            for name, age_days, text in (("WindowServer-2026-01-02-030623.ips", 1, "WSIOSurfaceDebugTallyAndAbort"), ("WindowServer-2026-01-01-010101.ips", 2, "watchdog"),
                                         ("WindowServer-2025-12-01-010101.ips", 30, ""), ("Other-2026-01-02.ips", 1, "")):
                path = os.path.join(folder, name)
                with open(path, "w") as handle:
                    handle.write(text)
                os.utime(path, (time.time() - age_days * 86400,) * 2)
            with mock.patch.object(probe, "run", return_value=""):
                found = probe.session(7, folder)
                self.assertEqual(probe.session(7, "/nonexistent/folder"), {"crashes": [], "hours": None})
        self.assertEqual([crash["buffers"] for crash in found["crashes"]], [False, True])
        rows = check.session_rows(dict(found, hours=26.5))
        self.assertEqual(statuses(rows, "session"), ["fail"])  # crashed twice, and this session is a day old
        self.assertEqual(statuses(check.session_rows(dict(found, hours=2)), "session"), ["warn"])  # logged in again since
        self.assertEqual(statuses(check.session_rows(dict(found, crashes=found["crashes"][:1], hours=26.5)), "session"), ["warn"])
        self.assertIn("crashed 2 times in the last 7 days", detail(rows, "session"))
        self.assertIn("This session is 26.5 hours old. Log out and back in", detail(rows, "session"))
        self.assertIn("Read the report", detail(check.session_rows({"crashes": [{"when": "2026-01-01 01:01", "buffers": False}]}), "session"))
        self.assertEqual(check.session_rows({}), [])

    def test_process_age(self):
        self.assertEqual([probe.parse_etime(text) for text in ("05:12", " 03:30:00", "2-01:30:59", "x")], [0.1, 3.5, 49.5, None])


class MemoryTests(unittest.TestCase):
    BOX = {"total_mb": 14436, "available_mb": 10505, "swaps": [{"name": "/dev/zram0", "zram": True, "size_mb": 7217, "used_mb": 2308}], "oomd": True}
    TEMP = {"path": "/tmp", "used_mb": 2500, "free_mb": 4600, "in_memory": True}

    def rows(self, agents, box=None, temp=None):
        return check.memory_rows(self.BOX if box is None else box, self.TEMP if temp is None else temp, agents, 1500, 3600)

    def test_six_agents_do_not_fit_a_14_gb_box(self):
        rows = self.rows(6)
        self.assertEqual(statuses(rows, "memory"), ["fail"])
        self.assertIn("6 agents x 1500 MB + one heavy command at 3600 MB = 12600 MB, against 9936 MB", detail(rows, "memory"))
        self.assertIn("This box fits 4 agents", detail(rows, "memory"))
        self.assertIn("2500 MB of temp files held in memory", detail(rows, "memory"))

    def test_temp_files_in_memory_take_room_from_agents(self):
        self.assertEqual(statuses(self.rows(5), "memory"), ["fail"])
        rows = self.rows(5, temp=dict(self.TEMP, used_mb=200))
        self.assertEqual((statuses(rows, "memory"), statuses(rows, "temp")), (["warn"], ["ok"]))
        self.assertEqual(statuses(self.rows(4, temp=dict(self.TEMP, used_mb=200)), "memory"), ["ok"])

    def test_the_ceiling_t3_sets_for_all_agents_counts_too(self):
        box = dict(self.BOX, total_mb=64000, agents_high_mb=8292)
        rows = self.rows(6, box=box, temp={})
        self.assertEqual(statuses(rows, "memory"), ["fail"])
        self.assertIn("above 8292 MB", detail(rows, "memory"))
        self.assertIn("This box fits 5 agents", detail(rows, "memory"))
        self.assertEqual(statuses(self.rows(5, box=box, temp={}), "memory"), ["ok"])

    def test_zram_oomd_and_full_temp_are_called_out(self):
        rows = self.rows(2)
        self.assertEqual((statuses(rows, "swap"), statuses(rows, "oomd"), statuses(rows, "temp")), (["warn"], ["warn"], ["warn"]))
        self.assertIn("jass heavy", detail(rows, "oomd"))

    def test_a_large_box_with_disk_swap_and_disk_temp_is_quiet(self):
        box = {"total_mb": 65536, "available_mb": 40000, "swaps": [{"name": "/swapfile", "zram": False, "size_mb": 8192, "used_mb": 0}], "oomd": False}
        rows = self.rows(8, box=box, temp={"path": "/var/tmp", "used_mb": 90000, "free_mb": 5, "in_memory": False})
        self.assertEqual([status for status, _, _ in rows], ["ok"])

    def test_memory_already_short_before_the_night(self):
        self.assertEqual(statuses(self.rows(1, box=dict(self.BOX, available_mb=2000), temp={}), "memory now"), ["warn"])

    def test_unreadable_memory_warns(self):
        self.assertEqual(check.memory_rows({}, {}, 3)[0][0], "warn")


class T3Tests(unittest.TestCase):
    def t3(self, windows, **facts):
        provider = {"ready": True, "signed_in": True, "plan": "Plan", "windows": windows}
        return dict({"found": True, "providers": {"Claude": provider}, "resume_after_limit": True, "keep_awake": True}, **facts)

    def test_usage_windows(self):
        week = {"label": "Weekly", "used": 40.0, "resets_in_hours": 90.0}
        self.assertEqual(check.t3_rows(self.t3([week]), 8), [("ok", "usage", "Claude (Plan): Weekly 40%")])
        rows = check.t3_rows(self.t3([dict(week, used=82.0)]), 8)
        self.assertEqual(statuses(rows, "usage"), ["warn"])
        self.assertIn("82% used, resets in 90.0 h", detail(rows, "usage"))
        self.assertEqual(statuses(check.t3_rows(self.t3([dict(week, used=97.0)]), 8), "usage"), ["fail"])
        self.assertEqual(statuses(check.t3_rows(self.t3([dict(week, used=97.0, resets_in_hours=0.5)]), 8), "usage"), ["ok"])

    def test_signed_out_provider_and_settings(self):
        rows = check.t3_rows(self.t3([], resume_after_limit=False, keep_awake=False,
                                     providers={"Codex": {"ready": True, "signed_in": False, "plan": "", "windows": []}}), 8)
        self.assertEqual((statuses(rows, "provider"), statuses(rows, "t3 setting")), (["fail"], ["warn", "warn"]))
        self.assertIn("autoResumeLimitedThreads", detail(rows, "t3 setting"))
        self.assertEqual(check.t3_rows({"found": False}, 8), [])

    def test_usage_cache_parser(self):
        text = ('{"usageLimits": {"windows": [{"id": "seven_day", "label": "Weekly", "usedPercent": 91, "resetsAt": "2026-11-26T08:00:00.000Z"},'
                ' {"id": "five_hour", "usedPercent": 10}, {"label": "broken"}]}}')
        now = probe.dt.datetime(2026, 11, 25, 8, tzinfo=probe.dt.timezone.utc).timestamp()
        self.assertEqual(probe.parse_usage(text, now), [{"label": "Weekly", "used": 91.0, "resets_in_hours": 24.0},
                                                         {"label": "five_hour", "used": 10.0, "resets_in_hours": None}])
        self.assertEqual(probe.parse_usage("not json", now), [])

    def test_thread_rows(self):
        threads = [{"id": "a", "title": "Lane A", "state": "working", "detail": ""},
                   {"id": "b", "title": "Lane B", "state": "blocked", "detail": "waits for the owner: user input"},
                   {"id": "c", "title": "Lane C", "state": "failed", "detail": "run 2 failed"},
                   {"id": "d", "title": "Lane D", "state": "ended", "detail": "run 1 completed"}]
        rows = check.thread_rows(threads)
        self.assertEqual([status for status, _, _ in rows], ["fail", "warn", "ok"])
        self.assertIn("Lane B (waits for the owner: user input)", rows[0][2])
        self.assertIn("Lane C", rows[1][2])
        self.assertEqual(check.thread_rows(threads[:1])[0], ("ok", "threads", "1 working now: Lane A; none waits for you"))
        self.assertEqual(check.thread_rows(None, "no store")[0][0], "warn")


class NetworkAndNeedTests(unittest.TestCase):
    def test_weak_wifi_warns_and_failed_probes_fail(self):
        rows = check.network_rows({"interface": "wlan0", "wireless": True, "signal_dbm": -81, "probes": 9, "failed": []})
        self.assertEqual((statuses(rows, "network"), statuses(rows, "link")), (["ok"], ["warn"]))
        self.assertIn("-81 dBm (weak)", detail(rows, "link"))
        rows = check.network_rows({"interface": "eth0", "wireless": False, "signal_dbm": None, "probes": 9, "failed": ["a:443", "a:443"]})
        self.assertEqual((statuses(rows, "network"), statuses(rows, "link")), (["fail"], ["ok"]))
        self.assertIn("2 of 9", detail(rows, "network"))
        self.assertEqual(check.network_rows({})[0][0], "warn")

    def test_need_commands_must_exit_zero(self):
        rows = check.need_rows(["true", "exit 3", "sleep 5"], timeout=0.5)
        self.assertEqual([status for status, _, _ in rows], ["ok", "fail", "fail"])
        self.assertIn("exit 3", rows[1][2])
        self.assertIn("waiting for a password", rows[2][2])

    def test_disk(self):
        rows = check.disk_rows({"path": "/home/x", "free_mb": 900, "used_mb": 1}, {"path": "/tmp", "free_mb": 1, "used_mb": 1, "in_memory": True}, {})
        self.assertEqual([(status, name) for status, name, _ in rows], [("warn", "disk")])


if __name__ == "__main__":
    unittest.main()
