"""Logger and run tracing for Browser Automation agent."""

import json
import os
import sys
import time
from datetime import datetime

LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


class AgentLogger:
    def __init__(self, goal="run"):
        os.makedirs(LOGS_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_goal = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in goal)[:30].strip("_")
        name = f"{timestamp}_{safe_goal}" if safe_goal else timestamp
        self.log_path = os.path.join(LOGS_DIR, f"{name}.log")
        self.json_path = os.path.join(LOGS_DIR, f"{name}.json")
        self._file = open(self.log_path, "a", encoding="utf-8")
        self.events = []
        self._log("INIT", f"Run started: goal={goal!r}")

    def _log(self, level, message, **data):
        now_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        prefix = f"[{now_str}] [{level:<6}]"
        line = f"{prefix} {message}"
        try:
            print(line, flush=True)
        except Exception:
            try:
                print(line.encode("ascii", errors="replace").decode("ascii"), flush=True)
            except Exception:
                pass
        try:
            self._file.write(line + "\n")
            if data:
                self._file.write("       DATA: " + json.dumps(data, default=str) + "\n")
            self._file.flush()
        except Exception:
            pass
        self.events.append({
            "timestamp": now_str,
            "level": level,
            "message": message,
            "data": data,
        })
        self._flush_json()

    def _flush_json(self):
        try:
            with open(self.json_path, "w", encoding="utf-8") as f:
                json.dump(self.events, f, indent=2, default=str)
        except Exception:
            pass

    def info(self, msg, **data):
        self._log("INFO", msg, **data)

    def warn(self, msg, **data):
        self._log("WARN", msg, **data)

    def error(self, msg, **data):
        self._log("ERROR", msg, **data)

    def decision(self, operation, index, element=None, probabilities=None, confidence=None):
        el_desc = f" [{index}]" if index is not None else ""
        if element:
            el_desc += f" ({element.get('role', '')} {element.get('label', '')!r})"
        msg = f"Decision: {operation}{el_desc}"
        if confidence is not None:
            msg += f" (confidence: {confidence:.2f})"
        self._log("DECIDE", msg, operation=operation, index=index, element=element, probabilities=probabilities, confidence=confidence)

    def act_start(self, operation, index, element=None, text=None):
        el_desc = f" [{index}]" if index is not None else ""
        if element:
            el_desc += f" ({element.get('role', '')} {element.get('label', '')!r})"
        msg = f"Act: {operation}{el_desc}"
        if text:
            msg += f" with text={text!r}"
        self._log("ACT", msg, operation=operation, index=index, element=element, text=text)

    def act_success(self, result_msg, page_url=None):
        self._log("SUCCESS", f"Act ok: {result_msg} (url: {page_url})", result=result_msg, url=page_url)

    def act_fail(self, error, operation, index, element=None, fail_count=1):
        el_desc = f" [{index}]" if index is not None else ""
        if element:
            el_desc += f" ({element.get('role', '')} {element.get('label', '')!r})"
        msg = f"Act FAILED: {operation}{el_desc} (fail #{fail_count}) -> {error}"
        self._log("FAIL", msg, error=str(error), operation=operation, index=index, element=element, fail_count=fail_count)

    def page_state(self, url, title, elements_count=0, sample_elements=None, scroll_percent=None, at_bottom=False):
        scroll_info = f" | Scroll: {scroll_percent}%" if scroll_percent is not None else ""
        if at_bottom:
            scroll_info += " (at bottom)"
        self._log(
            "PAGE",
            f"Page: {title!r} | URL: {url} | Elements visible: {elements_count}{scroll_info}",
            url=url,
            title=title,
            elements_count=elements_count,
            sample_elements=sample_elements,
            scroll_percent=scroll_percent,
            at_bottom=at_bottom,
        )

    def site_check(self, url, score, note):
        self._log("JUDGE", f"Site score={score:.2f} ({note}) for {url}", url=url, score=score, note=note)

    def close(self):
        try:
            self._log("END", f"Run ended. Full logs saved to: {self.log_path}")
            self._flush_json()
            self._file.close()
        except Exception:
            pass


_CURRENT_LOGGER = None


def get_logger(goal=None):
    global _CURRENT_LOGGER
    if _CURRENT_LOGGER is None:
        _CURRENT_LOGGER = AgentLogger(goal or "run")
    return _CURRENT_LOGGER


def set_logger(logger):
    global _CURRENT_LOGGER
    _CURRENT_LOGGER = logger
    return _CURRENT_LOGGER
