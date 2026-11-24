---
name: ios-sim
description: Test a web app in real Mobile Safari on an iOS Simulator with the `iosim` CLI. Drive the page, run JavaScript in it, take screenshots and video, audit it at iPhone size and at large Text Sizes, and check what the captures show. Use for "test on iPhone", "iOS Safari" or "Simulator" checks of a web page, including from a box with no Simulator.
---

# ios-sim

`iosim` drives Mobile Safari on an iOS Simulator and answers in JSON. Set it once:

```sh
IOSIM="$HOME/.agents/skills/ios-sim/scripts/iosim"   # where `jass` links it (not on PATH); Python 3.9+, stdlib only; runs on the Mac with the Simulator
```

Every command prints one JSON object with `ok`, and on failure `error` and `fix`. Exit codes: 0 means passed, 1 means a failure or a failed check, and 2 means bad arguments or a bad plan. `$IOSIM <command> --help` lists the flags. [references/cli.md](references/cli.md) explains each command, how it works inside, and its gotchas.

## 1. Find the Simulator

Run `$IOSIM doctor`. The answer decides the path:

- **`simulator` check fails** (Linux, or a Mac without Xcode): this box is not the Mac. Follow [references/remote.md](references/remote.md): delegate to the Mac's T3 environment or use SSH, and make your dev server reachable from the Simulator.
- **A required check fails**: apply its `fix`, then rerun `doctor`.
- **`agent-device session` fails**: another session holds the simulator. Close it only if it is yours; otherwise wait or ask the user.
- **`ok: true`**: you are on the Mac. Without `agent-device` there are no taps, typing or swipes; without `ffmpeg` contact sheets come from agent-device.

## 2. Bind the device (inside T3 Code)

Inside T3, device actions go through the `agent-device` command that T3's `device_open` returns, so the user watches them in the Device panel. Call `device_list`, then `device_open` with the simulator's `hostId` and `id`, and hand iosim the command:

```sh
export IOSIM_AGENT_DEVICE="<the agent-device path device_open returned>"
```

iosim refuses when that command drives a different simulator. Outside T3, it uses an `agent-device` on PATH, or falls back to `simctl` (no taps or typing).

## 3. Test the page

1. `$IOSIM device ensure --name "<a name of your own>"` finds or creates an iPhone 15 and boots it. Without `--name` it may reuse a simulator another agent is using. When several are booted, pass `--device <udid|name>` or set `IOSIM_DEVICE`.
2. Make the URL reachable from the Mac: `http://localhost:<port>` on the Mac itself; see remote.md for a server on another box.
3. Write a plan ([references/plan.md](references/plan.md)) and run it:

   ```sh
   $IOSIM run plan.json --out <evidence-dir> --var BASE=http://localhost:3000
   ```

   It writes numbered screenshots, `video.mp4`, `contact-sheet.png` and `report.json`, and exits 1 when an assertion, an audit or a step fails. `report.json` says `passed: true` only when every step ran and nothing failed.
4. For one-off checks, single commands work on the same page: `open`, `eval`, `console`, `tap`, `type`, `scroll`, `shot`, `audit`. Later commands act on the tab the last `open` loaded; run `open` again when one says the tab is gone or hidden.

## 4. Check the evidence

A green report proves only what its assertions and audits test. Read the PNGs and the contact sheet yourself: each should show Safari with the expected page, and text should be larger in the accessibility-size shots. `report.json` → `shots[]` flags a wrong size or a blank frame, `audits[]` lists each finding with a selector and a rect, and `console` lists errors since the last page load.

The built-in audit fails on sideways scroll, tap targets under 44 x 44 CSS px, text inputs under 16 px (iOS zooms the page when one gets focus) and a missing `width=device-width`. It warns about small inline links, tiny text, controls with no accessible name, images without alt, and blocked pinch zoom. A project can pass its own audit script; plan.md lists the accepted shapes.

## Real input versus JavaScript

Use `tap` and `type` (real touches and keyboard) whenever the behaviour under test depends on the input. `tap --selector` passes only when the page itself saw the touch on the target. When it refuses (several matches, a covered element, an ambiguous position), use `--first`, or `tap --label` / `--at` with `--expect <selector>`: without `--expect` those exit 0 even when the touch missed. A JavaScript `el.click()` or `el.value = ...` is not a substitute when:

- the page listens for touch or pointer events, or for `:active` and hover states;
- something covers the element: a real tap hits the overlay, `click()` does not;
- the test needs the software keyboard, the zoom-on-focus behaviour, or the viewport resize the keyboard causes;
- the page uses `input` or `beforeinput` events (React-controlled inputs ignore a plain `.value =`).

`scroll --to <selector>` is programmatic; `scroll down` is a real swipe.

## Text Size

Text Size (Dynamic Type) changes text only where the page uses `font: -apple-system-body` (or another `-apple-system-*` font); px and rem fonts stay put. So a large Text Size test shows whether the page opts into Dynamic Type, and how its layout copes when it does. Sizes go from `extra-small` to `accessibility-extra-extra-extra-large`; `large` is the default, and `ax1` to `ax5` are short names for the accessibility sizes.

## Limits

- A Simulator is not an iPhone. Touch latency, scrolling physics, GPU, memory and network are the Mac's, so performance numbers do not transfer. Camera, Face ID prompts, Apple Pay and the on-device keyboard feel differ.
- Home Screen web apps and their standalone mode are untested here. Check those on a real iPhone.
- iosim drives Safari pages only, not WKWebView inside an app. It has no network throttling.

## Finish

1. Run `$IOSIM cleanup`. It stops this device's inspector proxy and any recording iosim started, and closes only its own agent-device session.
2. Inside T3, then call `device_close`. A session left open locks the simulator for the next agent ("Device is already in use").
3. Leave the simulator booted unless you booted it only for this task (`$IOSIM device shutdown`).
4. Report the evidence folder path, the failures, and what you saw in the screenshots.
