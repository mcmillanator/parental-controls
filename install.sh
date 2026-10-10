#!/usr/bin/env bash
# One-command installer for Bazzite Parental Controls (MVP).
# Idempotent: safe to re-run. Must run as root.
#
#   sudo ./install.sh [--app-dir DIR] [--user USER] [--dry-run]
#
# Steps: venv + deps (as the service user) -> render + install systemd units
# -> render + install sudoers files (visudo-validated) -> auth store dir ->
# enable + start service and enforcement timer -> verify.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR"
SERVICE_USER="${SUDO_USER:-}"
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --app-dir) APP_DIR="$2"; shift 2 ;;
        --user) SERVICE_USER="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo ./install.sh" >&2
    exit 1
fi
if [ -z "$SERVICE_USER" ]; then
    echo "Could not determine service user (no --user and no SUDO_USER)." >&2
    exit 1
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "User '$SERVICE_USER' does not exist." >&2
    exit 1
fi
command -v python3 >/dev/null || { echo "python3 not found." >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemctl not found." >&2; exit 1; }
[ -f "$APP_DIR/app.py" ] || { echo "No app.py in $APP_DIR — wrong --app-dir?" >&2; exit 1; }

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] $*"
    else
        "$@"
    fi
}

as_user() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] sudo -u $SERVICE_USER $*"
    else
        sudo -u "$SERVICE_USER" "$@"
    fi
}

echo "== 1/6 venv + dependencies (as $SERVICE_USER) =="
if [ ! -d "$APP_DIR/.venv" ]; then
    as_user python3 -m venv "$APP_DIR/.venv"
fi
as_user "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "== 2/6 systemd units =="
TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
for unit in parental-controls.service parental-controls-enforce.service parental-controls-enforce.timer; do
    [ -f "$APP_DIR/systemd/$unit" ] || { echo "Missing $APP_DIR/systemd/$unit" >&2; exit 1; }
    sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
        "$APP_DIR/systemd/$unit" > "$TMPD/$unit"
    echo "--- rendered $unit ---"
    grep -E "^(User|WorkingDirectory|ExecStart)=" "$TMPD/$unit" || true
    run cp "$TMPD/$unit" /etc/systemd/system/"$unit"
    run chmod 644 /etc/systemd/system/"$unit"
done

echo "== 3/6 sudoers (visudo-validated) =="
for f in sudoers-parental-controls sudoers-parental-controls-budgets; do
    [ -f "$APP_DIR/$f" ] || { echo "Missing $APP_DIR/$f" >&2; exit 1; }
    target="/etc/sudoers.d/${f#sudoers-}"
    sed -e "s|^stan |${SERVICE_USER} |" "$APP_DIR/$f" > "$TMPD/$f"
    run cp "$TMPD/$f" "$target"
    run chmod 440 "$target"
done
if [ "$DRY_RUN" -eq 1 ]; then
    echo "validating rendered copies:"
    visudo -c -f "$TMPD/sudoers-parental-controls"
    visudo -c -f "$TMPD/sudoers-parental-controls-budgets"
else
    visudo -c
fi

echo "== 4/6 auth store dir =="
run mkdir -p /etc/parental-controls
run chown "$SERVICE_USER:$SERVICE_USER" /etc/parental-controls
run chmod 700 /etc/parental-controls

echo "== 5/6 enable + start =="
run systemctl daemon-reload
run systemctl enable --now parental-controls.service
run systemctl enable --now parental-controls-enforce.timer

if [ "$DRY_RUN" -eq 1 ]; then
    echo "Dry run complete — nothing was changed."
    exit 0
fi

echo "== 6/6 verify =="
sleep 3
systemctl is-active parental-controls.service
code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000/ || true)
echo "GET / -> $code (302 login redirect expected)"
[ "$code" = "302" ] || { echo "WARNING: unexpected status; check journalctl -u parental-controls.service" >&2; }

echo
echo "Installed. Next steps:"
echo "  1. Get the one-time admin credential: journalctl -u parental-controls.service | grep 'AUTH BOOTSTRAP'"
echo "  2. From your machine: ssh -L 5000:localhost:5000 <user>@<host>, open http://localhost:5000"
echo "  3. Log in and change the password immediately (forced on first login)."
