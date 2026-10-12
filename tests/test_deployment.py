#!/usr/bin/env python3
"""Deployment-artifact regression tests.

Guards against silent-breakage classes seen in production:

1. ProtectSystem=strict makes /etc read-only for the service, so `sudo
   usermod` cannot write /etc/passwd, /etc/shadow or /etc/.pwd.lock and
   every Lock/Unlock fails with "cannot lock /etc/passwd; try again later"
   (observed 2026-10-10). The unit must keep directory-level /etc writable.
2. Checkout/home paths (~/Documents/Projects/...) must never be baked into
   committed deployment files; the installer renders absolute paths at
   install time from @APP_DIR@.
3. Installed sudoers files contained a literal `@SERVICE_USER@` (installer
   replaced only a leading "stan ", never the placeholder), granting nothing
   to the service user — masked on dev by the stan-nopasswd blanket rule,
   fatal on a real machine. The installer must replace the placeholder.
4. Sudoers allowed /usr/bin/usermod while sudo resolves bare `usermod` via
   secure_path to /usr/sbin/usermod (sudo matches on path string), and the
   app's `sudo passwd -S` status check had no rule at all.
5. Notifications never reached the child: the warning path needs
   `systemd-run --machine=<user>@.host --user notify-send ...` to run as root,
   and the template granted no `systemd-run` at all, so every pre-lock warning
   failed with "Failed to connect to system scope bus via machine transport".
   Masked on dev by the stan-nopasswd blanket rule.
6. A sudoers rule can be present but never match the command the app
   actually runs (sudoers matches arguments as one concatenated string, and
   a rule with too few argument slots matches nothing). The grant is pinned
   against the real argv, and every granted binary path must exist.

Pure file-content assertions; no Flask, no real accounts, nothing installed.
"""

import ast
import fnmatch
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SUDOERS_TEMPLATES = ('sudoers-parental-controls',
                     'sudoers-parental-controls-budgets')


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


def rule_lines(rel):
    """Non-comment, non-blank lines of a sudoers template."""
    return [l.strip() for l in read(rel).splitlines()
            if l.strip() and not l.strip().startswith('#')]


def granted_commands(rel):
    """[(binary, argument pattern)] for every entry in a sudoers template.

    sudoers command specifications are comma separated after NOPASSWD:, so
    each entry is split out and the binary separated from its arguments.
    """
    out = []
    for line in rule_lines(rel):
        spec = line.split('NOPASSWD:', 1)[-1]
        for item in spec.split(','):
            item = item.strip()
            if not item:
                continue
            binary, _, args = item.partition(' ')
            out.append((binary, ' '.join(args.split())))
    return out


def authorizes(rel, argv):
    """True if the template would let sudo run `argv` (sudoers(5) semantics).

    `argv` omits both `sudo` and the binary name (e.g. NOTIFY_ARGV). Command
    arguments are matched as a single, concatenated string against the rule's
    argument pattern: `^…$` is a POSIX ERE over the arguments, anything else
    uses the shell-style wildcard rules.
    """
    joined = ' '.join(argv)
    return any(args and _args_match(args, joined)
               for _binary, args in granted_commands(rel))


# The exact argv notify_user() runs — pinned by
# tests/test_notifications.py::TestNotifyUser::test_command_shape.
NOTIFY_ARGV = ['--machine=testchild@.host', '--user', 'notify-send',
               '-u', 'critical', 'Bedtime in 15 minutes', '9:00 PM']


# ---------------------------------------------------------------------------
# Faithful sudoers(5) matching
# ---------------------------------------------------------------------------
# `authorizes()` above is deliberately loose: it matches only the argument
# pattern and ignores which binary the argv names. That is fine for the
# "does the notifications grant leak?" assertions it serves, but it cannot
# answer the question this file's coverage tests ask -- "would sudo actually
# run the exact command the app runs?". So the helpers below model the real
# matching rules:
#
#   * the command name is resolved through sudo's secure_path, and the
#     RESOLVED PATH STRING is what sudo compares (that is the sbin/bin
#     duplication the templates carry);
#   * sudoers(5): "A forward slash will not be matched by wildcards used in
#     the file name portion of the command... When matching the command line
#     arguments, however, a slash does get matched by wildcards since
#     command line arguments may contain arbitrary strings and not just path
#     names." So `*` is anchored per path segment for the binary and is
#     unbounded for the arguments;
#   * the arguments are matched as ONE string: argv joined by single spaces.
#   * sudo's own options (`-n` in budgets.py) are consumed by sudo and are
#     never part of the command sudoers sees.

# `Defaults secure_path` from the target box's /etc/sudoers. Only the
# leading entries matter here: they decide which of /usr/sbin/X and
# /usr/bin/X sudo resolves a bare `X` to. The tail (snap, linuxbrew, ...) is
# omitted deliberately -- it never holds these binaries, and /etc/sudoers is
# not world-readable so this cannot be discovered at runtime. Both /usr/sbin
# and /usr/bin appear in the templates precisely so the resolution order
# cannot change the answer.
SECURE_PATH = ('/usr/local/sbin', '/usr/local/bin', '/usr/sbin', '/usr/bin',
               '/sbin', '/bin')

# Options consumed by sudo itself (never part of the matched command).
SUDO_OPTIONS = frozenset({'-n', '-H', '-S', '-b', '-E', '-k', '-K', '-l',
                          '-v', '-p', '-s', '-h', '-A', '-C', '-c', '-D',
                          '-g', '-R', '-T', '-t'})

CGROUP_ROOT = '/sys/fs/cgroup'


def _glob_to_regex(pattern, pathname):
    """Translate a sudoers wildcard pattern the way fnmatch(3) would.

    pathname=True applies FNM_PATHNAME semantics, where `*` cannot cross a
    `/` -- the rule sudoers uses for the command's path portion.
    """
    out, i, n = [], 0, len(pattern)
    while i < n:
        char = pattern[i]
        i += 1
        if char == '*':
            out.append('[^/]*' if pathname else '.*')
        elif char == '?':
            out.append('[^/]' if pathname else '.')
        elif char == '[':
            j = i
            if j < n and pattern[j] == '!':
                j += 1
            if j < n and pattern[j] == ']':
                j += 1
            while j < n and pattern[j] != ']':
                j += 1
            if j >= n:
                out.append(re.escape('['))
            else:
                body = pattern[i:j].replace('\\', '\\\\')
                i = j + 1
                if body[0] == '!':
                    body = '^' + body[1:]
                out.append(f'[{body}]')
        else:
            out.append(re.escape(char))
    return re.compile(''.join(out) + r'\Z', re.DOTALL)


def _command_matches(pattern, path):
    """Match a command path. sudoers(5) forbids `*` crossing a `/` here.

    A leading `^` and trailing `$` mean the pattern is already a POSIX
    extended regex (the hardened templates anchor every grant), so match it
    as-is. Otherwise fall back to wildcard semantics.
    """
    if pattern.startswith('^') and pattern.endswith('$'):
        return re.fullmatch(pattern, path) is not None
    return _glob_to_regex(pattern, pathname=True).match(path) is not None


def _args_match(pattern, args):
    """Match arguments: one concatenated string, `*` matches anything.

    A leading `^` and trailing `$` mean the pattern is already a POSIX
    extended regex (the hardened templates anchor every grant), so match it
    as-is. Otherwise fall back to wildcard semantics.
    """
    if pattern.startswith('^') and pattern.endswith('$'):
        return re.fullmatch(pattern, args) is not None
    return _glob_to_regex(pattern, pathname=False).match(args) is not None


def resolve_command(name):
    """The path sudo would exec for `name` after its secure_path search."""
    if '/' in name:
        return name
    for directory in SECURE_PATH:
        candidate = os.path.join(directory, name)
        if os.path.exists(candidate):
            return candidate
    return None


def parse_command_specs(body):
    """[(binary, argument pattern)] for the NOPASSWD specs in a sudoers body.

    Arguments beginning with `^` are a POSIX ERE over the arguments only
    (sudoers(5)); they are returned verbatim and matched by _args_match.
    """
    out = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        for item in line.split('NOPASSWD:', 1)[-1].split(','):
            item = item.strip()
            if not item:
                continue
            binary, _, args = item.partition(' ')
            out.append((binary, ' '.join(args.split())))
    return out


def rendered_template(rel, service_user='stan'):
    """A sudoers template as the installer would write it to /etc/sudoers.d."""
    return read(rel).replace('@SERVICE_USER@', service_user)


def sudoers_authorizes(rendered_body, argv):
    """True if `rendered_body` lets sudo run this literal `['sudo', ...]` argv.

    argv is the exact list the code hands to subprocess.run, dynamic slots
    included. Sudo options are stripped, the command resolved via
    secure_path, and the path + joined arguments matched against every
    granted command spec in the file.
    """
    rest = list(argv[1:])
    while rest and rest[0] in SUDO_OPTIONS:
        rest.pop(0)
    if not rest:
        return False
    path = resolve_command(rest[0])
    if path is None:
        return False
    arg_string = ' '.join(rest[1:])
    return any(
        binary and _command_matches(binary, path)
        and (not args or _args_match(args, arg_string))
        for binary, args in parse_command_specs(rendered_body))


def authorized_by_template(argv):
    """True if ANY rendered template authorizes argv."""
    return bool(authorize_sources(argv))


def authorize_sources(argv):
    """Which templates authorize argv -> set of template names."""
    return {rel for rel in SUDOERS_TEMPLATES
            if sudoers_authorizes(rendered_template(rel), argv)}


# ---------------------------------------------------------------------------
# The pinned required-command set
# ---------------------------------------------------------------------------
# Every privileged command this application can issue, as the exact argv
# passed to subprocess.run with the dynamic slots filled by representative
# values. `sudo-audit.md` carries the line-by-line derivation. Adding a
# privileged command to the code REQUIRES adding it here; if it is not
# granted by the templates, the tests below fail -- which is exactly the
# failure that a leaked `stan-nopasswd` blanket used to hide.

USER = 'testchild'                 # passes app.valid_username + budgets regex
SESSION_ID = 'c2'                  # loginctl session id (hex, no dash)
SESSION_IDS = ['c2', 'c3']         # multiple lockable sessions
CATEGORIES = ('games', 'browsers', 'media', 'chat', 'other')  # budgets.CATEGORIES

REQUIRED_COMMANDS = (
    # --- app.py: account lock/unlock + session control -------------------
    ('app.py:get_lock_status', ['sudo', 'passwd', '-S', USER],
     'sudoers-parental-controls'),
    ('app.py:lock_user_sessions',
     ['sudo', 'loginctl', 'show-user', USER, '-p', 'Sessions', '--value'],
     'sudoers-parental-controls'),
    ('app.py:lock_user_sessions',
     ['sudo', 'loginctl', 'lock-session'] + SESSION_IDS,
     'sudoers-parental-controls'),
    ('app.py:_session_supports_lock',
     ['sudo', 'loginctl', 'show-session', SESSION_ID, '-p', 'Class', '-p',
      'Type'],
     'sudoers-parental-controls'),
    ('app.py:run_lock_command', ['sudo', 'usermod', '-L', USER],
     'sudoers-parental-controls'),
    ('app.py:run_lock_command', ['sudo', 'usermod', '-U', USER],
     'sudoers-parental-controls'),
    ('app.py:api_lock', ['sudo', 'usermod', '-L', USER],
     'sudoers-parental-controls'),
    ('app.py:api_lock_screen', ['sudo', 'usermod', '-L', USER],
     'sudoers-parental-controls'),
    ('app.py:api_logout', ['sudo', 'usermod', '-L', USER],
     'sudoers-parental-controls'),
    ('app.py:api_logout',
     ['sudo', 'loginctl', 'terminate-user', USER],
     'sudoers-parental-controls'),
    # --- app.py: notifications ------------------------------------------
    ('app.py:notify_user',
     ['sudo', 'systemd-run', f'--machine={USER}@.host', '--user',
      'notify-send', '-u', 'critical', 'Bedtime in 15 minutes', '9:00 PM'],
     'sudoers-parental-controls'),
    # --- budgets.py: cgroup freezer --------------------------------------
    ('budgets.py:_sudo_read_file',
     ['sudo', '-n', 'cat',
      f'{CGROUP_ROOT}/parental-{USER}-games/cgroup.freeze'],
     'sudoers-parental-controls-budgets'),
    ('budgets.py:_sudo_read_file',
     ['sudo', '-n', 'cat',
      f'{CGROUP_ROOT}/parental-{USER}-games/cgroup.procs'],
     'sudoers-parental-controls-budgets'),
    ('budgets.py:_sudo_write_file',
     ['sudo', '-n', 'tee',
      f'{CGROUP_ROOT}/parental-{USER}-games/cgroup.freeze'],
     'sudoers-parental-controls-budgets'),
    ('budgets.py:_sudo_write_file',
     ['sudo', '-n', 'tee',
      f'{CGROUP_ROOT}/parental-{USER}-games/cgroup.procs'],
     'sudoers-parental-controls-budgets'),
    ('budgets.py:ensure_cgroup',
     ['sudo', '-n', 'mkdir', '-p', f'{CGROUP_ROOT}/parental-{USER}-games'],
     'sudoers-parental-controls-budgets'),
)

# Commands the scoped rules must NEVER authorize. These are the escalations
# the stan-nopasswd blanket currently permits and that scoped sudoers has to
# actually close before the blanket can be dropped; see sudo-audit.md.
# With trailing-wildcard rules these all passed, because `usermod -U *` spans
# whitespace and `/` in the argument string. The regex-anchored templates must
# now deny each of them: root mutation, wheel/uid-0 takeover, kill-everything,
# path traversal, and password-flag injection on passwd.
REGEX_ATTACKS = (
    ['sudo', 'usermod', '-U', '-p', '$y$j9$hash', 'root'],
    ['sudo', 'usermod', '-U', '-G', 'wheel', 'kidaccount'],
    ['sudo', 'usermod', '-U', '-o', '-u', '0', 'kidaccount'],
    ['sudo', 'usermod', '-L', '-U', 'root'],
    ['sudo', 'loginctl', 'terminate-user', '--all'],
    ['sudo', 'loginctl', 'terminate-user', '-f', '--all'],
    ['sudo', 'tee', '/sys/fs/cgroup/parental-kidaccount/../system.slice/'
                   'sshd.service/cgroup.procs'],
    ['sudo', 'tee', '/sys/fs/cgroup/../parental-kidaccount-games/cgroup.freeze'],
    ['sudo', 'passwd', '-S', '-d', 'root'],
    ['sudo', 'passwd', '-S', '-u', 'root'],
    ['sudo', 'passwd', '-S', '-e', 'kidaccount'],
)

MUST_NEVER_AUTHORIZE = (
    ['sudo', 'bash'],
    ['sudo', '/bin/bash'],
    ['sudo', '/usr/bin/bash', '-c', 'id'],
    ['sudo', 'sh', '-c', 'id'],
    ['sudo', 'python3', '-c', 'import os'],
    ['sudo', 'find', '/', '-exec', '/bin/sh', ';'],
    ['sudo', 'tee', '/etc/passwd'],
    ['sudo', 'tee', '/etc/shadow'],
    ['sudo', 'tee', '/etc/sudoers.d/pwn'],
    ['sudo', 'cat', '/etc/shadow'],
    ['sudo', 'cat', '/etc/sudoers.d/stan-nopasswd'],
    ['sudo', 'chmod', '777', '/etc/parental-controls'],
    ['sudo', 'cp', '/etc/shadow', '/tmp/x'],
    ['sudo', 'chown', 'root', '/etc/parental-controls'],
    ['sudo', 'systemctl', 'restart', 'sshd'],
    # cgroup writes must stay inside the parental- namespace
    ['sudo', 'tee', '/sys/fs/cgroup/system.slice/sshd.service/cgroup.procs'],
    ['sudo', 'tee', '/sys/fs/cgroup/user.slice/cgroup.procs'],
    ['sudo', 'mkdir', '-p', '/etc/parental-evil'],
    # the notification grant must stay pinned to notify-send
    ['sudo', 'systemd-run', f'--machine={USER}@.host', '--user', '/bin/sh'],
    ['sudo', 'systemd-run', f'--machine={USER}@.host', '--user', 'sh',
     '-c', 'id'],
    ['sudo', 'systemd-run', '--user', 'notify-send', '-u', 'critical', 'a',
     'b'],
)


# ---------------------------------------------------------------------------
# Pulling the privileged argv out of the application source
# ---------------------------------------------------------------------------
# Hand-maintained command lists go stale. These helpers re-derive the argv
# list straight from the code with `ast`, so a future commit that adds a new
# `subprocess.run(['sudo', ...])` is covered by the tests whether or not
# somebody remembers to update REQUIRED_COMMANDS.

# Dynamic slot -> the value substituted when checking authorization.
_DYNAMIC_VALUES = {
    '<username>': USER,
    '<session_id>': SESSION_ID,
    '<lockable...>': None,      # expands to SESSION_IDS (list)
    '<path>': None,             # expands per-command (cgroup leaf)
    '?': USER,                  # f-string interpolation site
}


def _argv_element(node):
    """Literal text of one argv element, or a marker for a dynamic one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # f'--machine={username}@.host' -> '--machine=?@.host'
        return ''.join(str(v.value) if isinstance(v, ast.Constant) else '?'
                       for v in node.values)
    if isinstance(node, ast.Name):
        return f'<{node.id}>'
    if isinstance(node, ast.Starred):
        return '<...>'
    return '<dyn>'


def _sudo_list_argvs(tree):
    """Every argv list literal headed by 'sudo', as (lineno, argv-tokens).

    Handles the two shapes the code uses: a plain list, and a list
    concatenated with a runtime list (`['sudo', 'loginctl', 'lock-session']
    + lockable`), which carries the session ids the rule must still match.
    """
    # A list that is the left operand of `list + name` is the prefix of a
    # larger argv; scanning it standalone would report a bare
    # `loginctl lock-session` that the code never actually runs.
    concatenated = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
                and isinstance(node.left, ast.List)):
            concatenated.add(id(node.left))

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and node.elts:
            head = node.elts[0]
            if (isinstance(head, ast.Constant) and head.value == 'sudo'
                    and id(node) not in concatenated):
                found.append((node.lineno,
                              [_argv_element(e) for e in node.elts]))
        elif (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
                and isinstance(node.left, ast.List) and node.left.elts):
            head = node.left.elts[0]
            if isinstance(head, ast.Constant) and head.value == 'sudo':
                tokens = [_argv_element(e) for e in node.left.elts]
                if isinstance(node.right, ast.Name):
                    tokens.append(f'<{node.right.id}...>')
                found.append((node.lineno, tokens))
    return sorted(found)


def _instantiate(tokens):
    """Concrete argv(s) for a token list, or None if a slot is unknown.

    `['sudo', 'loginctl', 'lock-session'] + lockable` yields the populated
    form only -- the bare `lock-session` prefix is not a runnable argv, and
    whether the code can ever build the empty form is a separate question
    answered by the empty-list guard test.
    """
    # The cgroup path slot is the only one with a per-command expansion, so
    # we need the command name -- skipping sudo's own options to find it.
    command = None
    for token in tokens[1:]:
        if token in SUDO_OPTIONS or token.startswith('<'):
            continue
        command = token
        break
    # A trailing list-concatenation slot always contributes operands.
    variadic = [t for t in tokens if t.endswith('...>')]
    tokens = [t for t in tokens if not t.endswith('...>')]
    # f-string sites carry a bare '?' inside a larger literal
    # (e.g. '--machine=?@.host'); substitute the representative value so an
    # anchored-regex grant can match it the way sudo would.
    tokens = [t.replace('?', USER) if '?' in t and not t.startswith('<')
              else t for t in tokens]
    outputs = [list(tokens)]
    for index, token in enumerate(tokens):
        if token not in _DYNAMIC_VALUES:
            if token.startswith('<') and token not in ('<...>', '<dyn>'):
                return None          # unknown slot: caller must notice
            continue
        value = _DYNAMIC_VALUES[token]
        if token == '<path>':
            base = f'{CGROUP_ROOT}/parental-{USER}-{CATEGORIES[0]}'
            leaves = ([f'{base}/cgroup.freeze', f'{base}/cgroup.procs']
                      if command in ('cat', 'tee') else [base])
            outputs = [out[:index] + [leaf] + out[index + 1:]
                       for out in outputs for leaf in leaves]
        elif token == '<lockable...>':
            outputs = [out[:index] + list(SESSION_IDS) + out[index + 1:]
                       for out in outputs]
        else:
            outputs = [out[:index] + [value] + out[index + 1:]
                       for out in outputs]
    # Trailing list-concatenation operands ('... + lockable') come last.
    tails = [list(SESSION_IDS) if t == '<lockable...>' else ['x']
             for t in variadic]
    if variadic:
        # The list-concatenation always contributes operands at runtime, so
        # the bare prefix is not a separately reachable argv.
        return [out + tail for out in outputs for tail in tails]
    return outputs


def code_argvs(module):
    """[(lineno, concrete argv)] for every sudo call in `module`."""
    tree = ast.parse(read(module))
    out = []
    for lineno, tokens in _sudo_list_argvs(tree):
        for argv in _instantiate(tokens) or []:
            out.append((lineno, argv))
    return out


def _shape(argv):
    """The argv reduced to `sudo <command> <flag skeleton>`.

    Dynamic slots (usernames, session ids, cgroup paths, notify titles)
    become '*', so this identifies a *command shape* rather than one
    particular invocation. Used to detect drift between the pinned list and
    the code without depending on which representative value was chosen.
    """
    rest = [t for t in argv[1:] if t not in SUDO_OPTIONS]
    command = rest[0] if rest else '?'
    # loginctl's verb is the operation, so it belongs in the shape.
    if command == 'loginctl':
        verb = next((t for t in rest[1:] if not t.startswith('-')), '?')
        return (command, verb)
    skeleton = []
    for token in rest[1:]:
        # Options and option=value slots identify a shape; bare operands
        # (usernames, session ids, paths, titles) are the dynamic part.
        if token.startswith('-'):
            skeleton.append(token.split('=', 1)[0] + ('=' if '=' in token
                                                      else ''))
    return (command, tuple(skeleton))


def code_shapes(module):
    """{command shape} for every sudo call in `module`."""
    return {_shape(argv) for _, argv in code_argvs(module)}


def pinned_shapes():
    """{command shape} for every entry of REQUIRED_COMMANDS."""
    return {_shape(argv) for _, argv, _ in REQUIRED_COMMANDS}


class TestSudoersCoversTheCode(unittest.TestCase):
    """Every privileged command the code runs must be authorized.

    This is the guard that lets the blanket `stan-nopasswd` rule go away:
    if a new privileged command is not covered by the scoped templates, CI
    fails here instead of the failure surfacing on a child's locked screen.
    """

    MODULES = ('app.py', 'budgets.py')

    def test_required_commands_are_authorized(self):
        for origin, argv, expected in REQUIRED_COMMANDS:
            with self.subTest(origin=origin, argv=' '.join(argv)):
                sources = authorize_sources(argv)
                self.assertIn(
                    expected, sources,
                    f'{origin} runs `sudo {" ".join(argv[1:])}` but no '
                    f'rendered template authorizes it; a rule matching this '
                    f'argv must exist in {expected}')

    def test_every_sudo_call_in_the_code_is_authorized(self):
        """AST scan: no privileged argv escapes the templates.

        A `['sudo', ...]` list the templates do not authorize would only
        fail at runtime, and the stan-nopasswd blanket was hiding that.
        """
        for module in self.MODULES:
            argvs = code_argvs(module)
            self.assertTrue(argvs, f'no sudo argv found in {module} -- the '
                                   'scanner is broken, not the code')
            for lineno, argv in argvs:
                with self.subTest(module=module, line=lineno,
                                  argv=' '.join(argv)):
                    self.assertTrue(
                        authorized_by_template(argv) or
                        _unreachable_degenerate(argv),
                        f'{module}:{lineno} runs `sudo {" ".join(argv[1:])}`'
                        ' which no sudoers template authorizes')

    def test_scanner_sees_every_pinned_command(self):
        """The pinned list and the AST scan must not drift apart.

        Compared by command shape, so a rename is caught while a different
        representative username is not a false alarm. Drift in either
        direction is a bug: an unpinned command means the coverage test
        above is not seeing the whole surface.
        """
        code = set().union(*(code_shapes(m) for m in self.MODULES))
        pinned = pinned_shapes()
        self.assertFalse(
            pinned - code,
            'pinned but absent from the code (renamed or removed): '
            + '; '.join(sorted(map(str, pinned - code))))
        self.assertFalse(
            code - pinned,
            'privileged commands in the code that REQUIRED_COMMANDS does '
            'not pin -- add them to tests/test_deployment.py: '
            + '; '.join(sorted(map(str, code - pinned))))

    def test_dynamic_slots_are_covered_for_every_user_and_category(self):
        """cgroup argv built from username+category must always authorize.

        These are the dynamic cases: the path is assembled at runtime, so
        sweep the shapes the validators actually admit -- hyphens and dots
        in names, and every CATEGORIES value.
        """
        for user in ('a', USER, 'x' * 32, 'user-name', 'user.name',
                     'a.b-c_d'):
            for category in CATEGORIES:
                base = f'{CGROUP_ROOT}/parental-{user}-{category}'
                cases = (
                    ['sudo', '-n', 'cat', f'{base}/cgroup.freeze'],
                    ['sudo', '-n', 'cat', f'{base}/cgroup.procs'],
                    ['sudo', '-n', 'tee', f'{base}/cgroup.freeze'],
                    ['sudo', '-n', 'tee', f'{base}/cgroup.procs'],
                    ['sudo', '-n', 'mkdir', '-p', base],
                    ['sudo', 'usermod', '-L', user],
                    ['sudo', 'usermod', '-U', user],
                    ['sudo', 'passwd', '-S', user],
                    ['sudo', 'loginctl', 'terminate-user', user],
                    ['sudo', 'loginctl', 'show-user', user, '-p', 'Sessions',
                     '--value'],
                    ['sudo', 'systemd-run', f'--machine={user}@.host', '--user',
                     'notify-send', '-u', 'critical', 'T', 'B'],
                )
                for argv in cases:
                    with self.subTest(argv=' '.join(argv)):
                        self.assertTrue(
                            authorized_by_template(argv),
                            'not authorized: sudo ' + ' '.join(argv[1:]))

    def test_lock_session_is_only_run_with_at_least_one_session_id(self):
        """`loginctl lock-session` needs >=1 id; `lock-session *` needs one.

        With zero ids the code would build the argv 'loginctl lock-session',
        which the template does NOT authorize. Prove the guard exists rather
        than assuming it: lock_user_sessions() returns early on an empty
        list, and the only lock-session call site is inside that function.
        """
        tree = ast.parse(read('app.py'))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == 'lock_user_sessions')
        self.assertRegex(
            read('app.py'),
            r"if not lockable:\s*\n\s+return 'no lockable \(graphical\) session'",
            'lock_user_sessions must bail out on an empty `lockable` list; '
            'without that guard the bare `loginctl lock-session` argv is '
            'not authorized by sudoers')
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add)]
        self.assertTrue(calls, 'lock-session argv construction disappeared')

    def test_scoped_rules_do_not_authorize_privileged_escapes(self):
        """Least privilege: the grants must not reach a root shell or /etc.

        This is the property the stan-nopasswd blanket currently defeats.
        """
        for argv in MUST_NEVER_AUTHORIZE:
            with self.subTest(argv=' '.join(argv)):
                self.assertFalse(
                    authorized_by_template(argv),
                    'scoped sudoers must NOT authorize: sudo '
                    + ' '.join(argv[1:]))

    def test_regex_anchored_rules_deny_the_wildcard_attacks(self):
        """The six attacks the trailing-wildcard rules allowed are now denied.

        sudo matches arguments as ONE space-joined string, so `usermod -U *`
        also matched `usermod -U -p <hash> root`. The anchored regexes must
        reject every one of these while still authorizing the legitimate
        commands (covered by the coverage tests above).
        """
        for argv in REGEX_ATTACKS:
            with self.subTest(argv=' '.join(argv)):
                self.assertFalse(
                    authorized_by_template(argv),
                    'regex-anchored sudoers must NOT authorize: sudo '
                    + ' '.join(argv[1:]))

    def test_no_mutator_rule_uses_a_bare_trailing_wildcard(self):
        """A trailing `*` on a mutating command is the bug this fixes.

        `usermod -U *` spans whitespace in the argument string, so it
        authorized root mutation. Every mutating command must carry an
        args-anchored POSIX ERE (`^…$`) instead. The notify grant keeps
        `.* .*` deliberately: title and body are free text.
        """
        for rel in ('sudoers-parental-controls',
                    'sudoers-parental-controls-budgets'):
            for line in read(rel).splitlines():
                if line.startswith('#') or '@SERVICE_USER@' not in line:
                    continue
                for grant in line.split(':', 1)[1].split(','):
                    grant = grant.strip()
                    if not grant:
                        continue
                    parts = grant.split(None, 1)
                    if len(parts) < 2:
                        continue
                    binary = parts[0].rsplit('/', 1)[-1]
                    if binary in ('usermod', 'loginctl', 'tee', 'mkdir',
                                  'passwd'):
                        args = parts[1]
                        self.assertTrue(
                            args.startswith('^') and args.endswith('$'),
                            f'{rel}: mutating grant must anchor its arguments,'
                            f' got: {grant}')

    def test_residual_privilege_of_trailing_wildcards_is_now_denied(self):
        """The regex-anchored templates closed the trailing-wildcard residuals.

        This began life as a characterisation test documenting what the bare
        `usermod -U *` form allowed (sudoers(5) matches arguments as one
        concatenated string and `*` spans whitespace). The templates are now
        anchored regexes, so every one of those escapes must be DENIED.
        """
        residuals = (
            ['sudo', 'usermod', '-U', '-G', 'wheel', 'bluefox'],
            ['sudo', 'usermod', '-L', '-s', '/bin/bash', 'bluefox'],
            ['sudo', 'usermod', '-L', '-U', 'bluefox'],
            ['sudo', 'loginctl', 'terminate-user', '--all'],
        )
        for argv in residuals:
            with self.subTest(argv=' '.join(argv)):
                self.assertFalse(
                    authorized_by_template(argv),
                    'anchored templates must deny the old residual: sudo '
                    + ' '.join(argv[1:]))

    def test_residual_privilege_does_not_include_a_root_shell(self):
        """Even with the trailing-wildcard residuals, no shell is reachable.

        This is the line between "narrower" and "actually contained".
        """
        for argv in (['sudo', 'sh'], ['sudo', 'bash'], ['sudo', 'dash'],
                     ['sudo', 'env'], ['sudo', 'perl'], ['sudo', 'vi'],
                     ['sudo', 'tee', '/etc/shadow'],
                     ['sudo', 'cat', '/etc/shadow']):
            with self.subTest(argv=' '.join(argv)):
                self.assertFalse(
                    authorized_by_template(argv),
                    'scoped sudoers must never authorize a shell: sudo '
                    + ' '.join(argv[1:]))


def _unreachable_degenerate(argv):
    """True for argvs the code provably never builds.

    `loginctl lock-session` with no session id is not authorized by
    `lock-session *`; it is only reachable if the empty-list guard is
    removed, which its own test covers.
    """
    return argv[1:4] == ['loginctl', 'lock-session'] and len(argv) == 4


class TestUnitSandbox(unittest.TestCase):
    def test_protect_system_is_strict(self):
        unit = read(os.path.join('systemd', 'parental-controls.service'))
        directives = [l for l in unit.splitlines()
                      if l.startswith('ProtectSystem=')]
        self.assertEqual(directives, ['ProtectSystem=strict'],
                         'the only ProtectSystem directive must be "strict"')

    def test_etc_is_writable_for_usermod(self):
        unit = read(os.path.join('systemd', 'parental-controls.service'))
        # /etc (directory level) is required: per-file ReadWritePaths for
        # /etc/shadow and /etc/gshadow (mode 0000) fails namespace setup
        # (SELinux denies mounton on shadow_t; unit exits 226/NAMESPACE).
        self.assertIn('ReadWritePaths=@APP_DIR@ /etc/parental-controls /etc',
                      unit)


class TestNoHardcodedPaths(unittest.TestCase):
    """Deployment files must not name the dev checkout/home location."""

    FILES = [
        'systemd/parental-controls.service',
        'systemd/parental-controls-enforce.service',
        'systemd/parental-controls-enforce.timer',
        'install.sh',
        'enforce_bedtime_timer.py',
        'sudoers-parental-controls',
        'sudoers-parental-controls-budgets',
        'sudo-audit.md',
        'tls_serve.py',
        'tls_lib.sh',
    ]

    def test_no_checkout_home_paths_in_deployment_files(self):
        bad = re.compile(r'(/home/|Documents/Projects)')
        for rel in self.FILES:
            self.assertNotRegex(
                read(rel), bad,
                f'{rel} references a checkout/home path')


class TestInstallerStaging(unittest.TestCase):
    def test_default_install_dir_is_opt_personal_controls(self):
        script = read('install.sh')
        self.assertIn('INSTALL_DIR="/opt/parental-controls"', script)

    def test_service_user_defaults_to_the_dedicated_account(self):
        # The panel must not run as the shared human/agent login account.
        script = read('install.sh')
        self.assertIn('DEFAULT_SERVICE_USER="parental-controls"', script)
        self.assertNotRegex(script, r'(?m)^SERVICE_USER=.*SUDO_USER')
        self.assertIn('--user) SERVICE_USER="$2"', script)

    def test_units_use_placeholders_not_absolute_paths(self):
        for rel in ('systemd/parental-controls.service',
                    'systemd/parental-controls-enforce.service'):
            unit = read(rel)
            self.assertIn('@APP_DIR@', unit)
            self.assertNotIn('@SERVICE_USER@=stan', unit)

    def test_installer_replaces_service_user_placeholder(self):
        script = read('install.sh')
        self.assertIn('s|@SERVICE_USER@|${SERVICE_USER}|g', script)

    def test_runtime_state_never_staged(self):
        """Every runtime state file must be excluded from BOTH staging
        paths (rsync primary + tar fallback) so a deploy can never
        overwrite — or delete via --delete — live state, and so the
        tamper-evident audit log is never clobbered by a checkout copy."""
        script = read('install.sh')
        for name in ('bedtime.json', 'budgets.json', 'process_prefs.json',
                     'auth.json', 'audit.jsonl',
                     'state.db', 'state.db-wal', 'state.db-shm',
                     'state.db.bak.*'):
            self.assertIn(f'--exclude=/{name}', script,
                          f'rsync must exclude {name}')
            self.assertIn(f"--exclude='./{name}'", script,
                          f'tar fallback must exclude {name}')


class TestBindOptions(unittest.TestCase):
    def test_safe_defaults(self):
        script = read('install.sh')
        self.assertIn('BIND_HOST="127.0.0.1"', script)
        self.assertIn('BIND_PORT="5000"', script)
        self.assertIn('OPEN_FIREWALL=0', script)

    def test_unit_renders_bind_from_placeholders(self):
        unit = read(os.path.join('systemd', 'parental-controls.service'))
        self.assertIn('@BIND_HOST@', unit)
        self.assertIn('@BIND_PORT@', unit)
        for line in unit.splitlines():
            if line.startswith('ExecStart='):
                self.assertNotIn('127.0.0.1', line)
                self.assertNotIn('5000', line)

    def test_installer_substitutes_bind_placeholders(self):
        script = read('install.sh')
        self.assertIn('s|@BIND_HOST@|$BIND_HOST|g', script)
        self.assertIn('s|@BIND_PORT@|$BIND_PORT|g', script)

    def test_firewall_change_is_opt_in(self):
        script = read('install.sh')
        self.assertIn('--open-firewall', script)
        self.assertIn('firewall-cmd', script)
        # The firewall commands only run inside the opt-in branch.
        idx_flag = script.index('--open-firewall) OPEN_FIREWALL=1')
        idx_gate = script.index('if [ "$OPEN_FIREWALL" -eq 1 ]')
        idx_fw = script.index('firewall-cmd --permanent --add-port')
        self.assertLess(idx_flag, idx_gate)
        self.assertLess(idx_gate, idx_fw)

    def test_port_and_bind_are_validated(self):
        script = read('install.sh')
        self.assertIn("Invalid --port", script)
        self.assertIn("Invalid --bind", script)

    def test_service_restarts_only_on_unit_change(self):
        script = read('install.sh')
        self.assertIn('UNITS_CHANGED=0', script)
        self.assertIn('UNITS_CHANGED=1', script)
        self.assertIn('systemctl restart parental-controls.service', script)
        self.assertIn('units unchanged', script.lower())

    def test_stale_process_detected_via_mtime(self):
        script = read('install.sh')
        self.assertIn('ActiveEnterTimestamp', script)
        self.assertIn('NEED_RESTART', script)

    def test_lan_verify_hints_at_firewall(self):
        script = read('install.sh')
        self.assertIn('--open-firewall', script)
        self.assertIn('re-run with --open-firewall', script)

    def test_tls_flags_are_parsed(self):
        script = read('install.sh')
        for flag in ('--tls)', '--tls-cert)', '--tls-key)', '--tls-name)',
                     '--regen-tls-cert)', '--insecure-lan)'):
            self.assertIn(flag, script)

    def test_tls_variant_placeholders_live_in_the_panel_unit(self):
        unit = read(os.path.join('systemd', 'parental-controls.service'))
        self.assertIn('@TLS_EXECSTART@', unit)
        self.assertIn('@TLS_ENV_LINE@', unit)
        self.assertIn('tls_serve.py', read('tls_lib.sh'))
        self.assertTrue(os.path.isfile(os.path.join(ROOT, 'tls_serve.py')))
        self.assertTrue(os.path.isfile(os.path.join(ROOT, 'tls_lib.sh')))


class TestNextStepsOutput(unittest.TestCase):
    """The install.sh 'Next steps' block must present a direct, clickable URL
    as step 2 in every bind mode, with mode-specific context as a hint line
    below it (regression: loopback step 2 used to be a bare ssh -L command,
    and the LAN variants printed prose instead of a plain link)."""

    def _block(self):
        """The final 'Next steps' block: from the 'Next steps:' line through
        the '3. Log in' line (step 2 and its hint sit between them)."""
        lines = read('install.sh').splitlines()
        start = next(i for i, l in enumerate(lines) if 'Next steps:' in l)
        end = next(i for i, l in enumerate(lines)
                   if "3. Log in and change the password" in l)
        return lines[start:end + 1]

    def _step2_line(self, block, url):
        """The step-2 source line whose URL matches `url`. All three mode
        variants coexist in the source inside the case statement; the shell
        renders only one at install time, so each mode is pinned by its URL."""
        step2 = [l for l in block
                 if '"  2. Open your panel at:' in l and url in l]
        self.assertEqual(
            len(step2), 1,
            'expected exactly one step-2 line for %r, got %r' % (url, step2))
        return step2[0]

    def _block_text(self, block):
        return '\n'.join(block)

    def test_loopback_direct_url_with_tunnel_hint(self):
        block = self._block()
        text = self._block_text(block)
        self.assertIn(
            '  2. Open your panel at: http://localhost:${BIND_PORT}',
            self._step2_line(block, 'http://localhost:${BIND_PORT}'))
        self.assertIn(
            '(from another machine, tunnel first: '
            'ssh -L ${BIND_PORT}:localhost:${BIND_PORT} <user>@<host>)',
            text)

    def test_tls_direct_url_with_fingerprint_hint(self):
        block = self._block()
        text = self._block_text(block)
        self.assertIn(
            '  2. Open your panel at: https://${TLS_URL_NAME}:${BIND_PORT}',
            self._step2_line(block, 'https://${TLS_URL_NAME}:${BIND_PORT}'))
        self.assertIn(
            "(verify the printed SHA-256 fingerprint at the "
            "browser's certificate warning)",
            text)

    def test_insecure_direct_url_with_plain_http_hint(self):
        block = self._block()
        text = self._block_text(block)
        self.assertIn(
            '  2. Open your panel at: http://${BIND_HOST}:${BIND_PORT}',
            self._step2_line(block, 'http://${BIND_HOST}:${BIND_PORT}'))
        self.assertIn(
            '(warning: plain HTTP — session cookies travel unencrypted)',
            text)

    def test_step2_is_never_a_bare_ssh_tunnel(self):
        """Every '  2. ' source line is the URL line; the tunnel is only the
        loopback hint, so step 2 always shows the clickable URL."""
        for line in read('install.sh').splitlines():
            if '"  2. ' in line:
                self.assertTrue(
                    '"  2. Open your panel at:' in line,
                    'non-URL step-2 line: %r' % (line,))
                self.assertNotIn('ssh -L', line)

    def test_old_step2_phrasings_removed(self):
        script = read('install.sh')
        self.assertNotIn('From your machine: ssh -L', script)
        self.assertNotIn('2. Open http://', script)
        self.assertNotIn('2. Open https://', script)
        self.assertNotIn(' from a machine on the LAN (verify the printed',
                         script)

    def test_step1_and_step3_unchanged(self):
        script = read('install.sh')
        self.assertIn(
            "  1. Get the one-time admin credential: journalctl -u "
            "parental-controls.service | grep 'AUTH BOOTSTRAP'", script)
        self.assertIn(
            '  3. Log in and change the password immediately (forced on '
            'first login).', script)


class TestSudoersTemplates(unittest.TestCase):
    def test_templates_use_placeholder_not_a_real_user(self):
        for rel in ('sudoers-parental-controls',
                    'sudoers-parental-controls-budgets'):
            body = read(rel)
            self.assertIn('@SERVICE_USER@', body)
            self.assertNotRegex(body, r'(?m)^stan\s')

    def test_templates_render_for_the_dedicated_service_user(self):
        for rel in ('sudoers-parental-controls',
                    'sudoers-parental-controls-budgets'):
            rendered = read(rel).replace('@SERVICE_USER@',
                                         'parental-controls')
            self.assertNotIn('@SERVICE_USER@', rendered)
            self.assertRegex(rendered, r'(?m)^parental-controls\s')

    def test_rendered_templates_contain_no_placeholder(self):
        import re as _re
        script = read('install.sh')
        m = _re.search(r's\|@SERVICE_USER@\|\$\{SERVICE_USER\}\|g', script)
        self.assertIsNotNone(m, 'installer must substitute @SERVICE_USER@')
        for rel in ('sudoers-parental-controls',
                    'sudoers-parental-controls-budgets'):
            rendered = read(rel).replace('@SERVICE_USER@', 'stan')
            self.assertNotIn('@SERVICE_USER@', rendered)
            self.assertRegex(rendered, r'(?m)^stan\s')

    def test_account_rules_cover_resolved_sbin_and_bin_paths(self):
        body = read('sudoers-parental-controls')
        for verb, sub in (('usermod', '^-L'), ('usermod', '^-U'),
                          ('loginctl', '^terminate-user'),
                          ('loginctl', '^show-user'),
                          ('loginctl', '^lock-session'),
                          ('loginctl', '^show-session')):
            for root in ('/usr/sbin', '/usr/bin'):
                self.assertIn(f'{root}/{verb} {sub} ', body)

    def test_passwd_status_check_is_allowed_and_scoped(self):
        body = read('sudoers-parental-controls')
        self.assertIn('/usr/sbin/passwd ^-S [A-Za-z0-9_.-]+$', body)
        self.assertIn('/usr/bin/passwd ^-S [A-Za-z0-9_.-]+$', body)
        # No unscoped passwd rule: every passwd entry must carry the -S flag,
        # and -S is anchored so no other flag (-d/-u/-e) can ride along.
        for line in body.splitlines():
            if 'passwd' in line and not line.strip().startswith('#'):
                self.assertIn('passwd ^-S ', line)

    def test_budget_rules_cover_resolved_sbin_and_bin_paths(self):
        body = read('sudoers-parental-controls-budgets')
        for binary in ('tee', 'cat', 'mkdir'):
            self.assertIn(f'/usr/sbin/{binary}', body)
            self.assertIn(f'/usr/bin/{binary}', body)

    NOTIFY_ARGS = ('^--machine=[A-Za-z0-9_.-]+@[.]host --user notify-send '
                   '-u critical .* .*$')

    def test_systemd_run_granted_for_notifications(self):
        body = read('sudoers-parental-controls')
        for binary in ('/usr/sbin/systemd-run', '/usr/bin/systemd-run'):
            self.assertIn(f'{binary} {self.NOTIFY_ARGS}', body)

    def test_systemd_run_rule_is_scoped_to_the_machine_transport(self):
        for line in rule_lines('sudoers-parental-controls'):
            if 'systemd-run' not in line:
                continue
            self.assertIn('^--machine=[A-Za-z0-9_.-]+@[.]host', line)
            self.assertIn('--user notify-send -u critical', line)
            # `--user *` would authorize any command in a child's session
            # (sudoers(5): "Preventing shell escapes").

    def test_systemd_run_rule_matches_the_command_the_app_runs(self):
        self.assertTrue(
            authorizes('sudoers-parental-controls', NOTIFY_ARGV),
            'no granted command authorizes: '
            f'systemd-run {" ".join(NOTIFY_ARGV)}')

    def test_notify_user_uses_the_granted_transport(self):
        src = read('app.py')
        for token in ('--machine={username}@.host', "'--user'",
                      "'notify-send'", "'critical'"):
            self.assertIn(token, src)

    def test_notification_grant_authorizes_nothing_else(self):
        """Least privilege: notify-send only, and only via --machine."""
        for payload in (
                ['--machine=testchild@.host', '--user', '/bin/sh'],
                ['--machine=testchild@.host', '--user', 'sh', '-c', 'id'],
                ['--user', 'notify-send', '-u', 'critical', 'T', 'B']):
            self.assertFalse(
                authorizes('sudoers-parental-controls', payload),
                'notifications grant must not authorize: '
                f'systemd-run {" ".join(payload)}')

    def test_granted_binary_paths_exist_on_this_box(self):
        for rel in ('sudoers-parental-controls',
                    'sudoers-parental-controls-budgets'):
            for binary, _ in granted_commands(rel):
                self.assertTrue(
                    os.path.exists(binary),
                    f'{rel} grants {binary}, which does not exist here')


if __name__ == '__main__':
    unittest.main()