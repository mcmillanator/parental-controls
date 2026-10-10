# Authentication & Authorization Design — MVP

Status: **approved 2026-10-09. Not yet started.**
Date: 2026-10-09

## The problem this solves

Today the app binds `0.0.0.0:5000` with **zero authentication**, running as `stan`
with passwordless root sudo. Anyone who can reach port 5000 can:

- lock or unlock any account (`usermod -L/-U`)
- terminate any user's session (`loginctl terminate-user`) — closing their apps
- freeze any app category's cgroup

The person this app is meant to restrain (the child) is one of the accounts it can
act on, and has a browser. Today the child can lock the parent out or kill the
parent's session from their own machine. Prototype accepted this; MVP cannot.

## Approved decisions (2026-10-09)

1. **All users in the `wheel` group are admins.** Dynamic group membership, read at
   request time — not a static allowlist file.
2. **Wheel admins cannot be locked and cannot be removed.** Extends the existing
   wheel auto-lock exemption to manual lock and to removal from the admin tier.
3. **App-only admins** may also exist: accounts in the app with no corresponding
   system account. These are less trusted.
4. **App-only admins can be removed by wheel admins.** App-only admins cannot
   remove anyone (including each other) and cannot lock wheel admins.
5. **Idle timeout 30 min** — confirmed.
6. **localhost + SSH tunnel** for now. No LAN binding, no TLS in MVP.
7. **Force a password change on first login**, including for the generated
   initial credential.
8. **SUPERSEDED (2026-10-09): system-password auth is rejected.** Wheel admins do
   **not** log in with their Linux password. See "System-password auth — rejected"
   below. All admins — wheel and app-only — authenticate with app credentials.
9. **The PAM helper is excluded from every version of this project** — MVP and all
   later phases. Not deferred, not on any roadmap.

## Two tiers of admin

| | Wheel admin | App-only admin |
|---|---|---|
| Source | Member of system `wheel` group | Row in `auth.json` with no Linux UID |
| Credential | App password only | App password only |
| Can lock/terminate/freeze any non-wheel user | Yes | Yes |
| Can lock a wheel admin | **Never** | Never |
| Can edit another admin's account | No | No |
| Can remove an app-only admin | **Yes** | No |
| Can remove a wheel admin | **Never** | Never |
| Revocation | Automatic (removed from `wheel`) | Manual, by a wheel admin |

Authorization is evaluated **per request** from live group membership, so removing
someone from `wheel` takes effect immediately without restarting the app.

## Non-negotiable principles

**The child's account must never authenticate.** Not read-only, not "hidden
buttons", not "view only". Every capability here is a privilege over the child's
account, so a single authenticated child session defeats the product.

**A forgotten password must never lock the parent out of their machine.** Recovery
must not depend on the web UI — see CLI recovery below.

**Binding to localhost is load-bearing for auth safety.** It removes the entire
LAN from the brute-force surface; only someone with a shell on the box can reach
the app at all.

## System-password auth — REJECTED (decided 2026-10-09)

**Wheel admins will not log in with their Linux/system password.** Every admin,
regardless of tier, authenticates with an app credential. There is no PAM helper in
this project, in MVP, or in any later phase.

The analysis below is retained as the record of *why*, so this is not re-litigated.

PAM requires reading `/etc/shadow`, i.e. root. The app runs as `stan`, and running
it as root would put the entire HTTP surface, its templates, and its parsing paths
one bug away from full root. The only technically-secure route was `unix_chkpwd`-
style: a tiny, separately-audited root-owned helper calling `pam_authenticate()`,
returning exit status only. That is a new security-sensitive setuid binary living
outside the app's release cadence — a permanent, if small, privilege surface on a
machine whose whole job is to hand out privilege carefully.

| Approach | Verdict |
|---|---|
| `pamela` / python PAM bindings | Rejected — needs root in the app process |
| Run the app as root | Rejected — unacceptable blast radius |
| Sudo-gated password check | Not a thing; sudo authenticates the operator, not an arbitrary target user |
| Minimal setuid PAM helper | **Rejected by owner 2026-10-09 — excluded from all versions** |

Cost/benefit did not favour shipping and maintaining a setuid binary forever. The
simplicity of one uniform credential model (app password for everyone) is worth
more here than reusing the system password.

**Consequence to accept:** a wheel admin now has two passwords — their Linux one
and an app one. This is the same trade-off as a hardware key or a separate admin
account, and it is the standard shape for self-hosted control panels.

Brute-force protection: exponential backoff per
source address, plus a lockout-with-notification after repeated failures.

## Credential model (app-password tier)

| Concern | Detail |
|---|---|
| Store | `argon2` hashes, `/etc/parental-controls/auth.json`, mode `0600`, owned by `root` |
| Initial credential | Generated on first run, printed once to the journal + service stdout, flagged `must_change` |
| First login | Forces a password change before any other route is reachable |
| Identity | App username; the wheel tier resolves identity from the system account |
| Hashing library | `argon2-cffi` if available, else `passlib`; if neither is installed, **fail closed** rather than compare plaintext |

## Session model

- Server-side sessions. Flask's signed cookie carries **only** an opaque session ID;
  session data lives in memory with a TTL and is lost on restart.
- Cookie flags: `HttpOnly`, `SameSite=Lax`, `Secure` when TLS exists (Phase 3).
- Idle timeout **30 min**, absolute lifetime **12 h**, both server-enforced and
  measured with monotonic time (not wall clock, to avoid clock-skew exploits).
- Logout destroys the server session and clears the cookie.
- A restart invalidates all sessions; parents re-login. Acceptable.

## Request handling

A single `@require_admin` decorator on **every** page and **every** API route:

1. Unauthenticated → `302 /login` for pages, `401` JSON for `/api/*`.
2. Authenticated but not an admin → `403`.
3. **Target is a wheel admin and the action is lock/remove** → `403`, even for a
   wheel admin. Refused in the handler, not just hidden in the UI.
4. Locked admin attempting login → refused, with the CLI recovery command printed
   in the message.
5. Username validation unchanged: `^[A-Za-z0-9_.-]+$`, must exist in `/etc/passwd`.

## CSRF

Token per session; every state-changing request must echo it. AJAX sends it via
`X-CSRF-Token` (the templates already use `fetch`, so this is a small change).
`SameSite=Lax` is a second layer, never the only one.

## CLI recovery (the can't-lock-yourself-out guarantee)

Works even if the app is wedged or the credential is lost:

```bash
sudo parental-controls unlock <username>     # unlock any non-wheel account
sudo parental-controls unfreeze-all          # thaw every frozen category cgroup
sudo parental-controls reset-password <admin>
sudo parental-controls add-admin <username>  # app-only admin
sudo parental-controls remove-admin <username>
```

This ships in the same change as auth.

## Failure modes

| Scenario | Behavior |
|---|---|
| Credential file missing/corrupt | Fail closed, refuse to start, log clearly |
| Wheel admin forgets the app password | CLI `reset-password`; their Linux account is unaffected |
| App restart | Sessions invalidated, re-login required |
| Admin's own account locked by the app | Cannot happen (wheel admins are exempt); if it somehow occurs, CLI recovery applies |
| Admin removed from `wheel` | Access ends on the next request |
| Auth store write fails | Refuse to start rather than run with unknown auth state |

## Build order

1. `admins.json` + `auth.json`, `argon2` store, fail-closed startup
2. Login page, server-side sessions, `require_admin` on every route
3. Wheel-admin invariants (cannot lock/remove wheel admins, anywhere in the code)
4. CSRF protection
5. `127.0.0.1` default bind + `--allow-lan` opt-in flag
6. CLI recovery command
7. gunicorn/waitress + systemd hardening
8. Freezer watchdog
9. Tests: every denial path — unauthenticated, child account, app-only admin
   attempting privileged actions, tampered session, missing CSRF, revoked wheel
   member, lock/remove of a wheel admin