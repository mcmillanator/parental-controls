#!/usr/bin/env bash
# Liveness probe for the parental-controls panel.
#
# Runs from parental-controls-healthcheck.timer (see systemd/). Closes the gap
# Restart=always leaves open: a process that stays up while it stops serving
# correctly -- hung WSGI worker, port that never accepts, crash loop parked in
# "activating", or an auth store that became unreadable after startup.
#
# Checks, in order:
#   1. parental-controls.service reports "active" (not inactive/failed/
#      activating). A systemctl that cannot be queried at all is reported
#      as a warning, not a failure: the port probe below decides instead.
#   2. GET http://<bind host>:<port>/ answers 200, or the 302 login redirect.
#      An unauthenticated GET needs no credentials, and a hung process is
#      caught by curl's --max-time, not by a connection refusal.
#   3. The auth store parses as JSON and is readable *by the running service
#      user*. This is the one thing check 2 cannot see: require_admin() returns
#      the login redirect before it ever loads the store, so a store that went
#      bad after startup leaves the panel answering 302 while every real
#      request 500s. Same UID as the service, so the verdict matches the app's
#      own view (auth.init_store() refuses to start on an unwritable store).
#
# Deliberately NOT done here:
# - No restart/stop/mask of parental-controls.service. Restarting needs a root
#   sudo rule this unit must not have, and a panel restart drops every admin
#   session, so a failure is surfaced for a human instead:
#     systemctl status parental-controls-healthcheck.service
#     sudo systemctl restart parental-controls.service
# - No notification. This must never ring on a child's account.
#
# Usage: healthcheck.sh [--unit UNIT] [--host HOST] [--port PORT]
#                        [--auth-store PATH] [--timeout SECONDS]
#                        [--tls] [--cert PATH]
#
# --tls probes https:// (with -k: the healthcheck authenticates nothing and
# sends no credentials, so skipping verification is safe here; -k must
# never appear in any other project script) and warns when the TLS cert
# at --cert (default /etc/parental-controls/tls/server.crt) expires in
# under 30 days (warning-only, exit 0).
#
# Exit status: 0 = healthy, 1 = unhealthy, 2 = bad usage.
# Runs as @SERVICE_USER@ in production; flags exist so the tests can point it
# at a throwaway port, a stand-in unit name and a temp auth store.
set -euo pipefail

UNIT="parental-controls.service"
HOST="127.0.0.1"
PORT="5000"
AUTH_STORE="/etc/parental-controls/auth.json"
TIMEOUT="10"
PROBE_PATH="/"
USE_TLS=0
CERT="/etc/parental-controls/tls/server.crt"

usage() {
    # The header comment is the help text (and the design note).
    sed -n '2,43p' "$0" | sed 's/^#\( \|$\)//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --unit) [ $# -ge 2 ] || { echo "healthcheck: --unit needs a value" >&2; exit 2; }
                UNIT="$2"; shift 2 ;;
        --host) [ $# -ge 2 ] || { echo "healthcheck: --host needs a value" >&2; exit 2; }
                HOST="$2"; shift 2 ;;
        --port) [ $# -ge 2 ] || { echo "healthcheck: --port needs a value" >&2; exit 2; }
                PORT="$2"; shift 2 ;;
        --auth-store) [ $# -ge 2 ] || { echo "healthcheck: --auth-store needs a value" >&2; exit 2; }
                AUTH_STORE="$2"; shift 2 ;;
        --timeout) [ $# -ge 2 ] || { echo "healthcheck: --timeout needs a value" >&2; exit 2; }
                TIMEOUT="$2"; shift 2 ;;
        --tls) USE_TLS=1; shift ;;
        --cert) [ $# -ge 2 ] || { echo "healthcheck: --cert needs a value" >&2; exit 2; }
                CERT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "healthcheck: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

case "$PORT" in
    ''|*[!0-9]*) echo "healthcheck: invalid --port: '$PORT'" >&2; exit 2 ;;
esac
case "$TIMEOUT" in
    ''|*[!0-9]*) echo "healthcheck: invalid --timeout: '$TIMEOUT'" >&2; exit 2 ;;
esac
[ "$TIMEOUT" -ge 1 ] || { echo "healthcheck: --timeout must be >= 1" >&2; exit 2; }

fail() {
    # Every unhealthy outcome leaves the same grep-able prefix in the journal.
    echo "HEALTHCHECK FAILED: $*" >&2
    exit 1
}

warn() {
    echo "healthcheck: WARNING: $*" >&2
}

command -v systemctl >/dev/null 2>&1 || fail "systemctl not found on PATH"
command -v curl >/dev/null 2>&1 || fail "curl not found on PATH"
command -v python3 >/dev/null 2>&1 || \
    fail "python3 not found on PATH (needed to validate the auth store)"

errfile="$(mktemp)"
trap 'rm -f "$errfile"' EXIT

# 1. The unit itself. `is-active` is a read-only query, so this works
# unprivileged. If systemctl cannot reach the system manager at all (no
# stdout state), say so and let the HTTP probe below decide: a permanently
# unqueryable manager is a broken probe, not a broken panel, and must not
# cry wolf every five minutes.
unit_state="$(systemctl is-active "$UNIT" 2>"$errfile")" || true
case "$unit_state" in
    active) ;;
    '') warn "could not query the state of ${UNIT}" \
              "(systemctl: $(tr '\n' ' ' <"$errfile" | cut -c1-160))" \
              "; falling back to the HTTP probe" ;;
    *)  fail "${UNIT} is '${unit_state}', want 'active'" ;;
esac

# A wildcard bind is not a usable probe target; loopback answers the same
# socket. Mirrors the probe in install.sh's verify step.
probe_host="$HOST"
case "$probe_host" in
    0.0.0.0|"")   probe_host="127.0.0.1" ;;
    ::|"[::]")   probe_host="::1" ;;
esac
if [ "$USE_TLS" -eq 1 ]; then
    scheme="https"
    curl_tls=(-k)
else
    scheme="http"
    curl_tls=()
fi
url="${scheme}://${probe_host}:${PORT}${PROBE_PATH}"

# 2. The port. %{redirect_url} gives the Location target, so one request
# answers both "did it serve" and "did it serve the login page".
# -k is TLS-only (self-signed cert): the probe authenticates nothing and
# sends no credentials, so skipping verification is safe exactly here.
info="$(curl -sS "${curl_tls[@]}" -o /dev/null -w '%{http_code} %{redirect_url}' \
        --max-time "$TIMEOUT" "$url" 2>"$errfile")" && rc=0 || rc=$?
code="${info%% *}"
target="${info#* }"

if [ "$rc" -ne 0 ]; then
    detail="$(tr '\n' ' ' <"$errfile" | cut -c1-160)"
    [ -n "$detail" ] && detail=" (${detail})"
    case "$rc" in
        28) fail "no answer from ${url} within ${TIMEOUT}s${detail}" \
                "; the panel process is hung or the port is firewalled" ;;
        7)  fail "cannot reach ${url}${detail}" \
                "; nothing is listening on ${probe_host}:${PORT}" ;;
        *)  fail "curl exit ${rc} probing ${url}${detail}" ;;
    esac
fi

case "$code" in
    200) ;;
    302) case "$target" in
            *"/login"*|*"/login?"*) ;;
            *) fail "GET ${PROBE_PATH} -> 302 to '${target:-<no Location>}'" \
                    "; expected the login redirect" ;;
         esac ;;
    000) fail "GET ${PROBE_PATH} -> no HTTP status from ${url}" ;;
    *)  fail "GET ${PROBE_PATH} -> HTTP ${code}${target:+ (Location: ${target})}" \
            "; want 200 or a 302 to /login" ;;
esac

# 3. The auth store. Readable + parseable, as the same user the panel runs as.
# A missing store is only a warning: wsgi.py's init_store() bootstraps an empty
# one (nobody can log in until an admin is added) and the panel still serves.
if [ -e "$AUTH_STORE" ]; then
    [ -r "$AUTH_STORE" ] || \
        fail "auth store ${AUTH_STORE} is not readable by this user" \
             "; the panel answers every request with 500"
    store_err="$(python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as fh:
        json.load(fh)
except Exception as exc:
    sys.exit("%s: %s" % (type(exc).__name__, exc))
' "$AUTH_STORE" 2>&1)" && store_rc=0 || store_rc=$?
    [ "$store_rc" -eq 0 ] || \
        fail "auth store ${AUTH_STORE} is unusable (${store_err})" \
             "; the panel answers every request with 500"
else
    warn "auth store ${AUTH_STORE} does not exist; an empty store was" \
         "bootstrapped and nobody can log in until an admin is added"
fi

echo "healthcheck OK: ${UNIT} ${unit_state:-state unqueried}," \
     "GET ${PROBE_PATH} -> ${code}, auth store ${AUTH_STORE} usable"

# 4. TLS cert expiry (TLS mode only). Warning-only: a coming expiry must
# not page as a failure and must not restart anything (exit stays 0).
if [ "$USE_TLS" -eq 1 ]; then
    if [ -f "$CERT" ] && command -v openssl >/dev/null 2>&1 \
        && enddate="$(openssl x509 -in "$CERT" -noout -enddate 2>/dev/null \
            | cut -d= -f2-)" && [ -n "$enddate" ]; then
        end_epoch="$(date -d "$enddate" +%s 2>/dev/null || echo 0)"
        days=$(( (end_epoch - $(date +%s)) / 86400 ))
        if [ "$days" -lt 30 ]; then
            warn "TLS cert ${CERT} expires in ${days} days (${enddate})" \
                 "-- re-run install with --regen-tls-cert before it lapses"
        fi
    else
        warn "TLS enabled but cert ${CERT} is unreadable;" \
             "skipping expiry check"
    fi
fi
