# Parental Controls — browser QA suite (Katalon Recorder)

A ready-made set of **clicks-in-a-row** that drive the Parental Controls web
panel in a real browser and check what comes back: that you can log in, that a
locked account really is locked, that the Freeze button freezes, that the
10-second auto-refresh keeps working.

You never have to write any of it. You install one small free browser
extension, import two folders, fill in four boxes (including your own admin
password), and press **Play**.

> **Nothing here ever runs by itself.** These files are inert JSON on disk.
> They only touch your machine when you press Play inside the Recorder, against
> the address you type into `BASE_URL`.

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
extension that records what you click and replays it. It is the closest thing
to a macro recorder that also *checks* things ("this text must be here").

Pick your browser:

| Browser | Install link |
|---------|--------------|
| Chrome / Chromium / Brave | <https://chromewebstore.google.com/detail/katalon-recorder-selenium/ljdobmomdgdljniojadhoplhkpialdid> |
| Microsoft Edge | <https://microsoftedge.microsoft.com/addons/detail/katalon-recorder-seleniu/hdodkejagjkdomgbiioijegfmiiknoam> |
| Firefox | <https://addons.mozilla.org/en-US/firefox/addon/katalon-automation-record/> |

All three from the official download page: <https://katalon.com/download-recorder>

Notes:

- There is **no separate standalone app** any more — Katalon retired it; the
  browser extension *is* the product. If you see an old "Recorder desktop app"
  download, ignore it.
- Click **Add to Chrome / Add to Firefox / Add to Edge**, then pin the icon.
  Opening the panel: click the Katalon Recorder toolbar icon (it opens a small
  side panel next to your page).
- Everything is stored locally in your browser. Nothing is uploaded.

---

## 2. Point it at your panel

The panel listens on `127.0.0.1:5000` on the machine it runs on. Set
`BASE_URL` to whichever of these you use:

| Where your browser is | `BASE_URL` | How |
|-----------------------|------------|-----|
| On the same machine as the panel | `http://localhost:5000` | Open <http://localhost:5000> in a normal tab first. |
| On a different computer (recommended) | `http://localhost:5000` | Open an SSH tunnel first, then use that address on this machine:<br>`ssh -L 5000:localhost:5000 <your-user>@<the-machine>`<br>Leave that window open while testing. |

Never use a `0.0.0.0` or LAN address for daily testing: the MVP has no TLS, so
the session cookie would travel in the clear. The tunnel keeps it on
localhost.

---

## 3. Fill in the Variables

The suite never hardcodes anything. Every value you need is a **variable** you
set once in the Recorder.

**Where:** Recorder panel → **Variables** (in the sidebar list under the test
grid). Add one row per name below, then press **Save**. In the JSON the values
are written `${NAME}`; in the Recorder's UI they are just `NAME`.

| Variable | Meaning | Suggested value |
|----------|---------|-----------------|
| `BASE_URL` | Where the panel lives | `http://localhost:5000` |
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

Two rules about these values:

- They live in your browser, not in this repository. Do not paste a real
  password into a JSON file and commit it.
- `TEST_USER` and `TEST_CATEGORY` are also set by the object
  `01_set_test_config` at the start of every case, using the placeholder
  `<TEST_USER>`. Edit that object's value (in the Recorder, not in git) or,
  better, set your own `TEST_USER` **after** importing — otherwise the case
  overwrites it with the placeholder.

If a variable is missing, the page will simply navigate to a blank/odd URL and
the run fails at the first step. That is the safe failure mode: nothing is
clicked.

---

## 4. Import the suite

Both folders are plain JSON, one file per test object / test case.

1. Open the Recorder panel (toolbar icon).
2. Go to the **Objects** section (the object repository — the list of reusable
   step sequences; older/newer Recorder builds call it *Objects* or *Object
   Repository* in the sidebar).
3. Open its menu — the **⋮ / gear icon** at the top of that list — and choose
   **Import**.
4. Select **all** the `.json` files in `katalon/objects/` (multi-select them in
   the file dialog) and import them.
   Some Recorder builds accept a whole folder, and you can also drag the files
   from your file manager onto the Recorder panel. Use whichever your build
   offers.
5. Switch to **Test Cases** and repeat: menu → **Import** → all files in
   `katalon/test-cases/`.
6. You should now see 29 objects and 8 test cases, numbered in the order they
   are meant to run (`00_…` first, `05_logout` last).

If your Recorder build looks different (no *Objects* list, a different import
dialog), the ground truth is the Recorder's own **Export**: record any tiny
throwaway object, export it as JSON, and compare its field names with these
files. Recorder renames a few fields between major versions; if a field differs,
rename it in your imported copy — or export the whole suite back out and commit
that instead (see §5).

---

## 5. Run it — and export it back

- **Run a whole case:** pick the test case in the list, press **Play** (▶).
  The log shows each step: pass = green tick, fail = red, with the expected
  text next to it.
- **Run a single object:** pick the object, press **Play**. Objects assume the
  page they need; run them from a session the case set up (easiest: run
  `00_setup_reset_session`, then `01_set_test_config`, then `02_open_login_page`,
  then the object you are poking at).
- **Run from the middle:** click a step and press *Play from here* — handy when
  a case failed and you only want the tail.
- **Export back:** with the case/object selected, menu → **Export** → JSON. Drop
  the file into the matching folder in this repository and commit it. That is
  how a recording you make by hand becomes part of the suite.

---

## 6. What each test case covers — and what to watch for

Every case is self-contained: it starts by clearing cookies and logging in,
ends by logging out, and never chains into another case's state changes.

| # | Test case | What it proves | Watch for |
|---|-----------|----------------|-----------|
| 1 | `01_login_wrong_password_then_success` | A wrong password shows *Invalid username or password.*; the right one reaches the index page with the admin panel and "Logged in as …". | Repeated failures trigger a **login backoff** ("Too many failed attempts") for a few minutes. Do not loop this case. |
| 2 | `02_change_password_first_login` | On first login the app forces a change: the "You must change your password before continuing" banner, a wrong "current" password is rejected, then the new password is accepted and the index page loads. | **One-shot per admin.** Run it once on a freshly created admin (`sudo parental-controls reset-password <admin>` or a new admin row). Afterwards it takes the "already changed" branch and just verifies you are logged in. If it fails at the minimum-length check, `NEW_ADMIN_PASSWORD` must be ≥ 12 characters and identical in both boxes. |
| 3 | `03_user_details_page_smoke` | Read-only pass of the per-user page: account status text matches the Lock/Unlock button that is offered, the process section renders grouped rows (or the honest "No processes found" message), all five budget rows exist, and the 10-second live refresh completes **without a red error toast**. | Fails if the target has no processes *and* you expected groups — that is the honest message, and it still passes. The 11-second wait is the auto-refresh actually firing. |
| 4 | `04_process_groups_expand_hide_show` | A multi-instance process group expands to a PID detail row and collapses again (`aria-expanded` flips); Hide moves a group to *Show hidden*, Show brings it back. | Needs a test account with **two windows of the same app** open (otherwise the expand step aborts with a clear message — single-instance rows are deliberately not expandable). |
| 5 | `05_bonus_time_shortcut_buttons` | Each shortcut (+15m, +30m, +1h, +1h30, +2h) actually grants bonus time: the status line shows *Bonus active … left* (in hours + minutes) and the badge appears next to the account status. Then Revoke clears it. | Ends with no bonus active. If a bonus was already running before the case, revoke it by hand first — the case starts by requiring a clean slate. |
| 6 | `06_bonus_time_custom_grant_and_revoke` | The custom hours + minutes entry confirms, grants, and the badge/status update; Revoke clears it. | The confirm dialog is accepted automatically by the case (`closeAlert`). `BONUS_HOURS`/`BONUS_MINUTES` must total at least 5 minutes or the app refuses. |
| 7 | `07_time_budget_limit_freeze_toggle` | Setting a limit (hours + minutes) adds a **Freeze** button; Freeze shows a *Frozen* badge and an **Unfreeze** button; Unfreeze returns to *OK*; then the limit is cleared so the row goes back to a bare *OK*. | Enter `BUDGET_HOURS`/`BUDGET_MINUTES` **above** what the account has already used today, otherwise the row says *Exhausted* instead of *OK* and the last check fails. Ends with the limit cleared. |
| 8 | `08_manual_unlock_round_trip` | The manual **Lock** button on the details page really locks (status flips to `Locked`, Unlock appears), and **Unlock** really unlocks. This is the automated twin of QA_LOCK_UNLOCK items 2–4. | This locks a real account for a few seconds. Point it at `TEST_USER` only. |

### How this maps to the manual list in `QA_LOCK_UNLOCK.md`

| Manual item | Covered by |
|-------------|-----------|
| 1. Screen lock | **Not automatable in a browser** — it locks the child's screen on the machine. Use `24_manual_lock` (button + confirm + toast) and confirm the screen by eye. |
| 2. Locked account cannot log back in | `08_manual_unlock_round_trip` proves the button path; the PAM/greeter half still needs you at the physical machine. |
| 3. Manual lock survives bedtime | `24_manual_lock`/`25_manual_unlock` plus `11_verify_account_status` (status text ↔ button). |
| 4. All lock paths preserve the session | Partly: `24_manual_lock` never uses Emergency logout (`.btn-logout` is only *checked for presence*, never clicked). |
| 5. Wheel-admin protection | The app refuses to freeze an admin, so pointing case 7/8 at an admin fails loudly instead of damaging anything. |

---

## 7. The object repository — one file per flow

Objects are the reusable pieces the cases assemble. Running order is the file
name order.

| Object | What it does |
|--------|--------------|
| `00_setup_reset_session` | Deletes cookies, opens `BASE_URL`, asserts we land on the login page. The safety net: if setup fails, nothing else runs. |
| `01_set_test_config` | Sets the non-secret per-run context (`TEST_USER`, `TEST_CATEGORY`, `BUDGET_HOURS`, `BUDGET_MINUTES`, `BONUS_HOURS`, `BONUS_MINUTES`, `WRONG_PASSWORD`). |
| `01b_set_admin_credentials` | Optional helper that sets `ADMIN_USER`/`ADMIN_PASSWORD` from placeholders. Not used by any case — edit it locally if you prefer to store them here. |
| `02_open_login_page` | Opens `/login`; checks username, password and the *Log in* button. |
| `03_login_wrong_password` | Types the deliberately wrong password; expects the error banner. |
| `04_login_good_password` | Logs in and asserts the index page (aborts with a hint if the account must change its password). |
| `04b_login_tolerate_must_change` | Same login, but accepts either landing on the index or on the change-password page. |
| `05_logout` | Clicks *Log out* and asserts we are back at the login form. Every case ends here. |
| `06_change_password_verify_form` | Checks the three password fields, the submit button, and the forced-change banner. |
| `07_change_password_wrong_current` | Wrong "current" password → *Current password is incorrect.* |
| `08_change_password_set_new` | Sets the new password (min 12 chars, both boxes) and lands on the index. |
| `09_navigate_to_index` | Loads `/` and checks the table, the admin panel, the 10-second refresh note and the target's row. |
| `10_open_user_details` | Clicks *Details* on the target's row and checks every region of the page exists. |
| `11_verify_account_status` | Cross-checks the status **text** against the status **class** and the Lock/Unlock button that is offered. |
| `12_verify_process_groups` | Grouped process rows render (or the honest "no processes" message). |
| `13_expand_group_detail` | Expands a multi-instance group to its PID detail row, then collapses it. |
| `14_hide_and_show_process` | Hide → *Show hidden* section → Show again (round trip, prefs restored). |
| `15_verify_budget_rows` | All five categories: limit hours + minutes inputs, used, left and status cells. |
| `16_budget_set_limit` | Sets the daily limit (hours + minutes) and checks the Freeze button appears. |
| `17_budget_freeze_toggle` | Freeze → *Frozen* badge + Unfreeze → Unfreeze → *OK*. |
| `18_budget_clear_limit` | Clears the limit (empty/zero minutes) and asserts the Freeze button is gone. |
| `19_bonus_verify_status` | Checks the five shortcut buttons and either the active bonus (badge + Revoke) or the "no bonus" line. |
| `20_bonus_grant_shortcuts` | Clicks each of the five shortcuts, verifying the bonus goes active after each. |
| `21_bonus_grant_custom` | Fills hours + minutes, accepts the confirm, verifies the grant. |
| `22_bonus_revoke` | Revokes and verifies the bonus is gone. |
| `23_auto_refresh_health` | Waits 11 seconds, fails if an error toast is on screen, then re-checks the live regions. |
| `24_manual_lock` | Clicks *Lock*, accepts the confirm, verifies the locked status and the Unlock button. |
| `25_manual_unlock` | Clicks *Unlock*, accepts the confirm, verifies the unlocked status and the Lock button. |
| `90_example_drag_and_drop` | A `dragAndDrop` template, not used by any case — copy it if a future screen needs a drag. |

---

## 8. Record your own test (primer)

You do not need to edit JSON by hand to add coverage. Record it:

1. Open the panel in a tab: <http://localhost:5000>.
2. In the Recorder panel press **Record**, then do the thing by hand (log in,
   click around). Do **not** press the Lock/Emergency-logout buttons unless
   `TEST_USER` is really a test account.
3. Press **Stop**.
4. Name the recording something like `26_verify_bedtime_summary`, and use
   *Group steps into object* (the Recorder's object-repository command) so the
   recording becomes a reusable object rather than a case.
5. Export that object as JSON (menu → **Export**), drop it into
   `katalon/objects/`, and commit it.
6. Create a new test case: start from `00_setup_reset_session`, add your
   object, end with `05_logout`. Export it into `katalon/test-cases/` and
   commit it.
7. Run the CI check (§9). It will tell you if you referenced a missing object,
   used an object twice, or forgot the logout — and if a selector you picked
   no longer exists in the page's HTML.

Rules of thumb for the JSON:

- Every step carries `type`, and the fields the Recorder exports:
  `selector` + `parentSelectors` (empty list when not nested) + `value`.
- Text checks use `glob:` when the exact string has a variable part, e.g.
  `glob:*left*`.
- Use `${VARIABLE}` for anything that differs per machine, and a `${BASE_URL}`
  prefix on every `navigate`/`goToUrl` — the CI check enforces both.
- Never write a real password, a real account name or an absolute path.

---

## 9. The CI check (no browser needed)

A stdlib-only unit test keeps the suite honest without running any of it:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_katalon_objects.py' -v
```

`tests/test_katalon_objects.py` parses every JSON file and fails when:

- a file is not valid JSON, or is missing `name` / `steps` / `uuid`;
- two files share a `uuid`, or a `uuid` is malformed;
- a step type is unknown or is missing one of its fields;
- **a selector no longer matches the page** — every `#id`, `.class`, tag and
  attribute, plus XPath text literals, is looked up in `templates/*.html`
  (Jinja-templated ids like `#budget-games-hours` resolve against `id="budget-{{ cat }}-hours"`);
- **an expected string no longer exists** — login/change-password error text,
  button labels, toast messages, the 10-second refresh note, status words — in
  `templates/*.html` + `app.py`;
- a budget category is not in `budgets.py`'s `CATEGORIES`;
- a test case calls a missing object, calls it with a stale `uuid`, does not
  start with the setup object, does not end with the logout, or logs in twice;
- an object is never used (except the two documented helpers);
- a smoke case runs a state-changing object (lock/unlock/freeze/grant only run
  in the case that exists for them);
- the JSON mentions a protected account, a home/checkout path, or a literal
  password instead of a `${VARIABLE}`/placeholder;
- this README stops describing the suite.

That is what makes the suite self-updating: rename an id in `templates/` and CI
fails immediately, instead of the run failing on the parent's desktop.

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| The run navigates to a blank or odd URL | `BASE_URL` is not set (or is set in a different place than you think). Set it in **Variables**. |
| Every step fails with "element not found" | You are on `/login` when the case expected `/user/…`, usually because the login object was skipped. Always run a whole case. |
| Login case fails with *Too many failed attempts* | Backoff after repeated failures. Wait a few minutes before re-running. |
| Change-password case does nothing | That admin already changed its password (the flag is one-shot). Create a fresh admin row to re-test. |
| Expand step aborts | The test account has no app running twice. Open two windows of the same app. |
| Budget case ends at *Exhausted* | `BUDGET_HOURS`/`BUDGET_MINUTES` is below what the account already used today. |
| Recorder version has different field names | Recorder renamed some step fields between major versions. Export one of your own objects, compare, and rename in your imported copy (or export the suite back to JSON and commit that). |
| A check on toast text passes when it should not | Toasts keep their last text for a few seconds after hiding. The cases check toasts right after the click, but a red toast from an earlier step could linger — look at the actual page if a toast check surprises you. |