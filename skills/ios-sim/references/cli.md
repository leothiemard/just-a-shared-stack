# iosim commands

Global flags go before the command: `--device <udid|name>` (default `IOSIM_DEVICE`, else the only booted iOS simulator) and `--agent-device "<command>"` (default `IOSIM_AGENT_DEVICE`).

| Command | What it does |
| --- | --- |
| `doctor` | Checks Python, Xcode, the iOS runtime, devices, `ios_webkit_debug_proxy`, agent-device, Node and ffmpeg. Exits 1 on a required miss and gives the fix for each. On a box with no Simulator, it points at the remote path and tests `IOSIM_REMOTE` when that is set. |
| `device list` | iOS simulators with UDID, state and runtime. |
| `device ensure [--type "iPhone 15"] [--name N] [--no-boot]` | Reuses a simulator of that type (booted ones first) or creates `<type> (iosim)` on the newest runtime, then boots it. |
| `device boot`, `device shutdown`, `device status` | Shutting down also stops the device's proxy. `status` shows Text Size, appearance, proxy, recording and driver. |
| `text-size [SIZE]`, `appearance [light\|dark]` | Read, or set then read. |
| `open URL [--new-tab] [--no-wait] [--timeout 45]` | Navigates the current Safari tab (one tab, reused) or, with `--new-tab` or when Safari has no page, opens the URL through the device. Waits for `readyState == "complete"` on the new document. Reports `finalUrl` and whether it redirected. |
| `pages` | The Safari pages the inspector sees. |
| `eval EXPR`, `eval --body CODE`, `eval --file F` | Runs JavaScript in the page and prints `value` as JSON. Promises are awaited. A page exception exits 1 with its message. An expression cannot contain statements: use `--body` with `return`. |
| `console [--all]` | Errors (or every message) since the page loaded: console calls, uncaught exceptions and unhandled rejections. |
| `wait --selector S \| --text T \| --js E \| --ms N` | Polls every 250 ms up to `--timeout` (15 s). |
| `tap --selector S \| --label L \| --at X Y` | A real tap through agent-device. `--selector` scrolls the element into view if needed, measures it in the page, and taps its centre on screen. |
| `type TEXT [--selector S] [--enter] [--keep-keyboard]` | Types with the keyboard. Taps the field first when a selector is given, and dismisses the keyboard afterwards. |
| `scroll up\|down\|top\|bottom [--amount F]`, `scroll --to S` | A swipe through agent-device, or a programmatic `scrollIntoView`. |
| `shot PATH` | A screenshot at device resolution (1179 x 2556 on an iPhone 15), with the `check-shot` result. |
| `record start PATH`, `record stop` | A screen recording. The state lives in `IOSIM_HOME`, so start and stop can be separate calls. |
| `check-shot PATH [--expect WxH]` | PNG size, and whether it is blank (luminance spread from a `sips` thumbnail, macOS only). |
| `contact-sheet VIDEO --out PNG [--frames 12]` | Tiles frames into one PNG: ffmpeg when installed (evenly spaced frames, 4 columns), else `agent-device record contact-sheet` (macOS, cells where the screen changed). |
| `audit [--script F] [--min-target 44] [--min-input-font 16] [--warn-only]` | The page audit (see SKILL.md). Exits 1 on any `fail` finding. |
| `run PLAN --out DIR [--var K=V] [--check]` | Runs a plan (see plan.md). `PLAN` may be `-` for stdin. |
| `proxy start\|stop\|restart\|status` | This device's inspector proxy. Other commands start it as needed. |
| `cleanup` | Stops the proxy and any recording iosim started on the device. |
| `remote ARGS…` | Runs `iosim ARGS…` on the Mac named by `IOSIM_REMOTE` (see remote.md). |

## How it works

- **JavaScript, console, audits**: `ios_webkit_debug_proxy -s unix:<socket>` bridges the simulator's Web Inspector socket to WebSockets. iosim finds the socket of one simulator with `xcrun simctl getenv <udid> RWI_LISTEN_SOCKET`. It runs one proxy per simulator, from port `IOSIM_PROXY_PORT` (default 9320; pages on port + 1), and moves 20 ports up when the block is taken. The proxy restarts by itself when the simulator reboots and its socket moves. The WebKit page socket speaks to targets: each command is wrapped in `Target.sendMessageToTarget`, and answers come back in `Target.dispatchMessageFromTarget`. A bare `Runtime.evaluate` answers "'Runtime' domain was not found". WebKit ignores `awaitPromise` on `Runtime.evaluate`, so iosim awaits the returned promise with `Runtime.awaitPromise`. A cross-site navigation swaps the target (`Target.didCommitProvisionalTarget`), and iosim follows it. `Console.enable` replays the messages the current document has logged, so `console` sees errors from page load as well.
- **Device actions**: these go through agent-device when one is given or on PATH: opening a URL in a new tab, tap, type, swipe, screenshot (`--pixel-density 3`), recording (`--scope device`), Text Size and appearance. Otherwise iosim uses `simctl openurl`, `simctl io screenshot`, `simctl io recordVideo` (stopped with SIGINT) and `simctl ui`. agent-device needs a Safari session, so iosim opens Safari through it once per command.
- **Selector taps**: the element's rect comes from the page in CSS px, which equal screen points at zoom 1 (iosim multiplies by `visualViewport.scale` otherwise). The web area's origin on screen comes from an agent-device accessibility snapshot: the first full-width node under the WebView that starts below its top, which is y = 59 under the status bar on an iPhone 15.
- **State**: proxy and recording state live in `IOSIM_HOME` (default `~/.cache/iosim`), so separate calls share them.

## Gotchas

- Safari's first agent-device action on a fresh session builds an XCTest runner, which can take a couple of minutes. Later calls take about a second. The first accessibility snapshot of a page can take 20 s or more.
- Opening through the device always adds a tab. `open` without `--new-tab` reuses the current one.
- `localhost` inside the Simulator is the Mac.
- `simctl ui content_size` and `agent-device settings text-size` take the same names.
