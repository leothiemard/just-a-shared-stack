# Onboarding

How to adopt just-a-stack for yourself. [ARCHITECTURE.md](ARCHITECTURE.md) explains the design in more depth.

## The idea in five sentences

The **stack** (this public repository) holds how your agents work: skills, a short block of global rules, and the `jas` CLI. The **brain** (a private git repository of Markdown that you create) holds what your agents know: project notes, cross-project lessons and digests of past sessions. Every machine (**box**) links the stack's skills, carries the rules block in its global instruction file, and keeps a clone of the brain. Agents read the brain on demand with `jas hub` and `jas recall`, record into it with the `brain` skill, and `jas sync` shares it between boxes. Work and personal knowledge live in separate brains (**zones**), and `jas` refuses to mix them.

## Requirements

Git, Python 3.9 or newer with SQLite FTS5 (macOS system Python, python.org and Homebrew builds have it), macOS or Linux. Nothing runs in the background.

## Set up the first box

1. Fork or clone this repository. If you fork it, change the clone URL in `bootstrap.sh`.
2. Create an empty private repository for your brain on your git host, for example `brain-personal`.
3. Preview, then apply:

   ```sh
   git clone https://github.com/<owner>/just-a-stack.git ~/Development/just-a-stack
   ~/Development/just-a-stack/bin/jas init --box mac --zone personal --brain <brain-git-url>
   ~/Development/just-a-stack/bin/jas init --box mac --zone personal --brain <brain-git-url> --apply
   ```

   The preview prints one row per change (`add`, `update`, `keep`, `conflict`, `skip`) and changes nothing. `--apply` writes `~/.config/just-a-stack/box.json`, links the skills into `~/.agents/skills` and `~/.claude/skills`, links `~/.local/bin/jas`, writes the rules block into `~/.agents/AGENTS.md` (and links or updates `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`), clones the brain and creates its skeleton, then runs `jas doctor`. It prints a restore manifest path.

4. If `jas doctor` says `~/.local/bin` is not on `PATH`, add the printed line to your shell profile.
5. Run `jas sync` to push the brain skeleton.

No git host yet? `jas init --new-brain` creates a local-only brain; add a remote later with `jas init --brain <url> --apply`.

Already have notes in the same `Projects/<project>/` shape (an Obsidian vault, say)? Add `--import-notes <vault-folder>`. It copies `Projects/` and loose top-level `.md` files into the brain, never overwrites a file, and never changes the source.

Existing skills or files with the same names are reported as conflicts. Inspect them, then rerun with `--apply --replace`; replaced content is backed up and listed in the manifest. Text you wrote in `~/.agents/AGENTS.md` or `~/.claude/CLAUDE.md` outside the markers is kept byte for byte.

## Add another box

On the new machine:

```sh
curl -fsSL https://raw.githubusercontent.com/<owner>/just-a-stack/main/bootstrap.sh | sh -s -- --box linux-box --zone personal --brain <brain-git-url> --apply
```

`bootstrap.sh` checks for git and Python, clones the stack to `~/Development/just-a-stack` (or `$JAS_STACK_DIR`), and runs `jas init` with your arguments. Drop `--apply` to preview first. Then run `jas sync`.

## Keep a box current

```sh
jas update           # preview
jas update --apply   # pull the stack and layers, relink skills, refresh the rules block, run doctor
jas sync             # share the brain
```

## Daily use

You mostly do nothing. The rules block tells agents to:

- run `jas hub` when they start substantial work in a project, to read its hub note;
- run `jas recall "<query>"` when they need history, an earlier decision, something from another project or box, or when a tool keeps failing the same way;
- save decisions, root causes, gotchas and setup facts with the `brain` skill at the end of substantial work, then `jas sync`.

Things you can ask for:

- "What did we decide about the deploy hook last month?" or "Have we seen this error before?" (recall)
- "Remember that preview deploys ignore env vars added after the build starts." (a lesson)
- "Log what we did today." (a project log entry)
- "Brain review." (see below)

Run `jas sync` whenever you want the other boxes to see new knowledge; agents also run it after recording. `jas recall` syncs on its own when the brain is stale.

Commands you may use yourself: `jas doctor` (health and context budget), `jas sessions list --since 2w`, `jas sessions stats --by project` (where the tokens went), `jas path project`, `jas show <n>` after a recall.

## Add a skill

1. Create `skills/<name>/SKILL.md` with `name` and `description` frontmatter. Follow the `writing-for-agents` skill.
2. Add `<name>` to `skills.json`.
3. Commit and push, then `jas update --apply` on each box. Existing skills update through their links without any command.

## Add a rule

Edit `rules/global.md`. Keep it short: every byte loads in every session, and `jas doctor` warns past 2,000 bytes for the whole file. Commit, push, then `jas update --apply` on each box. Never edit the block between the `jas:begin` and `jas:end` markers by hand; `jas update` replaces it. A marker counts only on a line of its own at column zero outside code fences and indented code, so examples of the markers elsewhere in the file are left alone.

## Add a private layer

For a work machine with company skills and rules:

1. Create a private repository shaped like this one: `skills.json`, `skills/<name>/SKILL.md`, and optionally `rules/global.md`. Skill names must differ from the stack's.
2. Clone it on the work box and point the box at a separate work brain:

   ```sh
   jas init --box work-laptop --zone work --brain-path ~/brain-work --brain <work-brain-url> --layer ~/Development/company-layer --apply
   ```

The layer's skills are linked next to the stack's, and its rules are appended to the managed block. Personal boxes never list the layer, and the zone guard keeps the two brains apart.

## Run a review

Every week or two, say "brain review". The `brain` skill reads recent session digests and logs, turns recurring friction into lessons, trims hubs over budget, writes `Reviews/<year>-W<week>.md` and lists proposed stack changes for you to accept or reject. It never edits the stack itself.

## Roll back

Every `jas init --apply` and `jas update --apply` that changed something prints a manifest path. To undo that one transaction:

```sh
jas restore ~/.config/just-a-stack/backups/<stamp>/manifest.json
```

It first checks that nothing it wrote has changed since; if something has, it restores nothing and tells you which file to look at. An `--apply` that failed midway prints the same command in its error; restore then undoes the steps that ran. The brain clone runs before any change outside the brain, so a clone that fails leaves the box as it was. The brain is never touched by a restore: undo brain changes with git in the brain folder (`jas path brain`).

The skills step keeps a manifest of its own next to each skill root: `~/.agents/skill-backups/<stamp>/manifest.json` and `~/.claude/skill-backups/<stamp>/manifest.json`, with the backups of any replaced skills. `jas restore` undoes it through the `jas` manifest once the step has finished. If the process was killed during the skills step, the `jas` manifest does not list it yet; undo the links it made with the newest of those manifests, the one whose `status` is still `installing`, then run `jas restore` for the rest:

```sh
python3 "$(jas path stack)/scripts/install.py" --restore ~/.agents/skill-backups/<stamp>/manifest.json
```
