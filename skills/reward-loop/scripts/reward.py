#!/usr/bin/env python3
"""Frozen scorer for a reward loop. The trainer runs it; episode workers do not.

Commands, in the order a run uses them:
  measure   Score a checkout without recording anything (baseline, noise, sensitivity).
  freeze    Measure the baseline and lock the contract. Run after the user approves it.
  score     Validate and score one committed candidate. Appends to ledger.tsv.
  promote   Fast-forward the best branch to an improved episode.
  holdout   Run the holdout command on the best checkpoint.
  pivot     Acknowledge a plateau after diagnosing it.
  status    Print the run state and the next action.

$RUN/contract.json fields:
  eval_cmd         required. Run in the checkout; its last stdout line must be the score.
  direction        required. "max" or "min".
  target           required. Score to reach.
  max_episodes     required. Episode budget.
  gate_cmd         optional. Must exit 0, or the candidate is invalid.
  holdout_cmd      optional. Prints a score like eval_cmd. Never named in episode briefs.
  protected        optional. Git pathspecs a candidate may not change.
  runs             optional, default 5. Runs per measurement; the median counts.
  noise_margin     optional. A candidate must beat the best by more than this.
                   Default: the spread of the baseline runs.
  plateau_after    optional, default 3. Unpromoted episodes before a pivot.
  pivots           optional, default 2. Pivots before stopping at a local maximum.
  stop_at_target   optional, default true.
  timeout_seconds  optional, default 1800. Limit per command.

Commands see $REWARD_RUN, so evaluator files can live in $REWARD_RUN/frozen/.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LEDGER_FIELDS = [
    "episode", "time", "sha", "category", "hypothesis",
    "score", "best_before", "verdict", "reason",
]
DEFAULTS = {
    "gate_cmd": None, "holdout_cmd": None, "protected": [], "runs": 5,
    "noise_margin": None, "plateau_after": 3, "pivots": 2,
    "stop_at_target": True, "timeout_seconds": 1800,
}
REQUIRED = ["eval_cmd", "direction", "target", "max_episodes"]


class Refused(Exception):
    """The requested action must not happen. The message says why."""


def emit(payload):
    print(json.dumps(payload, indent=2))


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path, data):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


def load_contract(run):
    path = run / "contract.json"
    if not path.exists():
        raise Refused(f"no contract at {path}")
    contract = {**DEFAULTS, **load_json(path)}
    missing = [field for field in REQUIRED if contract.get(field) is None]
    if missing:
        raise Refused(f"contract is missing: {', '.join(missing)}")
    if contract["direction"] not in ("max", "min"):
        raise Refused('contract direction must be "max" or "min"')
    return contract


def fingerprint(run):
    digest = hashlib.sha256((run / "contract.json").read_bytes())
    frozen = run / "frozen"
    if frozen.exists():
        for path in sorted(p for p in frozen.rglob("*") if p.is_file()):
            digest.update(str(path.relative_to(frozen)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def load_state(run):
    path = run / "state.json"
    if not path.exists():
        raise Refused("run is not frozen; run freeze after the user approves the contract")
    state = load_json(path)
    if fingerprint(run) != state["fingerprint"]:
        raise Refused(
            "the contract or frozen evaluator changed after freeze; "
            "stop and tell the user, a changed contract needs a new run"
        )
    return state


def shell(cmd, cwd, run, timeout):
    """Run cmd in its own process group so a timeout kills the whole tree."""
    env = {**os.environ, "REWARD_RUN": str(run)}
    proc = subprocess.Popen(
        cmd, shell=True, cwd=str(cwd), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        return None, out, err
    return proc.returncode, out, err


def git(cwd, *args):
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def is_ancestor(cwd, older, newer):
    return subprocess.run(
        ["git", "-C", str(cwd), "merge-base", "--is-ancestor", older, newer],
        capture_output=True,
    ).returncode == 0


def tail(text, limit=800):
    return text.strip()[-limit:]


def parse_score(stdout):
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        value = float(lines[-1])
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def measure_cmd(cmd, cwd, run, contract):
    """Return (median, samples, failure reason)."""
    samples = []
    for _ in range(int(contract["runs"])):
        code, out, err = shell(cmd, cwd, run, contract["timeout_seconds"])
        if code is None:
            return None, samples, f"evaluator timed out: {tail(err)}"
        if code != 0:
            return None, samples, f"evaluator exited {code}: {tail(err or out)}"
        value = parse_score(out)
        if value is None:
            return None, samples, f"last stdout line is not a finite number: {tail(out)}"
        samples.append(value)
    return statistics.median(samples), samples, None


def run_gate(cwd, run, contract):
    if not contract["gate_cmd"]:
        return None
    code, out, err = shell(contract["gate_cmd"], cwd, run, contract["timeout_seconds"])
    if code is None:
        return f"gate timed out: {tail(err)}"
    if code != 0:
        return f"gate exited {code}: {tail(err or out)}"
    return None


def beats(contract, score, reference, margin):
    sign = 1 if contract["direction"] == "max" else -1
    return sign * (score - reference) > margin


def reached(contract, score):
    sign = 1 if contract["direction"] == "max" else -1
    return sign * (score - contract["target"]) >= 0


def next_action(state, contract):
    """Name the trainer's next action from the state alone."""
    best = state["best"]
    since = state["episodes_since_promotion"]
    target_met = reached(contract, best["score"])
    if target_met and contract["stop_at_target"]:
        if not contract["holdout_cmd"]:
            return "stop:target"
        result = state.get("holdout")
        if not result or result["sha"] != best["sha"]:
            return "holdout"
        if result["improved"]:
            return "stop:target"
        # The checkpoint overfit, so the climb continues until a limit stops it.
    if state["episodes"] >= contract["max_episodes"]:
        return "stop:budget"
    plateau_after = contract["plateau_after"]
    if since >= plateau_after * (contract["pivots"] + 1):
        return "stop:local-maximum"
    if since // plateau_after > state["pivots_done"]:
        return "pivot"
    return "continue"


def status_payload(state, contract):
    best = state["best"]
    return {
        "next": next_action(state, contract),
        "baseline": state["baseline"]["score"],
        "best": best["score"],
        "best_sha": best["sha"],
        "target": contract["target"],
        "target_met": reached(contract, best["score"]),
        "noise_margin": state["noise_margin"],
        "episodes": state["episodes"],
        "max_episodes": contract["max_episodes"],
        "episodes_since_promotion": state["episodes_since_promotion"],
        "holdout": state.get("holdout"),
    }


def append_ledger(run, row):
    path = run / "ledger.tsv"
    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, LEDGER_FIELDS, delimiter="\t")
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def cmd_measure(args):
    contract = load_contract(args.run)
    gate_failure = run_gate(args.repo, args.run, contract)
    median, samples, failure = measure_cmd(contract["eval_cmd"], args.repo, args.run, contract)
    emit({
        "score": median,
        "samples": samples,
        "spread": max(samples) - min(samples) if samples else None,
        "gate": gate_failure or "pass",
        "failure": failure,
    })
    return 0 if failure is None and gate_failure is None else 1


def cmd_freeze(args):
    run = args.run
    if (run / "state.json").exists():
        raise Refused("run is already frozen; a changed contract needs a new run")
    contract = load_contract(run)
    if git(args.repo, "status", "--porcelain"):
        raise Refused("freeze needs a clean checkout")
    gate_failure = run_gate(args.repo, run, contract)
    if gate_failure:
        raise Refused(f"baseline fails its own gate: {gate_failure}")
    median, samples, failure = measure_cmd(contract["eval_cmd"], args.repo, run, contract)
    if failure:
        raise Refused(f"baseline did not score: {failure}")
    holdout_baseline = None
    if contract["holdout_cmd"]:
        holdout_baseline, _, failure = measure_cmd(contract["holdout_cmd"], args.repo, run, contract)
        if failure:
            raise Refused(f"holdout baseline did not score: {failure}")
    sha = git(args.repo, "rev-parse", "HEAD")
    margin = contract["noise_margin"]
    if margin is None:
        margin = max(samples) - min(samples)
    (run / "lessons.md").touch()
    state = {
        "fingerprint": fingerprint(run),
        "baseline": {"sha": sha, "score": median, "samples": samples},
        "holdout_baseline": holdout_baseline,
        "best": {"sha": sha, "score": median},
        "noise_margin": margin,
        "episodes": 0,
        "episodes_since_promotion": 0,
        "pivots_done": 0,
        "scored": {},
        "holdout": None,
    }
    save_json(run / "state.json", state)
    emit(status_payload(state, contract))
    return 0


def judge(candidate, run, state, contract, sha):
    """Return (score, verdict, reason) for a committed candidate."""
    baseline_sha = state["baseline"]["sha"]
    best = state["best"]
    if git(candidate, "status", "--porcelain"):
        return None, "invalid", "uncommitted changes; only commits are scored"
    if sha == best["sha"]:
        return None, "invalid", "no new commit on top of the best checkpoint"
    if not is_ancestor(candidate, best["sha"], sha):
        return None, "invalid", f"stale; does not descend from best {best['sha']}"
    if contract["protected"]:
        touched = git(candidate, "diff", "--name-only", baseline_sha, sha, "--", *contract["protected"])
        if touched:
            return None, "invalid", "reward hacking: changed protected paths: " + ", ".join(touched.splitlines())
    gate_failure = run_gate(candidate, run, contract)
    if gate_failure:
        return None, "invalid", gate_failure
    median, _, failure = measure_cmd(contract["eval_cmd"], candidate, run, contract)
    if failure:
        return None, "invalid", failure
    if fingerprint(run) != state["fingerprint"]:
        raise Refused("the contract or frozen evaluator changed during scoring; stop and tell the user")
    if git(candidate, "status", "--porcelain", "--untracked-files=no"):
        return None, "invalid", "evaluation changed tracked files"
    if beats(contract, median, best["score"], state["noise_margin"]):
        return median, "improved", ""
    return median, "rejected", "did not beat the best by more than the noise margin"


def cmd_score(args):
    run = args.run
    contract = load_contract(run)
    state = load_state(run)
    sha = git(args.candidate, "rev-parse", "HEAD")
    previous = state["scored"].get(sha)
    if previous:
        emit({**previous, "already_scored": True, **status_payload(state, contract)})
        return 0
    best_before = state["best"]["score"]
    score, verdict, reason = judge(args.candidate, run, state, contract, sha)
    state["episodes"] += 1
    state["episodes_since_promotion"] += 1
    episode = state["episodes"]
    entry = {
        "episode": episode, "sha": sha, "score": score,
        "verdict": verdict, "reason": reason,
    }
    state["scored"][sha] = entry
    save_json(run / "state.json", state)
    append_ledger(run, {
        "episode": episode, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "sha": sha,
        "category": args.category, "hypothesis": args.hypothesis,
        "score": "" if score is None else score, "best_before": best_before,
        "verdict": verdict, "reason": " ".join(reason.split()),
    })
    emit({**entry, "best_before": best_before, **status_payload(state, contract)})
    return 0


def cmd_promote(args):
    run = args.run
    contract = load_contract(run)
    state = load_state(run)
    entry = next((e for e in state["scored"].values() if e["episode"] == args.episode), None)
    if entry is None:
        raise Refused(f"episode {args.episode} has not been scored")
    if entry["verdict"] != "improved":
        raise Refused(f"episode {args.episode} is {entry['verdict']}, not improved")
    if not beats(contract, entry["score"], state["best"]["score"], state["noise_margin"]):
        raise Refused(f"episode {args.episode} no longer beats the best; retry the idea as a new episode")
    if not is_ancestor(args.repo, state["best"]["sha"], entry["sha"]):
        raise Refused(f"episode {args.episode} is stale; retry the idea on the new checkpoint")
    if git(args.repo, "status", "--porcelain"):
        raise Refused("promote needs a clean best-branch checkout")
    git(args.repo, "merge", "--ff-only", entry["sha"])
    state["best"] = {"sha": entry["sha"], "score": entry["score"]}
    state["episodes_since_promotion"] = 0
    state["pivots_done"] = 0
    save_json(run / "state.json", state)
    append_ledger(run, {
        "episode": args.episode, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "sha": entry["sha"],
        "category": "", "hypothesis": "", "score": entry["score"], "best_before": "",
        "verdict": "promoted", "reason": "",
    })
    emit(status_payload(state, contract))
    return 0


def cmd_holdout(args):
    run = args.run
    contract = load_contract(run)
    state = load_state(run)
    if not contract["holdout_cmd"]:
        raise Refused("the contract has no holdout")
    sha = git(args.candidate, "rev-parse", "HEAD")
    if sha != state["best"]["sha"]:
        raise Refused(f"holdout runs on the best checkpoint {state['best']['sha']}")
    median, _, failure = measure_cmd(contract["holdout_cmd"], args.candidate, run, contract)
    if failure:
        raise Refused(f"holdout did not score: {failure}")
    state["holdout"] = {
        "sha": sha, "score": median, "baseline": state["holdout_baseline"],
        "improved": beats(contract, median, state["holdout_baseline"], 0),
    }
    save_json(run / "state.json", state)
    emit(status_payload(state, contract))
    return 0


def cmd_pivot(args):
    contract = load_contract(args.run)
    state = load_state(args.run)
    if next_action(state, contract) != "pivot":
        raise Refused("status does not call for a pivot")
    state["pivots_done"] = state["episodes_since_promotion"] // contract["plateau_after"]
    save_json(args.run / "state.json", state)
    emit(status_payload(state, contract))
    return 0


def cmd_status(args):
    contract = load_contract(args.run)
    emit(status_payload(load_state(args.run), contract))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name, handler, *checkouts):
        sub = commands.add_parser(name)
        sub.add_argument("--run", type=lambda p: Path(p).resolve(), required=True)
        for option in checkouts:
            sub.add_argument(option, type=lambda p: Path(p).resolve(), required=True)
        sub.set_defaults(handler=handler)
        return sub

    add("measure", cmd_measure, "--repo")
    add("freeze", cmd_freeze, "--repo")
    score = add("score", cmd_score, "--candidate")
    score.add_argument("--hypothesis", required=True)
    score.add_argument("--category", required=True)
    add("promote", cmd_promote, "--repo").add_argument("--episode", type=int, required=True)
    add("holdout", cmd_holdout, "--candidate")
    add("pivot", cmd_pivot)
    add("status", cmd_status)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        return args.handler(args)
    except Refused as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
