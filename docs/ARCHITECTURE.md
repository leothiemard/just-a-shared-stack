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
.gitattributes                     Log.md merge=union, * text=auto eol=lf
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
- **Ranking**: BM25 with headings weighted four times the body, multiplied by a kind weight (lessons first, then hubs, notes, reviews, logs, box cards, sessions) and a mild recency factor (half-life 180 days, floor 0.6).
- **Budget**: output stops before it exceeds `--budget` tokens (bytes/4, default 800), but always shows at least one hit. `jas show <n>` prints a hit in full.
- **Freshness**: when the brain has a remote and the last sync is older than `sync.max_age_minutes`, recall first runs a quick sync with an 8-second network timeout. A failure is one warning line; recall still answers from the local copy.

## Session digests

Each finished top-level agent session becomes a small Markdown file under `Sessions/<box>/`, so any box can search what happened on any other.

- A **source** reads one session store strictly read-only and yields session records. The interface is in `jas/sessions/__init__.py`. Sources: `t3` (T3 Code's state database), `claude` and `codex` (native transcripts), and `json:<file>` for tests. The `t3`, `claude` and `codex` sources are registered but not implemented yet; until they are, `jas doctor` lists them as disabled.
- Only top-level sessions idle for `sessions.idle_minutes` are exported. Subagent sessions are folded into their parent.
- The file name is stable per session, so a session that continued later overwrites its own digest. Export state lives in the cache, not the brain.
- Every text field passes through a scrubber that replaces known token shapes, private keys, credentials in URLs and `key=`/`token=`/`password=`/`secret=` values with `[redacted]`.
- A digest has frontmatter (`kind: session`, id, box, zone, project, title, dates, provider, model, turns, subagents, PRs, tokens, and the T3 environment id when known), then `## Asked` (the prompts; first and last three when there are more than six), `## Outcome` (the final answer), `## Turns` (one line per earlier turn, the last twelve) and `## Delegated` (children). It never exceeds 6,000 bytes.
- A source that finds its store missing or unrecognised prints one warning and yields nothing. It never breaks `jas sync`.

## Sync

`jas sync` keeps the brain shared. It never touches the stack; `jas update` does that.

1. Take a lock in the cache directory (stale after 10 minutes). If another sync holds it, exit quietly.
2. Export session digests (`--quick` skips this when the last export was under 6 hours ago).
3. Refresh `Boxes/<box>.md`: zone, OS, stack revision, always-loaded context size, projects seen on this box.
4. Commit all changes as `brain: <box> <UTC timestamp>`, with a fallback identity when git has none.
5. With a remote: fetch, rebase local commits onto it, push. On a conflict, abort the rebase, keep the local commits, name the conflicting files and exit 1. Nothing is lost and nothing is pushed. Never force.
6. Record the sync time.

Concurrent work on two boxes usually merges cleanly: `Log.md` uses git's `union` merge driver, and session digests and box cards are per-box files. A hub edited on two boxes at once is the remaining conflict case, and it fails safe as in step 5.

## Context budget

`jas doctor` measures what every session loads before the task starts: each global instruction file (symlinks counted once), the `name` and `description` bytes of every skill per skill root, enabled Claude plugins' skills, and configured MCP servers. It warns when a global file passes 2,000 bytes, a skill description passes 500 bytes, or the total passes 20,000 bytes. `jas sync` writes the total into the box card so drift shows over time.
