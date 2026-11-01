#!/usr/bin/env python3
"""Capability probes + gate decorators for the test suite.

Mission (2026-11-01): `python -m unittest discover -s tests` must be OK on
ANY machine. Tests whose assertions depend on the box itself (sudoers
resolution needs real binaries, PAM needs a real system, installer probes
need systemd) SKIP here instead of FAILing, and still RUN unchanged on the
authoritative Bazzite VM where the capabilities are present.

Invariant on every box: a test either runs (its capabilities are there)
or skips with a greppable reason. All reasons produced by this module
start with `caps:` so baseline logs can be diffed with `grep 'caps:'`.

Stdlib only, cheap side-effect-free probes, evaluated at most once per
process (lru_cache). Extends — not replaces — the suite's existing
idioms (`HAS_OPENSSL`/`HAS_CHEROOT` booleans, `shutil.which` guards).

The DANGEROUS gate
------------------
Tests that mutate real system state (useradd/usermod -L/-U, PAM auth via
`su -`, cgroup freeze) must NEVER run outside the Bazzite test VM —
mere presence of the binaries is not enough, a Fedora laptop has them
too. `is_bazzite_vm()` is a conservative conjunction:

1. hostname (first label) == ``VM_HOSTNAME`` ('opencode-vm'), and
2. the install dir ``VM_INSTALL_DIR`` ('/opt/parental-controls') exists,
3. and the agent account ``VM_AGENT_USER`` ('pcagent') has a pwd entry.

Any single check can occur on a dev box; all three together is the VM.
When in doubt the probes are false and the dangerous test skips. If the
VM is ever renamed or rebuilt, update these constants (they live here
only, in one place).
"""

import functools
import glob
import os
import pwd
import shutil
import socket
import unittest

VM_HOSTNAME = 'opencode-vm'
VM_INSTALL_DIR = '/opt/parental-controls'
VM_AGENT_USER = 'pcagent'

#: Prefix every skip reason carries (greppable in baseline logs).
REASON_PREFIX = 'caps:'


# ---------------------------------------------------------------------------
# Binaries
# ---------------------------------------------------------------------------

def missing_binaries(*names):
    """Sorted names among `names` that are not on PATH on this box."""
    return sorted(n for n in names if shutil.which(n) is None)


def has_binaries(*names):
    """True if every one of `names` is resolvable on this box."""
    return not missing_binaries(*names)


def missing_binaries_in(search_dirs, *names):
    """Like missing_binaries() but resolves `names` through `search_dirs`
    (sudo's secure_path semantics: only an existing file counts)."""
    out = []
    for name in names:
        if '/' in name:
            if not os.path.exists(name):
                out.append(name)
            continue
        if not any(os.path.exists(os.path.join(d, name))
                   for d in search_dirs):
            out.append(name)
    return sorted(out)


def needs_binaries(*names):
    """Class/method decorator: skip unless all `names` exist on PATH.

    Reason lists exactly what is absent, e.g.
    `caps: missing binaries: loginctl, systemd-run`.
    """
    missing = missing_binaries(*names)
    reason = (f'{REASON_PREFIX} missing binaries: ' + ', '.join(missing)
              if missing else f'{REASON_PREFIX} binaries present')
    return unittest.skipUnless(not missing, reason)


# ---------------------------------------------------------------------------
# The Bazzite test VM (DANGEROUS live-system tests)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def vm_identity():
    """(present_checks, missing_checks) for the VM conjunction.

    Each check is a short label; the skip reason names the ones that
    failed so an operator sees WHY the box was not trusted.
    """
    host = socket.gethostname().split('.')[0]
    checks = [
        (f'hostname={VM_HOSTNAME}', host == VM_HOSTNAME),
        (f'install-dir={VM_INSTALL_DIR}', os.path.isdir(VM_INSTALL_DIR)),
        (f'user={VM_AGENT_USER}', _has_user(VM_AGENT_USER)),
    ]
    present = [label for label, ok in checks if ok]
    missing = [label for label, ok in checks if not ok]
    return present, missing


def _has_user(name):
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def is_bazzite_vm():
    """True only on the authoritative Bazzite test VM (see module doc)."""
    present, _missing = vm_identity()
    return len(present) == 3


def needs_vm(what):
    """Decorator for tests that mutate the real system (accounts, PAM,
    cgroups). Skips everywhere except the VM; on the VM it behaves as if
    undecorated. `what` names the mutation for the reason string."""
    _present, missing = vm_identity()
    detail = ('; not this box: ' + ', '.join(missing)) if missing else ''
    return unittest.skipUnless(
        is_bazzite_vm(),
        f'{REASON_PREFIX} VM-only ({what}){detail}')


# ---------------------------------------------------------------------------
# Desktop-entry data (real installed apps, VM-side data)
# ---------------------------------------------------------------------------

DEFAULT_DESKTOP_DIRS = (
    '/usr/share/applications',
    '/var/lib/flatpak/exports/share/applications',
    os.path.expanduser('~/.local/share/flatpak/exports/share/applications'),
)


def has_desktop_entry(prog, dirs=None):
    """True if some *.desktop file named like `prog` is installed.

    Mirrors budgets_attribution's scan dirs. Checks file NAMES only — a
    present entry whose classification regressed must still FAIL (that
    is the bug the gated test guards), so this is a data probe, not a
    result probe.
    """
    for d in (dirs or DEFAULT_DESKTOP_DIRS):
        try:
            matches = [p for p in glob.glob(os.path.join(d, '*.desktop'))
                       if prog.lower() in os.path.basename(p).lower()]
        except OSError:
            continue
        if matches:
            return True
    return False


def needs_desktop_entry(prog, dirs=None):
    """Decorator: skip unless a `.desktop` entry for `prog` exists on
    this box (installed-app data, not a binary)."""
    found = has_desktop_entry(prog, dirs)
    return unittest.skipUnless(
        found,
        f'{REASON_PREFIX} no .desktop entry for {prog!r} installed here')
