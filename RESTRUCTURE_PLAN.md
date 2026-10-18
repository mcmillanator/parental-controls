# RESTRUCTURE_PLAN.md — making `app.py` / `budgets.py` human friendly

**Status:** plan only. No production code changes in this commit. Companion
machine-generated contract: [`RESTRUCTURE_IMPORTS.md`](RESTRUCTURE_IMPORTS.md)
(regenerate with `.venv/bin/python .scratch/import_graph.py > RESTRUCTURE_IMPORTS.md`).

**Scope:** `app.py` (3 037 lines) and `budgets.py` (3 303 lines) — 6 340 of the
10 895-line flat module set. Nothing else is renamed, moved or edited in phase 1.
Jinja templates, `static/js/`, `sudoers-*` templates, systemd units and
`install.sh` are untouched.

---

## 1. Why this is hard here (three measured facts)

The owner asked to "restructure the files in a human friendly way". Three
things about *this* codebase decide the shape of the answer, and each is
measured rather than assumed.

### 1.1 The tests patch the modules, not the code (490 sites, 35 of 52 files)

`RESTRUCTURE_IMPORTS.md` §4:

- **61 distinct `mock.patch` targets** naming `app.` / `budgets.`
- **490 patch sites** across **35 of the 52** test modules
- 46 of them are *attribute* patches — `patch.object(app, "get_lock_status")`,
  `patch.object(budgets, "BUDGETS_FILE")`, and the
  `patch.object(app.budgets, "load_prefs")` form that reaches *through* the
  facade to the real `budgets` module — and 9 are *submodule* patches:
  `patch.object(app, "budgets")`, `patch.object(budgets, "pwd")`.

A `patch.object(mod, "x")` rebinds one name in one module's globals. Code that
already did `from mod import x` at import time keeps the *original* object and
the patch silently stops working. That is not a cosmetic problem here: the
patched names include `app.subprocess` (65 sites, 8 files) and
`budgets.pwd` / `budgets.subprocess`, i.e. **the guards that stop a unit test
from shelling out to a real `sudo usermod`**. A patch that silently stops
working is a *safety* regression, not just a red test.

**The rule the split must obey**, therefore:

> A concern module never does `from app import x` / `from budgets import x`
> across a split seam. It does `import app` (or `import budgets`) and resolves
> the name **at call time**: `app.get_lock_status(u)`, `budgets.subprocess.run(...)`.
> `app.py` / `budgets.py` stay the only module objects tests patch.

This is proven, not asserted. `.scratch/poc/` + `.scratch/poc2/` are two
runnable 3-file micro-apps:

```
$ .venv/bin/python .scratch/poc_check.py      # `import facade` + facade.x()
patched   -> {'ok': [True, 'mocked']} calls: 1        <- patch reaches the moved code
endpoints -> [('static', ...), ('api_lock', '/api/lock')]   <- endpoint stays BARE

$ .venv/bin/python .scratch/poc2_check.py     # `from facade import x`
patched   -> {'ok': [True, 'lock tester: rc=0']} calls: 0  <- SILENTLY DEAD
AssertionError: PATCH DID NOT REACH
```

Both import orders are safe: importing a view module *before* `app` and
importing `app` first both register the route and keep the patch effective
(`.scratch/poc3_check.py`), because the facade imports the view modules last and
every facade reference is resolved at call time.

### 1.2 Four tests read `app.py` / `budgets.py` as *source*, not as modules

`tests/test_deployment.py` is the sudoers coverage guard — the test that let the
`stan-nopasswd` blanket go away. It AST-parses two filenames:

```python
MODULES = ('app.py', 'budgets.py')     # line 557
```

and additionally:

| site | what it does | breaks when |
|---|---|---|
| `test_deployment.py:648,653` | `ast.parse(read('app.py'))` → finds `lock_user_sessions`, regexes its body | `lock_user_sessions` leaves `app.py` |
| `test_deployment.py:1329` | `read('app.py')` must contain `--machine={username}@.host`, `'--user'`, `'notify-send'`, `'critical'` | `notify_user` leaves `app.py` |
| `test_qa_time_tracking.py:618` | line-slices the `bedtime_loop` body out of `app.py` text | `bedtime_loop` leaves `app.py` |
| `test_budget_locked.py:241` | `ast.parse(read('app.py'))` → finds `bedtime_loop`, regexes `account_locked=lambda…` | `bedtime_loop` leaves `app.py` |
| `test_ui_assets.py:129,146` | `APP_PY = ROOT / 'app.py'`, folded into `TEXT_CORPUS` for the §7.4 pinned-selector inventory | any render code leaves `app.py` |

**These six sites are the only test edits phase 1 needs**, and each one is a
*module-resolution* change (resolve "where does this function live" / "give me
the runtime-module corpus"), never a change of what is asserted. They are
enumerated per-slice in §5.

### 1.3 The `app` name is already a live hazard — in two directions

`app.py:30` is `app = Flask(__name__)`. Inside `app.py` the bare name `app` is
the **Flask instance**; everywhere else it is the **module**. And
`budgets.py:1219` binds a *local* `app` to a dict inside `group_usage_minutes()`,
so `app.get("used_minutes")` in that file is a dict lookup. Two different
meanings of one identifier already coexist; the split must not add a third.
(`RESTRUCTURE_IMPORTS.md` §8.)

---

## 2. Target module map

Vocabulary is taken from the files' own section banners (`app.py` has 24,
`budgets.py` has 28 — see `.scratch/check_structure.py`), so a reviewer can
follow a banner straight into a file. Line numbers are the **current** line
numbers on `master` (`a9bc745`; `ead99e3` + a docs-only `AGENT_CONTEXT.md`
commit, so every `.py` line number is identical on both) and they are the
contract for the execution phase.

### 2.1 `app.py` → 17 modules + facade

| # | module | one-line purpose (banner vocabulary) | from `app.py` | ≈ lines |
|---|---|---|---|---:|
| A0 | `app.py` *(facade)* | the Flask instance, config, jinja filters, shell context processors, named re-exports, route-module imports, `__main__` enforcement loop | 1–131, 3015–3037, + re-export tail | ~260 |
| A1 | `users.py` | human users, lock status, name validation (`get_human_users`, `get_lock_status`, `valid_username`, `MIN_UID`/`MAX_UID`) | 134–135, 151–211 | 65 |
| A2 | `locks.py` | account lock/unlock primitives — `usermod`/`loginctl` via scoped sudo, plus the screen-lock and emergency-logout cores | 214–363, 1145–1237 | 245 |
| A3 | `bedtime.py` | bedtime settings storage, window predicates, lock bookkeeping, canonical duration display | 137–139, 366–417, 568–612, 613–744, 823–862 | 330 |
| A4 | `bonus.py` | bonus time (parent-granted extra minutes past bedtime) — grants, revokes, state, history | 141–148, 420–565 | 175 |
| A5 | `notify.py` | desktop notifications (best-effort warnings before automatic locks) + the actor/audit shims | 747–820 | 75 |
| A6 | `enforce.py` | bedtime enforcement (background thread) — the one enforcement pass | 865–1013 | 150 |
| A7 | `guards.py` | authentication & authorization: request guards and the wheel/frozen/protected-target refusals | 1016–1142 | 130 |
| A8 | `remote_hub.py` | multi-device hub → agent routing: device entries, proxy, remote live context | 1240–1272, 2233–2287 | 90 |
| A9 | `admin_console.py` | admin-account console: store rows, add/remove app admins, audit-detail summary | 1279–1313, 2594–2683, 2996–3012 | 160 |
| A10 | `live.py` | the per-user live view model + the shared countdown | 2119–2179, 2290–2394 | 175 |
| A11 | `views_overview.py` | the Overview page and the no-JS `/overview/*` action forms | 1316–1539 | 225 |
| A12 | `views_api.py` | `/api/users`, lock / lock-screen / unlock / logout, bonus, bedtime, process-prefs, devices + device proxy | 1542–1931, 2744–2824 | 470 |
| A13 | `views_budgets.py` | `/api/budgets*` (limits, overrides, windows, resume/freeze/unfreeze) and `/budgets` | 1934–2116, 2924–2947 | 250 |
| A14 | `views_user.py` | `/user/<username>` and `/api/user/<username>/live` | 2186–2230, 2397–2451 | 100 |
| A15 | `views_auth.py` | login, logout, password change | 2454–2591 | 140 |
| A16 | `views_admin.py` | `/api/admins*`, `/api/audit`, `/admin/audit` | 2686–2741, 2827–2880 | 110 |
| A17 | `views_pages.py` | `/time-rules`, `/processes`, `/settings`, `_pick_child` | 2883–2921, 2950–2993 | 85 |

### 2.2 `budgets.py` → 13 modules + facade

| # | module | one-line purpose (banner vocabulary) | from `budgets.py` | ≈ lines |
|---|---|---|---|---:|
| B0 | `budgets.py` *(facade)* | the budget-model docstring, the patched path constants, named re-exports | 1–128 (docstring + constants), + re-export tail | ~300 |
| B1 | `budgets_guards.py` | admin self-protection (lockout safeguard) + username / comm / category validation | 129–193 | 70 |
| B2 | `budgets_filter.py` | process filtering: hidden denylists, the NEVER-pause safety list, per-user `process_prefs` | 196–381 | 190 |
| B3 | `budgets_attribution.py` | categorization: `.desktop` Categories + curated overrides, `.desktop` index, JVM/flatpak app tokens, display names | 384–786 | 405 |
| B4 | `budgets_format.py` | canonical duration display (`etime_to_minutes`, `format_minutes`, `format_hm`) + `group_by_comm` | 789–906 | 120 |
| B5 | `budgets_proc.py` | reading `ps` and computing per-process CPU deltas | 913–998 | 90 |
| B6 | `budgets_state.py` | budgets state IO + accounting: `blank_entry`, `set_limit`, `budget_status`, per-app usage, reset rules | 1000–1244 | 245 |
| B7 | `budgets_overrides.py` | app overrides and weekly windows: normalize / iterate / set / update / clear | 1246–1660 | 415 |
| B8 | `budgets_cgroup.py` | the cgroup freezer: paths, scoped-sudo read/write, freeze, migrate, buckets, `unfreeze_all_user` | 1662–1934 | 275 |
| B9 | `budgets_watchdog.py` | `parse_managed_cgroup`, the freezer `watchdog_tick`, `verify_freeze_safety`, the freezable-member gates | 1961–2243 | 285 |
| B10 | `budgets_focus.py` | the focus engine (per-user session agent): feed cursor, billing, degrade | 2250–2444 | 195 |
| B11 | `budgets_tick.py` | tick orchestration: summary emission, locked-account bookkeeping, the desktop index, `tick_budgets` | 2446–2535, 2967–3141 | 275 |
| B12 | `budgets_enforce.py` | the per-target enforcement decision (`_enforce_target`) and `account_and_enforce` | 2538–2964 | 430 |
| B13 | `budgets_actions.py` | the agent/UI-facing freeze / unfreeze / resume verbs + the window and bedtime predicates | 3144–3303 | 160 |

Largest resulting file: **470 lines** (`views_api.py`). Nothing exceeds the
500-line "one screen" budget; the check script enforces it (§8).

### 2.3 Dependency direction

```
helpers        users · locks · bedtime · bonus · notify · budgets_guards
               budgets_filter · budgets_format · budgets_proc · budgets_attribution
                     |
services        enforce · guards · remote_hub · admin_console · live
               budgets_state · budgets_overrides · budgets_cgroup
               budgets_watchdog · budgets_focus · budgets_enforce
                     |
views           views_overview · views_api · views_budgets · views_user
               views_auth · views_admin · views_pages
                     |
facade          app.py  (composition root — imports the views LAST)
```

Invariants, all machine-checkable:

1. **No helper imports a service, and no service or helper imports a view.**
2. **No view imports another view.** (The check script fails the build on it.)
3. The facade is the only module that imports views, and it imports them last.
4. Cross-seam references go `import app` / `import budgets` and are resolved at
   **call** time, so they are never import-time edges and cannot form a cycle.
   The check script verifies there is no module-level import cycle today and
   keeps it that way.

---

## 3. Packaging style: flat concern modules (option a)

**Recommendation: option (a), flat modules registered on the one shared Flask
`app` object, with `app.py` and `budgets.py` kept as the facades.**

### 3.1 Why not option (b), real packages `app/` + `budgets/`

Option (b) looks tidier but collides with four things this repo depends on:

| blocker | evidence |
|---|---|
| `app.py` must remain a **file** | `install.sh:241` `[ -f "$SOURCE_DIR/app.py" ]`; `enforce_bedtime_timer.py:15-19` refuses to start unless `app.py` sits next to it; `test_deployment.py` reads the literal filenames |
| `__file__`-anchored runtime paths | `app.py:137 BEDTIME_FILE`, `budgets.py:70 BASE_DIR`, `audit.py:30`, `store.py:49`, `agent.py:58` all derive paths from `os.path.dirname(__file__)`. Inside `app/`/`budgets/` those resolve one level too deep and the panel would read/write a *different* `state.db` / `bedtime.json`. Fixable, but it means a new `paths.py` and a behaviour-bearing change. |
| a directory `app/` and a file `app.py` cannot coexist | the package wins on `sys.path`; `app.py` becomes dead code that still satisfies `install.sh`'s `-f` check — the worst outcome |
| no benefit for the actual problem | the giants are *files*, not *packages*; `import app` already works. The pain is 3 000 lines in one unit, which flat concern modules solve directly |

Option (a) also keeps the whole install story byte-identical: `install.sh`
already `rsync -a --delete`s the **whole tree** with no whitelist (lines
302–319, plus the `tar` fallback), so new top-level `*.py` files are staged
with no `install.sh` edit.

The cost of (a) is file count: 16 runtime modules today → **46** after
(`wc -l *.py`). That is a real cost and it is an **open question** (§9).

### 3.2 The facade shape (exact, so review is trivial)

`app.py` after the split is, in order:

```python
#!/usr/bin/env python3
"""Parental Controls Web UI for Bazzite.   <- the ORIGINAL module docstring,
<the rest of the original docstring>          kept verbatim, now the facade's>

<original imports: subprocess, pwd, html, json, os, re, threading, time,
 traceback, functools, datetime; adminstore, auth, audit, budgets,
 device_policy, remote, store>

app = Flask(__name__)                       <- unchanged, line-for-line
... secret_key, jinja filters, app.config, context processors, NAV_SECTIONS ...

# --- named re-exports: the app.* facade surface ---------------------------
# Every line names its consumer. NO wildcards, ever (see §3.4).
from users import get_human_users, get_lock_status, valid_username  # tests, agent parity
from locks import run_lock_command, lock_user_sessions              # tests, enforce timer
from enforce import enforce_bedtime, bedtime_loop                   # enforce_bedtime_timer.py, __main__
...

# --- routes last: every endpoint registers on the shared instance -------
import views_overview   # noqa: F401  (/, /overview/*)
import views_api        # noqa: F401  (/api/*)
...

if __name__ == '__main__':        <- the enforcement-loop entrypoint, verbatim
    ...threading.Thread(target=bedtime_loop, daemon=True)...
```

| `budgets.py` is the same shape: the original 53-line budget-model docstring
stays (it is the domain spec `SESSION_AGENT_PLAN.md` refers to), the constants
that tests patch (`BASE_DIR`, `PROCESS_PREFS_FILE`, `BUDGETS_FILE`,
`OVERRIDES_FILE`, `CGROUP_ROOT`) stay, then named re-exports, then
`import budgets_*` at the bottom.

### 3.3 Everything downstream stays valid — item by item

| consumer | what it does | why it survives |
|---|---|---|
| `wsgi.py` | `from app import app`; `application = app` | `app.py` still owns `app = Flask(__name__)`; the name is re-exported, never moved |
| systemd units | `waitress-serve wsgi:application` | untouched; resolves through `wsgi.py` |
| `enforce_bedtime_timer.py` | `from app import enforce_bedtime, get_lock_status, load_bedtime, notify_user`; checks `os.path.exists(APP_DIR/'app.py')` | all four re-exported by name **with the consumer named in the comment**; `app.py` remains a file |
| `cli.py` | `import budgets`; `budgets.CGROUP_ROOT`, `parse_managed_cgroup`, `thaw_bucket` | plain `import` only; the three names are re-exported |
| `agent.py` | `import budgets` (24 attributes) | plain `import` only; every name re-exported |
| `device_policy.py` | `import budgets` (`is_admin_protected`) | re-exported |
| `focus_feed.py` | deferred `import budgets` | stays deferred (see §7 risk 2) |
| `install.sh` | `-f "$SOURCE_DIR/<file>"` presence checks + `rsync -a --delete` of the tree | **not import checks** — file-presence only, no edit needed; new `*.py` stage automatically |
| 40 test modules | `import app` / `import budgets` (no test uses `from app import`) | both module objects keep their names |
| 8 test modules | `from budgets import <25 distinct names>` | those names are re-exported from the facade by name |
| 35 test modules | 490 `mock.patch` sites | the call-time-facade rule of §1.1 |

### 3.4 Hard rules for the facades

1. **No `from x import *` anywhere.** Every re-export is listed by name and
   carries a trailing comment naming who needs it (`# wsgi.py`,
   `# 8 test modules`, `# enforce_bedtime_timer.py`). `tools/check_structure.py`
   fails on a wildcard.
2. **No public rename in phase 1.** `app.get_lock_status` is still
   `app.get_lock_status` after the move, forever, because tests bind it.
3. **Re-export order = definition order** in the old file, so a reviewer diffing
   `app.py` old-vs-new reads top-to-bottom.
4. **Comments and docstrings move with the code, verbatim.** No tidy-ups, no
   re-wrapping, no "while I'm here" — a slice's diff must show relocation only.
   House style, and the only way an "empty diff" on the inventory means anything.
5. Section banners (`# ---…` blocks) move with the code they introduce, and
   the two `test_qa_time_tracking` / `test_budget_locked` scanners that break a
   slice at a `# ---` line keep working because the shape is preserved.

---

## 4. Baseline (recorded 2026-10-18, container, `master` `a9bc745`)

Container, Python 3.13.5:

```sh
mkdir -p .scratch && TMPDIR="$PWD/.scratch" .venv/bin/python -m unittest discover -s tests
```

| gate | baseline | must be identical in every slice |
|---|---|---|
| full `discover` | **1401 tests, 109 failures + 1 error, 12 skips** | yes — same set, same names |
| `discover -p "test_ui_*.py"` | **386 tests, OK** | yes |
| `discover -p "test_qa_time_tracking.py"` | **32 tests, OK** | yes |
| `RESTRUCTURE_IMPORTS.md` | committed at this commit | `git diff --exit-code` after every regeneration |
| `tools/check_structure.py` | **5 violations** (5 files > 500 lines) | count must not increase; max size must decrease |

**Per-slice gate:** `.scratch/check_slice.sh` (promoted to `tools/check_slice.sh`
in slice 0) — the runnable form of every row above. It was executed against
this tree to produce the baseline:

```
== full container suite ==  1401 tests … FAILED (failures=109, errors=1, skipped=12)
                             failure set IDENTICAL to baseline
== UI set ==                 386 tests … OK
== test_qa_time_tracking ==  32 tests … OK
== import surface ==         import surface UNCHANGED        (empty diff)
== structure ==              no module-level import cycles … total violations: 5
== import-time smoke ==      app-first ok ('games', 'browsers')
```

The 110 baseline failures are all environment-bound and all already red:
`test_deployment` (101 — needs real `/etc/sudoers.d` renders + real binaries),
`test_multidevice_install` (5), `test_session_agent` (2), `test_budgets` (1 —
`test_real_desktop_index_has_firefox` needs a real Firefox install),
`test_lock_usermod_real` (1 error — needs live PAM).

The VM authoritative suite (1 302 → 1 401 tests, OK, 20 skips) is run **once**,
at the end, by the manager. This plan issues **no live-system command** and
touches nothing outside the container.

---

## 5. Migration slices

One commit each. Every slice is a pure relocation + the named re-export lines,
leaves the full container suite matching §4 exactly, and is undone with
`git revert <sha>`.

| # | commit subject | moves | files touched | extra guard / test edits |
|---:|---|---|---|---|
| 0 | `Add the import-surface and structure measurement tools` | nothing | `tools/import_graph.py`, `tools/check_structure.py`, `tools/check_slice.sh`, `tests/_corpus.py` *(new, additive)* | `.scratch/` versions promoted verbatim; full suite identical |
| 1 | `Split budgets duration display into budgets_format` | B4 `budgets_format.py` (789–906) | `budgets.py`, `+budgets_format.py` | — |
| 2 | `Split budgets validation into budgets_guards and ps reading into budgets_proc` | B1 (129–193) + B5 (913–998) | `budgets.py`, `+budgets_guards.py`, `+budgets_proc.py` | `test_safeguards` patches `budgets.pwd`/`grp` → moved code must use `budgets.pwd`, `budgets.grp` |
| 3 | `Split budgets process filtering into budgets_filter` | B2 (196–381) | `budgets.py`, `+budgets_filter.py` | `test_sqlite*` patch `budgets.PROCESS_PREFS_FILE` → moved `load_prefs`/`save_prefs` read `budgets.PROCESS_PREFS_FILE` |
| 4 | `Split budgets attribution into budgets_attribution` | B3 (384–786) | `budgets.py`, `+budgets_attribution.py` | `_DESKTOP_INDEX_CACHE` is shared by identity (tests `.clear()` it) — re-export the **same dict**, never rebind |
| 5 | `Split budgets state IO and overrides into budgets_state and budgets_overrides` | B6 (1000–1244) + B7 (1246–1660) | `budgets.py`, `+budgets_state.py`, `+budgets_overrides.py` | `budgets.BUDGETS_FILE` (10 test files) / `budgets.OVERRIDES_FILE` (3) resolved through the facade |
| 6 | `Split the cgroup freezer into budgets_cgroup` | B8 (1662–1934) | `budgets.py`, `+budgets_cgroup.py`, **`tests/test_deployment.py`** | first `sudo` argv moves → widen `MODULES` to include `budgets_cgroup.py`; `budgets.subprocess` (1 site) resolved via the facade |
| 7 | `Split the freezer watchdog and category verbs out of budgets` | B9 (1961–2243) + B13 (3144–3303) | `budgets.py`, `+budgets_watchdog.py`, `+budgets_actions.py`, **`tests/test_deployment.py`** | `MODULES` widened again; `parse_managed_cgroup`/`watchdog_tick`/`verify_freeze_safety` are pinned targets in 3 test files |
| 8 | `Split the focus engine into budgets_focus` | B10 (2250–2444) | `budgets.py`, `+budgets_focus.py` | **the cycle-breaker slice** — the deferred `import focus_feed` must stay deferred; `focus_feed` must not gain a module-level `import budgets` |
| 9 | `Split budgets enforcement and the tick loop out of budgets` | B11 (2446–2535, 2967–3141) + B12 (2538–2964) | `budgets.py`, `+budgets_enforce.py`, `+budgets_tick.py` | `budgets.py` becomes a pure facade (≈300 lines); `enforce_bedtime_timer.py` and `cli.py` need **no** edit |
| 10 | `Split human-user enumeration and account locks out of app` | A1 (134–135, 151–211) + A2 (214–363, 1145–1237) | `app.py`, `+users.py`, `+locks.py`, **`tests/test_deployment.py`** | first `app.py` `sudo` argv moves → widen `MODULES`; **`lock_user_sessions` source scanner** switches to "find it wherever it lives" |
| 11 | `Split bedtime storage and window predicates into bedtime` | A3 (137–139, 366–417, 568–612, 613–744, 823–862) | `app.py`, `+bedtime.py` | `app.BEDTIME_FILE` (4 test files) resolved through the facade; `app.subprocess` → `app.subprocess` in `get_lock_status` |
| 12 | `Split bonus time and notifications into bonus and notify` | A4 (141–148, 420–565) + A5 (747–820) | `app.py`, `+bonus.py`, `+notify.py`, **`tests/test_deployment.py`** | **`test_notify_user_uses_the_granted_transport`** resolves `notify_user`'s home module instead of `read('app.py')` — **1 of the 6 enumerated edits** |
| 13 | `Split the enforcement loop and the request guards out of app` | A6 (865–1013) + A7 (1016–1142) | `app.py`, `+enforce.py`, `+guards.py`, **`tests/test_budget_locked.py`**, **`tests/test_qa_time_tracking.py`** | **2 of the 6 enumerated edits**: both `bedtime_loop` body scanners become "resolve the function's home module, then slice its body" |
| 14 | `Split the remote-hub, admin-console and live view models out of app` | A8 (1240–1272, 2233–2287) + A9 (1279–1313, 2594–2683, 2996–3012) + A10 (2119–2179, 2290–2394) | `app.py`, `+remote_hub.py`, `+admin_console.py`, `+live.py` | `app.remote`/`app.device_policy`/`app.audit` submodule patches (4 sites) resolved via the facade |
| 15 | `Move the Overview routes into views_overview` | A11 (1316–1539) | `app.py`, `+views_overview.py` | first route module: prove endpoint names stay **bare** (`url_for('overview_lock')`, `url_for('admin_audit')`) — `test_ui_overview_*` + `test_ui_home` are the guard; **no blueprint** |
| 16 | `Move the JSON API routes into views_api` | A12 (1542–1931, 2744–2824) | `app.py`, `+views_api.py` | the slice that proves the call-time-facade rule at scale — most `patch.object(app, "budgets")` / `patch.object(app, "auth")` consumers exercise routes that land here, and all 490 sites must run unchanged |
| 17 | `Move the budgets and user routes into views_budgets and views_user` | A13 (1934–2116, 2924–2947) + A14 (2186–2230, 2397–2451) | `app.py`, `+views_budgets.py`, `+views_user.py` | `/budgets` has `endpoint='budgets'` explicitly — that pinned endpoint name must survive |
| 18 | `Move the auth, admin and page routes into views_*` | A15 (2454–2591) + A16 (2686–2741, 2827–2880) + A17 (2883–2921, 2950–2993) | `app.py`, `+views_auth.py`, `+views_admin.py`, `+views_pages.py` | `app.py` becomes a pure facade (≈260 lines); **1 of the 6 enumerated edits**: `test_ui_assets.py` `APP_PY` → the runtime-module corpus |
| 19 | `Turn the structure gate on` | nothing | `tools/check_structure.py` (strict default), `RESTRUCTURE_IMPORTS.md` | `--strict` now exits non-zero; `AGENT_CONTEXT.md` gets the new file map in its own later commit |

### 5.1 Per-slice checklist (identical every time)

1. `git checkout -b feature/restructure-sNN` off `master`.
2. Move the range **verbatim** — `sed -n 'A,Bp'` into the new file, comment
   banners and docstrings included, no edits. Delete it from the parent.
3. Append the named re-export lines to the facade, each with a `# who needs it`
   comment, in the original definition order.
4. Inside the moved code, rewrite every cross-seam reference to the call-time
   facade form (`from app import get_lock_status` → `app.get_lock_status`).
   Same-function-local references stay untouched.
5. Regenerate `RESTRUCTURE_IMPORTS.md` → **`git diff --exit-code` must be clean**
   (an empty diff here is the "empty diff" trick: the import surface did not move,
   only where the code is written).
6. Run the §4 gate. `git diff .scratch/baseline_failures.txt` must be empty.
7. `.venv/bin/python tools/check_structure.py` — violations ≤ before.
8. Commit. Never amend a landed slice; `git revert` is the rollback.

---

## 6. Verification matrix

| gate | when | command | pass |
|---|---|---|---|
| container full suite | every slice | `TMPDIR="$PWD/.scratch" .venv/bin/python -m unittest discover -s tests` | 1401 tests, failure set **identical** to `.scratch/baseline_failures.txt` |
| UI set | every slice | `… -p 'test_ui_*.py'` | 386 tests, OK |
| `test_qa_time_tracking` | every slice | `… -p 'test_qa_time_tracking.py'` | 32 tests, OK |
| import surface | every slice | `tools/import_graph.py > RESTRUCTURE_IMPORTS.md && git diff --exit-code` | empty diff |
| no import cycle | every slice | `tools/check_structure.py` | `no module-level import cycles` |
| view isolation | from slice 15 | same script | no `views_* -> views_*` edge |
| file size | every slice | same script | no file > 500; max size strictly decreases |
| no wildcard re-export | every slice | same script | `wildcard re-exports: 0` |
| `python -O` | end of series | `PYTHONOPTIMIZE=1 … -p 'test_ui_*.py'` | unchanged (there are **0** `assert` statements in runtime modules — measured, `RESTRUCTURE_IMPORTS.md` §7) |
| import-time smoke | end of series | `.venv/bin/python -c "import app, budgets, wsgi; print(app.app, budgets.CATEGORIES)"` and the same with `views_overview` imported *first* | both orders succeed (proven by `.scratch/poc3_check.py`) |
| sudo coverage | from slice 6 | `… -p 'test_deployment.py'` | the 102 baseline failures stay the *same* 102; the `code - pinned` and `pinned - code` sub-tests must not appear |
| VM authoritative suite | **once, at the end, by the manager** | the `parental-controls-deploy` skill | 1 401 tests OK on the VM; deploy + `curl -sk https://<bind-ip>:5000/` → 302 |

This plan touches no live system: no `sudo`, no `visudo`, no `systemctl`, no
`journalctl`, no reads outside the project. Every deploy/verify step is left as
a labelled note for the human.

---

## 7. Risk register (ranked)

### R1 — a patch target stops being effective, and a test shells out for real
**Likelihood: high without the rule; ~zero with it.** 490 sites, 35 files;
`patch.object(app, "subprocess")` alone is 65 sites in 8 files and is the only
thing standing between a unit test and a real `sudo usermod`. §1.1's
call-time-facade rule plus the `.scratch/poc2_check.py` counter-example.
**Mitigation:** the rule is stated in the facade's module docstring, enforced by
review, and proven by the fact that all 490 sites still run unchanged in every
slice. Phase 2 may retarget them to the owning module and drop the
`app.subprocess` indirection — that is a *separate*, opt-in series.

### R2 — import cycles / partial modules at import time
**Likelihood: medium.** `budgets` ⇄ `focus_feed` already imports each other, and
both sides **defer** (`budgets.py:2356,2380`; `focus_feed.py:332,347,384`).
That is a load-bearing convention, not an accident. Adding a module-level edge
in either direction produces a `ImportError` at boot — the panel would refuse
to start, and `enforce_bedtime_timer` would exit 1 every minute.
**Mitigation:** slice 8 is dedicated to the focus path and is the slice where
this is tested; `tools/check_structure.py` fails on any module-level local
import cycle; `focus_feed.py` and the focus billing code keep `import` inside
the function bodies, with a comment saying why.

### R3 — name shadowing: `app` module vs `app` Flask instance vs `app` dict
**Likelihood: medium, and it is already half-present** (`app.py:30`,
`budgets.py:1219`). A concern module that does `from app import app` (the Flask
instance) *and* `import app` (the module) in the same file silently breaks one
of them.
**Mitigation:** hard rule — a module uses **one** spelling. A view module does
`import app` and writes `app.app.route(...)`; it never does `from app import app`.
`budgets_*` modules use `budgets.x` and never rebind `app`/`budgets`. The check
script's `cross_attrs` pass is scoped the same way. `RESTRUCTURE_IMPORTS.md` §8
documents it for every future reader.

### R4 — the four source-scanning tests
**Likelihood: certain** — any split moves code out of the two filenames they
read. **Mitigation:** slice 0 lands `tests/_corpus.py` with
`runtime_modules()`, `read_source(name)`, `find_function(name)`; each of the six
sites switches to "resolve the home, then assert the same thing". No assertion
changes. After slice 18 not one test in the repo reads a specific implementation
filename, which is the durable fix.

### R5 — `__file__`-anchored runtime paths move with the file
**Likelihood: certain under packaging option (b); ~zero under option (a)** —
every new module is a sibling of `app.py` in the install dir, so
`BASE_DIR`/`BEDTIME_FILE` are unchanged.
**Mitigation:** option (a) is chosen partly for this. A guard in
`tools/check_structure.py` fails if any *new* module derives a runtime path from
its own `__file__`; paths belong to `store.py` / `audit.py` only.

### R6 — state singletons
`store.py` owns the SQLite connection; `budgets._DESKTOP_INDEX_CACHE`,
`_LAST_AUTO_PAUSE_SIGS` and `_LAST_SUMMARY_SIGS` are module-level dicts that
`test_qa_time_tracking` resets with `.clear()` (in-place).
**Mitigation:** the split moves the *dict objects*, never a rebinding
(`_X = {}` must stay the only definition site). In-place `.clear()` therefore
keeps working through the facade re-export. `tools/check_structure.py` flags any
module that re-assigns a name it did not define.

### R7 — the `__main__` enforcement loop
`app.py`'s `__main__` starts `bedtime_loop` and is **never booted in
production** (`AGENT_CONTEXT.md`: booting it can lock accounts and thaw
`parental-*` cgroups). Moving the loop into `enforce.py` means the `__main__`
block must `import enforce` (or resolve it from the facade) or it will start
with `bedtime_loop` unbound.
**Mitigation:** the `__main__` block moves verbatim into the facade and its
`threading.Thread(target=bedtime_loop, daemon=True)` target is a re-exported
name resolved in `app`'s globals — exactly the semantics today.
`test_budget_locked` + `test_qa_time_tracking` scan that function's body and
fail loudly if the wiring changes. Never run `app.py` to "check it".

### R8 — endpoint names must stay bare
**Likelihood: high if someone reaches for a blueprint** — a blueprint prefixes
every endpoint (`overview.overview_lock`), and templates/JS/tests call
`url_for('overview_lock')`, `url_for('admin_audit')`, `url_for('budgets')`.
**Mitigation:** option (a) has no blueprint by construction; slice 15 is
deliberately the *first* view slice so this is proven early, against
`test_ui_overview_parity.py` and `test_ui_assets.py` §7.4.

### R9 — `install.sh` / systemd
`install.sh` does **file-presence** checks (not import checks — corrected from
the initial blast-radius note) and `rsync -a --delete`s the tree with no
whitelist, so new `*.py` files stage themselves. `systemd/` templates are not
touched. **Mitigation:** nothing to do; `test_deployment.py`,
`test_service_user.py`, `test_install_webserver.py` and `test_tls.py` keep
reading `install.sh` and stay green because `install.sh` is unchanged.

### R10 — file count / reviewer fatigue
46 top-level `.py` files. **Mitigation:** each slice is one commit and one
concern; `tools/check_structure.py` output is the index; the file map lives in
`RESTRUCTURE_PLAN.md` §2. Raised as an open question (§9 Q1).

---

## 8. "Human friendly", for this codebase

Seven measurable properties. `tools/check_structure.py` enforces 1–3 and 5–7
today and is flipped to `--strict` by default in slice 19.

1. **Every runtime file ≤ 500 lines.** A file you cannot hold in one screen is
   the defect the owner named. Today: `budgets.py` 3 303, `app.py` 3 037,
   `agent.py` 1 169, `session_agent.py` 553, `focus_feed.py` 537 — **5
   violations**.
2. **Every module has a docstring that says what it is for**, not what it
   imports. Today every module passes, so this is a *keep it that way* rule —
   the new modules' docstrings are the moved section banners, promoted to
   one paragraph each.
3. **No wildcard re-exports.** `from x import *` is banned outright
   (`wildcard re-exports: 0` today, and it must stay 0).
4. **Section banners survive.** The `app.py` banners (*5-section IA shell
   context*, *Bedtime enforcement (background thread)*, *Account-action cores*,
   *Multi-device…*, *Overview action forms*) and the `budgets.py` banners
   (*Admin self-protection*, *Process filtering*, *Categorization*,
   *NEVER-pause safety list*, *Focus engine*) travel with their code and are the
   vocabulary of the new module names.
5. **No module-level import cycles, and no view module imports another view
   module.** Dependency direction is helpers → services → views → facade.
6. **No dead code.** Phase 1 moves and re-exports; it deletes only the moved
   lines. Anything newly-dead is a phase-2 finding, not a phase-1 sweep.
7. **Names match the domain vocabulary** already used in `PLAN.md`,
   `UI_UX_PLAN.md`, `TIME_TRACKING_QA.md` and the templates' pinned selectors
   (`overview`, `time_rules`, `budgets`, `processes`, `admin_audit`,
   `settings`, `bonus`, `bedtime`, `freeze`, `override`, `focus`). New module
   names are drawn from that list; `views_*` is the only new prefix and it
   names the one thing that is new: HTTP handlers.

### 8.1 The measurement script

`tools/check_structure.py` (promoted from `.scratch/check_structure.py` in
slice 0). It reports, today:

```
files over 500 lines: 5 (agent.py:1169, app.py:3037, budgets.py:3303,
                       focus_feed.py:537, session_agent.py:553)
files without a module docstring: 0 (none)
wildcard re-exports: 0
graph: 16 runtime modules, 27 module-level local edges
no module-level import cycles
total violations: 5
```

After slice 19 the target is `total violations: 0`, with `agent.py` either
split or explicitly out of scope (open question §9 Q3).

---

## 9. Open questions for the owner

1. **46 top-level `.py` files — acceptable?** Option (a) puts 31 new modules
   next to the existing 16. The alternative is two subpackages, which costs the
   `app.py` filename (`install.sh:241`, `enforce_bedtime_timer.py:15`) and the
   `__file__`-anchored runtime paths. Recommendation: accept (a) now; revisit a
   `pcpanel/` + `pcbudgets/` layout only if a third giant appears.
2. **Keep `app.subprocess` / `budgets.pwd` indirection, or retarget now?**
   Keeping it costs ~67 slightly-ugly `app.subprocess.run(...)` call sites and
   buys **zero test edits**. Retargeting is a mechanical but real diff across 9
   test files. Recommendation: keep in phase 1, retarget in a phase-2 cleanup.
3. **`agent.py` (1 169 lines) duplicates 22 helpers from `app.py` on purpose**
   ("mirroring `app.run_lock_command`") across `agent.py:151-501`. If
   `users.py` / `locks.py` / `bedtime.py` / `bonus.py` stay Flask-free and
   stdlib-only — which this plan does — `agent.py` could import them and lose
   ~350 duplicated lines. Worth a separate series, or in scope?
4. **One branch for the whole series, or a branch per slice?** Recommendation:
   one `feature/restructure` branch, 20 commits, reviewed slice by slice, merged
   to `master` after slice 10 (budgets done, both giants' facades stable) and
   again after 18 — so the owner gets two rollback points, not twenty.
5. **Should `RESTRUCTURE_IMPORTS.md` be regenerated and committed per slice, or
   only at the end?** Recommendation: per slice — it *is* the guard, and an
   empty diff is far easier to trust when it is in the same commit as the move.
6. **`views_api.py` lands at 470 lines** — the largest file in the map, close to
   the 500 budget. Split it into `views_api_account.py` / `views_api_devices.py`
   now, or leave the seam for later?