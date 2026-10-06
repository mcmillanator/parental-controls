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
