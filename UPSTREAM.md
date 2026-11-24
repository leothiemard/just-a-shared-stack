# Upstream fidelity

On 2026-10-27, the user approved restoring the borrowed workflows instead of maintaining behavioral rewrites. Upstream behavior is the default. Names, supported models, harness APIs and portable paths may need small patches; budgets, gates, permissions and output contracts are deliberate behavior and must not change silently.

## Adopted sources

| Source | Adopted commit | Skills |
| --- | --- | --- |
| [Theo's tstack](https://github.com/t3dotgg/tstack) | `92e38e2cf677a84a142e8df296861c588542135c` | council, sol-loop, full-stop, full-send, file-pr, babysit-pr, leaving-pr-comments |
| [Matt Pocock's skills](https://github.com/mattpocock/skills) | `59db609eb1e18aaf59b63284d0e52255aff0c63c` | diagnosing-bugs, grilling, handoff, retro, writing-for-agents |
| [Lauren's pstack](https://github.com/cursor/plugins/tree/main/pstack) | `519d36aa98955bf680db01bcde5fdfba686dc901` | blast-radius, create-verification-skill |
| [Sanity agent-toolkit](https://github.com/sanity-io/agent-toolkit) | `828641ff3bae87327ec73844d9c72f38e591842e` | content-modeling-best-practices, portable-text-conversion, portable-text-serialization, sanity-best-practices, seo-aeo-best-practices |

`sources.json` maps each complete upstream folder to its local folder. Original audit identifiers and folder hashes remain historical provenance; reviewed commits advance only for the adopted folders. The Tailwind package was outside this restoration and retains its existing folder-hash provenance.

### Later adoptions

- 2026-10-28: `council` adopted `1085050f3baed1ac724caa4a8166145fb2ac88af` (resolve known issues and run checks before requesting an audit).
- 2026-11-23: `council` adopted `11e37a74d0a6868c121cd0910ab2b3bd9873b92a` (evaluate delegate findings as they arrive without editing during active reviews, and treat non-blocking delegates as feedback without recording a score); `grilling` adopted `abeeaf95660ba9e8b7c753eb38f441ca707a8823` (dispatch fact-finding sub-agents with a fresh context window); `blast-radius` adopted `7a583913eb1b8d1a0ee76753d1a2cf072e7b54f9` (drop two `why` cross-references in Steps 1 and 4); and Sanity's `sanity-best-practices` and `portable-text-conversion` were refreshed to `828641ff3bae87327ec73844d9c72f38e591842e` with Git update tracking enabled for all five Sanity skills. All other Theo, Matt, and Lauren folders retain the table pins above.

## Restoration decisions

- Replace the custom council with Theo's three-provider feedback and scored-review workflow. Remove the custom snapshot helper and CLI protocol.
- Restore Theo's review budgets, severity rules, testing-permission grant and PR/CI helper skills. Adopt the captured full-stop trigger, including closing emphasis, as part of the requested upstream workflow. Restore the non-T3 watcher unchanged; T3 continues to use its host watcher.
- Restore Matt's mandatory hard-bug feedback loop, reproduction/minimization gates and helper; exhaustive grilling and final confirmation; temporary-file handoff with suggested skills; broad retrospective; and complete writing/mechanics reference.
- Restore Lauren's general-impact review and project-local verifier generator with feature-map examples and maintenance guidance. Remove the custom Sanity specialization, personal-generator template and hybrid debugging/parity excerpts. No custom variants are retained.
- Keep upstream invocation metadata. `disable-model-invocation` and `argument-hint` are harness fields used by Claude Code; upstream `agents/openai.yaml` files preserve corresponding Codex policy where supplied.

## Complete local patches

On 2026-11-06, the user approved bounded reviews and lean-thread handoffs to
reduce review and cache-rebuild costs. Upstream pins are unchanged.

On 2026-11-24, the user chose to put per-task choices in prompts rather than
patches: `council` returned to upstream (its panel is the latest flagship of
each provider; round caps, a single follow-up reviewer and model pins are
prompted when wanted), and the Gemini Neon reviewer trial left `sol-loop`
(prompt "use Neon" to keep it). The lean-thread handoffs stay. PR
descriptions end with `file-pr`'s model-and-harness line again: the global
rules no longer forbid it, and only the no-AI `Co-Authored-By` rule for
commits remains.

Theo's five other folders, Matt's five folders, and Sanity's five folders are copied without local modifications (apart from historical trailing-whitespace normalization on the untouched `content-modeling-best-practices` and `seo-aeo-best-practices` folders). The complete patches are:

- [sol-loop](patches/tstack-sol-loop.patch): hand off the ledger before reviewer waits above ~150k context.
- [babysit-pr](patches/tstack-babysit-pr.patch): hand off PR state before watching above ~150k context.

- [blast-radius](patches/pstack-blast-radius.patch): qualify companion calls as `pstack:how`, `pstack:why`, `pstack:arena` and `pstack:unslop`; mirror its explicit-only invocation policy in Codex metadata.
- [create-verification-skill](patches/pstack-create-verification-skill.patch): resolve the project-local skill directory for Codex, Claude Code or Cursor; qualify the pstack maintenance companion; mirror its explicit-only invocation policy in Codex metadata. The output stays project-local, and its feature coverage and proof requirements are unchanged.

These patches contain every local difference, including added files. Apply them after copying the pinned upstream folders. Keep executable modes and bundled resources. Do not replace upstream invocation fields merely to satisfy a validator for upload-only metadata; validate supported harness fields separately from the common skill format.

## Future updates

Run `python3 scripts/check_upstream.py` (or `--source <name>` for one source) to inspect source changes. It fetches the mapped public scopes into temporary bare repositories, never runs upstream code or edits skills, and writes `report.md` and `report.json` under the ignored `.local/upstream/`. It exits 1 only when a fetch fails. The Tailwind folder has a folder hash, not a Git commit, so it is not checked. Compare the local folder with its pinned source plus its recorded patch as a separate fidelity check: an upstream-to-upstream result of “unchanged” says nothing about local fidelity. Review resources as well as SKILL.md, retain license notices, update affected patches and record adoption or decline before advancing reviewed commits.
