---
name: pr-media
description: Upload screenshots and videos to a GitHub pull request through the T3 browser, so GitHub hosts them as its own attachments. Use whenever a PR description or PR comment needs images or video.
---

# PR media

PR media lives only in GitHub's attachment store (`https://github.com/user-attachments/assets/...`), uploaded through the T3 browser's GitHub sign-in. A branch, a commit, a `blob/...?raw=true` link or another host is never the route, even when an earlier PR did it that way.

## 1. Check the route

- **No `preview_*` tools**: in T3 Code, hand this skill to a Claude child (Codex children have no browser). Outside T3 Code, file the PR without media, list each file's absolute local path in your final report so the user can attach them, and stop.
- **T3 browser**: open the PR's conversation page in a background tab (`preview_open` with `open: false`, `reuseExistingTab: false`). The PR must exist, so file it first. Read `meta[name="user-login"]` with `preview_evaluate`. When it is empty, the profile is signed out: show the tab on `https://github.com/login` (`preview_open` with `open: true`), then ask the user with the harness's question tool to sign in there. The user types the credentials; continue once they answer.

Capture video with `preview_recording_start` and `preview_recording_stop` on the tab under test; the stop result gives the MP4 path.

## 2. Upload

GitHub's editor opens files with `window.showOpenFilePicker`, which the preview's file-chooser handling does not catch, so a click on "Paste, drop, or click to add files" shows nothing. Turn off that picker and the editor falls back to an `<input type=file>`, which `preview_act` can fill. On the freshly loaded PR page, before opening any other editor, run with `preview_evaluate`:

```js
(() => {
  Object.defineProperty(window, 'showOpenFilePicker', { value: undefined, configurable: true });
  const click = HTMLInputElement.prototype.click;
  HTMLInputElement.prototype.click = function () {
    if (this.type !== 'file') return click.call(this);
    this.id = 't3-upload-input';
    this.hidden = true;
    if (!this.isConnected) document.body.append(this);
  };
  [...document.querySelectorAll('button')].find(b => /add files/i.test(b.textContent)).click();
  return Boolean(document.getElementById('t3-upload-input'));
})()
```

It returns `true` when the input is ready. Then call `preview_act` with `action: "upload"`, `selector: "#t3-upload-input"` and every file's absolute path in `paths`. PNG and MP4 are verified; GitHub's input accepts SVG, GIF, JPEG, WebP, MOV and WebM as well.

GitHub uploads each file into the comment box's textarea. Poll it with `preview_evaluate`, with `N` set to the number of files:

```js
new Promise(done => {
  const ta = document.querySelector('textarea[aria-label="Markdown value"]');
  const start = Date.now();
  (function tick() {
    const urls = ta.value.match(/https:\/\/github\.com\/user-attachments\/assets\/[0-9a-f-]+/g) || [];
    if (urls.length >= N || Date.now() - start > 60000) done({ urls, markdown: ta.value });
    else setTimeout(tick, 500);
  })();
})
```

The poll is done when `urls` holds one URL per file. Fewer after a minute means GitHub refused a file; take a snapshot of the tab to read why. An image comes back as `<img width height alt src>`, a video as a bare URL.

## 3. Put the media in the PR

Copy `markdown` out, then empty the comment box so nothing is posted and no draft remains:

```js
(() => {
  const ta = document.querySelector('textarea[aria-label="Markdown value"]');
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(ta, '');
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  return ta.value === '';
})()
```

Place each `<img>` tag, and each video URL on a line of its own (GitHub renders it as a player), in the PR body, and apply it with `gh pr edit <number> --body-file <file>`. For a comment instead, write it with the `leaving-pr-comments` skill.

The work is done when, after `preview_navigate` reloads the PR page (which also undoes the overrides), every body image has `naturalWidth > 0` and every video reaches `readyState` 4. Close the tab you opened.
