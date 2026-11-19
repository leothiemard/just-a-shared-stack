# Plan format

A plan is JSON: an object with `steps`, or a bare array of steps. Check one without a Simulator by running `iosim run plan.json --out x --check`. It prints the resolved plan, or exits 2 with an error that names the failing step.

```json
{
  "name": "home-at-two-sizes",
  "video": true,
  "expectShot": [1179, 2556],
  "steps": [
    {"open": "${BASE}/"},
    {"wait": {"selector": "main"}},
    {"shot": "home-default"},
    {"audit": true, "name": "default"},
    {"tap": "button[type=submit]"},
    {"assert": {"text": "Saved", "message": "submitting shows Saved"}},
    {"text-size": "accessibility-large"},
    {"shot": "home-ax-large"},
    {"audit": {"failOn": "fail", "ignore": ["tiny-text"]}, "name": "ax-large"},
    {"assert": {"noConsoleErrors": true}}
  ]
}
```

## Plan keys

| Key | Meaning |
| --- | --- |
| `steps` | Required. Runs in order. |
| `name` | A label, copied into the report. |
| `video` | `true` records the whole run to `video.mp4`, then builds `contact-sheet.png`. |
| `textSize`, `appearance` | Set before the first step. |
| `device` | UDID or name of the simulator. `--device` overrides it. |
| `expectShot` | `[width, height]` in pixels. Any screenshot of another size fails. An iPhone 15 is `[1179, 2556]`. |
| `failOnConsoleErrors` | `true` fails the run if the page logged errors since its last load. A missing `/favicon.ico` does not count (nor in `noConsoleErrors`). |
| `restore` | Default `true`: put Text Size and appearance back at the end, also after a failure, a crash, Ctrl-C or SIGTERM. |
| `vars` | Defaults for `${NAME}` placeholders. `--var NAME=value` overrides them. An unknown name is an error. |

## Steps

Each step is an object with one action key. It may also carry `name`, `timeout` (seconds), `optional: true` (a failure is reported but does not fail the run or stop it) and, for `eval`, `as`.

| Action | Short form | Long form |
| --- | --- | --- |
| `open` | `"https://…"` | `{"url", "newTab": false}`: navigates the current tab and waits for `load`. `newTab` opens the URL through the device. Fails when Safari shows its error page instead. `about:blank` blanks the current tab. |
| `wait` | `500` (ms) | `{"selector"}`, `{"text"}` (in `innerText`), `{"js"}` (truthy), or `{"ms"}`. Default timeout 15 s. A `js` that throws is polled until the timeout, which reports the last exception; one that does not parse fails at once. |
| `eval` | `"expression"` | `{"js", "body": false}` or `{"file"}`: an expression, or a function body that uses `return`. Promises are awaited. `"as": "key"` stores the value in `report.values.key`. |
| `tap` | `"css selector"` | `{"selector", "first": false}` (one match, or the first with `first`; fails if covered, if iosim cannot place the page on screen without guessing, or if the page reports that another element got the touch), `{"label", "contains": false}` (the exact accessibility label, such as a Safari button; `contains` matches part of it), or `{"at": [x, y]}` in screen points. `label` and `at` take `"expect": "selector"` to fail unless that element got the touch. |
| `type` | `"text"` | `{"text", "selector", "first": false, "enter": false, "keepKeyboard": false}`. `selector` taps the field first and fails unless it then has focus. The keyboard is dismissed afterwards unless `keepKeyboard` is true. |
| `scroll` | `"down"`, `"up"`, `"top"`, `"bottom"`, or a selector | `{"direction", "amount"}` is a real swipe (amount is a fraction of the screen, up to 0.8). `{"to": selector}` uses `scrollIntoView`. |
| `shot` | `"name"` | `{"name"}`. Saved as `NN-name.png` and checked for size and blankness. It also fails when the page iosim drives is not the tab on screen (`pageVisible: false`). |
| `audit` | `true` | `{"script", "failOn": "fail", "ignore": [rules], "minTarget": 44, "minInputFont": 16, "minTextFont": 11}`. `failOn` is `fail`, `warn` (warnings fail too) or `none` (record only). |
| `assert` | n/a | One of `{"js": expr}` (truthy), `{"text": "…"}`, `{"selector": "…"}`, `{"eval": expr, "equals": value}`, or `{"noConsoleErrors": true}`. Add `"message"` to explain a failure. |
| `text-size` | `"accessibility-large"` | A Dynamic Type size name, or `ax1` to `ax5`. The step waits 1 s for Safari to restyle. |
| `appearance` | `"dark"` | `light` or `dark`. |
| `console` | `true` | `{"all": true}` includes every level, not only errors. Records the messages in the step result. |

`tap`, `type` and a direction `scroll` need agent-device (see SKILL.md). Every other step works with `simctl` alone.

A step error (the page never loaded, or an element is missing) stops the run, unless the step is `optional`. Failed assertions, failed audits and bad screenshots are recorded, and the run continues, so one run reports every problem.

## Paths and files

`eval.file` and `audit.script` resolve relative to the plan file. iosim reads them when it parses the plan, so `iosim remote run` sends them with the plan. Evidence goes to `--out`. Steps cannot write elsewhere.

## A project audit script

iosim accepts the file in either shape, unchanged:

- **An expression**, as written for Playwright's `page.evaluate` or T3's `preview_evaluate`: for example `(async () => { …; return problems; })();`. Comments and a trailing `;` are fine.
- **A function body** that uses `return`.

iosim tries the file as an expression first and, only if it does not parse, as a body. Either way it may give a promise, and `__opts` (the step's options) is defined before it runs. The value is a bare array of findings (`[]` means the page passed) or an object with a `findings` array. Anything else, such as axe's `{violations: [...]}`, a number or nothing, is an error that names the accepted shapes. A finding is `{rule, severity: "fail" | "warn", message, selector?, rect?}`. `element` and `detail` are read as `selector` and `message`. A finding with no severity counts as `fail`; `error` and `warning` are read as `fail` and `warn`, and any other severity is an error. The built-in `scripts/audit.js` is a function body. To run both, use two audit steps.

## report.json

| Field | Content |
| --- | --- |
| `passed` | True only when every step ran and nothing but `optional` steps failed. A crash, Ctrl-C or SIGTERM leaves it false. Also the exit code. The run first replaces the file with a `passed: false` placeholder; a run that cannot start (bad plan, no device) leaves `failures[0].kind: "setup"`. |
| `failures[]` | `{step, kind, message, detail?, optional?}`. `kind` is the step's, or `crash`, `interrupted`, `restore`, `console`, `setup` or `incomplete` (the placeholder). |
| `steps[]` | `{step, kind, ok, ms, result or error}`. |
| `shots[]` | Path, size, points, luminance statistics, `problems`, and the Text Size at capture. |
| `audits[]` | The viewport, root and body font sizes, `findings`, `counts`, and the Text Size. |
| `values` | The `eval … as` results. |
| `console` | Errors since the last page load, read at the end of the run. |
| `video`, `contactSheet` | Path and details, or `{error, fix}`. The report is written before the contact sheet is built; Ctrl-C or SIGTERM while it is built leaves `passed: false`. |
| `device`, `driver`, `initial`, `restored`, `seconds` | Run metadata. `interrupted` or `crashed` says why a run ended early. |
