#!/usr/bin/env bash
# One-command installer for Bazzite Parental Controls (MVP).
# Idempotent: safe to re-run. Must run as root.
#
#   sudo ./install.sh [--install-dir DIR] [--user USER] [--dry-run]
#
# The app is staged into a system location (default /opt/parental-controls)
# so the deployed systemd units never reference the checkout path (e.g. a
# home directory). Re-running leaves runtime state (bedtime.json,
# budgets.json, process_prefs.json) untouched.
#
# Steps: stage app -> venv + deps (as the service user) -> render + install
# systemd units -> render + install sudoers files (visudo-validated) ->
# auth store dir -> enable + start service and enforcement timer -> verify.
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/parental-controls"
SERVICE_USER="${SUDO_USER:-}"
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --install-dir|--app-dir) INSTALL_DIR="$2"; shift 2 ;;
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
[ -f "$SOURCE_DIR/app.py" ] || { echo "No app.py in $SOURCE_DIR — run install.sh from the app checkout." >&2; exit 1; }

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

echo "== 1/7 stage app into $INSTALL_DIR =="
run mkdir -p "$INSTALL_DIR"
if [ "$SOURCE_DIR" != "$INSTALL_DIR" ]; then
    # Sync code only. Runtime state files are excluded so live state is never
    # overwritten and never deleted (--delete skips excluded paths).
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] rsync -a --delete (excluding .venv/.git/state) $SOURCE_DIR/ -> $INSTALL_DIR/"
    elif command -v rsync >/dev/null; then
        rsync -a --delete \
            --exclude=/.venv --exclude=__pycache__ --exclude='*.pyc' \
            --exclude=/.git \
            --exclude=/bedtime.json --exclude=/budgets.json \
            --exclude=/process_prefs.json --exclude=/auth.json \
            "$SOURCE_DIR/" "$INSTALL_DIR/"
    else
        # Minimal fallback without rsync: copy code, keep existing state.
        ( cd "$SOURCE_DIR" && tar --exclude='./.venv' --exclude='./.git' \
            --exclude='*/__pycache__' --exclude='*.pyc' \
            --exclude='./bedtime.json' --exclude='./budgets.json' \
            --exclude='./process_prefs.json' --exclude='./auth.json' \
            -cf - . ) | ( cd "$INSTALL_DIR" && tar -xf - )
    fi
    echo "Synced code from $SOURCE_DIR"
fi
# Seed runtime state from the checkout only when the install dir has none
# (first migration); after that the install dir is authoritative.
for f in bedtime.json budgets.json process_prefs.json; do
    if [ ! -f "$INSTALL_DIR/$f" ] && [ -f "$SOURCE_DIR/$f" ]; then
        run cp "$SOURCE_DIR/$f" "$INSTALL_DIR/$f"
        echo "Seeded $f from checkout"
    fi
done
run chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# Templates are installed from the checkout (canonical), falling back to the
# staged copy if the checkout's are somehow absent.
TEMPLATE_DIR="$INSTALL_DIR"
[ -f "$SOURCE_DIR/systemd/parental-controls.service" ] && TEMPLATE_DIR="$SOURCE_DIR"

echo "== 2/7 venv + dependencies (as $SERVICE_USER) =="
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    as_user python3 -m venv "$INSTALL_DIR/.venv"
fi
as_user "$INSTALL_DIR/.venv/bin/pip" install -r "$TEMPLATE_DIR/requirements.txt"

echo "== 3/7 systemd units =="
TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
for unit in parental-controls.service parental-controls-enforce.service parental-controls-enforce.timer; do
    [ -f "$TEMPLATE_DIR/systemd/$unit" ] || { echo "Missing $TEMPLATE_DIR/systemd/$unit" >&2; exit 1; }
    sed -e "s|@APP_DIR@|$INSTALL_DIR|g" -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
        "$TEMPLATE_DIR/systemd/$unit" > "$TMPD/$unit"
    echo "--- rendered $unit ---"
    grep -E "^(User|WorkingDirectory|ExecStart)=" "$TMPD/$unit" || true
    run cp "$TMPD/$unit" /etc/systemd/system/"$unit"
    run chmod 644 /etc/systemd/system/"$unit"
done

echo "== 4/7 sudoers (visudo-validated) =="
for f in sudoers-parental-controls sudoers-parental-controls-budgets; do
    [ -f "$TEMPLATE_DIR/$f" ] || { echo "Missing $TEMPLATE_DIR/$f" >&2; exit 1; }
    target="/etc/sudoers.d/${f#sudoers-}"
    sed -e "s|^stan |${SERVICE_USER} |" "$TEMPLATE_DIR/$f" > "$TMPD/$f"
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

echo "== 5/7 auth store dir =="
run mkdir -p /etc/parental-controls
run chown "$SERVICE_USER:$SERVICE_USER" /etc/parental-controls
run chmod 700 /etc/parental-controls

echo "== 6/7 enable + start =="
run systemctl daemon-reload
run systemctl enable --now parental-controls.service
run systemctl enable --now parental-controls-enforce.timer

if [ "$DRY_RUN" -eq 1 ]; then
    echo "Dry run complete — nothing was changed."
    exit 0
fi

echo "== 7/7 verify =="
sleep 3
systemctl is-active parental-controls.service
code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000/ || true)
echo "GET / -> $code (302 login redirect expected)"
[ "$code" = "302" ] || { echo "WARNING: unexpected status; check journalctl -u parental-controls.service" >&2; }

echo
echo "Installed into $INSTALL_DIR. Next steps:"
echo "  1. Get the one-time admin credential: journalctl -u parental-controls.service | grep 'AUTH BOOTSTRAP'"
echo "  2. From your machine: ssh -L 5000:localhost:5000 <user>@<host>, open http://localhost:5000"
echo "  3. Log in and change the password immediately (forced on first login)."
