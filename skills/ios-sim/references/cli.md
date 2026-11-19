# iosim commands

Global flags go before the command: `--device <udid|name>` (default `IOSIM_DEVICE`, else the only booted iOS simulator) and `--agent-device "<command>"` (default `IOSIM_AGENT_DEVICE`).

| Command | What it does |
| --- | --- |
| `doctor` | Checks Python, Xcode, the iOS runtime, devices, `ios_webkit_debug_proxy`, agent-device, Node and ffmpeg. Exits 1 on a required miss and gives the fix for each. With agent-device, it also checks that the device is not held by another agent-device session (with a given command, by opening Safari through it, which also checks the command drives this simulator). On a box with no Simulator, it points at the remote path and tests `IOSIM_REMOTE` when that is set. |
| `device list` | iOS simulators with UDID, state and runtime. |
| `device ensure [--type "iPhone 15"] [--name N] [--no-boot]` | With `--name`, finds or creates that simulator. Without it, reuses any simulator of that type (booted ones first, so possibly one another agent is using) or creates `<type> (iosim)`. Creation uses the newest runtime that supports the type. Then boots it. |
| `device boot`, `device shutdown`, `device status` | Shutting down also stops the device's proxy. `status` shows Text Size, appearance, proxy, recording and driver. |
| `text-size [SIZE]`, `appearance [light\|dark]` | Read, or set then read. |
| `open URL [--new-tab] [--no-wait] [--timeout 45]` | Navigates the current Safari tab (one tab, reused) or, with `--new-tab` or when Safari has no page, opens the URL through the device. Waits for `readyState == "complete"` on a document that loaded after the call. Fails at once when an http(s) URL ends on Safari's error page (a `data:` URL, for example a server that is down); a redirect to another http(s) page passes with `redirected: true`. `about:blank` blanks the current tab and needs one. Reports `finalUrl` and whether it redirected. |
| `pages` | The Safari pages the inspector sees. |
| `eval EXPR`, `eval --body CODE`, `eval --file F` | Runs JavaScript in the page and prints `value` as JSON. Promises are awaited. A page exception exits 1 with its message. An expression cannot contain statements: use `--body` with `return`. A file may hold either: iosim reads it as an expression first, and as a function body only if that does not parse. If the connection drops after the code was sent, iosim says so rather than running it again. |
| `console [--all]` | Errors (or every message) since the page loaded: console calls, uncaught exceptions and unhandled rejections. |
| `wait --selector S \| --text T \| --js E \| --ms N` | Polls every 250 ms up to `--timeout` (15 s). An expression that throws fails at once. |
| `tap --selector S [--first] \| --label L [--contains] \| --at X Y` | A real tap through agent-device. `--selector` must match one element (`--first` takes the first of several); iosim scrolls it into view if needed, hit-tests its centre with `elementFromPoint` (and fails naming the element that covers it), places the page on screen, and taps the centre. `--label` is the exact accessibility label as `agent-device snapshot` shows it; `--contains` matches a case-insensitive part of it and must find one element. |
| `type TEXT [--selector S] [--first] [--enter] [--keep-keyboard]` | Types with the keyboard. With a selector, taps the field first and fails, typing nothing, unless it then has focus (`document.activeElement`). Dismisses the keyboard afterwards. |
| `scroll up\|down\|top\|bottom [--amount F]`, `scroll --to S` | A swipe through agent-device, or a programmatic `scrollIntoView`. |
| `shot PATH` | A screenshot at device resolution (1179 x 2556 on an iPhone 15), with the `check-shot` result. |
| `record start PATH`, `record stop` | A screen recording. The state lives in `IOSIM_HOME`, so start and stop can be separate calls. Expect 0.15 to 0.6 MB a second at full resolution (a still page is small, scrolling is large); agent-device keeps the captured resolution. To shrink one afterwards on macOS: `avconvert --preset Preset960x540 --source video.mp4 --output small.mp4`. |
| `check-shot PATH [--expect WxH]` | PNG size, and whether it is blank (luminance spread from a `sips` thumbnail, macOS only). |
| `contact-sheet VIDEO --out PNG [--frames 12]` | Tiles frames into one PNG: ffmpeg when installed (evenly spaced frames, 4 columns), else `agent-device record contact-sheet` (macOS, cells where the screen changed). |
| `audit [--script F] [--min-target 44] [--min-input-font 16] [--warn-only]` | The page audit (see SKILL.md), sizes in CSS px. Exits 1 on any `fail` finding. `--script` takes a project's own file in the shapes plan.md lists. |
| `run PLAN --out DIR [--var K=V] [--check]` | Runs a plan (see plan.md). `PLAN` may be `-` for stdin. |
| `proxy start\|stop\|restart\|status` | This device's inspector proxy. Other commands start it as needed. |
| `cleanup [--keep-session]` | Stops the proxy and any recording iosim started on the device, and closes the agent-device session of the command in use (the given command's own, or `iosim-<udid>`), never another. Inside T3, call `device_close` afterwards. |
| `remote ARGS…` | Runs `iosim ARGS…` on the Mac named by `IOSIM_REMOTE` (see remote.md). |

## How it works

- **JavaScript, console, audits**: `ios_webkit_debug_proxy -s unix:<socket>` bridges the simulator's Web Inspector socket to WebSockets. iosim finds the socket of one simulator with `xcrun simctl getenv <udid> RWI_LISTEN_SOCKET`. It runs one proxy per simulator, from port `IOSIM_PROXY_PORT` (default 9320; pages on port + 1), and moves 20 ports up when the block is taken. The proxy restarts by itself when the simulator reboots and its socket moves. The WebKit page socket speaks to targets: each command is wrapped in `Target.sendMessageToTarget`, and answers come back in `Target.dispatchMessageFromTarget`. A bare `Runtime.evaluate` answers "'Runtime' domain was not found". WebKit ignores `awaitPromise` on `Runtime.evaluate`, so iosim awaits the returned promise with `Runtime.awaitPromise`. A cross-site navigation swaps the target (`Target.didCommitProvisionalTarget`), and iosim follows it. `Console.enable` replays the messages the current document has logged, so `console` sees errors from page load as well.
- **Device actions**: these go through agent-device when one is given or on PATH: opening a URL in a new tab, tap, type, swipe, screenshot (`--pixel-density 3`), recording (`--scope device`), Text Size and appearance. Otherwise iosim uses `simctl openurl`, `simctl io screenshot`, `simctl io recordVideo` (stopped with SIGINT) and `simctl ui`. agent-device needs a Safari session, so iosim opens Safari through it once per command.
- **Selector taps**: the element's rect comes from the page in CSS px, which equal screen points at zoom 1 (iosim subtracts the `visualViewport` offset and multiplies by its scale otherwise). Where the page sits on screen comes from anchors: the target and up to 29 other visible named elements, matched by label and size against the nodes inside the WebView of an agent-device accessibility snapshot; the screen offset most of them agree on wins (y = 59 under the status bar on an iPhone 15). This holds with Safari's compact toolbar, its two-row form at accessibility Text Sizes and the collapsed bar after a scroll. When no anchor matches, iosim uses the WebView node labelled with the page's title, and otherwise refuses rather than guess. A centre outside the page's visible area (under Safari's toolbar) is refused too.
- **Device binding**: agent-device's `open` answers with the simulator it acted on; iosim compares it with the one it resolved before any device action. A `DEVICE_IN_USE` answer becomes an error that names the session holding the device and how to free it.
- **State**: proxy, recording and agent-device session state live in `IOSIM_HOME` (default `~/.cache/iosim`), so separate calls share them. A per-simulator lock keeps two commands from starting two proxies, and iosim checks a saved pid is still `ios_webkit_debug_proxy` (or `simctl`) before signalling it.

## Gotchas

- The first agent-device action on a machine may need to build its XCTest runner, which can take a while; usually it is already built and a call takes about a second. The first accessibility snapshot of a page can take several seconds.
- Opening through the device (`--new-tab`) usually adds a tab, but for a URL a tab already shows, Safari may reload that tab instead. `open` without `--new-tab` reuses the current tab.
- Safari can hold several tabs; iosim drives the page whose URL matches the last `open`. Keep to one tab when you can.
- `localhost` inside the Simulator is the Mac.
- `simctl ui content_size` and `agent-device settings text-size` take the same names.
