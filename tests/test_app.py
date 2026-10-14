"""
Tests for app.py — Flask routes.
"""

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import app as flask_app


@pytest.fixture
def client():
    """Flask test client."""
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


@pytest.fixture
def mock_load_config(monkeypatch):
    """Mock cfg.load_config to return a consistent test configuration."""
    test_config = {
        "settings_password": "admin",
        "restricted_hours": {"start": 2, "end": 7},
        "devices": [
            {
                "name": "TestServer",
                "ip": "10.0.0.1",
                "mac": "aa:bb:cc:dd:ee:ff",
                "ssh_key_path": "/path/to/key",
                "ssh_port": 22,
                "ssh_username": "pi",
                "ssh_key_passphrase": "",
                "shutdown_command": "suspend-now",
                "tautulli_ip": "10.0.0.10",
                "tautulli_port": 7979,
                "tautulli_apikey": "abc123",
            },
            {
                "name": "TestPC",
                "ip": "10.0.0.2",
                "mac": "11:22:33:44:55:66",
                "ssh_key_path": "",  # No SSH
                "ssh_port": 22,
                "ssh_username": "",
                "ssh_key_passphrase": "",
                "shutdown_command": "",
                "tautulli_ip": "",  # No Tautulli
                "tautulli_port": 7979,
                "tautulli_apikey": "",
            },
        ],
    }

    monkeypatch.setattr("app.cfg.load_config", lambda: json.loads(json.dumps(test_config)))
    monkeypatch.setattr("app.cfg.save_config", lambda x: None)
    monkeypatch.setattr("app.cfg.load_config_raw", lambda: "settings_password: admin\ndevices: []\n")
    monkeypatch.setattr("app.cfg.save_config_raw", lambda yaml_str: (True, ""))
    monkeypatch.setattr("app.logging_utils.log_event", lambda *a, **kw: None)
    monkeypatch.setattr("app.devices.ping", lambda ip, timeout=1.0: False)
    monkeypatch.setattr("operations.time.sleep", lambda s: None)
    flask_app.ops._ops.clear()
    for name in list(flask_app._schedules):
        flask_app._cancel_schedule(name, quiet=True)
    return test_config


def wait_for_op(name):
    """Wait until the device's operation leaves the running state."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        op = flask_app.ops.current(name)
        if op and op["state"] != "running":
            return op
        threading.Event().wait(0.01)
    raise AssertionError(f"operation for {name} did not finish")


class TestRoutes:
    def test_index_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"<html" in resp.data.lower() or b"<!doctype" in resp.data.lower()

    def test_manifest_returns_json(self, client):
        resp = client.get("/manifest.json")
        assert resp.status_code == 200

    def test_settings_page(self, client):
        resp = client.get("/settings")
        assert resp.status_code == 200


class TestDevicesAPI:
    def test_list_devices(self, client, mock_load_config):
        resp = client.get("/api/devices")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 2
        names = [d["name"] for d in data]
        assert "TestServer" in names
        assert "TestPC" in names

    def test_device_properties(self, client, mock_load_config):
        resp = client.get("/api/devices")
        data = resp.get_json()
        server = next(d for d in data if d["name"] == "TestServer")
        pc = next(d for d in data if d["name"] == "TestPC")

        assert server["has_ssh"] is True
        assert server["has_tautulli"] is True
        assert pc["has_ssh"] is False
        assert pc["has_tautulli"] is False


class TestStatusAPI:
    def test_get_status(self, client, mock_load_config):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "devices" in data
        assert "restricted_hours" in data
        server = data["devices"][0]
        assert server["online"] is False
        assert server["op"] is None
        assert server["scheduled"] is None


class TestControlAPI:
    def test_turn_on_unknown_device(self, client, mock_load_config):
        resp = client.post(
            "/api/control",
            json={"action": "turn_on", "device": "Nonexistent"},
        )
        assert resp.status_code == 404

    @patch("app.devices.check_restricted_hours", return_value=False)
    @patch("operations.devices.send_wol")
    def test_turn_on_success(self, mock_wol, mock_restricted, client, mock_load_config, monkeypatch):
        resp = client.post(
            "/api/control",
            json={"action": "turn_on", "device": "TestServer"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        monkeypatch.setattr("operations.devices.ping", lambda ip, timeout=1.0: True)
        op = wait_for_op("TestServer")
        assert op["state"] == "success"
        assert mock_wol.called
        assert "online" in op["steps"][-1]["msg"]

    @patch("app.devices.check_restricted_hours", return_value=True)
    def test_turn_on_denied_in_quiet_hours(self, mock_restricted, client, mock_load_config):
        resp = client.post("/api/control", json={"action": "turn_on", "device": "TestServer"})
        data = resp.get_json()
        assert data["success"] is False
        assert "02:00" in data["message"]

    def test_turn_off_success(self, client, mock_load_config, monkeypatch):
        monkeypatch.setattr("operations.devices.run_ssh", lambda d, c, timeout=10: (0, "ok", ""))

        resp = client.post(
            "/api/control",
            json={"action": "turn_off", "device": "TestServer"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        op = wait_for_op("TestServer")
        assert op["state"] == "success"

    def test_turn_off_blocked_then_dismissed(self, client, mock_load_config, monkeypatch):
        monkeypatch.setattr(
            "operations.devices.run_ssh",
            lambda d, c, timeout=10: (3, "goodnight: Plex: bob is watching X\n", ""),
        )
        client.post("/api/control", json={"action": "turn_off", "device": "TestServer"})
        op = wait_for_op("TestServer")
        assert op["state"] == "blocked"
        assert any("bob is watching" in s["msg"] for s in op["steps"])

        client.post("/api/control", json={"action": "dismiss", "device": "TestServer"})
        assert flask_app.ops.current("TestServer") is None

    def test_turn_off_no_ssh(self, client, mock_load_config):
        """Turning off a device without SSH should fail."""
        resp = client.post(
            "/api/control",
            json={"action": "turn_off", "device": "TestPC"},
        )
        assert resp.status_code == 400

    def test_schedule_off_and_cancel(self, client, mock_load_config):
        resp = client.post(
            "/api/control",
            json={"action": "schedule_off", "device": "TestServer", "hours": 2},
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        status = client.get("/api/status").get_json()
        assert status["devices"][0]["scheduled"]["at"] > status["now"]

        resp = client.post("/api/control", json={"action": "cancel_schedule", "device": "TestServer"})
        assert resp.get_json()["success"] is True
        assert client.get("/api/status").get_json()["devices"][0]["scheduled"] is None

    def test_schedule_rejects_bad_delay(self, client, mock_load_config):
        resp = client.post(
            "/api/control",
            json={"action": "schedule_off", "device": "TestServer", "hours": "abc"},
        )
        assert resp.status_code == 400


class TestSettingsAPI:
    def test_auth_wrong_password(self, client, mock_load_config):
        resp = client.post(
            "/api/settings/auth",
            json={"password": "wrongpassword"},
        )
        assert resp.status_code == 401

    def test_auth_correct_password(self, client, mock_load_config):
        resp = client.post(
            "/api/settings/auth",
            headers={"X-Settings-Password": "admin"},
            json={"password": "admin"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_save_settings_unauthorized(self, client, mock_load_config):
        resp = client.post(
            "/api/settings",
            headers={"X-Settings-Password": "wrong"},
            json={"devices": []},
        )
        assert resp.status_code == 401

    def test_save_settings_authorized(self, client, mock_load_config):
        resp = client.post(
            "/api/settings",
            headers={"X-Settings-Password": "admin"},
            json={
                "restricted_hours": {"start": 1, "end": 6},
                "devices": mock_load_config["devices"],
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True


class TestSettingsYamlAPI:
    """Tests for the YAML raw editor endpoints."""

    def test_get_yaml_unauthorized(self, client, mock_load_config):
        resp = client.get(
            "/api/settings/yaml",
            headers={"X-Settings-Password": "wrong"},
        )
        assert resp.status_code == 401

    def test_get_yaml_authorized(self, client, mock_load_config):
        resp = client.get(
            "/api/settings/yaml",
            headers={"X-Settings-Password": "admin"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "yaml" in data
        assert "devices" in data["yaml"]

    def test_save_yaml_unauthorized(self, client, mock_load_config):
        resp = client.post(
            "/api/settings/yaml",
            headers={"X-Settings-Password": "wrong"},
            json={"yaml": "devices: []"},
        )
        assert resp.status_code == 401

    def test_save_yaml_invalid(self, client, mock_load_config, monkeypatch):
        monkeypatch.setattr("app.cfg.save_config_raw", lambda y: (False, "Invalid YAML"))
        resp = client.post(
            "/api/settings/yaml",
            headers={"X-Settings-Password": "admin"},
            json={"yaml": "::: bad yaml :::"},
        )
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    def test_save_yaml_valid(self, client, mock_load_config):
        resp = client.post(
            "/api/settings/yaml",
            headers={"X-Settings-Password": "admin"},
            json={
                "yaml": "settings_password: admin\nrestricted_hours:\n  start: 2\n  end: 7\ndevices: []\n",
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True


class TestLogsAPI:
    def test_events_endpoint(self, client):
        resp = client.get("/api/events?category=power")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data["events"], list)
        assert "has_more" in data

    def test_system_log_endpoint(self, client):
        resp = client.get("/api/logs?type=system&limit=5")
        assert resp.status_code == 200
        assert isinstance(resp.get_json()["lines"], list)

    def test_activity_logs_returns_list(self, client):
        """Activity logs endpoint returns a list (may contain startup entries)."""
        resp = client.get("/api/logs?type=activity")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_invalid_log_type(self, client):
        resp = client.get("/api/logs?type=invalid")
        assert resp.status_code == 400


class TestOverrideWindow:
    """'Shut down anyway' / 'Keep it on' follow goodnight's 10-minute window."""

    def _block(self, client, monkeypatch, calls):
        def fake_ssh(device, command, timeout=10):
            calls.append(command)
            return (3, "", "") if command == "suspend-now" else (0, "", "")
        monkeypatch.setattr("operations.devices.run_ssh", fake_ssh)
        client.post("/api/control", json={"action": "turn_off", "device": "TestServer"})
        assert wait_for_op("TestServer")["state"] == "blocked"

    def test_force_inside_window(self, client, mock_load_config, monkeypatch):
        calls = []
        self._block(client, monkeypatch, calls)
        client.post("/api/control", json={"action": "turn_off", "device": "TestServer", "force": True})
        op = wait_for_op("TestServer")
        assert calls[-1] == "suspend-now --force"
        assert "(forced)" in op["title"]

    def test_force_after_window_is_a_fresh_check(self, client, mock_load_config, monkeypatch):
        calls = []
        self._block(client, monkeypatch, calls)
        flask_app.ops._ops["TestServer"]["force_until"] = 0  # window expired
        client.post("/api/control", json={"action": "turn_off", "device": "TestServer", "force": True})
        wait_for_op("TestServer")
        assert calls[-1] == "suspend-now"

    def test_keep_it_on_cancels_window(self, client, mock_load_config, monkeypatch):
        calls = []
        self._block(client, monkeypatch, calls)
        client.post("/api/control", json={"action": "dismiss", "device": "TestServer"})
        deadline = time.monotonic() + 3
        while "suspend-now --cancel" not in calls and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert "suspend-now --cancel" in calls
