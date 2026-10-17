# TIME_TRACKING_QA.md — QA diagnosis: Minecraft untracked & budgets not enforced

QA branch: `qa/time-tracking` (code reviewed at HEAD `9f7603e`).
Role: static analysis + in-tree reproduction tests. **No fixes, no live-system
actions.** Companion: `tests/test_qa_time_tracking.py` (22 tests).

---

## 1. Executive summary

**One root cause explains both symptoms for Minecraft, and it is an
attribution gap, not a freezer bug.** A Minecraft game process is a JVM:
`comm='java'`, full cmdline like
`java … net.minecraft.client.main.Main … --gameDir …/.minecraft …`. Nothing
in the categorization pipeline knows what `java` is:

* `CATEGORY_OVERRIDES['games']` (budgets.py:390-395) lists the Steam chain,
  Proton/Wine, Lutris, Heroic, Bottles — **no `minecraft`, no `java`, no
  launcher-spawn handling**.
* The `.desktop` fallback keys on the *launcher* executable
  (`minecraft-launcher`, `prismlauncher`), never on the running game binary
  (`java`) — `parse_desktop_file`/`build_desktop_index` (budgets.py:458-543)
  plus `categorize` lookups (budgets.py:650-671) can only ever hit the
  launcher basename. (Reproduction:
  `test_parse_desktop_file_keys_on_launcher_binary_not_the_game`.)

Consequences, in order:

1. **Symptom 1 (untracked).** With the system JRE (`/usr/bin/java`, the
   Prism/MultiMC case), `is_user_app` finds no rule (budgets.py:588-623), so
   `apply_filter` auto-hides the row on `/user/<username>`
   (budgets.py:376-377, marked at app.py:2253-2254) — the parent sees
   *nothing*. With the vanilla launcher (java under `~/.minecraft`), the
   home-dir rule makes it visible, but it lands in category **`other`**,
   displayed as bare `java` (app.py:2255-2260 + `annotate_display_names`
   budgets.py:633-647). Neither shows up under *Games* where the parent set
   the limit.
2. **Symptom 2 (no enforcement).** The category loop in `account_and_enforce`
   (budgets.py:2665-2711) bills/collects members only for processes that
   *categorize into the limited category*. Minecraft is never a `games`
   member, so:
   * if nothing else is tracked in games (the launcher GUI is idle in the
     tray → CPU-activity gate closed, budgets.py:2364-2366), the games limit
     never even exhausts → nothing to enforce; **and**
   * if games *does* exhaust via correctly-tracked processes (Steam…), the
     freeze fires over an empty/stale games membership: the JVM is never
     migrated (budgets.py:2390-2391 uses only `members`), so "Games paused"
     appears while the child keeps playing Minecraft. Both reproduced
     (`test_minimized_launcher_while_game_running_burns_nothing`,
     `test_exhausted_games_pauses_but_minecraft_keeps_playing`).
3. **The focus engine has the identical gap** (opt-in per
   SESSION_AGENT_PLAN; KWin `resourceClass` → `wm_class` in
   session_agent.py:314): `map_window_to_category` mirrors `categorize`
   (focus_feed.py:338-352), so a focused *Minecraft* window also maps to
   `other`; the "only bill what you can freeze" gate (focus_feed.py:478-484)
   then drops the seconds (`test_minecraft_window_not_billable_games_empty`).

**Secondary finding (affects all categories, static + live-testable):**
when exhaustion enforcement *does* fail, it fails **silently**. The freeze
state machine records reasons only in the per-user tick `summary['note']`
(ensure-cgroup failure budgets.py:2386-2389; freeze aborted
budgets.py:2394-2405; re-freeze failed 2426-2434), and **both production tick
callers discard the summary** — `enforce_bedtime_timer.py:40-42` and
`app.py:1002-1004` never print it. The journal then shows nothing about a
freeze that silently aborts every 60 s. (`test_tick_summaries_are_dropped_by_the_timer_path`)

So: for Minecraft it is **one root cause with two visible symptoms**; for
"budgets of *tracked* categories not enforcing", the likely culprits are in
the enforcement-gate table (§5) and need live-box verification to rank.

---

## 2. How the pipeline is wired (for context)

* Production tick: systemd `parental-controls-enforce.timer` (every 60 s,
  `systemd/parental-controls-enforce.timer`) → `enforce_bedtime_timer.py` →
  `budgets.tick_budgets(load_bedtime(), now, notify, run_watchdog=True,
  account_locked)` (lines 40-42). The Flask app's own `bedtime_loop` thread
  (app.py:992-1007) only runs when started via `python app.py`
  `__main__` (app.py:2944) — production is `wsgi:application` under waitress,
  so **the timer is the sole enforcement path in production**.
* Snapshot: `list_user_processes` — `ps -u <user> -o pid,comm,etime,time,args`
  + `/proc/<pid>/cmdline` (budgets.py:839-870).
* Billing: `compute_deltas` (budgets.py:873-893) computes **CPU-seconds
  deltas** per pid; `_enforce_target` (budgets.py:2342-2366) bills the tick's
  **wall-clock** seconds (`used[key] += wall`) for any category that has at
  least one member whose CPU delta ≥ `ACTIVE_THRESHOLD_SECONDS` (1.0 s,
  budgets.py:87), capped at 300 s/tick (budgets.py:91, 2583-2595).
* Exhaustion → freeze: `used ≥ limit` → ensure cgroup
  `/sys/fs/cgroup/parental-<user>-<category>` via `sudo -n mkdir -p` →
  migrate member pids via `sudo -n tee cgroup.procs` → read back
  `cgroup.procs` → `verify_freeze_safety` → `sudo -n tee cgroup.freeze 1`
  (budgets.py:1562-1598, 1607-1630, 1650-1749, 2378-2412). Privileges:
  `sudoers-parental-controls-budgets` lines 21-25, rendered by `install.sh`
  with `@SERVICE_USER@`; timer enabled by install.sh:599.

---

## 3. Findings with evidence

F1. **No Minecraft/JVM attribution anywhere.** `CATEGORY_OVERRIDES` games
    tuple (budgets.py:391-395) has no `minecraft`/`java`/`prismlauncher`;
    the string "minecraft" appears nowhere in production code (grep over
    *.py; only synthetic UI-test fixtures mention it, tests/test_ui_app_overrides.py:87).
    JVM → `categorize` returns `other` regardless of index
    (`test_java_matches_no_games_keyword`,
    `test_launcher_desktop_entry_does_not_help_the_jvm`).

F2. **System-JRE Minecraft is auto-hidden from the UI.** `is_user_app`
    (budgets.py:588-623) misses all four rules for
    `/usr/bin/java … net.minecraft.client.main.Main …`; `apply_filter`
    hides anything `not user_app` (budgets.py:376-377, docstring 361-366),
    and the `/user` page renders it under "Show hidden" with `used=''`
    (app.py:2261-2287). "Not being tracked at all" = this row vanishing
    even from the budget cards. (`test_system_jre_java_is_not_a_user_app_and_is_hidden`)

F3. **Vanilla-launcher Minecraft is visible but unidentifiable.** Home-dir
    rule (budgets.py:615-619) makes `~/.minecraft/runtime/…/bin/java` a
    "user app", yet it groups under *Other* displayed as `java`. A parent
    will never read `java` as "Minecraft".
    (`test_vanilla_launcher_jvm_is_visible_but_unnamed_other`)

F4. **Billing never touches games for a JVM.** The category loop visits only
    configured limits (budgets.py:2666-2687); members for `games` exclude the
    JVM (F1), and `other` is skipped entirely when no limit is set — usage
    accrues nowhere. Wall-clock billing requires an *active member of that
    category*; an idle minimized launcher never qualifies
    (budgets.py:2357-2366). (`test_minecraft_jvm_burns_no_games_minutes`,
    `test_minimized_launcher_while_game_running_burns_nothing`)

F5. **Enforcement over an empty membership is vacuous, not blocking.**
    When games exhausts with zero games-members (all gaming is via JVM), the
    state machine takes `not members and not current` → marks
    `paused['games']=True` + notifies, freezing nothing
    (budgets.py:2398-2401). UI then shows "Paused — out of time" while the
    child plays. With a *late-start* after pause, re-evaluation only
    migrates members of the correct category (2413-2434) — the JVM is still
    not a member. (`test_exhausted_games_pauses_but_minecraft_keeps_playing`)

F6. **Tick/UI parity gap (affects every .desktop-categorized app).** The UI
    builds an index per user (app.py:2242, 2249-2256) and a *display-names*
    map covering all installed apps; the tick path passes `index=None` —
    neither caller of `tick_budgets` supplies one (enforce_bedtime_timer.py:40-42,
    app.py:1002-1004; budgets.py:2769 default). Any app whose only games
    signal is its `.desktop` file (Prism launcher!) categorizes to `other`
    at billing time even when the UI groups it under Games.
    (`test_tick_with_no_index_miscategorizes_desktop_named_apps`)

F7. **Focus engine mirrors the same gap + fail-closed billing gate.**
    `map_window_to_category` builds a pseudo-proc from wm_class/app_id and
    calls `categorize` (focus_feed.py:338-352) → `Minecraft` → `other`;
    `_freezable_pids` returns ∅ for `other` (no limit) → interval dropped
    `not_billable` (focus_feed.py:377-389, 478-484). Also: a stale/missing
    feed halts billing entirely for focus-family users
    (`FOCUS_ENGINE_NO_DATA`, budgets.py:2655-2662) — worth checking live
    state (§6 item 7).

F8. **Silent failure of privileged freeze steps.** Every failure path in the
    exhaustion branch appends only to `summary['note']`
    (ensure-cgroup 2386-2389, safety abort 2394-2397 + 2402-2405, freeze
    write failure 2411-2412) and returns. Neither tick caller prints
    summaries; only admin-skip and watchdog messages reach stdout/journal
    (budgets.py:2827-2829, 1982-1998). `save_budgets` failure is swallowed
    (`except OSError: pass`, budgets.py:2870-2874) — enforcement decisions
    would then evaporate before the watchdog reads the same in-memory copy.
    (`test_sudoers_or_perms_failure_freezes_silently`,
    `test_tick_summaries_are_dropped_by_the_timer_path`)

F9. **A stale/foreign PID inside our own `parental-*` group permanently
    aborts freezing.** `verify_freeze_safety` re-reads `cgroup.procs` after
    migration and aborts if any live member is not in this tick's freezable
    set (budgets.py:2067-2087, gate at 2394-2397). Processes left in
    `parental-tester-games`-style groups (the pre-existing experiment noted
    in PROCESS_FILTERING_PLAN.md §1.1 ⚠️ / §3.1) reproduce this exactly:
    exhausted → aborted → note discarded → repeat every tick.
    (`test_stale_cgroup_member_permanently_aborts_freeze`)

F10. **Resume/Unfreeze suppresses enforcement until the daily reset — by
     design.** `resumed[category]` set on manual unfreeze
     (budgets.py:2963-2989) blocks the exhaustion branch (2378-2380) for the
     whole budget day (reset rules budgets.py:1124-1139). A parent who
     clicked Unfreeze once at 16:00 sees "exhausted, never paused again
     today". This is expected behavior but a likely contributor to the live
     complaint; budgets.json's `resumed` map settles it.

F11. **"Never-pause" list does not exempt java, but note `python`.**
     `NEVER_PAUSE_COMMS` (budgets.py:278-292) includes `python`/`bash` —
     so a java/Minecraft JVM *is* freezable once categorized correctly;
     launcher *wrappers* running as `bash` are both hidden-by-default
     (203-224) and never-paused. Good news for the fix plan: no safety
     change is needed to pause JVMs.

F12. **Accounting axis (H2) is fine.** Usage accrues from **wall-clock
     ticks gated by CPU activity**, not from CPU seconds themselves
     (budgets.py:2357-2366; `time=` from ps only feeds the gate via
     `compute_deltas`). A played game (≥1 CPU-s/min trivially true) bills
     the full tick — no undercounting mechanism once attribution works.

Research plan cross-check: `git show research/process-filtering:PROCESS_FILTERING_PLAN.md`
predicted this. Merged into master: curated denylist (F2 machinery),
NEVER-pause list, cgroup freezer (Phase 2 landed early). **Never merged:
the "app-rules table" keyed on full cmdlines (plan §2.2, the missing piece
F1), and its §3.1 coordination note about the pre-existing
`parental-tester-*` experiment groups (F9).** The plan's ETIME-based Phase-1
accounting was replaced by the CPU-gated wall model (F12).

---

## 4. Hypothesis verdicts

| H | Verdict | Evidence |
|---|---------|----------|
| **H1** — Minecraft is a generic JVM; attribution fails; one root cause for both symptoms | **CONFIRMED** (high confidence — pure static + reproduction) | F1-F5, F7; 7 characterization tests + 3 `expectedFailure` DESIRED tests |
| **H2** — CPU-second accounting undercounts tracked apps | **REFUTED** — deltas gate activity, billing is wall-clock | F12, budgets.py:2357-2366 |
| **H3** — enforcement needs several conditions that may be unmet live | **PLAUSIBLE for non-Minecraft categories** — ranked table §5; `ProtectKernelTunables=true` does *not* block cgroupfs writes (submount, not sysfs itself; the unit's "DELIBERATELY ABSENT: ProtectControlGroups" comment confirms the author verified this axis) | F8-F10; two reproduction tests |
| **H4** — timer dead/erroring on the live box | **CANNOT VERIFY HERE** — first live check (§6) | static: install.sh:599 enables it; production has no in-process tick (app.py:2944 is `__main__`-only) |

---

## 5. Enforcement gates — everything that must hold for "exhausted → frozen"

Ranked by estimated likelihood on the live box (given the owner's symptoms
and the research plan's notes):

1. **The process must be a `games` member at tick time** — false for
   Minecraft (F1/F4) and weak for .desktop-only apps (F6 index parity gap).
   *Static-confirmed.*
2. **`resumed[category]` must be unset** — any manual Unfreeze/Resume since
   the last reset suppresses re-pause for the whole day (F10). *Check
   budgets.json.*
3. **`parental-<user>-<category>` must be free of foreign/stale live PIDs** —
   the owner's pre-existing `parental-tester-*` experiment groups (research
   plan §1.1) are exactly the shape that trips
   `verify_freeze_safety` (F9). *Check cgroup.procs contents.*
4. **Rendered sudoers must allow the exact argv** —
   `sudo -n mkdir/tee/cat` regex-anchored to `/sys/fs/cgroup/parental-…`
   (sudoers-parental-controls-budgets:21-25; re-rendered only by
   `install.sh`). A box that deployed before these grants (or where
   `/etc/sudoers.d/` was edited) fails every freeze, silently (F8).
   *Check journal + `sudo -l -U parental-controls`.*
5. **The enforce timer must actually run every minute** (H4) —
   production has no other tick (`__main__` thread is dev-only). *Check
   `systemctl list-timers`, journal.*
6. **State must be persisting** — `save_budgets` OSError is swallowed
   (F8 tail); if `state.db` in the install dir is unwritable, flags/usage
   churn is invisible. *Secondary: locked accounts (note: `get_lock_status`
   treats **NP = no password** as locked, app.py:191-193 → tick bills
   nothing for passwordless child accounts, budgets.py:2845-2865) and
   wheel/admin members (2824-2832) skip entirely.*
7. **Focus-family users with a dead feed halt billing**
   (`FOCUS_ENGINE_NO_DATA`, 2655-2662) — usage stops instead of pausing.
   *Check entry `focus_engine`/`feed_status`.*

Any of 1-7 failing produces zero journal output **today** (F8) — the fix
plan must restore observability regardless of which one bites.

---

## 6. VERIFY ON LIVE BOX — read-only commands for the owner

(All read-only; nothing locks/freezes/signals. Run as `stan`. Install dir is
`/opt/parental-controls`; runtime state — `state.db` with `budgets.json`
mirror — lives **in the install dir**, not /etc.)

```bash
# 1. Is the enforcement loop alive and clean? (H4, gate 5)
systemctl list-timers 'parental-controls*' --no-pager
systemctl status parental-controls-enforce.timer parental-controls-enforce.service --no-pager
journalctl -u parental-controls-enforce.service --since today | tail -40
journalctl -u parental-controls.service --since today | grep -iE 'tick|budget|freeze|watchdog' | tail -40

# 2. What does Minecraft ACTUALLY look like on this box? (H1 evidence;
#    decide launcher variant: argv0 path, comm, gameDir)
ps -eo user,pid,ppid,comm,args | grep -iE 'java|minecraft|prism' | grep -v grep

# 3. Budget state: limits, usage, paused vs resumed (gates 1-2, 6-7)
sudo cat /opt/parental-controls/budgets.json | python3 -m json.tool
#    (state.db is the live truth; budgets.json is its mirror)
# 4. Did default-hiding swallow the JVM row? (gate 1 / symptom 1)
sudo cat /opt/parental-controls/process_prefs.json | python3 -m json.tool

# 5. Cgroup groups: which exist, frozen state, and WHO is inside (gates 3+4;
#    compare with the pre-existing experiment groups from the research plan)
sudo ls -d /sys/fs/cgroup/parental-* 2>/dev/null
for g in $(sudo ls -d /sys/fs/cgroup/parental-* 2>/dev/null); do
  echo "== $g freeze=$(sudo cat $g/cgroup.freeze 2>/dev/null)"
  sudo cat $g/cgroup.procs 2>/dev/null | while read -r p; do
    ps -o user=,comm=,args= -p "$p" 2>/dev/null; done
done

# 6. Are the rendered sudoers grants present & valid? (gate 4)
sudo cat /etc/sudoers.d/parental-controls-budgets
sudo visudo -cf /etc/sudoers.d/parental-controls-budgets
sudo -l -U parental-controls

# 7. Focus engine? (gate 7) — only matters if budgets.json shows
#    focus_engine: "focus"/"no_data" for the child
systemctl --user status parental-session-agent.service --no-pager  # run IN the child's session
ls -l /run/user/$(id -u <child>)/parental-controls/ 2>/dev/null

# 8. .desktop reality for the launcher (what the index does/doesn't know)
grep -rlEi 'minecraft|prism' /usr/share/applications \
  ~/.local/share/applications /var/lib/flatpak/exports/share/applications \
  <child-home>/.local/share/applications 2>/dev/null | \
  xargs -r grep -E '^(Exec|Name|Categories)='
```

Interpretation shortcuts:
* item 2 cmdline contains `/usr/bin/java` or `/usr/lib/jvm/...` → the
  **hidden** variant (F2); `/home/<child>/.minecraft/runtime/...` → the
  *visible-but-`java`-in-Other* variant (F3).
* item 3 `resumed: {"games": true}` → F10 explains symptom 2 (not a bug).
* item 5 `parental-tester-*` groups containing live non-gaming PIDs → F9.
* item 6 missing/older regex-anchored lines than the repo template → gate 4.
* item 1 no timer activity → H4.

---

## 7. Fix plan (for the implementer agent)

Ordered; each lands with the DESIRED tests in `tests/test_qa_time_tracking.py`
un-marked (`expectedFailure` → regular asserts) plus the regression tests
sketched below.

**FIX-1 (root cause): Minecraft/JVM attribution — budgets.py.**
1. Add `'minecraft'` (+ `'prismlauncher'`, `'multimc'`, `'adventure-launcher'`
   spellings as a policy choice) to `CATEGORY_OVERRIDES['games']`
   (budgets.py:391-395). One keyword is enough to flip: `categorize`
   haystack already contains `net.minecraft.client.main.Main` / `.minecraft`
   paths and the lowercase WM_CLASS `minecraft` via
   `map_window_to_category`'s pseudo-proc (focus_feed.py:348-350) — whole-word
   `\bminecraft\b` matches on `.`/space boundaries.
2. Generic launcher-spawn fallback in `categorize` (650-671): when
   `comm`/argv0 misses the index, scan the **cmdline as word tokens**
   against index keys (e.g. tokens from `--gameDir`/`-cp`/`-Djava.library.path`
   paths: `minecraft`). Keeps `java` itself category-neutral (JVM ≠ game by
   default: JetBrains IDEs must not become `games`) — attribution comes from
   app identity in args, mirroring research plan §2.2 "app-rules table".
3. `is_user_app` (588-623) inherits the keyword fix (same
   `_OVERRIDE_RES` scan) → F2's hidden row becomes visible + a legitimate
   pause target (java is not NEVER-pause, F11; no sudoers change).
4. Display name: give the JVM a friendly name (`annotate_display_names`
   fallback to the matched app token, e.g. `Minecraft`, instead of bare
   `java`) so parents can set per-app overrides for it
   (`/api/budgets/overrides` comm='java' currently shows as `java`).
*Tests:* un-xfail DESIRE tests; add: JetBrains-ish `java -jar idea` NOT
games; Discord `java`? n/a; window class `Minecraft` → games (already in
this file as characterization-after-fix); keep
`test_steam_control_*` green (no keyword-collision regressions: check no
existing fixture comm becomes newly-matched by word-boundary).

**FIX-2 (parity): the tick must classify like the UI — app.py +
enforce_bedtime_timer.py.** Build `index = budgets.build_desktop_index(username)`
once per user per tick (or cache per-user with TTL) in `tick_budgets`
(default `index=None` → per-user build; budgets.py:2841-2869) and pass it
into `account_and_enforce`. Do **not** mark `user_app` on the tick snapshot
in the same change (that would silently stop billing auto-hidden processes
in categories with limits — a deliberate behavior change to decide
separately with the owner; today's unmarked snapshot keeps
`apply_filter` default-visible, budgets.py:364).
*Tests:* `test_tick_with_no_index_…` inverted: with the fix, a tick of a
Prism launcher (index built) bills games. Add tick_budgets-level test with
`procs_provider` + faked `load_budgets` (house pattern from
test_freeze_buttons.py).

**FIX-3 (observability): surface every enforcement decision.**
1. `enforce_bedtime_timer.py`: print the summaries returned by
   `tick_budgets` — at minimum, one line per user whose `note` is non-empty
   or whose `froze`/`unfroze`/`degraded`/`skipped` changed; keep it stdout
   (journal). `app.py bedtime_loop` likewise (app.py:1002-1004).
2. `budgets.py`: `audit.safe_log` a `budget.auto_pause_failed` event on
   ensure/verify/write failures (reuse the existing audit hook, cf.
   `_set_frozen_path` which already audits manual paths,
   budgets.py:1667-1688).
3. Consider `save_budgets` failure (budgets.py:2870-2874 `except OSError:
   pass`) to print + audit; watchdog thaws next tick otherwise.
*Tests:* characterization test inverted to assert summary printing/piping
(keep it subprocess-free: call `tick_budgets(act=False, procs_provider=…)`
through a small `emit_summary` helper — factor the printing into a testable
function).

**FIX-4 (stale-member deadlock): budgets.py:2058-2092 + 2394-2405.**
When `verify_freeze_safety` aborts because a live pid in **our own**
`parental-*` group is absent from the freezable set, the group can never be
frozen again (F9). Options (owner decides): (a) before migrating, reconcile
`cgroup.procs`: re-classify stale pids from `/proc` — if owned by the same
user AND not NEVER-pause AND categorizes into this category, *add* them to
`expected`; if it categorizes elsewhere, move it out (write its pid into its
own category group / back to its original group) with an audit line; (b) if
reconciliation is unsafe, at least surface a persistent UI warning +
parent-actionable "rebuild cgroup" button (thaw, migrate known members,
refreeze). Foreign `parental-*` names that don't parse stay report-only
(hard rule).
*Tests:* `test_stale_cgroup_member_permanently_aborts_freeze` stays as a
characterization of the *safety* property, new test for reconciliation
outcome; live: re-run owner experiment after deploy.

**FIX-5 (owner-facing UX for 'other'):** show the *Other* category's usage
and let the parent see why a JVM row isn't in Games *before* FIX-1 lands is
optional; after FIX-1, `other` mainly catches unknown apps. Low priority.

**Deployment risk notes.**
* FIX-1 changes what category new processes bill into from the next tick;
  same-day usage counters are untouched (no migration needed; daily reset
  re-baselines). Parents' *existing* per-app overrides keyed on `java`
  (if any on the live box!) gain a games-classified sibling — check
  overrides.json in live checks before deploy.
* FIX-2 adds a desktop-dir glob per user per minute; cheap (UI does it per
  page render today) but cache it.
* FIX-3 is stdout-only — zero behavior risk, but it may *reveal* gate 4/5
  failures that need their own follow-up (re-run install.sh for sudoers;
  `systemctl enable --now parental-controls-enforce.timer`).
* FIX-4 touches the freeze safety envelope — land after FIX-3 observability
  so regressions are visible.
* Nothing here needs new sudoers grants (cgroup charset unchanged,
  `valid_comm` already pins `[A-Za-z0-9_.-]`, budgets.py:126).

---

## 8. Reproduction suite & baseline delta

`tests/test_qa_time_tracking.py` — stdlib unittest, no live PAM/sudo;
privileged cgroup primitives faked with `unittest.mock`; real
`verify_freeze_safety` exercised with own live pids; scratch via
`TMPDIR=$PWD/.scratch`.

22 tests: **17 characterization** (document current wrong behavior as
*passing*) + **5 `expectedFailure`** (one per confirmed bug, desired
behavior; FIX-1/FIX-2 flip three of them):

* `TestMinecraftAttribution` — F1/F2/F3/F7 + desktop-file proof + steam/firefox controls.
* `TestBillingNeverReachesTheBudget` — F4/F5 exhaustion never happens / minimized launcher.
* `TestTickUiParityGap` — F6.
* `TestEnforcementGateFailures` — F8/F9/F5 (ensure-cgroup fail silent, stale-member abort,
  vacuous pause while JVM plays, timer discards summaries).
* `TestFocusEngineGate` — F7 with the "bill only what you can freeze" gate + firefox control.

Baseline (this worktree, before adding): `Ran 1294 tests — failures=109,
errors=1, skipped=10` (env-bound live-box checks: test_deployment sudoers,
test_multidevice_install, test_session_agent transport grants,
`test_real_desktop_index_has_firefox`, `test_lock_usermod_real` load error).
After adding: `Ran 1316 — failures=109, errors=1, skipped=10,
expected failures=5`. **Delta = 0 regressions; failure set byte-identical**
(`.scratch/base.fails` vs `.scratch/after.fails`). UI set (test_ui_*,
test_ui_a11y, test_ui_assets) green.
