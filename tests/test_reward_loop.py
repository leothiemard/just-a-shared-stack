import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "reward-loop" / "scripts" / "reward.py"


class RewardLoopTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.write(self.repo, "value.txt", "10\n")
        (self.repo / "bench").mkdir()
        self.write(self.repo, "bench/eval.sh", "cat value.txt\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.counter = 0

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", "-C", str(cwd or self.repo), *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def write(self, base, name, content):
        (base / name).write_text(content, encoding="utf-8")

    def contract(self, **overrides):
        contract = {
            "eval_cmd": "sh bench/eval.sh",
            "direction": "max",
            "target": 30,
            "max_episodes": 20,
            "runs": 3,
            "protected": ["bench/"],
            "plateau_after": 2,
            "pivots": 1,
            **overrides,
        }
        self.write(self.run_dir, "contract.json", json.dumps(contract))

    def reward(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True, text=True,
        )

    def ok(self, *args):
        result = self.reward(*args)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def freeze(self, **overrides):
        self.contract(**overrides)
        return self.ok("freeze", "--run", str(self.run_dir), "--repo", str(self.repo))

    def candidate(self, value=None, edits=None):
        self.counter += 1
        path = self.root / f"candidate{self.counter}"
        self.git("worktree", "add", "-q", "--detach", str(path), self.best_sha())
        for name, content in (edits or {"value.txt": f"{value}\n"}).items():
            self.write(path, name, content)
        self.git("add", ".", cwd=path)
        self.git("commit", "-qm", f"episode {self.counter}", cwd=path)
        return path

    def best_sha(self):
        return self.git("rev-parse", "HEAD")

    def score(self, path):
        return self.ok(
            "score", "--run", str(self.run_dir), "--candidate", str(path),
            "--hypothesis", "h", "--category", "c",
        )

    def promote(self, episode):
        return self.ok("promote", "--run", str(self.run_dir), "--episode", str(episode), "--repo", str(self.repo))

    def status(self):
        return self.ok("status", "--run", str(self.run_dir))

    def test_improvement_promotes_and_reaches_target(self):
        self.assertEqual(self.freeze()["baseline"], 10)
        result = self.score(self.candidate(30))
        self.assertEqual(result["verdict"], "improved")
        self.assertEqual(result["next"], "continue")
        self.assertEqual(self.promote(result["episode"])["next"], "stop:target")
        self.assertEqual((self.repo / "value.txt").read_text().strip(), "30")

    def test_minimize_direction(self):
        self.freeze(direction="min", target=5)
        self.assertEqual(self.score(self.candidate(20))["verdict"], "rejected")
        self.assertEqual(self.score(self.candidate(4))["verdict"], "improved")

    def test_score_inside_noise_margin_is_rejected(self):
        self.freeze(noise_margin=5)
        self.assertEqual(self.score(self.candidate(14))["verdict"], "rejected")

    def test_protected_change_scores_invalid_even_with_high_score(self):
        self.freeze()
        result = self.score(self.candidate(edits={"bench/eval.sh": "echo 99\n"}))
        self.assertEqual(result["verdict"], "invalid")
        self.assertIn("reward hacking", result["reason"])

    def test_failed_gate_scores_invalid(self):
        self.freeze(gate_cmd="test $(cat value.txt) -lt 50")
        self.assertEqual(self.score(self.candidate(60))["verdict"], "invalid")

    def test_uncommitted_candidate_scores_invalid(self):
        self.freeze()
        path = self.candidate(20)
        self.write(path, "value.txt", "25\n")
        result = self.score(path)
        self.assertEqual(result["verdict"], "invalid")
        self.assertIn("uncommitted", result["reason"])

    def test_non_numeric_output_scores_invalid(self):
        self.freeze()
        result = self.score(self.candidate("not-a-number"))
        self.assertEqual(result["verdict"], "invalid")

    def test_evaluator_timeout_kills_the_command_tree(self):
        self.freeze(timeout_seconds=1, eval_cmd="if grep -q slow value.txt; then sleep 30; fi; echo 1")
        path = self.candidate("slow")
        result = self.score(path)
        self.assertEqual(result["verdict"], "invalid")
        self.assertIn("timed out", result["reason"])

    def test_changed_contract_stops_the_run(self):
        self.freeze()
        self.contract(target=1)
        self.assertEqual(self.reward("status", "--run", str(self.run_dir)).returncode, 2)

    def test_changed_frozen_evaluator_stops_the_run(self):
        (self.run_dir / "frozen").mkdir()
        self.write(self.run_dir / "frozen", "eval.sh", "cat value.txt\n")
        self.freeze(eval_cmd='sh "$REWARD_RUN/frozen/eval.sh"')
        self.write(self.run_dir / "frozen", "eval.sh", "echo 99\n")
        self.assertEqual(self.reward("status", "--run", str(self.run_dir)).returncode, 2)

    def test_second_improved_episode_in_a_generation_becomes_stale(self):
        self.freeze()
        first = self.score(self.candidate(20))
        second = self.score(self.candidate(25))
        self.promote(second["episode"])
        refused = self.reward("promote", "--run", str(self.run_dir), "--episode", str(first["episode"]), "--repo", str(self.repo))
        self.assertEqual(refused.returncode, 2)

    def test_rejected_episode_cannot_be_promoted(self):
        self.freeze()
        rejected = self.score(self.candidate(5))
        refused = self.reward("promote", "--run", str(self.run_dir), "--episode", str(rejected["episode"]), "--repo", str(self.repo))
        self.assertEqual(refused.returncode, 2)

    def test_rescoring_a_commit_returns_the_recorded_result(self):
        self.freeze()
        path = self.candidate(20)
        first = self.score(path)
        again = self.score(path)
        self.assertTrue(again["already_scored"])
        self.assertEqual(again["episode"], first["episode"])
        self.assertEqual(self.status()["episodes"], 1)

    def test_plateau_pivots_then_stops_at_local_maximum(self):
        self.freeze()
        seen = [self.score(self.candidate(5))["next"] for _ in range(2)]
        self.assertEqual(seen, ["continue", "pivot"])
        self.assertEqual(self.ok("pivot", "--run", str(self.run_dir))["next"], "continue")
        seen = [self.score(self.candidate(5))["next"] for _ in range(2)]
        self.assertEqual(seen, ["continue", "stop:local-maximum"])

    def test_pivot_stays_pending_until_acknowledged(self):
        self.freeze(plateau_after=2, pivots=5)
        for _ in range(3):
            self.score(self.candidate(5))
        self.assertEqual(self.status()["next"], "pivot")

    def test_pivot_is_refused_without_a_plateau(self):
        self.freeze()
        self.assertEqual(self.reward("pivot", "--run", str(self.run_dir)).returncode, 2)

    def test_plateau_fires_when_a_parallel_generation_jumps_past_the_boundary(self):
        self.freeze(plateau_after=3, pivots=2)
        for _ in range(2):
            self.score(self.candidate(5))
        self.assertEqual(self.status()["next"], "continue")
        for _ in range(2):
            self.score(self.candidate(5))
        self.assertEqual(self.status()["next"], "pivot")

    def test_promotion_resets_the_plateau(self):
        self.freeze()
        for _ in range(2):
            self.score(self.candidate(5))
        self.assertEqual(self.status()["next"], "pivot")
        self.promote(self.score(self.candidate(20))["episode"])
        self.assertEqual(self.score(self.candidate(5))["next"], "continue")
        self.assertEqual(self.score(self.candidate(5))["next"], "pivot")

    def test_rescoring_a_promoted_commit_keeps_its_result(self):
        self.freeze()
        path = self.candidate(20)
        episode = self.score(path)["episode"]
        self.promote(episode)
        again = self.score(path)
        self.assertEqual(again["verdict"], "improved")
        self.assertEqual(self.status()["episodes"], 1)

    def test_budget_stops_the_run(self):
        self.freeze(max_episodes=1)
        self.assertEqual(self.score(self.candidate(5))["next"], "stop:budget")

    def test_holdout_required_before_stopping_at_target(self):
        self.freeze(holdout_cmd="cat value.txt")
        self.promote(self.score(self.candidate(30))["episode"])
        self.assertEqual(self.status()["next"], "holdout")
        result = self.ok("holdout", "--run", str(self.run_dir), "--candidate", str(self.repo))
        self.assertTrue(result["holdout"]["improved"])
        self.assertEqual(result["next"], "stop:target")

    def test_overfit_checkpoint_keeps_climbing(self):
        self.write(self.repo, "holdout.txt", "7\n")
        self.git("add", ".")
        self.git("commit", "-qm", "holdout")
        self.freeze(holdout_cmd="cat holdout.txt")
        self.promote(self.score(self.candidate(30))["episode"])
        result = self.ok("holdout", "--run", str(self.run_dir), "--candidate", str(self.repo))
        self.assertFalse(result["holdout"]["improved"])
        self.assertEqual(result["next"], "continue")

    def test_measure_reports_baseline_without_freezing(self):
        self.contract()
        result = self.ok("measure", "--run", str(self.run_dir), "--repo", str(self.repo))
        self.assertEqual(result["score"], 10)
        self.assertFalse((self.run_dir / "state.json").exists())

    def test_ledger_records_every_episode_and_promotion(self):
        self.freeze()
        self.score(self.candidate(5))
        self.promote(self.score(self.candidate(20))["episode"])
        rows = (self.run_dir / "ledger.tsv").read_text().strip().splitlines()
        verdicts = [row.split("\t")[7] for row in rows[1:]]
        self.assertEqual(verdicts, ["rejected", "improved", "promoted"])


if __name__ == "__main__":
    unittest.main()
