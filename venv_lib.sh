#!/usr/bin/env bash
# venv health probe + self-heal for install.sh (which sources this) and its
# unit tests (tests/test_venv_selfheal.py). Like tls_lib.sh: helpers are
# pure apart from their documented side effects, clean under `set -eu`.
#
# Why this exists — the 2026-11-14 incident.
# A Bazzite rpm-ostree image update landed Python 3.15 on the VM at reboot.
# /opt/parental-controls/.venv had been created by install.sh from the
# floating `python3` (its packages live under lib/python3.14/site-packages),
# so after the update the venv's bin/python resolved to 3.15, the 3.14
# site-packages silently fell off sys.path, and parental-controls.service
# crash-looped `ModuleNotFoundError: No module named 'cheroot'` (27
# restarts). The old install.sh guard was a presence check
# (`if [ ! -d "$INSTALL_DIR/.venv" ]`): a version-orphaned venv is very
# much present, so the guard kept it, and pip — asked through the same
# venv path — reported the requirements "already satisfied" while the
# interpreter could import none of them. Presence proved nothing; only
# the interpreter itself can answer whether the venv works.
#
# Why NOT pin `python3.14` in install.sh: an rpm-ostree update removes the
# old interpreter entirely at reboot, so a pinned venv becomes an
# unrecoverable hard failure (bin/python dangling, no matching interpreter
# to rebuild with or to run the installer's repair path). Pinning trades a
# crash-loop for a brick. Self-heal is the correct pattern: detect the
# orphaning from the interpreter's own behavior, delete, and rebuild the
# venv from whatever `python3` the image ships now. The first installer
# run after any image update takes ~30s (venv create + pip install)
# instead of crash-looping forever.

# venv_healthy VENV_DIR REQUIREMENTS_FILE
#
# Returns 0 only if VENV_DIR/bin/python exists, starts, and can BOTH find
# (importlib.metadata.distribution) and actually import EVERY
# distribution named in REQUIREMENTS_FILE. The package list is derived
# from requirements.txt (never hardcoded): top-of-line name tokens, with
# version specifiers, extras, comments and option lines dropped.
#
# This fails EXACTLY in the orphaned-venv case: after an interpreter
# swap, the old lib/python<old>/site-packages is not on sys.path, so the
# distributions are invisible to the interpreter the service actually
# runs — the probe sees the same ModuleNotFoundError the crash-looping
# unit would. It also catches a dangling bin/python (interpreter removed
# without a replacement default), a half-deleted venv, and a venv whose
# packages were purged. A `pip install --dry-run` probe was considered
# and rejected as primary: it re-asks pip (the component that lied
# during the incident) and costs network/cache state; the interpreter
# import is ground truth. The dry-run check stays as the installer's
# normal repair step (step 2/8 still runs `pip install -r` after this).
#
# Return codes (non-zero means "rebuild"):
#   1  structural: no runnable interpreter, or requirements unreadable/empty
#   2  interpreter runs but a required distribution is missing/unimportable
#
# Import-name rule: this project's stack maps trivially (flask, argon2-cffi
# -> argon2, waitress, cheroot), so the smoke import is the distribution
# name up to the first '-'. -B keeps the probe from ever writing bytecode
# into the service user's tree when run as root; on success it prints
# nothing, on failure one line naming what broke (stderr).
venv_healthy() {
    local venv_dir="$1" req_file="${2:-}"
    local py="$venv_dir/bin/python"
    if [ ! -x "$py" ]; then
        echo "venv_healthy: $py missing or not executable" >&2
        return 1
    fi
    if [ ! -f "$req_file" ]; then
        echo "venv_healthy: requirements file not readable: $req_file" >&2
        return 1
    fi
    local pkgs
    pkgs="$(grep -Eo '^[A-Za-z0-9][A-Za-z0-9._-]*' "$req_file" | tr '\n' ' ')"
    if [ -z "${pkgs// /}" ]; then
        echo "venv_healthy: no distribution names found in $req_file" >&2
        return 1
    fi
    # shellcheck disable=SC2086  # word-splitting the package names is intended
    "$py" -B -c '
import importlib, importlib.metadata, sys
bad = []
for name in sys.argv[1:]:
    try:
        importlib.metadata.distribution(name)
    except Exception:
        bad.append(name + " (distribution not installed)")
        continue
    try:
        importlib.import_module(name.split("-")[0])
    except Exception as exc:
        bad.append(name + " (import failed: " + type(exc).__name__ + ")")
if bad:
    print("venv_healthy: " + "; ".join(bad), file=sys.stderr)
    sys.exit(2)
' $pkgs || return 2
    return 0
}

# ensure_venv VENV_DIR REQUIREMENTS_FILE
#
# install.sh step 2/8's decision, verbatim behavior otherwise:
#   * venv missing            -> create it (exactly the old fresh-install path,
#                                no probe noise).
#   * venv present + healthy  -> keep it untouched.
#   * venv present + broken   -> say so plainly, rm -rf, recreate.
#   * in every case           -> the caller still runs `pip install -r`
#                                afterwards, so requirements changes keep
#                                healing in place.
# Uses the globals install.sh sets immediately before calling it
# (VENV_HOME, VENV_CACHE) and the run()/as_user() wrappers, so dry-run and
# the homeless-service-user HOME pinning behave as before.
ensure_venv() {
    local venv_dir="$1" req_file="$2"
    if [ -d "$venv_dir" ] && venv_healthy "$venv_dir" "$req_file"; then
        return 0
    fi
    if [ -d "$venv_dir" ]; then
        echo "venv interpreter mismatch/orphaned -> rebuilding $venv_dir"
        run rm -rf "$venv_dir"
    fi
    as_user env HOME="$VENV_HOME" XDG_CACHE_HOME="$VENV_CACHE" \
        python3 -m venv "$venv_dir"
}
