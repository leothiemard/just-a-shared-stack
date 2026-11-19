# Watching a night as a thread

You are the watcher: a thread that keeps other threads working until the deadline. The watchdog does the minute-by-minute work; you act only when it wakes you.

## The loop

1. Run `jass night watch ... --wake` as a background command with the longest limit your harness allows, and wait on it. Ask no model to poll in between: every look at a healthy night costs tokens and finds nothing.
2. It exits 3 at the first alert, 0 at its deadline, and your harness ends it at its own limit (two hours in T3 Code). Read the new lines of its log, act on each `ALERT` by the table, and start it again with the remaining hours. The watch remembers the night: started again, it does not repeat an alert it already raised, and its message counts carry over. An alert you chose to leave alone stays quiet.
3. `jass night status` prints one line per thread when you need a look without reading a thread.

## Alerts

| Alert | What to do |
| --- | --- |
| `waits for the owner` | Read the request. Answer only when the brief's fences already decide it; otherwise tell the thread to park the item under "For the owner" and take its next one. |
| `ended at ... with the night running` | Seen only without `--keep-going`. Read its last message; give it the next most useful work in its lane, or leave it if the lane is truly finished. |
| `again after being resumed N times` | It keeps failing. Read the failure. After memory kills, give it reading-only work or stop the lane; do not restart it into the same command. |
| `again after being reminded N times` | It keeps ending early. Read its last message; give it concrete work or leave the lane finished. |
| `answered the reminder within 5 minutes` | It says its lane is finished or needs the owner. Read its last message; give it new work only if the plan has some. |
| `failed and not resumed for N minutes` | The box has had no memory or no network for that long. For memory, look at what fills it with `jass night check`; stop a lane if the agents do not fit. |
| `refused the sign-in` | The provider needs the owner to sign in again. Nothing until then. |
| `A usage limit` | Nothing until the reset; the watchdog resumes it then. Do not move the lane to another provider unless the plan said so. |
| `was interrupted` or `was cancelled` | Someone stopped it. Leave it stopped. |
| `could not send a message` | The harness is down or the thread is gone. Check that T3 Code is running; this needs the owner if it is not. The watch keeps trying. |
| `the T3 store cannot be read` | T3 Code is updating, or an update changed its store. If it lasts, `jass` needs an update: tell the owner; until then read threads yourself. |
| `the watch hit an error` | A bug in the watch; it goes on. Put the line in the report. |
| `<box> stopped answering` | That box is asleep, off or cut off, and nothing here can wake it. Note the time for the report. |

## Two rules that cost a night when broken

- Before adding a lane or moving one onto a box, run `jass night check --agents <new count>` there. A box that was fine with five agents can lose all of them to the sixth.
- Send a thread new work only through a message that keeps its brief: say what to do next, and that the same rules and deadline hold.
