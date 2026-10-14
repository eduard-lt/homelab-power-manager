"""
Background device monitor.

Pings every device and fetches Plex streams on a single background thread, so
page loads and status polls never wait on the network: /api/status just reads
the latest snapshot.

To stay light on a Pi 2 it only polls quickly (every 5s) while someone has the
dashboard open or an operation is running, and drops to once a minute otherwise.
"""

import logging
import threading
import time

import config as cfg
import devices

logger = logging.getLogger("homelab_pm")

FAST_INTERVAL = 5     # seconds between checks while the dashboard is open
SLOW_INTERVAL = 60    # seconds between checks while nobody is looking
DEMAND_WINDOW = 30    # dashboard counts as "open" this long after its last poll


class Monitor:
    def __init__(self, plex_monitor, on_change=None, is_busy=lambda: False):
        self._plex = plex_monitor
        self._on_change = on_change    # (device_name, online: bool) on every transition
        self._is_busy = is_busy        # True while an operation needs fresh data
        self._state: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._last_demand = 0.0
        self._thread = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
            self._thread.start()

    @property
    def running(self) -> bool:
        return self._thread is not None

    def demand(self) -> None:
        """Called on every dashboard poll; switches to fast polling immediately."""
        idle = time.time() - self._last_demand > DEMAND_WINDOW
        self._last_demand = time.time()
        if idle:
            self._wake.set()

    def snapshot(self, name: str) -> dict:
        with self._lock:
            return dict(self._state.get(name) or {"online": None, "since": None, "streams": []})

    def set_online(self, name: str, online: bool) -> None:
        """Record a fresh observation (also used by operations that ping on their own)."""
        changed = False
        with self._lock:
            st = self._state.setdefault(name, {"online": None, "since": None, "streams": []})
            if st["online"] != online:
                changed = st["online"] is not None  # first observation is not a transition
                st["online"] = online
                st["since"] = time.time() if changed else None
            if not online:
                st["streams"] = []
            st["checked"] = time.time()
        if changed and self._on_change:
            self._on_change(name, online)

    # -----------------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            fast = time.time() - self._last_demand < DEMAND_WINDOW or self._is_busy()
            try:
                self._tick(fetch_streams=fast)
            except Exception:
                logger.exception("Monitor tick failed")
            self._wake.wait(FAST_INTERVAL if fast else SLOW_INTERVAL)
            self._wake.clear()

    def _tick(self, fetch_streams: bool) -> None:
        devs = cfg.load_config().get("devices", [])
        names = {d.get("name", "") for d in devs}
        with self._lock:
            for gone in set(self._state) - names:
                del self._state[gone]

        for d in devs:
            name = d.get("name", "")
            online = devices.ping(d.get("ip", ""))
            self.set_online(name, online)
            if online and fetch_streams and cfg.has_tautulli(d):
                streams = self._plex.get_streams(d)
                with self._lock:
                    if name in self._state:
                        self._state[name]["streams"] = streams
