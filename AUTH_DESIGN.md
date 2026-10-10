# Authentication & Authorization Design — MVP

Status: **proposal, awaiting owner approval.** No code written yet.
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

## Non-negotiable principle

**The child's account must never authenticate to this app.** Not read-only, not
"view only", not "hidden buttons". Every capability here — lock, unlock, logout,
freeze, budget editing — is a privilege over the child's account. A single
authenticated child session defeats the entire product.

Second principle: **a forgotten password must never lock the parent out of their
own machine.** Recovery must exist that does not depend on the web UI.

## Credential model

Separate the app's own identity from Linux accounts:

| Concept | Detail |
|---|---|
| Admin accounts | Explicit allowlist, file-backed: `admins.json` → `{"admins": ["stan"]}` |
| Credential store | `argon2` hashes in `/etc/parental-controls/auth.json`, mode `0600`, owned by `root` |
| Login identity | App-specific username + password. **Not** the Linux password |
| Why not PAM/Linux auth | App creds are revocable and auditable without touching the child account; PAM would mean reading `/etc/shadow`, and locking the app must never be conflated with locking the OS account |
| Initial credential | Generated on first run, printed once to the journal + service stdout, e.g. `admin / <random 24-char>`, force-change on first login |

Passwords verified with `argon2-cffi` if available, else `passlib`; if neither is
installed, **fail closed** (refuse to start) rather than fall back to plaintext
comparison.

## Session model

- Server-side sessions. Flask `session` (signed cookie) holds only an opaque
  session ID; session data lives in memory (`dict` + TTL), lost on restart.
- Cookie: `HttpOnly`, `SameSite=Lax`, `Secure` when TLS is enabled (Phase 3).
- Idle timeout **30 min**, absolute lifetime **12 h**. Both server-enforced.
- Logout deletes the server session and clears the cookie.
- On restart, all sessions die (acceptable: parents re-login).

## Authorization rules

Enforced in a `@require_admin` decorator on **every** page and **every** API route:

1. Not authenticated → `302` to `/login` for pages, `401` JSON for `/api/*`.
2. Authenticated principal not in the admin allowlist → `403`. (Defense in depth:
   the allowlist is checked at every request, not just at login, so revoking
   access takes effect immediately.)
3. **Locked accounts cannot log in.** If the admin's own account is currently
   locked by this app, login is refused with a clear message pointing at the CLI
   recovery command.
4. Target validation unchanged: usernames still validated against
   `^[A-Za-z0-9_.-]+$` and matched against `/etc/passwd`.

### What the child sees
Login page and nothing else. Every route returns `403`. They cannot read the user
list, process lists, budgets, or invoke any action.

## CSRF

Flask-WTF CSRFProtect, or hand-rolled double-submit if dependency weight matters:

- Token in a per-session value; every `POST` must echo it in a header or form field.
- AJAX calls send it via the `X-CSRF-Token` header (templates already use `fetch`,
  so this is a small change).
- `SameSite=Lax` is a second layer, not the only one.

## Network exposure

- Default bind `127.0.0.1`.
- `--allow-lan` opt-in flag (systemd drop-in) binds `0.0.0.0`.
- **Recommendation: keep it on localhost.** Access it over SSH port-forward, or
  put it behind a reverse proxy with TLS (Phase 3). LAN exposure without TLS
  means the session cookie and admin password travel in the clear.

## CLI recovery (the "can't lock yourself out" guarantee)

Available without the web UI, works even if the app is wedged or the admin
password is lost:

```bash
# Unlock any account
sudo /usr/local/bin/parental-controls unlock <username>

# Thaw every frozen category cgroup
sudo /usr/local/bin/parental-controls unfreeze-all

# Reset the admin credential
sudo /usr/local/bin/parental-controls reset-password
```

This is why recovery doesn't depend on the UI. It is the single most important
piece of the MVP work and should ship in the same change as auth.

## Failure modes

| Scenario | Behavior |
|---|---|
| Credential file missing/corrupt | Fail closed, refuse to start, log clearly |
| App restarts | All sessions invalidated, parents re-login |
| Admin account locked by the app | Login refused; CLI recovery documented in the error |
| Admin removed from allowlist | Access ends on next request |
| Clock skew | Idle timeout uses monotonic time, not wall clock |

## Build order

1. `argon2` credential store + `admins.json` + fail-closed startup
2. Login page + server-side sessions + `require_admin` on every route
3. CSRF protection
4. `127.0.0.1` default bind + `--allow-lan`
5. CLI recovery command
6. gunicorn/waitress + systemd hardening
7. Freezer watchdog
8. Tests: auth bypass attempts (unauth, child-account, tampered session, missing
   CSRF, revoked admin), each asserting denial

## Open questions for the owner

1. **How many admins?** Just `stan`, or a second parent account?
2. **Should admins be allowed to lock themselves?** Currently yes (behind a
   confirm). Recommend: forbid manual self-lock, same as auto-lock.
3. **Idle timeout** — is 30 min right, or do you want longer?
4. **Access method** — localhost + SSH tunnel (recommended), or genuine LAN
   access with TLS now rather than in Phase 3?
5. **Force password change on first login**, or accept the generated one as-is?