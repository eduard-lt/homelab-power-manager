"""
Plex/Tautulli integration module.
Fetches active Plex streams via the Tautulli API with caching.
"""

import json
import logging
import time
import urllib.request

logger = logging.getLogger(__name__)

PLEX_CACHE_TTL = 10  # seconds


class PlexMonitor:
    """
    Monitors active Plex streams via Tautulli's get_activity API.
    Caches results for PLEX_CACHE_TTL seconds to avoid excessive polling.
    """

    def __init__(self):
        self._cache: dict[str, dict] = {}  # device_name -> {streams, fetched_at}

    def get_streams(self, device: dict) -> list[dict]:
        """
        Return cached streams for a device, refreshing when TTL has expired.
        Returns an empty list if Tautulli is not configured.

        Each stream dict contains: title, type, user, quality, state
        """
        ip = device.get("tautulli_ip", "").strip()
        port = device.get("tautulli_port", 7979)
        apikey = device.get("tautulli_apikey", "").strip()
        name = device.get("name", "")

        if not ip or not apikey:
            return []

        cache_entry = self._cache.get(name)
        now = time.time()

        if cache_entry and (now - cache_entry["fetched_at"] < PLEX_CACHE_TTL):
            return cache_entry["streams"]

        result = self._fetch_streams(ip, port, apikey)
        if result is not None:
            self._cache[name] = {"streams": result, "fetched_at": now}
        elif cache_entry:
            # Fetch failed — keep stale cache
            return cache_entry["streams"]
        else:
            self._cache[name] = {"streams": [], "fetched_at": now}

        return result if result is not None else []

    @staticmethod
    def _fetch_streams(ip: str, port: int, apikey: str) -> list[dict] | None:
        """
        Query Tautulli get_activity endpoint.
        Returns list of stream dicts, or None on failure.
        """
        url = f"http://{ip}:{port}/api/v2?cmd=get_activity&apikey={apikey}"

        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            logger.warning(f"Tautulli API request failed: {e}")
            return None

        if data.get("response", {}).get("result") != "success":
            logger.warning(f"Tautulli returned non-success: {data}")
            return None

        sessions = data.get("response", {}).get("data", {}).get("sessions", [])
        streams = []
        for s in sessions:
            media_type = s.get("media_type", "unknown")
            title = s.get("title", "Unknown")
            grandparent_title = s.get("grandparent_title", "")
            user = s.get("friendly_name", "")
            resolution = s.get("video_full_resolution", "")
            state = s.get("state", "")

            if media_type == "episode" and grandparent_title:
                display_title = f"{grandparent_title} \u2014 {title}"
            else:
                display_title = title

            streams.append(
                {
                    "title": display_title,
                    "type": media_type,
                    "user": user,
                    "quality": resolution,
                    "state": state,
                }
            )

        return streams
