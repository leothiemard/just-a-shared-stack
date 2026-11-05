# just-a-stack

Twenty portable skills shared between two independent machines. Borrowed workflows follow their upstream sources, with small documented compatibility patches. pstack-t3, Impeccable and other plugins remain independently maintained. The GitHub repository and T3 project are named `just-a-stack`. Existing checkouts can keep their `agent-skills` folder name so installed links continue working. Each machine keeps its own agents, credentials and project configuration. Company-specific workflows live separately on the work machine.

| Skill | Purpose |
| --- | --- |
| diagnosing-bugs | Build a red-capable feedback loop, reproduce and minimize, then diagnose and fix. |
| blast-radius | Trace contract and downstream risks beyond a diff. |
| council | Get review or feedback from three providers, with scored code reviews and further rounds as needed. |
| handoff | Save a concise conversation handoff in the OS temporary directory. |
| grilling | Explore the complete decision tree with the user before acting. |
| retro | Recommend improvements to the coding environment from a session retrospective. |
| sol-loop | Ask Sol for bounded review and fix confirmed issues. |
| full-stop | Finish, prove, review and prepare a PR for the user to merge. |
| full-send | The same workflow, with explicit authority to merge after checks and review. |
| file-pr | Open a concise PR describing the problem, solution and proof. |
| babysit-pr | Follow review and CI with the host watcher or the bundled non-T3 watcher. |
| leaving-pr-comments | Attribute agent-authored GitHub comments and verify the posted result. |
| writing-for-agents | Write skills and instructions using Matt's full guide and mechanics reference. |
| create-verification-skill | Generate and exercise a project-local verifier and feature map. |
| sanity-best-practices | Sanity schemas, queries, Studio and integrations. |
| content-modeling-best-practices | Structured content architecture. |
| portable-text-conversion | Import HTML and Markdown into Portable Text. |
| portable-text-serialization | Render and export Portable Text. |
| tailwind-4-docs | Tailwind v4 guidance with an optional local docs cache. |
| seo-aeo-best-practices | Metadata, structured data and search visibility. |

## Everyday workflow

Ordinary requests stay ordinary. Use these explicit entry points when wanted:

| Say | Result |
| --- | --- |
| "sol loop" | One Sol reviewer; three rounds by default, up to five for the upstream high-risk cases or at the user's request. |
| "full stop" | Complete the change through a reviewed PR, then leave it unmerged. |
| "full send" | Complete the same workflow and merge only after proof, review and required checks pass. |
| "council" | Three providers review the request; collect findings, disagreements and code-review scores. |

Theo's workflows and their PR/comment helpers are preserved from tstack, including their review budgets, severity policy and task-scoped testing permission. Full send and full stop authorize needed test methods even when a project normally asks first; higher-priority host/system constraints still apply. Production changes and data deletion remain outside that grant. Council uses Theo's three-provider feedback workflow with Astra preferred for OpenAI; the former custom exact-commit gate and snapshot tooling have been removed.

pstack remains independently installed. Lauren's blast-radius workflow calls its `how`, `why`, `arena` and `unslop` companions, and her verifier generator offers its maintenance skill. Those calls use the `pstack:` prefix here. Install pstack when using those companion workflows. In T3, `session hook: off` in its saved model sheet keeps automatic poteto-mode routing off; this per-machine preference is not changed by the installer.

Knowledge skills still load by topic, including Impeccable for UI work. Blast-radius again covers general downstream impact. The verifier generator writes a project-local skill for the active harness. Matt's debugging, grilling, handoff and retrospective workflows retain their original gates, output contracts and references. No separate custom variants are retained.

Manual cases in [evals/routing.json](evals/routing.json), [evals/sol-loop.json](evals/sol-loop.json), [evals/delivery.json](evals/delivery.json) and [evals/council.json](evals/council.json) record intended decisions. They do not establish better defect detection or prove live forge operations. [UPSTREAM.md](UPSTREAM.md) records the restoration decisions and the complete remaining compatibility patches.

## Set up either machine

Requires Git and Python 3.10+. Authenticate Git with this repository's personal GitHub account through your normal credential flow, then:

```sh
git clone https://github.com/leothiemard/just-a-stack.git ~/Development/just-a-stack
cd ~/Development/just-a-stack
python3 scripts/install.py
python3 scripts/install.py --apply
python3 scripts/install.py --check
```

The first command previews changes. If selected skill names already exist, inspect them and rerun with `--apply --replace`. Existing folders or symlinks move to a timestamped backup beside the skill root. The installer links only the names in `skills.json`; unrelated skills stay in place. It does not edit global instruction files, credentials or agent configuration.

The default root is `~/.agents/skills`, supported by [Codex](https://developers.openai.com/codex/skills) and [OpenCode](https://opencode.ai/docs/skills/). For local [Claude Code](https://code.claude.com/docs/en/skills), run the same installer with `--root ~/.claude/skills`. Avoid maintaining different copies of the same skill across roots. Restart an agent session if its skill list has not refreshed. This setup installs local skills; hosted sessions have their own configuration.

Every changed installation prints a restore manifest path. To undo that installation:

```sh
python3 scripts/install.py --restore /path/printed/by/installer/manifest.json
```

Restoration first verifies that its links have not been replaced. If you edited skill files through a symlink, those edits live in this checkout; restoring the previous folders does not discard the checkout.

## Existing checkouts after the rename

Run this inside each existing checkout on both machines:

```sh
git remote set-url origin https://github.com/leothiemard/just-a-stack.git
git pull --ff-only
python3 scripts/install.py --check
python3 scripts/install.py --root ~/.claude/skills --check
```

Keep the checkout at its existing path. Installed skills point to that folder, so the repository rename needs no reinstall. A later folder move needs a separate link migration.

## Daily use on two machines

Pull this repo before starting work or editing a shared skill:

```sh
git pull --ff-only
python3 scripts/install.py --check
```

Make a skill change on one machine, review the diff, commit and push it, then pull on the other. If both machines have local changes, resolve them in Git. The symlinks expose changes immediately; no copy or reinstall is needed unless the skill list changes. Use the upstream checker below to review new source versions; this repo does not auto-update or overwrite adaptations.

Each project remains a separate checkout on each machine. Give a task one active implementation owner at a time. To move it, commit/push intended code and use `handoff` with the repository, branch, revision, verification and portable evidence references. Uncommitted work, local processes, auth and scratch files do not transfer automatically. Work-only skills and context stay on the work machine.

Tailwind documentation downloads are optional and remain local. See that skill for official-docs fallback and explicit cache initialization. No cache is downloaded by the installer.

## Check borrowed upstream changes

From this checkout on either machine:

```sh
python3 scripts/check_upstream.py
```

The checker uses Git and Python's standard library to fetch public upstream content into temporary bare repositories. It compares selected scopes against their last-reviewed commits in `sources.json`, including skill resources, new files and borrowed references. It never checks out or runs upstream code, edits installed skills, or advances review baselines.

Open the printed `report.md` path for a summary and links to full upstream patches. `report.json` contains the same results for agents. Reports stay in the ignored `.local/upstream/` directory. A missing or moved source is flagged; a failed fetch is reported as an error, with exit code 1, rather than being called current. Completed checks exit 0 even when changes need review.

To check one source or choose a new output directory:

```sh
python3 scripts/check_upstream.py --source pstack
python3 scripts/check_upstream.py --source mattpocock --output /tmp/skills-upstream-review
```

Existing output directories are never overwritten. The default check covers the mapped pstack, Matt Pocock and tstack scopes. The Sanity and Tailwind reference packages retain their original folder hashes; those are not verified Git commits and need separate baseline work before update tracking can be added.

Compare each changed source with the mapped local adaptation and adopt useful parts selectively. When a scope has been fully assessed, update its `update_tracking.items[].reviewed_commit` to the captured upstream commit, including when you deliberately decline the change. Record the decision in the commit message, validate affected skills, then commit/push and pull on the other machine. Leave unreviewed scopes pinned. Keep the original `audit_commit` as historical provenance.

This checks net changes within the mapped scopes, not every new skill or change elsewhere in the source repositories. “Unchanged” means the upstream scope matches its reviewed baseline; it does not mean our personal adaptation is identical to upstream. Checks run only when invoked; no schedule is installed.

## Maintenance

Run `python3 -m unittest discover -s tests -v` after changing scripts. Validate edited skills with the active harness's skill validator and check their reference links. Keep this repository portable: no employer code, issue history, internal URLs, account configuration, credentials or generated documentation snapshots.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), [sources.json](sources.json) and [UPSTREAM.md](UPSTREAM.md) for origins, pins and compatibility patches.
