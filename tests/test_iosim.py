"""Unit tests for the ios-sim skill's iosim CLI: the parts that run without Xcode or a Simulator."""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "ios-sim" / "scripts"
spec = importlib.util.spec_from_file_location("iosim", SCRIPTS / "iosim.py")
iosim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(iosim)


def write_png(path, width, height, colour=(255, 255, 255)):
    raw = b"".join(b"\x00" + bytes(colour) * width for _ in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def test_shorthand_steps_normalize(self):
        plan = iosim.parse_plan({"steps": [
            {"open": "http://box:3000/"},
            {"wait": 300},
            {"tap": "#go"},
            {"type": "hello"},
            {"shot": "home page"},
            {"audit": True},
            {"textSize": "ax2"},
            {"scroll": "down"},
            {"scroll": "#footer"},
            {"js": "document.title", "as": "title"},
        ]})
        kinds = [s["kind"] for s in plan["steps"]]
        self.assertEqual(kinds, ["open", "wait", "tap", "type", "shot", "audit", "text-size", "scroll", "scroll", "eval"])
        s = plan["steps"]
        self.assertEqual(s[0]["url"], "http://box:3000/")
        self.assertEqual(s[1]["ms"], 300)
        self.assertEqual(s[2]["selector"], "#go")
        self.assertEqual(s[3]["text"], "hello")
        self.assertFalse(s[3]["keepKeyboard"])
        self.assertEqual(s[4]["shotName"], "home-page")
        self.assertEqual(s[5]["failOn"], "fail")
        self.assertEqual(s[6]["size"], "accessibility-large")
        self.assertEqual(s[7]["direction"], "down")
        self.assertEqual(s[8]["to"], "#footer")
        self.assertEqual(s[9]["as"], "title")
        self.assertFalse(plan["video"])

    def test_bare_list_is_a_plan(self):
        self.assertEqual(len(iosim.parse_plan([{"open": "https://example.com"}])["steps"]), 1)

    def test_errors_name_the_step(self):
        cases = [
            ({"steps": [{"open": "https://a"}, {"open": "no-scheme"}]}, "step 2"),
            ({"steps": [{"tap": {"selector": "a", "label": "b"}}]}, "one of selector, label or at"),
            ({"steps": [{"click": "#a"}]}, "unknown action 'click'"),
            ({"steps": [{"open": "https://a", "shot": "x"}]}, "exactly one action key"),
            ({"steps": [{"textSize": "huge"}]}, "unknown text size"),
            ({"steps": [{"assert": {"eval": "1"}}]}, "eval needs equals"),
            ({"steps": [{"audit": {"failOn": "sometimes"}}]}, "failOn"),
            ({"steps": []}, "at least one step"),
            ({"steps": [{"open": "https://a"}], "colour": "red"}, "unknown plan keys: colour"),
            ({"steps": [{"tap": {"at": [1]}}]}, "at is [x, y]"),
        ]
        for obj, needle in cases:
            with self.subTest(needle=needle):
                with self.assertRaises(iosim.IosimError) as cm:
                    iosim.parse_plan(obj)
                self.assertIn(needle, str(cm.exception))
                self.assertEqual(cm.exception.code, 2)

    def test_variables_substitute_and_unknown_fails(self):
        plan = iosim.parse_plan({"vars": {"PATH": "/a"}, "steps": [{"open": "${BASE}${PATH}"}]}, variables={"BASE": "http://box:3000"})
        self.assertEqual(plan["steps"][0]["url"], "http://box:3000/a")
        with self.assertRaises(iosim.IosimError) as cm:
            iosim.parse_plan({"steps": [{"open": "${NOPE}/"}]})
        self.assertIn("--var NOPE=", str(cm.exception))

    def test_files_are_inlined_relative_to_the_plan(self):
        (self.dir / "check.js").write_text("return 1 + 1;")
        (self.dir / "audit.js").write_text("return [];")
        plan = iosim.parse_plan({"steps": [{"eval": {"file": "check.js"}}, {"audit": {"script": "audit.js", "ignore": ["tiny-text"]}}]}, str(self.dir))
        self.assertEqual(plan["steps"][0]["source"], "return 1 + 1;")
        self.assertTrue(plan["steps"][0]["body"])
        self.assertEqual(plan["steps"][1]["script"]["source"], "return [];")
        self.assertEqual(plan["steps"][1]["ignore"], ["tiny-text"])
        with self.assertRaises(iosim.IosimError):
            iosim.parse_plan({"steps": [{"eval": {"file": "missing.js"}}]}, str(self.dir))

    def test_resolved_plan_round_trips_through_stdin(self):
        plan = iosim.parse_plan({"name": "x", "steps": [{"open": "https://example.com"}]})
        payload = json.dumps(dict(plan, iosimResolvedPlan=1))
        with mock.patch.object(sys, "stdin", io.StringIO(payload)):
            self.assertEqual(iosim.load_plan_arg("-", {}), plan)

    def test_parse_vars(self):
        self.assertEqual(iosim.parse_vars(["A=1", "B=x=y"]), {"A": "1", "B": "x=y"})
        with self.assertRaises(iosim.IosimError):
            iosim.parse_vars(["A"])


class ProtocolTest(unittest.TestCase):
    def test_wrap_for_target_nests_the_inner_command(self):
        msg = iosim.wrap_for_target("page-7", 3, "Runtime.evaluate", {"expression": "1"})
        self.assertEqual(msg["method"], "Target.sendMessageToTarget")
        self.assertEqual(msg["params"]["targetId"], "page-7")
        self.assertEqual(json.loads(msg["params"]["message"]), {"id": 3, "method": "Runtime.evaluate", "params": {"expression": "1"}})

    def test_unwrap_from_target(self):
        inner = {"id": 3, "result": {"result": {"type": "number", "value": 2}}}
        outer = {"method": "Target.dispatchMessageFromTarget", "params": {"targetId": "page-7", "message": json.dumps(inner)}}
        self.assertEqual(iosim.unwrap_from_target(outer), inner)
        self.assertIsNone(iosim.unwrap_from_target({"method": "Target.targetCreated", "params": {}}))
        self.assertIsNone(iosim.unwrap_from_target({"method": "Target.dispatchMessageFromTarget", "params": {"message": "{"}}))

    def test_js_program_wraps_expression_and_body(self):
        expr = iosim.js_program("document.title")
        self.assertIn("(\ndocument.title\n)", expr)
        self.assertTrue(expr.startswith("(async () =>"))
        body = iosim.js_program("return 1;", is_body=True)
        self.assertIn("{\nreturn 1;\n}", body)
        self.assertIn("JSON.stringify({ok: false", body)

    def test_ws_frame_is_masked_and_sized(self):
        key = b"\x01\x02\x03\x04"
        frame = iosim.ws_frame("hi", mask_key=key)
        self.assertEqual(frame[0], 0x81)
        self.assertEqual(frame[1], 0x80 | 2)
        self.assertEqual(frame[2:6], key)
        self.assertEqual(bytes(b ^ key[i % 4] for i, b in enumerate(frame[6:])), b"hi")
        long = iosim.ws_frame("x" * 300, mask_key=key)
        self.assertEqual(long[1], 0x80 | 126)
        self.assertEqual(struct.unpack(">H", long[2:4])[0], 300)

    def test_pick_page_prefers_url_then_host_then_last(self):
        pages = [
            {"url": "https://a.test/", "webSocketDebuggerUrl": "ws://1"},
            {"url": "https://b.test/x", "webSocketDebuggerUrl": "ws://2"},
            {"url": "https://c.test/", "webSocketDebuggerUrl": "ws://3"},
            {"url": "https://d.test/", "webSocketDebuggerUrl": ""},
        ]
        self.assertEqual(iosim.pick_page(pages, "https://a.test")["webSocketDebuggerUrl"], "ws://1")
        self.assertEqual(iosim.pick_page(pages, "https://b.test/other")["webSocketDebuggerUrl"], "ws://2")
        self.assertEqual(iosim.pick_page(pages, "https://zzz.test/")["webSocketDebuggerUrl"], "ws://3")
        self.assertIsNone(iosim.pick_page([]))

    def test_url_matches(self):
        self.assertTrue(iosim.url_matches("http://box:3000/recipes/1", "http://box:3000/recipes"))
        self.assertTrue(iosim.url_matches("http://box:3000/", "http://box:3000"))
        self.assertFalse(iosim.url_matches("http://box:3000/sign-in", "http://box:3000/recipes"))
        self.assertFalse(iosim.url_matches("http://other/", "http://box/"))

    def test_web_origin_and_tap_point(self):
        nodes = [
            {"type": "Application", "depth": 0, "rect": {"x": 0, "y": 0, "width": 393, "height": 852}},
            {"type": "WebView", "depth": 5, "rect": {"x": 0, "y": 0, "width": 393, "height": 852}},
            {"type": "Other", "depth": 6, "label": "Page", "rect": {"x": 0, "y": 0, "width": 393, "height": 852}},
            {"type": "Other", "depth": 6, "label": "Page", "rect": {"x": 0, "y": 59, "width": 393, "height": 793}},
            {"type": "Button", "depth": 2, "label": "Back", "rect": {"x": 34, "y": 770, "width": 48, "height": 48}},
        ]
        origin = iosim.web_origin_from_snapshot(nodes, "Page")
        self.assertEqual(origin, (0, 59))
        self.assertIsNone(iosim.web_origin_from_snapshot([{"type": "Other", "rect": {"x": 0, "y": 0, "width": 1, "height": 1}}]))
        self.assertEqual(iosim.contents_to_points({"x": 50, "y": 150, "width": 90, "height": 44}, origin), (95.0, 231.0))

    def test_normalize_text_size_and_audit(self):
        self.assertEqual(iosim.normalize_text_size("AX5"), "accessibility-extra-extra-extra-large")
        self.assertEqual(iosim.normalize_text_size("default"), "large")
        self.assertEqual(iosim.normalize_text_size("Extra Large"), "extra-large")
        res = iosim.normalize_audit([{"rule": "x", "severity": "warn"}, {"message": "bare"}, "text"])
        self.assertEqual(res["counts"], {"fail": 2, "warn": 1})
        with self.assertRaises(iosim.IosimError):
            iosim.normalize_audit("nope")


class CaptureTest(unittest.TestCase):
    def test_png_size_and_pixel_stats(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.png"
            write_png(p, 6, 4)
            self.assertEqual(iosim.png_size(p), (6, 4))
            (Path(d) / "b.png").write_bytes(b"nope")
            with self.assertRaises(iosim.IosimError):
                iosim.png_size(Path(d) / "b.png")
        flat = iosim.pixel_stats([(10, 10, 10)] * 50)
        self.assertEqual(flat["stdLuminance"], 0)
        busy = iosim.pixel_stats([(0, 0, 0), (255, 255, 255)] * 25)
        self.assertGreater(busy["stdLuminance"], 100)

    def test_bmp_pixels_reads_24_bit(self):
        width, height = 2, 2
        row = bytes([0, 0, 255, 0, 255, 0]) + b"\x00\x00"
        pixels = row * height
        header = b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54) + struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixels), 0, 0, 0, 0)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.bmp"
            p.write_bytes(header + pixels)
            self.assertEqual(iosim.bmp_pixels(p)[:2], [(255, 0, 0), (0, 255, 0)])


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.env = dict(os.environ, IOSIM_HOME=str(self.dir / "home"))
        for k in ("IOSIM_REMOTE", "IOSIM_DEVICE", "IOSIM_AGENT_DEVICE"):
            self.env.pop(k, None)

    def cli(self, *args, env=None, stdin=None):
        proc = subprocess.run([str(SCRIPTS / "iosim"), *args], capture_output=True, text=True, env=env or self.env, input=stdin, timeout=60)
        return proc.returncode, proc.stdout, proc.stderr

    def test_help_lists_commands(self):
        code, out, _ = self.cli("--help")
        self.assertEqual(code, 0)
        for cmd in ("doctor", "device", "open", "eval", "tap", "type", "scroll", "shot", "record", "audit", "run", "remote", "contact-sheet"):
            self.assertIn(cmd, out)

    def test_run_check_resolves_without_a_simulator(self):
        plan = self.dir / "plan.json"
        plan.write_text(json.dumps({"steps": [{"open": "${BASE}/"}, {"shot": "home"}]}))
        code, out, _ = self.cli("run", str(plan), "--out", str(self.dir / "ev"), "--var", "BASE=http://box:3000", "--check")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["plan"]["steps"][0]["url"], "http://box:3000/")

    def test_bad_plan_exits_2_with_json(self):
        plan = self.dir / "plan.json"
        plan.write_text(json.dumps({"steps": [{"click": "#a"}]}))
        code, out, _ = self.cli("run", str(plan), "--out", str(self.dir / "ev"), "--check")
        self.assertEqual(code, 2)
        body = json.loads(out)
        self.assertFalse(body["ok"])
        self.assertIn("plan.md", body["fix"])

    def test_check_shot_flags_blank_and_size(self):
        p = self.dir / "s.png"
        write_png(p, 30, 60)
        code, out, _ = self.cli("check-shot", str(p), "--expect", "1179x2556")
        body = json.loads(out)
        self.assertEqual(code, 1)
        self.assertIn("size 30x60, expected 1179x2556", body["problems"])

    def test_doctor_without_simulator_points_at_remote(self):
        with mock.patch.object(iosim, "have_simctl", return_value=False), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IOSIM_REMOTE", None)
            checks = iosim.doctor()
        sim = next(c for c in checks if c["name"] == "simulator")
        self.assertFalse(sim["ok"])
        self.assertIn("IOSIM_REMOTE", sim["fix"])
        self.assertIn("T3", sim["fix"])

    def test_no_simulator_and_no_remote_explains(self):
        with mock.patch.object(iosim, "have_simctl", return_value=False), mock.patch.dict(os.environ, {"IOSIM_HOME": str(self.dir)}):
            os.environ.pop("IOSIM_REMOTE", None)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = iosim.main(["open", "https://example.com"])
        self.assertEqual(code, 1)
        self.assertIn("remote options", json.loads(buf.getvalue())["fix"])

    def fake_bin(self):
        """ssh and scp stand-ins that run locally: ssh runs the command in a shell rooted at a fake remote home."""
        bin_dir, home = self.dir / "bin", self.dir / "remote-home"
        bin_dir.mkdir()
        home.mkdir()
        log = self.dir / "calls.log"
        (bin_dir / "ssh").write_text(f"""#!/bin/sh
echo "ssh $*" >> {log}
while [ "$1" = "-o" ]; do shift 2; done
shift
cd {home} && exec sh -c "$*"
""")
        (bin_dir / "scp").write_text(f"""#!/bin/sh
echo "scp $*" >> {log}
while [ "$1" = "-o" ]; do shift 2; done
[ "$1" = "-r" ] && shift
src=${{1#*:}}
cp -R "{home}/$src" "$2"
""")
        fake_iosim = self.dir / "remote-iosim"
        fake_iosim.write_text(f"""#!/bin/sh
echo "iosim $*" >> {log}
if [ "$1" = "run" ]; then
  mkdir -p "$4"; cat > "$4/plan-received.json"; echo '{{"ok": true, "out": "remote"}}' ; echo '{{"passed": true}}' > "$4/report.json"; exit 0
fi
echo '{{"ok": true, "args": "'"$*"'"}}'
""")
        for f in (bin_dir / "ssh", bin_dir / "scp", fake_iosim):
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        env = dict(self.env, PATH=f"{bin_dir}:{os.environ['PATH']}", IOSIM_REMOTE="mac.example", IOSIM_REMOTE_IOSIM=str(fake_iosim))
        return env, home, log

    def test_remote_run_sends_resolved_plan_and_copies_evidence_back(self):
        env, home, log = self.fake_bin()
        (self.dir / "check.js").write_text("return 2;")
        plan = self.dir / "plan.json"
        plan.write_text(json.dumps({"steps": [{"open": "${BASE}/"}, {"eval": {"file": "check.js"}}]}))
        out = self.dir / "evidence"
        code, stdout, stderr = self.cli("remote", "run", str(plan), "--out", str(out), "--var", "BASE=http://box:3000", env=env)
        self.assertEqual(code, 0, stdout + stderr)
        self.assertTrue(json.loads(stdout)["ok"])
        received = json.loads((out / "plan-received.json").read_text())
        self.assertEqual(received["steps"][0]["url"], "http://box:3000/")
        self.assertEqual(received["steps"][1]["source"], "return 2;")
        self.assertTrue((out / "report.json").exists())
        self.assertEqual(list((home / "iosim-runs").iterdir()), [])  # remote copy removed
        self.assertIn("BatchMode=yes", log.read_text())

    def test_remote_passthrough_quotes_arguments(self):
        env, _, _ = self.fake_bin()
        code, stdout, _ = self.cli("remote", "eval", "document.title + ' x'", env=env)
        self.assertEqual(code, 0)
        self.assertIn("document.title + ' x'", json.loads(stdout)["args"])


if __name__ == "__main__":
    unittest.main()
