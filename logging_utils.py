"""
Logging utilities for Homelab Power Manager.
Sets up rotating system logs and CSV activity logs.
"""

import csv
import datetime
import logging
import os
from logging.handlers import RotatingFileHandler

# Absolute path to the project root (where app.py lives)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(_BASE_DIR, "logs")


def _ensure_log_dir() -> None:
    """Create the logs directory if it doesn't exist."""
    os.makedirs(LOG_DIR, exist_ok=True)


def setup_system_logger() -> logging.Logger:
    """Create and configure the rotating system log handler."""
    _ensure_log_dir()

    handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "system.log"),
        maxBytes=1024 * 1024 * 5,  # 5 MB
        backupCount=5,
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )

    logger = logging.getLogger("homelab_pm")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger


def log_activity(action: str, result: str, details: str = "") -> None:
    """Append an activity entry to the CSV activity log."""
    _ensure_log_dir()
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_file = os.path.join(LOG_DIR, "activity.csv")

    file_exists = os.path.exists(log_file)

    try:
        with open(log_file, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "Action", "Result", "Details"])
            writer.writerow([timestamp, action, result, details])
    except IOError as e:
        logging.getLogger("homelab_pm").error(f"Failed to write activity log: {e}")
