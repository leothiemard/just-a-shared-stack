# Architecture

just-a-stack is a setup for one person who runs coding agents (Claude Code, Codex, usually inside T3 Code) across many repositories and several machines. This page explains how the pieces fit. To set it up, read [ONBOARDING.md](ONBOARDING.md).

## The problem

Agents work inside one project on one machine at a time. Without help:

- Skills and global rules must be copied to each machine by hand, and drift.
- What an agent learned in one project, or on one machine, never reaches the next session elsewhere.
- Past sessions live in each machine's local database. "What did we do five weeks ago on the Linux box?" has no answer from the Mac.
- Always-loaded context (global instruction files, skill descriptions) grows until it crowds out the task.

The design goals: one command to add a machine, one to update it, knowledge shared between machines of the same trust zone, past work searchable from anywhere, a small always-loaded context, work and personal kept apart by construction.

## Three layers and one file per machine

Each machine is a **box**. Every box has a name (`mac`, `linux-box`) and belongs to one **zone** (`personal`, `work`, or any label).

| Layer | Holds | Lives in | Visibility |
| --- | --- | --- | --- |
| Stack | How agents work: skills, global rules, the `jas` CLI, bootstrap, these docs | this repository | public; never private content |
| Brain | What agents know: project notes, lessons, session digests, box cards | a private git repository of Markdown, one per zone | private |
| Repo-local | What is true for anyone who clones a project: its `AGENTS.md`, `docs/`, project skills | each project | per project |
| Box config | Box name, zone, paths, brain remote, extra layers | `~/.config/just-a-stack/box.json` | never committed |

```
                        jas init / jas update
 stack (public git) ──────────────────────────▶ each box: skill links, managed rules block,
                                                 ~/.local/bin/jas, box.json
                                                            │
                           jas sync                         │ jas hub, recall, show, log, lesson
 brain (private git) ◀────────────────────────▶ each box's   ◀──────── agents in any project
   one per zone            (commit, rebase, push) brain clone
```

Agents run inside the project they work on, never from a special home folder. The stack reaches them through globally linked skills and a short managed block in the global instruction file. The brain reaches them only on demand, through `jas`.

A work box adds a private **layer**: a directory shaped like this repository (`skills.json`, `skills/`, optional `rules/global.md`) with company skills and rules. It points at a company brain in zone `work`. `jas` refuses to read or sync a brain whose zone differs from the box's, so personal boxes never see work knowledge and the reverse. Skill names must be unique across layers; a duplicate is an error, never a silent override.

## The brain

```
brain.json                         {"version":1,"zone":"personal"}
README.md                          what this is; history is not instructions
.gitattributes                     Log.md and Lessons/*.md merge=union, * text=auto eol=lf
.gitignore
Projects/<project>/<project>.md    hub note, target 3,000 bytes or less
Projects/<project>/Log.md          dated entries, newest first
Projects/<project>/<Topic>.md      one subject per note
Lessons/<topic>.md                 lessons that apply beyond one project
Sessions/<box>/<YYYY-MM>/<date>-<project>-<id8>.md   generated digests
Boxes/<box>.md                     generated box card
Reviews/<YYYY>-W<ww>.md            periodic review reports
```

The brain is also a valid Obsidian vault, which is handy for reading it, but nothing depends on Obsidian.

`jas init` accepts an existing folder as the brain only when it is a git repository with a `brain.json` of the box's zone; `--new-brain` and `--brain` need an empty or missing path, and a cloned repository with files but no `brain.json` is refused. Init commits only the files it writes (skeleton, imported notes), never other files in the folder.

Every brain reader and writer (recall's index walk, `hub`, `log`, `lesson`, `show`, the session export and listing, the box card) resolves the path and refuses one that leads outside the brain, for example through a symlinked project folder. The index skips such files; the commands stop with an error. `--import-notes` skips source files that lead outside the source folder, keeps any existing destination, a dangling link included, and stops the whole import before copying anything when a destination leads outside the brain.

The project key is the basename of the git repository root of the current directory, or the directory name outside a repository. It matches existing folders ignoring case, spaces, dashes and underscores, so `Example App` and `example-app` are the same project.

A **lesson** is one `##` heading phrased as the claim, at most four lines of body, and a trailing comment with its provenance:

```markdown
## Preview deploys ignore env vars added after the build starts
Redeploy after changing variables; the running build keeps the old set.
<!-- 2026-11-12 · box mac · project example-app · thread 5232d102 -->
```

A **log entry** is a dated `##` heading with bullets and a `<!-- box mac -->` trailer. `jas log` inserts it at the top without the agent reading the file.

## Recall

`jas recall "<query>"` searches the brain and prints the cheapest useful answer:

```
3 of 17 hits · ~410 tokens · brain 2f9c1ab · synced 4 min ago
[1] Lessons/vercel.md § Preview deploys ignore env vars added after the build starts   lesson · 2026-10-30
    Redeploy after changing variables; the running build keeps the old set.
[2] Projects/example-app/Log.md § 2026-10-26 — Deploy hook rollout   log · example-app · mac
    …snippet around the best-matching terms…
Expand with: jas show 1
```

- **Index**: SQLite FTS5 in `~/.cache/just-a-stack/`, never inside the brain. It updates incrementally by file modification time and size, and rebuilds when its schema version changes.
- **Chunks**: each Markdown file is split at `#`, `##` and `###` headings. Each chunk records its path, heading trail, line, and metadata: kind (hub, log, note, lesson, session, box, review, other), project, box and date.
- **Ranking**: BM25 with headings weighted four times the body, multiplied by a kind weight (lessons first, then hubs, notes, reviews, logs, box cards, sessions) and a mild recency factor (half-life 180 days, floor 0.6). At most two hits come from one file, so one long note cannot fill the answer. Session hits are labelled with the session title. `--since` keeps undated chunks and dates a chunk by its heading (log entries), its provenance comment (lessons) or the file's frontmatter.
- **Budget**: output stops before the whole answer, header and footer included, exceeds `--budget` tokens (bytes/4, default 800), but always shows at least one hit. The header states the answer's own size. `jas show <n>` prints a hit in full.
- **Freshness**: when the brain has a remote and the last sync is older than `sync.max_age_minutes`, recall first runs a quick sync with an 8-second network timeout. A failure is one warning line; recall still answers from the local copy.

## Session digests

Each finished top-level agent session becomes a small Markdown file under `Sessions/<box>/`, so any box can search what happened on any other.

- A **source** reads one session store strictly read-only and yields session records. The interface is in `jas/sessions/__init__.py`. Sources: `t3` (T3 Code's state database), `claude` and `codex` (native transcripts, registered but not implemented yet), and `json:<file>` for tests. A source is used only when `sessions.sources` lists it.
- The **`t3` source** reads the projection tables of `~/.t3/userdata/statev2.sqlite` (`sessions.t3_store` overrides the path) through a read-only SQLite URI. Those tables are internal to T3 Code and change often, so the source checks every table and column it reads before reading; on a mismatch it prints `t3 source disabled: <what changed>` once and exports nothing. A thread becomes a digest when it has no parent (forks count as top-level), is not deleted, and has a completed run or no runs at all. The second case is history imported from T3 Code v1, which has messages but no runs; the spec asked only for threads with a completed run, and the code exports these too so that old history is searchable. A thread without any assistant answer is never exported. Its project is the basename of the T3 project's workspace root, and anything under a `scratch` folder is project `scratch`. "Asked" holds only what a human typed (plus the brief of an agent-launched thread); delegated results, notifications and scheduled-task wrappers are left out.
- Only top-level sessions idle for `sessions.idle_minutes` are exported. Subagent threads, including nested ones, are counted on their top-level parent; app-owned children with runs get one line each under `## Delegated`.
- The file name is stable per session, so a session that continued later overwrites its own digest. Export state lives in the cache, not the brain: the last update time of each exported session and a horizon (the last run's time minus the idle window). Sessions last updated before the horizon were final at the last run, so an incremental export asks sources only for newer ones. `--all` ignores the horizon. A run where a source was disabled does not move it.
- Every text field passes through a scrubber that replaces known token shapes, private keys, credentials in URLs and credential values with `[redacted]`. A credential value follows a key that is, or ends in `_`, `-` or `.` plus, one of `password`, `passwd`, `secret`, `token`, `api_key`, `apikey`, `access_key`, `auth`, `authorization`, `credential(s)` (any case), in `key=value`, `key: value` or JSON `"key": "value"` form, with plain or backslash-escaped quotes. When in doubt it redacts more: a quoted value is replaced through its closing quote, across lines, or to the end of its line when it has none, and an `Authorization:` or `Proxy-Authorization:` header loses its whole value, scheme included. Other identifiers (`monkey`, `max_tokens`, `author`) are left alone.
- A digest has frontmatter (`kind: session`, id, box, zone, project, title, dates, provider, model, turns, subagents, PRs, tokens, `peak_context` (the largest context-window use seen), and, when known, the T3 environment id, `native_ref`, branch and `created_by`), then `## Asked` (the prompts; first and last three when there are more than six), `## Outcome` (the final answer), `## Turns` (one line per earlier turn, the last twelve) and `## Delegated` (children). It never exceeds 6,000 bytes. Headings in assistant text become bold and a code fence cut by a length cap is closed, so an answer cannot open sections of its own.
- `jas sessions stats [--by week|box|project]` sums sessions, turns, tokens and subagents from digest frontmatter and counts the sessions whose peak context passed 200,000 tokens. Long threads and delegated workers dominate cost, so the brain review starts from it.
- A source that finds its store missing or unrecognised prints one warning and yields nothing. It never breaks `jas sync`.

## Sync

`jas sync` keeps the brain shared. It never touches the stack; `jas update` does that.

1. Take an advisory lock (`flock`) on a file in the cache directory, held until the sync ends; the kernel releases it if the process dies. If another sync holds it, exit quietly. Index rebuilds by `recall` wait on a lock of their own the same way.
2. Stop with exit 1, before writing anything, while a rebase, merge, cherry-pick or revert is in progress in the brain or files are unmerged; the message says how to finish or abort it. `jas doctor` reports the same.
3. Stop with exit 1, before writing anything, on a detached HEAD. With a remote, fetch; when the remote has branches but none with the local branch's name, stop with exit 1 and name the branch it uses. Sync never creates a branch on a non-empty remote; an empty remote takes the first push. An unreachable remote is one warning: export and commit still run, nothing is pushed.
4. Export session digests (`--quick` skips this when the last export was under 6 hours ago).
5. Refresh `Boxes/<box>.md`: zone, OS, stack revision, always-loaded context size, projects seen on this box.
6. Commit all changes as `brain: <box> <UTC timestamp>`, with a fallback identity when git has none.
7. With a remote: check that the fetched remote branch's `brain.json` has this box's zone (exit 1 without rebasing or pushing when it does not), rebase local commits onto it, check the local zone again, push. On a conflict, abort the rebase, keep the local commits, name the conflicting files and exit 1. Nothing is lost and nothing is pushed. Never force.
8. Record the sync time.

Concurrent work on two boxes usually merges cleanly: `Log.md` and lesson files use git's `union` merge driver (lesson files carry no `updated:` field for that reason), and session digests and box cards are per-box files. A hub edited on two boxes at once is the remaining conflict case, and it fails safe as in step 7.

## Context budget

`jas doctor` measures what every session loads before the task starts: each global instruction file (symlinks counted once), the `name` and `description` bytes of every skill per skill root, enabled Claude plugins' skills, and configured MCP servers. It warns when a global file passes 2,000 bytes, a skill description passes 500 bytes, or the total passes 20,000 bytes. `jas sync` writes the total into the box card so drift shows over time.

Run inside a git repository, `jas doctor` also lists that repository's `AGENTS.md` and `CLAUDE.md` and Claude's auto-memory index for it (`~/.claude/projects/<slug>/memory/MEMORY.md`, where the slug is the absolute repository path with every non-alphanumeric character replaced by `-`). These rows are not part of the box total; they warn past 8,000 bytes (instructions) and 4,000 bytes (memory index).
