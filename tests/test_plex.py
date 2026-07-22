"""
Tests for plex.py — Plex/Tautulli stream monitoring.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

import plex


class TestPlexMonitor:
    @patch("plex.urllib.request.urlopen")
    def test_returns_empty_when_not_configured(self, mock_urlopen):
        """When Tautulli is not configured, get_streams returns []."""
        mon = plex.PlexMonitor()
        dev = {"name": "Test", "tautulli_ip": "", "tautulli_apikey": ""}
        assert mon.get_streams(dev) == []

    @patch("plex.urllib.request.urlopen")
    def test_returns_empty_when_no_ip(self, mock_urlopen):
        mon = plex.PlexMonitor()
        dev = {"name": "Test", "tautulli_ip": "", "tautulli_apikey": "abc"}
        assert mon.get_streams(dev) == []

    @patch("plex.urllib.request.urlopen")
    def test_returns_empty_when_no_apikey(self, mock_urlopen):
        mon = plex.PlexMonitor()
        dev = {"name": "Test", "tautulli_ip": "1.1.1.1", "tautulli_apikey": ""}
        assert mon.get_streams(dev) == []

    @patch("plex.urllib.request.urlopen")
    def test_parses_streams_correctly(self, mock_urlopen):
        """Verify stream parsing from Tautulli response."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "response": {
                    "result": "success",
                    "data": {
                        "sessions": [
                            {
                                "media_type": "movie",
                                "title": "Inception",
                                "friendly_name": "John",
                                "video_full_resolution": "1080p",
                                "state": "playing",
                            },
                            {
                                "media_type": "episode",
                                "title": "Pilot",
                                "grandparent_title": "Breaking Bad",
                                "friendly_name": "Jane",
                                "video_full_resolution": "4k",
                                "state": "paused",
                            },
                        ]
                    },
                }
            }
        ).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_response

        mon = plex.PlexMonitor()
        dev = {"name": "Test", "tautulli_ip": "1.1.1.1", "tautulli_apikey": "abc"}

        streams = mon.get_streams(dev)
        assert len(streams) == 2

        movie = streams[0]
        assert movie["title"] == "Inception"
        assert movie["type"] == "movie"
        assert movie["user"] == "John"
        assert movie["quality"] == "1080p"

        episode = streams[1]
        assert "Breaking Bad" in episode["title"]
        assert "Pilot" in episode["title"]
        assert episode["type"] == "episode"
        assert episode["user"] == "Jane"
        assert episode["quality"] == "4k"

    @patch("plex.urllib.request.urlopen")
    def test_handles_api_failure_gracefully(self, mock_urlopen):
        """On API failure, should return empty list (or stale cache)."""
        mock_urlopen.side_effect = Exception("Connection refused")

        mon = plex.PlexMonitor()
        dev = {"name": "Test", "tautulli_ip": "1.1.1.1", "tautulli_apikey": "abc"}

        streams = mon.get_streams(dev)
        assert streams == []

    @patch("plex.urllib.request.urlopen")
    def test_caches_results(self, mock_urlopen):
        """Results should be cached for PLEX_CACHE_TTL seconds."""
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock = MagicMock()
            mock.read.return_value = json.dumps(
                {
                    "response": {
                        "result": "success",
                        "data": {"sessions": []},
                    }
                }
            ).encode()
            return mock

        mock_urlopen.side_effect = side_effect

        mon = plex.PlexMonitor()
        dev = {"name": "Test", "tautulli_ip": "1.1.1.1", "tautulli_apikey": "abc"}

        # First call should hit the API
        mon.get_streams(dev)
        assert call_count == 1

        # Second call within TTL should use cache
        mon.get_streams(dev)
        assert call_count == 1

        # After TTL expires, should call API again
        mon._cache["Test"]["fetched_at"] = time.time() - plex.PLEX_CACHE_TTL - 1
        mon.get_streams(dev)
        assert call_count == 2
