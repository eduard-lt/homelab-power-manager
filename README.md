# Homelab Power Manager

A lightweight Flask web app for remotely managing devices on your local network
— wake them up via Wake-on-LAN, shut them down over SSH, schedule power events,
and monitor active Plex streams (via Tautulli).

Designed to run on a **Raspberry Pi 2nd gen** (or newer) and be used as an
iPhone/Android **PWA** from your home screen.

---

## Features

- **Multi-device support** — add as many devices as you want (servers, PCs, etc.)
- **Wake-on-LAN** — sends a magic packet to power on any WoL-capable device
- **SSH shutdown** — suspends/shuts down a device over SSH using your private key
- **Live shutdown timeline** — the dashboard shows each step as it happens
  (activity check, Minecraft save, disk sync, power off) and confirms when the
  device is actually off; wake-ups show progress until the device answers
- **Smart goodnight** — with the included [`server/goodnight`](server/goodnight)
  script the server refuses to shut down while someone plays Valheim or watches
  Plex, and tells you who; "Shut down anyway" (or running `goodnight` twice in
  10 minutes) overrides it and stops Plex streams with a goodnight message
- **Custom shutdown command** — per device (e.g. `suspend-now`, `poweroff`, `shutdown -h now`)
- **Auto-hiding buttons** — if no SSH key is configured for a device, the shutdown
  button is hidden automatically
- **Scheduled shutdown** — pick a delay (30 min–12h); if the server is in use when
  it fires, it tries again every 30 minutes (up to 3 hours)
- **Plex stream monitor** — shows active Plex streams per device via
  [Tautulli](https://tautulli.com/) (per-device configuration)
- **Restricted hours** — prevents wake-ups during a configurable time window
  (e.g. 2 AM – 7 AM)
- **Password-protected settings** — add/edit/delete devices, change restricted hours,
  change settings password — all from the web UI
- **Activity log** — wake/shutdown/schedule events, the server going on/offline
  by itself, with the full step timeline of each operation; filter by Power /
  Problems, grouped by day. Plus a searchable system log
- **Light on a Raspberry Pi 2** — device pings and Plex checks run on one
  background thread (fast only while the dashboard is open), config is cached,
  logs are read from the end of the file
- **Dark / light theme** — persisted per-browser via localStorage
- **PWA-ready** — add to your home screen for a native-app feel

---

## Requirements

- **Python 3.9+** (already on Raspberry Pi OS)
- **uv** — fast Python package manager (install once, see below)
- **SSH key** — an Ed25519 or RSA private key on the Pi for shutting down devices
- **Tautulli** (optional) — for Plex stream monitoring

---

## Quick Start

### 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Close and reopen your terminal, or run `source $HOME/.cargo/env`.

### 2. Clone the repository

```bash
git clone https://github.com/your-username/HomelabPowerMangeger.git
cd HomelabPowerMangeger
```

### 3. Set up the environment

```bash
uv venv
uv sync
```

### 4. Run

```bash
uv run python app.py
```

The server starts on **port 6464**. Open `http://<pi-ip>:5555` in any browser.

On first run, a `config.json` file is created with sensible defaults. If you
had a previous version installed, your old config will be migrated automatically.

---

## SSH Key Setup (required for shutdown)

To use the **Turn Off (SSH)** feature, you need to set up SSH key authentication
between the Pi (where the app runs) and each target device you want to shut down.
Here's the complete guide, from zero to working shutdown:

### How it works

Instead of passwords (which would hang waiting for input), the app uses **SSH key
authentication** — a pair of cryptographic files (`id_ed25519` and `id_ed25519.pub`)
that let you connect securely without typing anything.

1. **Generate** a key pair on the Pi
2. **Copy** the public key to the target device
3. **Point** the app at the private key in Settings
4. **Configure** the target so the shutdown command works without sudo

### Step 1: Generate an SSH key on the Pi

SSH into your Raspberry Pi (the one running the app) and run:

```bash
ssh-keygen -t ed25519 -C "homelab-power-manager"
```

You'll be prompted:
```
Enter file in which to save the key (/home/pi/.ssh/id_ed25519):
```
Press **Enter** to accept the default path.

```
Enter passphrase (empty for no passphrase):
```
Press **Enter** for no passphrase (simplest). If you do set a passphrase, you'll
need to enter it in the app's Settings later.

This creates two files:
- **Private key**: `/home/pi/.ssh/id_ed25519` — keep this **secret**, never share it
- **Public key**: `/home/pi/.ssh/id_ed25519.pub` — safe to share, goes on target devices

### Step 2: Copy the public key to the target device

This authorizes the Pi to SSH into the target. Run this **on the Pi**:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519 USERNAME@TARGET_IP
```

Replace `USERNAME` with your user on the target and `TARGET_IP` with its IP
(e.g. `pi@192.168.1.100`).

If `ssh-copy-id` isn't available, do it manually:

```bash
cat ~/.ssh/id_ed25519.pub | ssh USERNAME@TARGET_IP "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

**Test it works:**

```bash
ssh -i ~/.ssh/id_ed25519 USERNAME@TARGET_IP
```

You should log in without being asked for a password. Type `exit` to disconnect.

### Step 3: Make the shutdown command work without sudo

The target device needs to allow your user to run the shutdown command **without
typing a sudo password**. Otherwise SSH will hang waiting for input that never comes.

On the **target device**, run:

```bash
sudo visudo -f /etc/sudoers.d/suspend-nopasswd
```

Add this line (replace `USERNAME` with your user on the target):

```
USERNAME ALL=(ALL) NOPASSWD: /usr/bin/systemctl suspend
```

Save and exit. Test it from the Pi:

```bash
ssh USERNAME@TARGET_IP "sudo systemctl suspend"
```

If you prefer `shutdown -h now` or another command, add that to the sudoers file
instead. Or, if your system allows passwordless suspend via `loginctl`, use
`loginctl suspend` as the shutdown command — no sudo needed.

### Step 4: Fill in the app Settings

1. Open the app → **⚙ Settings** → enter password (`admin` by default)
2. Find or add your device, fill in:

| Field | Value |
|---|---|
| **SSH Key Path** | `/home/pi/.ssh/id_ed25519` |
| **SSH Port** | `22` (default) |
| **SSH Username** | Your username on the target |
| **SSH Key Passphrase** | Leave empty, or enter it if you set one in Step 1 |
| **Shutdown Command** | `sudo systemctl suspend` (if you set up sudoers) |

3. Click **Save All Settings**

Go back to the dashboard — the **Turn Off (SSH)** button should now appear on
that device's tab.

> **Repeat Steps 2–4 for each device** you want to shut down remotely.

---

## Configuration

All configuration is done via the **Settings** page in the web UI
(click the ⚙ tab, default password is `admin`).

### Adding a device

Each device has these fields:

| Field | Required | Description |
|---|---|---|
| **Device Name** | Yes | Display name (e.g. "Server", "Desktop PC") |
| **IP Address** | Yes | LAN IP for ping checks and SSH |
| **MAC Address** | Yes | MAC address for WoL (format: `AA:BB:CC:DD:EE:FF`) |
| **SSH Key Path** | No | Absolute path to private key *on the Pi* (e.g. `/home/pi/.ssh/id_ed25519`). Leave empty to disable shutdown for this device. |
| **SSH Port** | No | SSH port (default: 22) |
| **SSH Username** | No | SSH username on the target device |
| **SSH Key Passphrase** | No | Passphrase for the SSH key (if any) |
| **Shutdown Command** | No | Command to suspend/shutdown (default: `suspend-now`). Use the command you set up in the sudoers file, e.g. `sudo systemctl suspend`, or `goodnight`. Exit code `3` means "busy, not shutting down". See [SSH Key Setup](#ssh-key-setup-required-for-shutdown) above. |
| **Progress Command** | No | Prints shutdown progress for the live timeline, e.g. `goodnight --progress`. Output: `state<TAB>running\|idle`, then `epoch<TAB>level<TAB>message` lines. |
| **Tautulli IP** | No | IP of your Tautulli server |
| **Tautulli Port** | No | Tautulli port (default: 7979) |
| **Tautulli API Key** | No | Your Tautulli API key |

### Passwordless shutdown on the target device

See the full walkthrough in [SSH Key Setup](#ssh-key-setup-required-for-shutdown)
above. In short:

```bash
# On the target device:
sudo visudo -f /etc/sudoers.d/suspend-nopasswd
# Add: YOUR_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl suspend

# Then in the app Settings, set Shutdown Command to:
sudo systemctl suspend
```

---

## Smart goodnight (server side)

[`server/goodnight`](server/goodnight) is the shutdown script for the homelab
server itself. Install it on the server:

```bash
sudo install -m 755 server/goodnight /usr/local/bin/goodnight
```

| Command | What it does |
|---|---|
| `goodnight` | Checks Valheim players + Plex streams. Quiet → shuts down in the background. Busy → says who is active and exits `3` |
| `goodnight` again within 10 min | Ignores the activity; Plex viewers get a "see you tomorrow" message on screen, then 60s later the shutdown starts |
| `goodnight --force` | Skip the check (what the app's "Shut down anyway" runs) |
| `goodnight --cancel` | Close the 10-minute window (what the app's "Keep it on" runs) |
| `goodnight --check` | Only report activity |
| `goodnight --dry-run` | Check and show the steps, change nothing |
| `goodnight --status` | Is it running + last steps |
| `goodnight --progress` | Machine-readable progress (for the app's Progress Command) |

The Valheim check uses the image's own `valheim-is-idle`; if the Valheim
container is stopped or deleted the check is simply skipped. The Plex check
reads the Tautulli API key from the Tautulli container at run time. Stopping a
stream with a message needs Plex Pass.

---

## Deploying on a Raspberry Pi (systemd service)

### 1. Transfer files

```bash
git clone https://github.com/your-username/HomelabPowerMangeger.git
cd HomelabPowerMangeger
uv venv
uv sync
```

### 2. Copy your SSH key to the Pi

```bash
scp ~/.ssh/id_ed25519 pi@raspberrypi:/home/pi/.ssh/
```

### 3. Install the systemd service

Edit `device-controller.service` to match your paths and user:

```ini
[Service]
User=your-user
WorkingDirectory=/home/your-user/HomelabPowerMangeger
ExecStart=/home/your-user/HomelabPowerMangeger/.venv/bin/python /home/your-user/HomelabPowerMangeger/app.py
```

Then install it:

```bash
sudo cp device-controller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now device-controller
```

### 4. Verify

```bash
sudo systemctl status device-controller
journalctl -u device-controller -f   # follow logs
```

The app will now start automatically on boot and restart on failure.

---

## Using as an iPhone/Android PWA

1. Open `http://<pi-ip>:5555` in **Safari** (iOS) or **Chrome** (Android).
2. **iOS**: Tap **Share** → **Add to Home Screen**.
3. **Android**: Tap the menu → **Add to Home Screen**.

The app installs with its own icon and opens in full-screen mode.

---

## Running Tests

```bash
uv sync --group dev
uv run pytest -v
```

---

## Project Structure

```
HomelabPowerMangeger/
├── app.py                      # Flask app — routes, API, scheduling
├── config.py                   # Configuration management & migration
├── devices.py                  # Device operations (ping, WoL, SSH)
├── monitor.py                  # Background pinger + Plex poller (status snapshot)
├── operations.py               # Wake / shutdown runs with live step timelines
├── plex.py                     # Plex/Tautulli stream monitoring (cached)
├── logging_utils.py            # System log + events.jsonl (read newest-first)
├── config.json                 # Runtime config (auto-created, gitignored)
├── device-controller.service   # Systemd unit file
├── manifest.json               # PWA manifest
├── pyproject.toml              # Project metadata & deps (uv)
├── server/
│   └── goodnight               # Smart shutdown script for the server
├── static/
│   ├── app.css                 # Shared styles (mobile first, dark/light)
│   └── power-on-*.png          # PWA icons
├── templates/
│   ├── index.html              # Main dashboard (dynamic device tabs)
│   └── settings.html           # Password-protected settings page
├── tests/
│   ├── test_config.py          # Config unit tests
│   ├── test_devices.py         # Device operations unit tests
│   ├── test_plex.py            # Plex monitor unit tests
│   └── test_app.py             # Flask route integration tests
└── logs/                       # Created at runtime (gitignored)
    ├── events.jsonl            # Activity log (one JSON event per line)
    └── system.log              # Rotating system/error log
```

---

## FAQ

**Q: Can I change the port?**
A: Yes, edit the last line of `app.py`:
   `app.run(host="0.0.0.0", port=6464, debug=False)`
   Change `6464` to whatever you want, then restart the service.

**Q: Can I run this on something older than a Pi 2?**
A: The app is very lightweight (Flask + stdlib). It should run on any device
with Python 3.9+, including Pi Zero. Memory usage is typically under 30 MB.

**Q: The shutdown button doesn't appear for my device.**
A: Make sure you've set the **SSH Key Path** field in settings for that device.

**Q: How do I get a Tautulli API key?**
A: In Tautulli, go to **Settings** → **Web Interface** → **API** → copy the key.

**Q: I changed the settings password and forgot it.**
A: Edit `config.json` directly on the Pi and change the `settings_password` field.
Then restart the app: `sudo systemctl restart device-controller`.
