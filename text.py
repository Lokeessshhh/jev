"""Groq text writer: site guesses, search queries, field values. JSON only."""

import json
import os
import uuid

import httpx

def _load_env_if_needed():
    if "TEXT_MODEL_API_KEY" in os.environ:
        return
    for p in (".env", "../.env"):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
            break

_load_env_if_needed()

BASE_URL = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.groq.com/openai/v1")
MODEL = os.environ.get("TEXT_MODEL", "openai/gpt-oss-20b")
CLIENT = httpx.Client(http2=True, timeout=30)
# OpenCode Go requires an identifying UA (not a generic HTTP-library name)
# plus a stable per-conversation session id. Other backends get clean headers.
IS_OPENCODE = "opencode.ai" in BASE_URL
USER_AGENT = "jev-agent/0.1"
SESSION_ID = os.environ.get("OPENCODE_SESSION") or uuid.uuid4().hex


def _headers():
    headers = {"Authorization": "Bearer " + _key()}
    if IS_OPENCODE:
        headers.update({"User-Agent": USER_AGENT, "x-opencode-session": SESSION_ID})
    return headers


def _key():
    key = os.environ.get("TEXT_MODEL_API_KEY", "")
    if not key:
        raise ValueError("TEXT_MODEL_API_KEY is missing. Put it in .env.")
    return key


def _ask(system, user):
    """One JSON answer from the text model, retried once when the reply is unusable.

    The shared endpoint occasionally answers with empty or non-JSON content; a single
    retry keeps guesses and queries from silently degrading to the raw goal text.
    """
    last = None
    for strict in (True, False):
        try:
            return _ask_json(system, user, strict)
        except (ValueError, KeyError, IndexError) as e:
            last = e
    raise RuntimeError(f"text model returned unusable JSON: {last}")


def _ask_json(system, user, strict=True):
    payload = {
        "model": MODEL,
        "max_tokens": 256,
        "messages": [
                # Groq requires the word "JSON" in messages for json_object mode.
                {"role": "system", "content": system + " Reply in JSON."},
                {"role": "user", "content": json.dumps(user)},
            ],
    }
    if strict:
        payload["response_format"] = {"type": "json_object"}
    post = lambda body: CLIENT.post(
        BASE_URL.rstrip("/") + "/chat/completions", headers=_headers(), json=body
    )
    response = post(payload)
    if response.status_code == 400 and "json_validate" in response.text and strict:
        # Strict JSON mode rejected a trivial payload; retry plain and parse.
        payload.pop("response_format", None)
        response = post(payload)
    if response.is_error:
        raise RuntimeError(f"Text model returned HTTP {response.status_code}: {response.text[:150]}")
    content = response.json()["choices"][0]["message"]["content"] or ""
    try:
        return json.loads(content)
    except ValueError:
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"no JSON in text model reply: {content[:80]!r}")
        return json.loads(content[start : end + 1])


def guess_site(goal):
    """Direct site URL for the goal, or empty string when unsure."""
    out = _ask(
        'Guess the single best website URL to start this task. Reply {"url": "..."} or {"url": ""} when unsure.',
        {"goal": goal},
    )
    url = str(out.get("url", "")).strip()
    return url if url.startswith("http") else ""


def search_query(goal):
    """Google query that finds where to do the goal."""
    out = _ask('Write a short Google search query for this task. Reply {"query": "..."}.', {"goal": goal})
    return str(out.get("query", goal)).strip() or goal


def field_text(goal, field_label, page_title):
    """Value to type into a field. Never empty."""
    out = _ask(
        'Write the exact short text to type into this web form field. '
        'Use plain search terms or standard symbols (e.g. SBIN, NIFTY). '
        'Never append API exchange suffixes like .NS or .BO unless the goal explicitly mentions them. '
        'Reply {"text": "..."}.',
        {"goal": goal, "field": field_label, "page": page_title},
    )
    text = str(out.get("text", "")).strip()
    if not text:
        raise ValueError("Text model returned no value; nothing typed.")
    # Programmatic safeguard: strip market API suffixes (.NS, .BO) if not in original goal
    goal_lower = goal.lower()
    for suffix in (".ns", ".bo"):
        if text.lower().endswith(suffix) and suffix not in goal_lower:
            text = text[: -len(suffix)].strip()
    return text
