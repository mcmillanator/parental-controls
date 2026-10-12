# Multi-Device Support — Plan (research/design only)

Status: **proposal — not started, no code in this branch.**
Date: 2026-10-11
Base: `master` @ `bde1ba3` (release-ready 1.1 baseline).
Implementer: a separate agent (this file is the design contract for that work).

This plan is research and design only. It changes no code, no tests, no
systemd units, no `install.sh`, no sudoers, no live accounts. The implementer
shall follow the hard rules in `AGENTS.md` (never touch `tester`/`bluefox`/
`stan`, never restart `parental-controls.service`, venv-only Python, feature
worktree etiquette, no checkout-path hardcoding in artifacts).

---

## 1. What "device" means, and the topologies that matter

### 1.1 Definition

A **device** is a Bazzite box running its own install of this app: its own
`state.db` (bedtime/budgets/process_prefs), its own `auth.json`, its own
`audit.jsonl`, its own enforcement timer, its own sudoers grants, and its own
set of **local** user accounts (children) the box's panel can act on. Today
the app is one install per box with **zero** cross-box knowledge; the panel,
the enforcement timer, the cgroup freezer paths
(`/sys/fs/cgroup/parental-<user>-<category>`), and the audit chain are all
local to one machine.

In the multi-device world: **one box hosts the panel ("hub"), the other boxes
run a thin remote agent**. The hub is where the parent logs in; the agents are
headless (no UI) and only answer authenticated machine-to-machine calls from
the hub. Each box keeps acting on its own accounts: enforcement, freezing,
audit, and admin-tier resolution stay local to the box that performs them.

### 1.2 Realistic topologies the owner likely needs

| Topology | Shape | Phase |
|---|---|---|
| **A. One panel + one managed box** | Hub on parent's box A; agent on kids' box B. Parent sees and moderates B from A. | **Phase 1 (ships first)** |
| **B. One panel + several managed boxes** | Hub on A; agents on B, C, … (living-room box, kids' machines). Single panel lists all devices, per-device state. | Phase 2 |
| **C. Settings federation (sync-only)** | Same bedtime/budget rules applied to one child on several boxes; enforcement stays local per box. | Phase 3 (optional) |

The three things the owner can want to do "from the other box" are:

1. **Read** — see users, lock status, budgets, live processes, audit on box B.
2. **Live commands** — lock/unlock/freeze/terminate/bonus on box B right now.
3. **Settings sync** — make box B's bedtime/budget rules match box A's.

Live commands (2) are the core product — the same verbs the local panel has
today. Reading (1) is required to make (2) usable. Settings sync (3) is
orthogonal and harder (conflict resolution); it is deliberately phased last.

### 1.3 The honest constraints this starts from (do not design them away)

- **The panel is localhost-first today.** Bind is `127.0.0.1:5000` by default;
  LAN requires explicit opt-in (`--tls` or the deliberately-flagged
  `--insecure-lan`); loopback stays plain HTTP for SSH tunnels
  (`README.md` §Security Note, `PLAN.md` Architecture, `RELEASE.md` Known
  limitations). A remote control channel must follow the **same discipline**:
  loopback + tunnel by default, LAN + TLS only as an explicit opt-in, plain
  HTTP over LAN refused unless knowingly flagged.
- **Child accounts never authenticate** (`AUTH_DESIGN.md` "Non-negotiable").
  The hub↔agent link is therefore **machine-to-machine** — a bearer token the
  child can never obtain — never a user session, and never anything a child
  could mint from a browser.
- **Admin identity is per-box.** Admins are resolved against the *local*
  `wheel` group at request time (`auth.is_wheel_member` reads
  `grp.getgrnam('wheel')`; `budgets.is_admin_protected` likewise). A "wheel
  admin" on box A is not necessarily one on box B. The cross-box invariant
  that follows is stated in §4.4.
- **Per-box state, per-box enforcement, per-box audit.** SQLite `state.db`
  (three writers today: panel, 60s thread, enforce timer), `audit.jsonl`
  (hash-chained **per file**, sequence restarts per file), the cgroup freezer,
  and the `parental-controls-enforce.timer` are all local. Multi-device must
  not move any of these off their box.

---

## 2. Architecture comparison

### 2.1 Option 1 — Central panel + per-box agent (RECOMMENDED)

The existing per-box logic (lock/unlock, freeze/thaw, bedtime/bonus, audit,
user discovery) stays exactly where it is. A thin **agent** process runs on
each managed box and exposes the *same privileged operations* over HTTP,
authenticated by a per-box bearer token instead of a cookie session. The hub
panel authenticates its human the normal way, then acts as a client: it
forwards the parent's intent to the target box's agent, which executes it
with that box's own sudoers and that box's own `wheel` checks.

Key point: **the agent adds no new privileged surface.** It runs as the same
dedicated `parental-controls` service user, uses the same rendered sudoers
files, the same `state.db`, the same `audit.jsonl` — the exact binaries and
cgroup paths the panel already uses on that box. The only new file is a
0600 token store; the only new network listener is loopback by default.

The implementation shape that matters: the privileged verbs currently live
inside `@require_admin` Flask views in `app.py` (e.g. `api_lock` runs
`sudo usermod -L`, then `note_manual_lock`, then `lock_user_sessions`, then
audits — with `_wheel_target_refused` inside the handler, not only in the UI,
per `AUTH_DESIGN.md` §Request handling). The implementer must **extract the
privileged verb bodies into plain functions** (`_do_lock(username, actor)` …)
callable from both the session-authenticated web view and the
token-authenticated agent view. The `wheel` refusal, `valid_username`, and
audit calls stay inside those functions so neither surface can bypass them.

**Failure modes** are covered in §4. **Token/TLS** in §4.2/§4.3.

### 2.2 Option 2 — Federated / sync-only settings

Share bedtime/budget *settings* across boxes (push/pull or central copy);
enforcement stays local. There is no remote command channel.

- **Pros:** no remote privileged actions, smallest trust surface; matches how
  a young child on several boxes with "one schedule" would be governed.
- **Cons:** it does not give the owner the headline feature — moderating box B
  from box A *now* (lock, bonus, unfreeze). It also inherits the project's
  existing last-writer-wins whole-document semantics (`store.py`:
  "whole-document writes are last-writer-wins"; `RELEASE.md` Known
  limitations) with no resolution story unless we build one. It is the right
  **Phase 3** addition once live control exists, not the right first shippable.

### 2.3 Option 3 — Remote-CLI bridge over SSH

The hub shells out `ssh boxB sudo parental-controls <verb> …`, reusing the
per-box recovery CLI (`cli.py`) and per-box sudoers. No new agent.

- **Cons (rejected):**
  - The service user is nologin, homeless, locked (`install.sh` creates it
    that way) and deliberately never owns SSH keys; provisioning keys for it
    breaks that model, and using a human account breaks least privilege.
  - SSH known-host TOFU, host-key rotation, and `sudo` over SSH interact
    badly with the hardened units (`ProtectSystem=strict`, `PrivateTmp`).
  - It bypasses the app's own response shape (JSON + fail-closed),
    re-implements error/refusal parsing from text, and has no native way to
    carry the *actor* for audit.
  - The agent in Option 1 is literally the HTTP form of the same scoped
    operations — same sudoers, same wheel checks — without any of this.

### 2.4 Decision

**Option 1: central panel (hub) + per-box agent, hub-and-spokes.** Reasons:
(a) it keeps every safety invariant exactly where today's code enforces it —
on the box that executes the action, with that box's live `wheel` membership
and that box's sudoers, so the cross-box wheel/who-is-admin rules stay the
local rules; (b) the agent shares the service user, sudoers, state store, and
audit chain with the existing per-box install, so it adds almost no new
privileged surface; (c) it naturally supports Phase 2 (many spokes) and
Phase 3 (settings federation layered on top) without re-architecting; and
(d) the hub keeps the localhost-first, TLS-opt-in, child-never-authenticates
ethos intact — the default control channel is loopback + SSH tunnel, exactly
how the panel itself is reached today. Sync-only is deferred to Phase 3; the
SSH bridge is rejected outright.

---

## 3. Current single-instance assumptions being relaxed (audit of the code)

These are the concrete "this box" assumptions any multi-device feature must
confront. The design in §4 keeps each of them local; only the *addressing*
becomes remote.

| # | Assumption | Where it lives today | Multi-device treatment |
|---|---|---|---|
| 1 | Users are discovered from the **local** `/etc/passwd` | `app.get_human_users()` (`pwd.getpwall()`, UID >= 1000 not-nologin) | Agent runs on the box; discovery stays local. Hub never discovers remote users itself. |
| 2 | Lock/unlock state read via **local** `sudo passwd -S` | `app.get_lock_status()` | Same — executed by the agent on its box. |
| 3 | Lock/unlock/lock-screen/terminate run **local** `sudo usermod -L/-U`, `loginctl` | `app.api_lock`, `api_unlock`, `api_lock_screen`, `api_logout`; scoped sudoers `sudoers-parental-controls` | Same binaries, same sudoers — but invoked through the agent process on that box. |
| 4 | **Wheel refusal** checks the **local** wheel group at request time | `_wheel_target_refused()`; `budgets.is_admin_protected()`; `auth.classify_admin()` | **Must stay local to the executing box.** The agent re-evaluates refusal against *its* `wheel`, never the hub's. This is the single most important cross-box invariant (see §4.4). |
| 5 | Admin identity/credential store is per box | `/etc/parental-controls/auth.json` + live local `wheel` | Hub admins authenticate to the hub. Box B's admin rows are irrelevant to who may *drive* B; B's `wheel` membership is what protects B's admins from being locked. |
| 6 | Sessions + CSRF guard every state change | `@require_admin` + `auth.validate_csrf` | Hub→agent link is token-authenticated, not session/cookie, so **no CSRF on the agent link** (bearer tokens in headers; see §4.5). The hub's own web UI keeps session+CSRF unchanged. |
| 7 | Bedtime/budget/process-prefs state is **local** SQLite | `store.py` (`state.db`, WAL, busy timeout, last-writer-wins documents) | Agent writes the same store on its box. State never lives on the hub. Settings *sync* (Phase 3) would add replication — explicitly out of scope Phase 1. |
| 8 | Enforcement timer runs locally and independently | `parental-controls-enforce.{service,timer}` + the app's 60s `bedtime_loop` thread; `enforce_bedtime_timer.py` | **Unchanged.** Each box keeps enforcing its own children. Editing box B's bedtime/budget from the hub goes through the same save path the local UI uses, so B's own timer picks it up with no schema or cadence change. |
| 9 | Cgroup freezer paths are **local** | `budgets.cgroup_path()` → `/sys/fs/cgroup/parental-<user>-<category>`; watchdog only touches its own box's tree | Unchanged; the agent only ever addresses its own `/sys/fs/cgroup`. There is **no remote cgroup path** anywhere in the design. |
| 10 | Audit chain is **local, per file** | `audit.py` — sha256 chain, sequence restarts at 1 per `audit.jsonl` | Each box keeps its own chain. The hub *reads* remote audit and shows each box's `chain_ok` computed on that box (see §4.6). No cross-box chaining in Phase 1. |
| 11 | Emergency logout is the only session-terminating verb | `api_logout` (+ `_req_actor` audit) | Available remotely via the agent, with the same refusals and the same per-box audit. |
| 12 | Bind/TLS stance is per box, localhost-first | `run.sh`, `wsgi.py`, `install.sh` (`--bind`, `--tls`, `--insecure-lan`), `tls_lib.sh`, `tls_serve.py` | Agent gets the same localhost-first default (`127.0.0.1:<port>`), plain HTTP only on loopback, LAN requires self-signed TLS + TOFU fingerprint (reuse `tls_lib.sh` conventions) or the `--insecure-lan`-style explicit flag. |
| 13 | CLI recovery is a per-box root backstop | `cli.py` → `/usr/local/bin/parental-controls` | Stays per box; the hub is **never** a recovery path (AUTH_DESIGN: a forgotten password must never lock the parent out; an offline box must remain locally recoverable). |

---

## 4. Recommended architecture in detail

### 4.1 Components

**Agent (per managed box B, C, …):**

- A separate minimal WSGI app (`wsgi_agent.py` + agent routes module, e.g.
  `agent.py`) served by waitress on its own port (**default `127.0.0.1:5001`**)
  under its own systemd unit `parental-controls-agent.service`, reusing the
  panel unit's hardening block (`ProtectSystem=strict` etc.); it does not
  touch the panel service or the `enforce` timer.
- Routes under a versioned prefix `GET /api/v1/…` only. No HTML, no sessions,
  no CSRF. Token auth (below).
- The agent **reuses** the existing per-box modules: `budgets.py`, `store.py`,
  `audit.py`, `auth.py` (for `is_wheel_member`, `valid_username`), and the
  extracted verb functions from `app.py` (see §2.1). It must not import the
  Flask `app` object or the session machinery.
- It writes to the same `state.db` and `audit.jsonl` as everything else on its
  box (SQLite WAL + busy timeout already cope with three writers; a fourth is
  fine — verify in tests).

**Hub panel (box A):**

- A new store `/etc/parental-controls/devices.json` (0600, service-user
  owned) mapping: friendly name → {base URL, token fingerprint, box hostname,
  registered-at, optional TOFU fingerprint of the agent's TLS cert}.
- A small client module (`remote.py`) using **stdlib `urllib.request` +
  `ssl.SSLContext`** (no new runtime dependency; the project deliberately runs
  a lean `requirements.txt` — flask, argon2-cffi, waitress, cheroot). A thin
  transport abstraction so `requests` could be swapped in later without
  touching callers.
- New panel routes: `GET /api/devices` (list + per-device reachability) and a
  per-device prefix for proxied reads/commands, e.g.
  `GET/POST /api/device/<name>/v1/…`. All under the existing `@require_admin`
  + CSRF rules (the **human** still authenticates with a session; the *box* is
  addressed by the stored token).
- UI: a device selector on `index.html` and `user.html` that scopes the
  existing tables to the selected box (read state from the agent) and sends
  commands to the selected box. No new page needed in Phase 1; the device
  chip/selector is the seam.

### 4.2 Token handling

- **Per-box token, not a shared secret.** Box B's token is distinct from box
  C's, so stealing one box's token controls only that box — no lateral
  movement to the whole fleet. A shared fleet token is explicitly rejected.
- Generated at agent-install/registration time (`secrets.token_urlsafe(32)`,
  ≥ 256 bits), stored:
  - On the agent: `/etc/parental-controls/agent_token` (0600, owned by the
    `parental-controls` service user on that box — same file protection
    pattern as `auth.json`).
  - On the hub: inside `devices.json` (0600). The hub and agent are both
    hardened service-user processes; tokens at rest get the same OS-level
    protection as `auth.json`, which is the established trust model here.
- Provisioning (no UI, no child-accessible path): extended **root-only CLI**
  verbs — `parental-controls agent-token generate <box>`, `rotate`,
  `revoke` — plus an installer flag on the managed box. This keeps token
  minting out of any browser surface, matching "child accounts never
  authenticate" (a child can never obtain a token via the panel).
- **Never log tokens.** The agent's logging/failure paths must redact the
  `Authorization` header (tested). `audit.py` entries never contain tokens.
- Rotation = revoke old + mint new on the agent, and update `devices.json` on
  the hub. A rotated-out token yields 401 with a clear "re-register device"
  state on the hub (see failure modes §4.7).

### 4.3 TLS for the control channel

- **Default (Phase 1): loopback + SSH tunnel, plain waitress HTTP on
  127.0.0.1:5001** — byte-identical in spirit to how the panel itself is
  reached today (`ssh -L 5001:localhost:5001 <user>@boxB`). Zero new
  network surface.
- **Opt-in LAN (Phase 2):** the agent serves HTTPS via cheroot with an
  install-time self-signed certificate, **reusing the existing `tls_lib.sh` /
  `tls_serve.py` conventions** (`--tls-cert/--tls-key`, `--tls-name`,
  `--regen-tls-cert`, install-time fingerprint printed). The hub pins the
  agent's certificate by **TOFU fingerprint at registration** (recorded in
  `devices.json`); a fingerprint mismatch on reconnect is refused with a
  clear message and requires re-registration.
- Plain-HTTP over a **LAN** bind is refused by the installer/agent unless the
  operator explicitly passes an `--insecure-lan`-style flag **and** the hub
  UI shows a persistent warning banner for that device (same pattern as the
  existing `INSECURE_LAN` banner, `app.py:43`).
- No client certificates in Phase 1; mTLS is a possible Phase 3
  hardening listed in open questions.

### 4.4 How the frozen-account / wheel protections span boxes

**Rule: protection is evaluated on the box that would perform the action,
against that box's live system state.**

- The agent runs every privileged verb through the shared verb functions,
  which still call `_wheel_target_refused(username)` (and
  `budgets.is_admin_protected` for auto-lock paths) against **its own**
  `grp.getgrnam('wheel')`. So:
  - A hub admin who is `wheel` on box A cannot lock a `wheel` member of box B
    through the panel — the agent on B refuses, exactly as B's local panel
    would. 
  - A hub admin who is *not* an admin on B still cannot lock B's wheel
    members. The protection is **per machine**, which is the correct model:
    being unprivileged on B is not a waiver of B's admin protections.
- `auth.classify_admin` / `is_wheel_member` are never evaluated on the hub
  for remote targets; the hub only checks its own admin session (that the
  human is allowed on the hub) and then lets the target box decide target
  protections. This is the "thin remote agent within a thin client" property:
  the hub cannot accidentally widen privileges by misjudging another box's
  wheel membership.
- The cgroup freezer's foreign-group rule ("report, never touch") stays
  per box (`budgets.watchdog_tick`), and the agent only ever touches
  `parental-<user>-<category>` on its own box.

### 4.5 Agent API surface (Phase 1)

Versioned, bearer-token, JSON only. Command verbs reuse the existing handler
semantics via the shared functions:

```
GET  /api/v1/health                 # liveness; no token; returns app version + box id
GET  /api/v1/users                  # get_human_users + get_lock_status + bedtime summary (mirror of /api/users)
GET  /api/v1/user/<u>/live          # mirror of /api/user/<u>/live (processes, budgets, freeze state)
GET  /api/v1/audit?limit=N          # audit.read_entries + verify_chain, computed on this box
POST /api/v1/lock                   # usermod -L + screen-lock + note_manual_lock + audit (verb body of api_lock)
POST /api/v1/lock-screen            # verb body of api_lock_screen
POST /api/v1/unlock
POST /api/v1/logout                 # emergency logout (lock + terminate-user) — the only session-ender, verb body of api_logout
POST /api/v1/freeze|unfreeze        # budgets freeze/thaw a category (verb body of /api/budgets/freeze|unfreeze)
POST /api/v1/bonus|bonus/revoke
POST /api/v1/bedtime                # set/clear bedtime for a user
POST /api/v1/process-prefs          # hide/show a comm for a user
```

- Auth: `Authorization: Bearer <token>`. Constant-time compare
  (`secrets.compare_digest`) against the agent's token; absent/wrong/rotated
  token → 401. **No CSRF here** — bearer tokens in headers are not
  cookie-based, so browser CSRF does not apply; the hub's session+CSRF for its
  own web UI is untouched. Document this explicitly in the implementer's
  security notes.
- Every write carries the human actor from the hub: header
  `X-Panel-Actor: <hub username>` (and optionally `X-Panel-Id`). The agent
  records it in its own `audit.jsonl` as the `actor` (e.g. `remote:stan@hubA`).
  The agent **never** trusts a claimed actor for *authorization* — only for
  the audit trail; authorization is the hub session (human) + the box's own
  target checks (wheel refusal etc.).

### 4.6 Audit / tamper-evidence across boxes

- Each box's chain stays its own, append-only, hash-linked
  (`audit.py`). The hub reads each box's audit via `GET /api/v1/audit` and
  displays `chain_ok` computed **on that box** — the hub never stores or
  mangles remote entries, so it cannot accidentally "fix" a tamper break.
- Actions taken through the agent appear in the target box's chain with the
  hub actor (above) plus a `via: remote` marker on the detail; the hub's own
  chain records the dispatch (`device.command` with device name + result).
- Cross-box *anchoring* (e.g. chaining box B's tip into box A's chain, or an
  epoch/merkle anchor) is explicitly **out of scope for Phase 1** and listed
  as an open question; the per-box `chain_ok` display is the Phase 1
  guarantee. The audit is tamper-*evident*, not encrypted — unchanged.

### 4.7 Failure modes

| Scenario | Behavior |
|---|---|
| Box B offline / agent down | Hub shows the device as **unreachable** with no stale data for it (fail visible; no local cache, no fabricated state). Live commands fail fast with a clear error. Reads retried only for transient transport errors; no retry storms, no queued commands (a "lock in 10 minutes" queue would be new state — reject). |
| Agent wedged | Per-box recovery CLI (`parental-controls unlock/unfreeze-all/reset-password`) remains the backstop, exactly as today. The hub is never a recovery path. |
| Token rotated/revoked | Agent answers 401; hub flags the device as "token rejected — re-register" and disables commands to it until `devices.json` is updated via CLI. |
| Token leak | Per-box scope limits the blast radius to one box; rotation is a root-only CLI action. Never logged (tested). |
| TLS fingerprint mismatch (Phase 2 LAN) | Hub refuses the connection and marks the device "certificate changed — re-register"; no silent TOFU re-pin. |
| Box B reboot | Agent unit is `Restart=always` + enabled at boot (same as the panel). Hub polls health on its normal refresh cadence (10 s cells today) and marks reachable again. |
| SQLite multi-writer contention (agent + panel + enforce timer on B) | WAL + busy timeout already handle three writers; the agent is the fourth. Add a test that concurrent agent and timer writes serialize (no torn state, fail closed). |
| Clock skew between boxes | Audit entries are box-local UTC; the hub displays each box's timestamps verbatim and never merge-sorts relying on cross-box wall-clock order. |
| Hub admin not wheel on target | Not an error — the agent's own wheel refusals still protect the target box's admins (see §4.4). |

---

## 5. Phasing, tests, rollout/rollback

### 5.1 Phase 1 — one hub + one agent, live control over loopback (ships first)

Scope:
1. Extract privileged verbs from `@require_admin` views in `app.py` into
   shared functions (behavior-preserving; existing 526 tests stay green).
2. Agent process: `agent.py`/`wsgi_agent.py`, token auth, API surface of
   §4.5, systemd unit `parental-controls-agent.service` (loopback :5001), no
   new sudoers, no new requirements.
3. Hub client `remote.py` + `devices.json` store + CLI verbs
   (`device add/remove`, `agent-token …` on the target box).
4. Hub routes `/api/devices` and `/api/device/<name>/v1/…` under
   `@require_admin`+CSRF; device selector in the existing templates; offline
   handling; per-box audit tab (`chain_ok` shown).
5. Tests (§5.3), docs (README, RELEASE, AUTH_DESIGN or a new
   MULTIDEVICE section), `install.sh --enable-agent` on managed boxes.

Explicitly **not** in Phase 1: settings federation, mTLS, cross-box audit
anchoring, mesh (box B driving box A), agent UI, token expiry.

### 5.2 Phase 2 — fleet

- Many agents; per-device status page/list in the hub; device management UI
  (rename, remove, re-register); LAN control channel with self-signed TLS +
  TOFU pinning (reusing the TLS tooling); optional per-device mute/alert
  states; agent health surfaced like the existing health-check timer
  (failed-unit style surfacing, never auto-restart of anything remote).

### 5.3 Phase 3 — settings federation (optional; order set by owner)

- Replicate bedtime/budget **settings** (not live state) to multiple agents;
  enforcement stays local. Needs an explicit conflict-resolution design:
  recommend per-device divergence tracking (each box keeps its own last-writer
  version; hub shows "drifted" boxes with one-click re-apply) rather than a
  global last-writer-wins, given the existing whole-document semantics.

### 5.4 Test strategy

Follow the existing suite's conventions (stdlib `unittest`, mocked
subprocess/sudo calls, Flask test client, live tests only against throwaway
accounts with teardown, forbidden targets enforced).

| Area | Approach | Est. tests |
|---|---|---|
| Agent token auth | missing/wrong/rotated/malformed token → 401; constant-time compare; no CSRF needed; never logged | ~12 |
| Agent verbs (mock subprocess) | lock/unlock/lock-screen/logout/freeze/unfreeze/bonus/bedtime/process-prefs through the shared functions; validation `valid_username`; audit actor written as `remote:<hub>` | ~30 |
| **Cross-box wheel refusal (most important)** | agent refuses lock/freeze/terminate of a `wheel` member of **its own** box even when the caller is wheel on the hub; `is_admin_protected` paths refuse auto-lock remotely | ~6 |
| Hub remote client (mock agent over HTTP) | `remote.py` against a stub agent: reads, writes, TOFU mismatch, 401, unreachable box fail-visible | ~18 |
| devices.json store | add/remove/rotate, 0600, corrupt → fail closed, never logs tokens | ~8 |
| Audit across boxes | agent appends to its local chain with hub actor; `chain_ok` computed per box; hub never rewrites remote entries | ~8 |
| Offline/edge | unreachable → "unreachable" state + no partial writes; retry policy; concurrent agent+enforce write serialization | ~6 |
| Regression | the existing 526-test suite must stay green unchanged (behavior-preserving refactor gate) | 526 |
| Live (skippable) | real two-box live test only when a second Bazzite VM is available (env-gated skip); otherwise the live tests use the existing single-box throwaway-account pattern against the agent | 2–4 |

**Estimated new tests: ~85–95** (growing the suite from 526 to roughly
**610–620**, plus 2–4 environmental skips for the optional two-box live
tests).

### 5.5 Rollout

1. On each managed box B first: `sudo ./install.sh --enable-agent` (stages
   code, renders+starts `parental-controls-agent.service` on 127.0.0.1:5001,
   mints the agent token, prints it once), then set up the SSH tunnel
   `-L 5001:localhost:5001 boxB`.
2. On hub A: `sudo parental-controls device add <name> --url
   http://localhost:5001 --token <printed-once>` (writes `devices.json`
   0600). Smoke-test: hub shows B reachable; lock/unlock a **throwaway**
   account on B via the hub; verify B's audit shows the remote actor.
3. Feature-gated by the presence of `devices.json`: with no devices, the hub
   behaves byte-identically to today — single-box mode is the default and the
   rollout is invisible to existing installs.

### 5.6 Rollback

- Remove the device on the hub (`device remove <name>` or delete
  `devices.json`) → hub reverts to single-box mode immediately; no code change
  needed, and no change to any box's state.
- Disable the agent on B (`systemctl disable --now parental-controls-agent`)
  and delete the token file. No sudoers edits, no state migration; B's panel
  and enforcement keep working untouched.
- If the refactor regresses the hub: `install.sh` is the update *and* rollback
  mechanism today (stages code, preserves state, restarts) — re-installing the
  previous release restores the old behavior; `state.db`/`audit.jsonl` are
  untouched by the agent work.

---

## 6. Open questions for the owner (with recommendations)

1. **Who may drive a registered device?** Any hub admin, with the target
   box's own wheel refusals as the safety boundary (recommended — simplest and
   matches "per-machine admin"), or require the hub admin to also be `wheel`
   on the target box (stricter, adds the same multi-box identity bookkeeping
   we are trying to avoid)? **Recommend: any hub admin; per-box refusals
   already protect admins.**
2. **Is agent-only deployment (no panel UI) on kids' boxes acceptable?**
   Recommended yes — it is how hubs stay hubs and spokes stay slim, and it
   means a box can be *managed* without ever having its own panel enabled.
   Confirms the separate agent unit design.
3. **Control-channel default: loopback + SSH tunnel (Phase 1) — confirm?** Or
   is LAN-TLS needed immediately for the real household topology? **Recommend
   loopback+tunnel first** (consistent with today's localhost-first stance),
   LAN-TLS in Phase 2.
4. **Priority of settings federation:** is the actual pain point "one
   schedule for the same child across boxes" (→ push Phase 3 forward) or "sit
   at one box and moderate others" (→ Phase 1 as scoped)? **Recommend the
   latter first; federation only after live control exists.**
5. **Token lifecycle:** long-lived per-box tokens (recommended — matches
   `auth.json`'s static model, rotation via root CLI) vs expiring tokens with
   out-of-band re-pairing? Long-lived keeps ops simple; revisit if boxes are
   frequently reimaged.
6. **Box identity:** friendly names are needed in `devices.json` (recommend
   yes: name + hostname + registered-at, chosen at `device add`).
7. **Strictly hub-and-spokes, or can any box be both?** Recommend
   hub-and-spokes for Phase 1/2; mesh doubles the client surface for no
   phase-1 gain.
8. **Remote audit:** confirm the hub may *read* box B's audit (yes) and never
   modify/clear it (append-only stays; the hub only displays each box's
   `chain_ok`).
9. **mTLS / cross-box audit anchoring:** desirable hardening later, or
   permanent out-of-scope? Recommend defer both; revisit if the control
   channel ever leaves loopback by default.

---

## 7. Deliverables for the implementer (summary checklist)

- [x] Behavior-preserving refactor: privileged verbs out of `@require_admin`
      views into shared functions (existing 526 tests green after).
      *Implemented as `device_policy.py` (shared refusal/validation) +
      local-safe verb bodies in `agent.py`; see Phase-1 notes below for why
      the app's view bodies were left in place.*
- [x] Agent (`agent.py`, `wsgi_agent.py`, systemd unit, loopback :5001) with
      per-box bearer token auth and the §4.5 API.
- [x] Hub client (`remote.py`, stdlib), `devices.json` store, root-only CLI
      verbs for device/token management.
- [x] Hub routes + device selector UI + per-box audit view with `chain_ok`.
- [x] `install.sh --enable-agent` (stages agent, mints token, renders unit).
- [x] ~85–95 new tests incl. the cross-box wheel-refusal suite; docs.
- [x] Hard rules respected throughout: no touching `tester`/`bluefox`/`stan`,
      no restarting `parental-controls.service`, venv-only Python, no
      checkout-path hardcoding in any artifact (units keep `@APP_DIR@`).

---

## 8. Phase 1 — implementation notes (2026-10-12)

Status: **implemented on `feature/multidevice`.** Single-box installs are
byte-identical: no `devices.json` ⇒ the selector block renders nothing and
`/api/devices` reports `multi_device: false`.

### What shipped

| Piece | File(s) |
|---|---|
| Shared per-box target policy | `device_policy.py` (`valid_username`, `wheel_refusal`, `freeze_refusal`, `budget_config_refusal`) |
| Agent (managed box) | `agent.py`, `wsgi_agent.py`, `systemd/parental-controls-agent.service` |
| Hub client + store + CLI | `remote.py` (`RemoteClient`, `proxy`, `devices.json` 0600, `device add/list/remove`) |
| Hub routes + UI | `app.py` (`/api/devices`, `/api/device/<name>/v1/<path>`), `templates/index.html`, `templates/user.html` |
| Installer | `install.sh --enable-agent` (`--agent-bind`, `--agent-port`, `--agent-insecure-lan`) |
| Tests | `tests/test_multidevice_agent.py`, `tests/test_multidevice_hub.py`, `tests/test_multidevice_install.py`, `tests/test_multidevice_live.py` |

### Agent API surface (all JSON; `Authorization: Bearer <token>`)

`GET /api/v1/health` (no token), `GET /api/v1/users`,
`GET /api/v1/user/<u>/live`, `GET /api/v1/audit?limit=N`,
`GET|POST /api/v1/bedtime`, `POST /api/v1/lock`, `POST /api/v1/lock-screen`,
`POST /api/v1/unlock`, `POST /api/v1/logout`,
`POST /api/v1/bonus`, `POST /api/v1/bonus/revoke`,
`POST /api/v1/process-prefs`, `POST /api/v1/budgets/freeze`,
`POST /api/v1/budgets/unfreeze`. Writes carry `X-Panel-Actor`
(and optional `X-Panel-Id`), recorded on the box as
`remote:<actor>@<panel>` for the trail only.

### Token model

Per-box `secrets.token_urlsafe(32)`; agent stores it at
`/etc/parental-controls/agent_token` (0600, service-user owned), hub stores
it in `devices.json` (0600). Constant-time compare; missing/short/rotated ⇒
401 (503 when unprovisioned). Never logged; `Authorization` is redacted and
no token appears in any audit detail or error payload. No CSRF on the
machine link (bearer header, not a cookie); the hub's own session+CSRF is
unchanged.

### Cross-box refusal

`device_policy` calls the shared `auth.is_wheel_member` /
`budgets.is_admin_protected` (live, local) and is invoked by both `app.py`
and `agent.py`. `app.py`'s `_wheel_target_refused` /
`_refuse_protected_budget_target` / freeze+unfreeze checks now delegate to
it, so the two surfaces cannot drift. Tests pin that the agent refuses to
lock/freeze a wheel member of **its own** box even when the caller claims to
be a wheel admin on the hub.

### Deviations from §5.1 (with reasons)

1. **Privileged verb *bodies* were not physically moved out of `app.py`.**
   The 769-test suite monkeypatches app-module names (`app.run_lock_command`,
   `app.get_lock_status`, `app.load_bedtime`, `app.is_in_bedtime`,
   `app.get_human_users`, `app.get_user_live`, `app._safe_notify`). Moving the
   bodies into a stdlib module would silently break those patch points and
   the behavior-preserving-refactor gate. Instead the *safety-critical*
   decisions were extracted into the shared, Flask-free `device_policy.py`
   (so the agent provably re-runs the same wheel/admin refusals against its
   own box), and the agent re-implements the thin orchestration locally using
   the same shared low-level modules (`auth`, `audit`, `budgets`, `store`)
   and the same scoped sudoers. `tests/test_multidevice_install.py` proves
   every `sudo` argv in `agent.py` is authorized by the existing templates.
2. **Device management CLI lives in `remote.py`, not `cli.py`.**
   `cli.py` was outside this workstream's file ownership; `python3 remote.py
   device add|list|remove` keeps the token-minting/registration path root-only
   and out of any browser surface without touching a concurrently-owned file.
   Folding it into `parental-controls` subcommands is a small Phase-2 follow-up.
3. **Remote user-detail *commands* on `user.html`** ride the same
   `/api/device/<name>/v1/…` proxy via a template fetch shim (no per-call
   rewrites). Server-side remote rendering is implemented for both `/` and
   `/user/<u>`; deeper remote-only UI affordances (device management page,
   LAN-TLS TOFU pin) remain Phase 2.

### Cannot verify without two real boxes (human-run)

- Real SSH-tunnel reachability and the first-token registration handshake.
- That a real `sudo usermod -L` on box B succeeds under box B's rendered
  sudoers when driven by the agent unit (mocked in tests; sudoers coverage is
  asserted statically).
- TLS/TOFU over LAN (Phase 2) and SQLite multi-writer contention with the
  agent as a fourth writer on a live box.
`tests/test_multidevice_live.py` is env-gated (`MULTIDEVICE_LIVE_URL`,
`MULTIDEVICE_LIVE_TOKEN`) and skipped by default; it is read-only.
