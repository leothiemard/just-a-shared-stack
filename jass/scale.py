"""`jass board scale`: wake cloud VMs when board tasks wait for them, and stop them all at a spend cap.

Each run is one tick, meant for launchd or cron every couple of minutes. A tick:

1. takes a lock, so overlapping ticks never resume the same VM twice, and exits when `scale.enabled` is false or
   `--off` was set;
2. adds up what the pool's VMs cost since `scale.since`, and at `spend_cap_usd` stops every running pool VM and
   switches itself off;
3. counts the ready and claimed tasks of `scale.channels` (project keys such as `fma-boat`);
4. pushes back the auto-stop of each running VM whose box holds a claimed task, so a long task is not cut off;
5. stops a running VM after `idle_stop_minutes` without any demand, a safety net for a worker that failed to exit;
6. resumes stopped VMs until `ceil(demand / per_vm)` run, and starts each one's worker with `start_command`.

It never creates or deletes a VM. The workers stop their own VMs once the queue is empty (`jass board work
--idle-exit` in a unit whose `ExecStopPost` stops the machine), so the tick only ever has to start them.

`tick` touches nothing but the `Boat`, `Board` and clock it is given, so tests drive it with fakes.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import subprocess
import time
from pathlib import Path

from jass import JassError, paths
from jass.board import BOX, Board, BoardError, Logger

ACTIVE = ("provisioned", "provisioning", "cloning", "starting", "resuming", "ready", "idle", "running")
STOPPED = ("stopped",)


class ScaleError(JassError):
    pass


def settings(cfg: dict) -> dict:
    s = dict(cfg.get("scale") or {})
    pool = []
    for item in s.get("pool") or []:
        vm = {"id": item} if isinstance(item, str) else dict(item)
        if not vm.get("id"):
            raise ScaleError(f"scale.pool entry {item!r} has no id")
        vm.setdefault("box", "")
        pool.append(vm)
    s["pool"] = pool
    return s


def channels(cfg: dict, s: dict) -> list[str]:
    prefix = (cfg.get("board") or {}).get("channel", "build").rstrip("/")
    return [f"{prefix}/{key}" for key in s.get("channels") or []]


# --- ports ---------------------------------------------------------------------------------------


class Boat:
    """The `boat` CLI, always with `--json`."""

    def __init__(self, command: str = "boat", timeout: int = 120):
        self.command, self.timeout = command, timeout

    def run(self, *args: str, timeout: int | None = None) -> tuple[int, dict | None, str]:
        try:
            done = subprocess.run([self.command, *args], capture_output=True, text=True, timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            return 124, None, "timed out"
        except OSError as error:
            raise ScaleError(f"Cannot run boat ({error}). Install the boat CLI or set scale.boat to its path.")
        data = None
        # One object, or JSON lines (events) of which the last says how it ended.
        for line in reversed(done.stdout.strip().splitlines()):
            try:
                data = json.loads(line)
                break
            except ValueError:
                continue
        if data is None:
            try:
                data = json.loads(done.stdout)
            except ValueError:
                pass
        return done.returncode, data if isinstance(data, dict) else None, (done.stdout + done.stderr).strip()

    @staticmethod
    def reason(data: dict | None, raw: str) -> str:
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])[:200]
        return raw.splitlines()[-1][:200] if raw else "no output"

    def states(self) -> dict[str, str]:
        rc, data, raw = self.run("list", "--all", "--json")
        if rc != 0 or not data:
            raise ScaleError(f"boat list failed: {self.reason(data, raw)}")
        return {vm["id"]: vm.get("state", "") for vm in data.get("sandboxes") or []}

    def dollars(self, vm: str, since: str) -> float:
        args = ["usage", vm, "--json"] + (["--since", since] if since else [])
        rc, data, raw = self.run(*args)
        if rc != 0 or not data or "dollars" not in data:
            raise ScaleError(f"boat usage {vm} failed: {self.reason(data, raw)}")
        return float(data["dollars"])

    def resume(self, vm: str, ttl_s: int) -> str:
        """'' once the VM is ready, else the reason. A resume can take minutes, or fail and need a retry later."""
        rc, data, raw = self.run("resume", vm, "--ttl", str(ttl_s), "--json", timeout=900)
        if rc == 0 and isinstance(data, dict) and (data.get("event") == "ready" or data.get("state") == "ready"):
            return ""
        return self.reason(data, raw)

    def extend(self, vm: str, ttl_s: int) -> str:
        rc, data, raw = self.run("extend", vm, "--ttl", str(ttl_s), "--json")
        return "" if rc == 0 else self.reason(data, raw)

    def start_worker(self, vm: str, command: str) -> str:
        # A login shell: boat puts the VM's environment variables there only.
        rc, data, raw = self.run("exec", "--detach", "--json", vm, "--", "bash", "-lc", command)
        return "" if rc == 0 else self.reason(data, raw)

    def stop(self, vm: str) -> str:
        rc, data, raw = self.run("stop", vm, "--json", timeout=300)
        return "" if rc == 0 else self.reason(data, raw)


class State:
    """`~/.cache/jass/scale/state.json`: the off switch, starts per day, and when each VM was last seen without demand."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("off", False)
        self.data.setdefault("off_reason", "")
        self.data.setdefault("starts", {})
        self.data.setdefault("idle_since", {})
        self.data.setdefault("warned", False)

    def save(self) -> None:
        paths.private_dir(self.path.parent)
        paths.write_atomic(self.path, json.dumps(self.data, indent=2) + "\n")

    def starts_today(self, today: str) -> int:
        return int(self.data["starts"].get(today, 0))

    def count_start(self, today: str) -> None:
        self.data["starts"] = {today: self.starts_today(today) + 1}


def scale_dir() -> Path:
    return paths.cache_dir() / "scale"


# --- one tick ------------------------------------------------------------------------------------


def demand(board: Board, chans: list[str]) -> tuple[int, int, set[str]]:
    """Ready tasks, claimed tasks, and the boxes that hold the claimed ones."""
    ready, claimed, boxes = 0, 0, set()
    for channel in chans:
        ready += len(board.tasks(channel, "ready"))
        for item in board.tasks(channel, "claimed"):
            claimed += 1
            box = ((item.get("message", item)).get("labels") or {}).get(BOX)
            if box:
                boxes.add(box)
    return ready, claimed, boxes


def tick(cfg: dict, boat: Boat, board: Board, state: State, now: float, log) -> str:
    """One pass; returns a one-line summary of what it did."""
    s = settings(cfg)
    if not s.get("enabled"):
        return "scale.enabled is false"
    if state.data["off"]:
        return f"off ({state.data['off_reason'] or 'jass board scale --off'})"
    pool, chans = s["pool"], channels(cfg, s)
    if not pool or not chans:
        raise ScaleError("scale.pool and scale.channels must both be set: the VMs to wake and the project keys they work")
    ttl_s = int(float(s.get("ttl_minutes", 120)) * 60)
    cap, warn = float(s.get("spend_cap_usd", 8)), float(s.get("spend_warn_usd", 5))
    today = dt.datetime.fromtimestamp(now).date().isoformat()

    states = boat.states()
    running = [vm for vm in pool if states.get(vm["id"]) in ACTIVE]
    spend = sum(boat.dollars(vm["id"], s.get("since", "")) for vm in pool)
    state.data["spend"] = round(spend, 4)
    if spend >= cap:
        for vm in running:
            problem = boat.stop(vm["id"])
            log(f"spend cap: stopped {vm['id']}" + (f" (failed: {problem})" if problem else ""))
        state.data["off"], state.data["off_reason"] = True, f"spend ${spend:.2f} reached the ${cap:g} cap"
        log(f"spend ${spend:.2f} reached the ${cap:g} cap: stopped the pool and switched scaling off; `jass board scale --on` after raising the cap")
        return f"spend cap reached (${spend:.2f}); off"
    if spend >= warn and not state.data["warned"]:
        state.data["warned"] = True
        log(f"warning: pool spend ${spend:.2f} passed ${warn:g} (cap ${cap:g})")

    ready, claimed, busy_boxes = demand(board, chans)
    wanted = min(len(pool), math.ceil((ready + claimed) / max(1, int(s.get("per_vm", 2)))))
    actions = []

    idle_s = float(s.get("idle_stop_minutes", 15)) * 60
    for vm in running:
        if vm["box"] and vm["box"] in busy_boxes:
            problem = boat.extend(vm["id"], ttl_s)
            if problem:
                log(f"extending {vm['id']} failed: {problem}")
        if ready + claimed:
            state.data["idle_since"].pop(vm["id"], None)
            continue
        since = state.data["idle_since"].setdefault(vm["id"], now)
        if now - since >= idle_s:
            problem = boat.stop(vm["id"])
            log(f"stopped {vm['id']}: running with no task for {idle_s / 60:g} minutes" + (f" (failed: {problem})" if problem else ""))
            actions.append(f"stopped {vm['id']}")
            state.data["idle_since"].pop(vm["id"], None)
    for vm_id in list(state.data["idle_since"]):
        if vm_id not in {vm["id"] for vm in running}:
            state.data["idle_since"].pop(vm_id)

    missing = wanted - len(running)
    for vm in [vm for vm in pool if states.get(vm["id"]) in STOPPED][:max(0, missing)]:
        if state.starts_today(today) >= int(s.get("max_starts_per_day", 30)):
            log(f"not starting {vm['id']}: {state.starts_today(today)} starts today reached scale.max_starts_per_day")
            break
        state.count_start(today)
        state.save()
        log(f"starting {vm['id']} for {ready} ready and {claimed} claimed tasks")
        problem = boat.resume(vm["id"], ttl_s)
        if problem:
            log(f"resuming {vm['id']} failed: {problem}; the next tick tries again")
            continue
        problem = boat.start_worker(vm["id"], s.get("start_command") or "systemctl --user start jass-board-vm")
        log(f"started the worker on {vm['id']}" if not problem else f"starting the worker on {vm['id']} failed: {problem}")
        actions.append(f"started {vm['id']}")
    return (f"{ready} ready, {claimed} claimed, {len(running)} of {len(pool)} running, spend ${spend:.2f}"
            + (f"; {', '.join(actions)}" if actions else ""))


# --- command -------------------------------------------------------------------------------------


def run(cfg: dict, args) -> int:
    s = settings(cfg)
    folder = paths.private_dir(scale_dir())
    state = State(folder / "state.json")
    log = Logger(folder / f"scale-{dt.date.today().isoformat()}.log")
    if args.off or args.on:
        with paths.Lock(folder / "lock", wait=True):
            state = State(folder / "state.json")
            state.data["off"] = bool(args.off)
            state.data["off_reason"] = "jass board scale --off" if args.off else ""
            if args.on:
                state.data["warned"] = False
            state.save()
        log("scaling switched " + ("off" if args.off else "on"))
        return 0
    if args.status:
        return status(cfg, s, state, folder)
    with paths.Lock(folder / "lock") as held:
        if not held:
            return 0  # an earlier tick is still resuming a VM
        state = State(folder / "state.json")
        try:
            summary = tick(cfg, Boat(s.get("boat") or "boat"), Board(args.as_profile), state, time.time(), log)
        except (ScaleError, BoardError) as error:
            log(f"tick failed: {error}")
            state.save()
            raise
        state.save()
        if args.verbose:
            print(summary)
    return 0


def status(cfg: dict, s: dict, state: State, folder: Path) -> int:
    print(f"enabled: {bool(s.get('enabled'))}" + (f"; off: {state.data['off_reason']}" if state.data["off"] else ""))
    print(f"spend since {s.get('since') or 'each VM was created'}: ${state.data.get('spend', 0):.2f} "
          f"(warn ${float(s.get('spend_warn_usd', 5)):g}, cap ${float(s.get('spend_cap_usd', 8)):g})")
    today = dt.date.today().isoformat()
    print(f"starts today: {state.starts_today(today)} of {int(s.get('max_starts_per_day', 30))}")
    if s["pool"]:
        try:
            states = Boat(s.get("boat") or "boat").states()
        except ScaleError as error:
            states = {}
            print(f"pool: {error}")
        for vm in s["pool"]:
            print(f"  {vm['id']:<14} {vm['box'] or '-':<12} {states.get(vm['id'], 'unknown')}")
    logs = sorted(folder.glob("scale-*.log"))
    if logs:
        print("last actions:")
        for line in logs[-1].read_text().splitlines()[-8:]:
            print("  " + line[:160])
    return 0
