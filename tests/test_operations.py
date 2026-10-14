"""
Tests for operations.py - shutdown progress parsing and wake timeouts.
"""

import threading
import time

import pytest

import operations


class FakeMonitor:
    def __init__(self):
        self.seen = []

    def set_online(self, name, online):
        self.seen.append((name, online))


DEVICE = {
    "name": "Server",
    "ip": "10.0.0.1",
    "mac": "aa:bb:cc:dd:ee:ff",
    "ssh_key_path": "/key",
    "ssh_username": "me",
    "shutdown_command": "goodnight",
    "status_command": "goodnight --progress",
}


@pytest.fixture
def ops(monkeypatch):
    monkeypatch.setattr(operations.logging_utils, "log_event", lambda *a, **kw: None)
    monkeypatch.setattr(operations.time, "sleep", lambda s: None)
    return operations.Operations(FakeMonitor())


def wait(ops, name="Server"):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        op = ops.current(name)
        if op and op["state"] != "running":
            return op
        threading.Event().wait(0.01)
    raise AssertionError("operation did not finish")


def test_shutdown_follows_remote_progress(ops, monkeypatch):
    progress = iter([
        # right after the command: the activity check results
        "state\tidle\n1700000000\tstep\tchecking for activity\n1700000001\tok\tall quiet\n",
        # first poll: running
        "state\trunning\n1700000000\tstep\tchecking for activity\n1700000001\tok\tall quiet\n"
        "1700000002\tstep\t1/3 stopping Minecraft\n",
        # second poll: finished
        "state\trunning\n1700000002\tstep\t1/3 stopping Minecraft\n1700000009\tdone\tpowering off now\n",
    ])

    def fake_ssh(device, command, timeout=10):
        if command == "goodnight":
            return 0, "goodnight: all quiet\n", ""
        return 0, next(progress), ""

    monkeypatch.setattr(operations.devices, "run_ssh", fake_ssh)
    monkeypatch.setattr(operations.devices, "ping", lambda ip, timeout=1.0: False)

    ops.start_shutdown(DEVICE)
    op = wait(ops)
    msgs = [s["msg"] for s in op["steps"]]
    assert op["state"] == "success"
    # each remote line appears exactly once, in order
    assert msgs.count("1/3 stopping Minecraft") == 1
    assert msgs.index("checking for activity") < msgs.index("powering off now")
    assert ("Server", False) in ops._monitor.seen


def test_shutdown_blocked_shows_activity(ops, monkeypatch):
    def fake_ssh(device, command, timeout=10):
        if command == "goodnight":
            return 3, "", ""
        return 0, "state\tidle\n1700000001\tbusy\tPlex: bob is watching X (playing)\n", ""

    monkeypatch.setattr(operations.devices, "run_ssh", fake_ssh)
    ops.start_shutdown(DEVICE)
    op = wait(ops)
    assert op["state"] == "blocked"
    assert op["can_force"] is True
    assert any(s["level"] == "busy" and "bob" in s["msg"] for s in op["steps"])


def test_shutdown_ssh_failure(ops, monkeypatch):
    monkeypatch.setattr(operations.devices, "run_ssh", lambda d, c, timeout=10: (255, "", "Connection refused"))
    ops.start_shutdown(DEVICE)
    op = wait(ops)
    assert op["state"] == "failed"
    assert "Connection refused" in op["steps"][-1]["msg"]


def test_only_one_operation_at_a_time(ops, monkeypatch):
    gate = threading.Event()

    def slow_ssh(device, command, timeout=10):
        gate.wait(5)
        return 3, "", ""

    monkeypatch.setattr(operations.devices, "run_ssh", slow_ssh)
    assert ops.start_shutdown(DEVICE) is not None
    assert ops.start_shutdown(DEVICE) is None
    gate.set()
    assert wait(ops)["state"] == "blocked"


def test_wake_times_out(ops, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(operations.time, "time", lambda: clock[0])
    monkeypatch.setattr(operations.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(operations.devices, "send_wol", lambda mac, bcast: None)
    monkeypatch.setattr(operations.devices, "ping", lambda ip, timeout=1.0: False)
    ops.start_wake(DEVICE)
    op = wait(ops)
    assert op["state"] == "failed"
    assert any("sent the magic packet again" in s["msg"] for s in op["steps"])
