<p align="center"><img src="assets/logo.svg" width="128" height="128" alt="jass logo: a pixel-art stack of Jass cards with the ace of diamonds on top"></p>
<h1 align="center">jass</h1>
<p align="center"><b>Just a Shared Stack.</b> Every box is dealt the same hand.</p>

jass gives every machine you run coding agents on the same skills and global rules, and a shared memory of what those agents learned. One command sets up a machine. The name comes from Jass, the Swiss card game: here the deck is stacked, so every box starts from the same cards.

## How it works

- The **stack** is this public repository: skills, a short managed block of global rules, and the `jass` CLI.
- The **lore** is a private git repository of Markdown that you create. It holds project notes, cross-project lessons and digests of past sessions.
- A **box** is one machine. It links the stack's skills, carries the rules block in its global instruction file, keeps a clone of the lore, and has one config file, `~/.config/jass/box.json`.

Agents keep running inside each project. The rules block tells them to search the lore when they need it (`jass recall`) and to record decisions and gotchas with the `lore` skill. Nothing is injected into every session, and `jass sync` shares the lore between boxes. Work and personal boxes use separate lore repositories, and `jass` refuses to mix them.

## Use it

You need Git and Python 3.9 or newer, on macOS or Linux.

**Set up the first box.** Create an empty private repository for the lore, then preview and apply:

```sh
git clone https://github.com/leothiemard/just-a-shared-stack.git ~/Development/just-a-shared-stack
~/Development/just-a-shared-stack/bin/jass init --box mac --zone personal --lore <lore-git-url>
~/Development/just-a-shared-stack/bin/jass init --box mac --zone personal --lore <lore-git-url> --apply
jass sync --first-push
```

The preview changes nothing. `--apply` links the skills into `~/.agents/skills` and `~/.claude/skills`, installs `~/.local/bin/jass`, writes the rules block, clones the lore and runs `jass doctor`. It prints a restore manifest for `jass restore`. `--first-push` publishes the new lore once.

**Daily commands.** Agents run most of these themselves.

```sh
jass recall "preview deploy env vars"   # ranked search over the lore, within a token budget
jass show 1                             # print a recall hit in full
jass sync                               # share the lore with the other boxes
jass update --apply                     # pull the stack, relink skills, refresh the rules block
jass doctor                             # health check and always-loaded context size
```

**Add a skill.** Create `skills/<name>/SKILL.md` with `name` and `description` frontmatter, add `<name>` to `skills.json`, commit and push, then run `jass update --apply` on each box.

**Add another box.** On the new machine:

```sh
curl -fsSL https://raw.githubusercontent.com/leothiemard/just-a-shared-stack/main/bootstrap.sh | sh -s -- --box linux-box --zone personal --lore <lore-git-url> --apply
```

It clones the stack to `~/Development/just-a-shared-stack` and runs `jass init` with your arguments. Drop `--apply` to preview.

## Skills

| Skill | Purpose |
| --- | --- |
| lore | Recall and record shared knowledge across projects and boxes with `jass`. |
| diagnosing-bugs | Build a red-capable feedback loop, reproduce and minimize, then diagnose and fix. |
| blast-radius | Trace contract and downstream risks beyond a diff. |
| council | "council": Opus, Sol and Astra review or give feedback, with scored code reviews and further rounds as needed. |
| sol-loop | "sol loop": ask Sol for a bounded review and fix confirmed issues. |
| reward-loop | "reward loop": interview to lock a goal, reward and loop, then run subagent episodes against a frozen evaluator. |
| full-stop | "full stop": finish, prove, review and prepare a PR for the user to merge. |
| full-send | "full send": the same workflow, with authority to merge after checks and review. |
| grilling | Explore the complete decision tree with the user before acting. |
| handoff | Save a concise conversation handoff in the OS temporary directory. |
| retro | Recommend improvements to the coding environment from a session retrospective. |
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

Borrowed skills follow their upstream sources with small recorded patches; see [UPSTREAM.md](UPSTREAM.md) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). blast-radius and create-verification-skill call companions from [pstack](https://github.com/cursor/plugins/tree/main/pstack), which is installed separately.

## More

- [docs/ONBOARDING.md](docs/ONBOARDING.md): set up boxes, daily use, add skills, rules and private layers, review, roll back.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how the stack, the lore and the boxes fit together.
