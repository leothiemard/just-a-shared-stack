# Upstream fidelity

On 2026-10-27, the user approved restoring the borrowed workflows instead of maintaining behavioral rewrites. Upstream behavior is the default. Names, supported models, harness APIs and portable paths may need small patches; budgets, gates, permissions and output contracts are deliberate behavior and must not change silently.

## Adopted sources

| Source | Adopted commit | Skills |
| --- | --- | --- |
| [Theo's tstack](https://github.com/t3dotgg/tstack) | `92e38e2cf677a84a142e8df296861c588542135c` | council, sol-loop, full-stop, full-send, file-pr, babysit-pr, leaving-pr-comments |
| [Matt Pocock's skills](https://github.com/mattpocock/skills) | `59db609eb1e18aaf59b63284d0e52255aff0c63c` | diagnosing-bugs, grilling, handoff, retro, writing-for-agents |
| [Lauren's pstack](https://github.com/cursor/plugins/tree/main/pstack) | `519d36aa98955bf680db01bcde5fdfba686dc901` | blast-radius, create-verification-skill |

`sources.json` maps each complete upstream folder to its local folder. Original audit identifiers remain historical provenance; reviewed commits advance only for the adopted folders. The six Sanity/Tailwind packages were outside this restoration and retain their existing provenance.

## Restoration decisions

- Replace the custom council with Theo's three-provider feedback and scored-review workflow. Remove the custom snapshot helper and CLI protocol.
- Restore Theo's review budgets, severity rules, testing-permission grant and PR/CI helper skills. Adopt the captured full-stop trigger, including closing emphasis, as part of the requested upstream workflow. Restore the non-T3 watcher unchanged; T3 continues to use its host watcher.
- Restore Matt's mandatory hard-bug feedback loop, reproduction/minimization gates and helper; exhaustive grilling and final confirmation; temporary-file handoff with suggested skills; broad retrospective; and complete writing/mechanics reference.
- Restore Lauren's general-impact review and project-local verifier generator with feature-map examples and maintenance guidance. Remove the custom Sanity specialization, personal-generator template and hybrid debugging/parity excerpts. No custom variants are retained.
- Keep upstream invocation metadata. `disable-model-invocation` and `argument-hint` are harness fields used by Claude Code; upstream `agents/openai.yaml` files preserve corresponding Codex policy where supplied.

## Complete local patches

Council additionally adopts `1085050f3baed1ac724caa4a8166145fb2ac88af` (2026-10-28): resolve known issues and run checks before requesting an audit. The other six Theo folders retain the table pin.

On 2026-11-06, the user approved bounded reviews and lean-thread handoffs to
reduce review and cache-rebuild costs. Upstream pins are unchanged.

Theo's four other folders and Matt's five folders are copied without local modifications. The complete patches are:

- [council](patches/tstack-council.patch): prefer GPT-6 Astra; bound reviews with findings, scores, plateaus and a 3-round cap; use one diff-only reviewer after round 1 and escalate contradictions.
- [sol-loop](patches/tstack-sol-loop.patch): hand off the ledger before reviewer waits above ~150k context.
- [babysit-pr](patches/tstack-babysit-pr.patch): hand off PR state before watching above ~150k context.

- [blast-radius](patches/pstack-blast-radius.patch): qualify companion calls as `pstack:how`, `pstack:why`, `pstack:arena` and `pstack:unslop`; mirror its explicit-only invocation policy in Codex metadata.
- [create-verification-skill](patches/pstack-create-verification-skill.patch): resolve the project-local skill directory for Codex, Claude Code or Cursor; qualify the pstack maintenance companion; mirror its explicit-only invocation policy in Codex metadata. The output stays project-local, and its feature coverage and proof requirements are unchanged.

These patches contain every local difference, including added files. Apply them after copying the pinned upstream folders. Keep executable modes and bundled resources. Do not replace upstream invocation fields merely to satisfy a validator for upload-only metadata; validate supported harness fields separately from the common skill format.

## Future updates

Run the existing upstream checker to inspect source changes. Compare the local folder with its pinned source plus its recorded patch as a separate fidelity check: an upstream-to-upstream result of “unchanged” says nothing about local fidelity. Review resources as well as SKILL.md, retain license notices, update affected patches and record adoption or decline before advancing reviewed commits.
