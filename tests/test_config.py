"""
Tests for config.py — configuration management module.
Uses YAML-based config with auto-migration from legacy JSON.
"""

import json
import os
import tempfile

import pytest
import yaml

import config as cfg


@pytest.fixture
def temp_yaml(monkeypatch):
    """Create a temporary config.yaml and point CONFIG_FILE to it."""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    monkeypatch.setattr(cfg, "CONFIG_FILE", path)
    monkeypatch.setattr(cfg, "LEGACY_JSON_FILE", path + ".nonexistent")  # no legacy
    cfg._save_yaml(cfg.DEFAULT_CONFIG)
    yield path
    os.unlink(path)


@pytest.fixture
def temp_legacy_json(monkeypatch):
    """Create a legacy config.json and a temp YAML path for migration tests."""
    fd_json, json_path = tempfile.mkstemp(suffix=".json")
    os.close(fd_json)
    fd_yaml, yaml_path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd_yaml)
    os.unlink(yaml_path)  # delete it so migration creates it

    monkeypatch.setattr(cfg, "CONFIG_FILE", yaml_path)
    monkeypatch.setattr(cfg, "LEGACY_JSON_FILE", json_path)

    yield json_path, yaml_path

    if os.path.exists(json_path):
        os.unlink(json_path)
    if os.path.exists(yaml_path):
        os.unlink(yaml_path)


def test_default_config_structure():
    """Default config should have all required keys."""
    default = cfg.DEFAULT_CONFIG
    assert default["settings_password"] == "admin"
    assert "restricted_hours" in default
    assert "devices" in default
    assert isinstance(default["devices"], list)


def test_load_creates_file(temp_yaml):
    """Loading config when file exists returns data."""
    data = cfg.load_config()
    assert data["settings_password"] == "admin"
    assert data["devices"] == []


def test_save_and_load(temp_yaml):
    """Save config, then load it back — should match."""
    data = cfg.load_config()
    data["settings_password"] = "secret123"
    data["restricted_hours"]["start"] = 1
    cfg.save_config(data)

    loaded = cfg.load_config()
    assert loaded["settings_password"] == "secret123"
    assert loaded["restricted_hours"]["start"] == 1


def test_migrate_legacy_json(temp_legacy_json):
    """Old-style config.json should be migrated to config.yaml."""
    json_path, yaml_path = temp_legacy_json

    old_cfg = {
        "settings_password": "admin",
        "restricted_hours": {"start": 2, "end": 7},
        "target_device": {
            "mac_address": "aa:bb:cc:dd:ee:ff",
            "ip_address": "10.0.0.5",
            "ssh_port": 22,
            "ssh_username": "pi",
            "ssh_key_path": "/home/pi/.ssh/id_rsa",
            "ssh_key_passphrase": "",
        },
        "plex": {
            "tautulli_ip": "10.0.0.10",
            "tautulli_port": 8181,
            "tautulli_apikey": "abc123",
        },
        "devices": [],
    }
    with open(json_path, "w") as f:
        json.dump(old_cfg, f)

    loaded = cfg.load_config()
    assert os.path.exists(yaml_path), "config.yaml should be created"
    assert len(loaded["devices"]) == 1
    dev = loaded["devices"][0]
    assert dev["name"] == "Server"
    assert dev["ip"] == "10.0.0.5"
    assert dev["mac"] == "aa:bb:cc:dd:ee:ff"
    assert dev["ssh_key_path"] == "/home/pi/.ssh/id_rsa"
    assert dev["tautulli_ip"] == "10.0.0.10"
    assert dev["tautulli_apikey"] == "abc123"


def test_migrate_pc_device(temp_legacy_json):
    """Old-style 'pc_device' should also migrate."""
    json_path, yaml_path = temp_legacy_json

    old_cfg = {
        "settings_password": "admin",
        "restricted_hours": {"start": 2, "end": 7},
        "pc_device": {
            "mac_address": "11:22:33:44:55:66",
            "ip_address": "10.0.0.20",
        },
        "devices": [],
    }
    with open(json_path, "w") as f:
        json.dump(old_cfg, f)

    loaded = cfg.load_config()
    assert len(loaded["devices"]) == 1
    dev = loaded["devices"][0]
    assert dev["name"] == "PC"
    assert dev["ip"] == "10.0.0.20"
    assert dev["mac"] == "11:22:33:44:55:66"
    assert dev["ssh_key_path"] == ""


def test_get_device_by_name(temp_yaml):
    """get_device_by_name finds devices by name, returns None for missing."""
    data = cfg.load_config()
    data["devices"] = [
        {"name": "Server", "ip": "1.1.1.1", "mac": "aa:bb:cc:dd:ee:ff"},
        {"name": "PC", "ip": "2.2.2.2", "mac": "11:22:33:44:55:66"},
    ]
    cfg.save_config(data)

    assert cfg.get_device_by_name(cfg.load_config(), "Server") is not None
    assert cfg.get_device_by_name(cfg.load_config(), "PC") is not None
    assert cfg.get_device_by_name(cfg.load_config(), "Nonexistent") is None


def test_has_ssh():
    """has_ssh returns True only when ssh_key_path is non-empty."""
    assert cfg.has_ssh({"ssh_key_path": "/path/to/key"}) is True
    assert cfg.has_ssh({"ssh_key_path": "  "}) is False
    assert cfg.has_ssh({"ssh_key_path": ""}) is False
    assert cfg.has_ssh({}) is False


def test_has_tautulli():
    """has_tautulli returns True only when both IP and API key are set."""
    assert cfg.has_tautulli({"tautulli_ip": "1.1.1.1", "tautulli_apikey": "abc"}) is True
    assert cfg.has_tautulli({"tautulli_ip": "1.1.1.1", "tautulli_apikey": ""}) is False
    assert cfg.has_tautulli({"tautulli_ip": "", "tautulli_apikey": "abc"}) is False
    assert cfg.has_tautulli({}) is False


def test_load_corrupt_file(monkeypatch):
    """Corrupt YAML file should fall back to defaults."""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    monkeypatch.setattr(cfg, "CONFIG_FILE", path)
    monkeypatch.setattr(cfg, "LEGACY_JSON_FILE", path + ".nonexistent")

    with open(path, "w") as f:
        f.write("::: not valid yaml :::")

    data = cfg.load_config()
    assert data == cfg.DEFAULT_CONFIG
    os.unlink(path)


def test_load_config_raw(temp_yaml):
    """load_config_raw returns YAML string."""
    cfg.save_config(cfg.DEFAULT_CONFIG)
    raw = cfg.load_config_raw()
    assert "settings_password" in raw
    assert "devices" in raw


def test_save_config_raw_valid(temp_yaml):
    """save_config_raw with valid YAML should succeed."""
    raw = "settings_password: test123\nrestricted_hours:\n  start: 0\n  end: 6\ndevices: []\n"
    success, error = cfg.save_config_raw(raw)
    assert success is True
    assert error == ""

    loaded = cfg.load_config()
    assert loaded["settings_password"] == "test123"
    assert loaded["restricted_hours"]["start"] == 0


def test_save_config_raw_invalid_syntax(monkeypatch):
    """save_config_raw with invalid YAML should fail."""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    monkeypatch.setattr(cfg, "CONFIG_FILE", path)
    monkeypatch.setattr(cfg, "LEGACY_JSON_FILE", path + ".nonexistent")
    cfg._save_yaml(cfg.DEFAULT_CONFIG)

    success, error = cfg.save_config_raw("::: bad yaml :::")
    assert success is False
    assert "Invalid YAML" in error
    os.unlink(path)


def test_save_config_raw_missing_devices(monkeypatch):
    """save_config_raw without 'devices' key should fail."""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    monkeypatch.setattr(cfg, "CONFIG_FILE", path)
    monkeypatch.setattr(cfg, "LEGACY_JSON_FILE", path + ".nonexistent")
    cfg._save_yaml(cfg.DEFAULT_CONFIG)

    success, error = cfg.save_config_raw("settings_password: x\nrestricted_hours:\n  start: 2\n  end: 7\n")
    assert success is False
    assert "devices" in error.lower()
    os.unlink(path)


def test_migration_preserves_existing_yaml(temp_legacy_json):
    """When config.yaml already exists, legacy JSON should NOT overwrite it."""
    json_path, yaml_path = temp_legacy_json

    # Create config.yaml first
    existing = {
        "settings_password": "mypass",
        "restricted_hours": {"start": 0, "end": 6},
        "devices": [{"name": "MyDevice", "ip": "10.0.0.99", "mac": "00:11:22:33:44:55"}],
    }
    cfg._save_yaml(existing)

    # Create legacy JSON (should be ignored since YAML exists)
    old_cfg = {
        "settings_password": "old",
        "restricted_hours": {"start": 1, "end": 5},
        "target_device": {"mac_address": "aa:bb:cc:dd:ee:ff", "ip_address": "1.2.3.4"},
        "devices": [],
    }
    with open(json_path, "w") as f:
        json.dump(old_cfg, f)

    loaded = cfg.load_config()
    # Should still have the existing YAML data, not the migrated JSON
    assert loaded["settings_password"] == "mypass"
    assert len(loaded["devices"]) == 1
    assert loaded["devices"][0]["name"] == "MyDevice"
