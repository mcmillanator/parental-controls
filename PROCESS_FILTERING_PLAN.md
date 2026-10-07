# Process Filtering + Per-App Time Budgets (Pause-Not-Kill) — Research & Plan

Research date: 2026-10-07. System: Bazzite (Fedora Atomic, KDE Plasma, Wayland).
Scope: **research and plan only — no code changed.**
Constraint respected: read-only (`ps`, `loginctl`, cgroup reads via sudo); no processes
signalled, no accounts locked, no sessions terminated.

---

## 1. Topic 1 — Process filtering for parents

### 1.1 What we actually observed on THIS system

Test user `tester` (live background session, 31 processes at inspection time) vs
`bluefox` (locked, 0 processes). Full `ps -u tester -o pid,comm,etime,args` output
fell into clear groups:

**A. Session/plumbing the parent should never see (hide by default, never pausable)**

| comm (truncated) | Full path / args | Role |
|---|---|---|
| `dbus-broker-lau` (×2) / `dbus-broker` (×2) | session bus + AT-SPI bus | IPC backbone |
| `(sd-pam)`-type / `systemd`, `dmemcg-booster`, `uresourced` | user manager plumbing | session init |
| `pipewire`, `pipewire-pulse`, `wireplumber` | audio server + session manager | all audio |
| `xdg-desktop-por`, `xdg-permission-` | `xdg-desktop-portal`, `xdg-permission-store` | sandbox/file-chooser portals |
| `gvfsd`, `gvfs-udisks2-vo` | virtual FS + volume monitor | file access |
| `dconf-service` | settings db | app settings |
| `at-spi-bus-laun`, `at-spi2-registr` | accessibility bus | a11y |
| `mpris-proxy` | media-key proxy | media keys |
| `baloo_file` | KDE file indexer | search indexing |
| `obexd` | bluetooth OBEX | bluetooth file transfer |
| `kunifiedpush-di` | push notification distributor | notifications |
| `agent` | `geoclue-2.0/demos/agent` | location agent |
| `bash` (×3), `findmnt` | `ntfs-exfat-monitor-script`, `findmnt --poll` | automount monitoring |
| `srt-logger`, `srt-bwrap <defunct>`, `steam-runtime-l`, `catatonit` | Steam runtime internals | container scaffolding |

**B. Real apps the parent cares about (show, pausable candidates)**

| comm | Full args | Notes |
|---|---|---|
| `bazzite-steam` | `/usr/bin/bash /usr/bin/bazzite-steam -silent` | distro Steam launcher wrapper |
| `bash` | `.../.local/share/Steam/steam.sh ...` | Steam bootstrap |
| `steam` | `.../ubuntu12_32/steam -srt-logger-opened ... -silent` | the actual Steam client |

So of 31 rows, only ~3 describe anything a parent would recognise ("Steam").
Everything else is plumbing. A Plasma desktop session for an actively-logged-in
user (with `kwin_wayland`, `plasmashell`, browsers, games) will be 100+ rows with
the same ~95/5 plumbing/app ratio.

**Additional signals investigated:**

- `ps -o unit` shows `-` for all tester processes: tester has **no loginctl
  session** (`loginctl user-status tester` → "not logged in or lingering") and no
  `systemd --user` manager, so there are **no per-app systemd user units /
  `app-*.scope` units** to key off. On a normally-logged-in Plasma user,
  `systemctl --user status` and `app-*.scope` units DO exist and become a useful
  heuristic later — but they are absent here, so the prototype cannot depend on them.
- `flatpak list --app` (system): Firefox, Steam-adjacent tools (Protontricks,
  ProtonPlus), KDE apps (Haruna, Gwenview, Okular, KCalc), Flatseal, Warehouse…
  Flatpak app IDs (`org.mozilla.firefox`) are the cleanest app-identity signal
  when the child uses Flatpaks, but Steam here is a **native RPM install**, so the
  prototype must handle non-Flatpak apps too.
- `.desktop` `Categories=` coverage is good: 79 files under
  `/usr/share/applications/` carry `Categories=` (e.g. Steam:
  `Network;FileTransfer;Game;`, Firefox: `GNOME;GTK;Network;WebBrowser;`,
  Haruna: `AudioVideo;Player;Video;`). This is the cheapest category source.
- Cross-user inspection as `stan` (the Flask app's user): `/proc/PID/cmdline`
  readable, `/proc/PID/environ` **denied**, `readlink /proc/PID/exe` returned
  nothing (unreliable cross-user). So **full command line (`ps … -o args`) is the
  primary unprivileged identity signal**; anything needing environ/cgroup
  internals needs the existing passwordless-sudo path.
- ⚠️ **Pre-existing cgroups found:** `/sys/fs/cgroup/parental-tester-quarantine`
  (30 of 31 tester PIDs) and `/sys/fs/cgroup/parental-tester-games` (PID 32144
  `steam` only), root-owned, created 09:31 today. `parental-tester-games` reads
  `cgroup.freeze = 1` (currently **frozen** — Steam may be suspended right now).
  These were **not created by this research** (read-only throughout). The owner
  (or another session) is evidently already experimenting with the cgroup freezer.
  Coordinate before touching; see §2.1.

### 1.2 Filtering design options

| Option | How | Pros | Cons |
|---|---|---|---|
| **A. Hardcoded denylist** (comm/exe-path based) | `HIDDEN_DEFAULT = {'pipewire', 'dbus-broker', …}` or path prefixes (`/usr/libexec/`, `…/steam-runtime/`) | trivial; deterministic; works with today's `ps -o comm` pipeline; matches observed data exactly | needs curation; new plumbing names leak through over time |
| B. Heuristic (systemd user units) | hide anything owned by a `*.service` under `session.slice`/`background.slice` | automatic; adapts to new services | **doesn't work here**: no user manager for tester; also hides things inconsistently across login states |
| C. Per-user parent-managed hidden list | parent hides rows manually, persisted per user | fully parent-controlled | Day-1 UX is still 31 rows of gibberish; high burden |
| **D. Combination (recommended)** | A (curated default) + C (parent override), displayed-state rule | clean Day-1 + escape hatch; simplest thing that solves the complaint | two mechanisms to maintain (small) |

### 1.3 Recommendation for prototype

**Option D, Phase 1 = mostly A:**

1. Ship a curated `HIDDEN_DEFAULT` set (comm names + path-prefix rules) derived
   from §1.1 Table A. Path-prefix rules do most of the work:
   `/usr/libexec/`, `/usr/lib/systemd/`, `*/steam-runtime/*`, `geoclue`,
   `at-spi`, `gvfs`, `xdg-`, `dbus-`, `dconf`, plus exact comm matches
   (`pipewire`, `pipewire-pulse`, `wireplumber`, `mpris-proxy`, `baloo_file`,
   `obexd`, `kunifiedpush-distributor`, `uresourced`, `dmemcg-booster`,
   `findmnt`, `srt-logger`, `srt-bwrap`, `steam-runtime-launcher-service`,
   `catatonit`, `(sd-pam)`, `systemd`).
   Keep the Steam launch chain (`bazzite-steam`, `steam.sh`, `steam`) **visible**.
2. Defer the parent-managed override list to Phase 2 (data model already allows it).
3. Defer systemd-unit heuristics until we can test against a genuinely
   logged-in Plasma session (needs `app-*.scope` units to exist).

### 1.4 State & UI

- **State:** follow the existing `bedtime.json` pattern — new sibling file
  `process_prefs.json` next to `app.py`, shape:
  `{ "<username>": { "hidden_comm": ["…"], "shown_comm": ["…"] } }`
  (`shown_comm` re-shows something the default hides; `hidden_comm` hides
  something the default shows). Same load/save helpers, same no-DB philosophy.
- **UI on `/user/<username>`:** process table gains a 4th column with a
  per-row **Hide** button; rows hidden by default get a "why" tooltip
  ("system process — hidden"); a **"Show hidden (N)" toggle** reveals them
  greyed-out with **Unhide** buttons. Row identity for prefs = `comm`
  (documented limitation: comm truncation/collision, see §2.2).
- Backend change is small: `ps` call adds `args` (`ps -u <user> -o pid,comm,etime,args`),
  Python applies default filter + prefs overlay before rendering.

---

## 2. Topic 2 — Per-app / per-category time budgets with pause-not-kill

### 2.1 Pause mechanism options

| Option | Semantics | Pros | Cons / risks |
|---|---|---|---|
| **A. `SIGSTOP` / `SIGCONT` per PID** | `SIGSTOP` cannot be caught/blocked; kernel freezes threads in place; `SIGCONT` resumes. Memory, fds, GPU buffers, files all preserved — work is not lost. | universal (any process); no setup; per-PID precision; well-understood since forever | must enumerate every PID of the app (children forked after STOP need re-STOP); stop a Wayland client and KWin will flag it "not responding" after its ping timeout (~seconds) — cosmetic, clears on resume; stopped audio producers → PipeWire underrun → silence until resume (expected); in-app network timers (Steam, game servers) may time out and disconnect during long pauses — acceptable for a parental control, must be documented |
| **B. cgroup freezer (`cgroup.freeze`)** | write `1`/`0` to `cgroup.freeze`; kernel freezes/thaws the whole group atomically | **atomic group freeze** — no fork-race, no PID enumeration; whole app + children in one write; is the modern systemd-aligned primitive | needs a cgroup per app (setup + delegation); cgroup v2 `cgroup.procs`/`cgroup.freeze` on Bazzite are root-owned (verified: `Permission denied` as stan, readable via sudo) → enforcement daemon needs sudo rule extension (same pattern as existing `usermod`/`loginctl` rules); less familiar to debug |
| C. `flatpak kill` | sends SIGTERM into sandbox | Flatpak-native | **kills** — violates pause-not-kill; rejected |
| D. Session/systemd (`loginctl`, `systemctl freeze`) | `systemd freeze` on a scope = freezer under the hood | clean if per-app scopes existed | scopes don't exist for our test user (§1.1); rejected for prototype |

**Recommendation: Phase 1 = A (SIGSTOP/SIGCONT via process-group or enumerated
PIDs), with B (cgroup freezer) as the named Phase 2 upgrade.**
Rationale: zero setup, works for native Steam today, and the PID-enumeration race
is tolerable at prototype scale (re-sweep PIDs on a short loop while paused).
The owner's apparent in-progress cgroup experiment (§1.1 ⚠️) suggests B is
already on their mind — propose converging there in Phase 2 rather than running
two mechanisms.

**Bazzite/Flatpak wrinkles (from this system):**
- Stopping the **wrapper** (`bazzite-steam`, `steam.sh`, Flatpak `bwrap` parent)
  does **not** stop already-forked children — must signal the whole tree
  (`kill --signal STOP -<pgid>` or walk `/proc`/ps children). Same in reverse
  for `CONT`.
- For Flatpak apps the app processes live under `bwrap`; `FLATPAK_ID` in environ
  would identify them but environ is **not cross-user readable** (§1.1) — use
  `flatpak ps` / `flatpak-coredump`-style tooling or cmdline matching instead.
- Never touch the sandbox supervisor itself in ways that reap children; stop
  leaves and wrapper together, never kill.

**Verification plan for "resumes cleanly" (Phase 1 acceptance):**
STOP the Steam trio for 60 s → CONT → confirm: window repaints, client re-syncs,
no data loss; audio app STOP/CONT → sound resumes; document any KWin
"not responding" transient as expected/cosmetic.

### 2.2 Identifying "an app" and its processes

`comm` alone is ambiguous (three different `bash` rows mean three different
things in §1.1). Signals available, ranked for prototype use:

1. **Full command line** (`ps -o args`) — works cross-user today, distinguishes
   `steam.sh` from `ntfs-exfat-monitor-script`. Primary key for Phase 1.
2. **App-ID rules table** (hand-curated, prototype-scale): e.g.
   `steam = {cmdline-substrings: ['bazzite-steam', 'Steam/steam.sh', 'ubuntu12_32/steam']}`,
   `firefox = {flatpak-id: 'org.mozilla.firefox', cmdline: ['firefox']}`.
   Each app = list of match rules; a PID belongs to the first matching app.
3. Later upgrades (not Phase 1): Flatpak IDs via `flatpak ps`, systemd
   `app-*.scope` units on real login sessions, window→PID via KWin (Wayland
   makes this fiddly; skip), `.desktop` `Exec=` cross-referencing.

**Recommendation:** Phase 1 app identity = curated **app-rules table** matched
against full cmdlines (+ Flatpak ID match when `flatpak ps` shows the app).
Unmatched visible processes fall in "Other" and get no budget until mapped —
this naturally reuses the Topic 1 visible set as the candidate pool.

### 2.3 App categories

Options: (a) hardcoded `app → category` map; (b) `.desktop` `Categories=` fields;
(c) store-curated data (Flathub categories). Coverage check on this box favours
**(b) with (a) as fallback**: 79 `.desktop` files carry `Categories=`, and the
freedesktop main categories map cleanly (`Game`→games, `WebBrowser`→browsers,
`AudioVideo`→media, `Chat`/`InstantMessaging`→chat). Steam's own file says
`Game`. Plan: normalise `Categories=` → small parent-facing set
{games, browsers, media, chat, social, other}; hardcoded overrides for apps
whose desktop entry is missing/misleading (Steam launcher chain → games).

### 2.4 Safety design — the Topic 1 filter as safety boundary

Rule: **only processes visible after Topic 1 filtering (or explicitly
allowlisted in the app-rules table) are pausable. Anything hidden-by-default
can never be a pause target, even if a budget pattern accidentally matches it.**
Enforcement = pause function asserts `pid-comm` not in hidden set before
signalling (defence in depth: check at budget-match time AND at signal time).

**NEVER-pause list (red lines):** compositor/KWin (`kwin_wayland`),
`pipewire` / `pipewire-pulse` / `wireplumber` (stopping these kills audio
system-wide, including the parent's session), `dbus-broker*`, `systemd` /
`(sd-pam)`, `loginctl`/session leaders, `xdg-desktop-portal*` (freezing it
hangs file choosers in every app), the parental-controls Flask app itself and
its Python interpreter, `sshd`/session shells of the admin, and any PID 1 /
kernel threads. Pausing any of these risks a frozen desktop, silent system, or
locking the parent out — the exact failure the owner fears with pipewire.
Additionally: never auto-pause all of a user's processes at once (no "pause
user" button in Phase 1); budgets target apps, not sessions.

### 2.5 Time-budget model sketch

**Usage measurement options:**
- (a) Process elapsed time (`ps` ETIME sum over app PIDs) — trivial, works now,
  but counts background/idle time (Steam idling in tray burns budget).
- (b) Active-window focus time (KWin/Plasma idle + focus tracking) — accurate,
  Wayland-complex, needs a session agent.
- (c) CPU-time accumulation — measures intensity, not attention; bad for games-vs-idle.
- **Recommendation:** Phase 1 = (a) with documented idle-counts caveat; (b) is
  the Phase 3 accuracy upgrade.

**Exhaustion behaviour (Phase 1):** daemon tick (60 s, alongside bedtime loop)
accumulates per-app/per-category minutes; on exhaust → SIGSTOP the app's PIDs,
set a `paused_by_budget` flag in state, and surface "Paused — out of time"
on the user page + toast; **resume is manual by parent only** ("Resume" button
→ SIGCONT + flag clear). No auto-resume, no child self-resume path. Next-day
(or next-window) reset re-arms budgets.

**Phase breakdown:**
- **Phase 1 (minimal viable):** app-rules table (Steam + 2–3 more: Firefox,
  Haruna), daily per-app minute budgets in a new `budgets.json` (same pattern
  as `bedtime.json`), ETIME-based accounting, STOP/CONT enforcement,
  manual parent resume, Topic-1-filter safety gate, NEVER-pause list hardcoded.
- **Phase 2:** cgroup-freezer enforcement (converge with owner's experiment),
  per-category budgets via `.desktop` categories, parent-managed hide/show
  (Topic 1 option C), weekly windows.
- **Phase 3:** focus-based usage measurement, child-facing warnings
  ("10 minutes left" via `notify-send`/`systemd-run`), grace/bedtime interplay.

---

## 3. Open questions for the owner

1. The `parental-tester-*` cgroups + `freeze=1` on the games group predate this
   research — is another experiment in flight, and is Steam currently frozen
   intentionally? Should Phase 2 build on those exact cgroup names?
2. Budget granularity first: per-app (Steam = 60 min) or per-category
   (games = 90 min shared)? Phase 1 implements one — which?
3. Should paused apps auto-resume at the next window (morning), or stay paused
   until the parent resumes even after reset?
4. Daily reset time: midnight, or aligned to bedtime end (e.g. budget renews
   when bedtime ends)?
5. Is ETIME-with-idle-counts acceptable for Phase 1, or is background-Steam
   burning budget a Day-1 dealbreaker?
6. May enforcement eventually need a per-user session agent (for focus time,
   warnings), or must everything stay in the root/Flask layer?

## 4. Safety red lines (non-negotiable)

- Never SIGSTOP/SIGCONT (or freeze) anything in the NEVER-pause list (§2.4) —
  enforced in code at signal time, not just in UI.
- No cross-user kills; no `SIGKILL` in budget enforcement (pause means pause).
- Read-only research discipline stands until an enforcement design is approved.
- Any cgroup work must coordinate with the existing `parental-tester-*` setup,
  not trample it.
