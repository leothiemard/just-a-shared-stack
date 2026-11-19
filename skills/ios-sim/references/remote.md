# From a box with no Simulator

The Simulator runs on one Mac. Every other box reaches it in one of two ways, and the Simulator reaches the other box's dev server over the network.

| Path | Works when | Evidence comes back as |
| --- | --- | --- |
| T3 delegation to the Mac's environment | the caller is a T3 thread whose environment is linked to the Mac (needs no extra setting; designed for, not yet exercised from another box) | the child's final message: verdict, failures and what each screenshot shows. Files stay on the Mac unless the caller names an `scp` destination. |
| SSH: `iosim remote …` | the Mac has Remote Login or Tailscale SSH on, and the caller's key is authorized (off by default; turning it on is the Mac owner's decision) | the evidence folder copied to the caller's `--out` |

## Make the dev server reachable from the Simulator

The Simulator's Safari uses the Mac's network. A URL that works in `curl` on the Mac works in the Simulator.

1. Bind the server to all interfaces, not only loopback: for example `next dev -H 0.0.0.0`, `vite --host 0.0.0.0`, or `HOST=0.0.0.0`.
2. Use the dev box's Tailscale name (MagicDNS) or its 100.x address, and the port: `http://<dev-box>:3000/`. If the app checks hosts or origins (Next.js `allowedDevOrigins`, Vite `server.allowedHosts`), allow that name.
3. Check from the Mac before testing: `curl -sI http://<dev-box>:3000/`.
4. Fallback, when the server must stay on loopback: forward a port from the Mac with `ssh -N -L 3000:localhost:3000 <dev-box>` on the Mac (this needs SSH into the dev box), then test `http://localhost:3000/`.

Cookies marked `Secure`, service workers and some APIs need HTTPS or `localhost`. A plain-HTTP Tailscale name is not a secure context, so test those through the `localhost` forward or an HTTPS URL (for example `tailscale serve`).

## T3 delegation (works; exercised from a Linux box)

1. Find the Mac with `t3_environment_links` and note its `environmentId`.
2. Call `orchestrator_capabilities` once with that `environmentId`, then `delegate_task` with `target.environmentId` (no `target.projectId` was needed). A run takes a few minutes: `mode: "wait"` with a `timeoutMs` of several minutes returns the child's report in the same call.
3. Send a brief built from the template below. Put the plan inline, because the child cannot read your files.
4. The child reports back in its final message. Read its verdict and its description of each screenshot. When you need the files, give a destination in the brief.

### Brief template

```text
Use the ios-sim skill on this Mac to test a web page in Mobile Safari on the iOS Simulator. Do not change the page's code.

URL (reachable from this Mac; check with curl first): <http://dev-box:3000/path>
Device: <iPhone 15 | a simulator name or UDID>. Leave it booted. Safari may already hold tabs from earlier runs: run `iosim pages` first, and open the URL with `iosim open` before anything else.
Text Sizes: <large and accessibility-large>
Plan (save as plan.json and run `iosim run plan.json --out <dir>`; add steps if a check needs them):
<the plan JSON, with ${BASE} and the --var to pass>
Project audit script (save beside the plan if given): <none | the script>
Evidence: keep it in <~/iosim-evidence/<project>/<date-label>> on this Mac<; then copy it with `scp -r <dir> <user@caller-host:path>`>.

Inside T3, open the device with device_list and device_open, and export IOSIM_AGENT_DEVICE=<the command device_open returned> before running iosim.
Look at every screenshot and the contact sheet yourself.
Report back: pass or fail; each failure from report.json; the audit counts per Text Size; one line per screenshot saying what it shows; the console errors; the evidence path (and the scp result). Before you finish, run `iosim cleanup`, then device_close.
```

### Open points

- Files reach the caller only through `scp` from the Mac, which needs SSH into the caller's box (the Mac's own Remote Login can stay off). T3 attachments might carry images back across environments, but that is unverified.
- Both boxes need the skill installed (`jass update --apply`; pull the stack first, because `jass update` without `--apply` does not fetch).

## SSH (needs Remote Login or Tailscale SSH on the Mac)

Name the Mac in the environment. Nothing in iosim hardcodes a host.

```sh
export IOSIM_REMOTE=<ssh destination of the Mac>          # for example user@mac-name (a Tailscale name works)
export IOSIM_REMOTE_IOSIM=<path to iosim on the Mac>     # default ~/.agents/skills/ios-sim/scripts/iosim
```

- `iosim doctor` on the calling box reports that it has no Simulator and checks `ssh -o BatchMode=yes $IOSIM_REMOTE true`. `iosim remote doctor` runs doctor on the Mac.
- `iosim remote run plan.json --out ./evidence --var BASE=http://<dev-box>:3000` resolves the plan locally: it fills in variables and inlines `eval.file` and `audit.script` files. It sends the plan on stdin, runs it in `~/iosim-runs/<id>` on the Mac, copies that folder into `./evidence` with `scp -r`, deletes the remote copy, and exits with the run's exit code.
- `iosim remote <any command>` runs that command on the Mac and prints its JSON. Paths in those arguments are the Mac's.
- On a box with no Simulator and `IOSIM_REMOTE` set, any plain `iosim <command>` is forwarded the same way.
- `IOSIM_DEVICE` and `IOSIM_PROXY_PORT` are forwarded, and so is `--device` (as `IOSIM_DEVICE`). `--agent-device` names a command on the calling box, so it is not forwarded. A forwarded `run plan.json` is resolved locally first, like `remote run`. When copying the evidence back fails, the error includes what the remote run said. iosim runs non-interactively: `BatchMode=yes`, JSON on stdout, and no prompts. It finds Homebrew tools even when a non-interactive SSH login leaves `/opt/homebrew/bin` off PATH.
- Over SSH there is no T3 Device panel. iosim uses the Mac's `agent-device` from PATH (taps and typing), else `simctl`.
- The unit tests exercise this path with stand-in `ssh` and `scp`. It has not been run against a real Mac, because Remote Login is off on the Mac this was built on.
