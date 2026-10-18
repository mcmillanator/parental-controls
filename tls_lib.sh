#!/usr/bin/env bash
# TLS helpers for install.sh (sources this) and its unit tests.
#
# Pure decision/generation helpers with no side effects beyond their
# arguments, so tests/test_tls.py can source this file and exercise the
# exact logic the installer runs. Must stay clean under `set -u`.
# No verification-disabled curl here: the only probe that skips
# verification lives in healthcheck.sh (which authenticates nothing);
# install.sh verifies TLS with --cacert instead.

# 0/1: is this bind host loopback-only?
tls_is_loopback() {
    case "$1" in
        127.0.0.1|localhost|::1) return 0 ;;
        *) return 1 ;;
    esac
}

# Serving mode for a bind. Prints one of: loopback | tls | insecure |
# refuse-plain-lan. $1=bind host, $2=tls flag (0/1), $3=insecure flag (0/1).
# An explicit --tls always wins (TLS-over-loopback included); --insecure-lan
# is meaningless on loopback and ignored there.
tls_decide_mode() {
    if [ "$2" = 1 ]; then
        echo tls
    elif tls_is_loopback "$1"; then
        echo loopback
    elif [ "$3" = 1 ]; then
        echo insecure
    else
        echo refuse-plain-lan
    fi
}

# SAN entry for a --tls-name: IPs go in as IP:, names as DNS:.
tls_san_entry() {
    case "$1" in
        *:*) echo "IP:$1" ;;
        ''|*[!0-9.]*|*.*.*.*.*) echo "DNS:$1" ;;
        [0-9]*.[0-9]*.[0-9]*.[0-9]*) echo "IP:$1" ;;
        *) echo "DNS:$1" ;;
    esac
}

# Generate a self-signed pair with the exact argv install.sh documents.
# $1=cert path, $2=key path, $3=CN, $4=full SAN list. Files end up 0600.
tls_generate_selfsigned() {
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -subj "/CN=$3" -addext "subjectAltName=$4" \
        -keyout "$2" -out "$1"
    chmod 600 "$1" "$2"
}

# Validate an owner-supplied pair. $1=cert, $2=key. Prints the reason to
# stderr, returns 1 on any problem (install.sh turns that into exit 2).
tls_validate_owner_pair() {
    case "$1" in
        /*) ;;
        *) echo "tls: --tls-cert must be an absolute path: '$1'" >&2
           return 1 ;;
    esac
    case "$2" in
        /*) ;;
        *) echo "tls: --tls-key must be an absolute path: '$2'" >&2
           return 1 ;;
    esac
    [ -r "$1" ] || { echo "tls: --tls-cert not readable: '$1'" >&2
                     return 1; }
    [ -r "$2" ] || { echo "tls: --tls-key not readable: '$2'" >&2
                     return 1; }
    openssl x509 -in "$1" -noout >/dev/null 2>&1 \
        || { echo "tls: --tls-cert does not parse as a certificate: '$1'" >&2
             return 1; }
    cert_mod="$(openssl x509 -in "$1" -noout -modulus 2>/dev/null \
        | openssl md5)" || return 1
    key_mod="$({ openssl pkey -in "$2" -noout -modulus 2>/dev/null \
        || openssl rsa -in "$2" -noout -modulus 2>/dev/null; } \
        | openssl md5)" || return 1
    [ -n "$cert_mod" ] && [ "$cert_mod" = "$key_mod" ] \
        || { echo "tls: --tls-key does not match --tls-cert" >&2
             return 1; }
    return 0
}

# Whole days until $1 (a cert path) expires; prints the integer, returns 1
# when the cert cannot be read or parsed.
tls_expiry_days() {
    enddate="$(openssl x509 -in "$1" -noout -enddate 2>/dev/null \
        | cut -d= -f2-)" || return 1
    [ -n "$enddate" ] || return 1
    end_epoch="$(date -d "$enddate" +%s 2>/dev/null)" || return 1
    now_epoch="$(date +%s)"
    echo $(( (end_epoch - now_epoch) / 86400 ))
}

# Second render pass over a unit file already through install.sh's value
# substitution. $1=file $2=mode (loopback|tls|insecure) $3=install dir
# $4=bind host $5=bind port $6=cert path $7=key path $8=health --tls flag.
tls_apply_variant() {
    case "$2" in
        tls)
            exec_line="ExecStart=/bin/bash -c 'cd $3 && . .venv/bin/activate && exec python $3/tls_serve.py --host=$4 --port=$5 --cert=$6 --key=$7'"
            # `&` would expand to the match in a sed replacement: escape it.
            exec_line="${exec_line//&/\\&}"
            sed -i -e '/^ExecStart=.*waitress-serve.*$/d' \
                -e "s|^@TLS_EXECSTART@$|${exec_line}|" \
                -e 's|^@TLS_ENV_LINE@$|Environment=PARENTAL_TLS=1|' \
                -e "s|@HEALTH_TLS@|$8|g" "$1"
            ;;
        insecure)
            sed -i -e '/^@TLS_EXECSTART@$/d' \
                -e 's|^@TLS_ENV_LINE@$|Environment=PARENTAL_INSECURE_LAN=1|' \
                -e "s|@HEALTH_TLS@|$8|g" "$1"
            ;;
        *)
            sed -i -e '/^@TLS_EXECSTART@$/d' \
                -e '/^@TLS_ENV_LINE@$/d' \
                -e "s|@HEALTH_TLS@|$8|g" "$1"
            ;;
    esac
}

# --deploy-webserver conflict gate. $1=bind given (0/1) $2=bind value
# $3=port given (0/1) $4=port value $5=insecure-lan flag (0/1). Returns 1
# (reason on stderr) when an explicit flag fights the mode; install.sh
# turns that into exit 2 BEFORE applying the implied values. --tls and
# --open-firewall alongside are no-ops and pass.
tls_webserver_conflicts() {
    if [ "$5" = 1 ]; then
        echo "--deploy-webserver conflicts with --insecure-lan: the webserver mode is TLS-only (https on 443 plus an http->https redirect on 80)." >&2
        return 1
    fi
    if [ "$1" = 1 ] && [ "$2" != "0.0.0.0" ]; then
        echo "--deploy-webserver implies --bind 0.0.0.0; drop the explicit --bind '$2' (or pass --bind 0.0.0.0)." >&2
        return 1
    fi
    if [ "$3" = 1 ] && [ "$4" != "443" ]; then
        echo "--deploy-webserver implies --port 443; drop the explicit --port '$4' (or pass --port 443)." >&2
        return 1
    fi
    return 0
}

# Third render pass over the ALREADY variant-applied panel unit
# (install.sh --deploy-webserver; never call it on the other units).
# $1=file $2=http redirect port (0 = off) $3=privileged ports in play (0/1).
# With $2>0 the tls_serve.py ExecStart gains ` --http-port $2` (a no-op on
# non-tls ExecStarts). With $3=1 the CAP_NET_BIND_SERVICE lines are inserted
# after LockPersonality=true: Ambient gives the unprivileged service user
# the bind, and the bounding set retains it plus what the passwordless-sudo
# children need (usermod/passwd/loginctl, systemd-run, and tee/cat/mkdir on
# /sys/fs/cgroup — CAP_SYS_ADMIN), so narrowing it never breaks enforcement.
# Both branches off = byte-identical to the pre-webserver rendering.
tls_apply_webserver() {
    if [ "$2" -gt 0 ]; then
        sed -i -e "s|^\(ExecStart=.*tls_serve\.py .*--key=[^']*\)'$|\1 --http-port $2'|" "$1"
    fi
    if [ "$3" = 1 ]; then
        sed -i -e '/^LockPersonality=true$/a AmbientCapabilities=CAP_NET_BIND_SERVICE\nCapabilityBoundingSet=CAP_NET_BIND_SERVICE CAP_AUDIT_WRITE CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER CAP_FSETID CAP_KILL CAP_MKNOD CAP_NET_RAW CAP_SETFCAP CAP_SETGID CAP_SETUID CAP_SETPCAP CAP_SYS_ADMIN' "$1"
    fi
}
