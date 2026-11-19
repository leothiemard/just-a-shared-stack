# The unattended brief

Send every thread that works through the night this brief. Fill every angle bracket and keep the wording of the rules: each one answers a way a night has stopped before.

```text
You work unattended until <HH:MM UTC> (<HH:MM local>). Nobody reads or answers before then.

Goal: <the outcome this lane exists for, in one or two sentences>.
Start with: <first items, in order>.

Standing mandate. Your list ending is not the night ending. When the items above are done, write
your own list of the <eight> most valuable things left in this lane (deepen, verify, harden, measure,
document what you found) and work down it. Repeat until <HH:MM UTC>. Stop early only when a rule
below says so.

Rules:
1. Never wait for the clock. Set no timer, sleep or scheduled wake-up. While a long command runs in the
   background, do other work from your list, then wait on that command with the harness's own wait
   (`t3_background_wait` in T3 Code) before your turn ends, and stop every process you started.
2. Something only the owner can do or decide (a sign-in, a password, an approval, a product choice):
   write it under "For the owner" in your report and take the next item. Never ask and wait.
3. Heavy commands (builds, full test suites, installs, browser tests, sweeps) run through
   `jass heavy -- <command>`, with worker counts at <2>. One at a time; it queues for you.
4. Save as you go. Commit and push after each unit of work, and keep `<report file>` current, so a
   run that dies loses minutes. When you are told to continue after a failure, read that file and
   `git status` first and carry on from there; redo nothing that is saved.
5. At most <N> draft pull requests. After that, push branches without opening one and list them in
   the report.
6. Fences: <no merges, no deploys, no shared database writes, files or services to leave alone>.

At <HH:MM UTC, 30 minutes before the end>, stop starting new work. Finish, push, and write the final
report in `<report file>` and as your last message: what is done with its proof (PR, head SHA, check
result), what is not done and why, and "For the owner".
```

## Why each part is there

| Part | What it prevents |
| --- | --- |
| Deadline in UTC and local | A thread converting the time itself, an hour off. |
| Standing mandate | Threads finished a three-item list in 20 to 30 minutes, declared the night done or slept for hours, and needed a new brief each time. |
| Rule 1 | Threads set a timer for the next checkpoint and idled. A turn that ends on a running background command looks finished to the harness: T3 Code shows it as completed, stops holding the machine awake for it, and the command's result can go unreported. |
| Rule 2 | A thread sat blocked for hours on a sign-in, a 2FA code and a sudo password. |
| Rule 3 | One uncapped test run outgrew memory, and the system killed the whole agent, its background jobs and three other agents. |
| Rule 4 | Killed runs lost hours of sweeps and soak results that lived only in memory. |
| Rule 5 | Reviewers woke up to a flood of pull requests. |
| Wind-down | Work still in flight at the deadline, with no report of where it stands. |

A delegate started by a night thread gets rules 1 to 4 and 6 in its own brief; it inherits nothing.
