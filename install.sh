#!/usr/bin/env bash
# One-command installer for Bazzite Parental Controls (MVP).
# Idempotent: safe to re-run. Must run as root.
#
#   sudo ./install.sh [--install-dir DIR] [--user USER] [--bind HOST]
#                     [--port PORT] [--open-firewall] [--dry-run]
#                     [--session-user USER]... [--no-session-agent]
#                     [--tls [--tls-cert CERT --tls-key KEY] [--tls-name NAME]
#                      [--regen-tls-cert] | --insecure-lan]
#
# Runs the panel as a dedicated hardened service user (`parental-controls`
# by default): a system account with a nologin shell, no home and a locked
# password, holding no sudo of its own — it only ever receives the scoped
# sudoers grants rendered below. Pass --user (or set SERVICE_USER in the
# environment) to override the account; the shared human/agent login account
# must never own this network-exposed process, so $SUDO_USER is deliberately
# NOT the default.
#
# Defaults keep the panel on localhost only (127.0.0.1:5000, no firewall
# change). --bind/--port render into the service unit; --open-firewall opens
# the chosen port in the default firewalld zone. A non-loopback --bind
# refuses plain HTTP: pass --tls for a self-signed certificate (generated
# with the openssl CLI into /etc/parental-controls/tls/, or supply your
# own pair with --tls-cert/--tls-key) or --insecure-lan to knowingly stay
# on plain HTTP (every page then shows a warning banner).
#
# --tls never implies --open-firewall: pass it explicitly to expose the
# port. Generated certificates are never overwritten on re-run (stored
# browser trust would break); --regen-tls-cert forces rotation.
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
# Dedicated hardened service user. Explicit --user (or SERVICE_USER in the
# environment) wins; otherwise the default below. Never $SUDO_USER: that is
# the shared human/agent login account and must not own a network-exposed
# panel process.
DEFAULT_SERVICE_USER="parental-controls"
SERVICE_USER="${SERVICE_USER:-$DEFAULT_SERVICE_USER}"
BIND_HOST="127.0.0.1"
BIND_PORT="5000"
OPEN_FIREWALL=0
DRY_RUN=0
TLS_MODE=0
INSECURE_LAN=0
TLS_CERT_IN=""
TLS_KEY_IN=""
TLS_NAME=""
REGEN_TLS_CERT=0
TLS_CERT="/etc/parental-controls/tls/server.crt"
TLS_KEY="/etc/parental-controls/tls/server.key"
TLS_URL_NAME=""
# Managed children that should get the per-user focus session agent installed
# (repeatable --session-user). Empty by default: a plain install ships the
# panel only, and the agent is enabled per child, mirroring the per-user
# focus_engine opt-in.
SESSION_USERS=()

[ -f "$SOURCE_DIR/tls_lib.sh" ] \
    || { echo "Missing $SOURCE_DIR/tls_lib.sh — run install.sh from the app checkout." >&2; exit 1; }
# shellcheck disable=SC1091
. "$SOURCE_DIR/tls_lib.sh"

while [ $# -gt 0 ]; do
    case "$1" in
        --install-dir|--app-dir) INSTALL_DIR="$2"; shift 2 ;;
        --user) SERVICE_USER="$2"; shift 2 ;;
        --bind) BIND_HOST="$2"; shift 2 ;;
        --port) BIND_PORT="$2"; shift 2 ;;
        --open-firewall) OPEN_FIREWALL=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --session-user) SESSION_USERS+=("$2"); shift 2 ;;
        --session-user=*) SESSION_USERS+=("${1#*=}"); shift ;;
        --no-session-agent) SESSION_USERS=(); shift ;;
        --tls) TLS_MODE=1; shift ;;
        --tls-cert) TLS_CERT_IN="$2"; shift 2 ;;
        --tls-key) TLS_KEY_IN="$2"; shift 2 ;;
        --tls-name) TLS_NAME="$2"; shift 2 ;;
        --regen-tls-cert) REGEN_TLS_CERT=1; shift ;;
        --insecure-lan) INSECURE_LAN=1; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo ./install.sh" >&2
    exit 1
fi
if [ -z "$SERVICE_USER" ]; then
    echo "Could not determine service user (no --user and empty SERVICE_USER)." >&2
    exit 1
fi
# Create the dedicated service user idempotently. A system account: nologin
# shell, no home, locked password, no sudo of its own. Skipped when the
# account already exists, so re-runs (migration included) never reset it.
# Only the default account is auto-created: a custom --user must already
# exist, so a typo can never conjure a stray login account.
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    if [ "$SERVICE_USER" != "$DEFAULT_SERVICE_USER" ]; then
        echo "User '$SERVICE_USER' does not exist (only the default '$DEFAULT_SERVICE_USER' is auto-created)." >&2
        exit 1
    fi
    # Account creation itself is deferred to ensure_service_user, below the
    # run()/as_user() helper definitions it uses.
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
if [ -n "$TLS_CERT_IN" ] || [ -n "$TLS_KEY_IN" ] || [ "$REGEN_TLS_CERT" -eq 1 ]; then
    # Owner certs and regen only make sense with TLS: imply it.
    TLS_MODE=1
fi
if [ -n "$TLS_CERT_IN" ] || [ -n "$TLS_KEY_IN" ]; then
    if [ -z "$TLS_CERT_IN" ] || [ -z "$TLS_KEY_IN" ]; then
        echo "--tls-cert and --tls-key must both be given." >&2
        exit 2
    fi
fi
if [ -n "$TLS_NAME" ]; then
    case "$TLS_NAME" in
        *[!A-Za-z0-9.:-]*) echo "Invalid --tls-name: '$TLS_NAME'." >&2; exit 2 ;;
    esac
fi
if [ "$TLS_MODE" -eq 1 ] && [ "$INSECURE_LAN" -eq 1 ]; then
    echo "--tls and --insecure-lan are mutually exclusive." >&2
    exit 2
fi
if [ "$TLS_MODE" -eq 1 ]; then
    command -v openssl >/dev/null \
        || { echo "openssl not found: install openssl or supply --tls-cert/--tls-key (TLS validation, fingerprint and expiry need it)." >&2; exit 2; }
fi
MODE="$(tls_decide_mode "$BIND_HOST" "$TLS_MODE" "$INSECURE_LAN")"
if [ "$MODE" = "refuse-plain-lan" ]; then
    echo "refusing plain-HTTP LAN bind (session cookies travel in the clear); re-run with --tls (recommended) or --insecure-lan." >&2
    exit 2
fi
if [ "$MODE" = "insecure" ]; then
    echo "WARNING: --bind $BIND_HOST exposes the panel beyond this machine" >&2
    echo "without TLS; session cookies travel in the clear. Re-run install with --tls to encrypt logins." >&2
fi
if tls_is_loopback "$BIND_HOST" && [ "$INSECURE_LAN" -eq 1 ]; then
    echo "(note: --insecure-lan has no effect on a loopback bind)" >&2
fi
[ -f "$SOURCE_DIR/app.py" ] || { echo "No app.py in $SOURCE_DIR — run install.sh from the app checkout." >&2; exit 1; }
# Session-agent sources are staged into the install dir by the same rsync
# below (stdlib-only: no new venv dependency). Fail early if absent.
for agent_file in session_agent.py focus_feed.py; do
    [ -f "$SOURCE_DIR/$agent_file" ] \
        || { echo "Missing $SOURCE_DIR/$agent_file — run install.sh from the app checkout." >&2; exit 1; }
done

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

# Create the dedicated service user idempotently (called after the helpers
# above are defined). A system account: nologin shell, no home, locked
# password, no sudo of its own — it only ever receives the scoped sudoers
# grants. Skipped when the account already exists, so re-runs (including the
# stan → service-user migration) never reset it. Only the default account is
# auto-created; a custom --user must already exist, so a typo can never
# conjure a stray login account.
ensure_service_user() {
    id "$SERVICE_USER" >/dev/null 2>&1 && return 0
    [ "$SERVICE_USER" = "$DEFAULT_SERVICE_USER" ] || {
        echo "User '$SERVICE_USER' does not exist (only the default '$DEFAULT_SERVICE_USER' is auto-created)." >&2
        exit 1
    }
    NOLOGIN=/usr/sbin/nologin
    [ -x "$NOLOGIN" ] || NOLOGIN=/sbin/nologin
    run useradd --system --no-create-home --shell "$NOLOGIN" "$SERVICE_USER"
    # Belt and braces: a system account is born without a password hash, and
    # locking it refuses password auth even if one ever appears.
    run usermod -L "$SERVICE_USER"
}

ensure_service_user

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
            --exclude=/audit.jsonl \
            --exclude=/state.db --exclude=/state.db-wal \
            --exclude=/state.db-shm --exclude=/state.db.bak.* \
            "$SOURCE_DIR/" "$INSTALL_DIR/"
    else
        # Minimal fallback without rsync: copy code, keep existing state.
        ( cd "$SOURCE_DIR" && tar --exclude='./.venv' --exclude='./.git' \
            --exclude='*/__pycache__' --exclude='*.pyc' \
            --exclude='./bedtime.json' --exclude='./budgets.json' \
            --exclude='./process_prefs.json' --exclude='./auth.json' \
            --exclude='./audit.jsonl' \
            --exclude='./state.db' --exclude='./state.db-wal' \
            --exclude='./state.db-shm' --exclude='./state.db.bak.*' \
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
# Ownership migration only: live state files (excluded from the sync above)
# keep their bytes; only their owner moves to the service user.
run chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# Templates are installed from the checkout (canonical), falling back to the
# staged copy if the checkout's are somehow absent.
# Units are read from the checkout when running from one (the normal case),
# falling back to the staged copy when the installer runs from the install
# dir itself. Requirements are ALWAYS installed from the staged copy under
# $INSTALL_DIR: pip runs as the service user, which cannot traverse a home
# directory (the agent's home is mode 0700 by default), while $INSTALL_DIR
# is owned by that user.
TEMPLATE_DIR="$INSTALL_DIR"
[ -f "$SOURCE_DIR/systemd/parental-controls.service" ] && TEMPLATE_DIR="$SOURCE_DIR"

echo "== 2/8 venv + dependencies (as $SERVICE_USER) =="
# The service user is intentionally homeless (system account, nologin
# shell), so HOME-based tooling (pip's cache) would fall back to / and fail
# ("directory '/.cache/pip' ... not owned ... cache has been disabled").
# Point HOME and the cache at directories the service user owns instead;
# verified for a homeless account (venv create + wheel install + import).
VENV_HOME="$INSTALL_DIR"
VENV_CACHE="$INSTALL_DIR/.cache"
run mkdir -p "$VENV_CACHE"
run chown "$SERVICE_USER:$SERVICE_USER" "$VENV_CACHE"
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    as_user env HOME="$VENV_HOME" XDG_CACHE_HOME="$VENV_CACHE" \
        python3 -m venv "$INSTALL_DIR/.venv"
fi
as_user env HOME="$VENV_HOME" XDG_CACHE_HOME="$VENV_CACHE" \
    PIP_CACHE_DIR="$VENV_CACHE/pip" \
    "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

echo "== 2b/8 TLS certificate =="
if [ "$MODE" != "tls" ]; then
    echo "(skipped — pass --tls for an encrypted LAN bind)"
else
    TLS_DIR="/etc/parental-controls/tls"
    run mkdir -p "$TLS_DIR"
    run chown "$SERVICE_USER:$SERVICE_USER" "$TLS_DIR" /etc/parental-controls
    run chmod 700 "$TLS_DIR" /etc/parental-controls
    if [ -n "$TLS_CERT_IN" ]; then
        # Owner-supplied rotation: validated here, copied byte-for-byte
        # into the store (0600, service-user-owned). Never staged from
        # the checkout: absolute owner paths only (see tls_lib.sh).
        tls_validate_owner_pair "$TLS_CERT_IN" "$TLS_KEY_IN" || exit 2
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "[dry-run] install owner cert $TLS_CERT_IN -> $TLS_CERT (0600)"
        else
            if [ -f "$TLS_CERT" ]; then
                stamp="$(date +%Y%m%d%H%M%S)"
                cp "$TLS_CERT" "$TLS_CERT.bak-$stamp"
                cp "$TLS_KEY" "$TLS_KEY.bak-$stamp"
                echo "Backed up existing pair to *.bak-$stamp"
            fi
            cp "$TLS_CERT_IN" "$TLS_CERT"
            cp "$TLS_KEY_IN" "$TLS_KEY"
            chown "$SERVICE_USER:$SERVICE_USER" "$TLS_CERT" "$TLS_KEY"
            chmod 600 "$TLS_CERT" "$TLS_KEY"
            echo "Installed owner-supplied certificate."
        fi
        TLS_URL_NAME="${TLS_NAME:-owner-supplied}"
    elif [ -f "$TLS_CERT" ] && [ -f "$TLS_KEY" ] \
        && [ "$REGEN_TLS_CERT" -eq 0 ]; then
        # Never silently regenerated on re-run: a fresh fingerprint would
        # break every parent's stored trust-on-first-use decision.
        echo "Keeping existing certificate (never regenerated on re-run)."
        TLS_URL_NAME="${TLS_NAME:-existing}"
    else
        # (Re)generation needs a LAN identity for CN/SAN. --bind 0.0.0.0
        # is a wildcard, never a cert identity — hence --tls-name.
        if [ -z "$TLS_NAME" ]; then
            LAN_IP="$(hostname -I 2>/dev/null | tr ' ' '\n' \
                | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' \
                | grep -v '^127\.' | head -n 1)"
            if [ -z "$LAN_IP" ]; then
                echo "Could not detect a LAN address; re-run with --tls-name <lan-ip-or-name>." >&2
                exit 2
            fi
            SHORT="$(hostname -f 2>/dev/null | cut -d. -f1)"
            TLS_URL_NAME="$LAN_IP"
            CN="$LAN_IP"
            SAN="DNS:localhost,IP:127.0.0.1,IP:::1,IP:$LAN_IP"
            if [ -n "$SHORT" ] && [ "$SHORT" != "$LAN_IP" ]; then
                SAN="$SAN,DNS:$SHORT"
            fi
        else
            TLS_URL_NAME="$TLS_NAME"
            CN="$TLS_NAME"
            SAN="DNS:localhost,IP:127.0.0.1,IP:::1,$(tls_san_entry "$TLS_NAME")"
        fi
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "[dry-run] openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj /CN=$CN -addext SAN=$SAN -> $TLS_CERT/$TLS_KEY (0600)"
        else
            if [ "$REGEN_TLS_CERT" -eq 1 ] && [ -f "$TLS_CERT" ]; then
                stamp="$(date +%Y%m%d%H%M%S)"
                cp "$TLS_CERT" "$TLS_CERT.bak-$stamp"
                cp "$TLS_KEY" "$TLS_KEY.bak-$stamp"
                echo "Backed up existing pair to *.bak-$stamp"
            fi
            tls_generate_selfsigned "$TLS_CERT" "$TLS_KEY" "$CN" "$SAN"
            chown "$SERVICE_USER:$SERVICE_USER" "$TLS_CERT" "$TLS_KEY"
            echo "Generated self-signed certificate (10y, CN=$CN)."
        fi
    fi
    if [ "$DRY_RUN" -eq 0 ]; then
        echo "TLS certificate ${TLS_CERT}:"
        openssl x509 -in "$TLS_CERT" -noout -fingerprint -sha256
        openssl x509 -in "$TLS_CERT" -noout -ext subjectAltName || true
        openssl x509 -in "$TLS_CERT" -noout -enddate
        echo "Panel URL: https://${TLS_URL_NAME}:${BIND_PORT}"
        echo "Verify the fingerprint above at the browser's first certificate warning (trust on first use)."
    fi
fi

echo "== 3/8 systemd units (bind $BIND_HOST:$BIND_PORT) =="
TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
# The healthcheck probe needs --tls exactly when the panel serves TLS.
HEALTH_TLS=""
[ "$MODE" = "tls" ] && HEALTH_TLS=" --tls"
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
    # TLS variant second pass: loopback deletes the placeholder lines
    # (byte-identical to the pre-TLS rendering); tls/insecure render
    # their ExecStart/env lines. See tls_lib.sh.
    tls_apply_variant "$TMPD/$unit" "$MODE" "$INSTALL_DIR" "$BIND_HOST" \
        "$BIND_PORT" "$TLS_CERT" "$TLS_KEY" "$HEALTH_TLS"
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

echo "== 4b/8 session agent (per-child user unit + linger) =="
# The child-side agent (session_agent.py) is staged with the rest of the app
# above; this step installs its systemd *user* unit into each managed child's
# home and enables linger so the user manager (and thus the agent) starts at
# boot and survives logout. Code is never re-staged here — the install dir is
# the single source. @APP_DIR@ is resolved at render time; no checkout path.
SESSION_AGENT_UNIT="systemd/parental-session-agent.service"
[ -f "$TEMPLATE_DIR/$SESSION_AGENT_UNIT" ] \
    || { echo "Missing $TEMPLATE_DIR/$SESSION_AGENT_UNIT" >&2; exit 1; }
if [ "${#SESSION_USERS[@]}" -eq 0 ]; then
    echo "(no --session-user given: skipping per-child agent; pass"
    echo " --session-user <child> for each managed child to install it)"
else
    for session_user in "${SESSION_USERS[@]}"; do
        case "$session_user" in
            ''|*[!A-Za-z0-9_.-]*)
                echo "Invalid --session-user: '$session_user'." >&2; exit 2 ;;
        esac
        if ! id "$session_user" >/dev/null 2>&1; then
            echo "Unknown --session-user: '$session_user'." >&2; exit 1
        fi
        session_group="$(id -gn "$session_user")"
        session_home="$(getent passwd "$session_user" | cut -d: -f6)"
        [ -n "$session_home" ] \
            || { echo "No home dir for --session-user '$session_user'." >&2; exit 1; }
        session_udir="$session_home/.config/systemd/user"
        sed -e "s|@APP_DIR@|$INSTALL_DIR|g" \
            "$TEMPLATE_DIR/$SESSION_AGENT_UNIT" \
            > "$TMPD/parental-session-agent.service"
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "[dry-run] install $TMPD/parental-session-agent.service -> $session_udir/parental-session-agent.service (owner $session_user)"
            echo "[dry-run] loginctl enable-linger $session_user"
        else
            install -d -m 0755 -o "$session_user" -g "$session_group" \
                "$session_home/.config" "$session_home/.config/systemd" \
                "$session_udir"
            install -m 0644 -o "$session_user" -g "$session_group" \
                "$TMPD/parental-session-agent.service" \
                "$session_udir/parental-session-agent.service"
            loginctl enable-linger "$session_user"
            echo "Installed session-agent user unit for '$session_user' (linger enabled)."
        fi
    done
fi

echo "== 5/8 auth store dir =="
run mkdir -p /etc/parental-controls
# Migration, never reset: recursive chown moves existing auth.json / TLS
# material / audit data from the old owner to the service user byte-identical
# (only ownership changes); the mode stays 0700 on the directory.
run chown -R "$SERVICE_USER:$SERVICE_USER" /etc/parental-controls
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
# The service may still be starting after the restart: cheroot's TLS cold
# start (venv interpreter + imports + listen) is slower than waitress on
# loopback. A bare `systemctl is-active` exits non-zero while the state is
# `activating`/`auto-restart`, which under `set -e` aborts the installer
# before the 'Next steps' block ever prints. Poll instead: keep waiting for
# `active` (or a hard `failed`), then fall through to the final block either
# way, warning when the slow start times out.
SERVICE_STATE=""
for _ in $(seq 1 30); do
    SERVICE_STATE="$(systemctl is-active parental-controls.service || true)"
    [ "$SERVICE_STATE" = "active" ] && break
    [ "$SERVICE_STATE" = "failed" ] && break
    sleep 1
done
if [ "$SERVICE_STATE" = "active" ]; then
    echo "active"
else
    echo "WARNING: parental-controls.service did not reach 'active' (state: '${SERVICE_STATE:-unknown}');" >&2
    echo "inspect with: journalctl -u parental-controls.service -n 50" >&2
fi
# The panel must run as the dedicated service user — never as the shared
# human/agent login account — and the auth store must be owned and writable
# by it (password changes, lockouts and revocations persist there; startup
# fails closed otherwise).
UNIT_USER="$(systemctl show -p User --value parental-controls.service)"
if [ "$UNIT_USER" = "$SERVICE_USER" ]; then
    echo "unit User=$UNIT_USER (expected $SERVICE_USER)"
else
    echo "WARNING: parental-controls.service runs as '$UNIT_USER', expected '$SERVICE_USER'; re-run install.sh." >&2
fi
AUTH_FILE="/etc/parental-controls/auth.json"
if [ -f "$AUTH_FILE" ]; then
    AUTH_OWNER="$(stat -c %U "$AUTH_FILE")"
    if [ "$AUTH_OWNER" = "$SERVICE_USER" ]; then
        echo "auth store owned by $SERVICE_USER"
    else
        echo "WARNING: $AUTH_FILE owned by '$AUTH_OWNER', expected '$SERVICE_USER'; re-run install.sh." >&2
    fi
fi
if as_user test -w /etc/parental-controls; then
    echo "auth store writable by $SERVICE_USER"
else
    echo "WARNING: /etc/parental-controls is not writable by $SERVICE_USER." >&2
fi
PROBE_HOST="$BIND_HOST"
[ "$PROBE_HOST" = "0.0.0.0" ] && PROBE_HOST="127.0.0.1"
# cheroot's cold start (venv interpreter + imports + listen) can still be
# binding its socket a moment after the unit reports `active`, so a single
# probe can race the bind and see `000` on a perfectly healthy panel
# (observed live on --tls installs). Every install-time probe retries for a
# bounded window (~20s, 1s apart) until the panel answers with the expected
# code (200/302; with --cacert the TLS handshake is verified on every
# attempt, so a 302 here is always a TLS-validated response); a still-failing
# probe then warns for real.
probe_http() {
    http_code=000
    for _ in $(seq 1 "$1"); do
        if [ "$MODE" = "tls" ]; then
            # Validated against the installed cert (--cacert: this probe must
            # actually verify what it connects to; verification is skipped
            # only in the credential-less healthcheck.sh probe).
            http_code=$(curl -s -o /dev/null -w "%{http_code}" --cacert "$TLS_CERT" \
                "https://${PROBE_HOST}:${BIND_PORT}/" || true)
        else
            http_code=$(curl -s -o /dev/null -w "%{http_code}" \
                "http://${PROBE_HOST}:${BIND_PORT}/" || true)
        fi
        case "$http_code" in
            200|302) return 0 ;;
        esac
        sleep 1
    done
    return 1
}
PROBE_OK=0
if probe_http 20; then
    PROBE_OK=1
fi
echo "GET / -> $http_code (302 login redirect expected)"
if [ "$PROBE_OK" -ne 1 ]; then
    echo "WARNING: unexpected status; check journalctl -u parental-controls.service" >&2
    case "$BIND_HOST" in
        127.0.0.1|localhost|::1) ;;
        *) echo "HINT: for a LAN bind, an unreachable probe can also mean the host" >&2
           echo "firewall is blocking ${BIND_PORT}/tcp — re-run with --open-firewall." >&2
           if [ "$MODE" = "tls" ]; then
               echo "HINT: probed https://${PROBE_HOST}:${BIND_PORT}/; opening http:// in a browser fails — that scheme mismatch is why." >&2
           fi ;;
    esac
fi
if [ "$MODE" = "tls" ]; then
    echo "TLS certificate ${TLS_CERT}:"
    openssl x509 -in "$TLS_CERT" -noout -fingerprint -sha256
    openssl x509 -in "$TLS_CERT" -noout -enddate
    TLS_DAYS="$(tls_expiry_days "$TLS_CERT")"
    if [ "$TLS_DAYS" -lt 30 ]; then
        echo "WARNING: TLS certificate expires in ${TLS_DAYS} days — re-run with --regen-tls-cert before it lapses." >&2
    fi
elif [ "$MODE" = "insecure" ]; then
    echo "WARNING: insecure LAN mode (plain HTTP); session cookies travel in the clear — re-run install with --tls to encrypt logins." >&2
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
# reported at install time instead of silently on the first tick. The
# one-shot can race the same slow cheroot bind, so wait for a short
# bind-ready window (the same bounded probe above) before starting it —
# FAILED is then reserved for genuinely broken panels.
probe_http 20 || true
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
        # TLS-over-tunnel case: --tls on a loopback bind still serves TLS,
        # so the URL scheme must follow MODE, not the loopback host alone.
        if [ "$MODE" = "tls" ]; then
            echo "  2. Open your panel at: https://localhost:${BIND_PORT}"
        else
            echo "  2. Open your panel at: http://localhost:${BIND_PORT}"
        fi
        echo "     (from another machine, tunnel first: ssh -L ${BIND_PORT}:localhost:${BIND_PORT} <user>@<host>)" ;;
    *)
        if [ "$MODE" = "tls" ]; then
            echo "  2. Open your panel at: https://${TLS_URL_NAME}:${BIND_PORT}"
            echo "     (verify the printed SHA-256 fingerprint at the browser's certificate warning)"
        else
            echo "  2. Open your panel at: http://${BIND_HOST}:${BIND_PORT}"
            echo "     (warning: plain HTTP — session cookies travel unencrypted)"
        fi ;;
esac
echo "  3. Log in and change the password immediately (forced on first login)."
echo "  4. Monitoring: parental-controls-healthcheck.timer probes the panel every"
echo "     5 min. On failure the unit shows up in 'systemctl --failed' and"
echo "     'journalctl -u parental-controls-healthcheck.service' (it never notifies"
echo "     a child). Recover with: sudo systemctl restart parental-controls.service"
