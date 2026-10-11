# TLS for LAN-exposed panel — Implementation Plan (Phase 3)

Status: **plan only, not implemented.** Research and design for a later
implementing agent. No code, test, template, installer, or unit file is
changed by this document.

Branch: `feature/tls` @ `df5d382`. Worktree: `tls/`.

## 0. Key verified fact: waitress cannot do TLS

Checked read-only from the worktree venv (`tls/.venv` → `master/.venv`):

- Installed version: **waitress 3.0.2** (`.venv/bin/pip show waitress`).
- `waitress.serve(app, **kw)` takes only generic `Adjustments` keys. `grep`
  over `waitress/adjustments.py` and `waitress/runner.py` shows listener
  options `host` / `port` / `listen` only — **no `ssl` / `tls` / `cert` /
  `key` / `ssl_context` argument anywhere**. The only "https" strings in the
  package are `Forwarded`-header parsing in `proxy_headers.py` (reverse-proxy
  support, not termination).
- Conclusion: **any TLS design keeps waitress or replaces/swaps the server
  for the LAN path; there is no flag to switch on.** This single fact drives
  §2.

System facts (dev VM, read-only): `/usr/bin/openssl`, OpenSSL 3.5.9; stdlib
`ssl.OPENSSL_VERSION` identical. `openssl` CLI availability on a target box
must still be probed at install time (fail closed if `--tls` needs
generation and no `openssl` exists).

## 1. Current-state inventory (every bind/TLS-relevant line)

### App / WSGI

- `app.py:33-36` — `SECURE_COOKIES = (PARENTAL_TLS == '1')`. Secure-cookie
  support is **already wired, opt-in**; nothing sets `PARENTAL_TLS=1` today.
- `app.py:846-850` — `_auth_cookie_response()` always sets `HttpOnly` +
  `SameSite=Lax`, `secure=` follows `SECURE_COOKIES`.
- `app.py:1840-1847` — dev-server `argparse`: `--allow-lan` (bind `0.0.0.0`,
  else `127.0.0.1`), `--port` (default 5000).
- `app.py:1854-1857` — `--allow-lan` prints a cleartext-cookies WARNING.
- `wsgi.py:21-23` — `__main__` serves waitress on `127.0.0.1:5000` (dev
  fallback; production uses the unit file, not this).

### Dev entrypoint

- `run.sh:3-4` — header comment: localhost default, `--allow-lan` binds
  `0.0.0.0` with cookies in the clear.
- `run.sh:19-23` — `HOST` selection + WARNING echo on `--allow-lan`.
- `run.sh:27` — `exec waitress-serve --host="$HOST" --port=5000
  wsgi:application`.

### Installer (`install.sh` — the single deploy path)

- `install.sh:5-10` — header: localhost default, `--bind/--port` render into
  the unit, `--open-firewall` opens the port.
- `install.sh:26-28` — defaults `BIND_HOST=127.0.0.1`, `BIND_PORT=5000`.
- `install.sh:31-41` — flag parsing: `--install-dir/--app-dir`, `--user`,
  `--bind`, `--port`, `--open-firewall`, `--dry-run`.
- `install.sh:57-64` — `--port` numeric validation, `--bind` charset
  validation.
- `install.sh:65-69` — non-loopback `--bind` prints the cleartext-cookies
  WARNING (warn-and-continue today).
- `install.sh:133-155` — renders all units via `sed` (`@APP_DIR@`,
  `@SERVICE_USER@`, `@BIND_HOST@`, `@BIND_PORT@`; lines 144-145); restarts
  the service only when a rendered unit changed.
- `install.sh:206-221` — firewall is **opt-in** (`--open-firewall` only).
- `install.sh:259-261` — verify probe maps `0.0.0.0`→`127.0.0.1`, then
  `curl http://.../` (plain HTTP, line 261).
- `install.sh:265-270` — non-302 probe on a LAN bind hints at
  `--open-firewall`.
- `install.sh:291-298` — summary: loopback binds get SSH-tunnel instructions
  (line 295), LAN binds get `http://` instructions (line 297).

### Systemd units

- `systemd/parental-controls.service:9` — `ExecStart=... waitress-serve
  --host=@BIND_HOST@ --port=@BIND_PORT@ wsgi:application`.
- `systemd/parental-controls.service:15` — `Environment=PARENTAL_CONTROLS_AUTH=...`
  (note: **no `PARENTAL_TLS` line exists today** — the TLS unit variant adds it).
- `systemd/parental-controls-healthcheck.service:9` — `ExecStart=/bin/bash
  @APP_DIR@/healthcheck.sh --host @BIND_HOST@ --port @BIND_PORT@` (no TLS
  notion today).

### Healthcheck (`healthcheck.sh`)

- `healthcheck.sh:31-36` — flags `--unit/--host/--port/--auth-store/--timeout`.
- `healthcheck.sh:108-115` — `0.0.0.0`→`127.0.0.1` probe mapping; `url`
  hardcodes the `http://` scheme (line 115).
- `healthcheck.sh:119-134` — `curl --max-time` probe with per-exit-code
  failure taxonomy.
- `healthcheck.sh:136-146` — healthy = 200 or 302-to-`/login`.

### Docs

- `AUTH_DESIGN.md:6` (decision 6) — "localhost + SSH tunnel for now. No LAN
  binding, no TLS in MVP." (LAN bind has since been added as opt-in; the
  no-TLS part is this plan's subject.)
- `AUTH_DESIGN.md:63-66` — localhost bind is "load-bearing for auth safety".
- `AUTH_DESIGN.md:116` — cookie flags: `Secure` "when TLS exists (Phase 3)".
- `AUTH_DESIGN.md:171` — build step 5: `127.0.0.1` default + `--allow-lan`.
- `PLAN.md:17-18` — arch: waitress, `127.0.0.1:5000` default, LAN opt-in.
- `PLAN.md:61-62` — MVP bind item (done).
- `PLAN.md:74-75` — **Phase-3 item: "TLS when LAN-exposed (session cookies
  otherwise travel in the clear)"** — this plan.
- `PLAN.md:160-161` — status: waitress on `127.0.0.1`, `--allow-lan`/`--bind`
  opt-in.
- `README.md:93-94` — `./run.sh --allow-lan` binds `0.0.0.0`.
- `README.md:99-100` — manual `waitress-serve` / `app.py [--allow-lan]`.
- `README.md:103` — default listens on `127.0.0.1:5000`.
- `README.md:110-116` — Security Note: LAN opt-in exists, cookies in clear
  until post-MVP TLS.
- `README.md:143-148` — installer flags incl. `--bind/--port/--open-firewall`.
- `README.md:167-172` — `--bind 0.0.0.0` listens everywhere; non-localhost
  bind warns, prefers SSH tunnel.
- `README.md:179-181` — first-login SSH-tunnel instructions.
- `RELEASE.md:30` — served by waitress on `127.0.0.1:5000`.
- `RELEASE.md:54-59` — deploy + `curl http://127.0.0.1:5000/` smoke test
  (expect 302).
- `RELEASE.md:68-69` — installer flags list.
- `RELEASE.md:91-93` — SSH-tunnel first login.
- `RELEASE.md:111-112` — known limitation: "No TLS … session cookies travel
  in the clear" on LAN bind.
- `RELEASE.md:218-227` — rollback story (checkout old commit, re-run
  `install.sh`, smoke-test 302).
- `requirements.txt:3` — `waitress>=2.0` (resolved 3.0.2 in the venv).
- `sudo-audit.md:120` — references waitress on `127.0.0.1:5000` (context only).

### Templates

- `templates/` has 4 files, **no shared base layout**: `index.html`,
  `login.html`, `user.html`, `change_password.html`. There is **no warning
  banner anywhere today** — an insecure-LAN banner must be added per-template
  or as a new shared partial (implementer decision, see §4 test 8).

### Existing tests covering this area

- `tests/test_deployment.py:737-791` — `TestBindOptions` (safe defaults,
  placeholder rendering, firewall opt-in, validation, restart-on-change,
  LAN firewall hint).
- `tests/test_health.py:170-278` — `TestUnitsRender`, `TestNoHardcodedPaths`,
  `TestInstallerWiring` (bind placeholders substituted for all units, timer
  enabled, verify step).
- `tests/test_health.py:329-...` — `TestProbeIsSafe`, `TestProbeReportsHealthy`.

## 2. Recommended approach

**Self-signed certificate generated at install time (via the `openssl` CLI,
no new system package) + a minimal TLS-capable WSGI server used ONLY for
LAN binds + `PARENTAL_TLS=1` in the rendered unit (Secure cookies via the
already-wired `app.py:36`). Localhost serving stays waitress, byte-identical.
Plain-HTTP LAN binds are refused by the installer unless the parent passes
an explicit `--insecure-lan` flag.**

### Why this wins (and the others lose)

- **(a) Reverse proxy (Caddy/nginx): rejected.** Strongest TLS practice in
  the abstract, but it adds a new system service, its own config language
  and update cadence, and a second thing that can break — for a single-port
  home panel. Fails "no new system dependencies without strong justification";
  the justification is not strong here.
- **(b) stunnel/socat-style TLS wrapper: rejected.** Same system-dependency
  objection as (a) with less benefit (no HTTP awareness at all), plus a
  second supervised process and unit to manage.
- **(c) Self-signed + TLS-capable server: RECOMMENDED.** Zero new *system*
  packages: cert via `openssl` CLI (present on the box, probed at install;
  §5 covers absence), serving via one new PyPI package (see server choice
  below — same `pip install -r requirements.txt` step that already needs the
  network). Matches the home-LAN reality: no public DNS name exists, so
  private trust (self-signed + printed fingerprint, TOFU) is the honest
  model, not fake domain validation.
- **(d) Refuse-LAN / SSH-tunnel-only + banner: adopted as the guardrail, not
  the solution.** The plain-HTTP LAN path must stop being one warning away
  from credential theft (§3), but removing the LAN opt-in entirely would
  delete a shipped feature some parents may already use. So (d)'s mechanism
  (refuse/confirm + banner) becomes the insecure-mode policy *around* the
  TLS default.
- **(e) certbot/ACME: rejected.** Needs a public domain, port-80
  reachability, and renewal plumbing — wrong for a home LAN box behind NAT
  that is addressed by IP. Document this rejection so it is not re-litigated.

### Server choice for the LAN path: cheroot (primary), gunicorn (fallback)

Waitress cannot terminate (§0), so the LAN path needs a server that can. Two
PyPI-only candidates need no system packages:

- **Primary: `cheroot`** (the CherryPy WSGI server, pure-Python threaded like
  waitress, stdlib-`ssl`-based `BuiltinSSLAdapter(cert, key)`). Smallest
  behavioral delta from waitress; no new process model to reason about.
- **Fallback: `gunicorn --certfile/--keyfile`.** More widely known, but a
  pre-fork worker model — heavier and a bigger operational change for this box.

The implementer spikes both against the Flask test client + one live
`curl -k https://` check and picks by these criteria (in order): (1) Secure
cookie + login + lock/unlock round-trip works over TLS; (2) localhost
waitress path untouched; (3) smallest `requirements.txt` delta that still
gets security updates. **If the spike contradicts this ranking, the spike
wins — record the reason in the merge.** A hand-rolled stdlib proxy
(`http.server` + `ssl.SSLContext` forwarding to waitress-on-loopback) was
considered and rejected: it adds custom security-sensitive HTTP parsing
(request-smuggling surface) to avoid one pinned PyPI dependency — a bad trade.

### Cert provisioning (exact)

- Location: `/etc/parental-controls/tls/server.crt` + `server.key`, beside
  the auth store (dir already `0700`, service-user-owned via `install.sh`
  step 5/8). Both files mode `0600`, owned by the service user (same trust
  model as `auth.json`). New files are runtime state: gitignored-equivalent
  (they live outside the repo), never synced, never overwritten on re-run.
- Generation (only when `--tls` and no cert exists and no `--tls-cert/key`
  given): `openssl req -x509 -newkey rsa:2048 -nodes -days 3650
  -subj "/CN=<tls-name>" -addext "subjectAltName=..."`. SAN always includes
  `DNS:localhost,IP:127.0.0.1,IP:::1` plus the LAN identity (see `--tls-name`).
- `--tls-name NAME`: DNS name or IP baked into SAN/CN. Default: first
  non-loopback IPv4 from `hostname -I` plus `hostname -f` short name; if
  detection fails, installer exits 2 telling the parent to pass `--tls-name`
  explicitly. (`--bind 0.0.0.0` is a wildcard, never a cert identity — this
  flag is why generation needs it.)
- Owner-supplied override: `--tls-cert PATH --tls-key PATH` (must both be
  given; installer validates readable + `openssl x509/x509 -noout` parseable
  + key matches cert via modulus compare, else exit 2; copies into the
  store dir with `0600`). Staged-from-checkout is forbidden — absolute owner
  paths only, and they must never be committed.
- Rotation: existing cert/key are **never regenerated on re-run** (a fresh
  fingerprint would break every parent's stored TOFU trust). `--regen-tls-cert`
  forces regeneration (backs up old pair with a timestamp suffix).
- Installer prints: SHA-256 fingerprint (`openssl x509 -fingerprint -sha256
  -noout`), SAN list, expiry date, and the `https://` URL. The parent
  verifies the fingerprint at the browser's first self-signed warning (TOFU).

### Server wiring (exact)

- New entrypoint `tls_serve.py` (~30 lines, stdlib + chosen server only):
  parses `--host/--port/--cert/--key`, wraps the imported `wsgi:application`.
  No app logic; unit-testable import (bind is behind `main()`).
- `systemd/parental-controls.service` template keeps ONE `ExecStart` line
  shape; the installer renders one of two variants:
  - loopback (unchanged, byte-identical to today): `... exec waitress-serve
    --host=@BIND_HOST@ --port=@BIND_PORT@ wsgi:application` with NO
    `PARENTAL_TLS` line;
  - LAN+TLS: `... exec python @APP_DIR@/tls_serve.py --host=@BIND_HOST@
    --port=@BIND_PORT@ --cert=@TLS_CERT@ --key=@TLS_KEY@ wsgi:application`
    plus `Environment=PARENTAL_TLS=1` (new template line, rendered only in
    this variant; loopback rendering must not contain it).
  - New placeholders `@TLS_CERT@/@TLS_KEY@` default to the store paths above;
    rendered values are absolute install-dir-independent paths (no checkout
    paths — extends the existing `TestNoHardcodedPaths` rule).
- Scheme/port: **same `--port`, `https://` scheme** (default stays 5000).
  No separate redirect listener on port 80/5000-plain: the TLS port is the
  only listener, minimizing surface. The installer summary prints
  `https://<name>:<port>` for TLS binds.
- Healthcheck unit gains a `--tls` passthrough:
  `healthcheck.sh --host @BIND_HOST@ --port @BIND_PORT@ [--tls]`; with
  `--tls`, probe is `curl -k https://...` (self-signed requires `-k`; the
  healthcheck authenticates nothing and sends no credentials, so `-k` is
  safe *here* — document that `-k` must never appear in any other project
  script). Auth-store check unchanged.

### LAN UX (what the parent sees/does)

1. `sudo ./install.sh --bind 0.0.0.0 --tls --open-firewall` (or a LAN IP).
2. Installer prints fingerprint + expiry + `https://<name>:5000`.
3. Parent opens the URL, browser shows the expected self-signed warning,
   parent compares the fingerprint, proceeds (stored exception).
4. Login works; session cookie carries `Secure` (verify in devtools once).
5. Renewal/expiry warnings come from the verify step + healthcheck (§5).

### Localhost behavior (must be byte-identical to today)

- Loopback `--bind` values (`127.0.0.1`, `localhost`, `::1` — the exact set
  `install.sh:65-69` already tests): no cert generated/read, no new env
  vars, waitress `ExecStart` rendered exactly as today, `http://` probe,
  SSH-tunnel summary text unchanged. The regression test (§4, test 6)
  renders the loopback unit and diffs it against the checked-in template
  rendering — any byte difference fails.

## 3. Insecure-mode guardrails

After this ships, a plain-HTTP LAN bind requires deliberate consent:

- Installer: non-loopback `--bind` without `--tls` **exits 2** with: "refusing
  plain-HTTP LAN bind (session cookies travel in the clear); re-run with
  `--tls` (recommended) or `--insecure-lan`." `--insecure-lan` restores
  today's behavior (warning + `http://` summary) and additionally renders a
  persistent in-UI banner (below).
- `run.sh --allow-lan` / `app.py --allow-lan` (dev only): keep plain HTTP
  but strengthen the WARNING to name the fix ("dev-only; production LAN
  requires `install.sh --tls`"). No cert machinery in dev; documented as
  dev-only in README.
- Banner: when the service was started with `--insecure-lan` (implementer:
  e.g. `Environment=PARENTAL_INSECURE_LAN=1` rendered only in that variant,
  read into `app.config`, passed to all 4 templates or a new shared partial),
  every page shows: "Insecure LAN mode — logins can be read off the network.
  Re-run install with --tls." Login page shows it *above* the form (the
  credential goes in there). No banner in loopback or TLS mode (tested).
- Healthcheck/installer verify matrix:
  - loopback → `curl http://` (unchanged);
  - LAN+TLS → `curl -k https://` + print cert expiry (`openssl x509
    -enddate -noout`), warn when < 30 days;
  - LAN insecure → `curl http://` + WARNING line naming `--tls`.
- Firewall stays opt-in: `--tls` must NOT imply `--open-firewall` (a parent
  who only wants TLS-over-tunnel gets no surprise exposure). The verify hint
  (`install.sh:265-270`) is extended to mention scheme mismatch ("probed
  https; if you opened http://… in the browser, that is why").

## 4. Test list (stdlib `unittest` only, ~14 tests)

New file `tests/test_tls.py` (plus 1–2 additions to `test_deployment.py` /
`test_health.py` where the existing classes live). No live users, no ports
below 1024, no firewall changes; any socket test binds `127.0.0.1` on an
ephemeral port and closes in teardown. Nothing here touches
`tester`/`bluefox`/`stan`/`pcagent`.

1. `test_waitress_has_no_native_tls` — pins §0: `waitress.serve` /
   `Adjustments` accept no `ssl/cert/key/ssl_context` argument (fails if a
   future waitress gains TLS, forcing a re-evaluation of §2).
2. `test_installer_refuses_plain_lan_bind` — non-loopback `--bind` without
   `--tls`/`--insecure-lan` exits 2 with the "refusing plain-HTTP" message
   (shell-parse the installer arg block or extract the guard into a
   sourced/testable function — implementer picks, test asserts behavior).
3. `test_localhost_needs_no_tls_flags` — all three loopback binds install
   with zero TLS flags (regression: default path untouched).
4. `test_self_signed_cert_parses` — generate with the same `openssl` argv
   the installer uses (skip if no `openssl`): `openssl x509 -noout`
   parses; SAN contains localhost/127.0.0.1 + the given `--tls-name`;
   RSA ≥ 2048; expiry ≈ 10 y (± tolerance).
5. `test_cert_key_permissions_and_ownership` — generated pair is `0600`,
   parent dir `0700`; re-run preserves bytes (no silent regeneration).
6. `test_loopback_unit_is_byte_identical` — render loopback unit, diff
   against today's rendering (waitress `ExecStart`, no `PARENTAL_TLS` line).
7. `test_tls_unit_renders_cert_paths_and_env` — LAN+TLS rendering contains
   `tls_serve.py`, `--cert/--key` with the store paths, and exactly one
   `Environment=PARENTAL_TLS=1`; contains no `@PLACEHOLDER@` and no checkout
   path.
8. `test_secure_cookie_iff_tls_env` — Flask test client: `PARENTAL_TLS=1`
   → `Set-Cookie: ...; Secure`; unset → no `Secure` attribute (extends the
   existing `app.py:36/849` wiring; login POST through the real stack).
9. `test_insecure_banner_only_in_insecure_lan` — test client GET `/login`
   (and `/` when logged in): banner present iff insecure-LAN config set;
   absent in loopback and TLS modes.
10. `test_owner_cert_validation` — missing/unreadable/unparseable
    `--tls-cert/key` and mismatched pair each fail install validation
    (exit 2); matching pair renders verbatim into the unit.
11. `test_healthcheck_tls_flag_uses_https_insecure` — `healthcheck.sh --tls`
    builds an `https://` URL with `-k`; without it, `http://` (arg-parsing
    test + dry-run URL echo; no daemon needed).
12. `test_verify_reports_expiry` — installer verify (or helper) prints cert
    enddate + `<30d` warning path, using a fixture cert with a short life
    (generated at test time, temp dir only).
13. `test_tls_does_not_open_firewall` — `--tls` without `--open-firewall`
    emits no `firewall-cmd` invocation (dry-run capture).
14. `test_tls_over_loopback_roundtrip` — integration: `tls_serve` (or the
    chosen server adapter) on `127.0.0.1:0` with a throwaway cert —
    `ssl`-context client GET `/` returns the 302-to-`/login` through real
    TLS; same assertion over plain HTTP fails/refuses. (Skipped when the
    server dep is absent; uses only stdlib client side.)

## 5. Rollout / rollback

### Deploy (implementer ships, owner runs)

1. Review + merge `feature/tls` to `master`; deploy from the checkout:
   `sudo ./install.sh --bind 0.0.0.0 --tls --tls-name <lan-ip-or-name>
   --open-firewall` (omit `--open-firewall` for TLS-over-tunnel only).
2. Confirm: fingerprint printed; `curl -k -s -o /dev/null -w "%{http_code}
   https://127.0.0.1:5000/"` → 302; browser shows Secure cookie; healthcheck
   timer active; expiry date noted (calendar reminder at 11 months before,
   plus the 30-day installer/healthcheck warning).
3. Loopback-only parents do nothing: re-running plain `sudo ./install.sh`
   keeps the byte-identical localhost unit and never touches TLS.

### Revert to plain HTTP (per-bind, no state loss)

- Back to localhost: `sudo ./install.sh` (defaults) — renders the waitress
  unit, drops `PARENTAL_TLS=1`, probe returns to `http://`. Cert files stay
  inert under `/etc/parental-controls/tls/` (ignored, not deleted).
- Back to insecure LAN (not recommended): add `--insecure-lan`; banner
  appears; cookies in clear again — operator's explicit choice.
- Full code rollback: `RELEASE.md:218-227` procedure unchanged (checkout old
  commit, re-run installer, expect 302). Old builds ignore the `tls/`
  directory files and the extra unit env line is dropped at render time, so
  downgrade is inert. State files (`bedtime.json`, `budgets.json`,
  `process_prefs.json`, `auth.json`) are untouched by every TLS step —
  backup is still "copy those four files".

### Cert expiry (must never lock the parent out)

- Blast radius of an expired cert: browsers show a hard-looking error on the
  LAN URL, but (a) `Advanced → Proceed` still works for self-signed-expired
  in the browsers that matter here, (b) `curl -k` and the SSH tunnel keep
  working, (c) `http://127.0.0.1:5000` is unaffected only if the unit still
  binds loopback — note: a `0.0.0.0`+TLS unit serves loopback over TLS too,
  so document the `https://127.0.0.1:5000` tunnel URL for that case, and
  (d) the root recovery CLI (`cli.py`) never touches TLS at all.
- Detection: installer verify prints days-to-expiry every run; healthcheck
  emits a `WARNING: TLS cert expires in Nd days` journal line when < 30 d
  (**warning-only, exit 0** — a coming expiry must not page as a failure and
  must not restart anything).
- Recovery: `sudo ./install.sh --regen-tls-cert` (same bind flags; restarts
  service, prints the NEW fingerprint — parents must re-verify TOFU once),
  or rotate to owner-supplied certs via `--tls-cert/--tls-key`. Missing
  `openssl` at regen time fails closed with "install openssl or supply
  --tls-cert/--tls-key", leaving the running service alone.

## 6. Open questions for the owner (recommendation each)

1. **TLS server: cheroot (recommended) vs gunicorn vs stdlib-only proxy?**
   Recommend cheroot — pure-Python threaded server closest to waitress, one
   pinned PyPI line, stdlib-ssl adapter. The spike (§2 criteria) confirms or
   overturns; either way no system package.
2. **Refuse plain-HTTP LAN by default (recommended) vs warn-and-continue?**
   Recommend refuse-with-`--insecure-lan`-escape-hatch: fail-closed matches
   this project's auth/store ethos, and the banner makes the escape hatch
   honest. Warn-and-continue preserves today's one-warning-away footgun.
3. **Self-signed default + owner-supplied override (recommended) vs
   owner-supplied-only?** Recommend the default: zero-friction TLS for
   parents with no DNS; TOFU fingerprint keeps it honest. Owner-only would
   leave most LAN users on plain HTTP forever.
4. **Cert lifetime 10 years (recommended) vs 825 days?** Recommend 10 y:
   a home appliance gets no automated rotation discipline, and expiry here
   degrades to scary-but-recoverable (§5), not to silent insecurity.
   CA/Browser 825-day norms do not bind private trust. The 30-day warning
   covers diligence.
5. **Same `--port` with `https://` (recommended) vs a separate TLS port
   (e.g. 5443)?** Recommend same port: least config churn, firewall rule
   unchanged, one URL to document. Cost: the scheme flip must be
   communicated once (installer summary + README).
6. **No plain-HTTP→HTTPS redirect listener (recommended) vs redirect?**
   Recommend none: a second listener doubles the bind surface to save one
   "why did http:// stop working" confusion, which the installer summary
   already answers. Revisit if parents trip on it in practice.
7. **Dev `run.sh --allow-lan` stays plain-HTTP-with-louder-warning
   (recommended) vs dev certs?** Recommend as-is: dev traffic stays on
   loopback/LAN-test only, and cert machinery in dev invites real
   fingerprints to be minted carelessly. Document "dev-only".
8. **Keep localhost always-plain (recommended) vs HTTPS-everywhere
   including loopback?** Recommend always-plain loopback: byte-identical
   default, SSH-tunnel story unchanged, zero migration risk. HTTPS on
   loopback buys nothing (loopback is already out of the network
   attacker model this project assumes).
