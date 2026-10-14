"""
Power operations (wake / shutdown) with a live, step-by-step timeline.

Each operation runs on its own thread and records steps as it goes; the
dashboard polls /api/status and shows them, so you can watch a goodnight happen
instead of guessing. Finished operations are written to the event log together
with their steps.

Shutdown protocol with the server's `goodnight` script:
  * exit code 0  -> shutdown started (or the command just worked)
  * exit code 3  -> refused because of activity; running the same command again
                    within 10 minutes overrides it ("double goodnight")
  * status_command (e.g. `goodnight --progress`) prints "state<TAB>running|idle"
    followed by "epoch<TAB>level<TAB>message" lines, which become timeline steps.
"""

import itertools
import logging
import threading
import time

import devices
import logging_utils

logger = logging.getLogger("homelab_pm")

BLOCKED_EXIT_CODE = 3
OVERRIDE_WINDOW = 600       # goodnight's window: "Shut down anyway" is offered this long
WAKE_TIMEOUT = 180          # seconds to wait for a woken device to answer pings
WAKE_RESEND_AT = (20, 60)   # resend the magic packet if still silent at these marks
OFF_TIMEOUT = 15 * 60       # Plex grace + Minecraft save + sync can take a while
PROGRESS_POLL = 3           # seconds between status_command polls
FINISHED_LEVELS = {"done", "error"}

_ids = itertools.count(1)


class Operations:
    def __init__(self, monitor, on_finish=None):
        self._monitor = monitor
        self._on_finish = on_finish  # (device_name, op) after an op ends
        self._ops: dict[str, dict] = {}
        self._lock = threading.Lock()

    # -----------------------------------------------------------------------
    # Queries
    # -----------------------------------------------------------------------
    def current(self, name: str) -> dict | None:
        with self._lock:
            op = self._ops.get(name)
            return _copy(op) if op else None

    def is_running(self, name: str) -> bool:
        with self._lock:
            op = self._ops.get(name)
            return bool(op and op["state"] == "running")

    def any_running(self) -> bool:
        with self._lock:
            return any(op["state"] == "running" for op in self._ops.values())

    def dismiss(self, name: str) -> dict | None:
        """Forget a finished op. Returns it, so the caller can tell a 'keep it on'."""
        with self._lock:
            op = self._ops.get(name)
            if op and op["state"] != "running":
                return self._ops.pop(name)
        return None

    def force_allowed(self, name: str) -> bool:
        """True while a blocked shutdown's override window is still open."""
        with self._lock:
            op = self._ops.get(name)
            return bool(op and op["state"] == "blocked" and time.time() < op.get("force_until", 0))

    # -----------------------------------------------------------------------
    # Starters - return the new op, or None if one is already running
    # -----------------------------------------------------------------------
    def start_wake(self, device: dict, client_ip: str = "") -> dict | None:
        return self._start(device, "wake", f"Waking {device.get('name')}", self._run_wake, client_ip)

    def start_shutdown(self, device: dict, client_ip: str = "", source: str = "manual",
                       force: bool = False) -> dict | None:
        title = f"Shutting down {device.get('name')}"
        if source == "schedule":
            title = f"Scheduled shutdown of {device.get('name')}"
        if force:
            title += " (forced)"
            device = {**device, "shutdown_command": _with_flag(device, "--force")}
        return self._start(device, "shutdown", title, self._run_shutdown, client_ip, source)

    def cancel_override(self, device: dict) -> None:
        """'Keep it on': close goodnight's override window on the server (in the background)."""
        def run():
            rc, out, err = devices.run_ssh(device, _with_flag(device, "--cancel"), timeout=15)
            logger.info(f"Override cancel on {device.get('name')}: exit={rc} {(out or err).strip()}")
        threading.Thread(target=run, name="cancel-override", daemon=True).start()

    def _start(self, device, kind, title, runner, client_ip, source="manual"):
        name = device.get("name", "")
        with self._lock:
            existing = self._ops.get(name)
            if existing and existing["state"] == "running":
                return None
            op = {
                "id": next(_ids),
                "kind": kind,
                "title": title,
                "source": source,
                "state": "running",
                "started": time.time(),
                "ended": None,
                "steps": [],
                "ip": client_ip,
            }
            self._ops[name] = op
        threading.Thread(
            target=self._guard, args=(runner, device, op), name=f"op-{kind}", daemon=True
        ).start()
        return _copy(op)

    def _guard(self, runner, device, op):
        try:
            runner(device, op)
        except Exception as e:
            logger.exception("Operation crashed")
            self._finish(device, op, "failed", f"Unexpected error: {e}")

    # -----------------------------------------------------------------------
    # Step bookkeeping
    # -----------------------------------------------------------------------
    def _step(self, op, level, msg, t=None):
        with self._lock:
            op["steps"].append({"t": round(t or time.time(), 1), "level": level, "msg": msg})

    def _finish(self, device, op, state, msg, level=None):
        name = device.get("name", "")
        self._step(op, level or {"success": "ok", "blocked": "busy"}.get(state, "error"), msg)
        with self._lock:
            op["state"] = state
            op["ended"] = time.time()
            steps = [dict(s) for s in op["steps"]]
        duration = round(op["ended"] - op["started"])
        logger.info(f"{op['title']}: {state} - {msg}")
        logging_utils.log_event(
            op["kind"], state, msg, device=name, ip=op["ip"], duration=duration,
            steps=steps, source=op["source"] if op["source"] != "manual" else None,
        )
        if self._on_finish:
            self._on_finish(name, _copy(op))

    # -----------------------------------------------------------------------
    # Wake
    # -----------------------------------------------------------------------
    def _run_wake(self, device, op):
        name, ip, mac = device.get("name"), device.get("ip", ""), device.get("mac", "")
        if not mac or not ip:
            return self._finish(device, op, "failed", "Missing MAC or IP address in settings")

        bcast = devices._broadcast_for_ip(ip)
        try:
            devices.send_wol(mac, bcast)
        except ValueError as e:
            return self._finish(device, op, "failed", str(e))
        self._step(op, "ok", f"Magic packet sent to {mac}")
        self._step(op, "wait", f"Waiting for {name} to boot and answer")

        start = time.time()
        resends = list(WAKE_RESEND_AT)
        next_note = 30
        while True:
            elapsed = time.time() - start
            if devices.ping(ip):
                self._monitor.set_online(name, True)
                return self._finish(device, op, "success", f"{name} is online (took {_fmt(elapsed)})")
            if elapsed >= WAKE_TIMEOUT:
                return self._finish(
                    device, op, "failed",
                    f"No answer after {_fmt(WAKE_TIMEOUT)}. Check the server has power and Wake-on-LAN is on.",
                )
            if resends and elapsed >= resends[0]:
                resends.pop(0)
                devices.send_wol(mac, bcast)
                self._step(op, "info", "Still quiet - sent the magic packet again")
            elif elapsed >= next_note:
                self._step(op, "info", f"Still booting... {_fmt(elapsed)}")
                next_note += 30
            time.sleep(2)

    # -----------------------------------------------------------------------
    # Shutdown
    # -----------------------------------------------------------------------
    def _run_shutdown(self, device, op):
        name = device.get("name")
        command = device.get("shutdown_command", "").strip() or "suspend-now"
        status_cmd = device.get("status_command", "").strip()
        seen: set = set()

        self._step(op, "step", f"Connecting to {name} over SSH")
        rc, out, err = devices.run_ssh(device, command, timeout=60)

        if rc == -2:
            return self._finish(device, op, "failed", f"Could not run SSH: {err.strip()}")
        if rc == -1:
            # Some suspend commands never answer; the ping check below decides.
            self._step(op, "warn", "No reply from the shutdown command (normal for suspend)")
        else:
            state = None
            if status_cmd:
                state, _ = self._pull_progress(device, op, status_cmd, seen)
            if state is None:
                # No usable progress (none configured, or e.g. an older goodnight
                # without --progress): show what the command printed instead.
                for line in out.splitlines():
                    line = line.strip()
                    if line.startswith("goodnight: "):
                        line = line[len("goodnight: "):]
                    if line:
                        self._step(op, "info", line)
                if status_cmd:
                    self._step(op, "warn", "Progress command failed - live steps unavailable")
                    status_cmd = ""

        if rc == BLOCKED_EXIT_CODE:
            with self._lock:
                op["can_force"] = True
                op["force_until"] = time.time() + OVERRIDE_WINDOW
            return self._finish(device, op, "blocked", f"{name} is in use, so it stays on")
        if rc not in (0, -1):
            detail = (err or out).strip().splitlines()
            detail = detail[-1] if detail else "no output"
            return self._finish(device, op, "failed", f"Shutdown command failed (exit {rc}): {detail}")

        if rc == 0 and not seen:
            self._step(op, "ok", "Shutdown command accepted")

        # Follow the server's own progress until it stops answering
        if status_cmd and rc == 0:
            idle_polls = 0
            while True:
                time.sleep(PROGRESS_POLL)
                state, last_level = self._pull_progress(device, op, status_cmd, seen)
                if state is None or last_level in FINISHED_LEVELS:
                    break  # ssh gone (server powering off) or script finished
                idle_polls = idle_polls + 1 if state == "idle" else 0
                if idle_polls >= 3:
                    break
                if time.time() - op["started"] > OFF_TIMEOUT:
                    break
            if last_level == "error":
                return self._finish(device, op, "failed", "The server's shutdown script reported an error")

        self._step(op, "wait", f"Waiting for {name} to power off")
        offline_count = 0
        while time.time() - op["started"] < OFF_TIMEOUT:
            if devices.ping(device.get("ip", "")):
                offline_count = 0
            else:
                offline_count += 1
                if offline_count >= 2:
                    self._monitor.set_online(name, False)
                    took = _fmt(time.time() - op["started"])
                    return self._finish(device, op, "success", f"{name} is off. Good night! (took {took})")
            time.sleep(5)
        self._finish(device, op, "failed", f"{name} is still online after {_fmt(OFF_TIMEOUT)}")

    def _pull_progress(self, device, op, status_cmd, seen):
        """Run status_command once, add unseen lines as steps. Returns (state, last level)."""
        rc, out, _ = devices.run_ssh(device, status_cmd, timeout=10)
        if rc != 0:
            return None, None
        state, last_level = None, None
        for line in out.splitlines():
            parts = line.split("\t")
            if parts[0] == "state" and len(parts) == 2:
                state = parts[1].strip()
                continue
            if len(parts) != 3:
                continue
            t, level, msg = parts
            if level == "hint":
                continue  # advice for terminal users ("run goodnight again..."), not for the app
            last_level = level
            if (t, msg) in seen:
                continue
            seen.add((t, msg))
            try:
                ts = float(t)
            except ValueError:
                ts = None
            self._step(op, level, msg.strip(), ts)
        return state, last_level


def _with_flag(device: dict, flag: str) -> str:
    """The shutdown command with a goodnight flag (--force / --cancel) appended."""
    return f"{(device.get('shutdown_command') or '').strip()} {flag}"


def _copy(op: dict) -> dict:
    c = dict(op)
    c["steps"] = [dict(s) for s in op["steps"]]
    c.pop("ip", None)
    return c


def _fmt(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"
