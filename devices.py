"""
Device management module.
Handles ping checks, Wake-on-LAN, and SSH shutdown for configured devices.
Uses system `ssh` command — no paramiko, no C compilation needed.
"""

import datetime
import logging
import os
import socket
import subprocess
import time

logger = logging.getLogger(__name__)


def ping(ip: str, timeout: float = 1.0) -> bool:
    """Return True if the given IP responds to a single ping."""
    if not ip:
        return False
    try:
        if os.name == "nt":
            return (
                subprocess.run(
                    ["ping", "-n", "1", "-w", str(int(timeout * 1000)), ip],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode
                == 0
            )
        else:
            return (
                subprocess.run(
                    ["ping", "-c", "1", "-W", str(int(timeout)), ip],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode
                == 0
            )
    except Exception:
        return False


def _broadcast_for_ip(ip: str) -> str:
    """Derive a broadcast address from an IP (e.g. 192.168.1.x -> 192.168.1.255)."""
    try:
        parts = ip.split(".")
        if len(parts) == 4:
            parts[3] = "255"
            return ".".join(parts)
    except Exception:
        pass
    return "255.255.255.255"


def send_wol(mac_address: str, broadcast_ip: str = "255.255.255.255", port: int = 9) -> None:
    """Send a Wake-on-LAN magic packet to the given MAC address."""
    mac_clean = mac_address.replace(":", "").replace("-", "").lower()
    if len(mac_clean) != 12:
        raise ValueError(f"Invalid MAC address: {mac_address}")

    mac_bytes = bytes.fromhex(mac_clean)
    packet = b"\xff" * 6 + (mac_bytes * 16)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        targets = {broadcast_ip, "255.255.255.255"}
        for target in targets:
            try:
                s.sendto(packet, (target, port))
            except Exception as e:
                logger.warning(f"WoL send error to {target}:{port}: {e}")


def wake_device(device: dict) -> bool:
    """
    Send WoL to a device and confirm it came online.
    Returns True if the device becomes reachable within ~10 seconds.
    """
    mac = device.get("mac", "")
    ip = device.get("ip", "")

    if not mac or not ip:
        logger.error("Cannot wake device: missing MAC or IP.")
        return False

    bcast = _broadcast_for_ip(ip)
    logger.info(f"Sending WoL to {mac} via {bcast}")
    send_wol(mac, bcast)

    time.sleep(2)

    for attempt in range(1, 6):
        if ping(ip):
            logger.info(f"Device {device.get('name')} is now online (attempt {attempt}).")
            return True
        logger.info(f"Waiting for {device.get('name')} (attempt {attempt}/5)")
        time.sleep(2)

    logger.warning(f"Device {device.get('name')} did not respond after WoL.")
    return False


def shutdown_device(device: dict) -> bool:
    """
    Shut down a device via the system `ssh` command.
    Returns True on success, False on failure.
    """
    ip = device.get("ip", "")
    port = device.get("ssh_port", 22)
    username = device.get("ssh_username", "")
    key_path = device.get("ssh_key_path", "").strip()
    passphrase = device.get("ssh_key_passphrase", "")
    command = device.get("shutdown_command", "suspend-now").strip()
    name = device.get("name", "unknown")

    if not ip or not key_path or not username:
        logger.error(f"SSH shutdown: missing IP, key path, or username for {name}.")
        return False

    if not command:
        command = "suspend-now"

    if not os.path.isfile(key_path):
        logger.error(f"SSH key not found at {key_path}")
        return False

    # Build the ssh command
    ssh_cmd = [
        "ssh",
        "-i", key_path,
        "-p", str(port),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=5",
        "-o", "BatchMode=yes",
    ]

    # Add passphrase via sshpass if needed (sshpass must be installed)
    if passphrase:
        ssh_cmd = [
            "sshpass", "-p", passphrase,
            *ssh_cmd,
        ]

    ssh_cmd.append(f"{username}@{ip}")
    ssh_cmd.append(command)

    logger.info(f"Running: {' '.join(ssh_cmd)}")

    try:
        result = subprocess.run(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            text=True,
        )

        if result.returncode != 0:
            err = result.stderr.strip() or "no output"
            logger.error(f"SSH shutdown failed for {name}: exit={result.returncode}, stderr={err}")
            return False

        out = result.stdout.strip()
        logger.info(f"SSH shutdown succeeded for {name}" + (f" ({out})" if out else ""))
        return True

    except subprocess.TimeoutExpired:
        # Timeout can be normal — the target might suspend before replying
        logger.info(f"SSH to {name} timed out (likely suspended — OK)")
        return True
    except FileNotFoundError:
        logger.error("ssh command not found on this system")
        return False
    except Exception as e:
        logger.error(f"SSH shutdown error for {name}: {e}")
        return False


def check_restricted_hours(restricted_cfg: dict, now: datetime.datetime | None = None) -> bool:
    """Return True if the current time falls within the restricted hours window."""
    if now is None:
        now = datetime.datetime.now()
    start = restricted_cfg.get("start", 2)
    end = restricted_cfg.get("end", 7)
    return start <= now.hour < end
