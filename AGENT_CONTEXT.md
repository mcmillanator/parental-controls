# AGENT_CONTEXT.md — handoff for the next agent

Current state of Bazzite Parental Controls, and the things that are **not**
derivable from the code. Read this plus `AGENTS.md` before touching anything.

Last updated: 2026-10-19, on `master` @ `20956a4` — **three releases shipped
and deployed to the VM today** (all records in "Status right now" below).
(Historical note from the 2026-10-17 update: the
`docs/nojs-accuracy` correction is merged): the no-JS scope claims in `static/js/app.js` and `UI_UX_PLAN.md`
§11.1 were corrected to the shipped split — Overview actions are a full
plain-POST no-JS path; the other pages' mutations are JS/fetch and do not
submit without JS — the owner chose accurate docs over converging the
remaining pages. The time-tracking QA is merged too (`80bfce7`) and
implemented: one attribution bug (Minecraft runs as bare `java`, no category
keyword, `.desktop` index keys on the launcher) explained both owner
symptoms; FIX-1..3 (JVM attribution by app-identity tokens with friendly
names, tick builds the per-user .desktop index TTL-cached 300s, enforcement
observability) landed as `90e7bb8..b5a40dc` — every freeze/save failure now
surfaces as `budget-tick` journal lines + `budget.auto_pause_failed` /
`budget.save_failed` audit events. FIX-4 (stale-cgroup reconciliation) is
deferred by owner; F9 stays documented as live behavior. 2026-10-18 landed
three more merged units: the **wheel-parity payload fix** (the 10s rebuild
rendered wheel admins as lockable children because `/api/users` lacked
`wheel`; `tests/test_ui_overview_parity.py` is now a payload-contract guard
for every field the JS consumes), **admins hidden from the Overview with
the Admins card consolidated onto `/admin/audit`** (shared
`_is_admin_account` predicate in `index()`+`api_users()`, agent payload
gained `admin`, selector contract migrated in `UI_UX_PLAN.md` §7.4), and
**`install.sh --deploy-webserver`** (implies bind 0.0.0.0 + TLS + :443 +
an :80 301-redirect listener + firewall 80/443 + `CAP_NET_BIND_SERVICE`;
default installs render byte-identical units). The VM now runs in
webserver mode — panel URL is **`https://192.168.122.201/`** (plain http
301s; the old :5000 listener is gone). A restructure plan for the
`app.py`/`budgets.py` giants is in flight (`feature/restructure-plan`).
That sits on top of the `feature/bonus-modal` work (merged
as `567fcb9`): the
per-user Bonus time card is now a shared shell modal (`#pc-bonus`,
`_partials/bonus_dialog.html`, opened from the hero's "Add bonus time" button)
and the user-page countdown is `aria-live="off"` (it ticks at 1 Hz).
That sits on top of the 2026-10-14 state, after three local workstreams were
merged to `master` but **not yet pushed or deployed**: the tablet-band sidebar
token, the row-launched Processes override dialog, and the Overview no-JS POST
forms.
On 2026-10-15 the browser-recorder QA suite (a third-party browser-recorder
Selenium-IDE HTML suite plus its CI validator) was **removed** at the owner's
request — it is no longer used. The pinned-selector guard is now the stdlib
suite (`tests/test_ui_assets.py` `PinnedSelectorInventoryTest` plus the
per-page `test_ui_*.py` modules), with `UI_UX_PLAN.md` §7.4 still the spec.

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
  selectors the tests depend on — **preserve them**.
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
  only (`static/js/app.js`); the Overview's actions work as plain
  server-rendered POSTs, and extending that to the remaining pages is a
  deferred owner option (see "Open / deferred" — today their saves are
  JS/fetch-only).
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
- **The live countdown is shared between the two screens and now sits at the
  top of the user page** (`feature/countdown-top-and-overview`): on the Time
  rules page `#countdown-card` is the first card, directly under
  `#pc-user-title` and above the Account hero; the Overview account cards carry
  the same value as a compact `.pc-account__countdown` chip beside their
  badges. Both are built on one helper, `app.countdown_state(user)` →
  `{'kind': 'bonus'|'bedtime', 'seconds'} | None`, fed to the Overview through
  the existing `index()` + `/api/users` payload (server markup *and* the JS
  `accountHtml` rebuild) — never re-derived in JS. A single 1 Hz ticker in
  `static/js/app.js` decrements every `[data-countdown-seconds]` element and
  each 10s poll re-seeds it, so the Time rules readout is genuinely live (it
  used to change only on reload). **Both readouts are `role="timer"
  aria-live="off"`** (owner decision, `feature/bonus-modal`): the Time rules
  readout used to be `aria-live="polite"` and, at 1 Hz, made a screen reader
  announce the time every second. The status/toast region remains the announced
  one. Both render nothing when there is nothing to count down to, or on wheel
  "Protected" cards (the user page keeps its `--:--:--` placeholder).
- **Bonus time is a shared shell modal** (`feature/bonus-modal`): the Time
  rules page's always-open `<details id="bonus-section">` card is gone. The
  hero's "Add bonus time" button (`.btn-bonus-open` in `#account-actions`)
  opens `#pc-bonus` from `templates/_partials/bonus_dialog.html` (included from
  `base.html`), the same model as `#pc-override`: one native `<dialog>` in the
  shell, **outside the 10s-refreshed regions**, opened by
  `window.pcBonusDialog()` and outside-rebuilt by `renderAccount`. It holds the
  status + Revoke, the five shortcuts, and the custom h/m entry + Grant custom;
  all pinned ids/classes/labels are unchanged (`#bonus-section` now names the
  dialog body). Grant/revoke keep the same handlers, endpoints, payloads,
  toasts and refresh, and the dialog closes on success; Cancel/Esc/backdrop post
  nothing. The hero `.badge-bonus` chip stays so bonus state is visible at a
  glance (its `title` carries the 12h end time).

## Verifying work

```sh
mkdir -p .scratch && TMPDIR="$PWD/.scratch" .venv/bin/python -m unittest discover -s tests
```
All scratch stays **inside** the worktree (`.scratch/` is gitignored); never
`/tmp`. Last known on the VM: **1302 tests, OK, 20 skips** (the 20 are live-env
and root-gated skips). The pinned-selector guard is now the stdlib suite
(`tests/test_ui_assets.py` plus the per-page `test_ui_*.py` modules);
`UI_UX_PLAN.md` §7.4 is still the spec. (The browser-recorder suite that used to
carry that guard was removed 2026-10-15 — owner request, no longer used.)

Neither box has a browser by default, but the container now has Playwright +
Chromium **installed** under `.scratch/` (gitignored) with a working harness:
`.scratch/shoot.py` shoots all six routes × phone/tablet/desktop × light/dark into
`.scratch/screens/` (PNGs + `INDEX.md` + `measurements.json`). Rebuild only on a
fresh container:

```sh
python3 -m venv .scratch/qa-venv && .scratch/qa-venv/bin/pip install playwright pillow
PLAYWRIGHT_BROWSERS_PATH="$PWD/.scratch/pw-browsers" .scratch/qa-venv/bin/python -m playwright install chromium
.scratch/qa-venv/bin/python -m playwright install-deps chromium   # apt, as root
```
Shoot against an **isolated copy** run through the loop-free entrypoint (see
"Never boot `app.py`" below) — blank the runtime JSON first, then:

```sh
cd .scratch/app && PARENTAL_CONTROLS_AUTH=$PWD/auth.json PARENTAL_CONTROLS_AUDIT=$PWD/audit.jsonl \
  ../../.venv/bin/waitress-serve --host=127.0.0.1 --port=5055 wsgi:application
```
`PARENTAL_CONTROLS_AUTH` overrides the auth-store path (`auth.py`), so a preview
never touches `/etc/parental-controls/auth.json`. Seed it with
`auth.hash_password()` and `must_change: False` to skip the forced-password
redirect. Avoid ports **5060/5061** — Chromium blocks them. Screenshots are the
owner's preferred review artifact (they browse remotely; no port forwarding).

## Test modes (2026-11-01 — hermetic suite)

`unittest discover -s tests` is trustworthy on BOTH boxes now. The invariant:

> **On any box, every test either RUNS (its capabilities are present) or
> SKIPs with a greppable reason. Zero failures/errors anywhere.**

* **VM = authoritative.** On `opencode-vm` the full set runs exactly as
  before. No gate below ever fires there: secure_path resolves every
  binary, the desktop entries exist, `cheroot` is in the VM venv, and
  the VM identity checks pass. Expected post-change VM result:
  **`Ran 1510 … OK (skipped=22)`** — same run set and same skip count as
  the pre-change VM run (the historical "1502" was the CONTAINER count,
  where the crash at import of one module hid its 9 real tests behind a
  single loader pseudo-test; the VM always counted those 9).
* **Container = green-by-skips.** Env-bound failures became explicit
  skips (all reasons start `caps:` or reuse the suite's `… not available`
  idiom). Baseline before this change: 109F+4E → now `Ran 1510 …
  OK (skipped=160)`; 121 new skips = 107 secure_path subtests + 9 live
  usermod/PAM (VM-only) + 3 cheroot + 1 on-box binary paths + 1 firefox
  desktop data.
* **How gating works** (`tests/_caps.py`, stdlib-only):
  * sudoers-coverage tests (test_deployment / test_multidevice_install /
    test_session_agent) mirror sudo's secure_path lookup, which only
    matches binaries that EXIST on the box. Where `loginctl`/`systemd-run`
    are absent, the affected **subtests** skip
    (`caps: secure_path cannot resolve 'loginctl' …`); all other subtests
    (usermod/passwd/cat/tee/mkdir) still run.
  * **DANGEROUS gate** — any test that mutates the real system
    (`useradd`, `usermod -L/-U`, `su`/PAM, freeze) must also pass
    `_caps.needs_vm()`. The VM signal is a conservative AND of three
    checks: hostname == `opencode-vm`, `/opt/parental-controls` exists,
    and user `pcagent` exists (constants at the top of `tests/_caps.py` —
    update there if the VM is ever renamed/rebuilt). Anything uncertain
    skips; binary presence alone is NOT enough (a Fedora laptop has
    usermod too). Currently the only such module is
    `test_lock_usermod_real.py`; `test_granted_binary_paths_exist_on_this_box`
    is VM-only by design (it checks the *deployment OS's* layout).
  * Data probes: `needs_desktop_entry('firefox')` skips the real-desktop
    index test on boxes without Firefox .desktop files (probe checks file
    NAMES, so a classification regression still FAILs where firefox is
    installed). Missing pip deps use the existing `HAS_CHEROOT` idiom.
* **Adding new tests that touch the box:** probe first (`_caps.has_binaries`,
  `missing_binaries_in(SECURE_PATH, …)`, `is_bazzite_vm`), skip with a
  `caps:` reason at subtest granularity where a class gate would lose
  coverage. Never weaken an assertion to pass on the container — convert
  to a skip only when the failure is provably environmental.
* **PENDING: verify on the VM** (human/manager) — run the suite there and
  confirm parity with the pre-change VM run: `Ran 1510, OK (skipped=22)`;
  the skipped delta vs the VM's before count must be **0** (nothing that
  ran on the VM before may become a skip).

## Deploy (live box — owner runs it, or supervises it)

Merge to `master`, then `sudo ./install.sh` from the master worktree: stages code
to `/opt/parental-controls`, preserves runtime state, renders units, validates
sudoers with `visudo -c`, restarts the service. Smoke-test `curl -sk
https://192.168.122.201:5000/` → expect **302** to `/login` (the live install is
TLS on that address). Never restart `parental-controls.service` by hand; never
serve the panel from a checkout or as `stan`. Runtime state is SQLite `state.db`
(+ JSON mirrors); the service runs as the `parental-controls` system user; a
healthcheck timer probes every 5 min.

**If the panel answers "Authentication unavailable" (HTTP 500), check the auth
store's owner first:** `sudo stat -c '%U:%G %a' /etc/parental-controls/auth.json`
must read `parental-controls:parental-controls 600`. A root-side write used to
leave it `root:root`, which fails closed on *every* request — `auth.save_store()`
now preserves/inherits the owner and warns loudly when it cannot (fixed in
`e9bb2ef`), and `sudo chown parental-controls:parental-controls <file>` repairs it
live (no restart needed: the store is re-read per request).

Recovery CLI on `PATH`:
`sudo parental-controls {unlock,unfreeze-all,reset-password,add-admin,remove-admin,lock-admin}`.

## Open / deferred

- **Time-tracking fix status (2026-10-17):** FIX-1..3 merged, pushed and
  deployed (`0969c47`); owner decisions recorded: billing unchanged (tick
  snapshots do NOT mark `user_app`) and **FIX-4 deferred** (stale-cgroup
  reconciliation, option (a)/(b) undecided). FIX-5 (Other-category UI) not
  started. Journal observability confirmed live; still to watch: a child
  launching Minecraft must show up billed under Games with display name
  "Minecraft", and any future freeze failure will now appear as
  `budget-tick` notes + `budget.auto_pause_failed` / `budget.save_failed`
  audit events.
- **Overview actions are a real no-JS POST path** (resolved): every Overview
  account action (Lock / Unlock) and the admin add/remove controls are
  `<form method="post">` submissions to `/overview/*`,
  which run the same cores as the `/api/*` twins, flash and redirect (PRG); JS
  only adds the confirm dialog. The Overview's redundant **Lock screen** button
  was removed (2026-10-16) — Lock already locks the account *and* the screen via
  `_lock_account_and_screen`; `/overview/lock-screen` is gone, so `/`'s actions
  are now exactly Lock / Unlock / Details. `/api/lock-screen` is deliberately
  **kept** as a documented endpoint with no UI button (mirrored by the agent's
  `/api/v1/lock-screen` verb), so the JSON/agent/audit contract is unchanged.
  `#admin-add` / `#new-admin-username` stay
  visible for every admin because the selector contract asserts them on `/`
  (the endpoint refuses non-wheel actors server-side). **Resolved:** the owner
  confirmed the shared test admin is an **app-only** admin (not a wheel
  account), which is exactly why the Overview admin add-form has to render for
  every admin rather than only for wheel actors. Two live-box checks: the new
  `app.secret_key` (per-process random, `PARENTAL_SECRET_KEY` overridable) makes
  Flask's `session` cookie appear alongside `pc_sid`, and the one-time admin
  password rides in that signed (not encrypted) cookie until the redirect
  renders — confirm that is acceptable or add a session-cookie policy.
- **Processes override is a row-launched modal** (resolved): the inline editor
  card is gone; the row's Override button opens the shared `#pc-override`
  dialog (`_partials/override_dialog.html`).
- **Bonus time is a row/hero-launched modal** (resolved): the user page's
  always-open Bonus disclosure is gone; the hero's "Add bonus time" button
  (`.btn-bonus-open` in `#account-actions`) opens the shared `#pc-bonus` dialog
  (`_partials/bonus_dialog.html`, included from `base.html`), with the same
  `grantBonus`/`grantCustomBonus`/`revokeBonus` handlers and pinned
  ids/classes/labels unchanged (`#bonus-section` now names the dialog body).
- **The user-page countdown is silent** (resolved): `#bedtime-countdown` is
  `role="timer" aria-live="off"` — the open question *"should it keep
  `aria-live="polite"` at 1 Hz?"* is answered no; the status/toast region stays
  the announced one. See `UI_UX_PLAN.md` §7.4.
- Add-app-only-admin is still a `<details>` disclosure on the audit page, not
  the shared dialog.
- **No-JS coverage is incomplete outside the Overview** (owner decision
  2026-10-17: make the docs accurate, not the pages): `budgets`, `processes`
  and `admin_audit` (and much of `user`, including all time-rule edits) still
  post via `fetch` + a CSRF header, so with JS disabled those pages render
  and navigate but **those actions do not submit**. Emergency logout is part
  of this: it was removed from the Overview (owner request, 2026-10-15) and
  now lives only on the per-user page as a JS/fetch action against
  `/api/logout` (the `/overview/logout` route is gone). The Overview itself
  is a complete no-JS path (`/overview/*` plain-POST twins, PRG + flash), and
  the admin sign-out `<form method="post">` to `/logout` works JS-disabled on
  every page. The old overclaim in `static/js/app.js` ("with JavaScript
  disabled the pages still render, navigate and submit") and §11.1's
  "server-rendered POST/fetch path" wording were corrected to this split on
  2026-10-17. Converging the remaining pages onto plain-POST twins stays an
  OPEN DEFERRED owner option; not started.
- Honest placeholders: the design-specimen values are illustrative; per-account
  "using 1h 42m today" isn't in the payload; notification lead-time,
  the HTTPS toggle and multi-device are disabled. **The Overview's per-account
  live countdown is NOT a placeholder** — it is derived from `bedtime`/`bonus`,
  which *are* in the `/api/users` payload (`app.countdown_state()`), so it shows
  a real, ticking value; it is the first per-account live data on the Overview.
- The System-summary card's **active-session count is REAL, not a placeholder**
  (owner request, 2026-10-18). It replaced a hardcoded "1 active session" plus a
  "not exposed yet" hint: every `/api/users` and `/` row now carries an integer
  `sessions` (`app.active_session_count()`, mirrored in `agent.py`), counted as
  `Class=user` logind sessions only — the per-user `Class=manager` session is not
  a login. Measured with the two loginctl calls the screen-lock path already made
  (`show-user <u> -p Sessions --value`, `show-session <id> -p Class -p Type`), so
  **no new sudoers grant**; cached 30 s per user like the lock-status probe, and
  invalidated by the emergency logout.
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
**The whole release sequence (sync → suite → push → deploy → verify), including
the port-443 push workaround, is captured as the `parental-controls-deploy`
skill — follow it rather than reconstructing the steps.**

- A full `discover` run is **not** green in the container (~109 failures + 1
  error at baseline, all environment-bound: `test_deployment`,
  `test_multidevice_install`, `test_session_agent`, `test_budgets`,
  `test_lock_usermod_real`). The container's green signal is the `test_ui_*`
  set plus `test_ui_a11y` and `test_ui_assets`.
- The VM is the authoritative environment: **1302 tests, OK, 20 skips** at
  `567fcb9` (Python 3.14.8, live PAM/usermod included). Run it there with
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

`master` = `0969c47` — pushed and deployed (time-tracking FIX-1..3 =
`90e7bb8..b5a40dc`, QA report `80bfce7`, docs correction `de566ec`,
originally `567fcb9` atop `3af46f0`): the tablet-band sidebar token, the
row-launched Processes override dialog, the Overview no-JS POST forms, the auth
store ownership fix, the removal of the browser-recorder QA suite and the
Overview Emergency logout button (owner request, 2026-10-15), the live countdown
(moved to the top of the user page and added per-account on the Overview), the
removal of the redundant Overview "Lock screen" button (`/api/lock-screen` is
kept as an endpoint with no UI button, mirrored by the agent's
`/api/v1/lock-screen`), and the per-user Bonus editor moved into the shared shell
modal `#pc-bonus` (opened from a new hero "Add bonus time" button; the countdown
switched to `aria-live="off"` now that it ticks). Verified on the VM: full suite
**1302 tests, OK, 20 skips**, the no-JS Overview path over real HTTP
(`POST /overview/lock` → 302 → danger flash; non-wheel add-admin refused;
missing-CSRF 403), an outage replay of the store-ownership fix (`root:root 600` →
`parental-controls:parental-controls 600`), and that the removed routes 404 while
`/overview/lock` 302s and `/api/lock-screen` still answers 401.

**Pushed** to `origin/master` and **deployed on the live box** (2026-10-17) with
`sudo ./install.sh --port 5000 --bind 192.168.122.201 --tls --open-firewall`
(LAN HTTPS, self-signed cert fingerprint
`5A:06:42:8E:A4:38:EA:2E:63:1B:F4:CD:AD:C9:50:A4:7A:B0:91:29:A5:44:64:51:58:F3:CB:1C:AF:B8:74:72`;
`GET /` → 302, health probe OK; proceedings in the `parental-controls-deploy`
skill). Outbound SSH port 22 is filtered from **both** boxes — push over
`ssh.github.com:443`. The no-JS docs correction is merged (`de566ec`,
docs-only — no redeploy needed on its own); the time-tracking QA is merged
(`80bfce7`), its worktree and branch removed. Container→VM SSH now works
passwordlessly (key bootstrapped via ssh-copy-id — see the deploy skill's
SSH bootstrap). Live-box checks: enforce timer fires every minute and ticks
(fresh `last_tick`), budgets sudoers rendered + `visudo` OK, **no
`parental-*` cgroups exist** (so no stale-member deadlock in play today),
no stale `resumed` flags; `used_seconds` is 0 for both kids (tester logged
out; bluefox idle Steam only), so nothing has actually reached exhaustion —
symptom 2's enforcement gate is still unproven live. FIX-1..3 are
implemented, merged and **released** (2026-10-17: origin/master and the
live box at `0969c47`; VM suite 1334 OK, 20 skips; post-deploy: 302/401,
timers active, deployed greps confirm the attribution keywords, tick index
and summary emitter; the enforce journal now prints `budget-tick` lines —
first one observed: `budget-tick bluefox note=skipped bluefox: account
locked`). FIX-4 stays deferred (owner); the F9 stale-member deadlock remains
documented live behavior. Remaining real-world confirmation: a child must
actually launch Minecraft and see it billed under Games (tests prove the
attribution; nobody has watched a live JVM row yet).

**Release 2026-10-18 (`ead99e3`, pushed to origin/master and deployed).** VM
suite **1409 tests, OK, 22 skips** (the 2 new env-gated installer dry-runs
execute on the VM; the VM is authoritative). Deployed with
`sudo ./install.sh --deploy-webserver` on the VM: 8/8 verify green incl.
`GET http://127.0.0.1:80/ -> 301 https://…`; post-deploy checks: 443 → 302
`/login?next=/`, 80 → 301 with path+query preserved, ONE service process
holds both privileged ports, firewall `80/tcp 443/tcp`, **no SELinux
denials**, cert kept (fingerprint unchanged → browser TOFU trust carries
over), unit shows `AmbientCapabilities=CAP_NET_BIND_SERVICE` +
`CapabilityBoundingSet=` allow-list (which includes `CAP_SYS_ADMIN` for the
cgroup freezer). The **capability-regression gate** (the narrowed bounding
set must not break the sudo children) passed: replicating the unit's exact
bounding set via `prctl(PR_CAPBSET_DROP)` as `parental-controls`,
`sudo passwd -S pcagent` and `usermod -L/-U` all succeed (pcagent left
unlocked). ⚠️ **`systemd-run` transients are NOT a valid probe for this**:
systemd-run implies `NoNewPrivileges`+`RestrictSUIDSGID`, which break the
setuid `sudo` exec (203/EXEC "Unable to locate executable") — the installed
unit's own comment says the same about the real unit. Screenshot evidence
for the UI move: `.scratch/screens/{overview,admin-audit}-desktop-dark-ui-move*.png`
(first paint vs after the 10s poll; js errors none). aiden-pc has NOT been
switched yet (owner runs it there: `git pull && sudo ./install.sh
--deploy-webserver` + the same probes).

**Merged locally 2026-10-18; pushed and deployed 2026-10-19 with `56779e6`:
the friendly-status fix (`f0a8ecb`, branch deleted).** Every raw probe detail is classified at the
payload boundary (`status_display()` — one table: PAM/systemd chatter + a
parsed status → the normal pill; genuine probe failure → "Status check needs
help — ask your admin"; human copy verbatim), so `sudo: pam_open_session…`
can no longer reach any rendered page; raw text goes to the admin audit log
as `account.status_probe`, deduped per change; `tests/test_status_detail.py`
(36 tests) mutation-verifies it. **Root cause of the live warning box is NOT
this fix**: `get_lock_status` only leaks stderr when `sudo passwd -S` exits
non-zero — on the live boxes sudo's PAM session policy fails
(`TooManySessions`, seen in the journal), so the probe genuinely fails. **FIXED
2026-10-19 (`20956a4`, pushed + deployed):** the panel/enforce/agent units now
set `Environment=XDG_SESSION_CLASS=background` — `pam_systemd(8)` honours it
from sudo's own environment, and background-class sessions don't count against
logind's per-user `MaxSessions` (default 5) that the concurrent probe bursts
were tripping. Live proof: post-deploy window 64 probe execs / 0
`pam_open_session` denials; pre-deploy same window: 10 denials. Pinned by
`tests/test_service_user.py::test_probe_units_use_background_logind_sessions`
(+ the healthcheck-stays-plain guard). The 30s caches from `3532aa7` reduce
the churn; the class change removes the cap exposure entirely.

The queued workstream it prompted **shipped 2026-10-19 (`3532aa7`, pushed +
deployed)**: a 30s TTL cache for the lock-status probe (~6× fewer sudo PAM
sessions; the 10s Overview poll runs one per account) + active-session counts
via the EXISTING sudoers rules (`loginctl show-user -p Sessions --value`,
`show-session -p Class -p Type`; "active" = Class `user`, which excludes the
per-user manager session — verified live: bluefox had `21 unspecified/manager`
+ `20 wayland/user`) + "Accounts"→"Child Accounts" heading + UID removed from
the Overview cards (details page keeps UID/Shell/Home). The recovery session
rebased it onto the sliced layout: the probe cache now lives in `users.py`
(invalidation routed through the `app.` facade from `locks.py`), the
session-count cache is facade-local in `app.py`; `RESTRUCTURE_IMPORTS.md`
regenerated — 55/55 pre-existing patch-target rows retained, additions only.
The live warning box should now quiet down; `account.status_probe` audit
events quantify whether the probe still fails.

**VM state (2026-10-19): ON, synced, and RELEASED at `3532aa7`.** Release
record (#1 — friendly-status fix + restructure slices + clock-flake test
fix, `56779e6`): authoritative VM suite **1445 tests, OK, 22 skips**; pushed to
`origin/master` from the VM over `ssh.github.com:443`; deployed with
`sudo ./install.sh --deploy-webserver` — **8/8 verify**, 443 → 302
`/login?next=/`, 80 → 301 with path+query preserved, ONE python process
holds both ports, firewall `80/tcp 443/tcp`, cert kept (fingerprint
unchanged → browser TOFU trust carries), panel/enforce/health units active,
zero SELinux avc lines, capability-regression gate passed (`passwd -S` /
`usermod -L` / `usermod -U` succeed on `pcagent` under the unit's exact
bounding set; `pcagent` left UNLOCKED). Deployed-artifact greps confirm the
release content: `status_display` in `/opt/parental-controls/app.py` and the
sliced modules (`users.py`, `locks.py`, `budgets_tick.py`) present — the
`account.status_probe` audit events can now quantify the live probe failures.
aiden-pc has NOT been switched. The old `5000/tcp` firewalld entry lingers
from the pre-webserver install (harmless; no listener bound).

**Release #2 (2026-10-19, `3532aa7`): the owner's Overview workstream is
live.** `feat/sessions-and-child-accounts` rebased onto the sliced master
(lock-status probe cache → `users.py`, invalidation routed through the
`app.` facade from `locks.py`; session-count cache facade-local in `app.py`,
agent mirror unchanged), FF-merged, VM suite **1504 tests, OK, 22 skips**,
pushed over 443, deployed `--deploy-webserver`: 443→302, 80→301 path+query
preserved, bogus login→401, ONE process on :80+:443, timers active, 0 avc
lines, capability gate green (`pcagent` left UNLOCKED), sudoers untouched —
the counts ride EXISTING loginctl grants (`visudo -c` OK). Deployed greps:
"Child Accounts" ×3 in `templates/index.html`, `active_session_count` in
`app.py`, `invalidate_lock_status` ×6 + the 30s TTL constant in `users.py`.
Not yet eyeballed in a real browser (neither box has one) — owner glance at
the Overview recommended; watch `account.status_probe` events for the
warning-box quiet-down the 30s cache should deliver.

**Crashed-session recovery (2026-10-19).** The 2026-10-18 session died
~21:51 mid-flight with three workstreams running. All recovered; the unit of
work that blocked today's release was the **countdown clock flake**:
`tests/test_countdown` used a FIXED 21:00–07:00 bedtime fixture while
`countdown_state` reads the real clock, so the suite failed **every run
between 21:00 and 07:00 local** on either box (product behavior was correct
— in-window → 0 seconds). `56779e6` derives the window from `datetime.now()`
instead (test-only change, no product code, no redeploy implication beyond
being carried by this release). The container→VM SSH key had been wiped
(container restart) — re-bootstrapped per the deploy skill (key in BOTH
`.ssh` homes; password entered in chat that day — rotate `stan`'s password
if that matters). **Worktree gitfile pointers are now relative on BOTH
boxes** (`gitdir: ../.git/worktrees/<name>`); admin back-pointers stay
absolute — this git build resolves relative back-pointers against the CWD
and marks worktrees `prunable`, so **NEVER `git worktree prune`** remains
law. This git has no `git worktree add --relative-paths`; convert by hand
after every add (rule recorded in the root `AGENTS.md`). The reported host
view of the container checkout is
`/var/home/stan/Documents/Projects/local-llm-projects/...` — the relative
pointers are what keep it working from both spellings.

**Unmerged workstreams (owner review pending; all committed, worktrees
clean):** `feature/restructure` @ `431ea80` (slice 12 = `bonus.py`+`notify.py`
out of `app.py`; container slice gate green at baseline) — NOT FF-able
anymore: release #2 touched `app.py`/`agent.py`/`RESTRUCTURE_IMPORTS.md`
under it, so the branch needs a merge-master + slice-gate baseline refresh
(the established pattern, cf. `438d4f1`) before slice 13;
`fix/install-selfcheck` @ `2723539` (installer firewall/reachability
overhaul + hermetic `tests/test_install_firewall`, 41 tests) — still
FF-able; fixes the `https://existing:5000`-style URL wart still visible in
release #2's deploy output (`--tls-name` was the old workaround).

Still unverified (static renders + tests only — nobody has watched it in a
browser): the no-JS toggle, the 236px tablet sidebar at 768–959px (measured
correct by the harness, never eyeballed), the countdown ticking, and the bonus
modal's `showModal()`/Esc/Cancel interactions. Resolved 2026-10-17: the no-JS
scope above — the owner chose accurate docs over converging the remaining
pages; convergence stays an open deferred option. Resolved: the countdown
`aria-live` is now `off`, as on the Overview.

**Still failing after `20956a4` (2026-10-20): the sudoers `!pam_session` fix.**
The owner reported `sudo: pam_open_session: Permission denied` /
`policy plugin failed session initialization` on live boxes TODAY: the
status probes (`passwd -S`, `loginctl show-user/show-session`) still die at
PAM session init, the child cards keep showing "Status check needs help —
ask your admin" and the audit log records `{"class": "unavailable"}` events.
The `XDG_SESSION_CLASS=background` class-nudge is evidently not holding
(possibly insufficient on its own, possibly the failing box predates the
`20956a4` deploy — both scenarios need the same next step). The real fix:
BOTH sudoers templates now carry `Defaults:@SERVICE_USER@ !pam_session`
(sudoers(5) per-INVOKING-USER Defaults scope — note `Defaults:` is USER
scope, `Defaults!` is COMMAND scope; the `pam_session` flag exists since
sudo 1.8.7) so the service user's sudo children never open a PAM session
at all — logind is never consulted, so no `TooManySessions`, regardless of
the class. Cost: sudo's PAM session open/close journal lines and
PAM-applied resource limits for this user's non-interactive sudo ONLY;
auth/account phases and sudo's own command logging are untouched. The
units' `Environment=XDG_SESSION_CLASS=background` lines stay as
belt-and-braces (their tests still pin them). Pinned by
`tests/test_service_user.py::TestSudoersRenderAsServiceUser`
(placeholder + rendered `!pam_session` Defaults entry, both templates);
the existing rendered-sudoers `visudo -c` gate covers the grammar in both
files — **not runnable in this container (no sudo package), so the syntax
proof is the live `visudo -c` during install**. install.sh needs nothing:
it seds + visudo-validates exactly these template files.
**VERIFY ON THE LIVE BOX (human):** 1) redeploy FIRST — the failing box may
run pre-`20956a4` code (`git pull && sudo ./install.sh --deploy-webserver`,
install.sh's `visudo -c` aborts on a bad render); 2) `sudo grep pam_session
/etc/sudoers.d/parental-controls /etc/sudoers.d/parental-controls-budgets`
— expect the Defaults line naming the service user, no `@SERVICE_USER@`;
3) watch `journalctl -u parental-controls.service -u
parental-controls-enforce.service --since -30m | grep -c pam_open_session`
— expect 0; 4) Overview cards must stop showing the needs-help copy and
`account.status_probe` audit events should go quiet.

**Overview card meta stripped (2026-10-31, `fix/card-meta-row`).** Owner
decision: child cards on the Overview show the **username only**. The meta
segment — `· wheel admin/child · <status> until <bedtime end>` — was removed
from the server-rendered template (owner commit, carried onto this branch as
`b6034e7`) AND from the inline script's `accountHtml()` rebuild, so neither
the first paint nor the 10s poll shows role/status text. Deliberately
UNAFFECTED: the status pill (`pc_status_pill` / the `${esc(u.status_detail)}`
chip in `badges`), the `wheel admins can never be locked or removed.` warning
inside the defensive wheel branch (template + JS), the Bedtime range/days
line, countdown chips, and every action button/dialog. `status_detail` stays
in the payload contract — the Details page and the pill still read it.
Pinned by `tests/test_ui_child_accounts.py::OverviewCardMetaTest`
(`test_the_first_paint_card_name_is_the_username_only`,
`test_the_rebuild_script_emits_no_role_or_status_meta`). NOTE: the
`feature/index-components` branch was NOT touched per owner instruction —
this change lives on `fix/card-meta-row` off the master line only.

**P1 mirror retirement — shared cores `pcstatus.py` / `pcprobe.py`
(2026-11-13, `feature/shared-core`, NOT yet merged/deployed).** The
byte-identical app.py↔agent.py blocks that existed only because the agent
must never import app are gone; both sides now import ONE stdlib-only
source of truth and re-export under the unchanged names (phase-1 rule:
relocate code, never names — `app.status_display` / `agent.status_display`
/ `pcstatus.status_display` are now literally the same function object,
pinned by identity assertions in `tests/test_status_detail.py`).
- `pcstatus.py` (zero imports): the whole status-detail sanitizer —
  STATUS_OK/UNAVAILABLE/VERBATIM, STATUS_UNAVAILABLE_COPY, the ONE `_UNSET`
  sentinel (previously two separate `object()` calls), all three pattern
  tables (moved byte-identically — security boundary), and
  `status_residual_lines` / `classify_status_detail` / `status_display`.
  Plus `audit_needed` / `audit_status_probe_core` / `status_row_core`: the
  audit-decision + dedup + row-wiring logic. app.py and agent.py keep thin
  byte-identical WRAPPERS that inject their own module-local
  `_LAST_STATUS_PROBE_SIGS`, `_safe_audit` and `get_lock_status` at call
  time, so every existing `patch.object(app/agent, ...)` target still
  binds; `StatusDetailMirrorTest` became identity-based and a new
  `SharedCorePurityTest` AST-pins that both shared modules import nothing
  but the stdlib (agent's local imports are additionally allow-listed).
- `pcprobe.py` (imports only `subprocess`): the two loginctl session
  probes `_user_session_ids` / `_session_properties`, byte-identical
  between app.py and agent.py, moved verbatim and re-exported by both.
  `locks.py` untouched — it already resolves `app._user_session_ids` at
  call time. The 30s caches (`_SESSION_COUNT_CACHE`, TTL constants) stay
  per-module BY DESIGN (separate processes; `test_ui_session_counts` pins
  them as separate dicts); `active_session_count` / `invalidate_session_count`
  therefore remain intentionally-duplicated cache shells.
- Sudoers-scan follow-through: `test_deployment.MODULES` gained
  `pcprobe.py` (argv coverage cannot silently leave the scanned set — the
  slice-6 rule); `test_the_count_helpers_run_only_the_two_allowlisted_argv`
  now pins the argv literals in pcprobe.py AND binding identity in
  app/agent; `test_multidevice_install`'s agent.py argv scan simply sees
  two fewer inline argvs (they are shared now).
- DIVERGENT pairs audited and deliberately LEFT ALONE (unifying semantics
  needs owner decisions): agent.get_lock_status vs users._probe_lock_status
  (behaviourally identical, textually diverged: if-chain vs if/elif,
  `subprocess` vs `app.subprocess` call-time facade spelling — the cache
  difference is sanctioned); agent.get_human_users vs users.get_human_users;
  agent._session_supports_lock vs locks._session_supports_lock (locks.py
  resolves `app._session_properties` at call time per §1.1);
  agent.lock_user_sessions / run_lock_command vs the locks.py cores;
  agent._safe_notify vs app.notify_user; agent._admin_store_names vs
  app._admin_rows; agent.read_user_live vs app.get_user_live (payload-shape
  mirror); do_lock's 'OK (<screen note>)' text parse. `_safe_audit` is
  AST-identical app↔agent but unmarked and a patch target — left.
- Container suite: before **Ran 1510, OK (skipped=160)** → after **Ran
  1515, OK (skipped=158)**. The 5 new tests are the identity/purity/
  binding guards; the −2 skips are structural, not gating: two `loginctl`
  argv subtests left agent.py's scan with pcprobe.py's scan. UI set 428
  OK under `python -O`; both import orders smoke; zero failures/errors.
- PENDING: verify on the VM (human/manager) — expect `Ran 1515, OK
  (skipped=22)`, skip delta **0** vs the 2026-11-01 VM baseline (every VM
  subtest that ran still runs; loginctl resolves there, so the moved argvs
  are executed coverage, not skips).

## Calendar refactor — BACKEND slice (2026-11-13, `feature/calendar-schedule`)

Replaces the bedtime table + the overrides `windows` sub-doc with ONE
recurring **block** model (`schedule.py` pure evaluator + the whole-doc
`schedule` table, store v4 `schedule.json`). **Full spec + decisions:
`CALENDAR_DESIGN.md`** — read it before touching time config. Highlights:

- Block = target (`bedtime` | category | `app:<comm>`) × mode (`null`=lock
  for bedtime, `capped`=window-replaces-limit, `permitted`=gate inside/
  outside) × preset (`ONE`/`DAILY`/`SCHOOL`/`WEEKLY:BYDAY=…`/
  `BIWEEKLY:BYDAY=…`) × `dtstart`/`start`/`end`/`until`. Fail closed
  everywhere: a malformed block is absent; enforcement reads ONLY the
  schedule once a user's `migrated` marker exists.
- Migration is idempotent+deterministic, triggered by the panel
  `/api/schedule` routes (deliberately NOT the tick/agent verbs — see the
  design doc §3 for the hermeticity incident that decided this), audit
  `schedule.migrate` once per user. Legacy tables stay as
  write-synchronized mirrors: every mutation path (old `/api/bedtime`,
  old windows routes, new schedule API) syncs BOTH, so a corrupt schedule
  store falls back to current mirrors and deleted rules never resurrect.
- Bonus time is untouched live state; the events endpoint only reports it
  as a `derived: true` pseudo-band. UI slice is NOT built (later slice);
  the existing pages keep working off the mirrors + live-state fields.
- Patch surfaces preserved verbatim (`app/agent.is_in_bedtime`,
  `budgets._in_window/iter_windows/active_window/normalize_window`…):
  migrated users resolve through `_bedtime_flag`/`user_windows` shims;
  RESTRUCTURE_IMPORTS.md regenerated — name-additive (zero lost names),
  `schedule.py` joined `SharedCorePurityTest.SHARED`, the agent's local
  import allowlist gained exactly `schedule`.
- New API: `GET/POST/DELETE /api/schedule`, `GET /api/schedule/events`.
- Container suite: **Ran 1515 → Ran 1657, OK (skipped=158)** — 142 new
  tests in `tests/test_schedule_{eval,budgets,api,agent}.py`; two
  consecutive full runs leave ZERO residue in the checkout (state.db /
  schedule.json / overrides.json must never appear in a worktree root —
  the suite is hermetic again).
- PENDING (manager, on the VM): full suite expect `Ran 1657, OK
  (skipped=22)` skip-delta 0 vs the 1515/22 baseline; panel smoke:
  Overview/user-page chips + countdowns identical on a migrated config,
  `GET /api/schedule` shows the migrated blocks + `schedule.migrate`
  audit rows; enforce journal shows unchanged tick behaviour after the
  cutover.

## Calendar refactor — UI slice (2026-11-14, `feature/calendar-ui`)

The parent-facing **Calendar page** on the merged `/api/schedule` backend
(spec + decisions: `CALENDAR_DESIGN.md` §10). Highlights:

- `/calendar` (endpoint `calendar`, nav section `time`): vendored
  **FullCalendar 6.1.22** global bundle in
  `static/vendor/fullcalendar/` (MIT, banner + NOTICE.txt + hashes pinned
  by `tests/test_ui_calendar.py`); NO CDN anywhere (page + JS + vendor
  files pinned offline). Week grid default + month toggle, live `now`
  marker, child selector via `?user=`, events from
  `GET /api/schedule/events`, derived bonus band rendered read-only with
  a hint pointing at the child page's bonus dialog.
- Editor = `static/js/calendar.js` (vanilla ES module, no build step):
  drag creates, resize/move edits the WHOLE series (drag moves shift the
  times + anchor day; weekday-bearing presets rotate their BYDAY set),
  modal posts exactly the §2 grammar presets, overlap/validation errors
  surface inline in the modal. Polling: refetch on every mutation + a
  gentle 60 s timer (pauses while the modal is open / tab hidden); fetch
  failure shows a fail-closed banner, never an empty grid.
- **Retired (hard cutover):** `/time-rules` + `templates/time_rules.html`
  (404), `POST /api/bedtime` + `POST /api/budgets/windows` (405), the
  bedtime editor form + Weekly-Windows sections on `user.html`, the
  Weekly-Windows card on `budgets.html` (daily limits, per-app overrides,
  freeze/resume, bonus dialog and all live-state READS stay as-is;
  `GET /api/bedtime` still serves the mirrors for the chips).
- The old bedtime writer's immediate lock/unlock tail MOVED to
  `POST/DELETE /api/schedule` (`app._enforce_bedtime_now`): a bedtime
  save still auto-locks (session-preserving `usermod -L`, notification,
  bonus suppression, admin self-protection) and deleting the last bedtime
  series still releases a BEDTIME-owned lock — manual locks survive
  (pinned by `test_lock_regression`). One deliberate delta, design §6:
  deleting the bedtime series now KEEPS live keys (bonus until expiry);
  the old CLEAR deleted the whole row.
- Container suite: **Ran 1657 → Ran 1681, OK (skipped=158)**. New
  `tests/test_ui_calendar.py` (22: vendor pins, render guards, editor
  e2e incl. overlap 400 + lock argv path); 12 test modules re-pinned
  (retired-endpoint fixtures route through `/api/schedule`; behavioral
  assertions preserved, none deleted outright — old-editor write-through
  tests replaced by `RetiredLegacyWritersTest` retirement pins).
  `RESTRUCTURE_IMPORTS.md` regenerated (app loses
  api_bedtime_set/api_budgets_windows/time_rules, gains calendar +
  _enforce_bedtime_now).
- PENDING (manager, on the VM): run the suite (expect `Ran 1681, OK`,
  skip-delta vs the 22-skip VM baseline like the backend slice's) + the
  VERIFY-ON-VM checklist at the end of `CALENDAR_DESIGN.md` §10; after
  pull, a human eyeball on aiden-pc (calendar renders with real fonts/
  theme, drag-create a bedtime block, windows section gone from
  /budgets).

## Calendar create-path fix (2026-11-14, `fix/calendar-create`)

The owner on the deployed box: "I don't see any way to add to the
calendar." Root cause, one word: `boot()` in `static/js/calendar.js`
never set **`selectable: true`**. The FullCalendar options carried a
wired `select:` callback (+ `selectMirror`), but the vendored bundle's
shipped default is `selectable:!1` (pinned in
`tests/test_ui_calendar.CalendarCreatePathTest`), so drag-select did
nothing and there was no other create entry point at all. The modal
itself (`openEditor mode:'create'`) was never broken and is untouched
beyond how it is invoked.

Three creation paths now, all landing in that same modal:

1. **Drag-select** — `selectable: true` re-enables the existing flow.
2. **Plain click on a slot/day** — new `dateClick` handler (week: clicked
   hour + 1 h via the existing `hhmm`/`hhmmOfMin` helpers; month/all-day:
   date only, modal time defaults kept).
3. **'+ New block' toolbar button** — `customButtons.newBlock`, wired
   into `headerToolbar` left (`prev,next today,newBlock`); opens the
   modal for the displayed child, today 16:00–18:00.

Two FullCalendar-6 facts reverse-engineered from the minified bundle
(re-read them there before "fixing" this again): a zero-move click fires
BOTH `select` and `dateClick` unless `selectMinDistance` is set (we pin
`selectMinDistance: 5` so clicks belong to `dateClick` exclusively), and
the v6 standard bundle styles **no cursor** for selectable slots — the
crosshair affordance is one app.css rule (`.pc-calendar-card .fc
.fc-timegrid-col-frame`), since zero `crosshair` strings exist in the
artifact. All three paths also `return` early on `editorState` (the
never-clobber-open-edits convention).

- Suite: **Ran 1681 → Ran 1686, OK (skipped=158)** in the container
  (`.scratch/venv`); the +5 are `CalendarCreatePathTest` string pins.
- **Lesson: UI-slice JS has no runner in this suite** — when behavior
  lives in browser code, pin the shipped CONFIG LITERALS
  (`selectable: true`, `dateClick:`, `customButtons`, toolbar id) AND
  mirror each option against the vendored artifact so a typo'd or
  unsupported option fails loudly. The original calendar slice pinned
  assets/hashes but not options, and shipped a dead create path.
- PENDING (manager, on the VM): suite + `CALENDAR_DESIGN.md` §10.1 (step
  3 amended to cover all three paths); human eyeball: click a slot →
  modal with hour prefilled; drag → modal with the range; '+ New block'
  → modal today 16:00–18:00; crosshair over week-grid slots.

## Venv self-heal after an interpreter swap (2026-11-14, `fix/venv-self-heal`)

**Incident.** A Bazzite rpm-ostree image update landed Python **3.15** on
the VM at reboot. `/opt/parental-controls/.venv` had been created by
install.sh from the floating `python3` (its packages live under
`lib/python3.14/site-packages`), so the venv's `bin/python` resolved to
3.15, the 3.14 site-packages silently fell off `sys.path`, and
`parental-controls.service` crash-looped `ModuleNotFoundError: No module
named 'cheroot'` (27 restarts) until the manager `rm -rf`'d the venv and
re-ran install.sh. The installer's guard was a presence check (`[ ! -d
"$INSTALL_DIR/.venv" ]`) — a version-orphaned venv is very much present —
and pip, asked through the same broken venv, reported the requirements
"already satisfied". aiden-pc (the live family box) carried the identical
latent bomb for its next image update.

**Fix.** New `venv_lib.sh` (sourced + preflighted by install.sh, staged
like tls_lib.sh). `venv_healthy` asks the venv interpreter itself: it
derives the distribution names from requirements.txt (no hardcoded list)
and requires each to be both *findable* (`importlib.metadata`) and
*importable* (`flask`, `argon2`, `waitress`, `cheroot` — the incident's
exact failure surface). That fails exactly when orphaned — a new-minor
interpreter never sees `lib/python<old>/site-packages` — and also
catches dangling `bin/python`, half-deleted venvs and purged packages;
it runs `-B` so a root probe writes nothing into the service user's
tree. `ensure_venv` is step 2/8's new decision: keep a healthy venv,
otherwise announce `venv interpreter mismatch/orphaned -> rebuilding`,
`rm -rf`, recreate from the image's current `python3`; the inline
`pip install -r` repair line is byte-comparable. The recovery-CLI site
(step 5b) replaced its bare `-x bin/python` trust with the same probe.
Sites audited: staging excludes (not trust), systemd unit ExecStarts
(panel/enforce/agent — all start only after step 2/8 healed the venv;
their `.venv/bin/activate` is the healed tree), per-child session-agent
unit (runs **system** python3 by design, stdlib-only — no venv trust),
`run.sh`/dev venv (checkout-side, not a deployed artifact). Not
pinning `python3.14`: atomic updates *remove* the old interpreter at
reboot, so a pinned venv becomes a brick (dangling `bin/python`, no
interpreter to repair with) instead of a crash-loop — self-heal is the
permanent pattern.

**Post-fix behavior.** After an image update, a rebooted panel can still
crash-loop until someone runs the installer — but that run is now the
*entire* recovery: step 2/8 detects the orphan (prints the rebuilding
line) and the first run after any update takes ~30s (venv rebuild + pip
install) instead of crash-looping forever. `sudo ./install.sh
--deploy-webserver` is the one-command fix on both VM and aiden-pc; no
manual `rm -rf` ever again.

**Tests.** `tests/test_venv_selfheal.py` (19): probe matrix on throwaway
venvs with fake pip-style `.dist-info` (healthy / orphaned via
lib-rename + pyvenv.cfg version rewrite / dangling interpreter symlink /
metadata-present-but-unimportable / missing and nameless requirements),
requirements-derived naming (specifiers, extras, comments, option lines;
`dash-pkg` -> import `dash`), no-hardcoded-package-list pin on the probe
body, no-bytecode pin, `ensure_venv` replays with scripted `run()`/
`as_user()` stubs (dry-run echoes + real heal inside the tempdir), and
installer wiring pins including the regression pin that neither
presence-only guard survives. Container suite: **Ran 1686 -> Ran 1705,
OK (skipped=158)**, skip delta 0. `TestHomelessVenv`'s pinned-HOME
regexes now read install.sh + venv_lib.sh (the create moved into the
lib).
## Override card above the countdown + live DOM add/remove (2026-11-14, `fix/override-card-live`)

Owner request, user detail page: (1) the "Manual override active" card
renders ABOVE the "Live countdown" card; (2) the card appears in / leaves
the DOM with the override state — previously Jinja-only, so it needed a
page reload.

- **Single markup source:** the card lives in
  `templates/_partials/override_card.html`, included TWICE by `user.html`:
  live (only while `user.in_bedtime and bedtime.manual_override`, so the
  no-JS first paint stays correct and fail-safe) and INERTLY inside
  `<template id="override-card-tpl">`, both immediately before
  `#countdown-card`. Template content is not part of the document, so
  `getElementById` can never see the skeleton — no id collisions.
- **Sync mechanism:** `syncOverrideCard(data)` in the page script, called
  from `refreshUserLive()` right after `renderCountdown(data)` (the same
  10s `/api/user/<u>/live` path). Active = `in_bedtime && manual_override`
  (exactly the server condition). Absent+active → clone the skeleton
  (`cloneNode(true)`) and `anchor.parentNode.insertBefore(live, anchor)`
  before `#countdown-card`; present+inactive →
  `card.parentNode.removeChild(card)`. DOM add/remove, never hide/show
  (owner's wording). The Revoke flow already resyncs through
  `refreshUserLive()`, so the card vanishes on the next tick without a
  reload.
- **Template-skeleton chosen over JS-built DOM** because the card's
  markup (hero classes, `aria-labelledby` heading, revoke button literal +
  `revokeOverride('{{ username }}')` wiring, meta text) is pinned by the
  suite and hand-authored in Jinja: a JS builder would duplicate every
  pinned literal in a language with no test runner here, and drift would
  be invisible. The skeleton is server-rendered ONCE — the clone is
  byte-identical to the server card by construction.
- **Payload (additive only):** new top-level `bedtime_end` (`user.bedtime
  .get('end')`, display 'HH:MM', never a rename/removal) on BOTH
  `app.api_user_live` and `agent.read_user_live` — the JS label re-sync
  uses `fmt12(data.bedtime_end)` (mirror of the `twelve` filter) and only
  when the key is a string, so an older remote agent keeps the
  server-painted label (fail-safe). New
  `tests/test_ui_live_parity.py` pins the live payloads' shared key
  surface: panel-only = {countdown} (the sanctioned overview degradation),
  agent-only = the `_remote_live_context` context extras; adding a key on
  one side only now fails loudly (the pair was previously pinned only by
  this file's prose).
- **a11y:** the card keeps `aria-labelledby="override-heading"`; inserts/
  removes are SILENT, consistent with the page's live-region convention
  (countdown `aria-live="off"`, the status/toast region is the announced
  one; badges already rebuild via innerHTML unannounced). The revoke
  toast remains the user-facing announcement.
- Tests: DOM-order pins (active card and skeleton both precede
  `#countdown-card`), single-source counts, endpoint field pins, and
  JS-literal pins for the sync path (no CDN literals). The countdown
  first-card pin re-pointed its hero locator at the Account hero's unique
  `aria-labelledby="pc-user-title"` (the override card shares the
  `pc-card--hero` class now, including in the skeleton) — intent kept,
  not weakened.
- Suite: **Ran 1686 -> Ran 1695, OK (skipped=158)** in the container
  (`.scratch/venv`); the +9 are the five `test_ui_user` order/sync/
  payload pins + `tests/test_ui_live_parity.py` (4).
- VERIFY-ON-VM (human): open a child in bedtime with an override granted —
  card sits above the countdown; click Revoke override: card disappears
  within the 10s poll tick, no reload. Then grant one from the panel
  (manual unlock at bedtime) and watch the card APPEAR on the next tick,
  likewise without a refresh. Remote-device pages get the same behavior
  (payload parity pinned).
