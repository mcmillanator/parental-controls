#!/usr/bin/env python3
"""Shared per-box target policy for the parental-controls app + agent.

MULTIDEVICE_PLAN.md §4.4 / §5.1.1: every refusal that protects an account
(wheel members can never be locked/terminated/removed; admin-protected
accounts can never be frozen or budget-managed) must be evaluated by the
box that would *execute* the action, against that box's live system state.

This module is deliberately stdlib-only and Flask-free so the same decision
is reachable from:

  * `app.py` — the session-authenticated local web views, and
  * `agent.py` — the bearer-token-authenticated machine link.

`auth.is_wheel_member` and `budgets.is_admin_protected` read the *local*
wheel group / local admin list every call, so a hub admin's own wheel
membership never widens what may be done to another box's admins.
"""

import auth
import budgets


def valid_username(username):
    """True when `username` is safe to pass as a literal argv element.

    Mirrors the historical app.valid_username() rule: a non-empty name,
    no leading '-' (usermod would parse it as an option), and only
    [A-Za-z0-9_.-]. No real account can begin with '-'.
    """
    return bool(username) and not str(username).startswith('-') and all(
        c.isalnum() or c in '_-.' for c in str(username)
    )


def wheel_refusal(username):
    """Reason string when `username` is a wheel member of THIS box, else None.

    Wheel admins can never be locked, terminated, or removed (AUTH_DESIGN
    "Non-negotiable"), regardless of which box the request came from.
    """
    if auth.is_wheel_member(username):
        return (f'Refused: {username} is a wheel admin and can never be '
                'locked, terminated, or removed.')
    return None


def freeze_refusal(username):
    """Wheel + admin-protected refusal for freeze/unfreeze, else None."""
    refused = wheel_refusal(username)
    if refused is not None:
        return refused
    if budgets.is_admin_protected(username):
        return f'Refused: {username} is an admin and cannot be frozen'
    return None


def budget_config_refusal(username):
    """Wheel + admin-protected refusal for budget config writers, else None."""
    refused = wheel_refusal(username)
    if refused is not None:
        return refused
    if budgets.is_admin_protected(username):
        return f'Refused: {username} is an admin and cannot be budget-managed'
    return None
