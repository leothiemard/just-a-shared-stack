# Briefs

Fill in every placeholder. A subagent sees only its brief. Leave the holdout out of all of them.

## Episode

```text
This is episode <n> of a reward loop. A frozen evaluator pays the reward, and only your committed result is scored. Your episode ends when you commit. A fresh agent continues from the best checkpoint.

Goal: <goal>
Reward: <what the score means and its direction>. Best so far: <score>. Target: <target>.
Your hypothesis: <specific mechanism and expected change>. Category: <category>.
Already ruled out: <relevant lessons>

Start in your own checkout: git switch -c reward/<slug>/e<n> <base sha>
Check your work with: <eval command, with REWARD_RUN=<run> if it uses it>
Must pass: <gate command>
May change: <scope>
Protected: <protected paths>

Earn the reward through the goal. Changing the evaluator, its inputs or its tests, special-casing what it measures, or hiding behavior it checks scores zero and gets reported.

Test your hypothesis with one coherent change, commit all of it, and leave the tree clean. If the hypothesis fails or the evaluator looks wrong, commit nothing and say why. That is a useful result.

Work alone: no questions to the user and no further subagents. Leave project notes, pushes and PRs to the trainer.

Final message, only these lines:
checkout: <absolute path>
branch: <branch>
sha: <full commit sha, or none>
changed: <one line>
measured: <your score, unverified>
lesson: <one line about the mechanism>
next: <the best idea this suggests>
```

## Cold read

```text
Read-only: change nothing, and leave project notes to the trainer. Read <run>/contract.md and use only what it says. It describes a reward loop that agents will run unattended.

Reply with:
1. The goal, the reward and the loop, each restated in one sentence.
2. Each term or decision two reasonable agents could read differently.
3. The three cheapest ways to get a high score without achieving the goal.
4. What the contract leaves unanswered that would block you mid-run.
```

## Diagnosis

```text
Read-only: change nothing, and leave project notes to the trainer. A reward loop has hit a plateau.

Goal: <goal>
Reward: <what the score means>. Baseline: <baseline>. Best: <best>. Target: <target>.
Ledger: <run>/ledger.tsv
Lessons: <run>/lessons.md
Code at the best checkpoint: <checkout>

Read the ledger and the code, and measure if it helps. Work from evidence.

Reply with:
1. What limits the score now, with file:line or measurement evidence.
2. Which assumption the attempts so far share.
3. Three untried hypothesis categories, ranked by likely gain, each with a specific mechanism. Say whether each needs a multi-episode bet.
```
