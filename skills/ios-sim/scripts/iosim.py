"""iosim: test web pages in Mobile Safari on an iOS Simulator. Python 3.9+, stdlib only.

Run `iosim --help`, or read the ios-sim skill's references/cli.md.
"""

import argparse
import base64
import json
import os
import platform
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent
AUDIT_JS = HERE / "audit.js"
SAFARI = "com.apple.mobilesafari"
DEFAULT_DEVICE_TYPE = "iPhone 15"
DEFAULT_PROXY_PORT = 9320
PROXY_INSTALL = "brew install ios-webkit-debug-proxy"
FFMPEG_INSTALL = "brew install ffmpeg"
AGENT_DEVICE_INSTALL = "npm install -g agent-device"

# Dynamic Type categories, smallest to largest, as `simctl ui content_size` names them.
TEXT_SIZES = [
    "extra-small", "small", "medium", "large", "extra-large", "extra-extra-large", "extra-extra-extra-large",
    "accessibility-medium", "accessibility-large", "accessibility-extra-large",
    "accessibility-extra-extra-large", "accessibility-extra-extra-extra-large",
]
TEXT_SIZE_ALIASES = {
    "xs": "extra-small", "s": "small", "m": "medium", "l": "large", "default": "large", "xl": "extra-large",
    "xxl": "extra-extra-large", "xxxl": "extra-extra-extra-large",
    "ax1": "accessibility-medium", "ax2": "accessibility-large", "ax3": "accessibility-extra-large",
    "ax4": "accessibility-extra-extra-large", "ax5": "accessibility-extra-extra-extra-large",
}


class IosimError(Exception):
    """A failure the CLI reports as {"ok": false, "error": ..., "fix": ...} and exit 1."""

    def __init__(self, message, fix=None, code=1):
        super().__init__(message)
        self.fix = fix
        self.code = code


# ---------------------------------------------------------------- pure helpers


def normalize_text_size(value):
    v = str(value).strip().lower().replace("_", "-").replace(" ", "-")
    v = TEXT_SIZE_ALIASES.get(v, v)
    if v not in TEXT_SIZES:
        raise IosimError(f"unknown text size {value!r}", "use one of: " + ", ".join(TEXT_SIZES) + " (or ax1..ax5)", code=2)
    return v


def wrap_for_target(target_id, inner_id, method, params=None, outer_id=None):
    """WebKit's page socket speaks to targets: every command rides inside Target.sendMessageToTarget."""
    inner = {"id": inner_id, "method": method, "params": params or {}}
    return {
        "id": outer_id if outer_id is not None else inner_id,
        "method": "Target.sendMessageToTarget",
        "params": {"targetId": target_id, "message": json.dumps(inner)},
    }


def unwrap_from_target(message):
    """Return the inner message of a Target.dispatchMessageFromTarget event, or None."""
    if message.get("method") != "Target.dispatchMessageFromTarget":
        return None
    raw = message.get("params", {}).get("message")
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def js_program(source, is_body=False):
    """Wrap an expression (or, with is_body, a function body that uses `return`) so the page
    always answers with a JSON string: {"ok": true, "value": ...} or {"ok": false, "error": ...}."""
    inner = "{\n" + source + "\n}" if is_body else "(\n" + source + "\n)"
    return (
        "(async () => { try { const __v = await (async () => " + inner + ")();"
        " return JSON.stringify({ok: true, value: __v === undefined ? null : __v}); }"
        " catch (e) { return JSON.stringify({ok: false, error: String(e) + (e && /:\\d/.test(e.stack || '') ? ' at ' + e.stack : '')}); } })()"
    )


def pick_page(pages, url_hint=None):
    """Choose the Safari page to drive from the proxy's /json list: the last one whose URL matches the hint, else the last."""
    candidates = [p for p in pages if p.get("webSocketDebuggerUrl")]
    if not candidates:
        return None
    if url_hint:
        hint = url_hint.rstrip("/")
        matches = [p for p in candidates if (p.get("url") or "").rstrip("/") == hint]
        if not matches:
            host = urllib.parse.urlsplit(url_hint).netloc
            matches = [p for p in candidates if host and urllib.parse.urlsplit(p.get("url") or "").netloc == host]
        if matches:
            return matches[-1]
    return candidates[-1]


def url_matches(current, wanted):
    """Loose match after redirects: same host, and the wanted path is a prefix of the current one."""
    if not current or not wanted:
        return False
    c, w = urllib.parse.urlsplit(current), urllib.parse.urlsplit(wanted)
    return c.netloc == w.netloc and (c.path or "/").startswith((w.path or "/").rstrip("/") or "/")


def contents_to_points(rect, origin):
    """Centre of a CSS-pixel viewport rect, in screen points, given the web content origin on screen."""
    return (round(origin[0] + rect["x"] + rect["width"] / 2, 1), round(origin[1] + rect["y"] + rect["height"] / 2, 1))


def web_origin_from_snapshot(nodes, title=None):
    """Find where Safari's web content starts on screen from an agent-device snapshot: the first
    node under a WebView whose rect is narrower in height than the WebView itself (the web area
    sits below the status bar). Returns (x, y) or None."""
    webviews = [n for n in nodes if n.get("type") == "WebView" and n.get("rect")]
    if not webviews:
        return None
    full = webviews[-1]["rect"]
    depth = webviews[-1].get("depth", 0)
    start = nodes.index(webviews[-1])
    for n in nodes[start + 1:]:
        if n.get("depth", 0) <= depth:
            break
        r = n.get("rect")
        if not r:
            continue
        if title and n.get("label") and n.get("label") != title:
            continue
        if r["y"] > full["y"] and r["width"] >= full["width"] * 0.9:
            return (r["x"], r["y"])
    return None


def dict_get_path(obj, path):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def safe_name(name):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).strip("-.")
    return s or "item"


# ---------------------------------------------------------------- minimal WebSocket client (RFC 6455, text frames)


def ws_frame(payload, opcode=0x1, mask_key=None):
    """Encode one masked client frame."""
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    mask_key = mask_key if mask_key is not None else os.urandom(4)
    head = bytearray([0x80 | opcode])
    n = len(data)
    if n < 126:
        head.append(0x80 | n)
    elif n < 65536:
        head.append(0x80 | 126)
        head += struct.pack(">H", n)
    else:
        head.append(0x80 | 127)
        head += struct.pack(">Q", n)
    head += mask_key
    return bytes(head) + bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))


class WebSocket:
    def __init__(self, url, timeout=10.0):
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port or 80
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path + ("?" + parts.query if parts.query else "")
        request = (
            f"GET {path or '/'} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise IosimError("WebSocket handshake closed early")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise IosimError("WebSocket handshake refused: " + head.split(b"\r\n", 1)[0].decode(errors="replace"))

    def send(self, text):
        self.sock.sendall(ws_frame(text))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WebSocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self, timeout):
        """Return the next text message, or None on timeout."""
        self.sock.settimeout(max(timeout, 0.01))
        message = b""
        try:
            while True:
                b0, b1 = self._read(2)
                opcode, n = b0 & 0x0F, b1 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                mask = self._read(4) if b1 & 0x80 else None
                data = self._read(n)
                if mask:
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                if opcode == 0x8:
                    raise ConnectionError("WebSocket closed by peer")
                if opcode == 0x9:
                    self.sock.sendall(ws_frame(data, opcode=0xA))
                    continue
                if opcode in (0x1, 0x2, 0x0):
                    message += data
                    if b0 & 0x80:
                        return message.decode("utf-8", errors="replace")
        except socket.timeout:
            return None

    def close(self):
        try:
            self.sock.sendall(ws_frame(b"", opcode=0x8))
        except OSError:
            pass
        self.sock.close()


# ---------------------------------------------------------------- Web Inspector session


class Inspector:
    """One connection to a Safari page through ios_webkit_debug_proxy."""

    def __init__(self, ws_url, timeout=10.0):
        self.ws = WebSocket(ws_url, timeout=timeout)
        self.target = None
        self.next_id = 1
        self.events = []
        deadline = time.time() + timeout
        while self.target is None and time.time() < deadline:
            msg = self._recv(deadline - time.time())
            if msg is None:
                break
        if self.target is None:
            self.ws.close()
            raise IosimError(
                "the page answered but announced no inspector target",
                "reload the page (iosim open <url>) and retry; if it persists run `iosim proxy restart`",
            )

    def _recv(self, timeout):
        raw = self.ws.recv(timeout)
        if raw is None:
            return None
        msg = json.loads(raw)
        method = msg.get("method")
        if method == "Target.targetCreated":
            info = msg["params"]["targetInfo"]
            if info.get("type", "page") == "page" and not info.get("isProvisional"):
                self.target = info["targetId"]
        elif method == "Target.didCommitProvisionalTarget":
            self.target = msg["params"]["newTargetId"]
        elif method == "Target.targetDestroyed" and msg["params"].get("targetId") == self.target:
            self.target = None
        inner = unwrap_from_target(msg)
        if inner is not None and "method" in inner:
            self.events.append(inner)
        return msg if inner is None else {"inner": inner}

    def call(self, method, params=None, timeout=30.0):
        if self.target is None:
            raise IosimError("the page's inspector target went away (navigation?)", "retry the command")
        cid = self.next_id
        self.next_id += 1
        self.ws.send(json.dumps(wrap_for_target(self.target, cid, method, params)))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._recv(deadline - time.time())
            if msg is None:
                continue
            inner = msg.get("inner") if isinstance(msg, dict) else None
            if inner and inner.get("id") == cid:
                if "error" in inner:
                    raise IosimError(f"{method}: {inner['error'].get('message', inner['error'])}")
                return inner.get("result", {})
            if msg.get("id") == cid and "error" in msg:
                raise IosimError(f"{method}: {msg['error'].get('message', msg['error'])}")
        raise IosimError(f"{method} timed out after {timeout:.0f}s", "the page may be busy or navigating; retry")

    def drain(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self._recv(deadline - time.time())

    def evaluate(self, source, is_body=False, timeout=30.0):
        """Evaluate in the page, await a returned promise, and return the JSON value."""
        # WebKit ignores awaitPromise on Runtime.evaluate: get the promise object, then await it.
        result = self.call("Runtime.evaluate", {"expression": js_program(source, is_body), "returnByValue": False}, timeout=timeout)
        remote = result.get("result", {})
        if result.get("wasThrown"):
            raise IosimError("page threw: " + str(remote.get("description") or remote.get("value")))
        value = remote.get("value")
        if remote.get("type") == "object" and "objectId" in remote:
            awaited = self.call("Runtime.awaitPromise", {"promiseObjectId": remote["objectId"], "returnByValue": True}, timeout=timeout)
            if awaited.get("wasThrown"):
                raise IosimError("page threw: " + str(awaited.get("result", {}).get("description")))
            value = awaited.get("result", {}).get("value")
        try:
            answer = json.loads(value)
        except (TypeError, ValueError):
            raise IosimError(f"unexpected evaluate result: {remote!r}")
        if not answer.get("ok"):
            raise IosimError("page error: " + str(answer.get("error")))
        return answer.get("value")

    def close(self):
        self.ws.close()


# ---------------------------------------------------------------- environment


EXTRA_BIN_DIRS = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"), str(Path.home() / ".npm-global/bin")]


def which(name):
    """shutil.which, plus Homebrew and user bin directories a non-interactive SSH login leaves off PATH."""
    found = shutil.which(name)
    if found:
        return found
    for d in EXTRA_BIN_DIRS:
        p = Path(d) / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def state_dir():
    d = Path(os.environ.get("IOSIM_HOME") or Path.home() / ".cache" / "iosim")
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_state(name):
    p = state_dir() / name
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def write_state(name, data):
    p = state_dir() / name
    if data is None:
        p.unlink(missing_ok=True)
    else:
        p.write_text(json.dumps(data, indent=2))


def run_cmd(args, timeout=120, check=True, input_text=None):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=input_text)
    except FileNotFoundError:
        raise IosimError(f"{args[0]} not found", "run `iosim doctor`")
    except subprocess.TimeoutExpired:
        raise IosimError(f"{' '.join(args[:4])} timed out after {timeout}s")
    if check and proc.returncode != 0:
        raise IosimError(f"{' '.join(shlex.quote(a) for a in args[:5])} failed: {(proc.stderr or proc.stdout).strip()[:800]}")
    return proc


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def is_mac():
    return platform.system() == "Darwin"


def have_simctl():
    if not is_mac() or not which("xcrun"):
        return False
    return subprocess.run(["xcrun", "--find", "simctl"], capture_output=True).returncode == 0


# ---------------------------------------------------------------- devices


def simctl(*args, timeout=120, check=True):
    return run_cmd(["xcrun", "simctl", *args], timeout=timeout, check=check)


def list_devices():
    data = json.loads(simctl("list", "devices", "-j").stdout)
    out = []
    for runtime, devices in data.get("devices", {}).items():
        if "iOS" not in runtime:
            continue
        for d in devices:
            if d.get("isAvailable", True):
                out.append({"udid": d["udid"], "name": d["name"], "state": d["state"], "runtime": runtime.rsplit(".", 1)[-1],
                            "type": d.get("deviceTypeIdentifier", "").rsplit(".", 1)[-1]})
    return out


def resolve_device(spec=None):
    """A device by UDID or exact name; else IOSIM_DEVICE; else the only booted iOS simulator."""
    spec = spec or os.environ.get("IOSIM_DEVICE")
    devices = list_devices()
    if spec:
        for d in devices:
            if d["udid"] == spec or d["name"] == spec:
                return d
        raise IosimError(f"no iOS simulator named or with UDID {spec!r}", "run `iosim device list`, or `iosim device ensure --name <name>`")
    booted = [d for d in devices if d["state"] == "Booted"]
    if len(booted) == 1:
        return booted[0]
    if not booted:
        raise IosimError("no iOS simulator is booted", "run `iosim device ensure` (creates or boots an iPhone 15), or pass --device")
    names = ", ".join(f"{d['name']} ({d['udid']})" for d in booted)
    raise IosimError(f"several simulators are booted: {names}", "pass --device <udid|name> or set IOSIM_DEVICE")


def booted_device(spec=None):
    d = resolve_device(spec)
    if d["state"] != "Booted":
        raise IosimError(f"{d['name']} is {d['state']}", f"run `iosim device boot --device {shlex.quote(d['udid'])}`")
    return d


def latest_ios_runtime():
    data = json.loads(simctl("list", "runtimes", "-j").stdout)
    runtimes = [r for r in data.get("runtimes", []) if r.get("platform", "iOS") == "iOS" and "iOS" in r.get("name", "") and r.get("isAvailable")]
    if not runtimes:
        raise IosimError("no iOS Simulator runtime is installed", "install one: xcodebuild -downloadPlatform iOS (or Xcode > Settings > Components)")
    runtimes.sort(key=lambda r: [int(x) for x in re.findall(r"\d+", r.get("version", "0"))])
    return runtimes[-1]


def ensure_device(type_name=DEFAULT_DEVICE_TYPE, name=None, boot=True):
    devices = list_devices()
    wanted = name or f"{type_name} (iosim)"
    found = next((d for d in devices if d["name"] == wanted), None)
    created = False
    if not found and not name:
        # Reuse any existing simulator of the type, booted ones first.
        type_id = type_name.replace(" ", "-")
        same = [d for d in devices if d["type"] == type_id or d["name"] == type_name or d["name"].startswith(type_name + " (")]
        same.sort(key=lambda d: d["state"] != "Booted")
        found = same[0] if same else None
    if not found:
        types = json.loads(simctl("list", "devicetypes", "-j").stdout).get("devicetypes", [])
        dtype = next((t for t in types if t["name"] == type_name), None)
        if not dtype:
            raise IosimError(f"unknown device type {type_name!r}", "list them with `xcrun simctl list devicetypes`")
        runtime = latest_ios_runtime()
        udid = simctl("create", wanted, dtype["identifier"], runtime["identifier"]).stdout.strip()
        found = {"udid": udid, "name": wanted, "state": "Shutdown", "runtime": runtime["identifier"].rsplit(".", 1)[-1], "type": type_name}
        created = True
    if boot and found["state"] != "Booted":
        boot_device(found["udid"])
        found["state"] = "Booted"
    return dict(found, created=created)


def boot_device(udid):
    simctl("boot", udid, check=False)
    simctl("bootstatus", udid, "-b", timeout=300)


def text_size_get(udid):
    return simctl("ui", udid, "content_size").stdout.strip()


def appearance_get(udid):
    return simctl("ui", udid, "appearance").stdout.strip()


# ---------------------------------------------------------------- agent-device driver


class Driver:
    """Device actions. A given agent-device command (--agent-device or IOSIM_AGENT_DEVICE, for example the one T3's
    device_open returned) is used as is for every launch, tap, text entry, screenshot, recording and setting. Otherwise
    an agent-device on PATH gets --platform/--udid added, and simctl covers what it can."""

    def __init__(self, udid, given=None):
        self.udid = udid
        given = given if given is not None else os.environ.get("IOSIM_AGENT_DEVICE")
        if given:
            self.ad = shlex.split(given)
            self.mode = "agent-device (given)"
        else:
            found = which("agent-device")
            self.ad = [found, "--platform", "ios", "--udid", udid, "--session", "iosim"] if found else None
            self.mode = "agent-device (PATH)" if found else "simctl"
        self.session_opened = False

    def describe(self):
        return {"mode": self.mode, "agentDevice": " ".join(shlex.quote(a) for a in self.ad) if self.ad else None}

    def require_ad(self, what):
        if not self.ad:
            raise IosimError(
                f"{what} needs agent-device (real touch and keyboard input)",
                f"inside T3 pass the command device_open returned with --agent-device or IOSIM_AGENT_DEVICE; elsewhere: {AGENT_DEVICE_INSTALL}",
            )

    def ad_run(self, *args, timeout=180, as_json=True, ad=None):
        cmd = (ad or self.ad) + list(args) + (["--json"] if as_json else [])
        proc = run_cmd(cmd, timeout=timeout, check=False)
        out = proc.stdout.strip()
        data = None
        if as_json:
            try:
                data = json.loads(out) if out else None
            except ValueError:
                data = None
        if proc.returncode != 0 or (isinstance(data, dict) and data.get("success") is False):
            err = data.get("error") if isinstance(data, dict) else None
            msg = (err.get("message") if isinstance(err, dict) else err) or (proc.stderr or out).strip()[:800]
            raise IosimError(f"agent-device {args[0]} failed: {msg}")
        return data.get("data", data) if isinstance(data, dict) else out

    def ensure_session(self):
        """agent-device input, snapshots and recordings need an app session: open Safari once per process."""
        if self.ad and not self.session_opened:
            self.ad_run("open", SAFARI)
            self.session_opened = True

    def open_url(self, url):
        if self.ad:
            self.ad_run("open", SAFARI, url)
            self.session_opened = True
        else:
            simctl("openurl", self.udid, url)

    def tap(self, x, y):
        self.require_ad("tap")
        self.ensure_session()
        return self.ad_run("press", str(x), str(y))

    def tap_label(self, label):
        self.require_ad("tap")
        self.ensure_session()
        return self.ad_run("press", f"label={json.dumps(label)}")

    def type_text(self, text):
        self.require_ad("type")
        self.ensure_session()
        return self.ad_run("type", text)

    def keyboard(self, action):
        self.require_ad("keyboard")
        return self.ad_run("keyboard", action)

    def scroll(self, direction, amount=None):
        self.require_ad("scroll")
        self.ensure_session()
        args = ["scroll", direction] + ([str(amount)] if amount is not None else [])
        return self.ad_run(*args)

    def snapshot(self):
        self.require_ad("locating the page on screen")
        self.ensure_session()
        return self.ad_run("snapshot", timeout=120)

    def screenshot(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.ad:
            self.ensure_session()
            self.ad_run("screenshot", str(path), "--pixel-density", "3")
        else:
            simctl("io", self.udid, "screenshot", "--type=png", str(path))
        return str(path)

    def record_start(self, path):
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        key = f"record-{self.udid}.json"
        if read_state(key):
            raise IosimError("a recording is already running on this device", "run `iosim record stop` first")
        if self.ad:
            self.ensure_session()
            self.ad_run("record", "start", str(path), "--scope", "device")
            write_state(key, {"via": "agent-device", "path": str(path), "agentDevice": self.ad, "started": time.time()})
        else:
            log = open(state_dir() / f"record-{self.udid}.log", "w")
            proc = subprocess.Popen(["xcrun", "simctl", "io", self.udid, "recordVideo", "--codec=h264", "--force", str(path)],
                                    stdout=log, stderr=log, start_new_session=True)
            time.sleep(1.0)
            if proc.poll() is not None:
                raise IosimError("simctl recordVideo exited at once: " + (state_dir() / f"record-{self.udid}.log").read_text()[-500:])
            write_state(key, {"via": "simctl", "path": str(path), "pid": proc.pid, "started": time.time()})
        return str(path)

    def record_stop(self):
        key = f"record-{self.udid}.json"
        st = read_state(key)
        if not st:
            raise IosimError("no recording is running on this device")
        try:
            if st["via"] == "agent-device":
                self.ad_run("record", "stop", timeout=300, ad=self.ad or st.get("agentDevice"))
            else:
                pid = st["pid"]
                if pid_alive(pid):
                    os.kill(pid, 2)  # SIGINT lets simctl finish the file
                    for _ in range(100):
                        if not pid_alive(pid):
                            break
                        time.sleep(0.1)
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass
        finally:
            write_state(key, None)
        path = Path(st["path"])
        return {"path": str(path), "exists": path.exists(), "bytes": path.stat().st_size if path.exists() else 0,
                "seconds": round(time.time() - st["started"], 1)}

    def set_text_size(self, size):
        size = normalize_text_size(size)
        if self.ad:
            self.ad_run("settings", "text-size", size)
        else:
            simctl("ui", self.udid, "content_size", size)
        return size

    def set_appearance(self, mode):
        if mode not in ("light", "dark"):
            raise IosimError("appearance is light or dark", code=2)
        if self.ad:
            self.ad_run("settings", "appearance", mode)
        else:
            simctl("ui", self.udid, "appearance", mode)
        return mode


# ---------------------------------------------------------------- proxy


def proxy_status(udid):
    st = read_state(f"proxy-{udid}.json")
    if not st:
        return None
    if not pid_alive(st.get("pid")):
        write_state(f"proxy-{udid}.json", None)
        return None
    return st


def port_free(port):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def web_inspector_socket(udid):
    out = simctl("getenv", udid, "RWI_LISTEN_SOCKET", check=False).stdout.strip()
    if out and Path(out).exists():
        return out
    raise IosimError("this simulator has no Web Inspector socket yet", "boot it fully (iosim device boot) and open Safari once")


def proxy_stop(udid):
    st = read_state(f"proxy-{udid}.json")
    if st and pid_alive(st.get("pid")):
        os.kill(st["pid"], 15)
        for _ in range(30):
            if not pid_alive(st["pid"]):
                break
            time.sleep(0.1)
    write_state(f"proxy-{udid}.json", None)
    return bool(st)


def proxy_ensure(udid):
    """Start (or reuse) one ios_webkit_debug_proxy for this simulator. Its page list is on port+1."""
    sock = web_inspector_socket(udid)
    st = proxy_status(udid)
    if st and st.get("socket") == sock:
        return st
    if st:
        proxy_stop(udid)  # the simulator rebooted: its socket moved
    exe = which("ios_webkit_debug_proxy")
    if not exe:
        raise IosimError("ios_webkit_debug_proxy is not installed", PROXY_INSTALL)
    base = int(os.environ.get("IOSIM_PROXY_PORT", DEFAULT_PROXY_PORT))
    port = None
    for candidate in range(base, base + 200, 20):
        if all(port_free(p) for p in range(candidate, candidate + 3)):
            port = candidate
            break
    if port is None:
        raise IosimError(f"no free port block from {base}", "set IOSIM_PROXY_PORT to a free port")
    log = open(state_dir() / f"proxy-{udid}.log", "w")
    proc = subprocess.Popen([exe, "-s", f"unix:{sock}", "-c", f"null:{port},:{port + 1}-{port + 10}", "-F"],
                            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    st = {"pid": proc.pid, "port": port, "pagesPort": port + 1, "socket": sock, "started": time.time()}
    write_state(f"proxy-{udid}.json", st)
    for _ in range(50):
        if proc.poll() is not None:
            write_state(f"proxy-{udid}.json", None)
            raise IosimError("ios_webkit_debug_proxy exited: " + (state_dir() / f"proxy-{udid}.log").read_text()[-500:])
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1).read()
            return st
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    return st


def list_pages(udid):
    st = proxy_ensure(udid)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{st['pagesPort']}/json", timeout=5) as r:
            pages = json.loads(r.read().decode())
    except (OSError, urllib.error.URLError, ValueError):
        return []
    return [{"title": p.get("title"), "url": p.get("url"), "appId": p.get("appId"),
             "webSocketDebuggerUrl": (p.get("webSocketDebuggerUrl") or "").replace("ws://localhost:", "ws://127.0.0.1:")} for p in pages]


# ---------------------------------------------------------------- page session


TAP_RECT_JS = """
const el = document.querySelector(%s);
if (!el) throw new Error("no element matches " + %s);
const r0 = el.getBoundingClientRect();
if (r0.top < 0 || r0.bottom > innerHeight || r0.left < 0 || r0.right > innerWidth) {
  el.scrollIntoView({block: "center", inline: "center"});
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  await new Promise((r) => setTimeout(r, 150));
}
const r = el.getBoundingClientRect();
const vv = window.visualViewport;
return {rect: {x: r.x, y: r.y, width: r.width, height: r.height}, scale: vv ? vv.scale : 1,
        offset: vv ? {x: vv.offsetLeft, y: vv.offsetTop} : {x: 0, y: 0}, title: document.title, tag: el.tagName.toLowerCase()};
"""


class Session:
    def __init__(self, device=None, agent_device=None):
        self.device = booted_device(device)
        self.udid = self.device["udid"]
        self.driver = Driver(self.udid, agent_device)

    # pages -------------------------------------------------------
    def last_url(self):
        return (read_state(f"page-{self.udid}.json") or {}).get("url")

    def connect(self, hint=None, timeout=10.0):
        pages = list_pages(self.udid)
        page = pick_page(pages, hint or self.last_url())
        if not page:
            raise IosimError("Safari has no inspectable page", "run `iosim open <url>` first")
        return Inspector(page["webSocketDebuggerUrl"], timeout=timeout)

    def with_page(self, fn, hint=None, retries=3):
        last = None
        for attempt in range(retries):
            try:
                insp = self.connect(hint)
            except (IosimError, OSError) as e:
                last = e
                time.sleep(0.5)
                continue
            try:
                return fn(insp)
            except (ConnectionError, OSError) as e:
                last = e
                time.sleep(0.5)
            finally:
                insp.close()
        if isinstance(last, IosimError):
            raise last
        raise IosimError(f"lost the page connection: {last}", "retry; if it repeats run `iosim proxy restart`")

    def evaluate(self, source, is_body=False, timeout=30.0, hint=None):
        return self.with_page(lambda i: i.evaluate(source, is_body=is_body, timeout=timeout), hint=hint)

    def open(self, url, new_tab=False, wait=True, timeout=45.0):
        started = time.time()
        token = f"iosim-{time.time_ns()}"
        pages = list_pages(self.udid)
        before = {p["webSocketDebuggerUrl"] for p in pages}
        how = "new-tab"
        if pages and not new_tab:
            def navigate(insp):
                insp.evaluate(f"(window.__iosimNav = {json.dumps(token)}, setTimeout(() => location.assign({json.dumps(url)}), 0), true)")
            try:
                self.with_page(navigate, retries=1)
                how = "same-tab"
            except IosimError:
                self.driver.open_url(url)
        else:
            self.driver.open_url(url)
        write_state(f"page-{self.udid}.json", {"url": url, "at": time.time()})
        result = {"url": url, "how": how, "driver": self.driver.mode}
        if wait:
            result.update(self.wait_loaded(url, token if how == "same-tab" else None, before, timeout))
        result["ms"] = int((time.time() - started) * 1000)
        return result

    def wait_loaded(self, url, token, before, timeout):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            time.sleep(0.3)
            try:
                pages = list_pages(self.udid)
                if token is None:
                    fresh = [p for p in pages if p["webSocketDebuggerUrl"] not in before]
                    page = pick_page(fresh, url) or pick_page(pages, url)
                else:
                    page = pick_page(pages, self.last_url())
                if not page:
                    continue
                insp = Inspector(page["webSocketDebuggerUrl"], timeout=5)
                try:
                    info = insp.evaluate("({href: location.href, readyState: document.readyState, nav: window.__iosimNav || null, title: document.title})", timeout=5)
                finally:
                    insp.close()
                last = info
                if info["readyState"] == "complete" and (token is None or info["nav"] != token) and info["href"] != "about:blank":
                    write_state(f"page-{self.udid}.json", {"url": info["href"], "at": time.time()})
                    return {"finalUrl": info["href"], "title": info["title"], "redirected": not url_matches(info["href"], url)}
            except (IosimError, OSError, ConnectionError, ValueError) as e:
                last = str(e)
        raise IosimError(f"page did not finish loading within {timeout:.0f}s (last seen: {last})",
                         "check the URL loads from this Mac (curl it), or raise --timeout")

    def console(self, errors_only=True, settle=0.5):
        def read(insp):
            insp.call("Console.enable")
            insp.drain(settle)
            out = []
            for ev in insp.events:
                if ev.get("method") != "Console.messageAdded":
                    continue
                m = ev["params"]["message"]
                if errors_only and m.get("level") != "error":
                    continue
                out.append({"level": m.get("level"), "source": m.get("source"), "text": m.get("text"),
                            "url": m.get("url"), "line": m.get("line"), "column": m.get("column")})
            return out
        return self.with_page(read)

    def wait(self, selector=None, text=None, js=None, ms=None, timeout=15.0):
        if ms is not None:
            time.sleep(ms / 1000.0)
            return {"waitedMs": ms}
        if selector is not None:
            expr = f"!!document.querySelector({json.dumps(selector)})"
        elif text is not None:
            expr = f"(document.body && document.body.innerText || '').includes({json.dumps(text)})"
        elif js is not None:
            expr = js
        else:
            raise IosimError("wait needs selector, text, js or ms", code=2)
        start = time.time()
        while True:
            try:
                if self.evaluate(expr, timeout=5):
                    return {"waitedMs": int((time.time() - start) * 1000)}
            except IosimError:
                pass
            if time.time() - start > timeout:
                raise IosimError(f"wait timed out after {timeout:.0f}s for {selector or text or js}")
            time.sleep(0.25)

    # input -------------------------------------------------------
    def web_origin(self, title=None):
        snap = self.driver.snapshot()
        nodes = snap.get("nodes", []) if isinstance(snap, dict) else []
        origin = web_origin_from_snapshot(nodes, title) or web_origin_from_snapshot(nodes)
        if not origin:
            raise IosimError("could not find Safari's web area on screen", "tap with --label <accessible name> or --at <x> <y> instead")
        return origin

    def tap(self, selector=None, label=None, at=None):
        if at is not None:
            x, y = at
            self.driver.tap(x, y)
            return {"x": x, "y": y, "via": "coordinates"}
        if label is not None:
            self.driver.tap_label(label)
            return {"label": label, "via": "accessibility label"}
        info = self.evaluate(TAP_RECT_JS % (json.dumps(selector), json.dumps(selector)), is_body=True)
        origin = self.web_origin(info.get("title"))
        scale = info.get("scale") or 1
        rect = {k: info["rect"][k] * scale for k in ("x", "y", "width", "height")}
        x, y = contents_to_points(rect, origin)
        self.driver.tap(x, y)
        return {"selector": selector, "x": x, "y": y, "rect": info["rect"], "origin": list(origin), "via": "selector"}

    def type_text(self, text, selector=None, enter=False, keep_keyboard=False):
        out = {}
        if selector:
            out["tap"] = self.tap(selector=selector)
            time.sleep(0.4)
        self.driver.type_text(text)
        out["typed"] = len(text)
        if enter:
            self.driver.keyboard("enter")
            out["enter"] = True
        elif not keep_keyboard:
            # Leave the screen as a user would after typing: keyboard down, so later screenshots show the page.
            try:
                self.driver.keyboard("dismiss")
                out["keyboard"] = "dismissed"
            except IosimError:
                self.evaluate("(document.activeElement && document.activeElement.blur(), true)")
                out["keyboard"] = "blurred"
            time.sleep(0.4)
        return out

    def scroll(self, direction=None, amount=None, to=None):
        if to:
            self.evaluate(f"(document.querySelector({json.dumps(to)}) || (() => {{ throw new Error('no element matches ' + {json.dumps(to)}) }})()).scrollIntoView({{block: 'center'}})")
            return {"to": to, "via": "scrollIntoView (programmatic, not a gesture)"}
        self.driver.scroll(direction or "down", amount)
        return {"direction": direction or "down", "amount": amount, "via": "agent-device gesture"}

    def audit(self, script=None, options=None):
        options = options or {}
        if script:
            source = Path(script).read_text() if not isinstance(script, dict) else script["source"]
        else:
            source = AUDIT_JS.read_text()
        body = f"const __opts = {json.dumps(options)};\n" + source
        result = self.evaluate(body, is_body=True, timeout=60)
        return normalize_audit(result)


def normalize_audit(result):
    """Accept {findings: [...]} or a bare list from a project's audit script."""
    if isinstance(result, list):
        result = {"findings": result}
    if not isinstance(result, dict):
        raise IosimError("audit script must return {findings: [...]} or an array of findings")
    findings = []
    for f in result.get("findings") or []:
        if not isinstance(f, dict):
            f = {"message": str(f)}
        f.setdefault("severity", "fail")
        f.setdefault("rule", "custom")
        findings.append(f)
    result["findings"] = findings
    result["counts"] = {"fail": sum(f["severity"] == "fail" for f in findings), "warn": sum(f["severity"] == "warn" for f in findings)}
    return result


# ---------------------------------------------------------------- capture checks


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise IosimError(f"{path} is not a PNG")
    return struct.unpack(">II", head[16:24])


def bmp_pixels(path):
    data = Path(path).read_bytes()
    if data[:2] != b"BM":
        raise ValueError("not a BMP")
    offset = struct.unpack("<I", data[10:14])[0]
    width, height = struct.unpack("<ii", data[18:26])
    bpp = struct.unpack("<H", data[28:30])[0]
    step = bpp // 8
    row = (width * step + 3) & ~3
    pixels = []
    for y in range(abs(height)):
        base = offset + y * row
        for x in range(width):
            b, g, r = data[base + x * step: base + x * step + 3]
            pixels.append((r, g, b))
    return pixels


def pixel_stats(pixels):
    lum = [0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in pixels]
    mean = sum(lum) / len(lum)
    std = (sum((v - mean) ** 2 for v in lum) / len(lum)) ** 0.5
    return {"meanLuminance": round(mean, 1), "stdLuminance": round(std, 1), "distinctColours": len(set(pixels))}


def check_shot(path, expect=None):
    """Sanity of a screenshot: PNG, expected size, and not one flat colour."""
    w, h = png_size(path)
    out = {"path": str(path), "width": w, "height": h, "points": [round(w / 3), round(h / 3)], "problems": []}
    if expect and (w, h) != tuple(expect):
        out["problems"].append(f"size {w}x{h}, expected {expect[0]}x{expect[1]}")
    sips = which("sips") if is_mac() else None
    if sips:
        tmp = state_dir() / f"thumb-{os.getpid()}.bmp"
        try:
            run_cmd([sips, "-s", "format", "bmp", "--resampleWidth", "48", str(path), "--out", str(tmp)], timeout=30)
            out.update(pixel_stats(bmp_pixels(tmp)))
        finally:
            tmp.unlink(missing_ok=True)
        if out["stdLuminance"] < 2 or out["distinctColours"] < 4:
            out["problems"].append("looks blank (one flat colour)")
    out["ok"] = not out["problems"]
    return out


def video_duration(path):
    probe = which("ffprobe")
    if not probe:
        return None
    proc = run_cmd([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], check=False)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def contact_sheet(video, out, frames=12, driver=None):
    """Frames from a video tiled into one PNG an agent can read. ffmpeg first, then agent-device's macOS builder."""
    video, out = Path(video), Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = which("ffmpeg")
    if ffmpeg:
        duration = video_duration(video) or 10.0
        cols = 4
        rows = max(1, (frames + cols - 1) // cols)
        fps = max(frames / max(duration, 0.1), 0.01)
        run_cmd([ffmpeg, "-y", "-loglevel", "error", "-i", str(video), "-vf",
                 f"fps={fps:.4f},scale=300:-1,tile={cols}x{rows}:padding=6:margin=6", "-frames:v", "1", str(out)], timeout=300)
        return {"path": str(out), "via": "ffmpeg", "frames": frames, "videoSeconds": round(duration, 1)}
    ad = driver.ad if driver and driver.ad else None
    if not ad and which("agent-device"):
        ad = [which("agent-device")]
    if ad and is_mac():
        data = driver.ad_run("record", "contact-sheet", str(video), "--out", str(out), timeout=300, ad=ad) if driver else None
        return {"path": str(out), "via": "agent-device", "detail": data}
    raise IosimError("no tool to pull frames from a video", f"{FFMPEG_INSTALL} (or provide agent-device on macOS)")


# ---------------------------------------------------------------- plans


STEP_KINDS = ("open", "eval", "tap", "type", "wait", "shot", "audit", "assert", "text-size", "appearance", "scroll", "console")
STEP_ALIASES = {"textSize": "text-size", "text_size": "text-size", "screenshot": "shot", "js": "eval"}
STEP_META = ("name", "optional", "timeout", "as")
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class PlanError(IosimError):
    def __init__(self, message):
        super().__init__(message, "see the plan format in the ios-sim skill's references/plan.md", code=2)


def substitute(value, variables):
    """Replace ${NAME} in every string of a JSON value; unknown names are an error."""
    if isinstance(value, str):
        def rep(m):
            if m.group(1) not in variables:
                raise PlanError(f"unknown variable ${{{m.group(1)}}}; pass --var {m.group(1)}=...")
            return str(variables[m.group(1)])
        return VAR_RE.sub(rep, value)
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    return value


def _read_source(path, base_dir, where):
    p = Path(path)
    if not p.is_absolute():
        p = Path(base_dir) / p
    try:
        return p.read_text()
    except OSError as e:
        raise PlanError(f"{where}: cannot read {path}: {e.strerror}")


def parse_step(raw, index, base_dir="."):
    where = f"step {index + 1}"
    if not isinstance(raw, dict):
        raise PlanError(f"{where}: a step is an object like {{\"open\": \"https://...\"}}")
    keys = [k for k in raw if k not in STEP_META]
    if len(keys) != 1:
        raise PlanError(f"{where}: needs exactly one action key ({', '.join(STEP_KINDS)}), found {keys or 'none'}")
    key = keys[0]
    kind = STEP_ALIASES.get(key, key)
    if kind not in STEP_KINDS:
        raise PlanError(f"{where}: unknown action {key!r}; actions are {', '.join(STEP_KINDS)}")
    v = raw[key]
    step = {"kind": kind, "index": index}
    for meta in STEP_META:
        if meta in raw:
            step[meta] = raw[meta]
    if "timeout" in step and not isinstance(step["timeout"], (int, float)):
        raise PlanError(f"{where}: timeout is seconds, a number")

    def need(cond, msg):
        if not cond:
            raise PlanError(f"{where} ({kind}): {msg}")

    if kind == "open":
        if isinstance(v, str):
            v = {"url": v}
        need(isinstance(v, dict) and isinstance(v.get("url"), str) and re.match(r"^[a-z]+://", v["url"] or ""), "needs a URL with a scheme, like \"http://host:3000/\"")
        step.update(url=v["url"], newTab=bool(v.get("newTab", False)))
    elif kind == "eval":
        if isinstance(v, str):
            v = {"js": v}
        need(isinstance(v, dict) and (isinstance(v.get("js"), str) or isinstance(v.get("file"), str)), "needs an expression string, {\"js\": ...} or {\"file\": ...}")
        if "file" in v:
            step.update(source=_read_source(v["file"], base_dir, where), body=True, file=v["file"])
        else:
            step.update(source=v["js"], body=bool(v.get("body", False)))
    elif kind == "tap":
        if isinstance(v, str):
            v = {"selector": v}
        need(isinstance(v, dict) and sum(k in v for k in ("selector", "label", "at")) == 1, "needs one of selector, label or at")
        if "at" in v:
            need(isinstance(v["at"], list) and len(v["at"]) == 2 and all(isinstance(n, (int, float)) for n in v["at"]), "at is [x, y] in screen points")
        step.update({k: v[k] for k in ("selector", "label", "at") if k in v})
    elif kind == "type":
        if isinstance(v, str):
            v = {"text": v}
        need(isinstance(v, dict) and isinstance(v.get("text"), str), "needs text")
        step.update(text=v["text"], selector=v.get("selector"), enter=bool(v.get("enter", False)), keepKeyboard=bool(v.get("keepKeyboard", False)))
    elif kind == "wait":
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = {"ms": v}
        need(isinstance(v, dict) and sum(k in v for k in ("selector", "text", "js", "ms")) == 1, "needs a number of ms, or one of selector, text, js, ms")
        step.update({k: v[k] for k in ("selector", "text", "js", "ms") if k in v})
    elif kind == "shot":
        if isinstance(v, str):
            v = {"name": v}
        need(isinstance(v, dict) and isinstance(v.get("name", ""), str), "needs a name")
        step["shotName"] = safe_name(v.get("name") or f"shot-{index + 1}")
    elif kind == "audit":
        if v is True or v is None:
            v = {}
        need(isinstance(v, dict), "is true or an object")
        if "script" in v:
            step["script"] = {"file": v["script"], "source": _read_source(v["script"], base_dir, where)}
        step["options"] = {k: v[k] for k in ("minTarget", "minInputFont", "minTextFont", "max") if k in v}
        fail_on = v.get("failOn", "fail")
        need(fail_on in ("fail", "warn", "none"), "failOn is fail, warn or none")
        step["failOn"] = fail_on
        step["ignore"] = list(v.get("ignore", []))
    elif kind == "assert":
        need(isinstance(v, dict), "is an object: {\"js\": ...}, {\"text\": ...}, {\"selector\": ...}, {\"noConsoleErrors\": true} or {\"eval\": ..., \"equals\": ...}")
        forms = [k for k in ("js", "text", "selector", "noConsoleErrors", "eval") if k in v]
        need(len(forms) == 1, "needs exactly one of js, text, selector, noConsoleErrors, eval")
        if forms[0] == "eval":
            need("equals" in v, "eval needs equals")
            step["equals"] = v["equals"]
        step["check"] = forms[0]
        step["value"] = v[forms[0]]
        step["message"] = v.get("message")
    elif kind == "text-size":
        need(isinstance(v, str), "is a Dynamic Type size name like \"accessibility-large\"")
        step["size"] = normalize_text_size(v)
    elif kind == "appearance":
        need(v in ("light", "dark"), "is light or dark")
        step["mode"] = v
    elif kind == "scroll":
        if isinstance(v, str):
            v = {"direction": v} if v in ("up", "down", "left", "right", "top", "bottom") else {"to": v}
        need(isinstance(v, dict) and ("direction" in v or "to" in v), "is up, down, top, bottom, or {\"to\": selector}")
        step.update({k: v[k] for k in ("direction", "amount", "to") if k in v})
    elif kind == "console":
        step["all"] = bool(isinstance(v, dict) and v.get("all"))
    return step


def parse_plan(obj, base_dir=".", variables=None):
    if isinstance(obj, list):
        obj = {"steps": obj}
    if not isinstance(obj, dict) or not isinstance(obj.get("steps"), list) or not obj["steps"]:
        raise PlanError("a plan is {\"steps\": [...]} with at least one step")
    variables = dict(obj.get("vars") or {}, **(variables or {}))
    obj = substitute({k: v for k, v in obj.items() if k != "vars"}, variables)
    plan = {
        "name": str(obj.get("name") or "plan"),
        "device": obj.get("device"),
        "video": bool(obj.get("video", False)),
        "textSize": normalize_text_size(obj["textSize"]) if obj.get("textSize") else None,
        "appearance": obj.get("appearance"),
        "failOnConsoleErrors": bool(obj.get("failOnConsoleErrors", False)),
        "expectShot": obj.get("expectShot"),
        "restore": obj.get("restore", True),
    }
    if plan["appearance"] not in (None, "light", "dark"):
        raise PlanError("appearance is light or dark")
    plan["steps"] = [parse_step(s, i, base_dir) for i, s in enumerate(obj["steps"])]
    unknown = set(obj) - {"name", "device", "video", "textSize", "appearance", "failOnConsoleErrors", "expectShot", "restore", "steps", "description"}
    if unknown:
        raise PlanError(f"unknown plan keys: {', '.join(sorted(unknown))}")
    return plan


def parse_vars(pairs):
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise IosimError(f"--var takes NAME=value, got {p!r}", code=2)
        k, v = p.split("=", 1)
        out[k] = v
    return out


# ---------------------------------------------------------------- run


def run_plan(plan, out_dir, device=None, agent_device=None, log=None):
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    session = Session(device or plan.get("device"), agent_device)
    drv = session.driver
    udid = session.udid
    started = time.time()
    report = {
        "plan": plan["name"], "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "out": str(out),
        "device": {k: session.device[k] for k in ("name", "udid", "runtime")}, "driver": drv.describe(),
        "steps": [], "shots": [], "audits": [], "console": [], "values": {}, "failures": [],
    }
    original = {"textSize": text_size_get(udid), "appearance": appearance_get(udid)}
    report["initial"] = dict(original)
    current = {"textSize": original["textSize"]}
    shot_no = [0]

    def say(msg):
        if log:
            log(msg)

    def fail(step, message, detail=None):
        entry = {"step": step["index"] + 1, "kind": step["kind"], "message": message}
        if detail is not None:
            entry["detail"] = detail
        if step.get("optional"):
            entry["optional"] = True
        report["failures"].append(entry)

    video_path = None
    try:
        if plan.get("textSize"):
            current["textSize"] = drv.set_text_size(plan["textSize"])
        if plan.get("appearance"):
            drv.set_appearance(plan["appearance"])
        if plan.get("video"):
            video_path = drv.record_start(out / "video.mp4")
        for step in plan["steps"]:
            t0 = time.time()
            entry = {"step": step["index"] + 1, "kind": step["kind"], "name": step.get("name")}
            say(f"step {entry['step']}: {step['kind']}")
            try:
                result = run_step(session, step, out, report, current, shot_no, plan, fail)
                entry["ok"] = True
                if result is not None:
                    entry["result"] = result
            except IosimError as e:
                entry["ok"] = False
                entry["error"] = str(e)
                fail(step, str(e))
                if not step.get("optional"):
                    entry["ms"] = int((time.time() - t0) * 1000)
                    report["steps"].append(entry)
                    report["stoppedAt"] = entry["step"]
                    break
            entry["ms"] = int((time.time() - t0) * 1000)
            report["steps"].append(entry)
        # Errors since the last load, whatever the steps did.
        try:
            report["console"] = session.console(errors_only=True)
        except IosimError as e:
            report["consoleError"] = str(e)
        if plan.get("failOnConsoleErrors") and report["console"]:
            report["failures"].append({"step": None, "kind": "console", "message": f"{len(report['console'])} console error(s)"})
    finally:
        if video_path:
            try:
                report["video"] = drv.record_stop()
                try:
                    report["contactSheet"] = contact_sheet(video_path, out / "contact-sheet.png", driver=drv)
                except IosimError as e:
                    report["contactSheet"] = {"error": str(e), "fix": e.fix}
            except IosimError as e:
                report["video"] = {"error": str(e)}
        if plan.get("restore", True):
            restored = {}
            try:
                if text_size_get(udid) != original["textSize"]:
                    drv.set_text_size(original["textSize"])
                    restored["textSize"] = original["textSize"]
                if original["appearance"] in ("light", "dark") and appearance_get(udid) != original["appearance"]:
                    drv.set_appearance(original["appearance"])
                    restored["appearance"] = original["appearance"]
            except IosimError as e:
                restored["error"] = str(e)
            report["restored"] = restored
        report["seconds"] = round(time.time() - started, 1)
        report["passed"] = not [f for f in report["failures"] if not f.get("optional")]
        (out / "report.json").write_text(json.dumps(report, indent=2))
    return report


def run_step(session, step, out, report, current, shot_no, plan, fail):
    kind = step["kind"]
    drv = session.driver
    timeout = step.get("timeout")
    if kind == "open":
        return session.open(step["url"], new_tab=step["newTab"], timeout=timeout or 45)
    if kind == "eval":
        value = session.evaluate(step["source"], is_body=step["body"], timeout=timeout or 30)
        if step.get("as"):
            report["values"][step["as"]] = value
        return {"value": value}
    if kind == "tap":
        return session.tap(selector=step.get("selector"), label=step.get("label"), at=step.get("at"))
    if kind == "type":
        return session.type_text(step["text"], selector=step.get("selector"), enter=step["enter"], keep_keyboard=step["keepKeyboard"])
    if kind == "wait":
        return session.wait(selector=step.get("selector"), text=step.get("text"), js=step.get("js"), ms=step.get("ms"), timeout=timeout or 15)
    if kind == "shot":
        shot_no[0] += 1
        path = out / f"{shot_no[0]:02d}-{step['shotName']}.png"
        drv.screenshot(path)
        check = check_shot(path, plan.get("expectShot"))
        check.update(name=step["shotName"], textSize=current["textSize"], step=step["index"] + 1)
        report["shots"].append(check)
        if not check["ok"]:
            fail(step, f"screenshot {path.name}: {'; '.join(check['problems'])}")
        return {"path": str(path), "ok": check["ok"]}
    if kind == "audit":
        result = session.audit(step.get("script"), step.get("options"))
        ignore = set(step.get("ignore") or [])
        result["findings"] = [f for f in result["findings"] if f.get("rule") not in ignore]
        result = normalize_audit(result)
        result.update(step=step["index"] + 1, name=step.get("name"), textSize=current["textSize"],
                      script=(step.get("script") or {}).get("file", "built-in"))
        report["audits"].append(result)
        failing = {"fail": ["fail"], "warn": ["fail", "warn"], "none": []}[step["failOn"]]
        bad = [f for f in result["findings"] if f["severity"] in failing]
        if bad:
            rules = sorted({f["rule"] for f in bad})
            fail(step, f"audit: {len(bad)} finding(s): {', '.join(rules)}")
        return {"counts": result["counts"]}
    if kind == "assert":
        check, value = step["check"], step["value"]
        if check == "noConsoleErrors":
            errors = session.console(errors_only=True)
            ok = not errors if value else True
            detail = errors
        elif check == "js":
            got = session.evaluate(value, timeout=timeout or 30)
            ok, detail = bool(got), got
        elif check == "text":
            ok = bool(session.evaluate(f"(document.body && document.body.innerText || '').includes({json.dumps(value)})"))
            detail = value
        elif check == "selector":
            ok = bool(session.evaluate(f"!!document.querySelector({json.dumps(value)})"))
            detail = value
        else:
            got = session.evaluate(value, timeout=timeout or 30)
            ok, detail = got == step["equals"], {"got": got, "expected": step["equals"]}
        if not ok:
            fail(step, step.get("message") or f"assert {check} failed", detail)
        return {"passed": ok, "detail": detail}
    if kind == "text-size":
        current["textSize"] = drv.set_text_size(step["size"])
        time.sleep(1.0)  # Safari restyles -apple-system-* fonts live; give it a moment
        return {"textSize": current["textSize"]}
    if kind == "appearance":
        drv.set_appearance(step["mode"])
        time.sleep(0.5)
        return {"appearance": step["mode"]}
    if kind == "scroll":
        return session.scroll(direction=step.get("direction"), amount=step.get("amount"), to=step.get("to"))
    if kind == "console":
        messages = session.console(errors_only=not step.get("all"))
        return {"messages": messages}
    raise IosimError(f"unhandled step kind {kind}")


# ---------------------------------------------------------------- doctor


def doctor(agent_device=None):
    checks = []

    def add(name, ok, detail, fix=None, required=True):
        checks.append({"name": name, "ok": bool(ok), "required": required, "detail": detail, **({"fix": fix} if fix and not ok else {})})

    add("python", sys.version_info >= (3, 9), platform.python_version(), "install Python 3.9 or newer")
    remote = os.environ.get("IOSIM_REMOTE")
    if not have_simctl():
        why = "not macOS" if not is_mac() else "Xcode command line tools have no simctl"
        add("simulator", False, f"no iOS Simulator on this box ({why})",
            "run iosim on the Mac that has the Simulator: delegate to its T3 environment, or set IOSIM_REMOTE=<ssh host of the Mac> "
            "and rerun (see the ios-sim skill, references/remote.md)")
        if remote:
            ssh = which("ssh")
            proc = subprocess.run([ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", remote, "true"], capture_output=True, text=True) if ssh else None
            add("remote", bool(proc and proc.returncode == 0), f"ssh {remote}: " + ("reachable" if proc and proc.returncode == 0 else ((proc.stderr.strip()[:200] if proc else "no ssh client"))),
                "turn on Remote Login (or Tailscale SSH) on the Mac and install your key; then `iosim remote doctor`")
        return checks
    xcode = run_cmd(["xcode-select", "-p"], check=False).stdout.strip()
    version = run_cmd(["xcodebuild", "-version"], check=False).stdout.strip().replace("\n", " ")
    add("xcode", bool(xcode), f"{version or 'unknown'} at {xcode}", "install Xcode, then: xcode-select -s /Applications/Xcode.app")
    try:
        rt = latest_ios_runtime()
        add("runtime", True, rt.get("name"))
    except IosimError as e:
        add("runtime", False, str(e), e.fix)
    try:
        devices = list_devices()
        booted = [d for d in devices if d["state"] == "Booted"]
        add("device", bool(devices), f"{len(devices)} iOS simulator(s), booted: " + (", ".join(f"{d['name']} ({d['udid']})" for d in booted) or "none"),
            "iosim device ensure")
        add("booted", bool(booted), "a booted simulator" if booted else "none booted", "iosim device ensure", required=False)
    except IosimError as e:
        add("device", False, str(e), e.fix)
    proxy = which("ios_webkit_debug_proxy")
    pv = run_cmd([proxy, "--version"], check=False) if proxy else None
    add("proxy", bool(proxy), (proxy + " " + ((pv.stdout or pv.stderr).strip().splitlines() or [""])[0]) if proxy else "ios_webkit_debug_proxy missing (needed for eval, audit, console)", PROXY_INSTALL)
    given = agent_device or os.environ.get("IOSIM_AGENT_DEVICE")
    ad = given or which("agent-device")
    add("agent-device", bool(ad), (f"given: {given}" if given else ad) if ad else "missing: tap, type and scroll need it; screenshots, video and settings fall back to simctl",
        f"inside T3 pass the command device_open returned (--agent-device or IOSIM_AGENT_DEVICE); elsewhere {AGENT_DEVICE_INSTALL}", required=False)
    node = which("node")
    nv = run_cmd([node, "--version"], check=False).stdout.strip() if node else None
    add("node", bool(node), nv or "missing (a standalone agent-device needs Node)", "install Node 22+", required=False)
    ff = which("ffmpeg")
    add("ffmpeg", bool(ff), ff or "missing: contact sheets fall back to agent-device's macOS builder", FFMPEG_INSTALL, required=False)
    return checks


# ---------------------------------------------------------------- remote (SSH)


def remote_iosim():
    return os.environ.get("IOSIM_REMOTE_IOSIM", "~/.agents/skills/ios-sim/scripts/iosim")


def remote_call(args, stdin_text=None):
    host = os.environ.get("IOSIM_REMOTE")
    if not host:
        raise IosimError("IOSIM_REMOTE is not set", "export IOSIM_REMOTE=<ssh destination of the Mac with the Simulator>", code=2)
    ssh = which("ssh") or "ssh"
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in os.environ.items() if k in ("IOSIM_DEVICE", "IOSIM_PROXY_PORT"))
    command = f"{env} {remote_iosim()} {' '.join(shlex.quote(a) for a in args)}".strip()
    return subprocess.run([ssh, "-o", "BatchMode=yes", host, command], input=stdin_text, text=True, capture_output=True)


def remote_run(plan, out_dir):
    """Send a resolved plan over SSH, run it on the Mac, copy the evidence folder back, remove the remote copy."""
    run_id = f"iosim-runs/{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    payload = json.dumps(dict(plan, iosimResolvedPlan=1))
    proc = remote_call(["run", "-", "--out", run_id], stdin_text=payload)
    host = os.environ["IOSIM_REMOTE"]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    scp = subprocess.run([which("scp") or "scp", "-o", "BatchMode=yes", "-r", f"{host}:{run_id}/.", str(out)], capture_output=True, text=True)
    subprocess.run([which("ssh") or "ssh", "-o", "BatchMode=yes", host, f"rm -rf {shlex.quote(run_id)}"], capture_output=True)
    if scp.returncode != 0:
        raise IosimError(f"copying evidence back failed: {scp.stderr.strip()[:300]}", "check scp works to the Mac")
    return proc


# ---------------------------------------------------------------- CLI


def emit(obj):
    print(json.dumps(obj, indent=2, default=str))


def build_parser():
    p = argparse.ArgumentParser(prog="iosim", description="Test web pages in Mobile Safari on an iOS Simulator. Every command prints one JSON object.")
    p.add_argument("--device", help="simulator UDID or name (default: IOSIM_DEVICE, else the only booted one)")
    p.add_argument("--agent-device", help="agent-device command to drive the device with, e.g. the one T3's device_open returned (default: IOSIM_AGENT_DEVICE)")
    p.add_argument("--version", action="version", version=f"iosim {VERSION}")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    sub.add_parser("doctor", help="check Xcode, runtime, device, proxy, agent-device, Node, ffmpeg; exit 1 on a required miss")

    d = sub.add_parser("device", help="list | ensure | boot | shutdown | status")
    dsub = d.add_subparsers(dest="action", metavar="action")
    dsub.add_parser("list", help="iOS simulators on this Mac")
    e = dsub.add_parser("ensure", help="find or create a simulator of a type and boot it")
    e.add_argument("--type", default=DEFAULT_DEVICE_TYPE, help=f"device type (default {DEFAULT_DEVICE_TYPE})")
    e.add_argument("--name", help="exact simulator name to find or create")
    e.add_argument("--no-boot", action="store_true")
    dsub.add_parser("boot", help="boot --device")
    dsub.add_parser("shutdown", help="shut down --device (stops its proxy first)")
    dsub.add_parser("status", help="device, Text Size, appearance, proxy, pages")

    t = sub.add_parser("text-size", help="read or set Dynamic Type (Text Size)")
    t.add_argument("size", nargs="?", help="e.g. large (default), extra-extra-large, accessibility-large, or ax1..ax5")
    a = sub.add_parser("appearance", help="read or set light/dark")
    a.add_argument("mode", nargs="?", choices=["light", "dark"])

    o = sub.add_parser("open", help="open a URL in Safari and wait for load")
    o.add_argument("url")
    o.add_argument("--new-tab", action="store_true", help="open through the device instead of navigating the current tab")
    o.add_argument("--no-wait", action="store_true")
    o.add_argument("--timeout", type=float, default=45)

    sub.add_parser("pages", help="Safari pages the inspector sees")

    ev = sub.add_parser("eval", help="evaluate JavaScript in the page; awaits promises; prints the JSON value")
    ev.add_argument("expression", nargs="?")
    ev.add_argument("--file", help="file holding a function body that uses `return`")
    ev.add_argument("--body", action="store_true", help="treat the expression as a function body (statements and `return`)")
    ev.add_argument("--timeout", type=float, default=30)

    c = sub.add_parser("console", help="console errors and uncaught exceptions since the page loaded")
    c.add_argument("--all", action="store_true", help="every level, not only errors")

    w = sub.add_parser("wait", help="wait for a selector, text, JS condition or time")
    wg = w.add_mutually_exclusive_group(required=True)
    wg.add_argument("--selector")
    wg.add_argument("--text")
    wg.add_argument("--js")
    wg.add_argument("--ms", type=int)
    w.add_argument("--timeout", type=float, default=15)

    tp = sub.add_parser("tap", help="real tap (agent-device) on a CSS selector, an accessibility label, or screen points")
    tg = tp.add_mutually_exclusive_group(required=True)
    tg.add_argument("--selector")
    tg.add_argument("--label")
    tg.add_argument("--at", nargs=2, type=float, metavar=("X", "Y"))

    ty = sub.add_parser("type", help="type text with the keyboard (agent-device); --selector taps the field first")
    ty.add_argument("text")
    ty.add_argument("--selector")
    ty.add_argument("--enter", action="store_true", help="press return after typing (submits most forms)")
    ty.add_argument("--keep-keyboard", action="store_true", help="leave the keyboard up (default: dismiss it)")

    sc = sub.add_parser("scroll", help="swipe up/down/top/bottom (agent-device), or --to a selector (programmatic)")
    sc.add_argument("direction", nargs="?", choices=["up", "down", "left", "right", "top", "bottom"])
    sc.add_argument("--amount", type=float, help="fraction of the screen, up to 0.8")
    sc.add_argument("--to", help="CSS selector to scroll into view with scrollIntoView")

    sh = sub.add_parser("shot", help="screenshot to a PNG path, with a sanity check")
    sh.add_argument("path")

    r = sub.add_parser("record", help="start PATH | stop: screen recording")
    r.add_argument("action", choices=["start", "stop"])
    r.add_argument("path", nargs="?")

    cs = sub.add_parser("check-shot", help="sanity of a PNG: size, not blank")
    cs.add_argument("path")
    cs.add_argument("--expect", help="WIDTHxHEIGHT in pixels, e.g. 1179x2556")

    sheet = sub.add_parser("contact-sheet", help="tile frames from a video into one PNG")
    sheet.add_argument("video")
    sheet.add_argument("--out", required=True)
    sheet.add_argument("--frames", type=int, default=12)

    au = sub.add_parser("audit", help="generic mobile page audit (or --script yours); exit 1 on fail findings")
    au.add_argument("--script", help="JS file: a function body returning {findings: [...]}")
    au.add_argument("--min-target", type=float, default=44)
    au.add_argument("--min-input-font", type=float, default=16)
    au.add_argument("--warn-only", action="store_true", help="exit 0 even with fail findings")

    rn = sub.add_parser("run", help="run a JSON plan; write screenshots, video and report.json to --out; exit 1 on failure")
    rn.add_argument("plan", help="plan file, or - for stdin")
    rn.add_argument("--out", required=True, help="evidence folder")
    rn.add_argument("--var", action="append", default=[], help="NAME=value for ${NAME} in the plan (repeatable)")
    rn.add_argument("--check", action="store_true", help="only parse and print the resolved plan")

    px = sub.add_parser("proxy", help="start | stop | restart | status of this device's ios_webkit_debug_proxy")
    px.add_argument("action", choices=["start", "stop", "restart", "status"])

    sub.add_parser("cleanup", help="stop this device's proxy and any recording iosim started")

    rm = sub.add_parser("remote", help="run an iosim command on the Mac named by IOSIM_REMOTE over SSH")
    rm.add_argument("args", nargs=argparse.REMAINDER)
    return p


def load_plan_arg(path, variables):
    if path == "-":
        raw, base = sys.stdin.read(), "."
    else:
        try:
            raw, base = Path(path).read_text(), str(Path(path).resolve().parent)
        except OSError as e:
            raise PlanError(f"cannot read plan {path}: {e.strerror}")
    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise PlanError(f"plan is not valid JSON: {e}")
    if isinstance(obj, dict) and obj.get("iosimResolvedPlan"):
        obj.pop("iosimResolvedPlan")
        return obj
    return parse_plan(obj, base, variables)


def dispatch(args):
    cmd = args.cmd
    if cmd == "doctor":
        checks = doctor(args.agent_device)
        ok = all(c["ok"] for c in checks if c["required"])
        return {"ok": ok, "checks": checks}, 0 if ok else 1
    if cmd == "remote":
        rest = args.args[1:] if args.args[:1] == ["--"] else args.args
        if rest[:1] == ["run"] and len(rest) >= 2 and rest[1] != "-":
            rp = build_parser().parse_args(rest)
            plan = load_plan_arg(rp.plan, parse_vars(rp.var))
            proc = remote_run(plan, rp.out)
            sys.stderr.write(proc.stderr)
            try:
                report = json.loads(proc.stdout)
            except ValueError:
                report = {"ok": False, "error": "remote run printed no JSON", "stdout": proc.stdout[-1000:]}
            report["localOut"] = str(Path(rp.out).resolve())
            return report, proc.returncode
        proc = remote_call(rest)
        sys.stderr.write(proc.stderr)
        sys.stdout.write(proc.stdout)
        return None, proc.returncode
    if not have_simctl():
        if os.environ.get("IOSIM_REMOTE"):
            # Forward to the Mac. Paths in the command are the Mac's.
            return dispatch(argparse.Namespace(cmd="remote", args=sys.argv[1:], agent_device=None))
        raise IosimError("no iOS Simulator on this box", "run `iosim doctor` for the remote options (T3 delegation or IOSIM_REMOTE over SSH)")

    if cmd == "device":
        act = args.action or "status"
        if act == "list":
            return {"ok": True, "devices": list_devices()}, 0
        if act == "ensure":
            return {"ok": True, "device": ensure_device(args.type, args.name, boot=not args.no_boot)}, 0
        dev = resolve_device(args.device)
        if act == "boot":
            boot_device(dev["udid"])
            return {"ok": True, "device": dict(dev, state="Booted")}, 0
        if act == "shutdown":
            proxy_stop(dev["udid"])
            simctl("shutdown", dev["udid"], check=False)
            return {"ok": True, "device": dict(dev, state="Shutdown")}, 0
        out = {"ok": True, "device": dev}
        if dev["state"] == "Booted":
            out.update(textSize=text_size_get(dev["udid"]), appearance=appearance_get(dev["udid"]), proxy=proxy_status(dev["udid"]),
                       recording=read_state(f"record-{dev['udid']}.json"), driver=Driver(dev["udid"], args.agent_device).describe())
        return out, 0
    if cmd in ("check-shot", "contact-sheet"):
        if cmd == "check-shot":
            expect = tuple(int(n) for n in args.expect.lower().split("x")) if args.expect else None
            res = check_shot(args.path, expect)
            return {"ok": res["ok"], **res}, 0 if res["ok"] else 1
        drv = None
        try:
            drv = Driver(booted_device(args.device)["udid"], args.agent_device)
        except IosimError:
            pass
        return {"ok": True, **contact_sheet(args.video, args.out, args.frames, drv)}, 0
    if cmd == "run":
        plan = load_plan_arg(args.plan, parse_vars(args.var))
        if args.check:
            return {"ok": True, "plan": plan}, 0
        report = run_plan(plan, args.out, args.device, args.agent_device, log=lambda m: print(m, file=sys.stderr))
        summary = {"ok": report["passed"], "report": str(Path(report["out"]) / "report.json"), "out": report["out"],
                   "failures": report["failures"], "shots": [s["path"] for s in report["shots"]],
                   "video": (report.get("video") or {}).get("path"), "contactSheet": (report.get("contactSheet") or {}).get("path") or report.get("contactSheet"),
                   "seconds": report["seconds"]}
        return summary, 0 if report["passed"] else 1

    dev = booted_device(args.device)
    udid = dev["udid"]
    if cmd == "proxy":
        if args.action == "stop":
            return {"ok": True, "stopped": proxy_stop(udid)}, 0
        if args.action == "status":
            return {"ok": True, "proxy": proxy_status(udid)}, 0
        if args.action == "restart":
            proxy_stop(udid)
        return {"ok": True, "proxy": proxy_ensure(udid)}, 0
    if cmd == "cleanup":
        out = {"ok": True, "proxyStopped": proxy_stop(udid)}
        if read_state(f"record-{udid}.json"):
            out["recording"] = Driver(udid, args.agent_device).record_stop()
        return out, 0
    drv = Driver(udid, args.agent_device)
    if cmd == "text-size":
        if args.size:
            drv.set_text_size(args.size)
        return {"ok": True, "textSize": text_size_get(udid), "driver": drv.mode}, 0
    if cmd == "appearance":
        if args.mode:
            drv.set_appearance(args.mode)
        return {"ok": True, "appearance": appearance_get(udid), "driver": drv.mode}, 0
    if cmd == "shot":
        drv.screenshot(args.path)
        res = check_shot(args.path)
        return {"ok": res["ok"], "driver": drv.mode, **res}, 0 if res["ok"] else 1
    if cmd == "record":
        if args.action == "start":
            if not args.path:
                raise IosimError("record start needs a path", code=2)
            return {"ok": True, "recording": drv.record_start(args.path), "driver": drv.mode}, 0
        return {"ok": True, **drv.record_stop()}, 0

    session = Session(udid, args.agent_device)
    if cmd == "pages":
        return {"ok": True, "pages": list_pages(udid)}, 0
    if cmd == "open":
        return {"ok": True, **session.open(args.url, new_tab=args.new_tab, wait=not args.no_wait, timeout=args.timeout)}, 0
    if cmd == "eval":
        if args.file:
            source, body = Path(args.file).read_text(), True
        elif args.expression is not None:
            source, body = args.expression, args.body
        else:
            raise IosimError("eval needs an expression or --file", code=2)
        return {"ok": True, "value": session.evaluate(source, is_body=body, timeout=args.timeout)}, 0
    if cmd == "console":
        return {"ok": True, "messages": session.console(errors_only=not args.all)}, 0
    if cmd == "wait":
        return {"ok": True, **session.wait(args.selector, args.text, args.js, args.ms, args.timeout)}, 0
    if cmd == "tap":
        return {"ok": True, **session.tap(selector=args.selector, label=args.label, at=args.at)}, 0
    if cmd == "type":
        return {"ok": True, **session.type_text(args.text, args.selector, args.enter, args.keep_keyboard)}, 0
    if cmd == "scroll":
        if not args.direction and not args.to:
            raise IosimError("scroll needs a direction or --to", code=2)
        return {"ok": True, **session.scroll(args.direction, args.amount, args.to)}, 0
    if cmd == "audit":
        res = session.audit(args.script, {"minTarget": args.min_target, "minInputFont": args.min_input_font})
        ok = res["counts"]["fail"] == 0
        return {"ok": ok or args.warn_only, **res}, 0 if ok or args.warn_only else 1
    raise IosimError(f"unknown command {cmd}", code=2)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 2
    try:
        result, code = dispatch(args)
    except IosimError as e:
        emit({"ok": False, "error": str(e), **({"fix": e.fix} if e.fix else {})})
        return e.code
    except KeyboardInterrupt:
        return 130
    if result is not None:
        emit(result)
    return code


if __name__ == "__main__":
    sys.exit(main())
