"""Read-only facts about this box for `jass night`. Parsers take text, so tests feed them samples.

`collect()` returns a plain dict; a fact that cannot be read is left out or None, and the
checks say so instead of guessing.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from jass import paths

PROBE_HOSTS = ("api.anthropic.com:443", "api.openai.com:443", "github.com:443")
AGENTS_SLICE = "app-t3code-agents.slice"  # T3 Code runs every agent under this slice and sets its MemoryHigh
# Tools that hold a Mac awake on purpose, for as long as they run.
KEEP_AWAKE = ("caffeinate", "amphetamine", "keepingyouawake", "lungo")


def run(*argv: str, timeout: float = 8) -> str:
    """Standard output of a command, or an empty string when it is missing, fails or hangs."""
    if not shutil.which(argv[0]):
        return ""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout


def read(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


# ---- memory -------------------------------------------------------------------------------------

def parse_meminfo(text: str) -> dict:
    """MemTotal, MemAvailable, SwapTotal and SwapFree of /proc/meminfo, in MB."""
    found = {key: int(value) // 1024 for key, value in re.findall(r"^(\w+):\s+(\d+) kB", text, re.M)}
    names = {"MemTotal": "total_mb", "MemAvailable": "available_mb", "SwapTotal": "swap_total_mb", "SwapFree": "swap_free_mb"}
    return {name: found[key] for key, name in names.items() if key in found}


def parse_swaps(text: str) -> list[dict]:
    """/proc/swaps rows. zram swap is compressed RAM: it adds no memory beyond what compression saves."""
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and parts[2].isdigit():
            rows.append({"name": parts[0], "zram": "zram" in parts[0], "size_mb": int(parts[2]) // 1024, "used_mb": int(parts[3]) // 1024})
    return rows


def parse_vm_stat(text: str, total_mb: int) -> dict:
    """macOS `vm_stat`: pages that are free, inactive or speculative count as available."""
    size = re.search(r"page size of (\d+) bytes", text)
    pages = {name: int(count) for name, count in re.findall(r"^Pages ([\w ]+):\s+(\d+)\.", text, re.M)}
    if not size or not pages:
        return {}
    free = sum(pages.get(name, 0) for name in ("free", "inactive", "speculative"))
    return {"total_mb": total_mb, "available_mb": free * int(size.group(1)) // (1024 * 1024)}


def parse_pressure(text: str) -> float | None:
    """The `some avg60` share of /proc/pressure/memory: percent of the last minute with a task stalled on memory."""
    found = re.search(r"^some .*avg60=([\d.]+)", text, re.M)
    return float(found.group(1)) if found else None


def memory() -> dict:
    if sys.platform == "darwin":
        total = run("sysctl", "-n", "hw.memsize").strip()
        return parse_vm_stat(run("vm_stat"), int(total) // (1024 * 1024)) if total.isdigit() else {}
    facts = parse_meminfo(read("/proc/meminfo"))
    if facts:
        facts["swaps"] = parse_swaps(read("/proc/swaps"))
        facts["pressure"] = parse_pressure(read("/proc/pressure/memory"))
        facts["oomd"] = run("systemctl", "is-active", "systemd-oomd").strip() == "active"
        high = run("systemctl", "--user", "show", AGENTS_SLICE, "-p", "MemoryHigh", "--value").strip()
        if high.isdigit():  # "infinity" when there is no such slice or no limit
            facts["agents_high_mb"] = int(high) // (1024 * 1024)
    return facts


def parse_usage(text: str, now: float) -> list[dict]:
    """Provider quota windows from one of T3 Code's `caches/<provider>.json` files: label, percent used, hours until reset."""
    try:
        windows = json.loads(text).get("usageLimits", {}).get("windows", [])
    except (ValueError, AttributeError):
        return []
    out = []
    for window in windows if isinstance(windows, list) else []:
        try:
            used = float(window["usedPercent"])
            stamp = str(window.get("resetsAt") or "")
            reset = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() if stamp else None
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"label": str(window.get("label") or window.get("id") or "window"), "used": used,
                    "resets_in_hours": round((reset - now) / 3600, 1) if reset else None})
    return out


def t3(base: Path) -> dict:
    """What T3 Code's data folder says about its providers and the two settings that matter at night."""
    providers = {}
    try:
        names = sorted(path.stem for path in (base / "caches").glob("*.json"))
    except OSError:
        names = []
    for name in names:
        text = read(str(base / "caches" / f"{name}.json"))
        try:
            cache = json.loads(text) if text else {}
        except ValueError:
            cache = {}
        if isinstance(cache, dict) and cache.get("enabled") and cache.get("installed"):
            auth = cache.get("auth") if isinstance(cache.get("auth"), dict) else {}
            providers[cache.get("displayName") or name] = {
                "id": name, "ready": cache.get("status") == "ready", "signed_in": auth.get("status") == "authenticated",
                "plan": str(auth.get("label") or ""), "windows": parse_usage(text, time.time()),
            }
    try:
        settings = json.loads(read(str(base / "userdata" / "settings.json")) or "{}")
    except ValueError:
        settings = {}
    settings = settings if isinstance(settings, dict) else {}
    return {"providers": providers, "found": (base / "userdata").is_dir(),
            "resume_after_limit": bool(settings.get("autoResumeLimitedThreads", False)),
            "keep_awake": bool(settings.get("keepAwakeWhileAgentsWork", True))}


# ---- temp folder and disk -----------------------------------------------------------------------

def mount_of(path: Path, mounts: str) -> tuple[str, str]:
    """(mount point, filesystem type) of the /proc/mounts row that holds `path`."""
    best = ("", "")
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) >= 3 and (str(path) == parts[1] or str(path).startswith(parts[1].rstrip("/") + "/")) and len(parts[1]) >= len(best[0]):
            best = (parts[1], parts[2])
    return best


def space(path: Path) -> dict:
    try:
        stat = os.statvfs(path)
    except OSError:
        return {}
    block = stat.f_frsize / (1024 * 1024)
    return {"path": str(path), "free_mb": int(stat.f_bavail * block), "used_mb": int((stat.f_blocks - stat.f_bfree) * block)}


def temp() -> dict:
    """The temp folder. On tmpfs every file in it is held in memory or swap."""
    folder = Path(tempfile.gettempdir()).resolve()
    facts = space(folder)
    if facts:
        facts["in_memory"] = mount_of(folder, read("/proc/mounts"))[1] == "tmpfs"
    return facts


# ---- network ------------------------------------------------------------------------------------

def parse_route(text: str) -> str:
    """The interface of the default route, from Linux `ip -o route get` or macOS `route -n get default`."""
    found = re.search(r"\bdev (\S+)", text) or re.search(r"^\s*interface: (\S+)", text, re.M)
    return found.group(1) if found else ""


def parse_wireless(text: str) -> dict:
    """/proc/net/wireless: {interface: signal level in dBm}."""
    out = {}
    for line in text.splitlines()[2:]:
        parts = line.replace(".", " ").split()
        if len(parts) >= 4 and parts[0].endswith(":"):
            try:
                out[parts[0][:-1]] = int(parts[3])
            except ValueError:
                pass
    return out


def parse_hardware_ports(text: str) -> set[str]:
    """macOS `networksetup -listallhardwareports`: the devices that are Wi-Fi."""
    return set(re.findall(r"Hardware Port: Wi-Fi\s*\nDevice: (\S+)", text))


def reach(target: str, timeout: float = 5) -> bool:
    """Whether a TCP connection to `host:port` opens. A refused connection still proves the host is up."""
    host, _, port = target.rpartition(":")
    try:
        socket.create_connection((host or target, int(port) if host else 22), timeout=timeout).close()
    except ConnectionRefusedError:
        return True
    except (OSError, ValueError):
        return False
    return True


def network(hosts=PROBE_HOSTS, attempts: int = 3) -> dict:
    if sys.platform == "darwin":
        iface = parse_route(run("route", "-n", "get", "default"))
        wireless = iface in parse_hardware_ports(run("networksetup", "-listallhardwareports"))
        signal = None
    else:
        iface = parse_route(run("ip", "-o", "route", "get", "1.1.1.1"))
        wireless = bool(iface) and Path(f"/sys/class/net/{iface}/wireless").exists()
        signal = parse_wireless(read("/proc/net/wireless")).get(iface)
    failed = []
    for host in hosts:
        for attempt in range(attempts):
            if not reach(host):
                failed.append(host)
            if attempt + 1 < attempts:
                time.sleep(0.2)
    return {"interface": iface, "wireless": wireless, "signal_dbm": signal, "probes": len(hosts) * attempts, "failed": failed}


def online(hosts=PROBE_HOSTS) -> bool:
    """Whether any provider answers; the first that does ends the probing."""
    return any(reach(host) for host in hosts)


# ---- sleep and power ----------------------------------------------------------------------------

def parse_pmset(text: str) -> dict:
    """macOS `pmset -g`: minutes until idle sleep (0 is never), what prevents it now, and SleepDisabled."""
    facts: dict = {}
    line = re.search(r"^\s*sleep\s+(\d+)(?:\s+\((.*)\))?\s*$", text, re.M)
    if line:
        facts["idle_minutes"] = int(line.group(1))
        note = line.group(2) or ""
        names = [name.strip() for name in note.split("by", 1)[1].split(",")] if "prevented by" in note else []
        facts["prevented_by"] = list(dict.fromkeys(names))  # pmset names a process once per assertion
    disabled = re.search(r"^\s*SleepDisabled\s+(\d)", text, re.M)
    if disabled:
        facts["sleep_disabled"] = disabled.group(1) == "1"
    return facts


def parse_caffeinate(text: str) -> bool:
    """From `ps -axo pid=,args=`: whether a `caffeinate` runs that will last the night.

    One tied to a command or to a process with `-w` ends with it; T3 Code starts such a one and it lasts
    only while a run is going. The one `jass night watch --awake` starts lasts as long as the watch."""
    rows = [line.split() for line in text.splitlines()]
    args = {row[0]: row[1:] for row in rows if len(row) > 1}
    for words in args.values():
        if os.path.basename(words[0]) != "caffeinate":
            continue
        if not all(word.startswith("-") or word.isdigit() for word in words[1:]):
            continue  # `caffeinate make test` lasts as long as the command
        if "-w" not in words:
            return True
        watched = args.get(words[words.index("-w") + 1] if words.index("-w") + 1 < len(words) else "", [])
        if "night" in watched and "watch" in watched:
            return True
    return False


def parse_batt(text: str) -> dict:
    """macOS `pmset -g batt`: the power source and the battery charge."""
    source = re.search(r"Now drawing from '([^']+)'", text)
    charge = re.search(r"(\d+)%", text)
    return {"on_ac": source.group(1) == "AC Power" if source else None, "charge": int(charge.group(1)) if charge else None}


def power() -> dict:
    if sys.platform == "darwin":
        facts = parse_pmset(run("pmset", "-g"))
        facts.update(parse_batt(run("pmset", "-g", "batt")))
        clamshell = run("ioreg", "-r", "-k", "AppleClamshellState", "-d", "1")
        lid = re.search(r'"AppleClamshellState" = (Yes|No)', clamshell)
        sleeps = re.search(r'"AppleClamshellCausesSleep" = (Yes|No)', clamshell)
        # A shut lid matters only where it causes sleep: a docked Mac on power with a display stays awake.
        facts["lid_closed"] = lid.group(1) == "Yes" and not (sleeps and sleeps.group(1) == "No") if lid else None
        facts["caffeinate_lasts"] = parse_caffeinate(run("ps", "-axo", "pid=,args="))
        facts["platform"] = "mac"
        return facts
    idle = re.search(r'"(\w+)"', run("busctl", "get-property", "org.freedesktop.login1", "/org/freedesktop/login1",
                                     "org.freedesktop.login1.Manager", "IdleAction"))
    supplies = [read(str(p)).strip() for p in Path("/sys/class/power_supply").glob("*/online")] if Path("/sys/class/power_supply").is_dir() else []
    return {
        "platform": "linux",
        "sleep_masked": run("systemctl", "is-enabled", "sleep.target").strip() == "masked",
        "idle_action": idle.group(1) if idle else None,
        "on_ac": ("1" in supplies) if supplies else None,
    }


def parse_etime(text: str) -> float | None:
    """Hours from `ps -o etime=` output, which reads [[days-]hours:]minutes:seconds."""
    found = re.fullmatch(r"(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)", text.strip())
    if not found:
        return None
    days, hours, minutes, _ = (int(part or 0) for part in found.groups())
    return round(days * 24 + hours + minutes / 60, 1)


def session(days: float = 7, folder: str = "/Library/Logs/DiagnosticReports") -> dict:
    """Recent macOS WindowServer crashes and how long the current one has run.

    When WindowServer dies the login session ends, and every app in it stops until someone logs in.
    A report that names IOSurface means the system ran out of graphics buffers, which a long browser
    test run can leak; the count starts again with each new session."""
    cutoff = time.time() - days * 86400
    crashes = []
    try:
        reports = sorted(p for p in Path(folder).glob("WindowServer-*.ips") if p.stat().st_mtime >= cutoff)
    except OSError:
        reports = []
    for report in reports:
        try:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(report.stat().st_mtime))
        except OSError:
            continue
        try:
            buffers = "IOSurface" in report.read_text(errors="replace")
        except OSError:  # only admins may read the reports; the crash still counts
            buffers = False
        crashes.append({"when": when, "buffers": buffers})
    pid = run("pgrep", "-x", "WindowServer").split()
    return {"crashes": crashes, "hours": parse_etime(run("ps", "-o", "etime=", "-p", pid[0])) if pid else None}


# ---- everything ---------------------------------------------------------------------------------

def collect(hosts=PROBE_HOSTS) -> dict:
    return {"memory": memory(), "temp": temp(), "home": space(paths.home()), "network": network(hosts), "power": power(),
            "session": session() if sys.platform == "darwin" else {}}
