# Judged rewards

Use this when no command can compute the score, as with visual design, prose or UX. Judged rewards are the easiest to game, so keep the judges blind and the rubric frozen.

## Set up during the interview

1. Agree on 3-6 criteria with the user. Anchor each with what a 1, a 3 and a 5 look like, using real examples where possible.
2. Save the rubric as `$RUN/frozen/rubric.md`, along with a copy of the baseline artifact.
3. Make the evaluator read a stored verdict. Save this as `$RUN/frozen/eval.sh`:
   ```sh
   #!/bin/sh
   cat "$REWARD_RUN/judgements/$(git rev-parse HEAD).txt"
   ```
   In the contract, use `"eval_cmd": "sh \"$REWARD_RUN/frozen/eval.sh\""`, `"runs": 1`, and an explicit `noise_margin` of at least 0.5 on a five-point scale.
4. Leave the check command out of episode briefs. Workers have no score to read, so they test against the rubric text instead.

## Judge each commit

Before any `measure`, `freeze` or `score` of a commit:

1. Dispatch 3 fresh read-only judges, on different models when available. Each gets the rubric, the baseline artifact and the candidate artifact, in random order under neutral labels. Hide the hypothesis, the worker's report and all earlier scores.
2. Each judge scores every criterion with evidence, and returns one overall score for the candidate.
3. Write the median to `$RUN/judgements/<sha>.txt`.

Then call the script as usual.

## Holdout

Show the user before and after at each promotion when the taste call is theirs, or use a judge panel with a rubric workers never see. At the final report, always put both artifacts in front of the user.
