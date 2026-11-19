"""iosim: test web pages in Mobile Safari on an iOS Simulator. Python 3.9+, stdlib only.

Run `iosim --help`, or read the ios-sim skill's references/cli.md.
"""

import argparse
import base64
import fcntl
import json
import os
import platform
import re
import shlex
import shutil
import signal
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


class PageError(IosimError):
    """The page's own JavaScript threw (as opposed to a lost connection or a timeout)."""


class PageSyntaxError(PageError):
    """The source did not parse. Raised before anything ran, so trying another reading of it is safe."""


class Terminated(BaseException):
    """SIGTERM, turned into an exception so a run still restores the device and writes its report."""


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


def strip_expression(source):
    """An expression file may end with `;` (written for page.evaluate): drop trailing semicolons so it fits in ( )."""
    return re.sub(r"[\s;]+$", "", source)


def js_program(source, is_body=False, prelude=""):
    """Wrap an expression (or, with is_body, a function body that uses `return`) so the page
    always answers with a JSON string: {"ok": true, "value": ...} or {"ok": false, "error": ...}.
    `prelude` runs first in the same scope (iosim uses it to define `__opts` for audit scripts)."""
    inner = "{\n" + source + "\n}" if is_body else "(\n" + strip_expression(source) + "\n)"
    return (
        "(async () => { " + prelude + " try { const __v = await (async () => " + inner + ")();"
        " return JSON.stringify({ok: true, value: __v === undefined ? null : __v}); }"
        " catch (e) { return JSON.stringify({ok: false, error: String(e) + (e && /:\\d/.test(e.stack || '') ? ' at ' + e.stack : '')}); } })()"
    )


def page_key(page):
    """A Safari page's identity: the inspector page path (stable while the tab lives, across navigations, even to
    another site) and the Safari process. The page path is the proxy's own counter, which a restarted proxy hands
    out afresh, so the identity only holds for one proxy process: see proxy_id and driven_page."""
    ws = (page or {}).get("webSocketDebuggerUrl") or ""
    return {"page": urllib.parse.urlsplit(ws).path, "appId": (page or {}).get("appId")}


def find_page(pages, key):
    """The page with this identity, or None."""
    if not key or not key.get("page"):
        return None
    for p in pages:
        if p.get("webSocketDebuggerUrl") and page_key(p) == {"page": key["page"], "appId": key.get("appId")}:
            return p
    return None


def proxy_id(st):
    """What tells one inspector proxy process from the next: its pid and start time."""
    return {"pid": st.get("pid"), "started": st.get("started")} if st else None


def driven_page(pages, state, proxy=None):
    """The page iosim drives: the one the last `open` recorded, by identity, never by URL. Raises when it is gone,
    when the proxy that numbered it has been replaced (`proxy` is the running proxy's proxy_id), or when nothing
    was recorded and Safari has several pages to choose from."""
    pages = [p for p in pages if p.get("webSocketDebuggerUrl")]
    state = state or {}
    if state.get("page"):
        if proxy is not None and state.get("proxy") != proxy:
            raise IosimError(f"the inspector proxy restarted since `open`, so iosim can no longer tell which tab it was driving ({state.get('url')})",
                             "run `iosim open <url>` to load the page again")
        page = find_page(pages, state)
        if page:
            return page
        raise IosimError(f"the Safari page iosim was driving ({state.get('url')}) is gone: its tab was closed or Safari restarted",
                         "run `iosim open <url>` to load the page again")
    if len(pages) == 1:
        return pages[0]
    if not pages:
        raise IosimError("Safari has no inspectable page", "run `iosim open <url>` first")
    raise IosimError(f"Safari has {len(pages)} pages and iosim has not opened one it can name",
                     "run `iosim open <url>`: iosim then drives that page, whatever other tabs show")


def stale_document(time_origin_ms, started, slack=2.0):
    """True when a document's performance.timeOrigin (ms since the epoch, on the Mac's clock: the Simulator shares it)
    lies before the open that should have loaded it. Unknown values are not called stale."""
    if not isinstance(time_origin_ms, (int, float)) or not started:
        return False
    return time_origin_ms / 1000.0 < started - slack


def url_matches(current, wanted):
    """Loose match after redirects: same host, and the wanted path is a prefix of the current one."""
    if not current or not wanted:
        return False
    c, w = urllib.parse.urlsplit(current), urllib.parse.urlsplit(wanted)
    return c.netloc == w.netloc and (c.path or "/").startswith((w.path or "/").rstrip("/") or "/")


def is_http(url):
    return urllib.parse.urlsplit(url or "").scheme in ("http", "https")


def load_failure(requested, final_href):
    """Safari shows its own error page (a data: or about: URL) when a page cannot load. A real redirect, even to
    another origin, lands on another http(s) page. Return a message when the requested http(s) URL did not load."""
    if is_http(requested) and not is_http(final_href):
        return f"Safari could not load {requested}: it shows its own error page ({(final_href or '')[:40]})"
    return None


def contents_to_points(rect, origin):
    """Centre of a CSS-pixel viewport rect, in screen points, given the web content origin on screen."""
    return (round(origin[0] + rect["x"] + rect["width"] / 2, 1), round(origin[1] + rect["y"] + rect["height"] / 2, 1))


def webview_subtree(nodes):
    """The nodes inside Safari's outermost WebView (page content only, not Safari's toolbar)."""
    for i, n in enumerate(nodes):
        if n.get("type") == "WebView":
            depth = n.get("depth", 0)
            out = [n]
            for m in nodes[i + 1:]:
                if m.get("depth", 0) <= depth:
                    break
                out.append(m)
            return out
    return []


def _inside(ra, rb, slack=0.5):
    return (ra["x"] >= rb["x"] - slack and ra["y"] >= rb["y"] - slack and ra["x"] + ra["width"] <= rb["x"] + rb["width"] + slack
            and ra["y"] + ra["height"] <= rb["y"] + rb["height"] + slack)


def snapshot_label_counts(nodes):
    """{label: how many distinct elements inside the WebView carry it}. A wrapper that shares its label with a node
    inside it (a button and its text) counts once."""
    content = [n for n in webview_subtree(nodes)[1:] if n.get("rect") and (n.get("label") or "").strip()]
    counts = {}
    for n in content:
        label = n["label"].strip()
        if any(m is not n and m["label"].strip() == label and _inside(m["rect"], n["rect"]) for m in content):
            continue  # an outer wrapper of a node with the same label
        counts[label] = counts.get(label, 0) + 1
    return counts


def web_origin_from_anchors(nodes, anchors, tolerance=2.5):
    """Where the page's (0, 0) sits on screen, from elements seen both ways: each anchor is a page element with its
    accessible name, its rect in visual-viewport CSS px (points at zoom 1) and `pageCount`, how many page elements
    carry that name. A node inside the WebView of the accessibility snapshot with the same label and size gives
    screen - page. Offsets within a point of each other form one cluster.

    Returns None when no anchor matches; {"refused": reason} when the evidence does not single out one offset;
    else {"origin": (x, y), "anchors": n, "unique": u, "target": bool}. It refuses when two clusters have as many
    anchors each, when the best has one anchor whose label is not unique in both the page and the snapshot, and when
    another cluster exists and the best rests only on repeated labels (rows of identical buttons)."""
    sub = webview_subtree(nodes)
    content = [n for n in sub[1:] if n.get("rect") and n.get("label")]
    snap_counts = snapshot_label_counts(nodes)
    votes = {}
    for a in anchors:
        label = (a.get("label") or "").strip()
        if not label:
            continue
        unique = a.get("pageCount") == 1 and snap_counts.get(label) == 1
        for n in content:
            r = n["rect"]
            if n["label"].strip() != label:
                continue
            if abs(r["width"] - a["width"]) > tolerance or abs(r["height"] - a["height"]) > tolerance:
                continue
            # The AX frame can be the CSS box rounded outward: compare centres, not corners.
            dx = (r["x"] + r["width"] / 2) - (a["x"] + a["width"] / 2)
            dy = (r["y"] + r["height"] / 2) - (a["y"] + a["height"] / 2)
            entry = votes.setdefault((round(dx), round(dy)), {"ids": set(), "unique": set(), "target": False})
            entry["ids"].add(a.get("id", label))
            if unique:
                entry["unique"].add(a.get("id", label))
            entry["target"] = entry["target"] or bool(a.get("target"))
    if not votes:
        return None
    # Cluster keys a point apart (rounding), seeding from the most voted key.
    clusters = []
    for key in sorted(votes, key=lambda k: (-len(votes[k]["ids"]), k)):
        home = next((c for c in clusters if abs(c["key"][0] - key[0]) <= 1 and abs(c["key"][1] - key[1]) <= 1), None)
        if home is None:
            home = {"key": key, "ids": set(), "unique": set(), "target": False}
            clusters.append(home)
        home["ids"] |= votes[key]["ids"]
        home["unique"] |= votes[key]["unique"]
        home["target"] = home["target"] or votes[key]["target"]
    clusters.sort(key=lambda c: (-len(c["ids"]), -len(c["unique"]), not c["target"]))
    best = clusters[0]
    second = clusters[1] if len(clusters) > 1 else None
    offsets = lambda c: f"[{c['key'][0]}, {c['key'][1]}]"
    if second and len(second["ids"]) == len(best["ids"]):
        return {"refused": f"two screen offsets fit equally well ({offsets(best)} and {offsets(second)}, {len(best['ids'])} anchor(s) "
                           "each): the page repeats elements with the same name and size"}
    if len(best["ids"]) < 2 and not best["unique"]:
        return {"refused": "only one page element matched the accessibility snapshot, and its name is not unique on the page and on "
                           "screen (an iframe or another element may carry it)"}
    if second and not best["unique"]:
        return {"refused": f"the best screen offset {offsets(best)} ({len(best['ids'])} anchors) rests only on repeated names, and "
                           f"{offsets(second)} also fits {len(second['ids'])}"}
    return {"origin": (float(best["key"][0]), float(best["key"][1])), "anchors": len(best["ids"]),
            "unique": len(best["unique"]), "target": best["target"]}


def web_origin_from_title(nodes, title):
    """Fallback: the node inside the WebView that carries the document's title, spans its width and starts below
    its top (the web area under the status bar). Only a node labelled with the title counts."""
    sub = webview_subtree(nodes)
    if not sub or not title:
        return None
    full = sub[0].get("rect") or {}
    for n in sub[1:]:
        r = n.get("rect")
        if not r or n.get("label") != title:
            continue
        if r["y"] > full.get("y", 0) and r["x"] == full.get("x", 0) and r["width"] >= full.get("width", 0) - 1:
            return (r["x"], r["y"])
    return None


def verify_tap(record, target=None):
    """Judge what a real tap reached, from the event TAP_ARM_JS recorded in the page (None when no pointer or touch
    event reached the page's document). With a target (a description of the element that had to get the tap), raise
    unless the tap landed on it or inside it. Returns what was hit."""
    if record is None:
        if target is None:
            return {"pageHit": None}
        raise IosimError(f"the tap reached no element of the page (no touch, pointer, mouse or click event in its document), so {target} did not get it",
                         "it may have hit Safari's own toolbar, an iframe, or a tab other than the page iosim drives; "
                         "take a screenshot to see the screen, then tap --at <x> <y> or tap --label")
    hit = record.get("target")
    if target is None:
        return {"pageHit": hit, "event": record.get("type")}
    if not record.get("inside"):
        where = f" at ({record.get('x')}, {record.get('y')}) CSS px" if record.get("x") is not None else ""
        raise IosimError(f"the tap landed on {hit}{where}, not on {target}",
                         "iosim placed the page on screen wrongly; tap --at <x> <y> (screen points) or tap --label <accessible name>")
    return {"pageHit": hit, "event": record.get("type"), "verified": True}


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
        self.partial = b""
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

    def _frame(self):
        """Take one whole frame off the buffer, or return None (buffer untouched) when it is not all here yet."""
        buf = self.buf
        if len(buf) < 2:
            return None
        b0, b1 = buf[0], buf[1]
        n, pos = b1 & 0x7F, 2
        if n == 126:
            if len(buf) < 4:
                return None
            n, pos = struct.unpack(">H", buf[2:4])[0], 4
        elif n == 127:
            if len(buf) < 10:
                return None
            n, pos = struct.unpack(">Q", buf[2:10])[0], 10
        mask = None
        if b1 & 0x80:
            if len(buf) < pos + 4:
                return None
            mask, pos = buf[pos:pos + 4], pos + 4
        if len(buf) < pos + n:
            return None
        data = buf[pos:pos + n]
        self.buf = buf[pos + n:]
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return b0, data

    def recv(self, timeout):
        """Return the next text message, or None on timeout. A timeout never loses part of a frame: bytes stay
        buffered until the whole frame has arrived."""
        deadline = time.time() + max(timeout, 0.01)
        while True:
            frame = self._frame()
            if frame is None:
                left = deadline - time.time()
                if left <= 0:
                    return None
                self.sock.settimeout(left)
                try:
                    chunk = self.sock.recv(65536)
                except socket.timeout:
                    return None
                if not chunk:
                    raise ConnectionError("WebSocket closed")
                self.buf += chunk
                continue
            b0, data = frame
            opcode = b0 & 0x0F
            if opcode == 0x8:
                raise ConnectionError("WebSocket closed by peer")
            if opcode == 0x9:
                self.sock.sendall(ws_frame(data, opcode=0xA))
                continue
            if opcode in (0x1, 0x2, 0x0):
                self.partial += data
                if b0 & 0x80:
                    message, self.partial = self.partial, b""
                    return message.decode("utf-8", errors="replace")

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
        self.sent = 0
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
                "reload the page (iosim open <url>) and retry; if it persists run `iosim proxy restart` and then `iosim open <url>` again",
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
        self.sent += 1
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

    def evaluate(self, source, is_body=False, timeout=30.0, prelude=""):
        """Evaluate in the page, await a returned promise, and return the JSON value. is_body="auto" reads the
        source as an expression first and, only if that does not parse, as a function body."""
        if is_body == "auto":
            try:
                return self.evaluate(source, False, timeout, prelude)
            except PageSyntaxError:
                return self.evaluate(source, True, timeout, prelude)
        # WebKit ignores awaitPromise on Runtime.evaluate: get the promise object, then await it.
        result = self.call("Runtime.evaluate", {"expression": js_program(source, is_body, prelude), "returnByValue": False}, timeout=timeout)
        remote = result.get("result", {})
        if result.get("wasThrown"):
            text = str(remote.get("description") or remote.get("value"))
            # The wrapper catches everything that runs, so a throw here is the program failing to parse.
            cls = PageSyntaxError if "SyntaxError" in text or remote.get("className") == "SyntaxError" else PageError
            raise cls("page threw: " + text)
        value = remote.get("value")
        if remote.get("type") == "object" and "objectId" in remote:
            awaited = self.call("Runtime.awaitPromise", {"promiseObjectId": remote["objectId"], "returnByValue": True}, timeout=timeout)
            if awaited.get("wasThrown"):
                raise PageError("page threw: " + str(awaited.get("result", {}).get("description")))
            value = awaited.get("result", {}).get("value")
        try:
            answer = json.loads(value)
        except (TypeError, ValueError):
            raise IosimError(f"unexpected evaluate result: {remote!r}")
        if not answer.get("ok"):
            raise PageError("page error: " + str(answer.get("error")))
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


def pid_command(pid):
    """The command line of a running process, or None."""
    try:
        proc = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="], capture_output=True, text=True, timeout=10)
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None


def pid_is(pid, *names):
    """Whether a saved pid is still the process iosim started. After a reboot or a long time the number can belong to
    an unrelated process, which must never be signalled."""
    if not pid_alive(pid):
        return False
    command = pid_command(pid)
    return bool(command) and any(n in command for n in names)


class FileLock:
    """An exclusive lock on a file in IOSIM_HOME, so two iosim commands at once do not both start a proxy."""

    def __init__(self, name, timeout=60):
        self.path = state_dir() / name
        self.timeout = timeout

    def __enter__(self):
        self.fh = open(self.path, "w")
        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() > deadline:
                    self.fh.close()
                    raise IosimError(f"another iosim command holds {self.path.name}", "wait for it to finish, then retry")
                time.sleep(0.1)

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


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


def pick_runtime(runtimes, device_type=None):
    """The newest available iOS runtime, and when a device type identifier is given, the newest that supports it.
    Two runtimes can share a version (a general one and one made for a single new device); sorting by version alone
    can pick the one that refuses the type."""
    usable = [r for r in runtimes if r.get("platform", "iOS") == "iOS" and "iOS" in r.get("name", "") and r.get("isAvailable")]
    if not usable:
        raise IosimError("no iOS Simulator runtime is installed", "install one: xcodebuild -downloadPlatform iOS (or Xcode > Settings > Components)")
    if device_type:
        fits = [r for r in usable if "supportedDeviceTypes" not in r
                or any(t.get("identifier") == device_type for t in r.get("supportedDeviceTypes") or [])]
        if not fits:
            raise IosimError(f"no installed iOS runtime supports {device_type.rsplit('.', 1)[-1]}",
                             "pick another --type (xcrun simctl list devicetypes), or install a newer runtime")
        usable = fits
    # Newest version first; among equal versions, the runtime that supports the most device types.
    usable.sort(key=lambda r: ([int(x) for x in re.findall(r"\d+", r.get("version", "0"))], len(r.get("supportedDeviceTypes") or [])))
    return usable[-1]


def latest_ios_runtime(device_type=None):
    data = json.loads(simctl("list", "runtimes", "-j").stdout)
    return pick_runtime(data.get("runtimes", []), device_type)


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
        runtime = latest_ios_runtime(dtype["identifier"])
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


def in_use_fix(session, command):
    return (f"the device is claimed by agent-device session {session!r}. If that session is yours (an earlier agent-device "
            f"command of this task), close it: run that command with `close`, and inside T3 also call device_close for "
            f"the device, then device_open again and export the new command. If it belongs to another agent or task, "
            f"do not close it: wait for it to finish, or ask the user. (Your command: {command})")


def device_in_use(data):
    """The session named by an agent-device DEVICE_IN_USE error, else None."""
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict) and err.get("code") == "DEVICE_IN_USE":
        details = err.get("details") or {}
        if details.get("reason", "").startswith(("apple_simulator_recording", "native_")):
            return None
        return details.get("session") or "unknown"
    return None


def pick_label_node(nodes, text):
    """The one snapshot node whose label contains `text` (case-insensitive). Wrappers that share a label and nest
    collapse to the innermost; distinct matches are ambiguous."""
    want = text.lower()
    hits = [n for n in nodes if n.get("rect") and want in (n.get("label") or "").lower()]

    def inside(a, b):
        ra, rb = a["rect"], b["rect"]
        return (ra["x"] >= rb["x"] - 0.5 and ra["y"] >= rb["y"] - 0.5 and ra["x"] + ra["width"] <= rb["x"] + rb["width"] + 0.5
                and ra["y"] + ra["height"] <= rb["y"] + rb["height"] + 0.5)

    inner = [n for n in hits if not any(m is not n and m.get("label") == n.get("label") and inside(m, n) for m in hits)]
    labels = sorted({n.get("label") for n in inner})
    if not inner:
        raise IosimError(f"no element's accessibility label contains {text!r}", "check the labels with `agent-device snapshot`, or tap --selector")
    if len(inner) > 1:
        raise IosimError(f"{len(inner)} elements' labels contain {text!r}: {', '.join(repr(l) for l in labels[:8])}",
                         "use a longer --label, an exact one without --contains, or tap --selector")
    return inner[0]


def flag_value(args, flag):
    """The value of `--flag v` or `--flag=v` in an argument list, else None."""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def precheck_bound_device(args, udid, sessions=None):
    """Before any device action: the simulator a given agent-device command names, from its own --udid, or from the
    live session its --session names (`sessions`: an agent-device `session list` answer). Raises on a mismatch;
    returns the udid when it could tell, else None (then the first `open` answers it)."""
    named = flag_value(args, "--udid")
    if not named and sessions:
        session = flag_value(args, "--session")
        for item in (sessions.get("sessions") or []) if isinstance(sessions, dict) else []:
            if session and isinstance(item, dict) and item.get("name") == session:
                named = item.get("device_udid") or item.get("id")
    if named and named != udid:
        raise IosimError(f"the agent-device command drives simulator {named}, but iosim resolved {udid}",
                         f"pass --device {named} (or set IOSIM_DEVICE) to match the command, or call device_open for {udid} and export that command")
    return named


def check_bound_device(data, udid):
    """agent-device's `open` answers with the device it acted on. A command bound to another simulator (T3's
    device_open binds one) would send taps there while JavaScript goes to `udid`: refuse."""
    got = (data or {}).get("device_udid") or (data or {}).get("id") if isinstance(data, dict) else None
    if not got:
        raise IosimError("could not tell which simulator the agent-device command drives (its open answered no device_udid)",
                         "pass an agent-device command bound to this simulator, or unset IOSIM_AGENT_DEVICE to use agent-device from PATH")
    if got != udid:
        raise IosimError(f"the agent-device command drives simulator {got}, but iosim resolved {udid}",
                         f"pass --device {got} (or set IOSIM_DEVICE) to match the command, or call device_open for {udid} and export that command")
    return got


class Driver:
    """Device actions. A given agent-device command (--agent-device or IOSIM_AGENT_DEVICE, for example the one T3's
    device_open returned) is used as is for every launch, tap, text entry, screenshot, recording and setting, after
    checking it drives the resolved simulator. Otherwise an agent-device on PATH gets --platform/--udid/--session
    added, and simctl covers what it can."""

    def __init__(self, udid, given=None):
        self.udid = udid
        given = given if given is not None else os.environ.get("IOSIM_AGENT_DEVICE")
        if given:
            self.ad = shlex.split(given)
            self.mode = "agent-device (given)"
        else:
            found = which("agent-device")
            self.ad = [found, "--platform", "ios", "--udid", udid, "--session", f"iosim-{udid}"] if found else None
            self.mode = "agent-device (PATH)" if found else "simctl"
        self.session_opened = False
        self.session_name = None
        self.prechecked = False

    def describe(self):
        return {"mode": self.mode, "agentDevice": self.command_text(), "session": self.session_name}

    def command_text(self):
        return " ".join(shlex.quote(a) for a in self.ad) if self.ad else None

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
            claimed = device_in_use(data)
            if claimed:
                raise IosimError(f"agent-device {args[0]} failed: the simulator is in use by session {claimed!r}",
                                 in_use_fix(claimed, " ".join(shlex.quote(a) for a in (ad or self.ad))))
            err = data.get("error") if isinstance(data, dict) else None
            msg = (err.get("message") if isinstance(err, dict) else err) or (proc.stderr or out).strip()[:800]
            hint = err.get("hint") if isinstance(err, dict) else None
            raise IosimError(f"agent-device {args[0]} failed: {msg}", hint)
        return data.get("data", data) if isinstance(data, dict) else out

    def _opened(self, data):
        check_bound_device(data, self.udid)
        self.session_opened = True
        self.session_name = data.get("session") if isinstance(data, dict) else None
        write_state(f"agent-device-{self.udid}.json", {"command": self.ad, "session": self.session_name, "at": time.time()})

    def precheck(self):
        """For a given command, learn its simulator without a device action when it says (its --udid, or its --session
        in `session list`), so a mismatch is refused before Safari opens on the other simulator."""
        if self.mode != "agent-device (given)" or self.prechecked:
            return
        sessions = None
        if not flag_value(self.ad, "--udid") and flag_value(self.ad, "--session"):
            try:
                sessions = self.ad_run("session", "list", timeout=60)
            except IosimError:
                sessions = None
        precheck_bound_device(self.ad, self.udid, sessions)
        self.prechecked = True

    def ensure_session(self):
        """agent-device input, snapshots and recordings need an app session: open Safari once per process. Its answer
        names the simulator, which must be the one iosim resolved."""
        if self.ad and not self.session_opened:
            self.precheck()
            self._opened(self.ad_run("open", SAFARI))

    def open_url(self, url):
        if self.ad:
            self.precheck()
            self._opened(self.ad_run("open", SAFARI, url))
        else:
            simctl("openurl", self.udid, url)

    def close(self):
        """Close this command's own agent-device session (never another one). Returns what happened."""
        if not self.ad:
            return {"closed": False, "why": "no agent-device in use"}
        try:
            self.ad_run("close", timeout=60)
            result = {"closed": True}
        except IosimError as e:
            result = {"closed": False, "why": str(e)}
        write_state(f"agent-device-{self.udid}.json", None)
        return result

    def tap(self, x, y):
        self.require_ad("tap")
        self.ensure_session()
        return self.ad_run("press", str(x), str(y))

    def tap_label(self, label, contains=False):
        """Exact accessibility label through agent-device; with contains, a case-insensitive substring match on a
        fresh snapshot, which must name one element."""
        self.require_ad("tap")
        self.ensure_session()
        if not contains:
            return self.ad_run("press", f"label={json.dumps(label)}")
        snap = self.snapshot()
        node = pick_label_node((snap or {}).get("nodes", []) if isinstance(snap, dict) else [], label)
        r = node["rect"]
        x, y = round(r["x"] + r["width"] / 2, 1), round(r["y"] + r["height"] / 2, 1)
        self.ad_run("press", str(x), str(y))
        return {"matched": node.get("label"), "x": x, "y": y}

    def type_text(self, text):
        self.require_ad("type")
        self.ensure_session()
        return self.ad_run("type", text)

    def keyboard(self, action):
        self.require_ad("keyboard")
        self.ensure_session()
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
                if pid_is(pid, "simctl", "recordVideo"):
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
            self.ensure_session()
            self.ad_run("settings", "text-size", size)
        else:
            simctl("ui", self.udid, "content_size", size)
        return size

    def set_appearance(self, mode):
        if mode not in ("light", "dark"):
            raise IosimError("appearance is light or dark", code=2)
        if self.ad:
            self.ensure_session()
            self.ad_run("settings", "appearance", mode)
        else:
            simctl("ui", self.udid, "appearance", mode)
        return mode


# ---------------------------------------------------------------- proxy


def proxy_status(udid):
    st = read_state(f"proxy-{udid}.json")
    if not st:
        return None
    if not pid_is(st.get("pid"), "ios_webkit_debug_proxy"):
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
    if st and pid_is(st.get("pid"), "ios_webkit_debug_proxy"):
        os.kill(st["pid"], 15)
        for _ in range(30):
            if not pid_alive(st["pid"]):
                break
            time.sleep(0.1)
    write_state(f"proxy-{udid}.json", None)
    return bool(st)


def proxy_ensure(udid):
    """Start (or reuse) one ios_webkit_debug_proxy for this simulator. Its page list is on port+1. A per-simulator
    lock keeps two iosim commands from starting two proxies."""
    with FileLock(f"proxy-{udid}.lock"):
        return _proxy_ensure(udid)


def _proxy_ensure(udid):
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


# Measures a tap target. Runs as a function body after a prelude that defines __sel and __first.
TAP_RECT_JS = r"""
const describe = (n) => {
  if (!(n instanceof Element)) return String(n);
  let d = n.tagName.toLowerCase();
  if (n.id) return d + "#" + n.id;
  const cls = [...n.classList].slice(0, 2).join(".");
  if (cls) d += "." + cls;
  const text = (n.getAttribute("aria-label") || n.innerText || "").trim().replace(/\s+/g, " ").slice(0, 40);
  return text ? d + ' "' + text + '"' : d;
};
const all = document.querySelectorAll(__sel);
if (!all.length) throw new Error("no element matches " + __sel);
if (document.visibilityState !== "visible") return {hidden: true, target: describe(all[0])};
if (all.length > 1 && !__first) return {ambiguous: all.length, matches: [...all].slice(0, 5).map(describe)};
const el = all[0];
const inView = (r) => r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth;
let r = el.getBoundingClientRect();
if (r.width === 0 || r.height === 0) return {invisible: true, target: describe(el)};
if (!inView(r)) {
  el.scrollIntoView({block: "center", inline: "center"});
  await new Promise((res) => requestAnimationFrame(() => requestAnimationFrame(res)));
  await new Promise((res) => setTimeout(res, 150));
  r = el.getBoundingClientRect();
}
const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
const vv = window.visualViewport;
const scale = vv ? vv.scale : 1, ox = vv ? vv.offsetLeft : 0, oy = vv ? vv.offsetTop : 0;
const visW = vv ? vv.width : innerWidth, visH = vv ? vv.height : innerHeight;
if (cx - ox < 0 || cy - oy < 0 || cx - ox > visW || cy - oy > visH) {
  return {offscreen: true, target: describe(el), cssPoint: {x: cx, y: cy}, visual: {width: visW, height: visH, offsetLeft: ox, offsetTop: oy}};
}
const hit = document.elementFromPoint(cx, cy);
const toVisual = (b) => ({x: (b.x - ox) * scale, y: (b.y - oy) * scale, width: b.width * scale, height: b.height * scale});
const nameOf = (n) => {
  const label = n.getAttribute("aria-label") || (n.labels && n.labels[0] && n.labels[0].innerText) || n.getAttribute("placeholder")
    || n.getAttribute("alt") || n.getAttribute("title") || n.innerText || "";
  return label.trim().replace(/\s+/g, " ").slice(0, 160);
};
// Anchors: the target and other fully visible elements with a name, so the screen offset can be read from
// whichever of them the accessibility snapshot shows.
const anchors = [];
const seen = new Set();
const push = (n, target) => {
  if (seen.has(n)) return;
  seen.add(n);
  const b = n.getBoundingClientRect();
  const name = nameOf(n);
  if (!name || b.width === 0 || b.height === 0 || !inView(b)) return;
  anchors.push(Object.assign({id: anchors.length, label: name, target}, toVisual(b)));
};
const CANDIDATES = "button, a[href], input, select, textarea, h1, h2, h3, h4, h5, h6, p, label, li, [role=button], [aria-label]";
push(el, true);
const candidates = [...document.querySelectorAll(CANDIDATES)];
for (const n of candidates) {
  if (anchors.length >= 30) break;
  push(n, false);
}
// How many page elements carry each anchor's name (a wrapper of an element with the same name counts once).
// Unknown (null) on a very large page.
if (candidates.length <= 3000) {
  const wanted = new Set(anchors.map((a) => a.label));
  const byName = new Map();
  for (const n of candidates) {
    const name = nameOf(n);
    if (!wanted.has(name)) continue;
    if (!byName.has(name)) byName.set(name, []);
    byName.get(name).push(n);
  }
  for (const a of anchors) {
    const list = byName.get(a.label) || [];
    a.pageCount = list.filter((n) => !list.some((m) => m !== n && n.contains(m))).length;
  }
} else {
  for (const a of anchors) a.pageCount = null;
}
return {rect: toVisual(r), cssPoint: {x: cx, y: cy}, scale, visual: {width: visW * scale, height: visH * scale},
        hit: hit && (hit === el || el.contains(hit)) ? null : (hit ? describe(hit) : "nothing (outside the page)"),
        title: document.title, tag: el.tagName.toLowerCase(), target: describe(el), count: all.length, anchors};
"""

FOCUS_JS = r"""
const el = document.querySelector(__sel);
const a = document.activeElement;
const describe = (n) => !n ? "nothing" : n.tagName.toLowerCase() + (n.id ? "#" + n.id : "") + (n.getAttribute && n.getAttribute("name") ? "[name=" + n.getAttribute("name") + "]" : "");
return {focused: !!el && !!a && (a === el || el.contains(a)), active: describe(a)};
"""


# Arms a one-shot listener for the next touch, pointer, mouse or click event anywhere in the document, before a real
# tap. It records the event's target and whether it is (inside) the expected element, keeps the record in
# window.__iosimTap and logs it, so it reaches iosim even when the tap navigates away. Runs as a function body after
# a prelude that defines __tok and __sel (a selector or null).
TAP_ARM_JS = r"""
const describe = (n) => {
  if (!(n instanceof Element)) return n && n.nodeName ? n.nodeName.toLowerCase() : String(n);
  let d = n.tagName.toLowerCase();
  if (n.id) return d + "#" + n.id;
  const cls = [...n.classList].slice(0, 2).join(".");
  if (cls) d += "." + cls;
  const text = (n.getAttribute("aria-label") || n.innerText || "").trim().replace(/\s+/g, " ").slice(0, 40);
  return text ? d + ' "' + text + '"' : d;
};
let el = null;
if (__sel !== null) {
  el = document.querySelector(__sel);
  if (!el) throw new Error("no element matches " + __sel);
}
const log = console.debug.bind(console);
const types = ["touchstart", "pointerdown", "mousedown", "click"];
window.__iosimTap = null;
const off = () => types.forEach((t) => window.removeEventListener(t, handler, true));
function handler(ev) {
  if (!ev.isTrusted) return;  // a click the page's own script made is not the touch
  if (window.__iosimTap && window.__iosimTap.token === __tok) return;
  const path = ev.composedPath ? ev.composedPath() : [];
  const t = ev.target;
  const touch = ev.touches && ev.touches[0];
  const rec = {token: __tok, type: ev.type, target: describe(t),
               inside: el ? (path.includes(el) || (t instanceof Node && el.contains(t))) : null,
               x: touch ? touch.clientX : (typeof ev.clientX === "number" ? ev.clientX : null),
               y: touch ? touch.clientY : (typeof ev.clientY === "number" ? ev.clientY : null)};
  window.__iosimTap = rec;
  off();
  try { log("__iosimTap " + JSON.stringify(rec)); } catch (e) {}
}
types.forEach((t) => window.addEventListener(t, handler, {capture: true, passive: true}));
setTimeout(off, 20000);
return {armed: true, target: el ? describe(el) : null, visible: document.visibilityState === "visible"};
"""

TAP_MARK = "__iosimTap "


def tap_point(info, nodes):
    """Screen point for a measured target (TAP_RECT_JS's answer) and an accessibility snapshot. Raises when the
    target is covered or off screen, or when the page cannot be placed on screen without guessing."""
    found = web_origin_from_anchors(nodes, info.get("anchors") or [])
    title_origin = web_origin_from_title(nodes, info.get("title"))
    if found and found.get("refused"):
        raise IosimError("could not place Safari's web area on screen: " + found["refused"],
                         "tap --label <the element's exact accessible name>, give the element a unique accessible name, "
                         "or measure it with eval and tap --at <x> <y> (screen points)")
    if found:
        origin, how = found["origin"], {"originVia": "anchors", "anchors": found["anchors"], "uniqueAnchors": found["unique"],
                                        "targetAnchored": found["target"]}
        if title_origin is not None:
            if abs(title_origin[0] - origin[0]) > 2 or abs(title_origin[1] - origin[1]) > 2:
                raise IosimError(f"could not place Safari's web area on screen: the anchors put it at {list(origin)} but the web area "
                                 f"node says {list(title_origin)}",
                                 "tap --label <accessible name>, or measure the element with eval and tap --at <x> <y>")
            how["webAreaAgrees"] = True
    else:
        origin = title_origin
        how = {"originVia": "title node"}
        if origin is None:
            raise IosimError("could not place Safari's web area on screen: no page element matched the accessibility snapshot",
                             "measure the element with eval and tap with --at <x> <y> (screen points), or tap --label <accessible name>")
    x, y = contents_to_points(info["rect"], origin)
    visual = info.get("visual") or {}
    if visual and not (origin[0] <= x <= origin[0] + visual["width"] and origin[1] <= y <= origin[1] + visual["height"]):
        raise IosimError(f"the target's centre ({x}, {y}) falls outside the page's visible area on screen",
                         "scroll it into view (scroll --to) and retry, or tap --at")
    return x, y, list(origin), how


class Session:
    def __init__(self, device=None, agent_device=None):
        self.device = booted_device(device)
        self.udid = self.device["udid"]
        self.driver = Driver(self.udid, agent_device)

    # pages -------------------------------------------------------
    def page_state(self):
        return read_state(f"page-{self.udid}.json") or {}

    def remember_page(self, page, url):
        """Record the page iosim drives by identity (see page_key); every later command addresses it, not a URL."""
        write_state(f"page-{self.udid}.json",
                    dict(page_key(page), url=url, at=time.time(), proxy=proxy_id(proxy_status(self.udid))))

    def proxy_id(self):
        """The running proxy's identity; call after list_pages, which starts the proxy when needed."""
        return proxy_id(proxy_status(self.udid)) or {}

    def page(self):
        pages = list_pages(self.udid)
        return driven_page(pages, self.page_state(), self.proxy_id())

    def connect(self, timeout=10.0):
        return Inspector(self.page()["webSocketDebuggerUrl"], timeout=timeout)

    def with_page(self, fn, retries=3, idempotent=False):
        """Run fn on a fresh connection to the driven page. A failed connect is retried, except when the page is gone.
        A connection lost after fn sent a command is retried only when fn is idempotent (a read): an eval with side
        effects must not run twice."""
        last = None
        for attempt in range(retries):
            try:
                insp = self.connect()
            except IosimError as e:
                if "is gone" in str(e) or "has not opened one" in str(e) or "no inspectable page" in str(e):
                    raise
                last = e
                time.sleep(0.5)
                continue
            except OSError as e:
                last = e
                time.sleep(0.5)
                continue
            try:
                return fn(insp)
            except (ConnectionError, OSError) as e:
                last = e
                if insp.sent and not idempotent:
                    raise IosimError(f"lost the page connection after sending the command, so it may or may not have run: {e}",
                                     "check the page's state, then retry; if it repeats run `iosim proxy restart` and then `iosim open <url>` again")
                time.sleep(0.5)
            finally:
                insp.close()
        if isinstance(last, IosimError):
            raise last
        raise IosimError(f"lost the page connection: {last}", "retry; if it repeats run `iosim proxy restart` and then `iosim open <url>` again")

    def evaluate(self, source, is_body=False, timeout=30.0, prelude="", idempotent=False):
        return self.with_page(lambda i: i.evaluate(source, is_body=is_body, timeout=timeout, prelude=prelude), idempotent=idempotent)

    def _probe(self, page, extra=""):
        insp = Inspector(page["webSocketDebuggerUrl"], timeout=5)
        try:
            return insp.evaluate("({href: location.href, readyState: document.readyState, nav: window.__iosimNav || null,"
                                 " mark: window.__iosimMark || null, title: document.title,"
                                 " visible: document.visibilityState === 'visible', t0: performance.timeOrigin" + extra + "})", timeout=5)
        finally:
            insp.close()

    def _mark_pages(self, pages, token):
        """Tag every open document, so a page that answers without the tag is one that loaded after this point."""
        marked = set()
        for p in pages:
            try:
                insp = Inspector(p["webSocketDebuggerUrl"], timeout=5)
                try:
                    insp.evaluate(f"(window.__iosimMark = {json.dumps(token)}, true)", timeout=5)
                    marked.add(p["webSocketDebuggerUrl"])
                finally:
                    insp.close()
            except (IosimError, OSError, ConnectionError, ValueError):
                pass
        return marked

    def open(self, url, new_tab=False, wait=True, timeout=45.0):
        """Navigate the driven page (same tab), or open the URL through the device in a new tab when asked, when Safari
        has no page, or when iosim cannot name the page it drives. Only the navigated page is watched for the load."""
        started = time.time()
        token = f"iosim-{time.time_ns()}"
        pages = list_pages(self.udid)
        before = {p["webSocketDebuggerUrl"] for p in pages}
        blank = url == "about:blank"
        driven = None
        if pages and not new_tab:
            try:
                driven = driven_page(pages, self.page_state(), self.proxy_id())
            except IosimError as e:
                if blank:
                    raise IosimError(f"about:blank blanks the page iosim drives, and there is none: {e}", e.fix, code=2)
        if blank and (new_tab or not driven):
            raise IosimError("about:blank can only replace the page in the current tab, and Safari has no page open (or --new-tab was given)",
                             "open an http(s) URL first, then `iosim open about:blank` to blank that tab", code=2)
        how = "new-tab"
        marked = set()
        if driven:
            insp = Inspector(driven["webSocketDebuggerUrl"], timeout=10)
            try:
                insp.evaluate(f"(window.__iosimNav = {json.dumps(token)}, setTimeout(() => location.assign({json.dumps(url)}), 0), true)")
            finally:
                insp.close()
            how = "same-tab"
            self.remember_page(driven, url)
        else:
            write_state(f"page-{self.udid}.json", None)
            marked = self._mark_pages(pages, token)
            self.driver.open_url(url)
        result = {"url": url, "how": how, "driver": self.driver.mode}
        if wait:
            result.update(self.wait_loaded(url, token, how, before, marked, timeout, driven=driven, started=started))
        elif how == "new-tab":
            result.update(self._identify_new_tab(before))
        result["ms"] = int((time.time() - started) * 1000)
        return result

    def _identify_new_tab(self, before, seconds=5.0):
        """For open --new-tab --no-wait: record the one page that appeared, if exactly one did."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            fresh = [p for p in list_pages(self.udid) if p["webSocketDebuggerUrl"] not in before]
            if len(fresh) == 1:
                try:
                    on_screen = self._probe(fresh[0]).get("visible") is True
                except (IosimError, OSError, ConnectionError, ValueError):
                    on_screen = False
                if on_screen:  # a background tab the proxy listed late is not the tab this open made
                    self.remember_page(fresh[0], fresh[0].get("url"))
                    return {"page": page_key(fresh[0])["page"]}
            time.sleep(0.3)
        return {"page": None, "warning": "could not tell which tab opened; later commands refuse until `iosim open` names one"}

    def wait_loaded(self, url, token, how, before, marked, timeout, driven=None, started=None):
        """Wait for the document the open started: in the same tab, the navigated page (by identity) once it holds a
        document without our navigation tag; in a new tab, a page that did not exist before, or an old one that has
        since loaded a new document (no mark). Fails at once when Safari shows its own error page for an http(s) URL,
        and when the navigated page goes away."""
        deadline = time.time() + timeout
        last = None
        fresh_seen = False
        gone_since = None
        while time.time() < deadline:
            time.sleep(0.3)
            try:
                pages = list_pages(self.udid)
                if how == "same-tab":
                    page = find_page(pages, page_key(driven))
                    if page is None:
                        # A proxy blip can drop the list for a moment; a page missing for 3 s is gone.
                        gone_since = gone_since or time.time()
                        if time.time() - gone_since > 3:
                            raise IosimError(f"the tab iosim navigated to {url} went away (closed, or Safari restarted)",
                                             "run `iosim open <url>` again")
                        last = "the navigated page is not listed"
                        continue
                    gone_since = None
                    candidates = [page]
                else:
                    candidates = [p for p in pages if p["webSocketDebuggerUrl"] not in before or p["webSocketDebuggerUrl"] in marked]
                    candidates.sort(key=lambda p: not url_matches(p.get("url"), url))
                for page in candidates:
                    info = self._probe(page)
                    last = info
                    if how == "same-tab" and info["nav"] == token:
                        continue  # still the old document
                    if how != "same-tab" and info["mark"] == token:
                        continue  # a document that was already open
                    if how != "same-tab" and info.get("visible") is not True:
                        # The tab an open makes, or the tab Safari switches to because it already shows the URL, is
                        # the one on screen. A background tab that reloaded by itself is not it, and neither is one
                        # a just-started proxy had not listed yet, which is why `before` alone cannot tell.
                        last = dict(info, skipped="a background tab")
                        continue
                    if how != "same-tab" and stale_document(info.get("t0"), started):
                        # The front tab as it was before this open (unmarked because the proxy listed it late).
                        last = dict(info, skipped="a document loaded before this open")
                        continue
                    fresh_seen = True
                    if info["readyState"] != "complete":
                        continue
                    if info["href"] == "about:blank" and url != "about:blank":
                        continue
                    failure = load_failure(url, info["href"])
                    if failure:
                        raise IosimError(failure, f"check that the server is up and reachable from this Mac (curl -sI {url}), then open it again")
                    self.remember_page(page, info["href"])
                    out = {"finalUrl": info["href"], "title": info["title"], "redirected": not url_matches(info["href"], url),
                           "page": page_key(page)["page"], "visible": info.get("visible")}
                    if info.get("visible") is False:
                        out["warning"] = "the page loaded in a tab that is not on screen: screenshots show another tab and taps refuse"
                    return out
            except IosimError as e:
                if str(e).startswith(("Safari could not load", "the tab iosim navigated")):
                    raise
                last = str(e)
            except (OSError, ConnectionError, ValueError) as e:
                last = str(e)
        if how != "same-tab" and not fresh_seen:
            raise IosimError(f"Safari showed no newly loaded page for {url} within {timeout:.0f}s (last seen: {last})",
                             "Safari may have switched to a tab that already shows this URL instead of opening one: "
                             "open without --new-tab, or open a different URL")
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
                if str(m.get("text") or "").startswith(TAP_MARK):
                    continue  # iosim's own tap record
                out.append({"level": m.get("level"), "source": m.get("source"), "text": m.get("text"),
                            "url": m.get("url"), "line": m.get("line"), "column": m.get("column")})
            return out
        return self.with_page(read, idempotent=True)

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
        last_error = None
        while True:
            try:
                if self.evaluate(expr, timeout=5, idempotent=True):
                    return {"waitedMs": int((time.time() - start) * 1000)}
                last_error = None
            except PageSyntaxError as e:
                # It will never parse: say so now rather than at the timeout.
                raise IosimError(f"wait: the condition does not parse: {e}", "it must be one JavaScript expression")
            except PageError as e:
                last_error = e  # it may stop throwing once the page has set up (window.app before app exists)
            except IosimError as e:
                if "is gone" in str(e) or "has not opened one" in str(e):
                    raise
            if time.time() - start > timeout:
                if last_error is not None:
                    raise IosimError(f"wait timed out after {timeout:.0f}s for {selector or text or js}; the condition kept throwing: {last_error}",
                                     "guard what may not exist yet with ?. (window.app?.ready), or fix the expression")
                raise IosimError(f"wait timed out after {timeout:.0f}s for {selector or text or js}")
            time.sleep(0.25)

    # input -------------------------------------------------------
    def tap(self, selector=None, label=None, at=None, first=False, contains=False, expect=None):
        """A real tap. With a selector (or `expect`, a selector for --label and --at), a one-shot listener in the page
        records what the touch reached, and the tap fails unless that element (or one inside it) got it."""
        if at is not None:
            x, y = at
            out = {"x": x, "y": y, "via": "coordinates"}
            out.update(self._verified(lambda: self.driver.tap(x, y), expect, required=expect is not None))
            return out
        if label is not None:
            detail = {}

            def do():
                detail["d"] = self.driver.tap_label(label, contains=contains)
            check = self._verified(do, expect, required=expect is not None)
            out = {"label": label, "via": "accessibility label" + (" (contains)" if contains else "")}
            if contains and isinstance(detail.get("d"), dict):
                out.update(detail["d"])
            out.update(check)
            return out
        self.driver.require_ad("tap")
        prelude = f"const __sel = {json.dumps(selector)}, __first = {json.dumps(bool(first))};"
        info = self.evaluate(TAP_RECT_JS, is_body=True, prelude=prelude, idempotent=True)
        if info.get("ambiguous"):
            raise IosimError(f"{info['ambiguous']} elements match {selector!r}: {', '.join(info['matches'])}",
                             "make the selector match one element, or pass --first (plan: \"first\": true) to tap the first")
        if info.get("hidden"):
            raise IosimError(f"the page iosim drives is not the tab on screen, so a tap cannot reach {info['target']}",
                             "open the page in the visible tab (iosim open <url> --new-tab), or close Safari's other tabs")
        if info.get("invisible"):
            raise IosimError(f"{selector!r} matches {info['target']}, which has no size (hidden?)", "wait for it to show, or tap another element")
        if info.get("offscreen"):
            raise IosimError(f"{selector!r} ({info['target']}) is still off screen after scrolling it into view",
                             "scroll the page (or its container) so the element shows, then retry")
        if info.get("hit"):
            raise IosimError(f"{selector!r} ({info['target']}) is covered at its centre by {info['hit']}: a real tap would hit that instead",
                             "close or move what covers it first; if the cover is meant to take the tap, tap that element")
        snap = self.driver.snapshot()
        nodes = snap.get("nodes", []) if isinstance(snap, dict) else []
        x, y, origin, how = tap_point(info, nodes)
        check = self._verified(lambda: self.driver.tap(x, y), selector, required=True)
        return {"selector": selector, "target": info["target"], "x": x, "y": y, "rect": info["rect"], "origin": origin, "via": "selector",
                **how, **check}

    def _verified(self, do_tap, selector, required, wait=2.0):
        """Arm TAP_ARM_JS in the driven page, run do_tap, and judge what the touch reached (verify_tap). Without a
        selector the result only reports what the page saw. When `required` is false and the page cannot be armed
        (no page, say), the tap still happens and the result says it was not checked."""
        token = f"tap-{time.time_ns()}"
        try:
            insp = self.connect()
        except (IosimError, OSError) as e:
            if required:
                raise
            do_tap()
            return {"pageHit": None, "checked": False, "why": str(e)}
        try:
            try:
                insp.call("Console.enable")
                armed = insp.evaluate(TAP_ARM_JS, is_body=True, prelude=f"const __tok = {json.dumps(token)}, __sel = {json.dumps(selector)};")
            except (IosimError, OSError, ConnectionError) as e:
                if required:
                    raise IosimError(f"could not prepare the tap check in the page: {e}", getattr(e, "fix", None))
                do_tap()
                return {"pageHit": None, "checked": False, "why": str(e)}
            if required and not armed.get("visible"):
                raise IosimError(f"the page iosim drives is not the tab on screen, so a tap cannot reach {armed.get('target') or selector}",
                                 "open the page in the visible tab (iosim open <url> --new-tab), or close Safari's other tabs")
            do_tap()
            record = self._tap_record(insp, token, wait)
        finally:
            insp.close()
        return verify_tap(record, armed.get("target") if selector else None)

    @staticmethod
    def _tap_record(insp, token, wait):
        """The tap record: from the console message (survives a navigation the tap starts), else window.__iosimTap."""
        deadline = time.time() + wait
        while True:
            for ev in insp.events:
                if ev.get("method") != "Console.messageAdded":
                    continue
                text = str(ev["params"]["message"].get("text") or "")
                if text.startswith(TAP_MARK):
                    try:
                        rec = json.loads(text[len(TAP_MARK):])
                    except ValueError:
                        continue
                    if rec.get("token") == token:
                        return rec
            left = deadline - time.time()
            if left <= 0:
                break
            try:
                insp.drain(min(left, 0.25))
            except (ConnectionError, OSError, ValueError):
                break
        try:
            rec = insp.evaluate("window.__iosimTap || null", timeout=5)
        except (IosimError, ConnectionError, OSError, ValueError):
            return None
        return rec if isinstance(rec, dict) and rec.get("token") == token else None

    def type_text(self, text, selector=None, enter=False, keep_keyboard=False, first=False):
        out = {}
        if selector:
            out["tap"] = self.tap(selector=selector, first=first)
            time.sleep(0.4)
            focus = self.evaluate(FOCUS_JS, is_body=True, prelude=f"const __sel = {json.dumps(selector)};", idempotent=True)
            if not focus.get("focused"):
                raise IosimError(f"tapping {selector!r} did not focus it (focus is on {focus.get('active')}); nothing was typed",
                                 "check the selector names the input itself, and that nothing covers it")
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
                self.evaluate("(document.activeElement && document.activeElement.blur(), true)", idempotent=True)
                out["keyboard"] = "blurred"
            time.sleep(0.4)
        return out

    def scroll(self, direction=None, amount=None, to=None):
        if to:
            self.evaluate(f"(document.querySelector({json.dumps(to)}) || (() => {{ throw new Error('no element matches ' + {json.dumps(to)}) }})()).scrollIntoView({{block: 'center'}})", idempotent=True)
            return {"to": to, "via": "scrollIntoView (programmatic, not a gesture)"}
        self.driver.scroll(direction or "down", amount)
        return {"direction": direction or "down", "amount": amount, "via": "agent-device gesture"}

    def audit(self, script=None, options=None):
        """The built-in audit (a function body), or a project's script: an expression (for example an async IIFE, as
        written for page.evaluate) or a function body that uses `return`, giving {findings: [...]} or an array."""
        options = options or {}
        if script:
            source = Path(script).read_text() if not isinstance(script, dict) else script["source"]
            mode = "auto"
        else:
            source, mode = AUDIT_JS.read_text(), True
        result = self.evaluate(source, is_body=mode, timeout=120, prelude=f"const __opts = {json.dumps(options)};")
        return normalize_audit(result)


AUDIT_SHAPES = "an array of findings, or an object with a `findings` array ({findings: [...]}); [] means the page passed"
SEVERITIES = {"fail": "fail", "error": "fail", "warn": "warn", "warning": "warn"}


def normalize_audit(result):
    """Accept a bare list of findings or an object with a `findings` list. Anything else (axe's {violations}, a
    count, nothing) is an error: a shape iosim does not read must not pass as an empty audit."""
    if isinstance(result, list):
        result = {"findings": result}
    if not isinstance(result, dict) or not isinstance(result.get("findings"), list):
        got = json.dumps(result)[:200]
        keys = f" (keys: {', '.join(sorted(result)[:8])})" if isinstance(result, dict) else ""
        raise IosimError(f"an audit script must give {AUDIT_SHAPES}; got {got}{keys}",
                         "end the script with an expression (or `return`) that gives the findings array, or wrap it: {findings: [...]}")
    findings = []
    for f in result["findings"]:
        if not isinstance(f, dict):
            f = {"message": str(f)}
        f = dict(f)
        raw = f.get("severity", "fail")
        severity = SEVERITIES.get(str(raw).lower()) if raw is not None else "fail"
        if severity is None:
            raise IosimError(f"an audit finding has severity {raw!r}; iosim reads fail (or error) and warn (or warning)",
                             "give each finding severity \"fail\" or \"warn\" (no severity counts as fail)")
        f["severity"] = severity
        f.setdefault("rule", "custom")
        # Scripts written for other runners name things differently: {rule, element, detail} is common.
        if "message" not in f and "detail" in f:
            f["message"] = str(f["detail"])
        if "selector" not in f and "element" in f:
            f["selector"] = f["element"]
        findings.append(f)
    result = dict(result, findings=findings)
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
        need(isinstance(v, dict) and isinstance(v.get("url"), str) and (re.match(r"^[a-z]+://", v["url"] or "") or v["url"] == "about:blank"),
             "needs a URL with a scheme, like \"http://host:3000/\" (or about:blank)")
        step.update(url=v["url"], newTab=bool(v.get("newTab", False)))
    elif kind == "eval":
        if isinstance(v, str):
            v = {"js": v}
        need(isinstance(v, dict) and (isinstance(v.get("js"), str) or isinstance(v.get("file"), str)), "needs an expression string, {\"js\": ...} or {\"file\": ...}")
        if "file" in v:
            step.update(source=_read_source(v["file"], base_dir, where), body="auto", file=v["file"])
        else:
            step.update(source=v["js"], body=bool(v.get("body", False)))
    elif kind == "tap":
        if isinstance(v, str):
            v = {"selector": v}
        need(isinstance(v, dict) and sum(k in v for k in ("selector", "label", "at")) == 1, "needs one of selector, label or at")
        if "at" in v:
            need(isinstance(v["at"], list) and len(v["at"]) == 2 and all(isinstance(n, (int, float)) for n in v["at"]), "at is [x, y] in screen points")
        step.update({k: v[k] for k in ("selector", "label", "at") if k in v})
        if v.get("first"):
            need("selector" in v, "first goes with selector")
            step["first"] = True
        if v.get("contains"):
            need("label" in v, "contains goes with label")
            step["contains"] = True
        if "expect" in v:
            need("selector" not in v and isinstance(v["expect"], str), "expect is a selector, and goes with label or at")
            step["expect"] = v["expect"]
    elif kind == "type":
        if isinstance(v, str):
            v = {"text": v}
        need(isinstance(v, dict) and isinstance(v.get("text"), str), "needs text")
        step.update(text=v["text"], selector=v.get("selector"), enter=bool(v.get("enter", False)), keepKeyboard=bool(v.get("keepKeyboard", False)))
        if v.get("first"):
            step["first"] = True
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


def meaningful_console_errors(messages):
    """Console errors that say something about the page: a missing /favicon.ico is noise."""
    return [m for m in messages or [] if not re.search(r"favicon\.ico", f"{m.get('url') or ''} {m.get('text') or ''}")]


def _raise_terminated(signum, frame):
    raise Terminated(f"signal {signum}")


def write_placeholder_report(out_dir, why="the run started but has not finished"):
    """Replace report.json with one that says the run did not pass, before anything that can fail: a run that dies
    early must never leave an earlier passing report in its folder."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = {"passed": False, "placeholder": True, "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "failures": [{"step": None, "kind": "incomplete", "message": why}]}
    (out / "report.json").write_text(json.dumps(report, indent=2))
    return out / "report.json"


def write_setup_failure(out_dir, error):
    """A run that failed before its first step (bad plan, no device): say why in report.json."""
    try:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        report = {"passed": False, "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                  "failures": [{"step": None, "kind": "setup", "message": str(error), **({"detail": {"fix": error.fix}} if getattr(error, "fix", None) else {})}]}
        (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    except OSError:
        pass


def run_plan(plan, out_dir, device=None, agent_device=None, log=None, session=None):
    """Run a parsed plan. `passed` starts false and turns true only when every step ran to the end with no failure.
    An interruption (Ctrl-C, SIGTERM) or a crash is recorded as a failure; the Text Size and appearance are restored
    and report.json is written whatever happened, then the interruption propagates."""
    out = Path(out_dir).resolve()
    write_placeholder_report(out)
    in_main = True
    try:
        old_term = signal.signal(signal.SIGTERM, _raise_terminated)
    except ValueError:  # not the main thread (tests)
        in_main, old_term = False, None
    try:
        return _run_plan(plan, out, device, agent_device, log, session, in_main)
    finally:
        if in_main:
            signal.signal(signal.SIGTERM, old_term)


def _run_plan(plan, out, device, agent_device, log, session, in_main):
    try:
        session = session or Session(device or plan.get("device"), agent_device)
        udid = session.udid
        original = {"textSize": text_size_get(udid), "appearance": appearance_get(udid)}
    except IosimError as e:
        write_setup_failure(out, e)
        raise
    drv = session.driver
    started = time.time()
    report = {
        "plan": plan["name"], "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "out": str(out),
        "device": {k: session.device[k] for k in ("name", "udid", "runtime")}, "driver": drv.describe(),
        "steps": [], "shots": [], "audits": [], "console": [], "values": {}, "failures": [], "passed": False,
    }
    report["initial"] = dict(original)
    current = {"textSize": original["textSize"]}
    shot_no = [0]
    completed = False
    running = {"step": None}

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
            running["step"] = entry["step"]
            say(f"step {entry['step']}: {step['kind']}")
            stop = False
            try:
                result = run_step(session, step, out, report, current, shot_no, plan, fail)
                entry["ok"] = True
                if result is not None:
                    entry["result"] = result
            except IosimError as e:
                entry["ok"] = False
                entry["error"] = str(e)
                fail(step, str(e), {"fix": e.fix} if e.fix else None)
                stop = not step.get("optional")
            except Exception as e:  # a bug or an unexpected file: the step failed, and so does the run
                entry["ok"] = False
                entry["error"] = f"{type(e).__name__}: {e}"
                report["failures"].append({"step": entry["step"], "kind": "crash", "message": entry["error"]})
                stop = True
            entry["ms"] = int((time.time() - t0) * 1000)
            report["steps"].append(entry)
            if stop:
                report["stoppedAt"] = entry["step"]
                break
        running["step"] = None
        # Errors since the last load, whatever the steps did.
        try:
            report["console"] = session.console(errors_only=True)
        except IosimError as e:
            report["consoleError"] = str(e)
        if plan.get("failOnConsoleErrors") and meaningful_console_errors(report["console"]):
            report["failures"].append({"step": None, "kind": "console", "message": f"{len(meaningful_console_errors(report['console']))} console error(s)"})
        completed = "stoppedAt" not in report
    except BaseException as e:
        kind = "interrupted" if isinstance(e, (KeyboardInterrupt, Terminated)) else "crash"
        why = {KeyboardInterrupt: "interrupted (Ctrl-C)", Terminated: "terminated (SIGTERM)"}.get(type(e), f"{type(e).__name__}: {e}")
        report["failures"].append({"step": running["step"], "kind": kind, "message": f"the run was {why}" if kind == "interrupted" else why})
        report["interrupted" if kind == "interrupted" else "crashed"] = why
        raise
    finally:
        # Settle the device first (stop the recording, put settings back); a second Ctrl-C or SIGTERM must not cut
        # that short. The contact sheet and the report come after.
        if in_main:
            old_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            if video_path:
                try:
                    report["video"] = drv.record_stop()
                except Exception as e:
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
                except Exception as e:
                    restored["error"] = str(e)
                    report["failures"].append({"step": None, "kind": "restore", "message": f"could not restore the device: {e}"})
                report["restored"] = restored
        finally:
            if in_main:
                signal.signal(signal.SIGINT, old_int)
                signal.signal(signal.SIGTERM, _raise_terminated)
        report["seconds"] = round(time.time() - started, 1)
        report["passed"] = completed and not [f for f in report["failures"] if not f.get("optional")]
        report_path = out / "report.json"
        report_path.write_text(json.dumps(report, indent=2, default=str))
        # The verdict is on disk before the contact sheet, which can take a while; an interruption while it is built
        # still fails the run.
        if video_path and isinstance(report.get("video"), dict) and report["video"].get("exists") and "interrupted" not in report:
            try:
                report["contactSheet"] = contact_sheet(video_path, out / "contact-sheet.png", driver=drv)
            except (KeyboardInterrupt, Terminated) as e:
                why = "interrupted (Ctrl-C)" if isinstance(e, KeyboardInterrupt) else "terminated (SIGTERM)"
                report["contactSheet"] = {"error": f"the run was {why} while building the contact sheet"}
                report["failures"].append({"step": None, "kind": "interrupted", "message": f"the run was {why}"})
                report["interrupted"] = why
                report["passed"] = False
                report_path.write_text(json.dumps(report, indent=2, default=str))
                raise
            except Exception as e:
                report["contactSheet"] = {"error": str(e), "fix": getattr(e, "fix", None)}
            report["seconds"] = round(time.time() - started, 1)
            report_path.write_text(json.dumps(report, indent=2, default=str))
    return report


def driven_page_visible(session):
    """Whether the page iosim drives is the tab on screen; None when there is no page to ask."""
    try:
        return bool(session.evaluate("document.visibilityState === 'visible'", timeout=5, idempotent=True))
    except (IosimError, OSError, ConnectionError, ValueError):
        return None


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
        return session.tap(selector=step.get("selector"), label=step.get("label"), at=step.get("at"),
                           first=step.get("first", False), contains=step.get("contains", False), expect=step.get("expect"))
    if kind == "type":
        return session.type_text(step["text"], selector=step.get("selector"), enter=step["enter"], keep_keyboard=step["keepKeyboard"],
                                 first=step.get("first", False))
    if kind == "wait":
        return session.wait(selector=step.get("selector"), text=step.get("text"), js=step.get("js"), ms=step.get("ms"), timeout=timeout or 15)
    if kind == "shot":
        shot_no[0] += 1
        path = out / f"{shot_no[0]:02d}-{step['shotName']}.png"
        drv.screenshot(path)
        check = check_shot(path, plan.get("expectShot"))
        check["pageVisible"] = driven_page_visible(session)
        if check["pageVisible"] is False:
            check["problems"].append("the page iosim drives is not the tab on screen: this screenshot shows another tab")
            check["ok"] = False
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
            errors = meaningful_console_errors(session.console(errors_only=True))
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


def claims_from_devices(data):
    """{udid: session} for simulators an agent-device `devices --json` answer says are claimed."""
    devices = (data or {}).get("devices", []) if isinstance(data, dict) else []
    return {d.get("id"): (d.get("claimedBy") or {}).get("session") for d in devices if isinstance(d, dict) and d.get("claimedBy")}


def session_check(driver, claims):
    """(ok, detail, fix) for whether `driver` can act on its simulator: who claims it, and, for a given command, which
    simulator that command drives (checked by opening Safari through it, as every device action does first)."""
    udid = driver.udid
    claimed = claims.get(udid)
    if driver.mode == "agent-device (PATH)":
        own = f"iosim-{udid}"
        if claimed and claimed != own:
            return False, f"claimed by agent-device session {claimed!r}, not iosim's {own!r}", in_use_fix(claimed, driver.command_text())
        return True, f"free for session {own!r}" if not claimed else f"held by iosim's session {own!r}", None
    try:
        driver.ensure_session()
    except IosimError as e:
        return False, str(e), e.fix
    return True, f"the given command drives {udid} as session {driver.session_name!r}", None


def doctor(agent_device=None, device=None):
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
    if ad:
        target = None
        try:
            target = booted_device(device)
        except IosimError:
            pass
        if target:
            drv = Driver(target["udid"], given or "")
            claims = {}
            try:
                claims = claims_from_devices(drv.ad_run("devices", timeout=60, ad=shlex.split(given) if given else [ad]))
            except IosimError:
                pass
            ok, detail, fix = session_check(drv, claims)
            # Device actions fail while another session holds the simulator: required.
            add("agent-device session", ok, f"{target['name']}: {detail}", fix)
    node = which("node")
    nv = run_cmd([node, "--version"], check=False).stdout.strip() if node else None
    add("node", bool(node), nv or "missing (a standalone agent-device needs Node)", "install Node 22+", required=False)
    ff = which("ffmpeg")
    add("ffmpeg", bool(ff), ff or "missing: contact sheets fall back to agent-device's macOS builder", FFMPEG_INSTALL, required=False)
    return checks


# ---------------------------------------------------------------- remote (SSH)


def remote_iosim():
    return os.environ.get("IOSIM_REMOTE_IOSIM", "~/.agents/skills/ios-sim/scripts/iosim")


def split_global_flags(argv):
    """Pull iosim's global flags off a command line: (rest, device). --agent-device names a command on this box, so it
    is dropped when forwarding; --device travels as IOSIM_DEVICE."""
    rest, device, i = [], None, 0
    while i < len(argv):
        a = argv[i]
        if a in ("--device", "--agent-device") and i + 1 < len(argv) and not rest:
            if a == "--device":
                device = argv[i + 1]
            i += 2
            continue
        if (a.startswith("--device=") or a.startswith("--agent-device=")) and not rest:
            if a.startswith("--device="):
                device = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1
    return rest, device


def remote_call(args, stdin_text=None, device=None):
    host = os.environ.get("IOSIM_REMOTE")
    if not host:
        raise IosimError("IOSIM_REMOTE is not set", "export IOSIM_REMOTE=<ssh destination of the Mac with the Simulator>", code=2)
    ssh = which("ssh") or "ssh"
    forwarded = {k: v for k, v in os.environ.items() if k in ("IOSIM_DEVICE", "IOSIM_PROXY_PORT")}
    if device:
        forwarded["IOSIM_DEVICE"] = device
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in forwarded.items())
    command = f"{env} {remote_iosim()} {' '.join(shlex.quote(a) for a in args)}".strip()
    return subprocess.run([ssh, "-o", "BatchMode=yes", host, command], input=stdin_text, text=True, capture_output=True)


def ssh_could_not_connect(proc):
    """ssh's own failure to reach the host (refused, unknown host, no key), as against a remote command that died:
    both exit 255, so the message decides."""
    if proc.returncode != 255 or proc.stdout.strip():
        return False
    return bool(re.search(r"ssh: |Connection refused|Permission denied|Could not resolve|timed out|Host key verification failed",
                          proc.stderr or ""))


def remote_run(plan, out_dir, device=None):
    """Send a resolved plan over SSH, run it on the Mac, copy the evidence folder back, remove the remote copy."""
    run_id = f"iosim-runs/{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    payload = json.dumps(dict(plan, iosimResolvedPlan=1))
    proc = remote_call(["run", "-", "--out", run_id], stdin_text=payload, device=device)
    host = os.environ["IOSIM_REMOTE"]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    scp = subprocess.run([which("scp") or "scp", "-o", "BatchMode=yes", "-r", f"{host}:{run_id}/.", str(out)], capture_output=True, text=True)
    subprocess.run([which("ssh") or "ssh", "-o", "BatchMode=yes", host, f"rm -rf {shlex.quote(run_id)}"], capture_output=True)
    if scp.returncode != 0:
        remote_said = (proc.stdout.strip() or proc.stderr.strip())[-600:]
        raise IosimError(f"copying evidence back failed: {scp.stderr.strip()[:300]}; the remote run exited {proc.returncode}"
                         + (f" and said: {remote_said}" if remote_said else ""), "check scp works to the Mac, and read the remote error above")
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
    o.add_argument("url", help="http(s) URL; about:blank blanks the current tab")
    o.add_argument("--new-tab", action="store_true", help="open through the device instead of navigating the current tab")
    o.add_argument("--no-wait", action="store_true")
    o.add_argument("--timeout", type=float, default=45)

    sub.add_parser("pages", help="Safari pages the inspector sees")

    ev = sub.add_parser("eval", help="evaluate JavaScript in the page; awaits promises; prints the JSON value")
    ev.add_argument("expression", nargs="?")
    ev.add_argument("--file", help="file holding an expression or a function body that uses `return`")
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
    tg.add_argument("--selector", help="CSS selector of one element; fails when it matches several (see --first) or something covers it")
    tg.add_argument("--label", help="the element's exact accessibility label (as agent-device snapshot shows it); see --contains")
    tg.add_argument("--at", nargs=2, type=float, metavar=("X", "Y"), help="screen points")
    tp.add_argument("--first", action="store_true", help="with --selector: tap the first match when several match")
    tp.add_argument("--contains", action="store_true", help="with --label: match a label containing the text (case-insensitive); must be one element")
    tp.add_argument("--expect", metavar="SELECTOR", help="with --label or --at: fail unless this page element (or one inside it) got the tap")

    ty = sub.add_parser("type", help="type text with the keyboard (agent-device); --selector taps the field first")
    ty.add_argument("text")
    ty.add_argument("--selector", help="tap this field first and check it got focus")
    ty.add_argument("--first", action="store_true", help="with --selector: use the first match when several match")
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
    au.add_argument("--script", help="JS file: an expression (an async IIFE works) or a function body using return, giving an array of findings or {findings: [...]}")
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

    cl = sub.add_parser("cleanup", help="stop this device's proxy and any recording iosim started, and close iosim's agent-device session")
    cl.add_argument("--keep-session", action="store_true", help="leave the agent-device session open")

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
        checks = doctor(args.agent_device, args.device)
        ok = all(c["ok"] for c in checks if c["required"])
        return {"ok": ok, "checks": checks}, 0 if ok else 1
    if cmd == "remote":
        rest = args.args[1:] if args.args[:1] == ["--"] else args.args
        rest, device = split_global_flags(rest)
        device = device or args.device
        if rest[:1] == ["run"] and len(rest) >= 2 and rest[1] != "-":
            rp = build_parser().parse_args(rest)
            if not rp.check:
                write_placeholder_report(rp.out, "the remote run started but its evidence has not been copied back")
            plan = load_plan_arg(rp.plan, parse_vars(rp.var))
            if rp.check:
                return {"ok": True, "plan": plan}, 0
            proc = remote_run(plan, rp.out, device=device)
            sys.stderr.write(proc.stderr)
            try:
                report = json.loads(proc.stdout)
            except ValueError:
                report = {"ok": False, "error": "remote run printed no JSON", "stdout": proc.stdout[-1000:]}
            report["localOut"] = str(Path(rp.out).resolve())
            return report, proc.returncode
        proc = remote_call(rest, device=device)
        if ssh_could_not_connect(proc):
            sys.stderr.write(proc.stderr)
            raise IosimError(f"could not reach the Mac over SSH ({os.environ.get('IOSIM_REMOTE')}): {proc.stderr.strip()[-300:]}",
                             "turn on Remote Login (or Tailscale SSH) on the Mac and install your key, or use T3 delegation "
                             "(see references/remote.md)")
        sys.stderr.write(proc.stderr)
        sys.stdout.write(proc.stdout)
        return None, proc.returncode
    if not have_simctl():
        if os.environ.get("IOSIM_REMOTE"):
            # Forward to the Mac. Paths in the command are the Mac's.
            return dispatch(argparse.Namespace(cmd="remote", args=sys.argv[1:], agent_device=None, device=None))
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
        if not args.check:
            write_placeholder_report(args.out)  # first: a run that fails from here on never leaves an old pass behind
        try:
            plan = load_plan_arg(args.plan, parse_vars(args.var))
        except IosimError as e:
            if not args.check:
                write_setup_failure(args.out, e)
            raise
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
        drv = Driver(udid, args.agent_device)
        if read_state(f"record-{udid}.json"):
            try:
                out["recording"] = drv.record_stop()
            except IosimError as e:
                out["recording"] = {"error": str(e)}
        if args.keep_session:
            out["agentDevice"] = {"closed": False, "why": "--keep-session"}
        else:
            # Only the session of the command in use: the given one (its own bound session) or iosim-<udid>.
            out["agentDevice"] = drv.close()
        if drv.mode == "agent-device (given)":
            out["next"] = "inside T3, also call device_close for this device"
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
        pages, key = list_pages(udid), session.page_state()
        for pg in pages:
            pg["driven"] = (bool(key.get("page")) and key.get("proxy") == session.proxy_id()
                            and page_key(pg) == {"page": key["page"], "appId": key.get("appId")})
        return {"ok": True, "pages": pages}, 0
    if cmd == "open":
        return {"ok": True, **session.open(args.url, new_tab=args.new_tab, wait=not args.no_wait, timeout=args.timeout)}, 0
    if cmd == "eval":
        if args.file:
            source, body = Path(args.file).read_text(), "auto"
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
        if args.first and not args.selector or args.contains and args.label is None or args.expect and args.selector:
            raise IosimError("--first goes with --selector, --contains with --label, --expect with --label or --at", code=2)
        return {"ok": True, **session.tap(selector=args.selector, label=args.label, at=args.at, first=args.first, contains=args.contains,
                                          expect=args.expect)}, 0
    if cmd == "type":
        return {"ok": True, **session.type_text(args.text, args.selector, args.enter, args.keep_keyboard, first=args.first)}, 0
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
        emit({"ok": False, "error": "interrupted (Ctrl-C)"})
        return 130
    except Terminated:
        emit({"ok": False, "error": "terminated (SIGTERM)"})
        return 143
    if result is not None:
        emit(result)
    return code


if __name__ == "__main__":
    sys.exit(main())
