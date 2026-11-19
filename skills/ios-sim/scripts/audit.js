// iosim's built-in page audit. Evaluated as a function body in the page; `__opts` is defined before it.
// Returns {viewport, findings: [{rule, severity: "fail" | "warn", message, selector, rect?}], counts}.
const opts = Object.assign({minTarget: 44, minInputFont: 16, minTextFont: 11, max: 25}, typeof __opts === "object" ? __opts : {});
const findings = [];
const vw = document.documentElement.clientWidth || window.innerWidth;

function describe(el) {
  if (!(el instanceof Element)) return String(el);
  if (el.id) return el.tagName.toLowerCase() + "#" + CSS.escape(el.id);
  const parts = [];
  let node = el;
  while (node && node.nodeType === 1 && parts.length < 4) {
    let part = node.tagName.toLowerCase();
    if (node.id) { parts.unshift(part + "#" + CSS.escape(node.id)); break; }
    const cls = [...node.classList].slice(0, 2).map((c) => "." + CSS.escape(c)).join("");
    part += cls;
    const parent = node.parentElement;
    if (parent) {
      const same = [...parent.children].filter((c) => c.tagName === node.tagName);
      if (same.length > 1) part += ":nth-of-type(" + (same.indexOf(node) + 1) + ")";
    }
    parts.unshift(part);
    node = parent;
  }
  return parts.join(" > ");
}
function box(r) { return {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)}; }
function visible(el) {
  const s = getComputedStyle(el);
  if (s.display === "none" || s.visibility === "hidden" || Number(s.opacity) === 0) return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}
// The visually hidden pattern (a skip link, screen-reader text): a 1 px box, clipped away, or parked off screen.
function visuallyHidden(el) {
  const r = el.getBoundingClientRect();
  if (r.width <= 1 || r.height <= 1) return true;
  if (r.right <= 0 || r.bottom <= 0 || r.left >= vw + 1000) return true;
  const s = getComputedStyle(el);
  return /rect\(0(px)?,? 0(px)?,? 0(px)?,? 0(px)?\)/.test(s.clip) || /inset\(50%\)/.test(s.clipPath);
}
// Inside a container that scrolls or clips sideways, and that container itself fits: a carousel, not a page overflow.
function inSidewaysScroller(el) {
  for (let n = el.parentElement; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
    const ox = getComputedStyle(n).overflowX;
    if (ox !== "visible" && n.getBoundingClientRect().right <= vw + 1) return true;
  }
  return false;
}
function add(rule, severity, message, el, extra) {
  findings.push(Object.assign({rule, severity, message, selector: el ? describe(el) : null}, el ? {rect: box(el.getBoundingClientRect())} : {}, extra || {}));
}

// 1. Sideways scroll: the page is wider than the screen.
const scrollWidth = Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0);
if (scrollWidth > vw + 1) {
  add("sideways-scroll", "fail", `page is ${scrollWidth}px wide on a ${vw}px viewport`, null, {scrollWidth, viewportWidth: vw});
  const wide = [];
  for (const el of document.body ? document.body.querySelectorAll("*") : []) {
    const r = el.getBoundingClientRect();
    if (r.right > vw + 1 && r.width > 0 && visible(el) && !inSidewaysScroller(el)) {
      // Report the outermost offenders only.
      if (!wide.some((w) => w.contains(el))) wide.push(el);
    }
  }
  for (const el of wide.slice(0, opts.max)) {
    const r = el.getBoundingClientRect();
    add("too-wide", "fail", `extends to ${Math.round(r.right + scrollX)}px, past the ${vw}px viewport`, el);
  }
}

// 2. Tap targets smaller than minTarget x minTarget CSS px (CSS px equal points at zoom 1). Visually hidden elements are skipped. Links inside running text are warnings (WCAG's inline exception).
const interactive = "a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=link], [role=checkbox], [role=switch], [role=tab], [role=menuitem], [onclick], [tabindex]:not([tabindex='-1'])";
let small = 0;
for (const el of document.querySelectorAll(interactive)) {
  if (!visible(el) || el.disabled || visuallyHidden(el)) continue;
  const r = el.getBoundingClientRect();
  if (r.width >= opts.minTarget && r.height >= opts.minTarget) continue;
  // A label wrapping a small checkbox counts as the target.
  const label = el.closest("label");
  if (label) {
    const lr = label.getBoundingClientRect();
    if (lr.width >= opts.minTarget && lr.height >= opts.minTarget) continue;
  }
  const inline = el.tagName === "A" && getComputedStyle(el).display === "inline" && el.parentElement && /\S/.test((el.parentElement.textContent || "").replace(el.textContent || "", ""));
  small += 1;
  if (small > opts.max) continue;
  add(inline ? "small-inline-link" : "small-tap-target", inline ? "warn" : "fail",
    `${Math.round(r.width)}x${Math.round(r.height)} px, under ${opts.minTarget}x${opts.minTarget}`, el);
}

// 3. Text inputs under minInputFont px: iOS Safari zooms the page when they get focus. Readonly and disabled ones cannot get typing focus.
const zoomTypes = new Set(["", "text", "search", "email", "url", "tel", "password", "number", "date", "datetime-local", "month", "time", "week"]);
for (const el of document.querySelectorAll("input, textarea, select, [contenteditable=''], [contenteditable=true]")) {
  if (!visible(el) || el.disabled || el.readOnly) continue;
  if (el.tagName === "INPUT" && !zoomTypes.has((el.getAttribute("type") || "").toLowerCase())) continue;
  const size = parseFloat(getComputedStyle(el).fontSize);
  if (size < opts.minInputFont) add("input-zoom", "fail", `font-size ${size}px; iOS zooms on focus under ${opts.minInputFont}px`, el, {fontSize: size});
}

// 4. Viewport meta: missing width=device-width, or zoom disabled.
const meta = document.querySelector("meta[name=viewport]");
const content = meta ? (meta.getAttribute("content") || "").toLowerCase().replace(/\s/g, "") : "";
if (!meta || !content.includes("width=device-width")) add("viewport-meta", "fail", meta ? `viewport meta is "${content}" (no width=device-width)` : "no <meta name=viewport>", null);
if (/user-scalable=(no|0)/.test(content) || /maximum-scale=(1(\.0*)?|0?\.\d+)(,|$)/.test(content)) add("zoom-disabled", "warn", `viewport meta "${content}" blocks pinch zoom`, null);

// 5. Tiny text.
let tiny = 0;
const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
while (walker.nextNode() && tiny < opts.max) {
  const node = walker.currentNode;
  if (!/\S/.test(node.textContent)) continue;
  const el = node.parentElement;
  if (!el || !visible(el)) continue;
  const size = parseFloat(getComputedStyle(el).fontSize);
  if (size < opts.minTextFont) { tiny += 1; add("tiny-text", "warn", `text at ${size}px`, el, {fontSize: size}); }
}

// 6. Controls with no accessible name, and images without alt.
for (const el of document.querySelectorAll("button, a[href], [role=button]")) {
  if (!visible(el)) continue;
  const name = (el.getAttribute("aria-label") || el.getAttribute("title") || el.textContent || "").trim() || (el.querySelector("img[alt]:not([alt=''])") ? "img" : "") || (el.getAttribute("aria-labelledby") ? "ref" : "");
  if (!name) add("no-accessible-name", "warn", "control has no text or aria-label", el);
}
for (const el of document.querySelectorAll("img:not([alt])")) {
  if (visible(el)) add("img-no-alt", "warn", "image has no alt attribute", el);
}

const counts = {fail: findings.filter((f) => f.severity === "fail").length, warn: findings.filter((f) => f.severity === "warn").length};
return {
  url: location.href,
  viewport: {width: vw, height: window.innerHeight, dpr: window.devicePixelRatio, scale: window.visualViewport ? visualViewport.scale : 1},
  rootFontSize: getComputedStyle(document.documentElement).fontSize,
  bodyFontSize: document.body ? getComputedStyle(document.body).fontSize : null,
  findings,
  counts,
};
