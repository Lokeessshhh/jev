"""Goal in, done out. Guess-first entry, search-URL fallback, Jev-driven task loop."""

import os
import re
import sys
import time
import urllib.parse

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def load_env(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

load_env()

import browser as brows
import decide
import logger
import text

GOOGLE_SEARCH = "https://www.google.com/search?q={}"
DUCK_SEARCH = "https://duckduckgo.com/?q={}"
SITE_OK_AT = 0.6

BOT_MARKERS = (
    "captcha",
    "are you a robot",
    "are you a person",
    "unusual traffic",
    "access denied",
    "request blocked",
    "forbidden",
)


def search_urls(query):
    """Direct results URLs: Google first, DuckDuckGo when Google bot-walls us."""
    quoted = urllib.parse.quote_plus(query)
    return (("Google", GOOGLE_SEARCH.format(quoted)), ("DuckDuckGo", DUCK_SEARCH.format(quoted)))


# Verbs and generic nouns that must not make a guessed URL look goal-related.
GUESS_STOPWORDS = frozenset(
    """about best book check click comment comments find first goto http https
    latest official open page pages please price release releases search show
    ticket tickets video videos website""".split()
)


COMMON_ALIASES = {
    "yt": "youtube",
    "tv": "tradingview",
    "fb": "facebook",
    "ig": "instagram",
    "wiki": "wikipedia",
    "gh": "github",
    "py": "python",
}


def guess_is_obvious(goal, url):
    """True when a distinctive goal word or alias is right there in the guessed URL.

    Skips a judge call for cases like 'ripgrep' in github.com/BurntSushi/ripgrep or 'yt' for youtube.com.
    """
    parts = urllib.parse.urlparse(url)
    hay = re.sub(r"[^a-z0-9]+", " ", (parts.netloc + parts.path).lower())
    words = {w for w in re.findall(r"[a-z0-9]+", goal.lower()) if w not in GUESS_STOPWORDS}
    for w in words:
        if len(w) >= 5 and w in hay:
            return True
        if w in COMMON_ALIASES and COMMON_ALIASES[w] in hay:
            return True
    return False


def bot_wall(b):
    """True when a bot wall or captcha page is showing instead of real content."""
    if "sorry/index" in b.url or "/sorry" in b.url:
        return True
    try:
        page_text = (b.page_text() or "").lower()
    except Exception:
        return False
    return any(marker in page_text for marker in BOT_MARKERS)


def page_signature(page):
    """Cheap digest of visible state, used to detect actions with no effect."""
    elements = page.get("elements") or []
    return (
        page.get("url") or "",
        page.get("scroll_y", 0),
        page.get("at_bottom", False),
        len(elements),
        len(page.get("text") or ""),
        tuple(
            (e.get("index"), e.get("value"), e.get("checked"), e.get("selected"), e.get("expanded"))
            for e in elements
        ),
    )


def site_score(b, goal):
    """Returns (score, note). Never raises. Bot walls fail fast without a model call."""
    log = logger.get_logger()
    try:
        page_text = b.page_text()
    except Exception as e:
        log.warn(f"site_score: unreadable page ({e})", url=b.url)
        return 0.0, f"unreadable page ({e})"
    low = page_text.lower()
    marker = next((m for m in BOT_MARKERS if m in low), None)
    if marker or len(page_text.strip()) < 50:
        try:
            usable = bool(b.snapshot()["elements"])
        except Exception:
            usable = False
        if not usable:
            if marker:
                note = f"bot wall ({marker!r}), no usable elements"
                log.warn(f"site_score bot wall detected: {marker!r}", url=b.url)
                return 0.0, note
            note = "no content rendered (empty page)"
            log.warn("site_score empty page, no usable elements", url=b.url)
            return 0.0, note
    try:
        score = decide.ask_yes_no(
            page_text, b.url, f"Is this page the right site to accomplish this goal: {goal}?"
        )
    except Exception as e:
        log.error(f"site_score judge failed: {e}", url=b.url)
        return 0.0, f"judge failed ({e})"
    if score >= SITE_OK_AT:
        log.site_check(b.url, score, "accepted")
        return score, "accepted"
    note = f"score {score:.2f} below {SITE_OK_AT}"
    log.site_check(b.url, score, note)
    return score, note


def site_ok(b, goal):
    score, _ = site_score(b, goal)
    return score >= SITE_OK_AT



def snap_or_recover(b, empties):
    """Viewport snapshot; None means no usable content. Reloads once when stuck.

    After 3 straight empty snapshots the page is reloaded once; after 5 the
    caller should give up with a clear message instead of scrolling forever.
    """
    log = logger.get_logger()
    try:
        page = b.snapshot()
    except Exception as e:
        log.warn(f"snapshot exception: {e}", url=b.url)
        page = None
    if page and (page["elements"] or len(page.get("text") or "") >= 100):
        log.page_state(
            page.get("url", b.url),
            page.get("title", ""),
            elements_count=len(page.get("elements", [])),
            sample_elements=[
                {"index": e["index"], "role": e["role"], "label": e["label"][:50]}
                for e in page.get("elements", [])[:8]
            ],
            scroll_percent=page.get("scroll_percent"),
            at_bottom=page.get("at_bottom", False),
        )
        return page, 0
    empties += 1
    log.warn(f"Page empty or non-rendering (count={empties})", url=b.url)
    if empties == 3:
        log.info("page still empty, reloading once", url=b.url)
        try:
            b.reload()
        except Exception as e:
            log.error(f"reload failed: {e}", url=b.url)
    return None, empties


def find_site(b, goal):
    """Phase 1: guessed URL. Phase 2: direct results URL (Google, then DuckDuckGo)."""
    log = logger.get_logger()
    log.info(f"Phase 1: Finding entry site for goal: {goal!r}")
    guess = ""
    try:
        guess = text.guess_site(goal)
    except Exception as e:
        log.warn(f"site guess skipped: {e}")
    if guess:
        log.info(f"trying guessed site: {guess}")
        try:
            b.goto(guess)
            if guess_is_obvious(goal, guess) and not bot_wall(b):
                log.info(f"guessed site accepted by URL match (judge skipped): {guess}")
                return True
            score, note = site_score(b, goal)
            if score >= SITE_OK_AT:
                log.info(f"guessed site accepted: {guess} (score: {score:.2f})")
                return True
            log.info(f"guessed site rejected ({note}), falling back to a search URL")
        except Exception as e:
            log.warn(f"guessed site navigation failed: {e}", url=guess)

    query = ""
    try:
        query = text.search_query(goal)
    except Exception as e:
        log.warn(f"search query generation failed, using the goal text: {e}")
    query = query or goal

    for engine, url in search_urls(query):
        log.info(f"Phase 2: {engine} search URL: {url}")
        try:
            b.goto(url)
        except Exception as e:
            log.warn(f"{engine} navigation failed: {e}", url=url)
            continue
        if bot_wall(b):
            log.warn(f"{engine} served a bot wall, trying the next engine", url=b.url)
            continue
        try:
            usable = bool(b.snapshot()["elements"])
        except Exception as e:
            log.warn(f"{engine} snapshot failed: {e}", url=b.url)
            usable = False
        if usable:
            log.info(f"{engine} results loaded, continuing on: {b.url}")
            return True
        log.warn(f"{engine} results did not render, trying the next engine", url=b.url)
    log.error("no search engine returned usable results")
    return False


def act_with_recovery(b, op, index, label, value):
    """Run an action; on a stale node reference re-snapshot, re-resolve by label, retry once.

    Returns (result, error, index): exactly one of result/error is set, and index is
    the element actually used (it moves when the label is found at a new position).
    """
    log = logger.get_logger()
    try:
        return b.act(index, op, value), None, index
    except Exception as first:
        if op not in ("CLICK", "TYPE_TEXT") or not label:
            return None, first, index
        try:
            b.snapshot()
        except Exception:
            return None, first, index
        fresh = b.find_by_label(label, exclude=index)
        if not fresh:
            return None, first, index
        log.info(f"retry after re-snapshot: [{index}] {label!r} is now [{fresh}]")
        try:
            return b.act(fresh, op, value), None, fresh
        except Exception as second:
            return None, second, fresh


def run_task(b, goal, budget):
    """Phase 3: do the goal on the site. A DONE decision ends the run at once."""
    log = logger.get_logger()
    log.info(f"Phase 3: Running goal on site: {goal!r} (budget={budget} steps)")
    history, empties, fail_key, fail_count = [], 0, None, 0
    last_move, last_sig, stuck = None, None, 0
    pending_click = None
    url_history = []
    for step in range(1, budget + 1):
        log.info(f"--- Task Step {step}/{budget} ---")
        page, empties = snap_or_recover(b, empties)
        if not page:
            if empties >= 5:
                msg = "stopped: page shows no content (bot wall?)"
                log.error(msg, url=b.url, empties=empties)
                return msg
            continue

        # Guarded mid-task bot wall check
        if bot_wall(b) and (len(page.get("elements") or []) <= 2 or "sorry" in (b.url or "").lower()):
            msg = "stopped: bot wall detected mid-task (captcha or access blocked)"
            log.error(msg, url=b.url)
            return msg

        # Ping-pong navigation detector
        curr_url = page.get("url") or b.url
        if not url_history or url_history[-1] != curr_url:
            url_history.append(curr_url)
            if (
                len(url_history) >= 4
                and url_history[-1] == url_history[-3]
                and url_history[-2] == url_history[-4]
                and url_history[-1] != url_history[-2]
            ):
                msg = f"stopped: ping-pong loop detected between {url_history[-1]} and {url_history[-2]}"
                log.error(msg)
                return msg

        sig = page_signature(page)
        curr_el_count = len(page.get("elements") or [])
        if pending_click is not None:
            old_url, old_count, old_label, old_index = pending_click
            pending_click = None
            if sig[0] == old_url and old_count >= 8 and curr_el_count * 2 <= old_count:
                history.append(
                    {
                        "action": f"clicked [{old_index}] {old_label!r} led nowhere "
                        f"(same page, controls {old_count} -> {curr_el_count}); use a different link",
                        "kind": "advice",
                        "text": "",
                    }
                )
                log.warn(f"dead-end click: [{old_index}] {old_label!r} (controls {old_count} -> {curr_el_count})")
        try:
            move = decide.choose(page, goal, history)
        except Exception as e:
            msg = f"stopped: decide failed ({e})"
            log.error(msg, url=b.url)
            return msg
        op, index = move["operation"], move["index"]
        target_el = b.get_element(index) if index else None
        label = " ".join(((target_el or {}).get("label") or "").split())

        # Mechanical guard: Never click Search/Submit/Go to if an empty searchbox is present
        if op == "CLICK" and any(k in label.lower() for k in ("search", "submit", "go to")) and "quick" not in label.lower():
            empty_boxes = [e for e in page.get("elements", []) if e.get("role") in ("searchbox", "textbox", "combobox") and not e.get("value")]
            if empty_boxes:
                target_idx = empty_boxes[0]["index"]
                log.info(f"redirecting empty search click [{index}] '{label}' -> TYPE_TEXT [{target_idx}]")
                op, index = "TYPE_TEXT", target_idx
                target_el = empty_boxes[0]
                label = " ".join(((target_el or {}).get("label") or "").split())

        # Filled-box post-type guard: don't re-type into an already filled field
        if op == "TYPE_TEXT" and (target_el or {}).get("value"):
            val = str(target_el.get("value"))
            if any(h.get("kind") == "TYPE_TEXT" for h in history[-2:]):
                log.info(f"field [{index}] '{label}' already holds {val!r}; advising suggestion pick or submission")
                history.append(
                    {
                        "action": f"field [{index}] already holds {val!r}; select the matching suggestion from the dropdown or submit instead of typing again",
                        "kind": "advice",
                        "text": "",
                    }
                )

        log.decision(op, index, element=target_el)
        if op == "DONE":
            log.info("Goal reported DONE by the decider, run finished")
            return "done"
        if op == "BACK":
            log.act_start(op, None)
            done = b.act(None, "BACK")
            history.append({"action": "went back to previous page", "kind": "BACK", "text": ""})
            log.act_success(done, page_url=b.url)
            last_move, last_sig, stuck = (op, None), None, 0
            continue
        target_node = (target_el or {}).get("node")
        move_key = (op, target_node if target_node is not None else index, sig[0])
        if op in ("CLICK", "TYPE_TEXT", "SCROLL", "WAIT", "BACK") and (move_key == last_move or (op, index) == last_move):
            if sig == last_sig:
                stuck += 1
                max_stuck = 2 if op in ("WAIT", "SCROLL") else 3
                log.warn(f"repeat guard: {op} [{index}] again with no page change ({stuck}/{max_stuck})")
                if stuck >= 1 and op == "CLICK" and any(k in label.lower() for k in ("search", "menu", "quick")):
                    history.append(
                        {
                            "action": f"{op} [{index}] {label!r} was already clicked; wait for it to open or type into the searchbox instead of clicking it again",
                            "kind": "advice",
                            "text": "",
                        }
                    )
                elif stuck == 2 and op not in ("WAIT", "SCROLL"):
                    history.append(
                        {
                            "action": f"{op} [{index}] {label!r} had no visible effect, use a different element",
                            "kind": "advice",
                            "text": "",
                        }
                    )
                elif stuck == 1 and op == "SCROLL" and page.get("at_bottom"):
                    history.append(
                        {
                            "action": "scrolling down had no effect because the bottom of the page is reached; use on-screen links, sidebar, or search",
                            "kind": "advice",
                            "text": "",
                        }
                    )
                if stuck >= max_stuck:
                    msg = f"stopped: {op} [{index}] repeated with no page change"
                    log.error(msg, element=target_el, url=b.url)
                    return msg
            else:
                stuck = 0
        else:
            stuck = 0
        last_move, last_sig = move_key, sig
        value = None
        if op == "TYPE_TEXT":
            try:
                value = text.field_text(goal, label, b.title)
                log.info(f"Text generated for [{index}] ({label!r}): {value!r}")
            except Exception as e:
                log.warn(f"text generation failed for [{index}], skipping: {e}")
                history.append({"action": f"skipped [{index}]", "kind": "skip", "text": ""})
                continue
        log.act_start(op, index, element=target_el, text=value)
        done, err, index = act_with_recovery(b, op, index, label, value)
        if err:
            fail_count = fail_count + 1 if (op, index) == fail_key else 1
            fail_key = (op, index)
            target_el = b.get_element(index)
            log.act_fail(err, op, index, element=target_el, fail_count=fail_count)
            if fail_count >= 3:
                msg = f"stopped: [{index}] stays unusable (covered, gone, or disabled)"
                log.error(msg, element=target_el, url=b.url)
                return msg
            history.append({"action": f"failed to {op.lower()} [{index}] {label!r}", "kind": "fail", "text": ""})
            time.sleep(0.6)
            continue
        fail_key, fail_count = None, 0
        if op == "CLICK":
            pending_click = (sig[0], curr_el_count, label, index)
        else:
            pending_click = None
        history.append({"action": f"{done} {label!r}", "kind": op, "text": value or ""})
        log.act_success(done, page_url=b.url)
    msg = "stopped: step budget reached"
    log.warn(msg, budget=budget)
    return msg


def main():
    load_env()
    if len(sys.argv) < 2:
        print('usage: python loop.py "your goal"')
        raise SystemExit(2)
    goal = sys.argv[1]
    log = logger.get_logger(goal)
    task_budget = int(os.environ.get("MAX_TASK_STEPS", "20"))
    b = brows.Browser()
    started = time.perf_counter()
    try:
        if not find_site(b, goal):
            log.error("could not reach a usable site")
            return
        result = run_task(b, goal, task_budget)
        elapsed = round(time.perf_counter() - started, 1)
        log.info(f"Task finished with result: {result} in {elapsed}s")
    except Exception as e:
        log.error(f"Unhandled fatal error in run: {e}", exc=str(e))
    finally:
        b.close()
        log.close()


if __name__ == "__main__":
    main()

