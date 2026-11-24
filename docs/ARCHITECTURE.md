# Architecture

jass (Just a Shared Stack) is a setup for one person who runs coding agents (Claude Code, Codex, usually inside T3 Code) across many repositories and several machines. This page explains how the pieces fit. To set it up, read [ONBOARDING.md](ONBOARDING.md).

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
| Stack | How agents work: skills, global rules, the `jass` CLI, bootstrap, these docs | this repository | public; never private content |
| Lore | What agents know: project notes, lessons, session digests, box cards | a private git repository of Markdown, one per zone | private |
| Repo-local | What is true for anyone who clones a project: its `AGENTS.md`, `docs/`, project skills | each project | per project |
| Box config | Box name, zone, paths, lore remote, extra layers | `~/.config/jass/box.json` | never committed |

```
                        jass init / jass update
 stack (public git) ──────────────────────────▶ each box: skill links, managed rules block,
                                                 ~/.local/bin/jass, box.json
                                                            │
                           jass sync                         │ jass hub, recall, show, log, lesson
 lore (private git) ◀────────────────────────▶ each box's   ◀──────── agents in any project
   one per zone            (commit, rebase, push) lore clone
```

jass creates its own folders, `~/.config/jass` (box config, restore backups) and `~/.cache/jass` (recall index, sync and export state), as `0700`, and every file it writes there is `0600`, since they hold lore and session content.

Agents run inside the project they work on, never from a special home folder. The stack reaches them through globally linked skills and a short managed block in the global instruction file. The lore reaches them only on demand, through `jass`.

A work box adds a private **layer**: a directory shaped like this repository (`skills.json`, `skills/`, optional `rules/global.md`) with company skills and rules. It points at the company's lore repo in zone `work`. `jass` refuses to read or sync a lore repo whose zone differs from the box's, so personal boxes never see work knowledge and the reverse. Skill names must be unique across layers; a duplicate is an error, never a silent override.

Threat model: the zone guard exists to prevent mistakes, such as a box pointed at the wrong lore repo, a wrong branch, a stale clone, or a redirect left in git config. It is not a defence against someone who controls the box's user account or its git configuration; that person can read every lore repo the box holds. Session digests are scrubbed for common credential shapes as defence in depth, not as a guarantee, and the lore remote must be a private repository.

## The lore

```
lore.json                         {"version":1,"zone":"personal"}
README.md                          what this is; history is not instructions
.gitattributes                     Log.md and Lessons/*.md merge=union, * text=auto eol=lf
.gitignore                         ignores .<name>.jass-tmp-<8 hex>, the temp files of jass's atomic writes
Projects/<project>/<project>.md    hub note, target 3,000 bytes or less
Projects/<project>/Log.md          dated entries, newest first
Projects/<project>/<Topic>.md      one subject per note
Lessons/<topic>.md                 lessons that apply beyond one project
Sessions/<box>/<YYYY-MM>/<date>-<project>-<id8>.md   generated digests
Boxes/<box>.md                     generated box card
Reviews/<YYYY>-W<ww>.md            periodic review reports
```

The lore is also a valid Obsidian vault, which is handy for reading it, but nothing depends on Obsidian.

`jass init` accepts an existing folder as the lore only when it is a git repository with a `lore.json` of the box's zone; `--new-lore` and `--lore` need an empty or missing path, and a cloned repository with files but no `lore.json` is refused. Init commits only the files it writes (skeleton, imported notes), never other files in the folder.

Every lore reader and writer (recall's index walk, `hub`, `log`, `lesson`, `show`, the session export and listing, the box card) resolves the path and refuses one that leads outside the lore, for example through a symlinked project folder. Containment is decided by file identity, so another spelling of the lore's path on a case-insensitive filesystem still counts as inside. The index skips such files; the commands stop with an error. A `lore.json` that leads outside the lore does not mark a lore repo. `--import-notes` skips source files that lead outside the source folder, keeps any existing destination, a dangling link included, and stops the whole import before copying anything when a destination leads outside the lore.

The project key is the folder name of the git repository the current directory belongs to; a git worktree answers for its main repository, never for its own folder. Outside a repository it is the directory name, and anything under a `scratch` folder is `scratch`. The key matches an existing `Projects/` folder by name (case, spaces, `-` and `_` ignored), then by the repository name in the origin URL, then by an `aliases: [name, other-name]` line in a hub's frontmatter, so a repository cloned under another folder name on another box still finds its project. `-p <name>` goes through the same matching. Session digests are filed the same way.

A **lesson** is one `##` heading phrased as the claim, at most four lines of body, and a trailing comment with its provenance:

```markdown
## Preview deploys ignore env vars added after the build starts
Redeploy after changing variables; the running build keeps the old set.
<!-- 2026-11-12 · box mac · project example-app · thread 5232d102 -->
```

A **log entry** is a dated `##` heading with bullets and a `<!-- box mac -->` trailer. `jass log` inserts it at the top without the agent reading the file.

## Recall

`jass recall "<query>"` searches the lore and prints the cheapest useful answer:

```
3 of 17 hits · ~410 tokens · lore 2f9c1ab · synced 4 min ago
[1] Lessons/vercel.md § Preview deploys ignore env vars added after the build starts   lesson · 2026-10-30
    Redeploy after changing variables; the running build keeps the old set.
[2] Projects/example-app/Log.md § 2026-10-26 — Deploy hook rollout   log · example-app · mac
    …snippet around the best-matching terms…
Expand with: jass show 1
```

- **Index**: SQLite FTS5 in `~/.cache/jass/`, never inside the lore. It updates incrementally by file modification time and size, and rebuilds when its schema version changes.
- **Chunks**: each Markdown file is split at `#`, `##` and `###` headings. Each chunk records its path, heading trail, line, and metadata: kind (hub, log, note, lesson, session, box, review, other), project, box and date.
- **Ranking**: BM25 with headings weighted four times the body, multiplied by a kind weight (lessons first, then hubs, notes, reviews, logs, box cards, sessions) and a mild recency factor (half-life 180 days, floor 0.6). At most two hits come from one file, so one long note cannot fill the answer. Session hits are labelled with the session title. `--since` keeps undated chunks and dates a chunk by its heading (log entries), its provenance comment (lessons) or the file's frontmatter.
- **Budget**: output stops before the whole answer, header and footer included, exceeds `--budget` tokens (bytes/4, default 800), but always shows at least one hit. The header states the answer's own size. `jass show <n>` prints a hit in full.
- **Freshness**: when the lore has a remote and the last sync is older than `sync.max_age_minutes`, recall first runs a quick sync with an 8-second network timeout. A failure is one warning line; recall still answers from the local copy.

## Session digests

Each finished top-level agent session becomes a small Markdown file under `Sessions/<box>/`, so any box can search what happened on any other.

- A **source** reads one session store strictly read-only and yields session records. The interface is in `jass/sessions/__init__.py`. Sources: `t3` (T3 Code's state database), `claude` and `codex` (native transcripts, registered but not implemented yet), and `json:<file>` for tests. A source is used only when `sessions.sources` lists it.
- The **`t3` source** reads the projection tables of `~/.t3/userdata/statev2.sqlite` (`sessions.t3_store` overrides the path) through a read-only SQLite URI. Those tables are internal to T3 Code and change often, so the source checks every table and column it reads before reading; on a mismatch it prints `t3 source disabled: <what changed>` once and exports nothing. A thread becomes a digest when it has no parent (forks count as top-level), is not deleted, and has a completed run or no runs at all. The second case is history imported from T3 Code v1, which has messages but no runs; the spec asked only for threads with a completed run, and the code exports these too so that old history is searchable. A thread without any assistant answer is never exported. Its project is the basename of the T3 project's workspace root, and anything under a `scratch` folder is project `scratch`. "Asked" holds only what a human typed (plus the brief of an agent-launched thread); delegated results, notifications and scheduled-task wrappers are left out.
- Only top-level sessions idle for `sessions.idle_minutes` are exported. A thread that another thread launched as a step of its own work (T3's `delegatedFrom`) is left out like a subagent. The box config can leave out more: `sessions.exclude_projects` (project keys), `sessions.exclude_title_patterns` (regular expressions, case ignored) and `sessions.exclude_prompt_prefixes` (the opening message starts with the text, for workers and probes that agents launch). `jass sessions export --prune` reads every session and deletes this box's digests of sessions that are now left out; add `--dry-run` to list them first. An ordinary export never deletes. To remove one digest for good, delete the file and add its session id to `Sessions/excluded-ids.txt` in the lore (one id per line, `#` for comments): the list travels with the lore, so no box writes that digest again, not even with `--all`. Subagent threads, including nested ones, are counted on their top-level parent; app-owned children with runs get one line each under `## Delegated`.
- The file name is stable per session, so a session that continued later overwrites its own digest. Export state lives in the cache, not the lore: the last update time of each exported session and a horizon (the last run's time minus the idle window). Sessions last updated before the horizon were final at the last run, so an incremental export asks sources only for newer ones. `--all` ignores the horizon. A run where a source was disabled does not move it.
- Every text field passes through a scrubber that replaces known token shapes, private keys, credentials in URLs and credential values with `[redacted]`. A credential value follows a key that is, or ends in `_`, `-` or `.` plus, one of `password`, `passwd`, `passcode`, `passphrase`, `secret`, `token`, `api_key`, `apikey`, `access_key`, `auth`, `authorization`, `credential(s)` (any case), in `key=value`, `key: value` or JSON `"key": "value"` form, with plain or backslash-escaped quotes. When in doubt it redacts more: a quoted value is replaced through its closing quote, across lines, or to the end of its line when it has none, and an `Authorization:` or `Proxy-Authorization:` header loses its whole value, scheme included. Other identifiers (`monkey`, `max_tokens`, `author`) are left alone.
- A digest has frontmatter (`kind: session`, id, box, zone, project, title, dates, provider, model, turns, subagents, PRs, tokens, `peak_context` (the largest context-window use seen), and, when known, the T3 environment id, `native_ref`, branch and `created_by`), a line giving the date the state was true, then `## Asked` (the prompts, without repeats and harness commands; first and last three when there are more than six), `## Outcome` (the final answer, or the last answer of substance followed by the sign-off when the final one is under 400 characters), `## Turns` (one line per earlier turn, the last twelve) and `## Delegated` (children; the first thing dropped when a digest is too long). At most the twelve newest PRs are listed. It never exceeds 6,000 bytes. Headings in assistant text become bold and a code fence cut by a length cap is closed, so an answer cannot open sections of its own.
- `jass sessions stats [--by week|box|project]` sums sessions, turns, tokens and subagents from digest frontmatter and counts the sessions whose peak context passed 200,000 tokens. Long threads and delegated workers dominate cost, so the lore review starts from it.
- `jass sessions stats --by delegate-model [--since DATE] [--json]` reads this box's T3 store directly. It counts app-owned delegate starts by requested model and reports how many completed. Average elapsed minutes use completed delegates with usable start and completion timestamps. The date filter applies to the start time in UTC.
- A source that finds its store missing or unrecognised prints one warning and yields nothing. It never breaks `jass sync`.

## Sync

`jass sync` keeps the lore shared. It never touches the stack; `jass update` does that.

1. Take an advisory lock (`flock`) on a file in the cache directory, held until the sync ends; the kernel releases it if the process dies. If another sync holds it, exit quietly. Index rebuilds by `recall` wait on a lock of their own the same way.
2. Stop with exit 1, before writing anything, while a rebase, merge, cherry-pick or revert is in progress in the lore or files are unmerged; the message says how to finish or abort it. `jass doctor` reports the same.
3. Stop with exit 1, before writing anything, on a detached HEAD. With a remote, stop with exit 1 when origin's URL names another repository than `lore.remote` in the box config (any URL form counts as the same repository). Sync fetches from and pushes to `lore.remote` itself (origin's URL when none is set), and only as a literal location: a URL starting `https://`, `ssh://`, `git://` or `file://`, `user@host:path`, or a filesystem path, made absolute. Anything else, a value equal to one of the lore's remote names, or one that a `url.*.pushInsteadOf` setting rewrites, stops sync with a message to put the full URL in `box.json`, so a push URL, a remote name or a rewrite in git config never sends the lore elsewhere. `jass init --lore` applies the same rule before it records the remote. Fetch every remote branch with an explicit refspec into `refs/jass/remote/` (pruned), so the lore's fetch config and tracking refs never decide what the remote has; when the remote has branches but none with the local branch's name, stop with exit 1 and name the branch it uses. Sync never creates a branch on a non-empty remote, and plain `jass sync` never publishes to an empty one: it commits locally, pushes nothing, and says to run `jass sync --first-push` (so does `jass init --lore` when it clones an empty remote). `--first-push` is the provisioning step: it is refused when the remote has a branch, fetches again right before the push and stops if a branch appeared, then pushes with a lease that requires the branch to be absent. A branch another writer creates between that last fetch and the push is not caught when its name differs; that race is accepted, since a person runs `--first-push` once, by hand, at setup time. An unreachable remote is one warning: export and commit still run, nothing is pushed.
4. Export session digests (`--quick` skips this when the last export was under 6 hours ago).
5. Refresh `Boxes/<box>.md`: zone, OS, stack revision, always-loaded context size, projects seen on this box.
6. Delete temp files older than ten minutes that a killed write left in the lore: untracked regular files, outside `.git`, named exactly `.<name>.jass-tmp-<8 lowercase hex>`. A tracked file, a symlink or any other name is never deleted. Then commit all changes as `lore: <box> <UTC timestamp>`, with a fallback identity when git has none.
7. With a remote: check that the fetched remote branch's `lore.json` has this box's zone (exit 1 without rebasing or pushing when it does not), rebase local commits onto it, check the zone of the exact commit to push, push it with `--force-with-lease` set to the fetched commit, so a remote branch that moved since the fetch rejects the push. On a conflict, abort the rebase, keep the local commits, name the conflicting files and exit 1. Nothing is lost and nothing is pushed. Never a plain force.
8. Record the sync time.

Concurrent work on two boxes usually merges cleanly: `Log.md` and lesson files use git's `union` merge driver (lesson files carry no `updated:` field for that reason), and session digests and box cards are per-box files. A hub edited on two boxes at once is the remaining conflict case, and it fails safe as in step 7.

## Unattended work

`jass night` and `jass heavy` exist because a night of agent work is lost to the box far more often than to the agents: the machine goes away, runs out of memory or loses the network, a run fails and stays failed, a thread ends early or waits for its owner.

- `jass night check` only reads. Its rows come from the operating system (`pmset`, `/proc`, logind, the route table), from T3 Code's data folder (sign-in and quota of every enabled provider in `caches/`, two settings in `userdata/settings.json`) and from T3 Code's store. It never changes a setting; a `fail` row prints the command for a person to run.
- Thread states come from the same store the session export reads, strictly read-only, with every table and column pinned and checked first. T3 Code calls a thread `completed` when its turn ended while a delegate or a background command still works for it, so `jass night` derives its own states (`working`, `delegated`, `background`, `blocked`, `failed`, `stopped`, `stuck`, `ended`) and reads why a run failed from the `failure` T3 records on it. The last run that ran decides: a failed run wins over a delegate or background task it left behind, and a run still queued from before it is passed over.
- `jass night watch` is a loop in the foreground with one lock per box. Its only write outside its log is `t3 thread send <thread> -`: a message to continue for a thread whose run failed, once the network answers and memory has room, and with `--keep-going` a reminder for a thread that ended before the deadline. A thread gets at most four messages to continue and four reminders in a night, ten minutes apart, and only a delivered message counts; past that, and for anything a message cannot fix (a request for the owner, a usage limit before its reset, a refused sign-in, a thread a person stopped, a failed thread that has waited 30 minutes for memory or the network, a peer box gone quiet, a store it cannot read), it logs an alert. What it sent and alerted on is kept in `~/.cache/jass/night/watch-state.json` until the watch's deadline has passed (16 hours at most), so a watch started again the same night repeats no alert and keeps its counts, and the next night starts with nothing. An alert whose cause ended (a peer that answers again, a request that was answered) is forgotten, so its return is news. An error in one poll is an alert and the loop goes on, as it does when its log or state file cannot be written; SIGTERM and SIGHUP end it with a last log line. `--awake` holds the box awake with `caffeinate` or `systemd-inhibit` for as long as the watch runs. The watch is a process on the box: when T3 Code or the login session it runs in is gone, so is the watch's reach, and only a watch on another box that names this one with `--peer` reports it.
- `jass heavy` takes a box-wide lock in `~/.cache/jass`, then runs the command in its own `systemd-run --user --scope` with `MemoryMax` and no swap where systemd is available. The scope is its own cgroup, outside the agent's, so the kernel kills inside the command at its cap and the agent reads an exit code. When the command fails, `jass heavy` asks systemd whether the scope ended on an out-of-memory kill and says so, because a test runner whose worker was killed reports an ordinary failure. The wrapper holds the lock, not the command, and a `jass heavy` called from inside one runs straight through.

## Board queue

`jass board` turns [Vercel Board](https://vercel.com/docs/board) tasks into T3 Code threads, so the queue can live on the board while every agent still runs, and is watched, in T3 Code.

- **Queue:** a task is a board thread in `build/<project>`, where the project key maps to a folder in each box's `board.projects`. `jass board add` creates one as the `lead` profile; `--needs` makes it wait for another task.
- **Worker:** `jass board work` claims ready tasks of the mapped projects as `worker-<box>` (numbered when `board.workers` is above 1), and with nothing ready blocks on `vercel board wait`. Each claimed task gets one thread through `t3 thread start`, in a new worktree when the project is a git repository. The thread's id and box go on the task as labels `t3.thread` and `t3.box`.
- **Settling:** `t3 thread start --wait` returns when a turn ends, even while a background command or delegate still works, and exits 2 when the thread asks a question. So the worker polls `t3 thread show --json` instead and settles only after `activeRunId`, `pendingRequestCount`, `pendingBackgroundTasks` and `blockedOnThreadWait` have all been empty for `settle_seconds` with the same latest run. It posts the final answer as a reply and completes the task with its first line, or fails it on a `FAILED:` first line or a thread status other than `completed`. Exit 0 of a turn is not proof that the work was done; a review task with `--needs` is. The `t3` CLI cannot settle a thread, so the brief asks the agent to settle its own thread (`t3_thread_organize`) in the turn of its final answer; T3 applies that at turn end and leaves the thread active after a failed turn or a queued message, so a thread that waits for its owner stays in the inbox.
- **Claims:** the worker renews its lease every third of `lease_minutes`. A lost claim (the task was canceled or taken over) stops the watch and leaves the thread alone. On stop it releases the task; after a crash the lease runs out. The board raises no event when a lease runs out (it notices only when someone touches the task), so an idle worker re-checks the queue every `--wait-minutes` (5). The next claim on the same box attaches to the labelled thread instead of starting another, and sends a failed thread a message to continue. A claim from another box starts a new thread whose brief names the earlier one.
- **People:** a question keeps the claim and labels the task `t3.waiting=answer`; after `answer_hours` the task is blocked. T3 counts only multiple-choice questions in `pendingRequestCount`, not secret or sign-in cards, so an open run whose thread has not changed (`updatedAt`) for `stall_minutes`, with no background work, is labelled `t3.waiting=check` and treated the same way. A thread that waits on a delegate counts as busy, so the worker applies the same two checks to each delegated subthread and flags the task when one of them waits: a Codex delegate asks for approval before any `rm -f`, even with full access. Replies in the task's board thread from anyone but a `worker-` profile are forwarded into the T3 thread; the last one handled is the label `t3.seen`. The agent in the thread never calls the board.
- **Limits for paid boxes:** both are off by default. `board.task_minutes` fails a task whose thread still works, waits for an answer or stalls after that long, with the reason `timebox`; a thread that is only settling still completes. T3 has no interrupt, so the thread is left for a reopen. `--idle-exit` shares one idle clock among a box's workers: a worker counts as busy from the moment it looks for a task until that task is settled, so no worker claims a task once the box has decided to stop, and a finished task restarts the clock. While any worker is busy the others wait the normal `--wait-minutes`. When it runs out, every worker stops and `work` exits 0, which a VM's service turns into a machine stop.
- **Scaling:** `jass board scale` is one tick of a starter for cloud VMs, run by launchd on an always-on box. It holds a file lock so ticks never overlap, counts ready and claimed tasks in `scale.channels`, and resumes stopped VMs of `scale.pool` until `ceil(demand / per_vm)` run, then starts their worker over `boat exec`. It only starts machines: the workers stop their own (`--idle-exit`), and the tick stops a VM only as a safety net (no task for `idle_stop_minutes`) or at the spend cap, which also switches it off until `--on`. Boat is reached through a `Boat` port like `Board` and `T3`, so tests use fakes.
- **Viewing:** the Vercel dashboard has no Board page. `jass board open` runs `@vercel/board inspect`, pinned to an exact version because `npx` runs what it downloads, with `--url` set to the board `vercel board whoami` names, so no team name lives in the stack. It listens on `127.0.0.1` and acts as the signed-in user. Over SSH it prints an `ssh -L` tunnel rather than pass `--allow-host`, which would let anyone who reaches that host act as the owner.

`Job` holds every decision and reaches the board, T3 and the clock only through ports, so tests drive it with fakes.

## Context budget

`jass doctor` measures what every session loads before the task starts: each global instruction file (symlinks counted once), the `name` and `description` bytes of every skill per skill root, enabled Claude plugins' skills, and configured MCP servers. It warns when a global file passes 2,000 bytes, a skill description passes 500 bytes, or the total passes 20,000 bytes. `jass sync` writes the total into the box card so drift shows over time.

Run inside a git repository, `jass doctor` also lists that repository's `AGENTS.md` and `CLAUDE.md` and Claude's auto-memory index for it (`~/.claude/projects/<slug>/memory/MEMORY.md`, where the slug is the absolute repository path with every non-alphanumeric character replaced by `-`). These rows are not part of the box total; they warn past 8,000 bytes (instructions) and 4,000 bytes (memory index).
