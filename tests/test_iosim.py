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

    def test_driven_page_is_found_by_identity_not_url(self):
        pages = [
            {"url": "https://a.test/", "appId": "PID:1", "webSocketDebuggerUrl": "ws://127.0.0.1:9321/devtools/page/1"},
            {"url": "https://a.test/", "appId": "PID:1", "webSocketDebuggerUrl": "ws://127.0.0.1:9321/devtools/page/2"},
            {"url": "https://c.test/", "appId": "PID:1", "webSocketDebuggerUrl": "ws://127.0.0.1:9321/devtools/page/3"},
        ]
        state = {"page": "/devtools/page/1", "appId": "PID:1", "url": "https://c.test/"}  # its URL says nothing
        self.assertIs(iosim.driven_page(pages, state), pages[0])
        # A proxy on another port keeps the identity.
        moved = [dict(p, webSocketDebuggerUrl=p["webSocketDebuggerUrl"].replace("9321", "9341")) for p in pages]
        self.assertIs(iosim.driven_page(moved, state), moved[0])
        # The tab closed, or Safari restarted (another process): gone, never another tab.
        with self.assertRaises(iosim.IosimError) as cm:
            iosim.driven_page(pages[1:], state)
        self.assertIn("is gone", str(cm.exception))
        with self.assertRaises(iosim.IosimError):
            iosim.driven_page([dict(pages[0], appId="PID:2")], state)
        # A restarted proxy numbers the tabs afresh, so a recorded page number means nothing under another proxy.
        proxy = {"pid": 10, "started": 1.0}
        bound = dict(state, proxy=proxy)
        self.assertIs(iosim.driven_page(pages, bound, proxy), pages[0])
        for other in ({"pid": 11, "started": 2.0}, {"pid": 10, "started": 2.0}, {}):
            with self.assertRaises(iosim.IosimError) as cm:
                iosim.driven_page(pages, bound, other)
            self.assertIn("proxy restarted", str(cm.exception))
            self.assertIn("iosim open", cm.exception.fix)
        with self.assertRaises(iosim.IosimError):  # a record from before the proxy was noted cannot be trusted
            iosim.driven_page(pages, state, proxy)
        self.assertEqual(iosim.proxy_id({"pid": 10, "started": 1.0, "port": 9320}), proxy)
        self.assertIsNone(iosim.proxy_id(None))
        # Nothing recorded: one page is unambiguous; several are refused.
        self.assertIs(iosim.driven_page(pages[:1], {}), pages[0])
        with self.assertRaises(iosim.IosimError) as cm:
            iosim.driven_page(pages, {})
        self.assertIn("iosim open", cm.exception.fix)
        with self.assertRaises(iosim.IosimError):
            iosim.driven_page([], {})

    def test_url_matches(self):
        self.assertTrue(iosim.url_matches("http://box:3000/recipes/1", "http://box:3000/recipes"))
        self.assertTrue(iosim.url_matches("http://box:3000/", "http://box:3000"))
        self.assertFalse(iosim.url_matches("http://box:3000/sign-in", "http://box:3000/recipes"))
        self.assertFalse(iosim.url_matches("http://other/", "http://box/"))

    def test_normalize_text_size_and_audit(self):
        self.assertEqual(iosim.normalize_text_size("AX5"), "accessibility-extra-extra-extra-large")
        self.assertEqual(iosim.normalize_text_size("default"), "large")
        self.assertEqual(iosim.normalize_text_size("Extra Large"), "extra-large")
        res = iosim.normalize_audit([{"rule": "x", "severity": "warn"}, {"message": "bare"}, "text"])
        self.assertEqual(res["counts"], {"fail": 2, "warn": 1})
        with self.assertRaises(iosim.IosimError):
            iosim.normalize_audit("nope")
        self.assertEqual(res["findings"][1]["severity"], "fail")
        # {rule, element, detail}, as written for page.evaluate, maps onto message and selector.
        res = iosim.normalize_audit([{"rule": "tap-target", "element": "button#go", "detail": "30 x 30 px"}])
        self.assertEqual(res["findings"][0]["message"], "30 x 30 px")
        self.assertEqual(res["findings"][0]["selector"], "button#go")
        self.assertEqual(iosim.normalize_audit([])["counts"], {"fail": 0, "warn": 0})

    def test_audit_result_shapes(self):
        # An array is the findings; an object needs a findings array.
        self.assertEqual(iosim.normalize_audit({"findings": [{"rule": "a"}]})["counts"]["fail"], 1)
        self.assertEqual(iosim.normalize_audit({"findings": [], "viewport": {}})["counts"], {"fail": 0, "warn": 0})
        for bad in ({"violations": [{"id": "color-contrast"}]}, {}, {"findings": "none"}, None, 0, True, "ok"):
            with self.assertRaises(iosim.IosimError) as cm:
                iosim.normalize_audit(bad)
            self.assertIn("findings", str(cm.exception))
        with self.assertRaises(iosim.IosimError) as cm:
            iosim.normalize_audit({"violations": []})
        self.assertIn("violations", str(cm.exception))  # names the keys it got
        # Severities: aliases map, anything else is refused rather than counted as neither.
        res = iosim.normalize_audit([{"severity": "error"}, {"severity": "Warning"}])
        self.assertEqual(res["counts"], {"fail": 1, "warn": 1})
        with self.assertRaises(iosim.IosimError):
            iosim.normalize_audit([{"severity": "critical"}])


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

    def test_remote_scp_failure_shows_the_remote_error(self):
        env, home, log = self.fake_bin()
        (self.dir / "bin" / "scp").write_text("#!/bin/sh\necho 'scp: no such file' >&2\nexit 1\n")
        fake = Path(env["IOSIM_REMOTE_IOSIM"])
        fake.write_text("#!/bin/sh\ncat > /dev/null\necho '{\"ok\": false, \"error\": \"no iOS simulator is booted\"}'\nexit 1\n")
        plan = self.dir / "plan.json"
        plan.write_text(json.dumps({"steps": [{"open": "http://box/"}]}))
        code, stdout, _ = self.cli("remote", "run", str(plan), "--out", str(self.dir / "ev"), env=env)
        self.assertEqual(code, 1)
        self.assertIn("no iOS simulator is booted", json.loads(stdout)["error"])

    def test_forwarded_run_resolves_the_plan_and_drops_agent_device(self):
        env, home, log = self.fake_bin()
        plan = self.dir / "plan.json"
        plan.write_text(json.dumps({"steps": [{"open": "${BASE}/"}]}))
        with mock.patch.object(iosim, "have_simctl", return_value=False), mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "argv", ["iosim", "--device", "Lab", "--agent-device", "/local/ad", "run", str(plan),
                                                "--out", str(self.dir / "ev"), "--var", "BASE=http://box:3000"]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = iosim.main(sys.argv[1:])
        self.assertEqual(code, 0, buf.getvalue())
        calls = log.read_text()
        self.assertNotIn("/local/ad", calls)
        self.assertIn("IOSIM_DEVICE=Lab", calls)
        self.assertIn("run - --out", calls)
        received = json.loads((self.dir / "ev" / "plan-received.json").read_text())
        self.assertEqual(received["steps"][0]["url"], "http://box:3000/")

    def test_remote_passthrough_quotes_arguments(self):
        env, _, _ = self.fake_bin()
        code, stdout, _ = self.cli("remote", "eval", "document.title + ' x'", env=env)
        self.assertEqual(code, 0)
        self.assertIn("document.title + ' x'", json.loads(stdout)["args"])



# A snapshot as agent-device took it on an iPhone 15 (iOS 27.2) with Safari's compact toolbar: the web area starts
# at y = 59 under the status bar, and Safari's own toolbar is outside the WebView.
SNAPSHOT = [
    {"depth": 0, "type": "Application", "label": "Safari", "rect": {"x": 0, "y": 0, "width": 393, "height": 852}},
    {"depth": 5, "type": "WebView", "rect": {"x": 0, "y": 0, "width": 393, "height": 852}},
    {"depth": 7, "type": "WebView", "label": "iosim test", "rect": {"x": 0, "y": 0, "width": 393, "height": 852}},
    {"depth": 8, "type": "Other", "rect": {"x": 17, "y": 401, "width": 359, "height": 300}},  # the decoy that misled the old heuristic
    {"depth": 9, "type": "Other", "label": "iosim test", "rect": {"x": 0, "y": 59, "width": 393, "height": 793}},
    {"depth": 11, "type": "Other", "label": "Test page", "rect": {"x": 0, "y": 81, "width": 393, "height": 43}},
    {"depth": 12, "type": "Button", "label": "Button one", "rect": {"x": 0, "y": 146, "width": 114, "height": 45}},
    {"depth": 12, "type": "Button", "label": "Dup A", "rect": {"x": 117, "y": 146, "width": 80, "height": 45}},
    {"depth": 12, "type": "TextField", "label": "Name", "rect": {"x": 0, "y": 207, "width": 216, "height": 31}},
    {"depth": 2, "type": "Other", "label": "Back", "rect": {"x": 0, "y": 754, "width": 393, "height": 98}},
    {"depth": 5, "type": "Button", "label": "Button one", "rect": {"x": 34, "y": 770, "width": 114, "height": 45}},  # chrome, same name
    {"depth": 5, "type": "Button", "label": "Tabs", "rect": {"x": 311, "y": 770, "width": 48, "height": 48}},
]


def measured(target_y=87.53, anchors=None, visual_height=695):
    """TAP_RECT_JS's answer for #b1 on that page."""
    rect = {"x": 0, "y": target_y, "width": 113.45, "height": 44}
    return {"rect": rect, "title": "iosim test", "visual": {"width": 393, "height": visual_height},
            "anchors": anchors if anchors is not None else [
                dict(rect, id=0, label="Button one", target=True, pageCount=1),
                {"id": 1, "label": "Test page", "x": 0, "y": 21.44, "width": 393, "height": 42.9, "target": False, "pageCount": 1},
                {"id": 2, "label": "Name", "x": 0, "y": 148.5, "width": 215.3, "height": 30, "target": False, "pageCount": 1},
            ]}


class TapOriginTest(unittest.TestCase):
    def test_anchors_give_the_web_origin_and_ignore_safari_chrome(self):
        found = iosim.web_origin_from_anchors(SNAPSHOT, measured()["anchors"])
        self.assertEqual(found["origin"], (0.0, 59.0))
        self.assertEqual(found["anchors"], 3)
        self.assertEqual(found["unique"], 3)
        self.assertTrue(found["target"])

    def rows(self, scroll=800, row=56, top=59, count=40, above=3, below=3):
        """A page of identical "Delete" buttons scrolled by `scroll`: the page's anchors (the visible rows) and a
        snapshot that, like Safari's, also lists rows just above and below the visible area."""
        visible_h = 695
        anchors, nodes = [], [dict(n) for n in SNAPSHOT[:3]]
        for i in range(count):
            y = 80 + i * row - scroll  # page y in the visual viewport
            if 0 <= y and y + 44 <= visible_h:
                anchors.append({"id": i, "label": "Delete", "x": 8, "y": y, "width": 120, "height": 44, "target": i == 20, "pageCount": count})
            if -above * row <= y and y + 44 <= visible_h + below * row:
                nodes.append({"depth": 12, "type": "Button", "label": "Delete", "rect": {"x": 8, "y": top + y, "width": 120, "height": 44}})
        return anchors, nodes

    def test_a_tie_between_offsets_is_refused(self):
        # The reviewer's page: 40 identical rows, scrolled 800 px. Offsets one row apart fit as many anchors.
        anchors, nodes = self.rows()
        found = iosim.web_origin_from_anchors(nodes, anchors)
        self.assertIn("refused", found)
        self.assertIn("equally well", found["refused"])
        info = {"rect": next(a for a in anchors if a["target"]), "title": "rows", "visual": {"width": 393, "height": 695}, "anchors": anchors}
        with self.assertRaises(iosim.IosimError) as cm:
            iosim.tap_point(info, nodes)
        self.assertIn("could not place", str(cm.exception))

    def test_repeated_names_alone_are_refused_when_another_offset_fits(self):
        # The snapshot lists only the visible rows: the right offset leads by one, but only repeated names back it.
        anchors, nodes = self.rows(above=0, below=0)
        found = iosim.web_origin_from_anchors(nodes, anchors)
        self.assertIn("repeated names", found["refused"])
        # A unique heading on the same page settles it.
        anchors.append({"id": 99, "label": "Rows", "x": 8, "y": 0, "width": 200, "height": 40, "pageCount": 1})
        nodes.append({"depth": 11, "type": "Other", "label": "Rows", "rect": {"x": 8, "y": 59, "width": 200, "height": 40}})
        found = iosim.web_origin_from_anchors(nodes, anchors)
        self.assertEqual(found["origin"], (0.0, 59.0))

    def test_one_anchor_needs_a_unique_name_on_both_sides(self):
        # The reviewer's iframe page: the main page's "OK" (unique there) and an iframe's "OK" of the same size.
        anchors = [{"id": 0, "label": "OK", "x": 0, "y": 120, "width": 200, "height": 48, "target": True, "pageCount": 1}]
        nodes = SNAPSHOT[:3] + [
            {"depth": 12, "type": "Button", "label": "OK", "rect": {"x": 0, "y": 99, "width": 200, "height": 48}},   # in the iframe
            {"depth": 12, "type": "Button", "label": "OK", "rect": {"x": 0, "y": 179, "width": 200, "height": 48}},  # the target
        ]
        self.assertIn("refused", iosim.web_origin_from_anchors(nodes, anchors))
        # With only one "OK" on screen the name is unique on both sides, and one anchor is enough.
        found = iosim.web_origin_from_anchors(SNAPSHOT[:3] + nodes[-1:], anchors)
        self.assertEqual(found["origin"], (0.0, 59.0))
        # A name the page repeats is not unique even when the snapshot shows it once.
        weak = [dict(anchors[0], pageCount=2)]
        self.assertIn("refused", iosim.web_origin_from_anchors(SNAPSHOT[:3] + nodes[-1:], weak))
        # A wrapper sharing its label with the button inside it counts once.
        wrapped = SNAPSHOT[:3] + [{"depth": 11, "type": "Other", "label": "OK", "rect": {"x": 0, "y": 175, "width": 200, "height": 56}}] + nodes[-1:]
        self.assertEqual(iosim.snapshot_label_counts(wrapped)["OK"], 1)

    def test_anchors_and_the_web_area_node_must_agree(self):
        info = measured()
        nodes = [dict(n) for n in SNAPSHOT]
        nodes[4] = dict(nodes[4], rect={"x": 0, "y": 103, "width": 393, "height": 749})  # web area says y = 103
        with self.assertRaises(iosim.IosimError) as cm:
            iosim.tap_point(info, nodes)
        self.assertIn("web area node", str(cm.exception))
        self.assertTrue(iosim.tap_point(measured(), SNAPSHOT)[3]["webAreaAgrees"])

    def test_tap_point_lands_on_the_target(self):
        x, y, origin, how = iosim.tap_point(measured(), SNAPSHOT)
        self.assertEqual(origin, [0.0, 59.0])
        self.assertEqual((x, y), (56.7, 168.5))
        self.assertEqual(how["originVia"], "anchors")

    def test_two_row_toolbar_and_collapsed_toolbar_need_no_special_case(self):
        # Scrolled: the target sits lower and the visible area is taller; the anchor still places it.
        anchors = [{"id": 0, "label": "Button two", "x": 0, "y": 632.5, "width": 113.4, "height": 44, "target": True, "pageCount": 1}]
        nodes = SNAPSHOT[:3] + [{"depth": 12, "type": "Button", "label": "Button two", "rect": {"x": 0, "y": 691, "width": 114, "height": 45}}]
        info = {"rect": dict(anchors[0]), "title": "iosim test", "visual": {"width": 393, "height": 735}, "anchors": anchors}
        x, y, origin, _ = iosim.tap_point(info, nodes)
        self.assertEqual(origin, [0.0, 59.0])
        self.assertEqual(y, 713.5)

    def test_title_fallback_needs_the_title_label(self):
        info = measured(anchors=[])
        self.assertEqual(iosim.tap_point(info, SNAPSHOT)[2], [0, 59])
        # Without a labelled title node, an unlabelled full-width node is not trusted.
        nodes = [n for n in SNAPSHOT if n.get("label") != "iosim test"]
        with self.assertRaises(iosim.IosimError) as cm:
            iosim.tap_point(info, nodes)
        self.assertIn("--at", cm.exception.fix)

    def test_a_point_outside_the_visible_page_is_refused(self):
        info = measured(target_y=700)  # below the 695 px the toolbar leaves
        info["anchors"] = [{"id": 0, "label": "Name", "x": 0, "y": 148.5, "width": 215.3, "height": 30, "pageCount": 1}]
        with self.assertRaises(iosim.IosimError) as cm:
            iosim.tap_point(info, SNAPSHOT)
        self.assertIn("outside the page's visible area", str(cm.exception))

    def test_verify_tap_judges_what_the_page_saw(self):
        ok = iosim.verify_tap({"type": "touchstart", "target": "button#d20", "inside": True}, "button#d20")
        self.assertTrue(ok["verified"])
        with self.assertRaises(iosim.IosimError) as cm:
            iosim.verify_tap({"type": "touchstart", "target": "button#d19", "inside": False, "x": 68, "y": 394}, "button#d20")
        self.assertIn("landed on button#d19", str(cm.exception))
        with self.assertRaises(iosim.IosimError) as cm:
            iosim.verify_tap(None, "button#ok")  # an iframe, Safari's toolbar or another tab took it
        self.assertIn("reached no element", str(cm.exception))
        # With nothing expected, it only reports.
        self.assertEqual(iosim.verify_tap(None), {"pageHit": None})
        self.assertEqual(iosim.verify_tap({"type": "click", "target": "a#x", "inside": None})["pageHit"], "a#x")

    def tap_session(self, record, visible=True, info=None):
        """A Session whose page answers TAP_RECT_JS with `info` and whose tap check sees `record`."""
        sess = iosim.Session.__new__(iosim.Session)
        sess.udid = "U"
        sess.driver = mock.Mock(mode="fake")
        sess.driver.snapshot.return_value = {"nodes": SNAPSHOT}
        sess.evaluate = mock.Mock(return_value=info or dict(measured(), target="button#b1"))
        insp = mock.Mock(events=[])

        def evaluate(source, is_body=False, timeout=30.0, prelude=""):
            if "__iosimTap ||" in source:
                return None
            token = json.loads(prelude.split("__tok = ", 1)[1].split(",", 1)[0])
            if record is not None:
                insp.events.append({"method": "Console.messageAdded", "params": {"message": {
                    "text": iosim.TAP_MARK + json.dumps(dict(record, token=token))}}})
            return {"armed": True, "target": "button#b1", "visible": visible}
        insp.evaluate.side_effect = evaluate
        sess.connect = mock.Mock(return_value=insp)
        return sess

    def test_selector_tap_fails_when_another_element_got_it(self):
        sess = self.tap_session({"type": "touchstart", "target": "button#b2", "inside": False})
        with self.assertRaises(iosim.IosimError) as cm:
            sess.tap(selector="#b1")
        self.assertIn("landed on button#b2", str(cm.exception))
        sess.driver.tap.assert_called_once()
        sess = self.tap_session(None)
        with self.assertRaises(iosim.IosimError):
            sess.tap(selector="#b1")
        sess = self.tap_session({"type": "touchstart", "target": "button#b1", "inside": True})
        self.assertTrue(sess.tap(selector="#b1")["verified"])

    def test_a_tab_not_on_screen_is_never_tapped(self):
        sess = self.tap_session(None, info={"hidden": True, "target": "button#b1"})
        with self.assertRaises(iosim.IosimError) as cm:
            sess.tap(selector="#b1")
        self.assertIn("not the tab on screen", str(cm.exception))
        sess.driver.tap.assert_not_called()

    def test_label_and_at_taps_check_an_expectation(self):
        sess = self.tap_session({"type": "touchstart", "target": "button#b2", "inside": False})
        with self.assertRaises(iosim.IosimError):
            sess.tap(at=(10, 10), expect="#b1")
        with self.assertRaises(iosim.IosimError):
            sess.tap(label="Save", expect="#b1")
        # Without one, the tap reports what the page saw and passes.
        self.assertEqual(sess.tap(at=(10, 10))["pageHit"], "button#b2")
        self.assertEqual(iosim.parse_plan([{"tap": {"at": [1, 2], "expect": "#b1"}}])["steps"][0]["expect"], "#b1")
        with self.assertRaises(iosim.PlanError):
            iosim.parse_plan([{"tap": {"selector": "#a", "expect": "#b1"}}])

    def test_label_contains_picks_one_element(self):
        self.assertEqual(iosim.pick_label_node(SNAPSHOT[:9], "button o")["label"], "Button one")
        with self.assertRaises(iosim.IosimError):
            iosim.pick_label_node(SNAPSHOT, "button one")  # page and chrome both: ambiguous
        with self.assertRaises(iosim.IosimError):
            iosim.pick_label_node(SNAPSHOT, "nothing like it")


class FakeInspector(iosim.Inspector):
    """An Inspector whose Runtime.evaluate answers from a table, without a socket."""

    def __init__(self, parses_as_expression):
        self.parses = parses_as_expression
        self.sources = []
        self.target = "t"

    def call(self, method, params=None, timeout=30.0):
        source = params["expression"]
        self.sources.append(source)
        if "(\n" in source and not self.parses:
            return {"wasThrown": True, "result": {"className": "SyntaxError", "description": "SyntaxError: Unexpected keyword 'return'"}}
        return {"result": {"type": "string", "value": json.dumps({"ok": True, "value": []})}}


class AuditScriptTest(unittest.TestCase):
    def test_expression_first_then_function_body(self):
        insp = FakeInspector(parses_as_expression=True)
        self.assertEqual(insp.evaluate("(async () => [])();\n", is_body="auto"), [])
        self.assertEqual(len(insp.sources), 1)
        insp = FakeInspector(parses_as_expression=False)
        self.assertEqual(insp.evaluate("const a = [];\nreturn a;", is_body="auto"), [])
        self.assertEqual(len(insp.sources), 2)
        self.assertIn("{\nconst a", insp.sources[1])

    def test_a_runtime_page_error_is_not_retried_as_a_body(self):
        class Throws(FakeInspector):
            def call(self, method, params=None, timeout=30.0):
                self.sources.append(params["expression"])
                return {"result": {"type": "string", "value": json.dumps({"ok": False, "error": "SyntaxError: JSON.parse"})}}
        insp = Throws(True)
        with self.assertRaises(iosim.PageError):
            insp.evaluate("JSON.parse('x')", is_body="auto")
        self.assertEqual(len(insp.sources), 1)

    def test_trailing_semicolon_and_prelude(self):
        program = iosim.js_program("// header\n(async () => [])();\n", prelude="const __opts = {};")
        self.assertIn("(async () => [])()\n)", program)
        self.assertTrue(program.startswith("(async () => { const __opts = {};"))

    @unittest.skipUnless(shutil.which("node"), "node runs the wrapped program")
    def test_page_evaluate_style_file_runs_as_written(self):
        # The shape of a project gate written for page.evaluate: comments, then one async IIFE ending in `;`.
        source = "// gate: returns [] when the page passes\n(async () => {\n  const problems = [];\n  if (__opts.fail) problems.push({rule: 'x', element: 'b', detail: 'd'});\n  return problems;\n})();\n"
        for opts, want in (({}, []), ({"fail": True}, [{"rule": "x", "element": "b", "detail": "d"}])):
            program = iosim.js_program(source, prelude=f"const __opts = {json.dumps(opts)};")
            out = subprocess.run(["node", "-e", f"({program}).then((s) => console.log(s))"], capture_output=True, text=True, timeout=30)
            self.assertEqual(json.loads(out.stdout), {"ok": True, "value": want}, out.stderr)
        # The same file read as a function body returns nothing: that is why the expression goes first.
        body = iosim.js_program(source, is_body=True, prelude="const __opts = {};")
        out = subprocess.run(["node", "-e", f"({body}).then((s) => console.log(s))"], capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(out.stdout)["value"], None)


class OpenTest(unittest.TestCase):
    def test_load_failure_only_for_http_to_non_http(self):
        self.assertIn("could not load", iosim.load_failure("http://localhost:8799/", "data:text/html,"))
        self.assertIsNone(iosim.load_failure("http://box/", "https://login.example.com/sign-in"))  # a real redirect
        self.assertIsNone(iosim.load_failure("http://box/app", "http://box/sign-in"))
        self.assertIsNone(iosim.load_failure("about:blank", "about:blank"))

    def session(self, pages, probes):
        sess = iosim.Session.__new__(iosim.Session)
        sess.udid = "U"
        sess.driver = mock.Mock(mode="fake")
        it = iter(probes)
        sess._probe = lambda page, extra="": dict(next(it), url=page["url"])
        return sess, mock.patch.object(iosim, "list_pages", return_value=pages)

    def test_dead_server_fails_at_once(self):
        pages = [{"url": "data:text/html,", "webSocketDebuggerUrl": "ws://p/1"}]
        sess, lp = self.session(pages, [{"href": "data:text/html,", "readyState": "complete", "nav": None, "mark": None, "title": "Can't Open Page"}])
        with lp, mock.patch.object(iosim, "write_state"), mock.patch.object(iosim, "read_state", return_value={"url": "http://localhost:8799/"}):
            started = iosim.time.time()
            with self.assertRaises(iosim.IosimError) as cm:
                sess.wait_loaded("http://localhost:8799/", "tok", "same-tab", set(), set(), timeout=10, driven=pages[0])
        self.assertIn("could not load", str(cm.exception))
        self.assertLess(iosim.time.time() - started, 5)

    def test_dead_server_fails_when_the_driven_tab_is_not_last(self):
        # The reviewer's case: the navigated tab shows Safari's error page, and a loaded http tab comes after it.
        driven = {"url": "data:text/html,", "appId": "PID:1", "webSocketDebuggerUrl": "ws://p/devtools/page/1"}
        other = {"url": "http://127.0.0.1:8731/rows.html", "appId": "PID:1", "webSocketDebuggerUrl": "ws://p/devtools/page/2"}
        probes = {"data:text/html,": {"href": "data:text/html,", "readyState": "complete", "nav": None, "mark": None, "title": "Can't Open Page"},
                  other["url"]: {"href": other["url"], "readyState": "complete", "nav": None, "mark": None, "title": "rows"}}
        sess, lp = self.session([driven, other], [])
        probed = []
        sess._probe = lambda page, extra="": probed.append(page["url"]) or probes[page["url"]]
        with lp, mock.patch.object(iosim, "write_state") as ws:
            with self.assertRaises(iosim.IosimError) as cm:
                sess.wait_loaded("http://127.0.0.1:8799/dead", "tok", "same-tab", set(), set(), timeout=5, driven=driven)
        self.assertIn("could not load", str(cm.exception))
        self.assertEqual(set(probed), {"data:text/html,"})  # the other tab is never asked
        ws.assert_not_called()  # and never saved as the current page

    def test_the_navigated_tab_going_away_fails(self):
        driven = {"url": "http://box/", "appId": "PID:1", "webSocketDebuggerUrl": "ws://p/devtools/page/1"}
        other = {"url": "http://box/", "appId": "PID:1", "webSocketDebuggerUrl": "ws://p/devtools/page/2"}
        sess, lp = self.session([other], [])
        sess._probe = mock.Mock()
        with lp, mock.patch.object(iosim, "write_state"):
            with self.assertRaises(iosim.IosimError) as cm:
                sess.wait_loaded("http://box/", "tok", "same-tab", set(), set(), timeout=10, driven=driven)
        self.assertIn("went away", str(cm.exception))
        sess._probe.assert_not_called()

    def test_open_navigates_the_driven_tab_and_records_its_identity(self):
        driven = {"url": "http://box/a", "appId": "PID:1", "webSocketDebuggerUrl": "ws://p/devtools/page/1"}
        last = {"url": "http://box/b", "appId": "PID:1", "webSocketDebuggerUrl": "ws://p/devtools/page/2"}
        sess, lp = self.session([driven, last], [])
        sess.page_state = lambda: {"page": "/devtools/page/1", "appId": "PID:1", "url": "http://box/a",
                                   "proxy": {"pid": 7, "started": 1.0}}
        sess.proxy_id = lambda: {"pid": 7, "started": 1.0}
        navigated = []
        fake = mock.Mock()
        fake.evaluate.side_effect = lambda src, **kw: navigated.append(src)
        with lp, mock.patch.object(iosim, "Inspector", return_value=fake) as ctor, mock.patch.object(iosim, "write_state") as ws:
            res = sess.open("http://box/c", wait=False)
        self.assertEqual(res["how"], "same-tab")
        ctor.assert_called_once_with("ws://p/devtools/page/1", timeout=10)
        self.assertEqual(ws.call_args[0][1]["page"], "/devtools/page/1")
        sess.driver.open_url.assert_not_called()

    def test_new_tab_ignores_a_page_that_was_already_open(self):
        old = {"url": "http://box/", "webSocketDebuggerUrl": "ws://p/1"}
        probe = {"href": "http://box/", "readyState": "complete", "nav": None, "mark": "tok", "title": "old"}
        sess, lp = self.session([old], [probe] * 100)
        with lp, mock.patch.object(iosim, "write_state"):
            with self.assertRaises(iosim.IosimError) as cm:
                sess.wait_loaded("http://box/", "tok", "new-tab", {"ws://p/1"}, {"ws://p/1"}, timeout=1)
        self.assertIn("no newly loaded page", str(cm.exception))

    def test_new_tab_accepts_a_reloaded_document(self):
        old = {"url": "http://box/", "webSocketDebuggerUrl": "ws://p/1"}
        probe = {"href": "http://box/", "readyState": "complete", "nav": None, "mark": None, "title": "new", "visible": True}
        sess, lp = self.session([old], [probe])
        with lp, mock.patch.object(iosim, "write_state"):
            res = sess.wait_loaded("http://box/", "tok", "new-tab", {"ws://p/1"}, {"ws://p/1"}, timeout=5)
        self.assertEqual(res["title"], "new")

    def test_new_tab_passes_over_a_background_tab_that_reloaded_on_another_url(self):
        stale = {"url": "http://box/recipes/1", "webSocketDebuggerUrl": "ws://p/1"}
        new = {"url": "http://box/", "webSocketDebuggerUrl": "ws://p/2"}
        stale_probe = {"href": "http://box/recipes/1", "readyState": "complete", "nav": None, "mark": None, "title": "stale", "visible": False}
        new_probe = {"href": "http://box/", "readyState": "complete", "nav": None, "mark": None, "title": "new", "visible": True}
        # Only the stale tab answers: the open must not settle on it.
        sess, lp = self.session([stale], [stale_probe] * 100)
        with lp, mock.patch.object(iosim, "write_state"):
            with self.assertRaises(iosim.IosimError):
                sess.wait_loaded("http://box/", "tok", "new-tab", {"ws://p/1"}, {"ws://p/1"}, timeout=1)
        # A background tab the proxy had not listed before the open is passed over too.
        sess, lp = self.session([stale], [stale_probe] * 100)
        with lp, mock.patch.object(iosim, "write_state"):
            with self.assertRaises(iosim.IosimError):
                sess.wait_loaded("http://box/", "tok", "new-tab", set(), set(), timeout=1)
        # Both answer: the new tab wins whatever the order.
        sess, lp = self.session([stale, new], [new_probe])
        with lp, mock.patch.object(iosim, "write_state"):
            res = sess.wait_loaded("http://box/", "tok", "new-tab", {"ws://p/1"}, {"ws://p/1"}, timeout=5)
        self.assertEqual(res["title"], "new")
        # A visible page on another URL is a real redirect and still counts.
        moved = dict(new_probe, href="http://box/sign-in?next=/", title="sign in")
        sess, lp = self.session([dict(new, url="http://box/sign-in?next=/")], [moved])
        with lp, mock.patch.object(iosim, "write_state"):
            res = sess.wait_loaded("http://box/", "tok", "new-tab", set(), set(), timeout=5)
        self.assertEqual(res["title"], "sign in")

    def test_about_blank_needs_a_current_tab(self):
        sess, lp = self.session([], [])
        sess.page_state = lambda: {}
        with lp, self.assertRaises(iosim.IosimError) as cm:
            sess.open("about:blank")
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(iosim.parse_plan([{"open": "about:blank"}])["steps"][0]["url"], "about:blank")


class DeviceTest(unittest.TestCase):
    RUNTIMES = [
        {"identifier": "com.apple.CoreSimulator.SimRuntime.iOS-27-2", "version": "27.2", "name": "iOS 27.2", "platform": "iOS",
         "isAvailable": True, "supportedDeviceTypes": [{"identifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-15", "name": "iPhone 15"},
                                                       {"identifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-17", "name": "iPhone 17"}]},
        {"identifier": "com.apple.CoreSimulator.SimRuntime.iOS-27-2-iPhone19-4", "version": "27.2", "name": "iOS 27.2", "platform": "iOS",
         "isAvailable": True, "supportedDeviceTypes": [{"identifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-Duo", "name": "iPhone Duo"}]},
        {"identifier": "com.apple.CoreSimulator.SimRuntime.iOS-26-0", "version": "26.0", "name": "iOS 26.0", "platform": "iOS",
         "isAvailable": True, "supportedDeviceTypes": [{"identifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-15", "name": "iPhone 15"}]},
    ]

    def test_runtime_that_supports_the_type_wins(self):
        pick = iosim.pick_runtime(self.RUNTIMES, "com.apple.CoreSimulator.SimDeviceType.iPhone-15")
        self.assertEqual(pick["identifier"], "com.apple.CoreSimulator.SimRuntime.iOS-27-2")
        pick = iosim.pick_runtime(self.RUNTIMES, "com.apple.CoreSimulator.SimDeviceType.iPhone-Duo")
        self.assertEqual(pick["identifier"], "com.apple.CoreSimulator.SimRuntime.iOS-27-2-iPhone19-4")
        # With no type, the general runtime (most types) beats the single-device one of the same version.
        self.assertEqual(iosim.pick_runtime(self.RUNTIMES)["identifier"], "com.apple.CoreSimulator.SimRuntime.iOS-27-2")
        with self.assertRaises(iosim.IosimError):
            iosim.pick_runtime(self.RUNTIMES, "com.apple.CoreSimulator.SimDeviceType.iPad-Pro")

    def test_bound_device_must_match(self):
        self.assertEqual(iosim.check_bound_device({"device_udid": "A"}, "A"), "A")
        with self.assertRaises(iosim.IosimError) as cm:
            iosim.check_bound_device({"device_udid": "B"}, "A")
        self.assertIn("--device B", cm.exception.fix)
        with self.assertRaises(iosim.IosimError):
            iosim.check_bound_device({"message": "Opened"}, "A")  # cannot be checked: refuse

    def fake_agent_device(self, d, answer, code=0):
        script = Path(d) / "agent-device"
        script.write_text(f"#!/bin/sh\necho \"$*\" >> {d}/calls\ncat <<'JSON'\n{json.dumps(answer)}\nJSON\nexit {code}\n")
        script.chmod(0o755)
        return str(script)

    def test_mismatched_command_never_taps(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"IOSIM_HOME": d}):
            ad = self.fake_agent_device(d, {"success": True, "data": {"session": "s", "device_udid": "OTHER"}})
            drv = iosim.Driver("MINE", ad)
            with self.assertRaises(iosim.IosimError):
                drv.tap(10, 10)
            self.assertNotIn("press", (Path(d) / "calls").read_text())

    def test_a_command_that_names_another_device_is_refused_before_any_action(self):
        self.assertIsNone(iosim.precheck_bound_device(["/x/agent-device"], "MINE"))  # cannot tell: open answers later
        self.assertEqual(iosim.precheck_bound_device(["ad", "--udid", "MINE"], "MINE"), "MINE")
        with self.assertRaises(iosim.IosimError):
            iosim.precheck_bound_device(["ad", "--udid=OTHER"], "MINE")
        sessions = {"sessions": [{"name": "s1", "device_udid": "OTHER"}, {"name": "s2", "device_udid": "MINE"}]}
        with self.assertRaises(iosim.IosimError):
            iosim.precheck_bound_device(["ad", "--session", "s1"], "MINE", sessions)
        self.assertEqual(iosim.precheck_bound_device(["ad", "--session", "s2"], "MINE", sessions), "MINE")
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"IOSIM_HOME": d}):
            ad = self.fake_agent_device(d, {"success": True, "data": {"session": "s", "device_udid": "OTHER"}})
            drv = iosim.Driver("MINE", f"{ad} --udid OTHER")
            with self.assertRaises(iosim.IosimError):
                drv.open_url("http://box/")
            self.assertFalse((Path(d) / "calls").exists())  # Safari was never opened on the other simulator

    def test_device_in_use_names_the_session_and_gives_a_fix(self):
        answer = {"success": False, "error": {"code": "DEVICE_IN_USE", "message": "Device is already in use by session \"t3-old\".",
                                              "details": {"session": "t3-old", "deviceId": "U"}}}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"IOSIM_HOME": d}):
            drv = iosim.Driver("U", self.fake_agent_device(d, answer, code=1))
            with self.assertRaises(iosim.IosimError) as cm:
                drv.ensure_session()
        self.assertIn("t3-old", str(cm.exception))
        self.assertIn("close", cm.exception.fix)
        self.assertIn("do not close it", cm.exception.fix)

    def test_path_session_is_per_device_and_cleanup_closes_only_it(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"IOSIM_HOME": d}):
            ad = self.fake_agent_device(d, {"success": True, "data": {}})
            with mock.patch.object(iosim, "which", return_value=ad):
                drv = iosim.Driver("UDID-1", "")
            self.assertEqual(drv.ad[-2:], ["--session", "iosim-UDID-1"])
            self.assertEqual(drv.close(), {"closed": True})
            self.assertIn("--session iosim-UDID-1 close", (Path(d) / "calls").read_text())

    def test_claims_and_session_check(self):
        data = {"devices": [{"id": "U", "claimedBy": {"session": "t3-x"}}, {"id": "V"}]}
        self.assertEqual(iosim.claims_from_devices(data), {"U": "t3-x"})
        drv = mock.Mock(udid="U", mode="agent-device (PATH)", command_text=lambda: "agent-device ...")
        ok, detail, fix = iosim.session_check(drv, {"U": "t3-x"})
        self.assertFalse(ok)
        self.assertIn("t3-x", detail)
        self.assertTrue(iosim.session_check(drv, {"U": "iosim-U"})[0])
        self.assertTrue(iosim.session_check(drv, {})[0])


class RunPlanTest(unittest.TestCase):
    """run_plan's verdict and its restore, with the device stubbed out."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.device = {"textSize": "large", "appearance": "light"}
        drv = mock.Mock(mode="fake")
        drv.describe.return_value = {"mode": "fake"}
        drv.set_text_size.side_effect = lambda size: self.device.__setitem__("textSize", size) or size
        drv.set_appearance.side_effect = lambda mode: self.device.__setitem__("appearance", mode) or mode
        self.session = mock.Mock(udid="U", device={"name": "iPhone", "udid": "U", "runtime": "iOS-27-2"}, driver=drv)
        self.session.console.return_value = []
        for name, key in (("text_size_get", "textSize"), ("appearance_get", "appearance")):
            patcher = mock.patch.object(iosim, name, side_effect=lambda udid, key=key: self.device[key])
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_with(self, step_effect, steps=2):
        plan = iosim.parse_plan({"textSize": "ax3", "appearance": "dark", "steps": [{"wait": 1}] * steps})
        with mock.patch.object(iosim, "run_step", side_effect=step_effect):
            return iosim.run_plan(plan, self.dir, session=self.session)

    def report(self):
        return json.loads((self.dir / "report.json").read_text())

    def test_all_steps_pass(self):
        report = self.run_with(lambda *a: {"ok": 1})
        self.assertTrue(report["passed"])
        self.assertEqual(self.device, {"textSize": "large", "appearance": "light"})

    def test_ctrl_c_fails_the_run_and_restores(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_with(KeyboardInterrupt())
        report = self.report()
        self.assertFalse(report["passed"])
        self.assertEqual(report["failures"][0]["kind"], "interrupted")
        self.assertEqual(report["restored"], {"textSize": "large", "appearance": "light"})
        self.assertEqual(self.device, {"textSize": "large", "appearance": "light"})

    def test_sigterm_takes_the_same_path(self):
        def term(*a):
            os.kill(os.getpid(), iosim.signal.SIGTERM)
            iosim.time.sleep(1)
        before = iosim.signal.getsignal(iosim.signal.SIGTERM)
        with self.assertRaises(iosim.Terminated):
            self.run_with(term)
        self.assertFalse(self.report()["passed"])
        self.assertIn("SIGTERM", self.report()["interrupted"])
        self.assertEqual(self.device["textSize"], "large")
        self.assertEqual(iosim.signal.getsignal(iosim.signal.SIGTERM), before)

    def test_a_crash_in_a_step_fails_the_run(self):
        report = self.run_with(FileNotFoundError("no such screenshot"))
        self.assertFalse(report["passed"])
        self.assertEqual(report["failures"][0]["kind"], "crash")
        self.assertEqual(report["stoppedAt"], 1)
        self.assertEqual(self.device["textSize"], "large")

    def test_a_non_json_frame_crash_outside_steps_fails_the_run(self):
        self.session.console.side_effect = ValueError("Expecting value")
        with self.assertRaises(ValueError):
            self.run_with(lambda *a: None)
        report = self.report()
        self.assertFalse(report["passed"])
        self.assertEqual(report["failures"][-1]["kind"], "crash")
        self.assertEqual(self.device["appearance"], "light")

    def passing_report(self):
        (self.dir / "report.json").write_text(json.dumps({"passed": True, "failures": []}))

    def test_a_run_that_cannot_start_never_leaves_an_old_pass(self):
        self.passing_report()
        plan = iosim.parse_plan([{"wait": 1}])
        with mock.patch.object(iosim, "Session", side_effect=iosim.IosimError("no iOS simulator named or with UDID 'nope'")):
            with self.assertRaises(iosim.IosimError):
                iosim.run_plan(plan, self.dir)
        report = self.report()
        self.assertFalse(report["passed"])
        self.assertEqual(report["failures"][0]["kind"], "setup")
        self.assertIn("nope", report["failures"][0]["message"])

    def test_the_placeholder_is_written_before_anything_else(self):
        self.passing_report()
        seen = {}

        def session_ctor(*a):
            seen["report"] = self.report()
            raise KeyboardInterrupt()
        with mock.patch.object(iosim, "Session", side_effect=session_ctor), self.assertRaises(KeyboardInterrupt):
            iosim.run_plan(iosim.parse_plan([{"wait": 1}]), self.dir)
        self.assertFalse(seen["report"]["passed"])
        self.assertTrue(seen["report"]["placeholder"])
        self.assertFalse(self.report()["passed"])  # a death at any point leaves at least the placeholder

    def test_cli_run_with_a_bad_device_replaces_an_old_pass(self):
        self.passing_report()
        plan = self.dir / "plan.json"
        plan.write_text(json.dumps([{"wait": 1}]))
        with mock.patch.object(iosim, "have_simctl", return_value=True), mock.patch.object(iosim, "list_devices", return_value=[]):
            with contextlib.redirect_stdout(io.StringIO()):
                code = iosim.main(["--device", "nope", "run", str(plan), "--out", str(self.dir)])
        self.assertEqual(code, 1)
        self.assertFalse(self.report()["passed"])
        # A plan that does not parse also replaces it.
        self.passing_report()
        plan.write_text("{not json")
        with mock.patch.object(iosim, "have_simctl", return_value=True), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(iosim.main(["run", str(plan), "--out", str(self.dir)]), 2)
        self.assertFalse(self.report()["passed"])

    def test_ctrl_c_during_the_contact_sheet_keeps_a_failing_report(self):
        drv = self.session.driver
        drv.record_start.return_value = str(self.dir / "video.mp4")
        drv.record_stop.return_value = {"path": str(self.dir / "video.mp4"), "exists": True}
        plan = iosim.parse_plan({"video": True, "steps": [{"wait": 1}]})
        with mock.patch.object(iosim, "run_step", return_value=None), \
                mock.patch.object(iosim, "contact_sheet", side_effect=KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
            iosim.run_plan(plan, self.dir, session=self.session)
        report = self.report()
        self.assertFalse(report["passed"])
        self.assertIn("contact sheet", report["contactSheet"]["error"])

    def test_a_shot_of_a_tab_that_is_not_on_screen_fails(self):
        def screenshot(path):
            write_png(path, 4, 4)
        self.session.driver.screenshot.side_effect = screenshot
        self.session.evaluate.return_value = False  # document.visibilityState is hidden
        plan = iosim.parse_plan([{"shot": "home"}])
        with mock.patch.object(iosim, "check_shot", side_effect=lambda path, expect=None: {"path": str(path), "problems": [], "ok": True}):
            report = iosim.run_plan(plan, self.dir, session=self.session)
        self.assertFalse(report["passed"])
        self.assertIn("not the tab on screen", report["failures"][0]["message"])

    def test_optional_failure_still_passes_and_favicon_is_noise(self):
        self.session.console.return_value = [{"text": "Failed to load resource: 404", "url": "http://box/favicon.ico"}]
        plan = iosim.parse_plan({"failOnConsoleErrors": True, "steps": [{"wait": 1, "optional": True}, {"wait": 1}]})
        calls = iter([iosim.IosimError("x"), None])

        def effect(*a):
            e = next(calls)
            if e:
                raise e
        with mock.patch.object(iosim, "run_step", side_effect=effect):
            report = iosim.run_plan(plan, self.dir, session=self.session)
        self.assertTrue(report["passed"])


class PlumbingTest(unittest.TestCase):
    def test_websocket_keeps_a_partial_frame_across_a_timeout(self):
        ws = iosim.WebSocket.__new__(iosim.WebSocket)
        ws.partial = b""
        payload = b'{"id": 1, "result": {}}' * 20
        frame = bytes([0x81, 126]) + iosim.struct.pack(">H", len(payload)) + payload
        ws.buf = frame[:7]
        self.assertIsNone(ws._frame())
        self.assertEqual(ws.buf, frame[:7])  # nothing consumed
        ws.buf = frame + frame[:3]
        b0, data = ws._frame()
        self.assertEqual(data, payload)
        self.assertEqual(ws.buf, frame[:3])

    def test_pid_is_checks_the_process_name(self):
        self.assertTrue(iosim.pid_is(os.getpid(), "python", "Python"))
        self.assertFalse(iosim.pid_is(os.getpid(), "ios_webkit_debug_proxy"))
        self.assertFalse(iosim.pid_is(999999999, "anything"))

    def test_file_lock_excludes_a_second_holder(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"IOSIM_HOME": d}):
            with iosim.FileLock("x.lock"):
                with self.assertRaises(iosim.IosimError):
                    with iosim.FileLock("x.lock", timeout=0.3):
                        pass
            with iosim.FileLock("x.lock", timeout=0.3):
                pass

    def test_wait_js_fails_at_once_only_on_a_syntax_error(self):
        sess = iosim.Session.__new__(iosim.Session)
        sess.evaluate = mock.Mock(side_effect=iosim.PageSyntaxError("page threw: SyntaxError: Unexpected token"))
        started = iosim.time.time()
        with self.assertRaises(iosim.IosimError) as cm:
            sess.wait(js="a b", timeout=10)
        self.assertIn("does not parse", str(cm.exception))
        self.assertLess(iosim.time.time() - started, 2)

    def test_wait_js_keeps_waiting_through_a_runtime_throw(self):
        sess = iosim.Session.__new__(iosim.Session)
        # window.app.ready before app exists throws, then the page sets it up.
        sess.evaluate = mock.Mock(side_effect=[iosim.PageError("page error: TypeError: undefined is not an object"), True])
        with mock.patch.object(iosim.time, "sleep"):
            self.assertIn("waitedMs", sess.wait(js="window.app.ready", timeout=10))
        sess.evaluate = mock.Mock(side_effect=iosim.PageError("page error: TypeError: undefined is not an object"))
        with self.assertRaises(iosim.IosimError) as cm:
            sess.wait(js="window.app.ready", timeout=0.3)
        self.assertIn("kept throwing", str(cm.exception))
        self.assertIn("?.", cm.exception.fix)

    def test_with_page_does_not_rerun_a_sent_command(self):
        sess = iosim.Session.__new__(iosim.Session)
        insp = mock.Mock(sent=1)
        sess.connect = mock.Mock(return_value=insp)
        fn = mock.Mock(side_effect=ConnectionError("closed"))
        with self.assertRaises(iosim.IosimError) as cm:
            sess.with_page(fn)
        self.assertEqual(fn.call_count, 1)
        self.assertIn("may or may not have run", str(cm.exception))
        fn.reset_mock()
        with mock.patch.object(iosim.time, "sleep"), self.assertRaises(iosim.IosimError):
            sess.with_page(fn, idempotent=True)
        self.assertEqual(fn.call_count, 3)

    def test_split_global_flags(self):
        self.assertEqual(iosim.split_global_flags(["--device", "X", "--agent-device", "ad", "run", "p.json", "--out", "o"]),
                         (["run", "p.json", "--out", "o"], "X"))
        self.assertEqual(iosim.split_global_flags(["eval", "--device"]), (["eval", "--device"], None))

    def test_plan_tap_first_and_contains(self):
        steps = iosim.parse_plan([{"tap": {"selector": ".a", "first": True}}, {"tap": {"label": "Sign", "contains": True}},
                                  {"type": {"text": "x", "selector": "input", "first": True}}])["steps"]
        self.assertTrue(steps[0]["first"])
        self.assertTrue(steps[1]["contains"])
        self.assertTrue(steps[2]["first"])
        with self.assertRaises(iosim.PlanError):
            iosim.parse_plan([{"tap": {"label": "x", "first": True}}])


if __name__ == "__main__":
    unittest.main()


class TapCheckIgnoresScriptedEvents(unittest.TestCase):
    def test_the_tap_listener_drops_untrusted_events_first(self):
        source = (SCRIPTS / "iosim.py").read_text()
        handler = source[source.index("function handler(ev) {"):]
        first = handler.split("\n")[1].strip()
        self.assertTrue(first.startswith("if (!ev.isTrusted) return;"), first)
