"""Headful Chromium owned by this project. Viewport snapshot -> numbered elements -> act.

Snapshot and execution adapted from jev-ultrafast (browser-use/jev-ultrafast):
only viewport-visible elements and text are reported, elements keep stable node
ids in window.__jevFast, and geometry is re-resolved with an occlusion check
right before every input. Below-fold content appears only after SCROLL."""

import os
import time

from playwright.sync_api import sync_playwright

PAGE_TEXT_LIMIT = 6000
# A JS app can render with almost no text (YouTube's home settles at ~154 chars),
# so readiness also counts on-screen controls instead of demanding text alone.
READY_TEXT_MIN = 120
READY_CONTROLS_MIN = 1
READY_POLL_MS = 100
READY_TIMEOUT_MS = 8000
# Wheel scrolling on these players swipes to the next item instead of scrolling.
FEED_MARKERS = ("/shorts/", "tiktok.com", "instagram.com/reel", "/reel/")

READY_JS = r"""(() => {
  if (!document.body) return {text: 0, controls: 0};
  const box = 'a[href],button,input,textarea,select,[role="button"],[role="link"],' +
    '[role="textbox"],[role="searchbox"],[role="combobox"]';
  let controls = 0;
  for (const e of document.querySelectorAll(box)) {
    const r = e.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && r.top >= 0 && r.bottom <= innerHeight &&
        r.left >= 0 && r.right <= innerWidth) controls++;
  }
  return {text: document.body.innerText.length, controls};
})()"""


def labels_overlap(wanted, have, min_length=6, min_ratio=0.5):
    """True when one label contains the other and they are of comparable size.

    Guards against short labels ('All') matching a long, unrelated one.
    """
    if not wanted or not have:
        return False
    if wanted in have or have in wanted:
        shorter, longer = sorted((len(wanted), len(have)))
        return shorter >= min_length and shorter >= min_ratio * longer
    return False


def page_ready(page):
    """True when a page shows real text and at least one on-screen control.

    Text is the real signal: a partially mounted SPA has no text yet, and acting on
    it means clicking a half-rendered page (the YouTube action bar mounts ~1.7s in).
    """
    try:
        stats = page.evaluate(READY_JS) or {}
    except Exception:
        return False
    text = stats.get("text") or 0
    controls = stats.get("controls") or 0
    return text > READY_TEXT_MIN and controls >= READY_CONTROLS_MIN


SNAPSHOT_JS = r"""(() => {
  if (!document.body) return null;
  try {
    document.querySelectorAll('.vector-toc-list-item, .vector-toc-list').forEach(e => {
      e.style.display = 'list-item';
    });
  } catch (_) {}
  const cache = window.__jevFast = window.__jevFast || {ids: new WeakMap(), nodes: new Map(), next: 1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e, cache.next++);
    const id = cache.ids.get(e);
    cache.nodes.set(id, e);
    return id;
  };
  for (const [id, e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const safe = e => !['password','file','hidden'].includes(e.type);
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true, checkVisibilityCSS:true});
  const name = (e, seen) => {
    seen = seen || new Set();
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced = (e.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(id => name(document.getElementById(id), seen)).filter(Boolean).join(' ');
    const raw = referenced || e.getAttribute('aria-label') ||
      [...(e.labels || [])].map(l => name(l, seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName === 'INPUT' ? '' : [...e.childNodes].map(n => n.nodeType === 3 ? n.textContent :
        n.nodeType === 1 && n.getAttribute('aria-hidden') !== 'true' ? name(n, seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
    return raw.replace(/\s+/g, ' ').trim();
  };
  const roles = ['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector = 'a[href],button,input,textarea,select,summary,[contenteditable="true"],' +
    roles.map(r => '[role="' + r + '"]').join(',');
  const role = e => {
    const explicit = e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName === 'BUTTON' || e.tagName === 'SUMMARY') return 'button';
    if (e.tagName === 'A') return 'link';
    if (e.tagName === 'SELECT') return 'combobox';
    if (e.tagName === 'TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName === 'INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type === 'search') return 'searchbox';
      if (e.type === 'number') return 'spinbutton';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  const actions = [];
  for (const e of document.querySelectorAll(selector)) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const r = e.getBoundingClientRect();
    const x = r.x + r.width / 2, y = r.y + r.height / 2, rname = role(e);
    if (!rname || r.width <= 0 || r.height <= 0 || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
    if (rname === 'gridcell' && e.querySelector('button,[role="button"]')) continue;
    const base = {node: identity(e), role: rname, label: (name(e) || rname).slice(0, 120)};
    for (const key of ['checked','selected','expanded']) {
      const value = e.getAttribute('aria-' + key);
      if (value !== null) base[key] = value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked = String(e.checked);
    if (e.tagName === 'SELECT') {
      // Native dropdown options are OS-rendered, so closed <select> options are
      // enumerated here and applied programmatically instead of clicked.
      base.value = [...e.selectedOptions].map(o => o.label).join(', ').slice(0, 80);
      for (const o of e.options) {
        if (o.selected || o.disabled || o.closest('optgroup[disabled]')) continue;
        actions.push(Object.assign({}, base, {select: o.value, label: (base.label + ' → ' + o.label).slice(0, 120)}));
      }
      continue;
    }
    const editable = !e.readOnly && e.getAttribute('aria-readonly') !== 'true' &&
      (['textbox','searchbox','spinbutton'].includes(rname) ||
        (rname === 'combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
    if (editable) {
      base.editable = true;
      const value = 'value' in e ? String(e.value) : e.innerText.trim();
      if (value) base.value = value.slice(0, 80);
    }
    actions.push(base);
  }
  const words = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  let node, length = 0;
  while ((node = walker.nextNode()) && length < 6000) {
    const value = node.textContent.trim(), parent = node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node);
    const r = range.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth) {
      words.push(value);
      length += value.length;
    }
  }
  const elements = actions.slice(0, 250).map((a, i) => Object.assign({}, a, {index: String(i + 1)}));
  const doc = document.documentElement || document.body || {};
  const scrollY = window.scrollY || window.pageYOffset || 0;
  const scrollHeight = Math.max(doc.scrollHeight || 0, document.body ? document.body.scrollHeight : 0);
  const clientHeight = window.innerHeight || doc.clientHeight || 1;
  const isScrollable = scrollHeight > clientHeight + 80;
  const atBottom = isScrollable && (scrollY + clientHeight >= scrollHeight - 30);
  const scrollPercent = isScrollable ? Math.round((scrollY / (scrollHeight - clientHeight)) * 100) : 0;
  return {
    url: location.href,
    title: document.title,
    text: words.join('\n').slice(0, 6000),
    elements,
    scrollY: Math.round(scrollY),
    scrollHeight: Math.round(scrollHeight),
    atBottom,
    scrollPercent
  };
})()"""

CLICK_TARGET_JS = r"""([node, filling]) => {
  const e = window.__jevFast ? window.__jevFast.nodes.get(node) : null;
  if (!e || !e.isConnected || e.matches(':disabled') ||
      e.closest('[aria-disabled="true"],[inert]') ||
      !e.checkVisibility({checkOpacity:true, checkVisibilityCSS:true})) return null;
  if (filling && (e.readOnly || e.getAttribute('aria-readonly') === 'true')) return null;
  const r = e.getBoundingClientRect();
  const x = r.x + r.width / 2, y = r.y + r.height / 2;
  if (!r.width || !r.height || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return null;
  const stack = (document.elementsFromPoint ? document.elementsFromPoint(x, y) :
    [document.elementFromPoint(x, y)]) || [];
  // An ancestor, descendant, or bound label over the target is the control's own
  // chrome (e.g. a styled wrapper div covering its input). A full-viewport canvas
  // (trading charts) or another huge non-interactive layer is background, not a
  // cover; only a small unrelated subtree at the click point is a real blocker.
  const related = el => el && (el === e || e.contains(el) || el.contains(e) ||
    (el.tagName === 'LABEL' && el.control === e));
  const interactive = el => el && el.closest &&
    el.closest('a[href],button,input,textarea,select,[role="button"],[role="link"],[role="option"]') != null;
  const area = r.width * r.height;
  for (const el of stack) {
    if (related(el)) return {x, y};
    const tag = (el.tagName || '').toUpperCase();
    if (tag === 'CANVAS' || tag === 'HTML' || tag === 'BODY') continue;
    if (!interactive(el)) {
      const b = el.getBoundingClientRect();
      if (area > 0 && b.width * b.height >= 3 * area) continue;
    }
    return null;
  }
  return null;
}"""

SELECT_OPTION_JS = r"""([node, value]) => {
  const s = window.__jevFast ? window.__jevFast.nodes.get(node) : null;
  if (!s || s.tagName !== 'SELECT' || ![...s.options].some(o => o.value === value && !o.disabled)) return false;
  s.value = value;
  s.dispatchEvent(new Event('input', {bubbles: true}));
  s.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
}"""

SETTLE_JS = r"""([node, cap]) => new Promise(resolve => {
  const field = window.__jevFast ? window.__jevFast.nodes.get(node) : null;
  const boxes = field
    ? (field.getAttribute('aria-controls') || field.getAttribute('aria-owns') || '')
        .split(/\s+/).filter(Boolean).map(id => document.getElementById(id)).filter(Boolean)
    : [];
  const roots = boxes.length ? boxes : [document];
  const count = () => {
    let n = 0;
    for (const root of roots) {
      for (const e of root.querySelectorAll('[role="option"],[role="listbox"],button,a,li[tabindex],input,textarea')) {
        const r = e.getBoundingClientRect();
        if (r.width && r.height && r.bottom > 0 && r.top < innerHeight &&
            e.checkVisibility({checkOpacity:true, checkVisibilityCSS:true})) n++;
      }
    }
    return n;
  };
  const started = performance.now();
  let last = -1, stable = 0, stopped = false;
  const finish = () => { if (!stopped) { stopped = true; resolve(); } };
  setTimeout(finish, cap);
  const check = () => {
    if (stopped) return;
    const n = count();
    stable = n === last ? stable + 1 : 0;
    last = n;
    if (performance.now() - started > 150 && stable >= 4) finish();
    else requestAnimationFrame(check);
  };
  requestAnimationFrame(check);
})"""


class Browser:
    def __init__(self):
        headful = os.environ.get("HEADFUL", "1") != "0"
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=not headful)
        self._page = self._browser.new_page(viewport={"width": 1280, "height": 800})
        self._elements = {}

    # -- navigation ------------------------------------------------------
    @property
    def is_feed(self):
        """Short-video / reel players, where a wheel event loads the next item."""
        url = self._page.url.lower()
        return any(marker in url for marker in FEED_MARKERS)

    def _wait_for_content(self, timeout_ms=READY_TIMEOUT_MS):
        """JS apps render after domcontentloaded. Wait until text or controls exist."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if page_ready(self._page):
                return True
            self._page.wait_for_timeout(READY_POLL_MS)
        return False

    def goto(self, url):
        self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self._wait_for_content()

    def reload(self):
        self._page.reload(wait_until="domcontentloaded", timeout=30000)
        self._wait_for_content()

    @property
    def url(self):
        return self._page.url

    @property
    def title(self):
        return self._page.title()

    def page_text(self):
        return self._page.evaluate(
            f"document.body ? document.body.innerText.slice(0, {PAGE_TEXT_LIMIT}) : ''"
        )

    # -- observation -----------------------------------------------------
    def snapshot(self, retries=3):
        """Viewport-only elements with state + visible text, keyed by stable node ids."""
        page = None
        for _ in range(max(1, retries)):
            try:
                page = self._page.evaluate(SNAPSHOT_JS)
            except Exception:
                page = None
            if page and (page.get("elements") or len(page.get("text") or "") > READY_TEXT_MIN):
                break
            self._page.wait_for_timeout(250)
        page = page or {}
        elements = []
        for a in page.get("elements") or []:
            el = {
                "index": a["index"],
                "node": a["node"],
                "role": a["role"],
                "label": a["label"],
                "operations": ["TYPE_TEXT"] if a.get("editable") else ["CLICK"],
            }
            if a.get("select") is not None:
                el["select"] = a["select"]
            for key in ("value", "checked", "selected", "expanded"):
                if key in a:
                    el[key] = a[key]
            elements.append(el)
        self._elements = {el["index"]: el for el in elements}
        return {
            "url": page.get("url") or self.url,
            "title": page.get("title") or "",
            "text": page.get("text") or "",
            "elements": elements,
            "scroll_y": page.get("scrollY", 0),
            "scroll_height": page.get("scrollHeight", 0),
            "at_bottom": page.get("atBottom", False),
            "scroll_percent": page.get("scrollPercent", 0),
        }

    def get_element(self, index):
        return self._elements.get(index)

    def find_by_label(self, label, exclude=None):
        """Index of the element whose label matches after a fresh snapshot, else None."""
        wanted = " ".join((label or "").lower().split())
        if not wanted:
            return None
        partial = None
        for index in sorted(self._elements, key=lambda i: int(i)):
            if index == exclude:
                continue
            have = " ".join((self._elements[index].get("label") or "").lower().split())
            if have == wanted:
                return index
            if partial is None and labels_overlap(wanted, have):
                partial = index
        return partial


    # -- action ----------------------------------------------------------
    def go_back(self):
        try:
            self._page.go_back(wait_until="domcontentloaded", timeout=4000)
            self._page.wait_for_timeout(600)
            return "went back to previous page"
        except Exception as e:
            return f"could not go back ({e})"

    def act(self, index, kind, text=None):
        if kind == "BACK":
            return self.go_back()
        if kind == "WAIT":
            self._page.wait_for_timeout(500)
            return "waited"
        if kind == "SCROLL":
            if self.is_feed:
                self._page.wait_for_timeout(200)
                return "no scroll available (short-video feed): scrolling opens the next video, use the on-screen buttons"
            old_y = self._page.evaluate("window.scrollY || window.pageYOffset || 0")
            self._page.mouse.move(640, 400)
            self._page.mouse.wheel(0, 600)
            self._page.wait_for_timeout(100)
            new_y = self._page.evaluate("window.scrollY || window.pageYOffset || 0")
            if new_y == old_y:
                self._page.evaluate("window.scrollBy({top: 600, behavior: 'instant'})")
                self._page.wait_for_timeout(100)
            return "scrolled"
        el = self._elements[index]
        if "select" in el:
            if not self._page.evaluate(SELECT_OPTION_JS, [el["node"], el["select"]]):
                raise RuntimeError(f"option for [{index}] is gone")
            self._page.wait_for_timeout(250)
            return f"selected option in [{index}]"
        old_url = self._page.url
        target = self._page.evaluate(CLICK_TARGET_JS, [el["node"], kind == "TYPE_TEXT"])
        if not target:
            raise RuntimeError(f"element [{index}] is gone, covered, or disabled")
        self._page.mouse.click(target["x"], target["y"])
        if kind == "TYPE_TEXT":
            self._page.keyboard.press("Control+a")
            self._page.keyboard.insert_text(text or "")
            role = el.get("role", "")
            lbl = (el.get("label") or "").lower()
            if role in ("searchbox", "combobox") or "search" in lbl or "symbol" in lbl:
                self._settle_after_typing(el)
                self._page.wait_for_timeout(200)
                self._page.keyboard.press("Enter")
                try:
                    self._page.wait_for_load_state("domcontentloaded", timeout=600)
                except Exception:
                    self._page.wait_for_timeout(350)
            else:
                self._settle_after_typing(el)
            return f"typed into [{index}]"
        lbl = (el.get("label") or "").lower()
        role = el.get("role", "")
        if any(k in lbl for k in ("search", "find", "quick search", "symbol")) or role == "combobox" or el.get("expanded") == "false":
            self._settle_after_typing(el)
            self._page.wait_for_timeout(200)
        else:
            self._page.wait_for_timeout(200)
        if self._page.url != old_url or role == "link":
            try:
                self._page.wait_for_load_state("domcontentloaded", timeout=1500)
            except Exception:
                pass
            try:
                el_count = self._page.evaluate("document.querySelectorAll('button, a, input, [role=\"button\"]').length")
                if el_count < 15:
                    self._page.wait_for_timeout(600)
            except Exception:
                pass
        return f"clicked [{index}]"

    def _settle_after_typing(self, el):
        """Let suggestions or modal contents render before observing: wait until
        the visible clickable count stabilizes. Comboboxes and search fields get a longer cap."""
        role = (el or {}).get("role", "")
        lbl = ((el or {}).get("label") or "").lower()
        is_search = role in ("combobox", "searchbox") or any(k in lbl for k in ("search", "find", "symbol"))
        cap = 1500 if is_search else 700
        try:
            self._page.evaluate(SETTLE_JS, [(el or {}).get("node"), cap])
        except Exception:
            pass

    def close(self):
        self._browser.close()
        self._pw.stop()
