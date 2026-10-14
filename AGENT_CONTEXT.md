# AGENT_CONTEXT.md — handoff for the next agent

Current state of Bazzite Parental Controls, and the things that are **not**
derivable from the code. Read this plus `AGENTS.md` before touching anything.

Last updated: 2026-10-14, after three local workstreams were merged to `master`
but **not yet pushed or deployed**: the tablet-band sidebar token, the
row-launched Processes override dialog, and the Overview no-JS POST forms.

## ⚠️ Moving machines: what does NOT come with a clone

These are **untracked** and live only on the old box. Copy them by hand:

| File | Why it matters |
|---|---|
| `<project root>/AGENTS.md` | The safety rules for a **live** desktop (never touch `tester`/`bluefox`/`stan`, don't restart the service, worktree etiquette, in-project scratch). Nothing here replaces it. |
| `<project root>/opencode.json` | Project config: the Flowstep MCP server + `oauth.callback_port` + external-directory permissions. |
| `master/.venv/` | Rebuild: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` (Flask 3.1.x, argon2-cffi, waitress — Python 3.14 here). Feature worktrees carry a `.venv` **symlink** → `../master/.venv`; it is gitignored, never commit it. |

The bare repo root must stay code-free — all code lives in worktrees
(`master/`, plus any feature worktree). Recreate one with
`git worktree add ../<name> <branch>`.

After cloning, re-auth the MCP: `opencode mcp list` → if flowstep shows
"needs authentication", run `/mcps`, select it, sign in. The OAuth callback is a
loopback listener on the machine running OpenCode — if you browse from another
host, tunnel that port.

## Where the durable knowledge lives

- `UI_UX_PLAN.md` — **§11 "Design translation — Flowstep canvas alignment"** is
  the authoritative record of the design system, the shell/IA, per-screen shape,
  the selector contract, and §11.8 (design-intent notes). §7.4 pins the
  selectors that tests and Katalon depend on — **preserve them**.
- `PLAN.md` — overall plan + current suite size. `README.md` — usage/deploy.
- `QA_LOCK_UNLOCK.md` — lock/unlock QA procedures.

## Source of truth for the UI: the Flowstep canvas

The six hi-fi screens are the design spec — via the Flowstep MCP, not pasted
exports (a `design-exports/` directory was deleted on purpose; don't recreate it).

- fileId `216e5d0f-761c-40a9-ad76-bbd4bb8b8d0d`, all six `colorTheme: blueDark`.
- S1 Overview `264a26a1…` (phone) · S2 Time rules `3a65058f…` (phone) ·
  S3 Time budgets `5b016ff4…` (tablet) · S4 Processes `4a19bb75…` (tablet) ·
  S5 Admin console `2a9bf8c0…` (desktop) · S6 Settings `37be5ee8…` (desktop)
- Each screen's **`prompt` carries implementation-intent notes**; chat message
  **sequence 32** is the owner's full starter prompt. Read both before changing
  a screen — that's how the fixtures/dialog work below was derived.
- **Treat the owner's canvas as read-only.** Never `ai-*`, `replace-screen-jsx`,
  `delete-*` on it. To sample another theme, create a throwaway file, add a
  screen with the copied JSX + `fragments` and a different `colorTheme`, render
  it, then delete the file.
- The canvas JSX is React/Tailwind with **shadcn semantic tokens and no hex**
  (`bg-background`, `bg-card`, `bg-sidebar`, `bg-primary`, `bg-accent`,
  `bg-muted`, `border-input`, `ring-ring`, `bg-destructive`, …). Colours must be
  sampled from `get-screen-image` renders — guessing caused a palette drift once.

## Design decisions now locked in

- Stack: **Flask + Jinja + vanilla JS + locally vendored Bootstrap 5**, no
  framework, **no build step**, no CDN/external assets. Progressive enhancement
  only (`static/js/app.js`); every action must work as a plain server-rendered
  POST.
- Palette: dark = Flowstep `blueDark` (`#0a0a0b` / surfaces `#18181b`, primary
  `#2563eb`), light = the paired `blue`. Token **names** are stable; only values
  move. Active nav is a **solid blue** row (verified against the canvas).
- Theme **follows the OS** (`:root` is dark, `[data-theme=light]` +
  `prefers-color-scheme`), plus a persisted manual toggle. Owner chose this over
  defaulting to light.
- **Accessibility beats pixel-fidelity**: where a canvas colour fails WCAG AA,
  pick the nearest AA-safe shade and document it. `tests/test_ui_a11y.py`
  computes contrast ratios and must stay green.
- **All six screens use the shared shell** (sidebar + app bar; bottom tab bar
  below the breakpoint). The Overview's phone-first special case was removed at
  the owner's request.
- **Annotation cards, state/theme specimens and dialog examples are design
  fixtures, not production content.** They render only behind `?preview=1` (or
  `PARENTAL_DEV_PREVIEW=1`). Destructive/access-changing actions all route
  through the shared confirmation-dialog macro in `templates/_partials/dialog.html`
  (`window.pcConfirm`, optional typed `REMOVE` / checkbox gate); the app-override
  uses the shared `#pc-override` dialog.
- Mobile: no horizontal overflow at 320/375 — guarded by `.pc-chip`/`.pc-pill`/
  `.badge-*` `max-width:100%` + wrapping and `.pc-table` `min-width:0`;
  dense tables become labelled cards. `tests/test_ui_assets.py` pins these.

## Verifying work

```sh
mkdir -p .scratch && TMPDIR="$PWD/.scratch" .venv/bin/python -m unittest discover -s tests
```
All scratch stays **inside** the worktree (`.scratch/` is gitignored); never
`/tmp`. Last known: **1247 tests, OK, 14 skips**. `tests/test_katalon_objects.py`
is the selector contract — keep it green and avoid changing `katalon/objects/*`.

There is **no browser installed**. A Playwright screenshot harness was used for
the design work and was deleted with its worktree; to rebuild it in `.scratch/`:

```sh
python3 -m venv .scratch/qa-venv && .scratch/qa-venv/bin/pip install playwright pillow
PLAYWRIGHT_BROWSERS_PATH="$PWD/.scratch/pw-browsers" .scratch/qa-venv/bin/python -m playwright install chromium
```
…then boot a throwaway instance against an isolated auth store and shoot every
route at phone/tablet/desktop, light and dark:

```sh
PARENTAL_CONTROLS_AUTH=$PWD/.scratch/preview-auth.json .venv/bin/python app.py --port 5050
```
`PARENTAL_CONTROLS_AUTH` overrides the auth-store path (`auth.py`), so a preview
never touches `/etc/parental-controls/auth.json`. Seed it with
`auth.hash_password()` and `must_change: False` to skip the forced-password
redirect. Avoid ports **5060/5061** — Chromium blocks them. Screenshots are the
owner's preferred review artifact (they browse remotely; no port forwarding).

## Deploy (live box — owner runs it, or supervises it)

Merge to `master`, then `sudo ./install.sh` from the master worktree: stages code
to `/opt/parental-controls`, preserves runtime state, renders units, validates
sudoers with `visudo -c`, restarts the service. Smoke-test `curl localhost:5000/`
→ expect **302** to `/login`. Never restart `parental-controls.service` by hand;
never serve the panel from a checkout or as `stan`. Runtime state is SQLite
`state.db` (+ JSON mirrors); the service runs as the `parental-controls` system
user; a healthcheck timer probes every 5 min. Recovery CLI on `PATH`:
`sudo parental-controls {unlock,unfreeze-all,reset-password,add-admin,remove-admin,lock-admin}`.

## Open / deferred

- **Overview actions are a real no-JS POST path** (resolved): every account
  action (Lock / Unlock / Lock screen / Emergency logout) and the admin
  add/remove controls are `<form method="post">` submissions to `/overview/*`,
  which run the same cores as the `/api/*` twins, flash and redirect (PRG); JS
  only adds the confirm dialog. `#admin-add` / `#new-admin-username` stay
  visible for every admin because Katalon asserts them on `/` (the endpoint
  refuses non-wheel actors server-side). Two live-box checks: the new
  `app.secret_key` (per-process random, `PARENTAL_SECRET_KEY` overridable) makes
  Flask's `session` cookie appear alongside `pc_sid`, and the one-time admin
  password rides in that signed (not encrypted) cookie until the redirect
  renders — confirm that is acceptable or add a session-cookie policy.
- **Processes override is a row-launched modal** (resolved): the inline editor
  card is gone; the row's Override button opens the shared `#pc-override`
  dialog (`_partials/override_dialog.html`).
- Add-app-only-admin is still a `<details>` disclosure on the audit page, not
  the shared dialog.
- **No-JS coverage is incomplete outside the Overview:** `budgets`, `processes`
  and `admin_audit` (and much of `user`) still post via `fetch` + a CSRF
  header. §11.1's "server-rendered POST/fetch path" wording is weaker than the
  claim in `static/js/app.js` ("with JavaScript disabled the pages still render,
  navigate and submit"). Whether to converge the rest — or soften the docs — is
  an open owner question; not started.
- Honest placeholders: "1 active session" and specimen values are illustrative;
  per-account "using 1h 42m today" isn't in the payload; notification lead-time,
  the HTTPS toggle and multi-device are disabled.
- Tablet (768–959) sidebar sizing is **resolved** — `app.css` pins the tablet
  band to `--sidebar-w: 236px` (canvas S3/S4); see `UI_UX_PLAN.md` §11.9.
- Multi-device implementation is parked (owner: not soon). The session agent is
  installed per child only with `--session-user`; the remote agent needs
  `--enable-agent`.
- Stale remote branches still on origin: `empty`, `feature/bootstrap`,
  `feature/freezer-watchdog`, `feature/mvp-auth`. `research/process-filtering`
  has a live worktree.

## Agent container vs the live box (two separate checkouts)

There are **two separate checkouts** of this repo — it is not a bind mount:

| Where | Path | Notes |
|---|---|---|
| OpenCode container | `/mnt/Documents/Projects/bazzite-parental-controls-2` | its own repo + `.venv`; Python **3.13.5**; no `sudo`, `pexpect` or browser |
| The original VM | `stan@opencode-vm:/var/home/stan/Documents/Projects/bazzite-parental-controls-2` | separate repo + worktrees; its own `.venv` on Python **3.14.8**; passwordless `sudo`; `pcagent`/`pcverify` present; **no browser** |

SSH to the VM works (`ssh stan@opencode-vm`). Sync with a bundle (no remote-side
ref churn): on the container `git bundle create .scratch/w.bundle <old>..master`
then `scp` it over, and on the VM
`git -C master fetch <bundle> master && git -C master merge --ff-only FETCH_HEAD`.
Do **not** push to `origin` from either side without the owner's say-so.

- A full `discover` run is **not** green in the container (~109 failures + 1
  error at baseline, all environment-bound: `test_deployment`,
  `test_multidevice_install`, `test_session_agent`, `test_budgets`,
  `test_lock_usermod_real`). The container's green signal is the `test_ui_*`
  set plus `test_katalon_objects`, `test_ui_a11y`, `test_ui_assets`.
- The VM is the authoritative environment: **1278 tests, OK, 14 skips** at
  `25342c6` (Python 3.14.8, live PAM/usermod included). Run it there with
  `cd master && mkdir -p .scratch && TMPDIR="$PWD/.scratch" .venv/bin/python -m unittest discover -s tests`.
- Each side's worktree `.git` pointers are kept **relative** so `git` works
  regardless of mount path; the admin `gitdir` files under
  `.git/worktrees/*/gitdir` are absolute, so **never run `git worktree prune`**
  — it would drop the metadata for whichever worktree path that view cannot see.

**Never boot `app.py` on the live box.** `app.py`'s `__main__` starts
`bedtime_loop` (bedtime enforcement + `budgets.tick_budgets`), which reads the
checkout's `bedtime.json`/`budgets.json` and can lock accounts and thaw
`parental-*` cgroups — its watchdog thaws a frozen group it sees no paused
budget for. Production separates the two: `parental-controls.service` runs
`waitress-serve wsgi:application` (loop-free) and
`parental-controls-enforce.timer` runs the loop. Any throwaway/preview instance
must use the `wsgi` entrypoint (or blank the runtime JSON in an isolated copy).

## Status right now

`master` = `80a99fd`, holding three merges on top of `3af46f0`: the tablet-band
sidebar token, the row-launched Processes override dialog and the Overview
no-JS POST forms. Verified on the VM: full suite **1278 tests, OK, 14 skips**,
and the no-JS Overview path over real HTTP (`POST /overview/lock` → 302 →
danger flash; non-wheel add-admin refused; missing-CSRF 403).

**Deployed on the live box** on 2026-10-14 with
`sudo ./install.sh --port 5000 --bind 192.168.122.201 --tls --open-firewall`
(LAN HTTPS; self-signed cert fingerprint
`5A:06:42:8E:A4:38:EA:2E:63:1B:F4:CD:AD:C9:50:A4:7A:B0:91:29:A5:44:64:51:58:F3:CB:1C:AF:B8:74:72`;
`GET /` → 302, health probe OK; proceedings documented in the
`parental-controls-deploy` skill). **Not pushed** to `origin`. All three feature
worktrees and branches are deleted; nothing is in flight. Pending the owner:
push, confirm the Katalon `ADMIN_USER` is wheel or app-only, and decide the
no-JS scope question above. Still unverified (needs a browser): the no-JS toggle
in a real browser and the 236px tablet sidebar at 768–959px.
