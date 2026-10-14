"""
Configuration management for Homelab Power Manager.
Reads and writes config.yaml. Auto-migrates from old config.json on first run.

The parsed config is cached and only re-read when the file changes: PyYAML is slow
on a Pi 2 and the dashboard asks for the config several times a second.
"""

import copy
import json
import logging
import os

import yaml

# libyaml-backed loader/dumper when available (much faster on the Pi)
_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_Dumper = getattr(yaml, "CSafeDumper", yaml.SafeDumper)

logger = logging.getLogger(__name__)

CONFIG_FILE = "config.yaml"           # primary config
LEGACY_JSON_FILE = "config.json"      # auto-migrated on first run

_cache: dict = {"key": None, "cfg": None}  # last parsed config + (path, mtime, size)

DEFAULT_CONFIG = {
    "settings_password": "admin",
    "restricted_hours": {
        "start": 2,
        "end": 7,
    },
    "devices": [],
}


def _default_device(name="", ip="", mac=""):
    """Return a fresh device configuration dict with sensible defaults."""
    return {
        "name": name,
        "ip": ip,
        "mac": mac,
        "ssh_key_path": "",
        "ssh_port": 22,
        "ssh_username": "",
        "ssh_key_passphrase": "",
        "shutdown_command": "suspend-now",
        "status_command": "",
        "tautulli_ip": "",
        "tautulli_port": 7979,
        "tautulli_apikey": "",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """
    Load configuration from CONFIG_FILE (YAML).
    - Creates a fresh file with defaults if nothing exists.
    - Migrates old config.json → config.yaml if present.
    """
    # Auto-migrate from legacy JSON
    if os.path.exists(LEGACY_JSON_FILE) and not os.path.exists(CONFIG_FILE):
        _migrate_json_to_yaml()
        logger.info("Migrated config.json → config.yaml")

    if not os.path.exists(CONFIG_FILE):
        _save_yaml(DEFAULT_CONFIG)
        logger.info(f"Created new configuration file: {CONFIG_FILE}")
        return copy.deepcopy(DEFAULT_CONFIG)

    try:
        st = os.stat(CONFIG_FILE)
        key = (CONFIG_FILE, st.st_mtime_ns, st.st_size)
        if _cache["key"] == key:
            return copy.deepcopy(_cache["cfg"])
        with open(CONFIG_FILE, "r") as f:
            cfg = yaml.load(f, Loader=_Loader) or {}
    except (yaml.YAMLError, IOError) as e:
        logger.error(f"Error parsing {CONFIG_FILE}: {e}. Using defaults.")
        return copy.deepcopy(DEFAULT_CONFIG)

    # Fill in any missing top-level keys
    changed = _ensure_defaults(cfg)
    if changed:
        _save_yaml(cfg)
    else:
        _cache["key"], _cache["cfg"] = key, copy.deepcopy(cfg)

    return cfg


def save_config(cfg: dict) -> None:
    """Persist configuration to disk as YAML."""
    _save_yaml(cfg)


def load_config_raw() -> str:
    """
    Return the raw YAML content of the config file.
    Used by the YAML editor in settings.
    """
    if not os.path.exists(CONFIG_FILE):
        return yaml.dump(DEFAULT_CONFIG, default_flow_style=False, sort_keys=False)
    try:
        with open(CONFIG_FILE, "r") as f:
            return f.read()
    except IOError as e:
        logger.error(f"Error reading raw config: {e}")
        return ""


def save_config_raw(raw_yaml: str) -> tuple[bool, str]:
    """
    Validate and save raw YAML.
    Returns (success, error_message).
    """
    # Validate by parsing
    try:
        cfg = yaml.load(raw_yaml, Loader=_Loader)
        if not isinstance(cfg, dict):
            return False, "YAML must be a dictionary (key-value pairs)."
        if "devices" not in cfg or not isinstance(cfg["devices"], list):
            return False, "YAML must contain a 'devices' list."
    except yaml.YAMLError as e:
        return False, f"Invalid YAML: {e}"

    try:
        with open(CONFIG_FILE, "w") as f:
            f.write(raw_yaml)
        _cache["key"] = None
        return True, ""
    except IOError as e:
        return False, f"Failed to write config: {e}"


def get_device_by_name(cfg: dict, name: str) -> dict | None:
    """Find a device in config by its name. Returns None if not found."""
    for device in cfg.get("devices", []):
        if device.get("name") == name:
            return device
    return None


def has_ssh(device: dict) -> bool:
    """Return True if the device has SSH configured (key path is set)."""
    return bool(device.get("ssh_key_path", "").strip())


def has_tautulli(device: dict) -> bool:
    """Return True if Tautulli is configured for this device."""
    return bool(
        device.get("tautulli_ip", "").strip()
        and device.get("tautulli_apikey", "").strip()
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _save_yaml(cfg: dict) -> None:
    """Write config dict as pretty YAML with comments."""
    _cache["key"] = None
    try:
        with open(CONFIG_FILE, "w") as f:
            yaml.dump(cfg, f, Dumper=_Dumper, default_flow_style=False, sort_keys=False,
                      allow_unicode=True)
    except IOError as e:
        logger.error(f"Failed to save configuration: {e}")


def _ensure_defaults(cfg: dict) -> bool:
    """Fill in missing top-level keys. Returns True if anything was added."""
    changed = False
    if "settings_password" not in cfg:
        cfg["settings_password"] = DEFAULT_CONFIG["settings_password"]
        changed = True
    if "restricted_hours" not in cfg:
        cfg["restricted_hours"] = DEFAULT_CONFIG["restricted_hours"]
        changed = True
    if "devices" not in cfg:
        cfg["devices"] = []
        changed = True
    return changed


def _migrate_json_to_yaml() -> None:
    """
    Read old config.json, migrate to new multi-device format, write config.yaml.
    Leaves config.json in place as a backup.
    """
    try:
        with open(LEGACY_JSON_FILE, "r") as f:
            old_cfg = json.load(f)
    except (json.JSONDecodeError, IOError):
        logger.warning("Could not read legacy config.json; starting fresh.")
        _save_yaml(DEFAULT_CONFIG)
        return

    new_cfg = {
        "settings_password": old_cfg.get("settings_password", DEFAULT_CONFIG["settings_password"]),
        "restricted_hours": old_cfg.get("restricted_hours", DEFAULT_CONFIG["restricted_hours"]),
        "devices": [],
    }

    # Migrate old single-device "target_device" key
    if "target_device" in old_cfg:
        td = old_cfg["target_device"]
        dev = _default_device(
            name="Server",
            ip=td.get("ip_address", ""),
            mac=td.get("mac_address", ""),
        )
        dev["ssh_port"] = td.get("ssh_port", 22)
        dev["ssh_username"] = td.get("ssh_username", "")
        dev["ssh_key_path"] = td.get("ssh_key_path", "")
        dev["ssh_key_passphrase"] = td.get("ssh_key_passphrase", "")
        # Old top-level plex config → this device's Tautulli
        plex = old_cfg.get("plex", {})
        if plex:
            dev["tautulli_ip"] = plex.get("tautulli_ip", "")
            dev["tautulli_port"] = plex.get("tautulli_port", 7979)
            dev["tautulli_apikey"] = plex.get("tautulli_apikey", "")
        new_cfg["devices"].append(dev)

    # Migrate old "pc_device" key
    if "pc_device" in old_cfg:
        pd = old_cfg["pc_device"]
        dev = _default_device(
            name="PC",
            ip=pd.get("ip_address", ""),
            mac=pd.get("mac_address", ""),
        )
        new_cfg["devices"].append(dev)

    _save_yaml(new_cfg)
    logger.info("Migration complete: config.json → config.yaml")
