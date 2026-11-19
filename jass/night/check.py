"""`jass night check`: the pre-flight report to read before leaving agent work unattended.

Each function takes facts (from `probe` and `t3state`) and returns rows of (status, check, detail).
A `fail` is something that has stopped a night before; the detail names the command that fixes it.
"""

from __future__ import annotations

import subprocess

from jass.night import probe

# Memory an agent holds while it reads, edits and runs small commands, and what stays for the system
# and the T3 server. Measured on a 14 GB Linux box over one night: agent scopes peaked at 1.6 to 2.7 GB,
# file cache included, when their heavy commands ran elsewhere, and five of them ran together for hours.
AGENT_MB = 1500
RESERVE_MB = 2000
HEAVY_MB = 3000
WEAK_SIGNAL_DBM = -75
# A login session that crashed on graphics buffers twice in a week and has not been started again since
# about a day is a `fail`. An estimate: the two crashes this was written after came 25 and 27 hours in.
FRESH_SESSION_HOURS = 12
LOW_DISK_MB = 5000


def sleep_rows(power: dict, hours: float) -> list[tuple]:
    """Will this box stay awake for `hours`?"""
    if not power:
        return [("warn", "sleep", "could not read the sleep policy; check by hand that this box stays awake")]
    hold = f"caffeinate -ims -t {int(hours * 3600)} &"
    rows = []
    if power.get("platform") == "mac":
        holders = [name for name in power.get("prevented_by", []) if any(tool in name.lower() for tool in probe.KEEP_AWAKE)]
        if power.get("idle_minutes") is None:
            rows.append(("warn", "sleep", "could not read `pmset -g`; check by hand that this Mac stays awake"))
        elif power.get("sleep_disabled") or power["idle_minutes"] == 0:
            rows.append(("ok", "sleep", "idle sleep is off"))
        elif holders:
            rows.append(("ok", "sleep", f"held awake by {', '.join(holders)}; it must keep running until the work ends"))
        else:
            others = ", ".join(power.get("prevented_by", []))
            rows.append(("fail", "sleep", f"this Mac sleeps after {power['idle_minutes']} idle minutes and its threads freeze"
                         + (f" (only {others} holds it awake now, and that can end)" if others else "") + f". Run: {hold}"))
        if power.get("lid_closed"):
            rows.append(("fail", "lid", "the lid is closed: a MacBook sleeps with the lid shut unless it has power and an external display"))
        if power.get("on_ac") is False:
            charge = f" at {power['charge']}%" if power.get("charge") is not None else ""
            rows.append(("fail", "power", f"on battery{charge}: plug it in. The battery will not last the night, and `caffeinate -s` holds a Mac awake only on AC power"))
        elif power.get("on_ac"):
            rows.append(("ok", "power", "on AC power"))
        return rows
    if power.get("sleep_masked"):
        rows.append(("ok", "sleep", "sleep targets are masked"))
    elif power.get("idle_action") == "ignore":
        rows.append(("ok", "sleep", "logind does not suspend when idle (a desktop session's own auto-suspend is not checked)"))
    elif power.get("idle_action"):
        rows.append(("fail", "sleep", f"logind IdleAction is {power['idle_action']}: this box can suspend when idle. "
                     f"Hold it for the night with: systemd-inhibit --what=sleep:idle sleep {int(hours * 3600)} &"))
    else:
        rows.append(("warn", "sleep", "could not read logind's IdleAction; check by hand that this box does not suspend"))
    if power.get("on_ac") is False:
        rows.append(("fail", "power", "on battery: plug it in"))
    return rows


def session_rows(session: dict) -> list[tuple]:
    """A Mac whose login session has been crashing will likely lose it again tonight, and T3 Code with it."""
    crashes = (session or {}).get("crashes") or []
    if not crashes:
        return []
    text = (f"the login session crashed {len(crashes)} time{'s' if len(crashes) > 1 else ''} in the last 7 days ({', '.join(c['when'] for c in crashes)}; WindowServer reports). "
            "When it does, every app in it stops, T3 Code and whatever holds the Mac awake included, until someone logs in again")
    if any(c.get("buffers") for c in crashes):
        age = f" This session is {session['hours']:g} hours old." if session.get("hours") is not None else ""
        text += (". The reports name IOSurface: the system ran out of graphics buffers, which long browser test runs can leak." + age
                 + " Log out and back in before the night to start the count again, and keep browser test runs short or on another box")
        old = session.get("hours") is None or session["hours"] >= FRESH_SESSION_HOURS
        return [("fail" if len(crashes) > 1 and old else "warn", "session", text)]
    return [("warn", "session", text + ". Read the report for the cause, or keep tonight's important work on another box")]


def network_rows(net: dict) -> list[tuple]:
    if not net or not net.get("probes"):
        return [("warn", "network", "not probed")]
    rows = []
    failed = net.get("failed", [])
    if failed:
        rows.append(("fail", "network", f"{len(failed)} of {net['probes']} connections failed ({', '.join(sorted(set(failed)))}); a run that loses the provider fails and stays failed"))
    else:
        rows.append(("ok", "network", f"{net['probes']} of {net['probes']} connections opened"))
    name = net.get("interface") or "unknown interface"
    if net.get("wireless"):
        signal = net.get("signal_dbm")
        weak = signal is not None and signal < WEAK_SIGNAL_DBM
        strength = f", signal {signal} dBm" + (" (weak)" if weak else "") if signal is not None else ""
        rows.append(("warn", "link", f"Wi-Fi ({name}{strength}): a short drop fails a run. Use a cable if you can, and run `jass night watch` so failed runs get noticed"))
    elif net.get("interface"):
        rows.append(("ok", "link", f"wired ({name})"))
    return rows


def memory_rows(mem: dict, temp: dict, agents: int, agent_mb: int = AGENT_MB, heavy_mb: int = HEAVY_MB, reserve_mb: int = RESERVE_MB) -> list[tuple]:
    """Does the planned number of agents fit, with one heavy command running under its cap?"""
    if not mem.get("total_mb"):
        return [("warn", "memory", "could not read memory; count your agents by hand")]
    total = mem["total_mb"]
    in_temp = temp.get("used_mb", 0) if temp.get("in_memory") else 0
    room = total - reserve_mb - in_temp
    fits = max(0, (room - heavy_mb) // agent_mb)
    need = agents * agent_mb + heavy_mb
    sums = f"{agents} agents x {agent_mb} MB + one heavy command at {heavy_mb} MB = {need} MB, against {room} MB ({total} MB less {reserve_mb} MB for the system" \
           + (f" and {in_temp} MB of temp files held in memory" if in_temp else "") + ")"
    rows = []
    high = mem.get("agents_high_mb")
    if high:
        fits = min(fits, high // agent_mb)
    if high and agents * agent_mb > high:
        rows.append(("fail", "memory", f"{agents} agents x {agent_mb} MB = {agents * agent_mb} MB, but T3 Code slows all agents together above {high} MB "
                     f"and the system then kills some. This box fits {fits} agents; move the rest to another box or night"))
    elif need > room:
        rows.append(("fail", "memory", f"{sums}. This box fits {fits} agents; move the rest to another box or night"))
    elif need > room * 0.85:
        rows.append(("warn", "memory", f"{sums}. Tight: no room for a second heavy command"))
    else:
        rows.append(("ok", "memory", sums))
    if mem.get("available_mb") is not None and mem["available_mb"] < total * 0.25:
        rows.append(("warn", "memory now", f"only {mem['available_mb']} MB of {total} MB is available before the night starts; find what holds it"))
    swaps = mem.get("swaps") or []
    if swaps and all(swap["zram"] for swap in swaps):
        rows.append(("warn", "swap", f"all swap ({sum(s['size_mb'] for s in swaps)} MB) is zram, which is compressed memory, not extra memory; the sums above do not count it"))
    if mem.get("oomd"):
        rows.append(("warn", "oomd", "systemd-oomd is active: under memory pressure it kills an agent's whole process group, background jobs included. "
                     "Agents must run builds, test suites and installs through `jass heavy -- <command>`"))
    if temp.get("in_memory"):
        level = "warn" if in_temp > total * 0.1 else "ok"
        rows.append((level, "temp", f"{temp['path']} is held in memory and has {in_temp} MB in it"
                     + ("; clear old scratch folders, or move the temp folder to disk" if level == "warn" else "")))
    return rows


def disk_rows(*spaces: dict) -> list[tuple]:
    rows = []
    for facts in spaces:
        if facts and not facts.get("in_memory"):
            low = facts["free_mb"] < LOW_DISK_MB
            rows.append(("warn" if low else "ok", "disk", f"{facts['path']}: {facts['free_mb'] // 1024} GB free" + ("; builds and worktrees may fill it" if low else "")))
    return rows


def need_rows(commands: list[str], timeout: float = 30) -> list[tuple]:
    """Each `--need` command proves one thing the night depends on (a sign-in, a tool, a device) by exiting 0."""
    rows = []
    for command in commands:
        try:
            done = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
            ok, why = done.returncode == 0, f"exit {done.returncode}"
        except subprocess.TimeoutExpired:
            ok, why = False, f"no answer in {int(timeout)} s (it may be waiting for a password)"
        rows.append(("ok" if ok else "fail", "need", f"`{command}`" + ("" if ok else f": {why}. Fix it now; nobody can at night")))
    return rows


def peer_rows(peers: list[str]) -> list[tuple]:
    rows = []
    for peer in peers:
        up = probe.reach(peer)
        rows.append(("ok" if up else "fail", "peer", f"{peer} answers; run `jass night check` there too, this box cannot see inside it" if up
                     else f"{peer} does not answer: it is asleep, off or off the network"))
    return rows


def t3_rows(t3: dict, hours: float) -> list[tuple]:
    """Providers signed in and with quota left for the night, and T3 Code's own night settings."""
    if not t3 or not t3.get("found"):
        return []
    rows = []
    for name, provider in sorted(t3.get("providers", {}).items()):
        if not (provider["ready"] and provider["signed_in"]):
            rows.append(("fail", "provider", f"{name} is not signed in or not ready in T3 Code; threads on it fail at their first turn"))
            continue
        tight = [w for w in provider["windows"] if w["used"] >= 70 and (w["resets_in_hours"] is None or w["resets_in_hours"] > 1)]
        for window in tight:
            reset = f", resets in {window['resets_in_hours']} h" if window["resets_in_hours"] is not None else ""
            spent = window["used"] >= 95
            rows.append(("fail" if spent else "warn", "usage", f"{name} {window['label']}: {window['used']:.0f}% used{reset}. "
                         + ("Threads on it will stop almost at once" if spent else f"{hours:g} hours of agents may reach the limit")))
        if not tight:
            used = ", ".join(f"{w['label']} {w['used']:.0f}%" for w in provider["windows"]) or "no quota figures yet"
            rows.append(("ok", "usage", f"{name} ({provider['plan'] or 'signed in'}): {used}"))
    if not t3.get("resume_after_limit"):
        rows.append(("warn", "t3 setting", "autoResumeLimitedThreads is off: a thread that reaches a usage limit stays stopped after the limit resets. "
                     "Turn it on in T3 Code's settings"))
    if not t3.get("keep_awake"):
        rows.append(("warn", "t3 setting", "keepAwakeWhileAgentsWork is off: T3 Code will not hold this box awake while agents run"))
    return rows


def thread_rows(threads: list[dict] | None, problem: str = "") -> list[tuple]:
    """What T3 Code's threads need before the owner leaves. `threads` is `t3state.snapshot()`, or None with `problem`."""
    if threads is None:
        return [("warn", "threads", f"T3 Code threads not checked: {problem}")]

    def names(state: str) -> list[str]:
        return [t["title"] or t["id"][:16] for t in threads if t["state"] == state]

    rows = []
    blocked = [f"{t['title'] or t['id'][:16]} ({t['detail']})" for t in threads if t["state"] == "blocked"]
    if blocked:
        rows.append(("fail", "threads", f"{len(blocked)} wait for you and will wait all night: " + "; ".join(blocked)))
    broken = names("failed") + names("stuck")
    if broken:
        rows.append(("warn", "threads", f"{len(broken)} stopped on a failed or stuck run: " + "; ".join(broken[:8]) + ". Send each one `continue`, or archive it"))
    going = names("working") + names("delegated") + names("background")
    rows.append(("ok", "threads", f"{len(going)} working now" + (": " + "; ".join(going[:8]) if going else "") + ("" if blocked else "; none waits for you")))
    return rows


def report(cfg: dict, facts: dict, threads: list[dict] | None, problem: str, *, hours: float, agents: int, needs: list[str], peers: list[str]) -> list[tuple]:
    """Every row of `jass night check`, in the order a night usually fails."""
    night = cfg.get("night", {})
    planned = max(1, agents)
    total = facts.get("memory", {}).get("total_mb", 0)
    heavy = night.get("heavy_mb")
    heavy = int(heavy) if heavy is not None else max(2000, total // 4)
    return [
        *sleep_rows(facts.get("power", {}), hours),
        *session_rows(facts.get("session", {})),
        *peer_rows(peers),
        *memory_rows(facts.get("memory", {}), facts.get("temp", {}), planned, int(night.get("agent_mb") or AGENT_MB), heavy or HEAVY_MB, int(night.get("reserve_mb") or RESERVE_MB)),
        *network_rows(facts.get("network", {})),
        *disk_rows(facts.get("home", {}), facts.get("temp", {})),
        *t3_rows(facts.get("t3", {}), hours),
        *thread_rows(threads, problem),
        *need_rows(needs),
    ]
