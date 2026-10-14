"""
Logging utilities for Homelab Power Manager.

* system.log  - rotating text log of everything the app does (debugging).
* events.jsonl - one JSON object per line, one line per thing a person cares about
  (wake, shutdown, schedule, the server going on/offline, settings changes).
  Read newest-first straight from the end of the file, so the Pi never loads the
  whole log into memory.
"""

import csv
import datetime
import json
import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler

# Absolute path to the project root (where app.py lives)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(_BASE_DIR, "logs")
SYSTEM_LOG = "system.log"
EVENTS_FILE = "events.jsonl"
LEGACY_ACTIVITY_CSV = "activity.csv"
MAX_EVENTS_BYTES = 1024 * 1024  # rotate to events.jsonl.1 past 1 MB (~5k events)

# Which event kinds / results each filter chip in the UI shows
POWER_KINDS = {"wake", "shutdown", "schedule", "state"}
PROBLEM_RESULTS = {"failed", "denied", "blocked", "error"}

_events_lock = threading.Lock()


def _path(name: str) -> str:
    return os.path.join(LOG_DIR, name)


def _ensure_log_dir() -> None:
    """Create the logs directory if it doesn't exist."""
    os.makedirs(LOG_DIR, exist_ok=True)


def setup_system_logger() -> logging.Logger:
    """Create and configure the rotating system log handler."""
    _ensure_log_dir()
    logger = logging.getLogger("homelab_pm")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            _path(SYSTEM_LOG),
            maxBytes=1024 * 1024,  # 1 MB
            backupCount=3,
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        )
        logger.addHandler(handler)
    return logger


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
def log_event(kind: str, result: str, message: str, device: str = "", **extra) -> dict:
    """
    Append an event. `extra` may carry `ip`, `duration` (seconds) and `steps`
    (the timeline of an operation) - empty values are dropped to keep lines short.
    """
    entry = {"ts": round(time.time(), 1), "kind": kind, "result": result, "msg": message}
    if device:
        entry["device"] = device
    for key, value in extra.items():
        if value not in (None, "", [], {}):
            entry[key] = value

    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _events_lock:
        try:
            _ensure_log_dir()
            path = _path(EVENTS_FILE)
            if os.path.exists(path) and os.path.getsize(path) > MAX_EVENTS_BYTES:
                os.replace(path, path + ".1")
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            logging.getLogger("homelab_pm").error(f"Failed to write event log: {e}")
    return entry


def _event_matches(ev: dict, category: str, device: str) -> bool:
    if device and ev.get("device") not in ("", None, device):
        return False
    if category == "power":
        return ev.get("kind") in POWER_KINDS
    if category == "problems":
        return ev.get("result") in PROBLEM_RESULTS
    return True


def read_events(limit: int = 50, before: float | None = None,
                category: str = "all", device: str = "") -> dict:
    """
    Newest-first page of events. `before` is the `ts` of the last event the
    client already has (for "load more").
    """
    items = []
    has_more = False
    for name in (EVENTS_FILE, EVENTS_FILE + ".1"):
        for line in _reverse_lines(_path(name)):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if before is not None and ev.get("ts", 0) >= before:
                continue
            if not _event_matches(ev, category, device):
                continue
            if len(items) == limit:
                has_more = True
                break
            items.append(ev)
        if has_more:
            break
    return {"events": items, "has_more": has_more}


def read_system_log(limit: int = 200, level: str = "", query: str = "", skip: int = 0) -> dict:
    """Newest-first system log lines, optionally filtered by level / text."""
    level = level.upper()
    query = query.lower()
    lines = []
    matched = 0
    has_more = False
    for name in (SYSTEM_LOG, SYSTEM_LOG + ".1"):
        for line in _reverse_lines(_path(name)):
            if level and f" - {level} - " not in line:
                continue
            if query and query not in line.lower():
                continue
            matched += 1
            if matched <= skip:
                continue
            if len(lines) == limit:
                has_more = True
                break
            lines.append(_parse_system_line(line))
        if has_more:
            break
    return {"lines": lines, "has_more": has_more}


def _parse_system_line(line: str) -> dict:
    # "2026-10-14 16:05:21,398 - INFO - message"
    parts = line.split(" - ", 2)
    if len(parts) == 3 and len(parts[0]) >= 19:
        return {"time": parts[0][:19], "level": parts[1], "msg": parts[2]}
    return {"time": "", "level": "", "msg": line}


def _reverse_lines(path: str, chunk_size: int = 8192):
    """Yield the lines of a text file from last to first, reading from the end."""
    if not os.path.exists(path):
        return
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        tail = b""
        while pos > 0:
            step = min(chunk_size, pos)
            pos -= step
            f.seek(pos)
            block = f.read(step) + tail
            parts = block.split(b"\n")
            tail = parts.pop(0)  # possibly incomplete first line
            for raw in reversed(parts):
                if raw.strip():
                    yield raw.decode("utf-8", errors="replace").rstrip("\r")
        if tail.strip():
            yield tail.decode("utf-8", errors="replace").rstrip("\r")


# ---------------------------------------------------------------------------
# One-time migration of the old activity.csv
# ---------------------------------------------------------------------------
_LEGACY_KINDS = {
    "turn_on": ("wake", "Wake-on-LAN sent to {d}"),
    "shutdown": ("shutdown", "Shutdown sent to {d}"),
    "schedule": ("schedule", "Scheduled shutdown: {d}"),
    "settings": ("settings", "{d}"),
    "startup": ("app", "App started"),
}


def migrate_activity_csv() -> int:
    """Convert logs/activity.csv into events.jsonl (once). Returns rows migrated."""
    csv_path = _path(LEGACY_ACTIVITY_CSV)
    events_path = _path(EVENTS_FILE)
    if not os.path.exists(csv_path) or os.path.exists(events_path):
        return 0

    out = []
    try:
        with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
            for row in csv.reader(f):
                if len(row) < 4 or row[1] not in _LEGACY_KINDS:
                    continue  # header, page visits, junk
                try:
                    ts = datetime.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").timestamp()
                except ValueError:
                    continue
                kind, template = _LEGACY_KINDS[row[1]]
                details = row[3]
                device = details.split(" - ")[0] if kind in ("wake", "shutdown", "schedule") else ""
                ev = {"ts": ts, "kind": kind, "result": row[2], "msg": template.format(d=details)}
                if device:
                    ev["device"] = device
                out.append(json.dumps(ev, ensure_ascii=False, separators=(",", ":")))
        _ensure_log_dir()
        with open(events_path, "w", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in out))
        os.replace(csv_path, csv_path + ".migrated")
    except OSError as e:
        logging.getLogger("homelab_pm").error(f"activity.csv migration failed: {e}")
        return 0
    return len(out)
