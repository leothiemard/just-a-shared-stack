---
name: overnight
description: Unattended agent work that must still be running hours later. Use when the user leaves agents working overnight or while away, asks for a pre-flight check before leaving, or asks for a night watch.
---

Unattended work stops in a few known ways: its box goes away (sleep, a crashed login session, a dropped network), the box runs out of memory, a provider limit is reached, a thread ends early, or a thread waits for the owner. Each step closes some of them. Do the steps in order while the owner is still here; `jass night --help` and `jass heavy --help` describe the commands.

## 1. Plan

Show the owner a table with one row per lane: its goal, its box, its thread, its cap on pull requests and its fences. State the deadline in UTC and in local time.

In the same message ask for everything only the owner can give before the deadline: sign-ins, 2FA codes, passwords, installs that prompt, approvals, product decisions. What they cannot give now leaves the plan or becomes a "For the owner" item in that lane's brief.

Done when every lane has a row and no row needs the owner before the deadline.

## 2. Pre-flight every box

On each box that hosts a lane, run:

```sh
jass night check --hours <hours> --agents <agents on this box, delegates included> --need '<command>' --peer <other box>
```

Give one `--need` per sign-in, tool or device a lane depends on (`--need 'gh auth status'`); it must exit 0. No box can see inside another, so on a linked machine run the check there through a delegate.

A `fail` row names something that has stopped a night before, and its fix. Apply a fix that lasts only for the night yourself. A fix that changes a system setting (power, swap, services, the network) is the owner's: show them the command and wait. When the agents do not fit in memory, move a lane to another box or night; adding swap or hoping does not make them fit.

Done when no box reports a `fail` and the owner has seen every `warn`.

## 3. Brief

Send each lane's thread the unattended brief in [references/brief.md](references/brief.md), every part filled in. Threads run in a mode that never prompts for approval.

Done when every lane's thread has acknowledged its brief and started.

## 4. Watch

Start the watchdog on each box, naming every lane on it:

```sh
jass night watch --hours <hours> --thread '<title or id>' --thread '<title or id>' --keep-going --awake --peer <other box>
```

It needs no model: it sends a failed thread a message to continue once the network or memory is back, reminds a thread that ended early of its standing mandate (`--keep-going`), holds the box awake (`--awake`) and logs everything. It leaves alone what it cannot fix and raises an alert: a thread that waits for the owner, a usage limit, a thread stopped by a person, a thread that fails four times.

- The owner starts it in a terminal that outlives the session (`tmux`, or `systemd-run --user --collect --unit=jass-night jass night watch ...` on Linux). This is the default: nothing more is needed.
- A watcher thread is worth its tokens only when lanes will need new work or judgement during the night. It runs the same command with `--wake` as a background command, handles each alert by [references/watching.md](references/watching.md), and starts the watch again.

Done when the watch's first line names every lane and the owner knows where its log is.

## 5. Report

At the deadline, read each lane's final report and the watch log (`~/.cache/jass/night/`). Tell the owner per lane what is done with its proof, what is not and why, and every "For the owner" item; then what interrupted the night, with times from the log. Record a cause the lore does not have yet with the `lore` skill.
