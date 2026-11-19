---
name: ios-sim
description: Test a web app in real Mobile Safari on an iOS Simulator with the `iosim` CLI. Drive the page, run JavaScript in it, take screenshots and video, audit it at iPhone size and at large Text Sizes, and check what the captures show. Use for "test on iPhone", "iOS Safari" or "Simulator" checks of a web page, including from a box with no Simulator.
---

# ios-sim

`iosim` drives Mobile Safari on an iOS Simulator and answers in JSON. Set it once:

```sh
IOSIM="<this skill's directory>/scripts/iosim"   # Python 3.9+, stdlib only; runs on the Mac with the Simulator
```

Every command prints one JSON object with `ok`, and on failure `error` and `fix`. Exit codes: 0 means passed, 1 means a failure or a failed check, and 2 means bad arguments or a bad plan. `$IOSIM --help` and `$IOSIM <command> --help` list the flags. [references/cli.md](references/cli.md) explains each command and how it works inside.

## 1. Find the Simulator

Run `$IOSIM doctor`. The answer decides the path:

- **`simulator` check fails** (Linux, or a Mac without Xcode): this box is not the Mac. Follow [references/remote.md](references/remote.md). It covers delegating to the Mac's T3 environment (works today) and SSH (needs Remote Login), and how the Simulator reaches your dev server.
- **A required check fails**: apply its `fix`, then rerun `doctor`.
- **`ok: true`**: you are on the Mac. The optional checks only limit features: without `agent-device` there are no taps, typing or swipes; without `ffmpeg` contact sheets come from agent-device.

## 2. Bind the device (inside T3 Code)

Inside T3, every app launch, tap, text entry, screenshot, recording and setting on a device goes through the `agent-device` command that T3's `device_open` returns, so the user watches it in the Device panel. Call `device_list`, then `device_open` with the simulator's `hostId` and `id`, and hand iosim the returned command:

```sh
export IOSIM_AGENT_DEVICE="<the agent-device path device_open returned>"
```

iosim then uses that command for those actions, and uses the Web Inspector (not a device action) for JavaScript. Outside T3, iosim adds `--platform ios --udid <udid>` to an `agent-device` on PATH, or falls back to `simctl` for opening URLs, screenshots, video and settings. Taps and typing need agent-device.

## 3. Test the page

1. Pick the device. `$IOSIM device ensure` finds or creates an iPhone 15 and boots it. Pass `--device <udid|name>` (or set `IOSIM_DEVICE`) when several are booted.
2. Make the URL reachable from the Mac. On the Mac itself, `http://localhost:<port>` works. For a server on another box, see remote.md.
3. Write a plan and run it. The format is in [references/plan.md](references/plan.md).

   ```sh
   $IOSIM run plan.json --out <evidence-dir> --var BASE=http://localhost:3000
   ```

   The run writes numbered screenshots, `video.mp4`, `contact-sheet.png` and `report.json` to the evidence folder. It exits 1 when an assertion, an audit or a step fails. It restores the Text Size and appearance it changed.
4. For one-off checks, single commands work on the same page: `open`, `eval`, `console`, `tap`, `type`, `scroll`, `shot`, `audit`.

## 4. Check the evidence

A green report proves only what its assertions and audits test. To judge how the page looks, read the PNGs and the contact sheet with your image viewer. Confirm that each one shows Safari with the expected page, and that text is larger in the accessibility-size shots. `report.json` → `shots[]` already flags a wrong size or a blank frame. `audits[]` lists each finding with a selector and a rect. `console` lists errors and uncaught exceptions since the last page load.

The built-in audit fails on sideways scroll, tap targets under 44 x 44 pt, text inputs under 16 px (iOS zooms the page when one gets focus) and a missing `width=device-width`. It warns about small inline links, tiny text, controls with no accessible name, images without alt, and blocked pinch zoom. A project can pass its own audit script, described in plan.md.

## Real input versus JavaScript

Use `tap` and `type` (real touches and keyboard through agent-device) whenever the behaviour under test depends on the input. A JavaScript `el.click()` or `el.value = ...` is not a substitute when:

- the page listens for touch or pointer events, or for `:active` and hover states;
- something covers the element: a real tap hits the overlay, `click()` does not;
- the test needs the software keyboard, the zoom-on-focus behaviour, or the viewport resize the keyboard causes;
- the page uses `input` or `beforeinput` events (React-controlled inputs ignore a plain `.value =`).

`scroll --to <selector>` is programmatic (`scrollIntoView`). A swipe (`scroll down`) is a real gesture.

## Text Size

Text Size (Dynamic Type) changes text only where the page uses `font: -apple-system-body` (or another `-apple-system-*` font). Safari restyles those fonts live, with no reload. Fonts set in px or rem stay put, and the root stays 16 px. So a large Text Size test shows whether the page opts into Dynamic Type, and how its layout copes when it does. Sizes go from `extra-small` to `accessibility-extra-extra-extra-large`; `large` is the default, and `ax1` to `ax5` are short names for the accessibility sizes.

## Limits

- A Simulator is not an iPhone. Touch latency, scrolling physics, GPU, memory and network are the Mac's, so performance numbers do not transfer. Safari features that need hardware (camera, Face ID prompts, Apple Pay) and the on-device keyboard feel differ.
- Adding a web app to the Home Screen and its standalone mode are untested here. Check those on a real iPhone.
- iosim drives Safari pages only, not WKWebView inside an app. It has no network throttling.
- `safaridriver` (WebDriver) does not reach the Simulator with current Safari, so iosim uses the Web Inspector protocol through `ios_webkit_debug_proxy`.

## Finish

Run `$IOSIM cleanup`. It stops this device's inspector proxy and any recording iosim started. Leave the simulator booted unless you booted it only for this task (`$IOSIM device shutdown`). Report the evidence folder path, the failures, and what you saw in the screenshots.
