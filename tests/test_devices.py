"""
Tests for devices.py — device management (ping, WoL, SSH, restricted hours).
"""

import datetime
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import devices


class TestPing:
    def test_ping_empty_ip(self):
        assert devices.ping("") is False

    @patch("subprocess.run")
    def test_ping_success(self, mock_run):
        mock_run.return_value.returncode = 0
        assert devices.ping("192.168.1.1") is True

    @patch("subprocess.run")
    def test_ping_failure(self, mock_run):
        mock_run.return_value.returncode = 1
        assert devices.ping("192.168.1.1") is False

    @patch("subprocess.run", side_effect=Exception("boom"))
    def test_ping_exception(self, mock_run):
        assert devices.ping("192.168.1.1") is False


class TestSendWoL:
    @patch("socket.socket")
    def test_send_wol_valid_mac(self, mock_socket_class):
        mock_sock = MagicMock()
        mock_socket_class.return_value.__enter__.return_value = mock_sock

        devices.send_wol("aa:bb:cc:dd:ee:ff")
        mock_sock.sendto.assert_called()

    def test_send_wol_invalid_mac(self):
        with pytest.raises(ValueError):
            devices.send_wol("not-a-mac")


class TestWakeDevice:
    @patch("devices.send_wol")
    @patch("devices.ping")
    def test_wake_device_success(self, mock_ping, mock_wol):
        mock_ping.side_effect = [False, False, True]  # Online after 2 tries
        dev = {"name": "Test", "mac": "aa:bb:cc:dd:ee:ff", "ip": "10.0.0.1"}
        assert devices.wake_device(dev) is True

    @patch("devices.send_wol")
    @patch("devices.ping")
    def test_wake_device_failure(self, mock_ping, mock_wol):
        mock_ping.return_value = False  # Never comes online
        dev = {"name": "Test", "mac": "aa:bb:cc:dd:ee:ff", "ip": "10.0.0.1"}
        assert devices.wake_device(dev) is False

    def test_wake_device_no_mac(self):
        dev = {"name": "Test", "ip": "10.0.0.1"}
        assert devices.wake_device(dev) is False

    def test_wake_device_no_ip(self):
        dev = {"name": "Test", "mac": "aa:bb:cc:dd:ee:ff"}
        assert devices.wake_device(dev) is False


class TestShutdownDevice:
    def _make_result(self, returncode=0, stdout="", stderr=""):
        """Helper: create a CompletedProcess-like mock."""
        from unittest.mock import MagicMock
        result = MagicMock()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = stderr
        return result

    def test_shutdown_missing_config(self):
        """Missing IP, username, or key should fail early."""
        dev = {"name": "Test", "ip": "", "ssh_key_path": "/key", "ssh_username": "pi"}
        assert devices.shutdown_device(dev) is False

        dev = {"name": "Test", "ip": "1.1.1.1", "ssh_key_path": "", "ssh_username": "pi"}
        assert devices.shutdown_device(dev) is False

        dev = {"name": "Test", "ip": "1.1.1.1", "ssh_key_path": "/key", "ssh_username": ""}
        assert devices.shutdown_device(dev) is False

    @patch("devices.os.path.isfile", return_value=True)
    @patch("devices.subprocess.run")
    def test_shutdown_uses_custom_command(self, mock_run, mock_isfile):
        """Verify the custom shutdown command is used."""
        mock_run.return_value = self._make_result(returncode=0)

        dev = {
            "name": "Test",
            "ip": "10.0.0.1",
            "ssh_port": 22,
            "ssh_username": "pi",
            "ssh_key_path": "/key",
            "ssh_key_passphrase": "",
            "shutdown_command": "poweroff",
        }

        devices.shutdown_device(dev)
        # The last arg to ssh should be the command
        cmd_list = mock_run.call_args[0][0]
        assert cmd_list[-1] == "poweroff"

    @patch("devices.os.path.isfile", return_value=True)
    @patch("devices.subprocess.run")
    def test_shutdown_default_command(self, mock_run, mock_isfile):
        """If no shutdown_command is set, default to suspend-now."""
        mock_run.return_value = self._make_result(returncode=0)

        dev = {
            "name": "Test",
            "ip": "10.0.0.1",
            "ssh_port": 22,
            "ssh_username": "pi",
            "ssh_key_path": "/key",
        }

        devices.shutdown_device(dev)
        cmd_list = mock_run.call_args[0][0]
        assert cmd_list[-1] == "suspend-now"

    @patch("devices.os.path.isfile", return_value=False)
    def test_shutdown_key_not_found(self, mock_isfile):
        """Missing SSH key file should fail."""
        dev = {
            "name": "Test",
            "ip": "10.0.0.1",
            "ssh_username": "pi",
            "ssh_key_path": "/nonexistent",
        }
        assert devices.shutdown_device(dev) is False

    @patch("devices.os.path.isfile", return_value=True)
    @patch("devices.subprocess.run")
    def test_shutdown_command_fails(self, mock_run, mock_isfile):
        """If the remote command returns non-zero exit code, should return False."""
        mock_run.return_value = self._make_result(returncode=1, stderr="Permission denied")

        dev = {
            "name": "Test",
            "ip": "10.0.0.1",
            "ssh_username": "pi",
            "ssh_key_path": "/key",
        }

        assert devices.shutdown_device(dev) is False

    @patch("devices.os.path.isfile", return_value=True)
    @patch("devices.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=10))
    def test_shutdown_timeout_is_ok(self, mock_run, mock_isfile):
        """Timeout during shutdown is normal (target suspends before replying)."""
        dev = {
            "name": "Test",
            "ip": "10.0.0.1",
            "ssh_username": "pi",
            "ssh_key_path": "/key",
        }

        assert devices.shutdown_device(dev) is True

    @patch("devices.os.path.isfile", return_value=True)
    @patch("devices.subprocess.run")
    def test_shutdown_with_passphrase(self, mock_run, mock_isfile):
        """When passphrase is set, sshpass should be prepended."""
        mock_run.return_value = self._make_result(returncode=0)

        dev = {
            "name": "Test",
            "ip": "10.0.0.1",
            "ssh_username": "pi",
            "ssh_key_path": "/key",
            "ssh_key_passphrase": "secret123",
        }

        devices.shutdown_device(dev)
        cmd_list = mock_run.call_args[0][0]
        assert cmd_list[0] == "sshpass"


class TestRestrictedHours:
    def test_not_restricted(self):
        """At noon, restricted hours (2-7) should not be active."""
        now = datetime.datetime(2025, 1, 1, 12, 0, 0)
        assert devices.check_restricted_hours({"start": 2, "end": 7}, now=now) is False

    def test_restricted(self):
        """At 4 AM, restricted hours should be active."""
        now = datetime.datetime(2025, 1, 1, 4, 0, 0)
        assert devices.check_restricted_hours({"start": 2, "end": 7}, now=now) is True

    def test_edge_start(self):
        """At exactly the start hour, should be restricted."""
        now = datetime.datetime(2025, 1, 1, 2, 0, 0)
        assert devices.check_restricted_hours({"start": 2, "end": 7}, now=now) is True

    def test_edge_end(self):
        """At exactly the end hour, should NOT be restricted."""
        now = datetime.datetime(2025, 1, 1, 7, 0, 0)
        assert devices.check_restricted_hours({"start": 2, "end": 7}, now=now) is False
