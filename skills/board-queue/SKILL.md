---
name: board-queue
description: Queue self-contained work (maintenance, dependency bumps, bug fixes, reviews) as Vercel Board tasks that `jass board work` runs as T3 Code threads. Use when the user asks to queue, hand off or batch work for the board or the workers, or asks what the board queue holds.
---

Tasks go to the board with `jass board add`; a worker on each box (`jass board work`) claims them and runs each as its own T3 thread titled `[board] …`, in a new worktree. The owner watches and answers those threads in T3 Code. `jass board --help` lists the options.

## 1. Check the project key

Run `jass board status` and read `~/.config/jass/box.json`. A key (such as `shop`) names the channel `build/<key>`, and two lists hold the keys that have workers:

- `board.projects`: workers on boxes that run `jass board work`, this one among them;
- `scale.channels`: cloud VMs that `jass board scale` wakes while their tasks wait (such as `shop-boat`). Use one of these keys when the owner asks for the VMs or the cloud, and the plain key for work that needs this box's data, devices or credentials.

A project in neither list has no worker: tell the owner and stop, rather than queue work nothing will pick up.

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

Done when `jass board status` lists every task, and you have told the owner the ids and what starts them: a running worker (`jass board work`) for a `board.projects` key, the scaler's schedule for a `scale.channels` key.

## 4. Follow up

`jass board status` shows each open task's state and its T3 thread with the box it runs on. A thread on another box is readable when that box is a linked environment (`t3_environment_links`): pass its `environmentId` to the T3 thread tools. A cloud VM is reachable only while it runs; once it stops, read its threads' digests in the lore (`jass recall "<task>" --box <vm box>`). `jass board report --since <6h or a time>` lists what finished, failed or is stuck, with box, thread and PR links. `jass board open` shows the owner the board in a browser (it runs until Ctrl-C, so suggest it rather than run it). `waiting for an answer in T3` means a thread asked the owner a question: point them at that thread. A failed task's reason is on the board (`vercel board read <id>`); `vercel board tasks reopen <id>` retries it in the same thread.
