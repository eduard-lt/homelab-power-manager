"""
Homelab Power Manager — Flask web application.
Provides remote Wake-on-LAN, SSH shutdown, scheduled shutdown,
Plex/Tautulli stream monitoring, and a password-protected settings page.
"""

import csv
import logging
import os
import threading
import datetime

from flask import Flask, jsonify, render_template, request, send_from_directory

import config as cfg
import devices
import logging_utils
import plex

# ---------------------------------------------------------------------------
# App & logging setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
system_logger = logging_utils.setup_system_logger()

# ---------------------------------------------------------------------------
# Plex monitor (shared across devices)
# ---------------------------------------------------------------------------
plex_monitor = plex.PlexMonitor()

# ---------------------------------------------------------------------------
# Scheduled shutdown timers (device_name -> threading.Timer)
# ---------------------------------------------------------------------------
off_timers: dict[str, threading.Timer] = {}


# ---------------------------------------------------------------------------
# Werkzeug TLS scanner noise suppression
# ---------------------------------------------------------------------------
class _TLSScannerFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if "Bad request version" in msg or "\x16\x03" in msg:
            return False
        return True


logging.getLogger("werkzeug").addFilter(_TLSScannerFilter())


# ---------------------------------------------------------------------------
# Routes — UI
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    system_logger.info("Web interface accessed")
    logging_utils.log_activity("page_visit", "success", "Main page loaded")
    return render_template("index.html")


@app.route("/settings")
def settings_page():
    """Serve the password-protected settings page."""
    return render_template("settings.html")


@app.route("/manifest.json")
def manifest():
    return send_from_directory(app.root_path, "manifest.json")


# ---------------------------------------------------------------------------
# Routes — Device API
# ---------------------------------------------------------------------------
@app.route("/api/devices", methods=["GET"])
def api_devices():
    """Return all configured devices (without sensitive fields)."""
    config = cfg.load_config()
    devices_list = config.get("devices", [])

    result = []
    for d in devices_list:
        result.append(
            {
                "name": d.get("name", ""),
                "has_ssh": cfg.has_ssh(d),
                "has_tautulli": cfg.has_tautulli(d),
            }
        )
    return jsonify(result)


@app.route("/api/status", methods=["GET"])
def api_status():
    """Return status for all devices + restricted hours info."""
    config = cfg.load_config()
    devices_list = config.get("devices", [])
    restricted = config.get("restricted_hours", {"start": 2, "end": 7})

    in_restricted = devices.check_restricted_hours(restricted)

    statuses = []
    for d in devices_list:
        name = d.get("name", "")
        ip = d.get("ip", "")
        online = devices.ping(ip) if ip else False
        streams = []
        if cfg.has_tautulli(d):
            streams = plex_monitor.get_streams(d)

        # Check for scheduled timer
        scheduled = None
        timer = off_timers.get(name)
        # We store scheduled times in a simple dict alongside the timer
        # Since Timer doesn't let us query the remaining time easily,
        # we track it separately
        statuses.append(
            {
                "name": name,
                "online": online,
                "has_ssh": cfg.has_ssh(d),
                "has_tautulli": cfg.has_tautulli(d),
                "streams": streams,
            }
        )

    # Gather scheduled off times
    scheduled_times = {}
    for name, timer in off_timers.items():
        if timer and timer.is_alive():
            # We store scheduled times in a module-level dict
            scheduled_times[name] = _scheduled_off_times.get(name)

    return jsonify(
        {
            "devices": statuses,
            "restricted_hours": in_restricted,
            "scheduled": scheduled_times,
        }
    )


# Module-level dict for tracking scheduled shutdown times
_scheduled_off_times: dict[str, str] = {}


@app.route("/api/control", methods=["POST"])
def api_control():
    """Handle device control actions: turn_on, turn_off, schedule_off."""
    data = request.json
    if not data:
        return jsonify({"success": False, "message": "No data provided"}), 400

    action = data.get("action")
    device_name = data.get("device")
    client_ip = request.remote_addr

    system_logger.info(f"Control action '{action}' for '{device_name}' from {client_ip}")

    config = cfg.load_config()
    device = cfg.get_device_by_name(config, device_name)

    if not device:
        return jsonify({"success": False, "message": f"Device '{device_name}' not found"}), 404

    restricted = config.get("restricted_hours", {"start": 2, "end": 7})

    if action == "turn_on":
        if devices.check_restricted_hours(restricted):
            system_logger.warning(f"Turn on denied for '{device_name}' during restricted hours")
            logging_utils.log_activity("turn_on", "denied", f"Restricted hours - {device_name}")
            return jsonify(
                {
                    "success": False,
                    "message": "Cannot turn on during restricted hours (2 AM - 7 AM)",
                }
            )

        success = devices.wake_device(device)

        if success:
            # Cancel any scheduled off timer for this device
            _cancel_timer(device_name)
            logging_utils.log_activity("turn_on", "success", device_name)
        else:
            logging_utils.log_activity("turn_on", "failed", device_name)

        return jsonify({"success": success})

    elif action == "turn_off":
        if not cfg.has_ssh(device):
            return jsonify(
                {"success": False, "message": "SSH not configured for this device"}
            ), 400

        success = devices.shutdown_device(device)

        if success:
            _cancel_timer(device_name)
            logging_utils.log_activity("shutdown", "success", device_name)
        else:
            logging_utils.log_activity("shutdown", "failed", device_name)

        return jsonify({"success": success})

    elif action == "schedule_off":
        hours = int(data.get("hours", 1))
        if not cfg.has_ssh(device):
            return jsonify(
                {"success": False, "message": "SSH not configured for this device"}
            ), 400

        schedule_device_off(device_name, device, hours)
        return jsonify(
            {
                "success": True,
                "scheduled_time": _scheduled_off_times.get(device_name),
            }
        )

    return jsonify({"success": False, "message": f"Invalid action: {action}"}), 400


def _cancel_timer(device_name: str) -> None:
    """Cancel a scheduled shutdown timer for a device if one exists."""
    timer = off_timers.pop(device_name, None)
    if timer:
        timer.cancel()
        system_logger.info(f"Cancelled scheduled shutdown for '{device_name}'")
    _scheduled_off_times.pop(device_name, None)


def schedule_device_off(device_name: str, device: dict, hours: int) -> None:
    """Schedule a shutdown for a device after the given number of hours."""
    _cancel_timer(device_name)

    def turn_off():
        system_logger.info(f"Executing scheduled shutdown for '{device_name}'")
        devices.shutdown_device(device)
        off_timers.pop(device_name, None)
        _scheduled_off_times.pop(device_name, None)

    seconds = hours * 3600
    timer = threading.Timer(seconds, turn_off)
    timer.daemon = True
    timer.start()

    scheduled_time = datetime.datetime.now() + datetime.timedelta(hours=hours)
    time_str = scheduled_time.strftime("%Y-%m-%d %H:%M:%S")
    off_timers[device_name] = timer
    _scheduled_off_times[device_name] = time_str

    system_logger.info(f"Scheduled shutdown for '{device_name}' in {hours}h ({time_str})")
    logging_utils.log_activity(
        "schedule", "success", f"{device_name} - shutdown in {hours}h"
    )


# ---------------------------------------------------------------------------
# Routes — Plex streams (per-device)
# ---------------------------------------------------------------------------
@app.route("/api/plex/streams", methods=["GET"])
def api_plex_streams():
    """Get Plex streams for a specific device."""
    device_name = request.args.get("device", "")
    config = cfg.load_config()
    device = cfg.get_device_by_name(config, device_name)

    if not device or not cfg.has_tautulli(device):
        return jsonify([])

    return jsonify(plex_monitor.get_streams(device))


# ---------------------------------------------------------------------------
# Routes — Logs
# ---------------------------------------------------------------------------
@app.route("/api/logs", methods=["GET"])
def api_logs():
    log_type = request.args.get("type", "activity")
    limit = int(request.args.get("limit", 100))

    if log_type == "activity":
        log_file = os.path.join(logging_utils.LOG_DIR, "activity.csv")
        if not os.path.exists(log_file):
            return jsonify([])

        try:
            with open(log_file, "r") as f:
                reader = csv.reader(f)
                headers = next(reader)
                all_rows = list(reader)
                rows = all_rows[-limit:] if len(all_rows) > limit else all_rows

                logs = []
                for row in rows:
                    if len(row) >= 4:
                        logs.append(
                            {
                                "timestamp": row[0],
                                "action": row[1],
                                "result": row[2],
                                "details": row[3],
                            }
                        )
                return jsonify(logs)
        except Exception as e:
            system_logger.error(f"Error reading activity logs: {e}")
            return jsonify({"error": str(e)}), 500

    elif log_type == "system":
        log_file = os.path.join(logging_utils.LOG_DIR, "system.log")
        if not os.path.exists(log_file):
            return jsonify([])

        try:
            with open(log_file, "r") as f:
                lines = f.readlines()
                lines = lines[-limit:] if len(lines) > limit else lines
                return jsonify(lines)
        except Exception as e:
            system_logger.error(f"Error reading system logs: {e}")
            return jsonify({"error": str(e)}), 500

    return jsonify({"error": "Invalid log type"}), 400


# ---------------------------------------------------------------------------
# Routes — Settings API (password-protected)
# ---------------------------------------------------------------------------
def _get_password() -> str | None:
    """Extract settings password from header or JSON body."""
    # Header works for both GET and POST
    pwd = request.headers.get("X-Settings-Password", "")
    if pwd:
        return pwd
    # Fallback: JSON body (POST requests)
    if request.is_json:
        data = request.get_json(silent=True) or {}
        return data.get("password", "")
    return None


def _check_settings_password() -> bool:
    """Check if the provided password matches the configured one."""
    pwd = _get_password()
    if not pwd:
        return False
    config = cfg.load_config()
    return pwd == config.get("settings_password", "admin")


@app.route("/api/settings/auth", methods=["POST"])
def api_settings_auth():
    """Verify settings password."""
    if _check_settings_password():
        return jsonify({"success": True, "token": "authenticated"})
    return jsonify({"success": False, "message": "Invalid password"}), 401


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """Return current settings (requires auth)."""
    if not _check_settings_password():
        return jsonify({"success": False, "message": "Invalid password"}), 401

    config = cfg.load_config()
    return jsonify(
        {
            "restricted_hours": config.get("restricted_hours", {"start": 2, "end": 7}),
            "devices": config.get("devices", []),
        }
    )


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    """Save settings via form data (requires auth)."""
    if not _check_settings_password():
        return jsonify({"success": False, "message": "Invalid password"}), 401

    data = request.get_json(silent=True) or {}
    config = cfg.load_config()

    if "restricted_hours" in data:
        config["restricted_hours"] = data["restricted_hours"]

    if "devices" in data:
        config["devices"] = data["devices"]

    if "settings_password" in data:
        config["settings_password"] = data["settings_password"]

    cfg.save_config(config)
    logging_utils.log_activity("settings", "success", "Settings updated via form")
    return jsonify({"success": True})


@app.route("/api/settings/yaml", methods=["GET"])
def api_settings_yaml_get():
    """Return raw YAML config (requires auth)."""
    if not _check_settings_password():
        return jsonify({"success": False, "message": "Invalid password"}), 401

    raw = cfg.load_config_raw()
    return jsonify({"yaml": raw})


@app.route("/api/settings/yaml", methods=["POST"])
def api_settings_yaml_save():
    """Save raw YAML config (requires auth)."""
    if not _check_settings_password():
        return jsonify({"success": False, "message": "Invalid password"}), 401

    data = request.get_json(silent=True) or {}
    raw_yaml = data.get("yaml", "")
    success, error = cfg.save_config_raw(raw_yaml)

    if success:
        logging_utils.log_activity("settings", "success", "Settings updated via YAML")
        return jsonify({"success": True})
    else:
        return jsonify({"success": False, "message": error}), 400


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
system_logger.info("Application starting up")
logging_utils.log_activity("startup", "success", "Application initialized")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=6464, debug=False)
