---
name: reward-loop
description: Train on a hard goal through subagent episodes scored by a frozen reward. Use for "reward loop", or for hill-climbing a complex task against a measurable target. Starts with an interview that locks the goal, reward and loop.
---

# reward-loop

Run a training loop on a real task. A frozen evaluator pays the **reward**, fresh subagents run **episodes**, and only scored improvements become the next **checkpoint**. You are the trainer: you own the contract, the evaluator and the ledger. Subagents write every attempt, and their own numbers are advisory.

The structure does the work. The training vocabulary keeps everyone thinking about the score, but only `scripts/reward.py` decides what counts.

Set these once and reuse them:

```sh
REWARD="<this skill's directory>/scripts/reward.py"   # Python 3.9+, stdlib only
RUN="$(git rev-parse --path-format=absolute --git-common-dir)/reward-loop/<slug>"
```

`$RUN` sits inside the Git directory, so every worktree shares it and it stays out of commits. Run `python3 "$REWARD" --help` for the command and contract reference.

On resume, read `$RUN/contract.md` and `$RUN/lessons.md`, run `status`, and follow its `next`.

## 1. Survey

Find the facts yourself before asking anything: what the target is, which commands could score it, which tests could gate it, and what the current result is. Run `mkdir -p "$RUN/frozen"`, draft `$RUN/contract.json`, then run `measure` on the current code to learn the baseline and its noise.

The target must be a Git repo with a clean tree. If it is not, ask whether to initialize one or commit first. Leave the user's uncommitted work untouched.

**Done when** every contract field below has a recommended answer, backed by what you found.

## 2. Interview

Make the goal, reward and loop understandable to the user and to a stranger. Interview in rounds: ask every question whose prerequisites are settled, number them, give your recommendation, and word each so "yes" accepts it. Wait for answers, then ask what they unlocked. Decisions are the user's; facts are yours.

**Goal**

- What outcome is wanted, and why? Phrase it so an outsider could check it.
- What may change, and what is out of scope?
- What behavior must survive?

**Reward**

- Which single number, in which direction, printed by which command? Everything else that matters becomes a gate. If a command cannot compute it, use [judged rewards](references/judged-reward.md).
- What are the measured baseline, the noise, and the target?
- Which failures score zero?
- **Proxy test:** which high score would the user still reject? Turn each answer into a gate or a holdout.
- What is the holdout, a second check that episode briefs never mention? Recommend one whenever the evaluator uses a fixed sample that could be overfit.

**Loop**

- How many episodes, how much time, and which model?
- How many parallel episodes per generation? Recommend 1 for a narrow search and 2-3 when independent hypotheses are live.
- How many unpromoted episodes count as a plateau, and how many pivots before calling it a local maximum?
- Stop at the target, or keep climbing until the budget ends?
- What is the trainer allowed to do? Default: commit to `reward/<slug>/*` branches only, with no pushes, PRs or merges into the user's branches.

**Done when** no branch is left assumed and every answer is in the contract.

## 3. Lock

1. Put any evaluator wrapper in `$RUN/frozen/`. It is hashed at freeze, and commands reach it through `$REWARD_RUN`. List repo-side evaluator files, fixtures and tests under `protected`. Commands run in a fresh checkout, so include any setup they need, such as installing dependencies.
2. Write `$RUN/contract.md` and finalize `contract.json`. A stranger must be able to run the loop from `contract.md` alone, so include the exact commands, how the score is computed, what the interface under change is, what "promoted", "plateau" and "pivot" mean here, and what happens if the budget ends below target.
3. **Sensitivity check:** make a deliberately worse variant in a temporary worktree and `measure` it. The gate and evaluator must run from that fresh checkout, and the score must get worse by more than the noise. If not, fix the evaluator.
4. **Cold read:** dispatch the [cold-read brief](references/briefs.md#cold-read) to a fresh read-only subagent. Bring its ambiguities and cheapest hacks back to the user, and close each with a contract change or an explicit acceptance.
5. **Readback:** show the goal, reward and loop in three short paragraphs, plus the known ways to game the reward and the expected cost. Ask for an explicit yes.
6. After the yes: create and switch to `reward/<slug>/best`, then run `freeze --run "$RUN" --repo .`.

**Done when** the user approved this exact contract and `freeze` printed a baseline. Any later change to the contract means stopping and starting a new run.

## 4. Train

Repeat one generation at a time. Run `status` first and follow `next`:

| `next` | Action |
| --- | --- |
| `continue` | Run the generation below. |
| `holdout` | Run `holdout` on the best checkpoint, then `status` again. |
| `pivot` | Diagnose the plateau, run `pivot --run "$RUN"`, then run a generation. |
| `stop:*` | Go to step 5. |

A generation:

1. Read `lessons.md`. Choose one hypothesis per episode, up to the remaining budget. Each names a specific mechanism and a category, and differs from what the ledger already ruled out, or says what is different this time.
2. Dispatch one fresh subagent per hypothesis, in its own isolated checkout, using the [episode brief](references/briefs.md#episode).
   - T3: `delegate_task` with `isolation: "worktree"`, a new `clientRequestId`, and explicit effort options. Wait for the wake.
   - Claude Code: `Agent` with `isolation: "worktree"`.
   - Elsewhere: a fresh subagent per episode, with a worktree you create.
3. Score each returned candidate, **one at a time** so measurements do not disturb each other:
   ```sh
   python3 "$REWARD" score --run "$RUN" --candidate <checkout> --hypothesis "<text>" --category "<category>"
   ```
4. Promote the highest `improved` episode, from your `reward/<slug>/best` checkout:
   ```sh
   python3 "$REWARD" promote --run "$RUN" --episode <n> --repo .
   ```
   Use the `episode` number from the `score` result. Other improved episodes are now stale. Retry their ideas as new episodes on the new checkpoint.
5. Append one line per episode to `lessons.md`: what the result taught you about the mechanism. A worker that committed nothing has no candidate to score, so record its lesson only.

**Plateau.** Small adjustments have stopped paying, so explore:

1. Dispatch the [diagnosis brief](references/briefs.md#diagnosis) to a fresh read-only subagent. It names the limiting factor and untried categories.
2. Run the next generation in those categories. Prefer structural changes.
3. A structural change may need several episodes before it pays. Run it as a **bet**: each episode builds on the previous bet commit, and the score can dip along the way. Only a bet commit that beats the best checkpoint gets promoted. Abandon the bet at the next `pivot` or `stop:*`.

**Integrity.** Reward comes only through the goal. An `invalid` result for a protected-path change is **reward hacking**: discard that line of work and include it in the report. If `score` reports that the contract changed, stop and tell the user.

**Done when** `status` returns `stop:*`.

## 5. Report

Run `measure` from your `reward/<slug>/best` checkout. It must reproduce the best score, which proves the result lives in the commits and not in ignored files in a worker's checkout. Run `holdout` too if the contract has one and it has not been run there. Clean up finished worktrees you created, and list any left behind.

Report:

- the goal, and baseline to best score against the target, saying plainly if the target was missed
- the holdout result
- episodes promoted, rejected and invalid
- one line per promoted change
- approaches ruled out, and any reward hacking
- the `reward/<slug>/best` branch and the `$RUN` path
- the best idea to try next

Merging and pushing stay with the user. Treat a holdout that disagrees with the score as overfitting, and report it that way even if the target was met.
