"""
Homelab Power Manager — Flask web application.
Provides remote Wake-on-LAN, SSH shutdown with a live step timeline, scheduled
shutdown, Plex/Tautulli stream monitoring, an event log and a password-protected
settings page.
"""

import datetime
import logging
import threading
import time

from flask import Flask, jsonify, render_template, request, send_from_directory

import config as cfg
import devices
import logging_utils
import monitor as monitor_mod
import operations
import plex

SCHEDULE_RETRY_MINUTES = 30   # a scheduled shutdown blocked by activity tries again
SCHEDULE_MAX_RETRIES = 6      # ...up to 3 hours later, then gives up

# ---------------------------------------------------------------------------
# App & logging setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 7 * 24 * 3600  # icons/css rarely change
system_logger = logging_utils.setup_system_logger()

# The dev server logs every request; on a Pi that is CPU and SD-card wear for nothing.
logging.getLogger("werkzeug").setLevel(logging.ERROR)


def _on_state_change(name: str, online: bool) -> None:
    if ops.is_running(name):
        return  # the wake/shutdown operation logs this itself, with its timeline
    msg = f"{name} came online" if online else f"{name} went offline"
    system_logger.info(msg)
    logging_utils.log_event("state", "online" if online else "offline", msg, device=name)


def _on_op_finish(name: str, op: dict) -> None:
    """Scheduled shutdowns that were blocked by activity try again later."""
    entry = _schedules.get(name)
    if not entry or op.get("source") != "schedule" or not entry.get("firing"):
        return
    if op["state"] == "blocked" and entry["retries"] < SCHEDULE_MAX_RETRIES:
        entry["retries"] += 1
        entry["firing"] = False
        _arm_timer(name, time.time() + SCHEDULE_RETRY_MINUTES * 60)
        at = datetime.datetime.fromtimestamp(entry["at"]).strftime("%H:%M")
        logging_utils.log_event(
            "schedule", "retry", f"{name} is in use - will try again at {at}", device=name
        )
    else:
        _schedules.pop(name, None)


plex_monitor = plex.PlexMonitor()
monitor = monitor_mod.Monitor(
    plex_monitor, on_change=_on_state_change, is_busy=lambda: ops.any_running()
)
ops = operations.Operations(monitor, on_finish=_on_op_finish)

# Scheduled shutdowns: device name -> {"at": epoch, "timer": Timer, "retries": n, "firing": bool}
_schedules: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Routes — UI
# ---------------------------------------------------------------------------
@app.route("/")
def index():
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
    return jsonify([
        {
            "name": d.get("name", ""),
            "has_ssh": cfg.has_ssh(d),
            "has_tautulli": cfg.has_tautulli(d),
        }
        for d in config.get("devices", [])
    ])


@app.route("/api/status", methods=["GET"])
def api_status():
    """
    Everything the dashboard shows, from the monitor's snapshot (no network I/O
    here, so it answers instantly even when a device is offline).
    """
    monitor.demand()
    config = cfg.load_config()
    restricted = config.get("restricted_hours", {"start": 2, "end": 7})

    statuses = []
    for d in config.get("devices", []):
        name = d.get("name", "")
        snap = monitor.snapshot(name)
        if not monitor.running:
            # No background thread (tests, or first start): ping inline once
            monitor.set_online(name, devices.ping(d.get("ip", "")))
            snap = monitor.snapshot(name)
        sched = _schedules.get(name)
        statuses.append({
            "name": name,
            "online": snap["online"],
            "since": snap["since"],
            "has_ssh": cfg.has_ssh(d),
            "has_tautulli": cfg.has_tautulli(d),
            "streams": snap["streams"],
            "op": ops.current(name),
            "scheduled": {"at": sched["at"], "retries": sched["retries"]} if sched else None,
        })

    return jsonify({
        "devices": statuses,
        "restricted_hours": devices.check_restricted_hours(restricted),
        "restricted": {"start": restricted.get("start", 2), "end": restricted.get("end", 7)},
        "now": time.time(),
    })


@app.route("/api/control", methods=["POST"])
def api_control():
    """Device actions: turn_on, turn_off, schedule_off, cancel_schedule, dismiss."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "message": "No data provided"}), 400

    action = data.get("action")
    device_name = data.get("device")
    client_ip = request.remote_addr or ""

    system_logger.info(f"Control action '{action}' for '{device_name}' from {client_ip}")

    config = cfg.load_config()
    device = cfg.get_device_by_name(config, device_name)
    if not device:
        return jsonify({"success": False, "message": f"Device '{device_name}' not found"}), 404

    if action == "turn_on":
        restricted = config.get("restricted_hours", {"start": 2, "end": 7})
        if devices.check_restricted_hours(restricted):
            window = f"{restricted.get('start', 2):02d}:00–{restricted.get('end', 7):02d}:00"
            msg = f"Wake-ups are blocked during quiet hours ({window})"
            system_logger.warning(f"Turn on denied for '{device_name}' during restricted hours")
            logging_utils.log_event("wake", "denied", msg, device=device_name, ip=client_ip)
            return jsonify({"success": False, "message": msg})
        op = ops.start_wake(device, client_ip)
        if op is None:
            return jsonify({"success": False, "message": "Already working on it"}), 409
        _cancel_schedule(device_name, quiet=True)
        return jsonify({"success": True, "op": op})

    if action in ("turn_off", "schedule_off") and not cfg.has_ssh(device):
        return jsonify({"success": False, "message": "SSH not configured for this device"}), 400

    if action == "turn_off":
        # "Shut down anyway" only forces while goodnight's override window is open;
        # after that it is a fresh check, so nobody gets cut off on stale info.
        force = bool(data.get("force")) and ops.force_allowed(device_name)
        op = ops.start_shutdown(device, client_ip, force=force)
        if op is None:
            return jsonify({"success": False, "message": "A shutdown is already running"}), 409
        _cancel_schedule(device_name, quiet=True)
        return jsonify({"success": True, "op": op})

    if action == "schedule_off":
        try:
            hours = float(data.get("hours", 1))
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "Invalid delay"}), 400
        if not 0 < hours <= 24:
            return jsonify({"success": False, "message": "Delay must be between 0 and 24 hours"}), 400
        at = schedule_device_off(device_name, hours, client_ip)
        return jsonify({
            "success": True,
            "scheduled_at": at,
            "scheduled_time": datetime.datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M:%S"),
        })

    if action == "cancel_schedule":
        cancelled = _cancel_schedule(device_name, client_ip=client_ip)
        return jsonify({"success": cancelled})

    if action == "dismiss":
        op = ops.dismiss(device_name)
        if op and op["state"] == "blocked" and time.time() < op.get("force_until", 0):
            ops.cancel_override(device)
            logging_utils.log_event(
                "shutdown", "cancelled", f"Kept {device_name} on", device=device_name, ip=client_ip
            )
        return jsonify({"success": True})

    return jsonify({"success": False, "message": f"Invalid action: {action}"}), 400


# ---------------------------------------------------------------------------
# Scheduled shutdown
# ---------------------------------------------------------------------------
def _arm_timer(device_name: str, at: float) -> None:
    entry = _schedules[device_name]

    def fire():
        device = cfg.get_device_by_name(cfg.load_config(), device_name)
        if not device or _schedules.get(device_name) is not entry:
            return
        entry["firing"] = True
        system_logger.info(f"Executing scheduled shutdown for '{device_name}'")
        if ops.start_shutdown(device, source="schedule") is None:
            entry["firing"] = False
            _schedules.pop(device_name, None)

    timer = threading.Timer(max(0.0, at - time.time()), fire)
    timer.daemon = True
    entry.update(at=at, timer=timer)
    timer.start()


def schedule_device_off(device_name: str, hours: float, client_ip: str = "") -> float:
    """Schedule a shutdown after the given number of hours. Returns the epoch time."""
    _cancel_schedule(device_name, quiet=True)
    at = time.time() + hours * 3600
    _schedules[device_name] = {"retries": 0, "firing": False}
    _arm_timer(device_name, at)

    when = datetime.datetime.fromtimestamp(at).strftime("%H:%M")
    system_logger.info(f"Scheduled shutdown for '{device_name}' in {hours}h ({when})")
    logging_utils.log_event(
        "schedule", "success", f"{device_name} will shut down at {when}",
        device=device_name, ip=client_ip,
    )
    return at


def _cancel_schedule(device_name: str, quiet: bool = False, client_ip: str = "") -> bool:
    entry = _schedules.pop(device_name, None)
    if not entry:
        return False
    if entry.get("timer"):
        entry["timer"].cancel()
    system_logger.info(f"Cancelled scheduled shutdown for '{device_name}'")
    if not quiet:
        logging_utils.log_event(
            "schedule", "cancelled", f"Scheduled shutdown of {device_name} cancelled",
            device=device_name, ip=client_ip,
        )
    return True


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
def _int_arg(name: str, default: int, lo: int = 1, hi: int = 500) -> int:
    try:
        return max(lo, min(hi, int(request.args.get(name, default))))
    except ValueError:
        return default


@app.route("/api/events", methods=["GET"])
def api_events():
    """Newest-first events. Filters: category=all|power|problems, device, before=<ts>."""
    before = request.args.get("before")
    try:
        before = float(before) if before else None
    except ValueError:
        before = None
    return jsonify(logging_utils.read_events(
        limit=_int_arg("limit", 40),
        before=before,
        category=request.args.get("category", "all"),
        device=request.args.get("device", ""),
    ))


@app.route("/api/logs", methods=["GET"])
def api_logs():
    """Logs: type=events (default, alias activity) or type=system."""
    log_type = request.args.get("type", "events")
    limit = _int_arg("limit", 100)

    if log_type in ("events", "activity"):
        return jsonify(logging_utils.read_events(limit=limit)["events"])

    if log_type == "system":
        return jsonify(logging_utils.read_system_log(
            limit=limit,
            level=request.args.get("level", ""),
            query=request.args.get("q", ""),
            skip=_int_arg("skip", 0, lo=0, hi=100000),
        ))

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
    logging_utils.log_event("settings", "success", "Settings saved", ip=request.remote_addr or "")
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
        logging_utils.log_event("settings", "success", "Settings saved (YAML editor)",
                                ip=request.remote_addr or "")
        return jsonify({"success": True})
    else:
        return jsonify({"success": False, "message": error}), 400


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
migrated = logging_utils.migrate_activity_csv()
if migrated:
    system_logger.info(f"Migrated {migrated} rows from activity.csv to events.jsonl")
system_logger.info("Application starting up")

if __name__ == "__main__":
    logging_utils.log_event("app", "success", "Power manager started")
    monitor.start()
    app.run(host="0.0.0.0", port=6464, debug=False, threaded=True)
