# Onboarding

How to adopt jass for yourself. [ARCHITECTURE.md](ARCHITECTURE.md) explains the design in more depth.

## The idea in five sentences

The **stack** (this public repository) holds how your agents work: skills, a short block of global rules, and the `jass` CLI. The **lore** (a private git repository of Markdown that you create) holds what your agents know: project notes, cross-project lessons and digests of past sessions. Every machine (**box**) links the stack's skills, carries the rules block in its global instruction file, and keeps a clone of the lore. Agents read the lore on demand with `jass hub` and `jass recall`, record into it with the `lore` skill, and `jass sync` shares it between boxes. Work and personal knowledge live in separate lore repos (**zones**), and `jass` refuses to mix them.

## Requirements

Git, Python 3.9 or newer with SQLite FTS5 (macOS system Python, python.org and Homebrew builds have it), macOS or Linux. Nothing runs in the background; `jass night watch` runs only while you keep it running.

## Set up the first box

1. Fork or clone this repository. If you fork it, change the clone URL in `bootstrap.sh`.
2. Create an empty private repository for your lore on your git host, for example `lore-personal`.
3. Preview, then apply:

   ```sh
   git clone https://github.com/<owner>/just-a-shared-stack.git ~/Development/just-a-shared-stack
   ~/Development/just-a-shared-stack/bin/jass init --box mac --zone personal --lore <lore-git-url>
   ~/Development/just-a-shared-stack/bin/jass init --box mac --zone personal --lore <lore-git-url> --apply
   ```

   The preview prints one row per change (`add`, `update`, `keep`, `conflict`, `skip`) and changes nothing. `--apply` writes `~/.config/jass/box.json`, links the skills into `~/.agents/skills` and `~/.claude/skills`, links `~/.local/bin/jass`, writes the rules block into `~/.agents/AGENTS.md` (and links or updates `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`), clones the lore and creates its skeleton, then runs `jass doctor`. It prints a restore manifest path.

4. If `jass doctor` says `~/.local/bin` is not on `PATH`, add the printed line to your shell profile.
5. Run `jass sync --first-push` to publish the lore skeleton to the empty repository. Plain `jass sync` never pushes to a repository that has no branches; it commits locally and prints this hint instead. `--first-push` runs once, at setup, and is refused as soon as the repository has a branch.

No git host yet? `jass init --new-lore` creates a local-only lore; add a remote later with `jass init --lore <url> --apply`.

Already have notes in the same `Projects/<project>/` shape (an Obsidian vault, say)? Add `--import-notes <vault-folder>`. It copies `Projects/` and loose top-level `.md` files into the lore, never overwrites a file, and never changes the source.

Existing skills or files with the same names are reported as conflicts. Inspect them, then rerun with `--apply --replace`; replaced content is backed up and listed in the manifest. Text you wrote in `~/.agents/AGENTS.md` or `~/.claude/CLAUDE.md` outside the markers is kept byte for byte.

## Add another box

On the new machine:

```sh
curl -fsSL https://raw.githubusercontent.com/<owner>/just-a-shared-stack/main/bootstrap.sh | sh -s -- --box linux-box --zone personal --lore <lore-git-url> --apply
```

`bootstrap.sh` checks for git and Python, clones the stack to `~/Development/just-a-shared-stack` (or `$JASS_STACK_DIR`), and runs `jass init` with your arguments. A folder already there is used only when it is the top of a git checkout whose `origin` is the same repository as the clone URL (`$JASS_STACK_URL`, in any https, ssh or `git@host:` form) and holds `bin/jass` and `jass/cli.py`; anything else stops bootstrap. Drop `--apply` to preview first. Then run `jass sync`.

## Keep a box current

```sh
jass update           # preview
jass update --apply   # pull the stack and layers, relink skills, refresh the rules block, run doctor
jass sync             # share the lore
```

## Daily use

You mostly do nothing. The rules block tells agents to:

- run `jass hub` when they start substantial work in a project, to read its hub note;
- run `jass recall "<query>"` when they need history, an earlier decision, something from another project or box, or when a tool keeps failing the same way;
- save decisions, root causes, gotchas and setup facts with the `lore` skill at the end of substantial work, then `jass sync`.

Things you can ask for:

- "What did we decide about the deploy hook last month?" or "Have we seen this error before?" (recall)
- "Remember that preview deploys ignore env vars added after the build starts." (a lesson)
- "Log what we did today." (a project log entry)
- "Lore review." (see below)

Run `jass sync` whenever you want the other boxes to see new knowledge; agents also run it after recording. `jass recall` syncs on its own when the lore is stale.

Commands you may use yourself: `jass doctor` (health and context budget), `jass sessions list --since 2w`, `jass sessions stats --by project` (where the tokens went), `jass path project`, `jass show <n>` after a recall.


## Leave agents working unattended

Start unattended threads on a reachable Linux box that can stay awake, with the project's checkout, tools and provider sign-in ready there. Open that environment from your Mac to steer the work. To send a child from an active thread to another box, find the link with `t3 environment list` and name the destination in the brief. The orchestrator passes its ID as `target.environmentId` to `delegate_task`. For independent jobs that can wait for a VM to start, use the [board queue](#queue-work-on-a-board). The scaler wakes VMs for queued work.

Before you leave agents working overnight or while you are away, on every box that hosts work:

```sh
jass night check --hours 8 --agents 4 --need 'gh auth status'    # exit 1 when something will stop the night
jass night watch --hours 8 --thread "Lane A" --thread "Lane B" --keep-going --awake
```

`check` reads this box and changes nothing: whether it will sleep, power, a login session that has been crashing (macOS), memory for that many agents, the network link, disk, provider sign-in and quota, and threads that wait for you. Each `fail` row names its fix. `watch` runs in the foreground until the hours pass, so start it in `tmux` or a similar terminal that outlives your session; it resumes T3 Code threads whose run failed and logs what needs you to `~/.cache/jass/night/`. Started again the same night, it repeats no alert and keeps its message counts. It reads T3 Code's store and sends through `t3 thread send`; without T3 Code, `check` and `heavy` still work.

`jass heavy -- <command>` runs one heavy command at a time on the box under a memory cap (Linux with systemd; elsewhere only the queue), so a build that outgrows memory is killed alone instead of taking its agent and that agent's background jobs with it, and `jass heavy` says when a failure was a kill at the cap. The cap is a quarter of memory unless `night.heavy_mb` in `box.json` sets it (`0` for none); `night.agent_mb` and `night.reserve_mb` tune the sums `check` does.

The `overnight` skill walks an agent through the whole routine: plan, pre-flight, brief, watch, report.

## Queue work on a board

`jass board` uses [Vercel Board](https://vercel.com/docs/board) as a task queue whose tasks run as ordinary T3 Code threads, so you keep watching and answering them in T3 Code. Use it for self-contained work: maintenance, dependency bumps, bug fixes with a clear check.

Once per box: install the Vercel CLI (version 66 or later, which has `vercel board tasks`), run `vercel login`, check `vercel board whoami`, and map the projects this box works on in `~/.config/jass/box.json`:

```json
"board": {
  "projects": {"shop": "~/Development/shop"},
  "workers": 2,
  "provider": "claudeAgent", "model": "claude-sonnet-5-5", "options": ["effort=medium"]
}
```

A project key names the channel `build/<key>` and must be the same on every box; the folder may differ. Leave `provider` and `model` empty to use the T3 project's default. A task can pick another provider with `--provider` and `--model` (for example `codex` with `gpt-6.2-sol`, or `antigravity` with `gemini-4-neon`); it then takes none of the box's `model` or `options`, while a task on the box's own provider keeps the box's `options`. `--effort` uses the provider's option (`reasoningEffort` for Codex, `thought_level` for Antigravity) and works only on the provider ids `claudeAgent`, `codex`, `antigravity` and `fx`; a task whose model or effort cannot reach T3 fails with the reason. The provider must be signed in on every box that serves the channel. Each project must already be a project in T3 Code on that box.

Queue a task, from your terminal or by asking an agent:

```sh
jass board add shop "Fix the date shown on order emails (apps/web/emails/order.tsx). Done when: npm test -- order passes."
jass board add shop --needs <id> "Review the order-email fix on its branch; report problems, change nothing."
jass board status            # open tasks, their state and their T3 thread
jass board report --since 6h # tasks done, failed and stuck, with box, thread and pull requests
jass board open              # the board in a browser, as you: read, post, react
```

`jass board open` runs Vercel's `@vercel/board inspect` (a pinned version, through `npx`, so it needs Node.js) against the board `vercel board whoami` names, on `127.0.0.1` until Ctrl-C. The Vercel dashboard has no Board page, and `inspect` finds no board on its own outside a Vercel project. Over SSH, on a box like a headless server, it opens no browser and prints the tunnel to use from your computer: `ssh -N -L 4747:127.0.0.1:4747 <box>`, then open `http://127.0.0.1:4747`. Do not use `inspect --allow-host` to reach it over the tailnet instead: anyone who reaches that host acts as you on the board.

Work the queue on each box: `jass board work` runs in the foreground (start it in `tmux` or as a service, below). For each ready task of a mapped project it starts one T3 thread titled `[board] …`, in a new worktree from origin's default branch (`board.base`, or `--base` on the task; `none` for the project folder). It completes the task with the first line of the thread's final answer once the thread has been idle for `settle_seconds`: no run, no question, no background command or delegate still working. A final answer starting `FAILED:`, or a thread that ends in an error, fails the task. The brief asks the agent to settle its thread with that final answer, so finished threads leave the T3 inbox and only threads that wait for you stay.

- A question in the thread keeps the task held and labels it `t3.waiting=answer`; answer it in T3 Code. After `answer_hours` (24) without an answer the task is blocked; unblock it with `vercel board tasks unblock <id>` once you answered.
- A reply that you or an agent posts in the task's board thread is forwarded into the T3 thread.
- A worker that stops or crashes leaves the thread running; the task returns to the queue when its lease runs out, and the same box picks the same thread up again.
- The agent in the thread never writes to the board. Board text is still instructions to an agent with your access: keep the `build/` channels to people and agents you trust.

As a service on Linux, with systemd (`systemctl --user daemon-reload && systemctl --user enable --now jass-board`; `loginctl enable-linger $USER` keeps it running after you log out):

```ini
# ~/.config/systemd/user/jass-board.service
[Unit]
Description=jass board worker
After=network-online.target

[Service]
ExecStart=%h/.local/bin/jass board work
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

Add the folders of `vercel`, `t3` and `node` to that `PATH` if they live elsewhere (`command -v vercel t3 node`). The worker logs to `~/.cache/jass/board/` and to the journal (`journalctl --user -u jass-board -f`).

On a cloud VM that bills while it runs, let the worker end itself and the VM stop after it. `--idle-exit 3` ends `jass board work` once no worker on the box has had a task for 3 minutes. `board.task_minutes` (for example 60) fails a task whose thread is still working, or still waiting for an answer, after that long, which keeps every task below the VM's own auto-stop; a thread that has finished and is only settling still completes. T3 cannot interrupt a thread, so it keeps running until the VM stops; reopening the task (`vercel board tasks reopen <id>`) on the same box attaches to it again. Replace `ExecStopPost` with your provider's command for a VM to stop itself (here boat.dev's):

```ini
# ~/.config/systemd/user/jass-board-vm.service
[Unit]
Description=jass board worker that stops this VM when the queue is empty

[Service]
# Login shells: boat puts the VM's environment variables (tokens, its in-VM key) there only.
ExecStart=/bin/bash -lc 'exec jass board work --workers 2 --idle-exit 3'
# Export this VM's session digests to the lore, then stop the VM whatever the export did.
ExecStopPost=/bin/bash -lc 'timeout -k 10 150 jass sync --before-stop; boat stop self'
# Above systemd's 90 s default, which would kill the line above before `boat stop self`.
TimeoutStopSec=5min
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=no
```

Run `loginctl enable-linger $USER` once and `systemctl --user daemon-reload` after writing the unit. Without linger, the user's systemd stops when your SSH session ends, and with it the worker and the VM. Then start it with `systemctl --user start jass-board-vm` after each boot or resume, not `enable`: a VM that stops itself on every start would never stay up for setup. `Restart=no` matters for the same reason. Any end of the worker stops the VM: an empty queue, a crash (its tasks return to the queue when their leases run out), a failure at startup such as no network yet, and a manual `systemctl --user stop` too.

A stopped VM's threads cannot be read, so the stop line first runs `jass sync --before-stop`: it exports the sessions that just finished (without waiting `sessions.idle_minutes`) to `Sessions/<box>/` in the lore and pushes. Give each VM its own box name (`boat-w1`, `boat-w2`, the names in `scale.pool`), so two VMs that stop together write separate box cards. The VM needs a lore clone it can push to (`jass init --box boat-w1 --lore <url> --apply`, with a credential that may write the lore repository, such as a deploy key). That clone gives the VM's agents the whole lore of its zone to read and write, so set it up only on VMs you trust as much as your other boxes. Without one, `jass sync` fails at once and the VM still stops. `timeout` ends an export that hangs (an unreachable remote, a slow push) after 150 seconds; the lore keeps what it committed, and the next stop pushes it. A VM that the scaler stops (no task for `idle_stop_minutes`, the spend cap) runs no stop line; its digests wait for its next stop.

Watch the VMs through the board, not T3 Code's sidebar. A VM that stops whenever the queue is empty looks like a broken machine there, raises sign-in warnings and leaves threads that cannot be settled while it is down. Remove each VM's environment from the sidebar, but keep the agent link (`t3 environment`) that lets an agent on another box read a VM's threads while it runs. Then follow the work with:

- `jass board report --since 6h` on any box: the tasks done and failed in that time and the tasks stuck now (blocked, waiting for an answer, ready with no worker, waiting for a task that failed), each with its box, T3 thread and pull requests. Add `--json` for a script. A scheduled thread on an always-on box can run it and pass on what needs you.
- The pull requests the tasks open, and their reviews.
- The VMs' session digests in the lore: `jass recall "<task>" --box boat-w1`.

A finished thread whose run failed or was interrupted stays unsettled on its VM: the brief asks every thread to settle itself, but a failed turn does not, and the `t3` CLI has no command to settle or archive a thread. The report lists those tasks as failed; reopening one (`vercel board tasks reopen <id>`) continues it in the same thread when the VM next runs.

### Wake VMs when tasks wait

`jass board scale` runs on an always-on box and starts stopped VMs when their tasks wait; the VMs stop themselves as above. Give the VMs their own project keys (`fma-boat` mapped to the checkout in each VM's `box.json`), so the Mac and other boxes never take those tasks, and queue a task there with `jass board add fma-boat "<brief>"`. On the box that runs the scaler, add a `scale` block to `~/.config/jass/box.json` and sign the `boat` CLI in:

```json
"scale": {
  "enabled": true,
  "pool": [{"id": "bx_…", "box": "boat-w1"}, {"id": "bx_…", "box": "boat-w2"}],
  "channels": ["fma-boat"],
  "per_vm": 2,
  "spend_cap_usd": 8,
  "since": "2026-11-23T00:00:00Z"
}
```

Each tick resumes stopped pool VMs until one runs per `per_vm` waiting or claimed tasks, and (re)starts the worker on every running pool VM while tasks wait (`start_command`, through a login shell; starting a running unit does nothing). It pushes back the auto-stop (`ttl_minutes`) of a VM whose box holds a claimed task, stops a VM that has run `idle_stop_minutes` (15) with no task at all, and at `spend_cap_usd`, counted from `since`, stops the whole pool and switches itself off, then keeps stopping any pool VM that is not stopped until you switch it back `--on`. Ready tasks that a VM was started for and left unclaimed (meant for another profile, or a worker that fails at start) wake no VM again, and while a VM runs they start no second one; a new task does, and `--on` forgets them. It never creates or deletes a VM. Set `board.task_minutes` on the VMs, or a task waiting for your answer keeps its VM up for `answer_hours`.

`--status` shows the pool, spend and last actions; `--off` and `--on` pause and resume it, and `--off` takes effect at once, even during a tick's resume. A VM you resume by hand for setup counts too: switch scaling `--off` first, or it is stopped after 15 minutes without tasks. While the scaler's box sleeps no tick runs; the VMs still stop themselves, but waiting tasks wake nothing. Prefer a Linux box that stays on, and run the scaler on one box only: the lock that keeps ticks apart is per box.

On Linux, run a tick every 2 minutes with a systemd user timer (`systemctl --user daemon-reload` after writing the units, then `systemctl --user enable --now jass-board-scale.timer`; `disable --now` to stop; `loginctl enable-linger $USER` so it runs without a login). On a headless box, sign `boat` in with an API key limited to the pool's VMs: `boat api-key create <name> --actions sandbox.read,sandbox.update,sandbox.resume,sandbox.stop,exec,account.read --sandbox <id> [--sandbox <id> ...]` on a signed-in box, piped into `boat login --key-stdin` on this one. The `full-sandbox` preset cannot resume a VM. A key cannot outlive the key that created it, so watch its expiry.

```ini
# ~/.config/systemd/user/jass-board-scale.service
[Unit]
Description=jass board scale: one tick
After=network-online.target

[Service]
Type=oneshot
ExecStart=%h/.local/bin/jass board scale
Environment=PATH=%h/.local/bin:%h/.ascii/bin:/usr/local/bin:/usr/bin:/bin
Environment=BOAT_NO_UPDATE=1
TimeoutStartSec=35min

# ~/.config/systemd/user/jass-board-scale.timer
[Timer]
OnCalendar=*:0/2
AccuracySec=10s

[Install]
WantedBy=timers.target
```

Use `OnCalendar`: `OnUnitInactiveSec` alone never arms until the service has run and exited once, so the timer stays idle after a reboot. `TimeoutStartSec` must cover one tick that resumes VMs one after another (up to 15 minutes each); raise it for a pool of more than two.

Put the folders of `jass`, `boat`, `vercel` and `node` in the unit's `PATH` if they live elsewhere (`command -v jass boat vercel node`), for example under nvm or a global npm prefix. The tick logs to `~/.cache/jass/scale/scale-<date>.log` and its errors to the journal (`journalctl --user -u jass-board-scale`).

On macOS, run a tick every 2 minutes with launchd (`launchctl load ~/Library/LaunchAgents/jass.board-scale.plist`; `unload` to stop):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>jass.board-scale</string>
  <key>ProgramArguments</key>
  <array><string>/bin/zsh</string><string>-lc</string><string>jass board scale</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/Users/you/.local/bin:/Users/you/.ascii/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>StartInterval</key><integer>120</integer>
  <key>StandardErrorPath</key><string>/tmp/jass-board-scale.err</string>
</dict>
</plist>
```

Put the folders of `jass`, `boat`, `vercel` and `node` in that `PATH` (`command -v jass boat vercel node`): a login `zsh` reads `.zprofile` but not `.zshrc`. The tick logs to `~/.cache/jass/scale/scale-<date>.log` and its errors to `/tmp/jass-board-scale.err`. A tick that finds an earlier one still resuming a VM exits at once.

## Add a skill

1. Create `skills/<name>/SKILL.md` with `name` and `description` frontmatter. Follow the `writing-for-agents` skill.
2. Add `<name>` to `skills.json`.
3. Commit and push, then `jass update --apply` on each box. Existing skills update through their links without any command.

## Add a rule

Edit `rules/global.md`. Keep it short: every byte loads in every session, and `jass doctor` warns past 2,000 bytes for the whole file. Commit, push, then `jass update --apply` on each box. Never edit the block between the `jass:begin` and `jass:end` markers by hand; `jass update` replaces it. A marker counts only on a line of its own at column zero outside code fences and indented code, so examples of the markers elsewhere in the file are left alone.

## Add a private layer

For a work machine with company skills and rules:

1. Create a private repository shaped like this one: `skills.json`, `skills/<name>/SKILL.md`, and optionally `rules/global.md`. Skill names must differ from the stack's.
2. Clone it on the work box and point the box at a separate work lore repo:

   ```sh
   jass init --box work-laptop --zone work --lore-path ~/lore-work --lore <work-lore-url> --layer ~/Development/company-layer --apply
   ```

The layer's skills are linked next to the stack's, and its rules are appended to the managed block. Personal boxes never list the layer, and the zone guard keeps the two lore repos apart.

## Run a review

Every week or two, say "lore review". The `lore` skill reads recent session digests and logs, turns recurring friction into lessons, trims hubs over budget, writes `Reviews/<year>-W<week>.md` and lists proposed stack changes for you to accept or reject. It never edits the stack itself.

## Roll back

Every `jass init --apply` and `jass update --apply` that changed something prints a manifest path. To undo that one transaction:

```sh
jass restore ~/.config/jass/backups/<stamp>/manifest.json
```

It first checks that nothing it wrote has changed since; if something has, it restores nothing and tells you which file to look at. An `--apply` that failed midway prints the same command in its error; restore then undoes the steps that ran. The lore clone runs before any change outside the lore, so a clone that fails leaves the box as it was. The lore is never touched by a restore: undo lore changes with git in the lore folder (`jass path lore`).

The skills step keeps a manifest of its own next to each skill root: `~/.agents/skill-backups/<stamp>/manifest.json` and `~/.claude/skill-backups/<stamp>/manifest.json`, with the backups of any replaced skills. `jass restore` undoes it through the `jass` manifest once the step has finished. If the process was killed during the skills step, the `jass` manifest does not list it yet; undo the links it made with the newest of those manifests, the one whose `status` is still `installing`, then run `jass restore` for the rest:

```sh
python3 "$(jass path stack)/scripts/install.py" --restore ~/.agents/skill-backups/<stamp>/manifest.json
```
