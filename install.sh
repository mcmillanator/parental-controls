#!/usr/bin/env bash
# One-command installer for Bazzite Parental Controls (MVP).
# Idempotent: safe to re-run. Must run as root.
#
#   sudo ./install.sh [--install-dir DIR] [--user USER] [--bind HOST]
#                     [--port PORT] [--open-firewall] [--dry-run]
#
# Defaults keep the panel on localhost only (127.0.0.1:5000, no firewall
# change). --bind/--port render into the service unit; --open-firewall opens
# the chosen port in the default firewalld zone.
#
# The app is staged into a system location (default /opt/parental-controls)
# so the deployed systemd units never reference the checkout path (e.g. a
# home directory). Re-running leaves runtime state (bedtime.json,
# budgets.json, process_prefs.json) untouched.
#
# Steps: stage app -> venv + deps (as the service user) -> render + install
# systemd units -> render + install sudoers files (visudo-validated) ->
# auth store dir -> enable + start service, enforcement timer and health-check
# timer -> verify (including one live health probe).
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/parental-controls"
SERVICE_USER="${SUDO_USER:-}"
BIND_HOST="127.0.0.1"
BIND_PORT="5000"
OPEN_FIREWALL=0
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --install-dir|--app-dir) INSTALL_DIR="$2"; shift 2 ;;
        --user) SERVICE_USER="$2"; shift 2 ;;
        --bind) BIND_HOST="$2"; shift 2 ;;
        --port) BIND_PORT="$2"; shift 2 ;;
        --open-firewall) OPEN_FIREWALL=1; shift ;;
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
case "$BIND_PORT" in
    ''|*[!0-9]*) echo "Invalid --port: '$BIND_PORT' (want 1-65535)." >&2; exit 2 ;;
esac
[ "$BIND_PORT" -ge 1 ] && [ "$BIND_PORT" -le 65535 ] || \
    { echo "Invalid --port: '$BIND_PORT' (want 1-65535)." >&2; exit 2; }
case "$BIND_HOST" in
    ''|*[!A-Za-z0-9.:-]*) echo "Invalid --bind: '$BIND_HOST'." >&2; exit 2 ;;
esac
if [ "$BIND_HOST" != "127.0.0.1" ] && [ "$BIND_HOST" != "localhost" ] \
   && [ "$BIND_HOST" != "::1" ]; then
    echo "WARNING: --bind $BIND_HOST exposes the panel beyond this machine" >&2
    echo "without TLS; session cookies travel in the clear (MVP has no TLS)." >&2
fi
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

echo "== 1/8 stage app into $INSTALL_DIR =="
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

echo "== 2/8 venv + dependencies (as $SERVICE_USER) =="
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    as_user python3 -m venv "$INSTALL_DIR/.venv"
fi
as_user "$INSTALL_DIR/.venv/bin/pip" install -r "$TEMPLATE_DIR/requirements.txt"

echo "== 3/8 systemd units (bind $BIND_HOST:$BIND_PORT) =="
TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
# Track whether any unit actually changed: a changed unit needs a service
# restart to take effect (`enable --now` never restarts a running service,
# which silently left a new --bind unlistened). Unchanged re-installs leave
# the running service alone.
UNITS_CHANGED=0
for unit in parental-controls.service parental-controls-enforce.service parental-controls-enforce.timer \
            parental-controls-healthcheck.service parental-controls-healthcheck.timer; do
    [ -f "$TEMPLATE_DIR/systemd/$unit" ] || { echo "Missing $TEMPLATE_DIR/systemd/$unit" >&2; exit 1; }
    sed -e "s|@APP_DIR@|$INSTALL_DIR|g" -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
        -e "s|@BIND_HOST@|$BIND_HOST|g" -e "s|@BIND_PORT@|$BIND_PORT|g" \
        "$TEMPLATE_DIR/systemd/$unit" > "$TMPD/$unit"
    if [ ! -f /etc/systemd/system/"$unit" ] \
       || ! cmp -s "$TMPD/$unit" /etc/systemd/system/"$unit"; then
        UNITS_CHANGED=1
    fi
    echo "--- rendered $unit ---"
    grep -E "^(User|WorkingDirectory|ExecStart)=" "$TMPD/$unit" || true
    run cp "$TMPD/$unit" /etc/systemd/system/"$unit"
    run chmod 644 /etc/systemd/system/"$unit"
done

echo "== 4/8 sudoers (visudo-validated) =="
for f in sudoers-parental-controls sudoers-parental-controls-budgets; do
    [ -f "$TEMPLATE_DIR/$f" ] || { echo "Missing $TEMPLATE_DIR/$f" >&2; exit 1; }
    target="/etc/sudoers.d/${f#sudoers-}"
    # Templates use @SERVICE_USER@; also accept a legacy leading "stan " so
    # hand-written files keep working.
    sed -e "s|@SERVICE_USER@|${SERVICE_USER}|g" -e "s|^stan |${SERVICE_USER} |" \
        "$TEMPLATE_DIR/$f" > "$TMPD/$f"
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

echo "== 5/8 auth store dir =="
run mkdir -p /etc/parental-controls
run chown "$SERVICE_USER:$SERVICE_USER" /etc/parental-controls
run chmod 700 /etc/parental-controls

# Recovery CLI. The panel locks a parent out of the web UI the moment they
# lose their app password, so the documented escape hatch must exist on a
# machine even without the checkout. It is root-only (it unlocks accounts,
# thaws cgroups and rewrites the auth store) and always runs the interpreter
# from the install dir's own venv, so it fails closed with the app's own
# missing-hashing-backend check rather than a bare ImportError.
echo "== 5b/8 recovery CLI =="
CLI_WRAPPER="$TMPD/parental-controls"
cat >"$CLI_WRAPPER" <<WRAPPER
#!/usr/bin/env bash
# Generated by install.sh -- recovery CLI for the parental-controls panel.
# Root-only: unlocks accounts, thaws cgroups, rotates admin passwords.
set -euo pipefail
exec ${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/cli.py "\$@"
WRAPPER
if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] install -m 0750 -o root -g root $CLI_WRAPPER /usr/local/bin/parental-controls"
elif [ ! -x "${INSTALL_DIR}/.venv/bin/python" ]; then
    echo "ERROR: ${INSTALL_DIR}/.venv/bin/python missing; cannot install the recovery CLI." >&2
    exit 1
else
    run install -m 0750 -o root -g root "$CLI_WRAPPER" /usr/local/bin/parental-controls
    echo "Installed recovery CLI: sudo parental-controls {unlock,unfreeze-all,reset-password,add-admin,remove-admin,lock-admin}"
fi

echo "== 6/8 firewall =="
if [ "$OPEN_FIREWALL" -eq 1 ]; then
    if ! command -v firewall-cmd >/dev/null; then
        echo "WARNING: firewall-cmd not found; skipping firewall change." >&2
    elif [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] firewall-cmd --permanent --add-port=${BIND_PORT}/tcp && firewall-cmd --reload"
    elif firewall-cmd --state >/dev/null 2>&1; then
        firewall-cmd --permanent --add-port="${BIND_PORT}/tcp"
        firewall-cmd --reload
        echo "Opened ${BIND_PORT}/tcp in the default zone."
    else
        echo "WARNING: firewalld is not running; nothing to open." >&2
    fi
else
    echo "(skipped — pass --open-firewall to open ${BIND_PORT}/tcp)"
fi

echo "== 7/8 enable + start =="
run systemctl daemon-reload
run systemctl enable --now parental-controls.service
run systemctl enable --now parental-controls-enforce.timer
run systemctl enable --now parental-controls-healthcheck.timer
# Restart when the configuration changed. Two cases: the rendered units
# differ from the installed ones, or the installed units are newer than the
# running process (left over by an installer run without restart support).
NEED_RESTART="$UNITS_CHANGED"
if [ "$NEED_RESTART" -eq 0 ] \
   && [ "$DRY_RUN" -eq 0 ] \
   && systemctl is-active -q parental-controls.service; then
    UNIT_MTIME=$(stat -c %Y /etc/systemd/system/parental-controls.service)
    START_EPOCH=$(date -d "$(systemctl show -p ActiveEnterTimestamp \
        --value parental-controls.service)" +%s)
    if [ "$UNIT_MTIME" -gt "$START_EPOCH" ]; then
        echo "Installed units are newer than the running service — restarting."
        NEED_RESTART=1
    fi
fi
if [ "$NEED_RESTART" -eq 1 ]; then
    [ "$UNITS_CHANGED" -eq 1 ] && \
        echo "Unit files changed — restarting service to pick up the new configuration."
    run systemctl restart parental-controls.service
else
    echo "(units unchanged — service left running)"
fi

if [ "$DRY_RUN" -eq 1 ]; then
    echo "Dry run complete — nothing was changed."
    exit 0
fi

echo "== 8/8 verify =="
sleep 3
systemctl is-active parental-controls.service
PROBE_HOST="$BIND_HOST"
[ "$PROBE_HOST" = "0.0.0.0" ] && PROBE_HOST="127.0.0.1"
code=$(curl -s -o /dev/null -w "%{http_code}" "http://${PROBE_HOST}:${BIND_PORT}/" || true)
echo "GET / -> $code (302 login redirect expected)"
if [ "$code" != "302" ]; then
    echo "WARNING: unexpected status; check journalctl -u parental-controls.service" >&2
    case "$BIND_HOST" in
        127.0.0.1|localhost|::1) ;;
        *) echo "HINT: for a LAN bind, an unreachable probe can also mean the host" >&2
           echo "firewall is blocking ${BIND_PORT}/tcp — re-run with --open-firewall." >&2 ;;
    esac
fi

echo
echo "-- health-check timer --"
if systemctl is-enabled -q parental-controls-healthcheck.timer \
   && systemctl is-active -q parental-controls-healthcheck.timer; then
    echo "parental-controls-healthcheck.timer enabled and active (probes every 5 min)"
else
    echo "WARNING: parental-controls-healthcheck.timer is not enabled+active;" >&2
    echo "run: sudo systemctl enable --now parental-controls-healthcheck.timer" >&2
fi
# Run one probe now (the same script the timer runs) so a broken panel is
# reported at install time instead of silently on the first tick.
if systemctl start --wait --quiet parental-controls-healthcheck.service; then
    echo "health probe -> OK"
else
    echo "WARNING: health probe FAILED - the panel process is up but not serving" >&2
    echo "correctly; details: journalctl -u parental-controls-healthcheck.service" >&2
fi

echo
echo "Installed into $INSTALL_DIR (listening on ${BIND_HOST}:${BIND_PORT}). Next steps:"
echo "  1. Get the one-time admin credential: journalctl -u parental-controls.service | grep 'AUTH BOOTSTRAP'"
case "$BIND_HOST" in
    127.0.0.1|localhost|::1)
        echo "  2. From your machine: ssh -L ${BIND_PORT}:localhost:${BIND_PORT} <user>@<host>, open http://localhost:${BIND_PORT}" ;;
    *)
        echo "  2. Open http://${BIND_HOST}:${BIND_PORT} from a machine on the LAN" ;;
esac
echo "  3. Log in and change the password immediately (forced on first login)."
echo "  4. Monitoring: parental-controls-healthcheck.timer probes the panel every"
echo "     5 min. On failure the unit shows up in 'systemctl --failed' and"
echo "     'journalctl -u parental-controls-healthcheck.service' (it never notifies"
echo "     a child). Recover with: sudo systemctl restart parental-controls.service"
