"""
Tests for logging_utils.py - event log, reverse reading, CSV migration.
"""

import json
import os

import pytest

import logging_utils as lu


@pytest.fixture
def logdir(tmp_path, monkeypatch):
    monkeypatch.setattr(lu, "LOG_DIR", str(tmp_path))
    return tmp_path


def _write_events(logdir, events):
    (logdir / lu.EVENTS_FILE).write_text("".join(json.dumps(e) + "\n" for e in events))


def test_events_newest_first_and_paged(logdir):
    _write_events(logdir, [
        {"ts": 1000 + i, "kind": "wake", "result": "success", "msg": f"event {i}"} for i in range(5)
    ])

    page = lu.read_events(limit=2)
    assert [e["msg"] for e in page["events"]] == ["event 4", "event 3"]
    assert page["has_more"] is True

    page2 = lu.read_events(limit=10, before=page["events"][-1]["ts"])
    assert [e["msg"] for e in page2["events"]] == ["event 2", "event 1", "event 0"]
    assert page2["has_more"] is False


def test_event_categories(logdir):
    lu.log_event("wake", "success", "woke")
    lu.log_event("settings", "success", "saved")
    lu.log_event("shutdown", "blocked", "busy")
    power = [e["msg"] for e in lu.read_events(category="power")["events"]]
    problems = [e["msg"] for e in lu.read_events(category="problems")["events"]]
    assert power == ["busy", "woke"]
    assert problems == ["busy"]


def test_empty_values_are_dropped(logdir):
    ev = lu.log_event("wake", "success", "x", ip="", steps=[])
    assert "ip" not in ev and "steps" not in ev


def test_events_rotate(logdir, monkeypatch):
    monkeypatch.setattr(lu, "MAX_EVENTS_BYTES", 200)
    for i in range(20):
        lu.log_event("wake", "success", f"event {i}")
    assert os.path.exists(logdir / (lu.EVENTS_FILE + ".1"))
    # reading spans both files
    assert len(lu.read_events(limit=100)["events"]) > 3


def test_reverse_lines_crosses_chunks(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("".join(f"line {i}\n" for i in range(500)))
    out = list(lu._reverse_lines(str(p), chunk_size=64))
    assert out[0] == "line 499"
    assert out[-1] == "line 0"
    assert len(out) == 500


def test_system_log_filters(logdir):
    (logdir / lu.SYSTEM_LOG).write_text(
        "2026-10-14 16:05:21,398 - INFO - hello world\n"
        "2026-10-14 16:05:22,000 - ERROR - boom\n"
    )
    assert [line["msg"] for line in lu.read_system_log(level="error")["lines"]] == ["boom"]
    assert [line["msg"] for line in lu.read_system_log(query="HELLO")["lines"]] == ["hello world"]


def test_migrate_activity_csv(logdir):
    (logdir / lu.LEGACY_ACTIVITY_CSV).write_text(
        "Timestamp,Action,Result,Details\n"
        "2026-10-13 02:59:17,page_visit,success,Main page loaded\n"
        "2026-10-14 03:09:49,shutdown,success,Server\n"
        "2026-10-14 16:05:37,turn_on,failed,Server\n"
    )
    assert lu.migrate_activity_csv() == 2
    events = lu.read_events()["events"]
    assert [(e["kind"], e["result"], e["device"]) for e in events] == [
        ("wake", "failed", "Server"),
        ("shutdown", "success", "Server"),
    ]
    assert not os.path.exists(logdir / lu.LEGACY_ACTIVITY_CSV)
    assert lu.migrate_activity_csv() == 0  # only once
