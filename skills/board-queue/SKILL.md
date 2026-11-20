---
name: board-queue
description: Queue self-contained work (maintenance, dependency bumps, bug fixes, reviews) as Vercel Board tasks that `jass board work` runs as T3 Code threads. Use when the user asks to queue, hand off or batch work for the board or the workers, or asks what the board queue holds.
---

Tasks go to the board with `jass board add`; a worker on each box (`jass board work`) claims them and runs each as its own T3 thread titled `[board] …`, in a new worktree. The owner watches and answers those threads in T3 Code. `jass board --help` lists the options.

## 1. Check the project key

Run `jass board status` and read `board.projects` in `~/.config/jass/box.json`. The key there (such as `shop`) names the channel `build/<key>`. A project with no key has no worker on this box: tell the owner and stop, rather than queue work nothing will pick up.

## 2. Write each brief for a stranger

The thread starts with no memory and no access to this conversation. Each brief says, in this order:

- what to change and why, in one or two sentences;
- the files or areas it owns, and what is out of scope;
- the command that proves it is done (`Done when: npm test -- order passes`);
- whether to open a pull request (the default is a commit on the worktree's branch only).

Keep one deliverable per task; a brief with more than three deliverables is two tasks. Never put secrets, tokens or credentials in a brief: the whole team can read the board.

## 3. Queue

```sh
jass board add <key> "<brief>"                         # prints the task id
jass board add <key> --needs <id> "Review <id>'s branch: <what to check>. Change nothing; report problems."
```

Add a review task with `--needs` for anything whose result others will rely on: a completed task is the thread's claim, not proof. Use `--model`, `--provider` and `--effort` only when the owner asks for a specific model.

Done when `jass board status` lists every task, and you have told the owner the ids and that a worker must be running (`jass board work`) for them to start.

## 4. Follow up

`jass board status` shows each open task's state and its T3 thread. `waiting for an answer in T3` means a thread asked the owner a question: point them at that thread. A failed task's reason is on the board (`vercel board read <id>`); `vercel board tasks reopen <id>` retries it in the same thread.
