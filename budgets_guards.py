"""Validation and admin self-protection — the guards every name passes first.

`is_admin_protected` is the lockout safeguard: a member of the admin
(`wheel`) group is never auto-locked by bedtime nor auto-frozen by budget
enforcement. `valid_username`, `valid_category` and `valid_comm` are the
charset / allow-list guards that run BEFORE a name reaches a cgroup path, a
sudo argv or an override key. Rule-based, no hardcoded names.

Moved verbatim out of budgets.py by RESTRUCTURE_PLAN.md slice 2.

**Cross-seam convention (the whole `budgets_*` family follows it).** This
module never does `from budgets import x`. Where a name that stayed in the
facade is needed, the function body opens with `import budgets` and calls
`budgets.x` — resolved at CALL time, so the suite's `mock.patch` sites on the
facade (`budgets.grp`, `budgets.pwd`, …) keep reaching this code, and no
module-level edge back to the facade can ever form an import cycle
(plan §1.1, §2.3).
"""

# ---------------------------------------------------------------------------
# Admin self-protection (lockout safeguard, MVP)
# ---------------------------------------------------------------------------
# Rule-based, no hardcoded names: members of the admin (`wheel`) group are
# never auto-locked by bedtime enforcement nor auto-frozen by budget
# enforcement. Manual lock via the UI still works (behind its confirm
# dialog). Every skip logs a note so it is visible in the service journal.

ADMIN_GROUP = 'wheel'


def is_admin_protected(username):
    """True if `username` is an admin (member of the `wheel` group).

    Covers both supplementary membership (gr_mem) and a wheel primary GID.
    Returns False when the group or user is unknown.
    """
    import budgets
    if not username:
        return False
    try:
        wheel = budgets.grp.getgrnam(ADMIN_GROUP)
    except Exception:
        return False
    if username in wheel.gr_mem:
        return True
    try:
        return budgets.pwd.getpwnam(username).pw_gid == wheel.gr_gid
    except Exception:
        return False


def valid_username(username):
    """Guard against path traversal before a username touches cgroup paths.

    Beyond the charset check this rejects a leading '-': usernames reach
    privileged commands as bare argv elements, and a name like '-x'
    would be parsed as an option rather than a login. No real account
    can begin with '-' (mirrors app.valid_username), so this costs
    nothing and closes an argument-injection path.
    """
    import budgets
    return (bool(username) and not username.startswith('-')
            and bool(budgets._USERNAME_RE.match(username)))


def valid_category(category):
    import budgets
    return category in budgets.CATEGORIES


def valid_comm(comm):
    """Guard for per-app override keys and ``parental-<user>-app-<comm>``.

    comm originates from ``ps -o comm`` (already truncated to 15 chars)
    and reaches privileged cgroup paths and argv as a bare element, so it
    must stay inside the same charset the sudoers grant is anchored on
    (``[A-Za-z0-9_.-]``, no leading ``-``), be non-empty and not made
    only of dots (``.``/``..`` would be path segments), and fit a real
    comm (1-15 chars). Checked BEFORE any cgroup path/argv construction.
    """
    import budgets
    if not comm or len(comm) > 15:
        return False
    if comm.startswith('-') or not budgets._COMM_RE.match(comm):
        return False
    if not comm.strip('.'):
        return False
    return True
