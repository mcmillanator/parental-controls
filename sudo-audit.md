# Sudo audit — is the `stan-nopasswd` blanket safe to remove?

**Scope.** Every privileged command `app.py` and `budgets.py` can issue
through `sudo`, checked against `sudoers-parental-controls` and
`sudoers-parental-controls-budgets` rendered with `@SERVICE_USER@=stan`,
using sudo's real matching rules. Analysis and tests only — nothing under
`/etc` was modified and no account was touched.

> **Service-user migration note.** This audit was performed when the panel
> ran as `stan`. The installer now deploys it as the dedicated system user
> `parental-controls` (nologin, homeless, locked password, no sudo of its
> own), rendering the same templates with `@SERVICE_USER@=parental-controls`.
> Matching semantics are user-independent, so the coverage tables below hold
> unchanged; read every "`stan`" below as "the service account". The
> `stan-nopasswd` blanket itself is untouched — it remains the dev VM's
> human/agent ops rule only.

**Headline:** the scoped rules are **complete** — every command the code
runs is authorized, so the blanket is not needed for the app to function.
But the scoped rules are **not yet contained**: the trailing `*` in the
`usermod`, `loginctl` and cgroup rules still authorizes argument injection
that reaches full root. Removing the blanket is safe *functionally*; it is
a meaningful security improvement only after argument-regex containment of
those trailing wildcards lands.

> **Status note (2026-10-11, docs refresh).** The containment work described
> above (hardening the trailing-wildcard grants to anchored argument regexes)
> exists and is tracked as its own workstream; its landing details — the final
> rule text and its verification — will be recorded in this file by that work.
> The coverage tables in this audit describe the grant set the code needs, not
> the final rule form.

---

## 1. Every privileged command, with its coverage

Matching semantics used (all confirmed against real sudo, see §4):
a bare command name is resolved through `Defaults secure_path`, and the
**resolved path string** is what the rule is compared against; arguments
are matched as **one space-joined string**; sudoers(5) states a `/` is
**not** matched by wildcards in the *path* portion of a command but **is**
matched in the *arguments*.

| # | file:line | exact argv (dynamic slots) | granted by | covered |
|---|---|---|---|---|
| 1 | `app.py:81` | `passwd -S <user>` | `sudoers-parental-controls` (`passwd -S *`) | ✅ |
| 2 | `app.py:142` | `loginctl show-user <user> -p Sessions --value` | `…` (`loginctl show-user *`) | ✅ |
| 3 | `app.py:154` | `loginctl lock-session <id> [<id> …]` | `…` (`loginctl lock-session *`) | ✅ (see note A) |
| 4 | `app.py:175` | `loginctl show-session <id> -p Class -p Type` | `…` (`loginctl show-session *`) | ✅ |
| 5 | `app.py:215` | `usermod -L <user>` | `…` (`usermod -L *`) | ✅ |
| 6 | `app.py:235` | `usermod -U <user>` | `…` (`usermod -U *`) | ✅ |
| 7 | `app.py:625` | `systemd-run --machine=<user>@.host --user notify-send -u critical <title> <body>` | `…` (`systemd-run --machine=*@.host --user notify-send -u critical * *`) | ✅ |
| 8 | `app.py:1013` | `usermod -L <user>` (`/api/lock`) | `…` | ✅ |
| 9 | `app.py:1055` | `usermod -L <user>` (`/api/lock-screen`) | `…` | ✅ |
| 10 | `app.py:1339` | `usermod -L <user>` (`/api/logout`) | `…` | ✅ |
| 11 | `app.py:1354` | `loginctl terminate-user <user>` | `…` (`loginctl terminate-user *`) | ✅ |
| 12 | `budgets.py:911` | `cat /sys/fs/cgroup/parental-<user>-<cat>/cgroup.freeze` | `sudoers-parental-controls-budgets` | ✅ |
| 13 | `budgets.py:911` | `cat …/cgroup.procs` | `…` (`cat …/cgroup.procs`) | ✅ |
| 14 | `budgets.py:925` | `tee …/cgroup.freeze` | `…` (`tee …/cgroup.freeze`) | ✅ |
| 15 | `budgets.py:925` | `tee …/cgroup.procs` | `…` (`tee …/cgroup.procs`) | ✅ |
| 16 | `budgets.py:942` | `mkdir -p /sys/fs/cgroup/parental-<user>-<cat>` | `…` (`mkdir -p /sys/fs/cgroup/parental-*`) | ✅ |

**16/16 covered. No gaps.**

Both `/usr/sbin/X` and `/usr/bin/X` are listed in each template and both
exist on this box (they are hardlinks to the same inodes), so whichever one
`secure_path` resolves is granted. `secure_path` puts `/usr/sbin` first, so
the effective binary is the `sbin` path in each case.

`sudo`'s own `-n` (rows 12–16) is consumed by sudo and is never part of
the command sudoers matches.

**Dynamic-argv cases, resolved:**
- `<user>` — `app.valid_username()` and `budgets._USERNAME_RE` both admit
  only `[A-Za-z0-9_.-]+` and reject a leading `-`; rows 1–11 are pinned to
  a single operand that the `*` swallows. The cgroup paths in rows 12–16
  are built by `budgets.cgroup_path()`, which re-validates both the
  username and the category (`CATEGORIES` is a fixed 5-tuple).
- `<id>` (row 3) — `lock-session` is called as
  `['sudo','loginctl','lock-session'] + lockable`, i.e. **N session ids**.
  Arguments are joined with single spaces and `*` matches whitespace, so
  `lock-session c2 c3` matches `lock-session *`. Verified ALLOW for 1, 2
  and 4 ids.

**Note A — one degenerate form.** The bare argv `loginctl lock-session`
with **zero** session ids is *not* authorized (`lock-session *` needs at
least one operand). The code cannot produce it: `lock_user_sessions()`
returns early via `if not lockable: return 'no lockable (graphical)
session'`. `test_lock_session_is_only_run_with_at_least_one_session_id`
asserts that guard exists so the exemption cannot silently rot.

---

## 2. Gaps — what the scoped rules still allow (the important part)

Every row below was verified **ALLOW** against real sudo policy via the
`pcverify` account (§4). These are *rule* gaps: the app never builds these
argv, but anything that can run `sudo` as the service account — including a
compromised web app — can.

| command | why it is authorized | impact |
|---|---|---|
| `usermod -U -p <hash> root` | `-U *` swallows extra flags | **root.** set root's password, then log in |
| `usermod -L -p <hash> <user>` | `-L *` | set any password, even on a locked account |
| `usermod -U -o -u 0 <user>` | `-U *` | duplicate-UID-0 account |
| `usermod -U -G wheel <user>` | `-U *` | grant the sudo group → root via `sudo -i` |
| `usermod -L root` / `-U root` | `-L/-U *` | lock/unlock **root** |
| `loginctl terminate-user --all` | `terminate-user *` | kill every session on the box |
| `loginctl terminate-user <admin>` | `terminate-user *` | log the admin out |
| `tee /sys/fs/cgroup/parental-x/../system.slice/sshd.service/cgroup.procs` | `*` matches `/` in arguments | migrate arbitrary PIDs into arbitrary cgroups; freeze arbitrary units |
| `mkdir -p /sys/fs/cgroup/parental-x/../../etc/…` | `*` matches `/` in arguments | `mkdir` outside `/sys/fs/cgroup` |
| `passwd -S -d root` | `-S *` | `-S` does **not** pin the rest of the argv; `-d` deletes root's password |
| `systemd-run --machine=root@.host --user notify-send -u critical <a> <b>` | `--machine=*@.host` | target any user's session, not just the intended child |

The last row of the negative set is confirmed closed: the notify grant
does **not** authorize a different payload (`--user /bin/sh` → DENY),
because `notify-send` is a literal in the rule, not a wildcard. That was
the `shell escapes` risk the template comment cites, and it holds.

**Net:** today `stan-nopasswd` (`NOPASSWD: ALL`) is what makes this
urgent, and it is strictly worse than every entry above. Removing it is
still the right move — it deletes `sudo <anything>` outright — but it will
not by itself contain a compromised service account.

---

## 3. Why the blanket is dangerous *here*

`/etc/sudoers.d/stan-nopasswd` contains exactly one line:

```
stan ALL=(ALL) NOPASSWD: ALL
```

`parental-controls` is the dedicated account the web service runs as
(`User=@SERVICE_USER@` in `systemd/parental-controls.service`,
`waitress-serve` on `127.0.0.1:5000`; previously the shared `stan` account,
see the migration note in Scope).
The panel is a Flask app that takes a **username from an HTTP form field**
and passes it to `subprocess.run(['sudo', 'usermod', '-L', username])`.
The app's own validators are the only thing between that form field and
root. So:

- The blanket means **any** RCE, auth bypass, or template-injection bug in
  the panel is an instant root shell (`sudo bash`), and it is also a root
  shell for *every other process running as stan* — including the user's
  own desktop session, which is why this rule was scoped down originally.
- It silently masks missing grants. `sudoers-parental-controls` §header
  already records this happening: the installed files once shipped a
  literal `@SERVICE_USER@`, and `systemd-run` was granted nothing at all,
  so **every pre-lock notification failed** — invisibly, because the
  blanket answered anyway. That is the strongest argument for removal:
  while it exists, `sudo -l` and the test suite cannot tell you the truth.
- Note `stan` is also in `wheel`, so `%wheel ALL=(ALL) ALL` still grants
  password-prompted root. The blanket is what makes it *passwordless*;
  after removal, `sudo` from the service falls back to the scoped rules
  (NOPASSWD) and an interactive `sudo` would ask for a password. That is
  the intended end state.

---

## 4. How these results were verified

Static analysis plus a **policy oracle**, not guesses:

1. `tests/test_deployment.py` implements sudoers(5) matching directly
   (`_glob_to_regex`, `_command_matches`, `_args_match`), including the
   FNM_PATHNAME rule for paths and the wildcard-spans-slash rule for
   arguments.
2. Every argv is re-derived from the source with `ast`, so the list cannot
   go stale.
3. Cross-checked against **real sudo 1.9.17p2** using the dedicated
   least-privilege account `pcverify`, which carries the same scoped rules
   and **no blanket**. `sudo -l -- CMD` answers "may run?" without ever
   executing the command:

   ```bash
   probe() {
     out=$(sudo -n -u pcverify sudo -n -l -- "$@" 2>&1); rc=$?
     [ "$rc" -eq 0 ] && [ -n "$out" ] && echo ALLOW || echo DENY
   }
   ```

   The static matcher and the oracle agreed on all 38 probes, including
   every gap in §2 and every denial in §1's notes. A nonsense binary is
   DENY, so the oracle discriminates properly.

Mutation-tested: deleting any single rule line (10 different rules tried)
fails the suite; injecting an ungranted command such as
`['sudo', 'chpasswd']` fails with
`app.py:1013 runs 'sudo chpasswd' which no sudoers template authorizes`.

---

## 5. Removal procedure

> Run these yourself. Nothing below has been executed — no file under
> `/etc` was touched and no account was modified during this audit.

### Step 0 — preflight (read-only)

```bash
sudo visudo -c                                  # current tree must parse
sudo -l -U stan                                # see today's grants
sudo cat /etc/sudoers.d/parental-controls      # confirm no @SERVICE_USER@
sudo cat /etc/sudoers.d/parental-controls-budgets
```

If either scoped file still contains a literal `@SERVICE_USER@`, the
scoped rules grant nothing to `stan` and **removing the blanket will break
enforcement**. Fix that first (`sudo ./install.sh` re-renders and
`visudo -c`s both files; `sudo systemctl restart parental-controls.service`
is part of the installer and is needed to pick up the change).

### Step 1 — back up

```bash
sudo cp -a /etc/sudoers.d/stan-nopasswd /root/stan-nopasswd.bak
sudo cp -a /etc/sudoers.d/parental-controls /root/parental-controls.bak
sudo cp -a /etc/sudoers.d/parental-controls-budgets /root/parental-controls-budgets.bak
sudo visudo -c
```

### Step 2 — remove the blanket

```bash
sudo mv /etc/sudoers.d/stan-nopasswd /root/stan-nopasswd.removed
sudo visudo -c          # MUST print "parsed OK"; abort if not
```

Keep it at `/root/stan-nopasswd.removed` rather than deleting it, so
rollback is a single `mv` back.

### Step 3 — APPLIED — the residuals are now closed

The gaps in §2 came from the trailing `*` spanning whitespace. The templates
now use POSIX extended regexes anchored `^…$` (sudo ≥ 1.9.10), which pin each
argv exactly. Applied in `sudoers-parental-controls{,-budgets}`; both render
and pass `visudo -c -f`. The legitimate command set is unchanged (all 16
coverage rows still authorize); the seven wildcard escapes below are now
DENIED.

```
stan ALL=(ALL) NOPASSWD: ^/usr/sbin/usermod -L [A-Za-z0-9_.-]+$, ^/usr/bin/usermod -L [A-Za-z0-9_.-]+$, ^/usr/sbin/usermod -U [A-Za-z0-9_.-]+$, ^/usr/bin/usermod -U [A-Za-z0-9_.-]+$
stan ALL=(ALL) NOPASSWD: ^/usr/sbin/loginctl terminate-user [A-Za-z0-9_.-]+$, ^/usr/bin/loginctl terminate-user [A-Za-z0-9_.-]+$
stan ALL=(ALL) NOPASSWD: ^/usr/sbin/loginctl show-user [A-Za-z0-9_.-]+ -p Sessions --value$, ^/usr/bin/loginctl show-user [A-Za-z0-9_.-]+ -p Sessions --value$
stan ALL=(ALL) NOPASSWD: ^/usr/sbin/loginctl show-session [A-Za-z0-9_.-]+ -p Class -p Type$, ^/usr/bin/loginctl show-session [A-Za-z0-9_.-]+ -p Class -p Type$
stan ALL=(ALL) NOPASSWD: ^/usr/sbin/loginctl lock-session [A-Za-z0-9_. -]+$, ^/usr/bin/loginctl lock-session [A-Za-z0-9_. -]+$
stan ALL=(ALL) NOPASSWD: ^/usr/sbin/passwd -S [A-Za-z0-9_.-]+$, ^/usr/bin/passwd -S [A-Za-z0-9_.-]+$
stan ALL=(root) NOPASSWD: ^/usr/sbin/systemd-run --machine=[A-Za-z0-9_.-]+@[.]host --user notify-send -u critical .* .*$, ^/usr/bin/systemd-run --machine=[A-Za-z0-9_.-]+@[.]host --user notify-send -u critical .* .*$
```

```
stan ALL=(root) NOPASSWD: ^/usr/sbin/tee /sys/fs/cgroup/parental-[A-Za-z0-9_.-]+/cgroup[.]freeze$, ^/usr/bin/tee /sys/fs/cgroup/parental-[A-Za-z0-9_.-]+/cgroup[.]freeze$
stan ALL=(root) NOPASSWD: ^/usr/sbin/tee /sys/fs/cgroup/parental-[A-Za-z0-9_.-]+/cgroup[.]procs$, ^/usr/bin/tee /sys/fs/cgroup/parental-[A-Za-z0-9_.-]+/cgroup[.]procs$
stan ALL=(root) NOPASSWD: ^/usr/sbin/cat /sys/fs/cgroup/parental-[A-Za-z0-9_.-]+/cgroup[.]freeze$, ^/usr/bin/cat /sys/fs/cgroup/parental-[A-Za-z0-9_.-]+/cgroup[.]freeze$
stan ALL=(root) NOPASSWD: ^/usr/sbin/cat /sys/fs/cgroup/parental-[A-Za-z0-9_.-]+/cgroup[.]procs$, ^/usr/bin/cat /sys/fs/cgroup/parental-[A-Za-z0-9_.-]+/cgroup[.]procs$
stan ALL=(root) NOPASSWD: ^/usr/sbin/mkdir -p /sys/fs/cgroup/parental-[A-Za-z0-9_.-]+$, ^/usr/bin/mkdir -p /sys/fs/cgroup/parental-[A-Za-z0-9_.-]+$
```

Note the `[.]` escapes — `:` is special to sudoers, and `.` should be
literal. Confirm with `visudo -c` after writing, and re-run the test suite
(`tests/test_deployment.py` asserts the *coverage* still holds, so a regex
that is too tight fails CI rather than silently breaking enforcement).

Note the one regex that stays deliberately broad: `systemd-run …
critical .* .*`, because the notify title and body are free text and are
app-generated, not user input.

### Rollback (any step, ~2 seconds)

```bash
sudo mv /root/stan-nopasswd.removed /etc/sudoers.d/stan-nopasswd
sudo chmod 440 /etc/sudoers.d/stan-nopasswd
sudo visudo -c
sudo systemctl restart parental-controls.service   # if you restarted it
```

For Step 3 only (rule-level rollback, blanket stays removed):

```bash
sudo cp -a /root/parental-controls.bak /etc/sudoers.d/parental-controls
sudo cp -a /root/parental-controls-budgets.bak /etc/sudoers.d/parental-controls-budgets
sudo visudo -c
```

---

## 6. Post-removal verification

Do this **as the human owner**, from a normal desktop session. Use a
**non-wheel throwaway account** you can lock and unlock freely — never
`tester`, `bluefox`, `stan` or `pcagent`. `pcverify` already carries the
scoped rules and is the right account for the sudo half:

```bash
# 1. Policy is what we think it is, and the blanket is gone.
sudo visudo -c
sudo -l                                   # no "(ALL) NOPASSWD: ALL" line
sudo -u pcverify sudo -l                   # scoped grants only

# 2. Each grant answers ALLOW (policy check only, runs nothing).
sudo -n -u pcverify sudo -n -l -- /usr/sbin/usermod -L pcverify
sudo -n -u pcverify sudo -n -l -- /usr/sbin/loginctl lock-session c2
sudo -n -u pcverify sudo -n -l -- /usr/sbin/systemd-run \
     --machine=pcverify@.host --user notify-send -u critical T B
sudo -n -u pcverify sudo -n -l -- /usr/sbin/tee \
     /sys/fs/cgroup/parental-pcverify-games/cgroup.freeze

# 3. The escalations are closed.
sudo -n -u pcverify sudo -n -l -- /usr/bin/bash      # must be DENY
sudo -n -u pcverify sudo -n -l -- /usr/sbin/tee /etc/shadow   # must be DENY

# 4. End-to-end through the panel, on a throwaway non-wheel account.
#    Log in to http://localhost:5000 as the admin and, for that account:
#      a) Lock        -> "Account locked and screen locked (...)"
#      b) Unlock      -> "OK"
#      c) Lock again  -> the child's screen locks and the session survives
#         (the Lock button covers the screen lock; there is no separate
#         "Lock Screen" button in the UI any more)
#      d) Emergency logout -> session terminated
#      e) Set a tiny app budget -> the category freezes (app freezes, not
#         the whole session); let it expire and confirm it thaws
#      f) Save a bedtime window starting in ~20 min -> the child gets a
#         critical notification (exercises systemd-run)
#    After each, check the service log:
sudo journalctl -u parental-controls.service -n 100 --no-pager | grep -iE \
     'sudo|password is required|not allowed|denied'

# 5. The enforce timer runs as the service user too — it must still work.
sudo systemctl start parental-controls-enforce.service
sudo systemctl status parental-controls-enforce.service --no-pager
sudo journalctl -u parental-controls-enforce.service -n 50 --no-pager

# 6. The trust boundary really is gone.
sudo -u pcverify sudo -n id      # must FAIL: "password is required"
```

`sudo: a password is required` in step 4's log is the **failure signature**
of an incomplete grant: it means some code path reached for a command the
scoped rules do not cover. Roll back, add the missing rule to the
appropriate template, re-run `tests/test_deployment.py`, then try again.

---

## 7. Regression tests added

`tests/test_deployment.py`, class `TestSudoersCoversTheCode` (plus a
faithful sudoers matcher and an `ast` argv scanner):

| test | pins |
|---|---|
| `test_required_commands_are_authorized` | all 16 commands → the template that must grant them |
| `test_every_sudo_call_in_the_code_is_authorized` | AST scan: no privileged argv escapes the templates |
| `test_scanner_sees_every_pinned_command` | pinned list and the code cannot drift apart, either direction |
| `test_dynamic_slots_are_covered_for_every_user_and_category` | every validator-admitted username × all 5 `CATEGORIES` |
| `test_lock_session_is_only_run_with_at_least_one_session_id` | the empty-`lockable` guard that keeps note A unreachable |
| `test_scoped_rules_do_not_authorize_privileged_escapes` | 21 must-never-authorize commands stay denied |
| `test_residual_privilege_of_trailing_wildcards_is_now_denied` | the seven former wildcard escapes are now denied |
| `test_regex_anchored_rules_deny_the_wildcard_attacks` | same escapes, via the pinned command list |
| `test_no_mutator_rule_uses_a_bare_trailing_wildcard` | no mutator regresses to a trailing `*` |
| `test_residual_privilege_does_not_include_a_root_shell` | the line between "narrower" and "contained" |

A future commit that adds a privileged command fails
`test_scanner_sees_every_pinned_command`; one that adds one **and** pins
it fails `test_every_sudo_call_in_the_code_is_authorized` until the
templates grant it.