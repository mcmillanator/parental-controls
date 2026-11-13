#!/usr/bin/env python3
"""logind session probes via scoped sudo — shared panel/agent core.

The two privileged reads the Overview's active-session counts and the
screen-lock path are built on:

* ``_user_session_ids``  — ``sudo loginctl show-user <u> -p Sessions --value``
* ``_session_properties`` — ``sudo loginctl show-session <id> -p Class -p Type``

Both argvs are EXISTING scoped-allowlist entries
(``sudoers-parental-controls``; the second is the screen-lock filter's
rule), so neither the panel nor the agent gains a privilege from reading
sessions — and no new sudoers rule may ever appear here without the
sudoers templates changing too (pinned: ``test_deployment`` scans this
file's sudo argv, ``test_ui_session_counts`` pins the exact two literals
and the pinned grant set).

Why this module exists (P1 of the restructure roadmap, 2026-11-13):
app.py and agent.py carried byte-identical copies ("Mirrors app..." in
the agent), the classic copy-paste drift hazard. app.py and agent.py now
import and re-export both helpers under their original names —
``app._user_session_ids`` / ``agent._user_session_ids`` etc. still resolve
as attributes, and locks.py keeps calling ``app._user_session_ids`` at
call time (RESTRUCTURE_PLAN.md §1.1).

Stdlib only (``subprocess``). The ``subprocess.run`` reference resolves
through THIS module's import of the stdlib module — the same module object
every test patches with ``patch.object(mod.subprocess, 'run', ...)``
(attribute form, 65+ sites), so fakes installed through app or agent reach
this code exactly as they reached the inlined copies.

Timeout semantics are load-bearing: a timeout is RE-RAISED so the callers
that report it (the screen lock's "screen lock timed out" note) still say
so; every other failure degrades quietly because a cosmetic counter must
never break a render or an audit path.
"""

import subprocess


def _user_session_ids(username):
    """The logind session IDs currently attached to `username`.

    One `sudo loginctl show-user <u> -p Sessions --value` call — already on
    the scoped allowlist (sudoers-parental-controls), so reading sessions
    costs the service user no new privilege. Returns [] when the account is
    offline, logind refuses the call or anything else goes wrong; never
    raises. A timeout is re-raised so a caller that reports it (the screen
    lock) still says so.

    The screen-lock consumer lives in locks.py and reaches this helper as
    `app._user_session_ids` at call time (RESTRUCTURE_PLAN.md §1.1);
    agent.py re-exports the same object.
    """
    try:
        result = subprocess.run(
            ['sudo', 'loginctl', 'show-user', username, '-p', 'Sessions',
             '--value'],
            capture_output=True, text=True, timeout=15
        )
        return result.stdout.split() if result.returncode == 0 else []
    except subprocess.TimeoutExpired:
        raise
    except Exception:
        return []


def _session_properties(session_id):
    """`{'Class': ..., 'Type': ...}` for one logind session, {} if unknown.

    One `sudo loginctl show-session <id> -p Class -p Type` call — the other
    scoped allowlist entry, shared by the screen-lock filter and the
    active-session count so neither adds a rule. A vanished session, a
    refused probe or an empty answer is {}; a timeout is re-raised for the
    same reason as `_user_session_ids`. (Consumed by locks.py through
    `app._session_properties`, call time, per §1.1; agent.py re-exports
    the same object.)

    WHAT COUNTS downstream: logind reports one extra `Class=manager`
    session for every user running a systemd user manager (verified live:
    bluefox -> "21 20", where 21 is unspecified/manager and 20 is
    wayland/user). A manager session is not a login, so the active-session
    count keeps only `Class=user` — the same exclusion the screen-lock
    filter already applies.
    """
    try:
        result = subprocess.run(
            ['sudo', 'loginctl', 'show-session', session_id,
             '-p', 'Class', '-p', 'Type'],
            capture_output=True, text=True, timeout=15
        )
    except subprocess.TimeoutExpired:
        raise
    except Exception:
        return {}
    if result.returncode != 0:
        return {}
    props = {}
    for line in result.stdout.splitlines():
        if '=' in line:
            key, _, value = line.partition('=')
            props[key.strip()] = value.strip()
    return props
