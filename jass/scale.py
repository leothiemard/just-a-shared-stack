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
UP = ("ready", "idle", "running")


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
    """`~/.cache/jass/scale/state.json` (starts per day, idle clocks, the tasks each start was for) and the off switch,
    a separate file `off` holding its reason, so `--off` works at once even while a tick holds the lock."""

    def __init__(self, path: Path):
        self.path, self.off_path = path, path.parent / "off"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        self.data = data if isinstance(data, dict) else {}
        for key, empty in (("starts", {}), ("idle_since", {}), ("offered", []), ("held", []), ("warned", False)):
            if not isinstance(self.data.get(key), type(empty)):
                self.data[key] = empty

    def save(self) -> None:
        paths.private_dir(self.path.parent)
        paths.write_atomic(self.path, json.dumps(self.data, indent=2) + "\n")

    def off(self) -> str:
        """Why scaling is off, or '' when it is on."""
        try:
            return self.off_path.read_text().strip() or "jass board scale --off"
        except OSError:
            return ""

    def switch_off(self, reason: str) -> None:
        paths.private_dir(self.off_path.parent)
        paths.write_atomic(self.off_path, reason + "\n")

    def switch_on(self) -> None:
        try:
            self.off_path.unlink()
        except FileNotFoundError:
            pass
        self.data["warned"] = False
        self.data["offered"], self.data["held"] = [], []

    def starts_today(self, today: str) -> int:
        return int(self.data["starts"].get(today, 0))

    def count_start(self, today: str) -> None:
        self.data["starts"] = {today: self.starts_today(today) + 1}


def scale_dir() -> Path:
    return paths.cache_dir() / "scale"


# --- one tick ------------------------------------------------------------------------------------

CAP_PREFIX = "spend cap:"


def demand(board: Board, chans: list[str]) -> tuple[list[str], int, set[str]]:
    """Ids of ready tasks, the number of claimed tasks, and the boxes that hold the claimed ones."""
    ready, claimed, boxes = [], 0, set()
    for channel in chans:
        ready += [str((item.get("message", item)).get("id", "")) for item in board.tasks(channel, "ready")]
        for item in board.tasks(channel, "claimed"):
            claimed += 1
            box = ((item.get("message", item)).get("labels") or {}).get(BOX)
            if box:
                boxes.add(box)
    return ready, claimed, boxes


def spend_of(boat: Boat, pool: list[dict], since: str) -> float:
    return sum(boat.dollars(vm["id"], since) for vm in pool)


def stop_all(boat: Boat, pool: list[dict], states: dict[str, str], log, why: str) -> None:
    """Stop every pool VM that is not stopped, whatever its state (an `error` or `stopping` VM included)."""
    for vm in pool:
        if vm["id"] in states and states[vm["id"]] not in STOPPED:
            problem = boat.stop(vm["id"])
            log(f"{why}: stopping {vm['id']} ({states.get(vm['id'], 'unknown')})" + (f" failed: {problem}" if problem else ""))


def tick(cfg: dict, boat: Boat, board: Board, state: State, now: float, log) -> str:
    """One pass; returns a one-line summary of what it did."""
    s = settings(cfg)
    if not s.get("enabled"):
        return "scale.enabled is false"
    pool, chans = s["pool"], channels(cfg, s)
    if not pool or not chans:
        raise ScaleError("scale.pool and scale.channels must both be set: the VMs to wake and the project keys they work")
    off = state.off()
    if off.startswith(CAP_PREFIX):
        # Keep stopping until the whole pool reads stopped: a stop can fail, and a VM can be mid-resume.
        states = boat.states()
        stop_all(boat, pool, states, log, "spend cap")
        return f"off ({off})"
    if off:
        return f"off ({off})"
    ttl_s = int(float(s.get("ttl_minutes", 120)) * 60)
    cap, warn = float(s.get("spend_cap_usd", 8)), float(s.get("spend_warn_usd", 5))
    since = s.get("since", "")
    today = dt.datetime.fromtimestamp(now).date().isoformat()
    start_command = s.get("start_command") or "systemctl --user start jass-board-vm"

    states = boat.states()
    running = [vm for vm in pool if states.get(vm["id"]) in ACTIVE]
    spend = spend_of(boat, pool, since)
    state.data["spend"] = round(spend, 4)
    if spend >= cap:
        reason = f"{CAP_PREFIX} ${spend:.2f} reached ${cap:g}"
        state.switch_off(reason)
        stop_all(boat, pool, states, log, "spend cap")
        log(f"{reason}: stopped the pool and switched scaling off; raise scale.spend_cap_usd, then `jass board scale --on`")
        return f"off ({reason})"
    if spend >= warn and not state.data["warned"]:
        state.data["warned"] = True
        log(f"warning: pool spend ${spend:.2f} passed ${warn:g} (cap ${cap:g})")

    ready, claimed, busy_boxes = demand(board, chans)
    tasks = len(ready) + claimed
    actions = []

    idle_s = float(s.get("idle_stop_minutes", 15)) * 60
    for vm in running:
        if vm["box"] and vm["box"] in busy_boxes:
            problem = boat.extend(vm["id"], ttl_s)
            if problem:
                log(f"extending {vm['id']} failed: {problem}")
        if tasks:
            state.data["idle_since"].pop(vm["id"], None)
            if states.get(vm["id"]) in UP:
                # Idempotent: a running worker is left alone, and a VM whose resume or worker start went wrong in an
                # earlier tick gets its worker now instead of billing without one until its auto-stop.
                problem = boat.start_worker(vm["id"], start_command)
                if problem:
                    log(f"starting the worker on {vm['id']} failed: {problem}")
            continue
        idle_from = state.data["idle_since"].setdefault(vm["id"], now)
        if now - idle_from >= idle_s:
            problem = boat.stop(vm["id"])
            log(f"stopped {vm['id']}: running with no task for {idle_s / 60:g} minutes" + (f" (failed: {problem})" if problem else ""))
            actions.append(f"stopped {vm['id']}")
            state.data["idle_since"].pop(vm["id"], None)
    for vm_id in list(state.data["idle_since"]):
        if vm_id not in {vm["id"] for vm in running}:
            state.data["idle_since"].pop(vm_id)

    # Ready tasks that a VM was already started for, and that are still ready with every VM stopped again, are tasks the
    # VMs cannot take (meant for another profile, or a worker that fails at start). They wake no VM again; new tasks do.
    offered = set(state.data["offered"]) & set(ready)
    state.data["offered"] = sorted(offered)
    fresh = [task for task in ready if task not in offered]
    if running:
        # Running VMs take offered tasks as they free up; only claimed and new tasks call for more VMs.
        count = claimed + len(fresh)
    elif claimed or fresh:
        count = claimed + len(ready)
    else:
        count = 0
    held = sorted(offered) if ready and not fresh and not claimed and not running else []
    if held and held != state.data.get("held"):
        log(f"not starting a VM: the {len(held)} ready tasks were offered to a VM that stopped without claiming them; check them on the board")
    state.data["held"] = held
    wanted = min(len(pool), math.ceil(count / max(1, int(s.get("per_vm", 2)))))
    missing = wanted - len(running)
    for vm in [vm for vm in pool if states.get(vm["id"]) in STOPPED][:max(0, missing)]:
        if state.starts_today(today) >= int(s.get("max_starts_per_day", 30)):
            log(f"not starting {vm['id']}: {state.starts_today(today)} starts today reached scale.max_starts_per_day")
            break
        # A resume can take many minutes, so check the switch and the spend again right before each one.
        if state.off():
            log(f"not starting {vm['id']}: scaling was switched off")
            break
        spend = spend_of(boat, pool, since)
        if spend >= cap:
            log(f"not starting {vm['id']}: spend ${spend:.2f} reached the cap; the next tick stops the pool")
            break
        state.count_start(today)
        state.save()
        log(f"starting {vm['id']} for {len(ready)} ready and {claimed} claimed tasks")
        problem = boat.resume(vm["id"], ttl_s)
        if problem:
            log(f"resuming {vm['id']} failed: {problem}; the next tick tries again")
            continue
        state.data["offered"] = sorted(set(state.data["offered"]) | set(ready))
        problem = boat.start_worker(vm["id"], start_command)
        log(f"started the worker on {vm['id']}" if not problem else f"starting the worker on {vm['id']} failed: {problem}; the next tick tries again")
        actions.append(f"started {vm['id']}")
    return (f"{len(ready)} ready, {claimed} claimed, {len(running)} of {len(pool)} running, spend ${spend:.2f}"
            + (f"; {', '.join(actions)}" if actions else ""))


# --- command -------------------------------------------------------------------------------------


def run(cfg: dict, args) -> int:
    s = settings(cfg)
    folder = paths.private_dir(scale_dir())
    log = Logger(folder / f"scale-{dt.date.today().isoformat()}.log")
    if args.off or args.on:
        # No lock: the switch is its own file, and a running tick reads it again before each resume.
        state = State(folder / "state.json")
        if args.off:
            if not state.off().startswith(CAP_PREFIX):  # the cap's reason keeps its repeated stops going
                state.switch_off("jass board scale --off")
        else:
            with paths.Lock(folder / "lock", wait=True):
                state = State(folder / "state.json")
                state.switch_on()
                state.save()
        log("scaling switched " + ("off" if args.off else "on"))
        return 0
    if args.status:
        return status(cfg, s, State(folder / "state.json"), folder)
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
    print(f"enabled: {bool(s.get('enabled'))}" + (f"; off: {state.off()}" if state.off() else ""))
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
