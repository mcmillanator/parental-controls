# Parental Controls — browser QA suite (Katalon Recorder)

A ready-made set of **clicks-in-a-row** that drive the Parental Controls web
panel in a real browser and check what comes back: that you can log in, that a
locked account really is locked, that the Freeze button freezes, that the
10-second auto-refresh keeps working.

You never have to write any of it. You install one small free browser
extension, load **one file**, fill in a handful of variables (including your own
admin password), and press **Play**.

The suite is a single file:

```
katalon/parental-controls.html
```

It is **Selenium-IDE HTML** — one `<table>` per test case, one row per step —
which is exactly the format Katalon Recorder itself writes and reads. It is not
JSON, and Katalon Recorder has **no object repository**: the old
`katalon/objects/*.json` + `katalon/test-cases/*.json` files could not be
imported at all and have been removed. See §7 for how the objects were
flattened into the cases.

> **Nothing here ever runs by itself.** This file is inert HTML on disk. It only
> touches your machine when you press Play inside the Recorder, against the
> address you type into `BASE_URL`.

---

## ⚠️ Read this first (safety)

These tests press real buttons. Real buttons run real commands: they can lock
an account, freeze a category of apps, or grant bonus time.

| Rule | Why |
|------|-----|
| **Only run these against a test account you do not mind messing with.** | The Lock, Freeze and bonus buttons really act on the account on the page. |
| **Never point the suite at an account with a live desktop session you depend on.** | The emergency-logout path is not used here, but a lock is a lock. |
| **Never use an admin (wheel) account as `TEST_USER`.** | The app refuses to freeze an admin — the test will fail loudly instead of doing damage. |
| Each state-changing case **puts things back** before it finishes. | Lock → unlock, freeze → unfreeze, grant → revoke, limit → cleared. Re-check the table in §6 for what each case leaves behind. |

The suite never contains a real username or password — only the placeholders
`<ADMIN_USER>`, `<ADMIN_PASSWORD>`, `<WRONG_PASSWORD>`, `<TEST_USER>`. You type
the real values into the Recorder, on your own machine; they are never written
to this repository.

---

## 1. Install Katalon Recorder

[Katalon Recorder](https://katalon.com/katalon-recorder-ide) is a free
extension that records what you click and replays it. It is the closest thing to
a macro recorder that also *checks* things ("this text must be here").

Pick your browser:

| Browser | Install link |
|---------|--------------|
| Chrome / Chromium / Brave | <https://chromewebstore.google.com/detail/katalon-recorder-selenium/ljdobmomdgdljniojadhoplhkpialdid> |
| Microsoft Edge | <https://microsoftedge.microsoft.com/addons/detail/katalon-recorder-seleniu/hdodkejagjkdomgbiioijegfmiiknoam> |
| Firefox | <https://addons.mozilla.org/en-US/firefox/addon/katalon-automation-record/> |

All three from the official download page: <https://katalon.com/download-recorder>

Notes:

- **You must sign in with a free Katalon account** (or create one) before the
  extension is usable — Katalon's own quickstart says to install it, "then sign
  in with your Katalon account … and you're good to go". A local-only account is
  not enough.
- There is **no separate standalone app** any more — Katalon retired it; the
  browser extension *is* the product. If you see an old "Recorder desktop app"
  download, ignore it.
- **The extension is no longer actively maintained** — Katalon's GitHub repo
  carries the banner "no further feature development, updates, or security
  patches are planned". It still works against a local panel; just do not expect
  fixes. Pin the toolbar icon so the side panel is one click away.
- Everything is stored locally in your browser. Nothing is uploaded (sign-in
  aside, which is with Katalon, not this project).

---

## 2. Point it at your panel

The panel is served over **HTTPS on the live box** at
<https://192.168.122.201:5000> with a **self-signed certificate**. Set
`BASE_URL` to whatever address you actually use:

| Where your browser is | `BASE_URL` | How |
|-----------------------|------------|-----|
| On the same machine as the panel | `https://localhost:5000` | Open it in a normal tab first. |
| On another machine on the LAN (the usual case) | `https://192.168.122.201:5000` | Open that URL in a normal tab first. |
| Tunnelling instead of hitting the LAN address | `http://localhost:5000` | `ssh -L 5000:localhost:5000 <your-user>@<the-machine>` — leave that window open while testing. |

**Expect a certificate warning.** The cert is self-signed, so the browser (and
sometimes the Recorder) will warn the first time; accept it once. The live
fingerprint is
`5A:06:42:8E:A4:38:EA:2E:63:1B:F4:CD:AD:C9:50:A4:7A:B0:91:29:A5:44:64:51:58:F3:CB:1C:AF:B8:74:72`.
If you are comparing builds, confirm it before accepting.

The SSH tunnel is the alternative when you do not want to touch the LAN address
at all: it keeps the session cookie on `localhost`.

### Navigating the redesigned panel

The panel is mobile-first since the UI redesign (`UI_UX_PLAN.md`), so where you
click has changed. The ids and classes the suite targets are unchanged; use this
map to find the controls by hand:

| What | Where it is now |
|------|-----------------|
| **Home** (account list) | The **Overview** nav link (sidebar/app bar) or the **Home** tab in the phone **bottom bar**. This is the `/` index page, and the accounts are now **cards**, not a table. |
| **Admin console** | The **Admin** link (app bar/sidebar) → `/admin/audit`. |
| **Change password**, **Log out** | Inside the **Account** menu in the app bar (top-right): open it first, then click. The suite clicks the menu summary (`Account menu`) and then **Log out**. |
| **A child's page** | Click **Details** on the child's card (or their username). The page keeps a **← Back to Parental Controls** link. |
| Tables | On a phone the remaining wide tables render as labelled cards; the same ids/classes are still in the page. |

The 10-second status refresh and the toast messages behave as before. The
destructive actions (Lock/Unlock, Freeze/Unfreeze, Revoke bonus, grant custom
bonus, Remove admin, Clear schedule) now confirm in a **native `<dialog>`**
element rather than a browser `confirm()` popup — a dialog with a visible
**Confirm** button rather than a system alert. The suite clicks that button at
`#pc-confirm [data-pc-dialog-confirm]`; it never uses `closeAlert`.

---

## 3. Set the Variables

The suite never hardcodes anything. Every value you need is a **variable** you
set once in the Recorder.

**Where:** Test Explorer → **Profiles (Global Variables)**. Click **+** to
create a profile, add one global variable per name below, then
**right-click the profile** and choose **"Use this in a test case"** so the
profile is applied to the case you are about to run.

There is **no separate "save" step for variables** and **no Objects section** in
Katalon Recorder. The only place variables live is the Profiles (Global
Variables) panel described above.

In the suite file a step writes a variable as `${NAME}` (for example
`${ADMIN_PASSWORD}`, `${BASE_URL}`); in the Recorder's UI you just name it
`ADMIN_PASSWORD`. The `${…}` form is how the step knows to substitute.

| Variable | Meaning | Suggested value |
|----------|---------|-----------------|
| `BASE_URL` | Where the panel lives | `https://192.168.122.201:5000` (or your tunnel/localhost URL) |
| `ADMIN_USER` | Your admin username (app account, **not** your Linux password) | your admin name |
| `ADMIN_PASSWORD` | Your admin app password | your admin password |
| `WRONG_PASSWORD` | A deliberately wrong password (used by the negative login test) | `not-the-password` |
| `NEW_ADMIN_PASSWORD` | The new password the change-password test sets (min 12 characters) | something you are willing to replace |
| `TEST_USER` | The account whose page these tests inspect | a spare/test account |
| `TEST_CATEGORY` | Which app category the budget tests touch | `games` (or `browsers`, `media`, `chat`, `other`) |
| `BUDGET_HOURS` | Hours part of the daily limit the budget test sets, then clears | `1` |
| `BUDGET_MINUTES` | Minutes part (0–59) of that daily limit | `0` |
| `BONUS_HOURS` | Hours for the custom bonus grant | `1` |
| `BONUS_MINUTES` | Minutes for the custom bonus grant (0–59) | `5` |

Three rules about these values:

- They live in your browser, not in this repository. The placeholders in the
  table above (`<ADMIN_USER>`, `<ADMIN_PASSWORD>`, `<WRONG_PASSWORD>`,
  `<TEST_USER>`) are documentation only — do not paste a real password into a
  file and commit it.
- **`TEST_USER` must be a real account name you set yourself.** Every case
  targets `/user/${TEST_USER}` and the `#row-${TEST_USER}` card on the Overview.
  The suite deliberately does *not* set `TEST_USER` for you (an earlier version
  did, which overwrote your value with the placeholder and broke the run).
- A few variables are **seeded by the suite** at the start of each case so it is
  self-contained: `TEST_CATEGORY`, `BUDGET_HOURS`/`BUDGET_MINUTES`,
  `BONUS_HOURS`/`BONUS_MINUTES`, and `WRONG_PASSWORD` (stored as the literal
  `<WRONG_PASSWORD>`, which is guaranteed wrong). You can leave those out of
  your profile.

If a variable is missing, the page will navigate to a blank/odd URL (or a
`/user/${TEST_USER}` 404) and the run fails at the first step. That is the safe
failure mode: nothing is clicked.

---

## 4. Load the suite

1. Open the Recorder panel (toolbar icon).
2. In the **Actions sidebar**, click **Open Test Suite** (there is also a
   sidebar menu entry for it). Alternatively, **drag and drop** the file onto
   the test-case container.
3. Pick `katalon/parental-controls.html`.

**Only file names containing `.html` or `.krecorder` are accepted.** The
Recorder's loader returns immediately for anything else —
`if (!f.name.includes(".krecorder") && !f.name.includes(".html")) return;` — so a
`.json` file is silently ignored. If you renamed the file, keep one of those two
suffixes.

You should now see **8 test cases**, numbered in the order they are meant to run
(`01_…` first, `08_…` last). Each case is self-contained: it clears cookies,
logs in, does its checks, and logs out.

This suite is one file, so there is nothing else to import — no objects, no
folders of JSON. See §7 for what happened to the old object files.

---

## 5. Run it — and export it

- **Run a whole case:** pick the test case in the list, press **Play** (▶). The
  log shows each step: pass = green tick, fail = red, with the expected text
  next to it.
- **Run from the middle:** click a step and press **Play from here** — handy when
  a case failed and you only want the tail. (There is no "run a single object"
  any more: objects no longer exist.)
- **Export** (the toolbar button, and the same entry in the Actions sidebar) is
  **code export**: it turns the case/suite into Katalon Studio, Selenium
  WebDriver, and other frameworks' source. It is *not* a JSON round-trip, and
  exporting is not how you save the suite.
- **Saving** a suite (the save/backup path) writes the same Selenium-IDE HTML
  with a **`.krecorder`** extension. If you want to commit a change you made by
  hand, export/record it, then save it and drop the resulting file in as
  `parental-controls.html`.

---

## 6. What each test case covers — and what to watch for

Every case is self-contained: it starts by clearing cookies and logging in, ends
by logging out, and never chains into another case's state changes.

| # | Test case | What it proves | Watch for |
|---|-----------|----------------|-----------|
| 1 | `01_login_wrong_password_then_success` | A wrong password shows *Invalid username or password.*; the right one reaches the index page with the admin panel and "Logged in as …". | Repeated failures trigger a **login backoff** ("Too many failed attempts") for a few minutes. Do not loop this case. |
| 2 | `02_change_password_first_login` | On first login the app forces a change: the "You must change your password before continuing" banner, a wrong "current" password is rejected, then the new password is accepted and the index page loads. | **One-shot per admin.** Run it once on a freshly created admin (`sudo parental-controls reset-password <admin>` or a new admin row). Afterwards it takes the "already changed" branch and just verifies you are logged in. `NEW_ADMIN_PASSWORD` must be ≥ 12 characters and identical in both boxes — otherwise the password becomes the literal `${NEW_ADMIN_PASSWORD}`. |
| 3 | `03_user_details_page_smoke` | Read-only pass of the per-user page: account status text matches the Lock/Unlock button that is offered, the process section renders grouped rows (or the honest "No processes found" message), all five budget rows exist, and the 10-second live refresh completes **without a red error toast**. | Fails if the target has no processes *and* you expected groups — that is the honest message, and it still passes. The 11-second wait is the auto-refresh actually firing. |
| 4 | `04_process_groups_expand_hide_show` | A multi-instance process group expands to a PID detail row and collapses again (`aria-expanded` flips); Hide moves a group to *Show hidden*, Show brings it back. | Needs a test account with **two windows of the same app** open (otherwise the expand step fails with a clear message — single-instance rows are deliberately not expandable). |
| 5 | `05_bonus_time_shortcut_buttons` | Each shortcut (+15m, +30m, +1h, +1h30, +2h) actually grants bonus time: the status line shows *Bonus active … left* and the badge appears next to the account status. Then Revoke clears it. | Ends with no bonus active. If a bonus was already running before the case, revoke it by hand first — the case starts by requiring a clean slate. |
| 6 | `06_bonus_time_custom_grant_and_revoke` | The custom hours + minutes entry confirms in the native dialog, grants, and the badge/status update; Revoke clears it. | The confirm is the **Confirm** button in the shared dialog (`#pc-confirm`), not a browser alert. `BONUS_HOURS`/`BONUS_MINUTES` must total at least 5 minutes or the app refuses. |
| 7 | `07_time_budget_limit_freeze_toggle` | Setting a limit (hours + minutes) adds a **Freeze** button; Freeze shows a *Frozen* badge and an **Unfreeze** button; Unfreeze returns to *OK*; then the limit is cleared so the row goes back to a bare *OK*. | Enter `BUDGET_HOURS`/`BUDGET_MINUTES` **above** what the account has already used today, otherwise the row says *Exhausted* instead of *OK* and the last check fails. Ends with the limit cleared. |
| 8 | `08_manual_unlock_round_trip` | The manual **Lock** button on the details page really locks (status flips to `Locked`, Unlock appears), and **Unlock** really unlocks. This is the automated twin of QA_LOCK_UNLOCK items 2–4. | This locks a real account for a few seconds. Point it at `TEST_USER` only. |

Step counts (for review): 01 = 68, 02 = 74, 03 = 127, 04 = 126, 05 = 134,
06 = 118, 07 = 149, 08 = 136 — **932 steps across 8 cases**.

### How this maps to the manual list in `QA_LOCK_UNLOCK.md`

| Manual item | Covered by |
|-------------|-----------|
| 1. Screen lock | **Not automatable in a browser** — it locks the child's screen on the machine. Lock the screen by hand and confirm by eye. |
| 2. Locked account cannot log back in | `08_manual_unlock_round_trip` proves the button path; the PAM/greeter half still needs you at the physical machine. |
| 3. Manual lock survives bedtime | `08_manual_unlock_round_trip` plus `11_verify_account_status` (status text ↔ button). |
| 4. All lock paths preserve the session | Partly: the suite never clicks Emergency logout (`.btn-logout` is only *checked for presence*). |
| 5. Wheel-admin protection | The app refuses to freeze an admin, so pointing case 7/8 at an admin fails loudly instead of damaging anything. |

---

## 7. How the suite is built (flattening + command mapping)

Katalon Recorder has **no object repository**, so the old 29 reusable objects
were **flattened**: each case is one `<table>`, with the referenced objects'
steps expanded in order, and a `#` comment row naming the original object at
every seam. For example, the start of case 01 is:

```
#                  == 00_setup_reset_session ==
deleteAllVisibleCookies
open               ${BASE_URL}/
pause              800
if                 document.getElementById('password') === null
#                  Setup failed: after clearing cookies, …
assertElementPresent  css=#password
endIf
verifyElementPresent  css=#login …
```

Two objects from the old repository are **not** in any case and were dropped:
`01b_set_admin_credentials` (an opt-in helper) and `90_example_drag_and_drop`
(a `dragAndDrop` template for a future screen). They no longer exist as files.

### Custom step → real Selenese

| Old custom step | Selenese | Note |
|-----------------|----------|------|
| `navigate` / `goToUrl` | `open` | target starts with `${BASE_URL}` |
| `deleteCookies` | `deleteAllVisibleCookies` | **not** `deleteAllCookies` — that command does not exist in Katalon Recorder (`content/selenium-api.js` only defines `doDeleteCookie` and `doDeleteAllVisibleCookies`) |
| `waitFor` (ms) | `pause` | target is the millisecond count |
| `verifyElementVisible` | `verifyElementPresent` | presence, not visibility |
| `input` | `type` | |
| `click` | `click` | |
| `setContext` | `store` | target = value, value-column = variable name |
| `getContext` | `echo` | prints the variable |
| `if` / `elseIf` / `else` / `endIf` | `if` / `elseIf` / `else` / `endIf` | natively supported |
| `closeAlert` | `click` on `#pc-confirm [data-pc-dialog-confirm]` | there is no browser alert any more |
| `abortTest` | a `#` comment carrying the message + the nearest failing assertion (`assertElementPresent` / `assertElementNotPresent`) | there is no `abortTest`/`fail` command |
| `objectReference` | the object's steps inlined | no object repository |
| `dragAndDrop` | *unused* | its example object was dropped |

Locators are written as `css=…` (or `xpath=…`), matching Katalon's
Selenese locator syntax; the ids/classes they name are the ones pinned by
`UI_UX_PLAN.md` §7.4 and the stdlib tests.

The **destructive-action dialog** is now a native `<dialog>`: after clicking
Lock / Unlock / Freeze / Unfreeze / Revoke / Grant custom the suite clicks
`#pc-confirm [data-pc-dialog-confirm]`. `chooseOkOnNextConfirmation`,
`closeAlert` and `waitForAlert` are gone for good.

---

## 8. The CI check (no browser needed)

A stdlib-only unit test keeps the suite honest without running any of it:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_katalon_suite.py' -v
```

`tests/test_katalon_suite.py` parses the HTML suite and fails when:

- the file is not the Recorder's XHTML suite, or a case is missing/renamed;
- a command is not a real Selenese command, `if`/`endIf` do not balance, a
  `pause` is not milliseconds, or an `open` does not come from `${BASE_URL}`;
- a step still expects a browser alert (`closeAlert` etc.), or a case clicks a
  destructive action without confirming in `#pc-confirm`;
- **a locator no longer matches the page** — every `css=` id, class, tag and
  attribute, plus `xpath=` literals, is looked up in `templates/*.html`
  (Jinja-templated ids like `#budget-games-hours` resolve against
  `id="budget-{{ cat }}-hours"`);
- **an expected string no longer exists** — login/change-password error text,
  button labels, toast messages, the 10-second refresh note, status words — in
  `templates/*.html` + `app.py`;
- a budget category is not in `budgets.py`'s `CATEGORIES`;
- an object seam names a missing object, a case does not start with the setup or
  end with the logout, logs in twice, or a state-changing object runs in a smoke
  case;
- the suite mentions a protected account, a checkout path, or a literal password
  instead of a `${VARIABLE}`/placeholder;
- `katalon/README.md` stops describing the suite (or resurrects the removed
  object-repository / variables-table workflow that Katalon never had).

That is what makes the suite self-updating: rename an id in `templates/` and CI
fails immediately, instead of the run failing on the parent's desktop.

---

## 9. Record your own test (primer)

You do not need to edit HTML by hand to add coverage. Record it, then flatten it
into the file:

1. Open the panel in a tab (`BASE_URL`).
2. In the Recorder panel press **Record**, then do the thing by hand (log in,
   click around). Do **not** press the Lock/Emergency-logout buttons unless
   `TEST_USER` is really a test account.
3. Press **Stop**.
4. Name the recording something like `26_verify_bedtime_summary`, and use
   **Play from here / Export** to review it.
5. Save the suite (**`.krecorder`**) and replace `katalon/parental-controls.html`
   with it (adding your new `<table>` case), or hand-add the new `<table>` in the
   same shape as the others.
6. **Open Test Suite** in a fresh session to confirm it loads, then run the CI
   check (§8). It will tell you if a locator or expected string no longer exists,
   if the `if`/`endIf`s do not balance, or if the case breaks the setup/logout
   convention — and it will tell you if you forgot to document the new case in
   this README.

Rules of thumb:

- One `<table>` per case; the case name lives in the `<thead>` cell.
- A plain comment is command `#` with the text as the target.
- Text checks use `glob:` when the exact string has a variable part, e.g.
  `glob:*left*`.
- Use `${VARIABLE}` for anything that differs per machine, and a `${BASE_URL}`
  prefix on every `open`.
- Never write a real password, a real account name or an absolute path.
- Anything you cannot check by reading the DOM (a toast that has already
  auto-hidden, a native OS prompt) is **verify on the live box**, not a claim in
  the suite.

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| **Open Test Suite** shows nothing after you pick the file | The file name does not contain `.html` or `.krecorder`. Rename it and try again — the loader ignores anything else. |
| The run navigates to a blank or odd URL | `BASE_URL` is not set (or the profile is not applied — right-click the profile → **Use this in a test case**). |
| Certificate error / the page never loads | The live box's cert is self-signed; accept it once in a normal tab first, then re-run. |
| Every step fails with "element not found" | You are on `/login` when the case expected `/user/…`, usually because the login steps were skipped. Always run a whole case. |
| Case fails at `#row-${TEST_USER}` or `/user/…` | `TEST_USER` is unset or is not a real account. Set it in Profiles. |
| Login case fails with *Too many failed attempts* | Backoff after repeated failures. Wait a few minutes before re-running. |
| Change-password case does nothing | That admin already changed its password (the flag is one-shot). Create a fresh admin row to re-test. |
| Expand step fails at the guard | The test account has no app running twice. Open two windows of the same app. |
| Budget case ends at *Exhausted* | `BUDGET_HOURS`/`BUDGET_MINUTES` is below what the account already used today. |
| A step stops right after a Lock / Unlock / bonus / log-out click | There is no browser alert any more: the destructive actions confirm in the native `<dialog>`, and **Log out** moved into the **Account** menu. The suite already clicks **Confirm** / opens the menu; if you recorded your own step, add the same clicks. |
| A check on toast text passes when it should not | Toasts keep their last text for a few seconds after hiding and success toasts auto-remove after 6s. The cases check toasts right after the click, but a red toast from an earlier step could linger — look at the actual page if a toast check surprises you. |

### What has *not* been verified

There is **no browser and no Katalon Recorder extension in this environment**, so
the suite has only been validated **statically** (the §8 checks) and by
inspection against the templates. Nobody has pressed Play against the live box.
Specifically **verify on the live box**:

- that the file imports cleanly ("Open Test Suite" → 8 cases, no
  "Unknown command" in the log);
- that clicking the app-bar **Account menu** summary then **Log out** really
  logs out;
- that the `#pc-confirm` **Confirm** click lands after Lock/Unlock/Freeze/
  Revoke/Grant custom (and that the little `pause` before it is enough);
- that `deleteAllVisibleCookies` clears the session on the HTTPS origin.
