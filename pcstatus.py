#!/usr/bin/env python3
"""Status-detail sanitization — the ONE parent-safe mapping (shared core).

Single source of truth for the table the panel AND the session agent both
classify `sudo passwd -S` probe details with. app.py and agent.py import
this module and re-export every name below under its original spelling
(``app.status_display`` / ``agent.status_display`` / ``app.STATUS_*`` ...),
so no consumer or ``mock.patch`` target changed when the duplication was
retired (RESTRUCTURE_PLAN.md phase-1 rule: relocate code, never move names).

Why this module exists (P1 of the restructure roadmap, 2026-11-13): the
agent must never import ``app`` (its module docstring; MULTIDEVICE_PLAN
§4.1), so app.py and agent.py carried BYTE-IDENTICAL MIRROR blocks pinned
by cross-checked source tests — every edit became two edits, and any future
miss is a silent drift bug between what a hub shows and what a managed box
ships. Both sides now import this one table; there is nothing left to drift.

Bug (owner report, 2026-10-18): the child cards on the Overview showed a
warning box full of raw system text --

    sudo: pam_open_session: Permission denied
    sudo: policy plugin failed session initialization

``get_lock_status`` captures the probe's stderr verbatim whenever
``sudo passwd -S`` exits non-zero, and every payload copied that string
straight into ``status_detail``, which accountHtml()'s status pill and
``templates/user.html`` render to the parent. A parent can do nothing with
"pam_open_session"; the raw text belongs in the admin-only audit log, never
on a card.

So the RAW probe output never crosses the payload boundary. Every producer
runs ``status_display()`` and puts its RETURN VALUE in ``status_detail``.
The helper is pure (text in, text + class out) so the Jinja/JS renderers
cannot leak: by the time a row leaves the module boundary it already
carries parent-safe copy, and there is no code path left that hands raw
stderr to a template.

This module is a security boundary. It imports NOTHING but the standard
library (``audit_needed`` / ``audit_status_probe_core`` /
``status_row_core`` take the side-effecting collaborators — the audit
writer, the probe, the dedup map — as parameters, which is exactly what
keeps them patchable through the app/agent facades). Pinned by
tests/test_status_detail.py::SharedCorePurityTest.

SECURITY BOUNDARY NOTE (unchanged semantics, byte-pinned where noted):
the pattern tables and class values below must not be "tidied" — they are
what stops raw sudo/PAM/systemd stderr from reaching a parent's screen.

Dedup state and the audit/probe wiring deliberately stay per-module
(``app._LAST_STATUS_PROBE_SIGS`` / ``agent._LAST_STATUS_PROBE_SIGS`` are
separate process-local maps that tests patch and clear by name); this
module provides the shared *decision + bookkeeping* core they delegate to.
"""

# ---------------------------------------------------------------------------
# Probe outcome classes. Only UNAVAILABLE ever reaches a parent as the
# "needs help" copy; OK is a real status the pill already had; VERBATIM is
# already-human product copy that must survive untouched.
# ---------------------------------------------------------------------------

STATUS_OK = 'ok'
STATUS_UNAVAILABLE = 'unavailable'
STATUS_VERBATIM = 'verbatim'

STATUS_UNAVAILABLE_COPY = 'Status check needs help — ask your admin'

# Sentinel: "no probe ran behind this detail". Distinct from None, which
# is a real probe outcome (get_lock_status returns None for "could not
# determine") and must therefore NOT be treated as "status succeeded".
_UNSET = object()

# BENIGN NOISE. These markers mean the probe's *session* layer (sudo's PAM
# stack / logind / systemd-logind via Varlink) was unhappy while the
# password-status read itself was fine. A line containing one of these is
# DROPPED before the detail is judged: with a real status word left over
# the parent keeps the normal pill and sees no warning box at all. This is
# the exact pair the VM produced -- pam_systemd's login policy answered
# `io.systemd.Login.TooManySessions`, `passwd -S` still reported the
# account status. Matched case-insensitively against each line.
STATUS_NOISE_PATTERNS = (
    'pam_open_session',
    'pam_close_session',
    'pam_unix',
    'pam_systemd',
    'policy plugin',
    'session initialization',
    'session opened',
    'session closed',
    'toomanysessions',
    'too many sessions',
    'io.systemd.login',
    'varlink',
)

# NEVER PARENT-SAFE. Anything still carrying one of these after the noise
# lines are dropped is a genuine probe failure or a leak: a missing
# binary, a permission error, a timeout, a malformed status line, or an
# exception string with a path/cgroup in it. Treated fail-closed --
# UNAVAILABLE -- so the parent gets the friendly copy and the raw text goes
# to the audit log.
STATUS_TECHNICAL_PATTERNS = (
    'sudo',
    'pam_',
    'policy plugin',
    'cgroup',
    '/sys/',
    '/usr/',
    '/bin/',
    '/sbin/',
    '/etc/',
    '/var/',
    'errno',
    'traceback',
    'exception',
    'command not found',
    'not found',
    'no such file',
    'permission denied',
    'not permitted',
    'operation not permitted',
    'access denied',
    'authentication failure',
    'incorrect password',
    'is not in the sudoers',
    'sorry, user',
    'timed out',
    'timeout',
    'unexpected passwd output',
    'unknown status',
    'failed',
    'error',
)

# The status words `get_lock_status` produces on a successful read. A
# residual line that is one of these is a real status (OK), which is what
# keeps the parent's pill reading "Unlocked"/"Locked"/"No password" even
# when the probe also logged PAM noise.
STATUS_HUMAN_WORDS = (
    'locked',
    'unlocked',
    'no password',
    'unreachable',
)


def status_residual_lines(detail):
    """The detail's lines with the BENIGN noise lines dropped.

    Split on newlines, strip empties, and drop any line carrying a
    `STATUS_NOISE_PATTERNS` marker. Pure and total: never raises, whatever
    `detail` is (None, a number, a multi-kilobyte blob).
    """
    try:
        lines = str(detail if detail is not None else '').splitlines()
    except Exception:
        return []
    kept = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if any(pattern in low for pattern in STATUS_NOISE_PATTERNS):
            continue
        kept.append(line)
    return kept


def classify_status_detail(detail):
    """Classify one probe detail: OK / UNAVAILABLE / VERBATIM.

    The table, in order:
      1. noise lines dropped -> nothing left ......... UNAVAILABLE
         (pure PAM/systemd chatter with no usable status behind it: the
         probe told the parent nothing, so say so kindly)
      2. anything left carries a technical marker .... UNAVAILABLE
         (command not found, permission error, timeout, paths, cgroups)
      3. what is left is a known human status word .... OK
         (the normal pill, no warning box -- even when the probe also
         logged benign session noise)
      4. anything else ............................... VERBATIM
         (already-human product copy: passed through UNCHANGED)

    Pure function of the text: no I/O, no globals, no exception.
    """
    residual = status_residual_lines(detail)
    if not residual:
        return STATUS_UNAVAILABLE
    text = ' '.join(residual)
    low = text.lower()
    if any(pattern in low for pattern in STATUS_TECHNICAL_PATTERNS):
        return STATUS_UNAVAILABLE
    if any(low == word or low.startswith(word + ':')
           for word in STATUS_HUMAN_WORDS):
        return STATUS_OK
    return STATUS_VERBATIM


def status_display(detail, locked=_UNSET):
    """(parent_safe_text, klass) for one probe detail.

    This is the ONLY function that writes a `status_detail` value into a
    payload. Raw probe output can therefore not reach a rendered page for
    ANY input: OK details render the status word, UNAVAILABLE renders
    `STATUS_UNAVAILABLE_COPY`, VERBATIM renders the text unchanged
    because the table has already established it is non-technical.

    `locked` is the probe's other return value, and is consulted for exactly
    one degenerate case: the probe succeeded but produced no text at all.
    Then the status IS the answer and the parent should see it rather than
    the warning copy. Omit it for details with no probe behind them
    (remote rows, where `locked` may legitimately be None/unreachable).
    """
    residual = status_residual_lines(detail)
    klass = classify_status_detail(detail)
    if klass == STATUS_UNAVAILABLE:
        if not residual and locked is not _UNSET and locked is not None:
            # No text at all, but the probe told us a status: render it.
            return ('Locked' if locked else 'Unlocked'), STATUS_OK
        return STATUS_UNAVAILABLE_COPY, STATUS_UNAVAILABLE
    return ' '.join(residual), klass


# ---------------------------------------------------------------------------
# Admin audit hook: the shared decision + bookkeeping core.
#
# The per-module wrappers (app.audit_status_probe / agent.audit_status_probe)
# pass in their OWN dedup map and their OWN audit writer, resolved at CALL
# time, so `patch.object(app, '_LAST_STATUS_PROBE_SIGS')` and
# `patch.object(app, '_safe_audit')` keep binding the wiring exactly as they
# did when the whole body was duplicated (RESTRUCTURE_PLAN.md §1.1). The
# dedup state itself stays process-local by design, exactly like
# budgets._LAST_AUTO_PAUSE_SIGS and budgets._LAST_SUMMARY_SIGS: the Overview
# polls every 10s, so without a "log only when it CHANGES" rule one broken
# account would append an identical line forever. The per-minute enforce
# timer process keeps its own fresh state and re-emits, so a persistent
# problem still surfaces in the journal; the long-running app thread stays
# quiet until the text changes.
# ---------------------------------------------------------------------------


def audit_needed(klass, raw):
    """True when a probe outcome must (perhaps) reach the admin audit log.

    Fires for UNAVAILABLE (the probe genuinely failed) and for OK-with-
    noise (the status is fine but the probe logged session chatter) --
    both carry text that belongs in the admin log, not on a card. A plain
    OK/VERBATIM outcome is not worth a line.
    """
    if klass not in (STATUS_UNAVAILABLE, STATUS_OK):
        return False
    noisy = any(pattern in str(raw if raw is not None else '').lower()
                for pattern in STATUS_NOISE_PATTERNS)
    if klass == STATUS_OK and not noisy:
        return False
    return True


def audit_status_probe_core(username, klass, raw, state, log):
    """Deduped admin recording of one status-probe outcome.

    `state` is the caller's dedup map (username -> (klass, raw) of the last
    line written) and `log` the caller's audit writer — injected per module
    so each process keeps its own state and its own audit path (the panel
    writes the hub's chain, the agent writes its box's own). Mirrors
    budgets._audit_auto_pause_failed's `state=None` injection so tests can
    pass their own map through the module wrapper.
    """
    if not audit_needed(klass, raw):
        return False
    sig = (klass, raw)
    if state.get(username) == sig:
        return False
    state[username] = sig
    log('system', 'account.status_probe', username,
        {'ok': klass == STATUS_OK, 'class': klass, 'raw': raw})
    return True


def status_row_core(username, probe, audit):
    """(locked, parent_safe_detail, status_ok) using injected wiring.

    The single payload-boundary shape: probe once, sanitize the detail,
    hand the raw text to the module's audit hook when the class demands
    it, and return only parent-safe values. `probe` is the module's
    `get_lock_status` (the panel's is the cached read from users.py, the
    agent's is its own uncached probe — the CACHE is the accepted
    difference between the two sides; the classification here is not) and
    `audit` is the module's `audit_status_probe`, both resolved at call
    time by the thin wrappers so patches on the facade still bind.
    index(), api_users(), get_user_live() and calendar() all build their
    rows from the wrappers, so no producer can forget the mapping.
    """
    locked, detail = probe(username)
    safe, klass = status_display(detail, locked)
    audit(username, klass, detail)
    return locked, safe, klass != STATUS_UNAVAILABLE
