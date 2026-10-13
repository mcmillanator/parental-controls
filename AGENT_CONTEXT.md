# AGENT_CONTEXT.md — handoff for the next agent

Current state of Bazzite Parental Controls, and the things that are **not**
derivable from the code. Read this plus `AGENTS.md` before touching anything.

Last updated: 2026-10-13, after the Flowstep design translation was merged and
deployed. Written when the project was moved to a new machine.

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

- Overview actions POST via `fetch` + CSRF header rather than a pure no-JS form
  submit (the dialog itself is a real POST form).
- Processes keeps an inline override card; the notes want a row-launched modal.
- Add-app-only-admin is a `<details>` disclosure, not the shared dialog.
- Honest placeholders: "1 active session" and specimen values are illustrative;
  per-account "using 1h 42m today" isn't in the payload; notification lead-time,
  the HTTPS toggle and multi-device are disabled.
- Tablet (768–959) sidebar sizing is inferred from S3/S4, not specified.
- Multi-device implementation is parked (owner: not soon). The session agent is
  installed per child only with `--session-user`; the remote agent needs
  `--enable-agent`.
- Stale remote branches still on origin: `empty`, `feature/bootstrap`,
  `feature/freezer-watchdog`, `feature/mvp-auth`. `research/process-filtering`
  has a live worktree.

## Status right now

`master` = `3af46f0` (merge of `feature/design-translation`), pushed, **deployed
and healthy** on the live box; the design-translation worktree and branch are
deleted. Nothing is in flight.
