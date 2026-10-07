# Bazzite Parental Controls — Web UI

A lightweight Flask web UI for managing user account lock/unlock on Bazzite.

## Features

- Lists all human users on the system (UID >= 1000, discovered dynamically)
- Shows lock/unlock status for each user
- Lock button: runs `sudo usermod -L <user>` + `sudo loginctl terminate-user <user>`
- Unlock button: runs `sudo usermod -U <user>`
- Auto-refreshes every 10 seconds
- Error messages displayed in the UI

## Requirements

- Python 3
- Flask
- sudo access (the app runs `usermod` and `loginctl` via sudo)

## Setup

```bash
# Clone or navigate to this directory
cd .worktrees/webui

# Create venv and install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Running

### Quick start (recommended)

```bash
./run.sh
```

This creates a venv (if needed), installs dependencies, and starts the app.

### Manual

```bash
.venv/bin/python app.py
```

The app listens on `0.0.0.0:5000`.

## Usage

Open `http://<host>:5000` in a browser. You'll see a table of users with their
lock status and buttons to lock or unlock each account.

## Security Note

This is a prototype with **no authentication**. Anyone with network access can
lock/unlock accounts. Do not expose this on untrusted networks.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Main UI page |
| GET | `/api/users` | JSON list of users with lock status |
| POST | `/api/lock` | Lock a user (form field: `username`) |
| POST | `/api/unlock` | Unlock a user (form field: `username`) |

## Systemd Integration

Files live in `systemd/`:

| File | Purpose |
|------|---------|
| `parental-controls.service` | Runs the Flask app on boot (Restart=always, RestartSec=5, User=stan) |
| `parental-controls-enforce.service` | Oneshot that runs one bedtime-enforcement pass |
| `parental-controls-enforce.timer` | Runs the enforcement oneshot every minute |
| `enforce_bedtime_timer.py` | Script invoked by the enforce service |

### Install

```bash
# Copy the enforcement script next to app.py
cp enforce_bedtime_timer.py /var/home/stan/Documents/Projects/bazzite-parental-controls-2/master/

# Install units
sudo cp systemd/parental-controls.service /etc/systemd/system/
sudo cp systemd/parental-controls-enforce.service /etc/systemd/system/
sudo cp systemd/parental-controls-enforce.timer /etc/systemd/system/

# Enable
sudo systemctl daemon-reload
sudo systemctl enable --now parental-controls.service
sudo systemctl enable --now parental-controls-enforce.timer
```

Check status with:

```bash
systemctl status parental-controls.service
systemctl list-timers parental-controls-enforce.timer
```
