#!/usr/bin/env python3
"""venv self-heal tests (incident 2026-11-14, see venv_lib.sh header).

An rpm-ostree image update swapped python3 under the install dir's venv;
install.sh's old presence-only guard kept the version-orphaned venv and
the panel crash-looped `ModuleNotFoundError: No module named 'cheroot'`.
venv_lib.sh's probe asks the venv interpreter itself whether it can find
and import every distribution named in requirements.txt, and ensure_venv
rebuilds when that fails.

Idioms: venv_lib.sh helpers run sourced in bash (the same harness as
tests/test_install_webserver.py's tls_lib.sh runner), with scripted
run()/as_user() stubs — dry-run style (commands echoed, nothing
executed) and real style (commands executed, inside the temp dir only).
All venvs are throwaway fixtures: `python -m venv --without-pip` plus
fake .dist-info metadata and a stub module, so no pip and no network.
Installer wiring is pinned as text like the other install.sh guards.
Nothing system-wide is touched; live post-image-update behavior on the
VM/aiden-pc is a checklist item, not a test.
"""

import glob
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VENV_LIB = os.path.join(ROOT, 'venv_lib.sh')


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


def lib(stubs, func, *args):
    """Run a venv_lib.sh helper with a bash preamble after sourcing.

    `stubs` defines the installer globals/helpers the functions rely on
    (run, as_user, VENV_HOME, VENV_CACHE) — the scripted-stub style of
    tests/test_install_webserver.py's lib() runner, extended with the
    preamble because ensure_venv calls through install.sh's wrappers.
    """
    script = 'source "$0"\n%s\n"%s" "${@:1}"' % (stubs, func)
    return subprocess.run(['bash', '-c', script, VENV_LIB, *args],
                          capture_output=True, text=True, timeout=120)


def dry_stubs(tmpd):
    """Echoing stubs: plans without side effects (the --dry-run shape)."""
    return ('run() { echo "[dry-run] $*"; }\n'
            'as_user() { echo "[dry-run] sudo -u pcsvc $*"; }\n'
            'VENV_HOME="%s"; VENV_CACHE="%s/.cache";' % (tmpd, tmpd))


def real_stubs(tmpd):
    """Executing stubs: the same commands run for real under `tmpd`."""
    return ('run() { "$@"; }\n'
            'as_user() { "$@"; }\n'
            'VENV_HOME="%s"; VENV_CACHE="%s/.cache";' % (tmpd, tmpd))


def make_venv(tmpd, name='venv'):
    """A real throwaway venv, no pip (hermetic: no network, fast)."""
    venv = os.path.join(tmpd, name)
    proc = subprocess.run(
        [sys.executable, '-m', 'venv', '--without-pip', venv],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return venv


def site_packages(venv):
    hits = glob.glob(os.path.join(venv, 'lib', 'python*', 'site-packages'))
    assert len(hits) == 1, hits
    return hits[0]


def add_fake_dist(venv, dist, module):
    """Install a fake distribution the way pip does: .dist-info named with
    the NORMALIZED dist name (dashes -> underscores), import `module`."""
    site = site_packages(venv)
    info = os.path.join(site, f'{dist.replace("-", "_")}-1.0.dist-info')
    os.makedirs(info, exist_ok=True)
    with open(os.path.join(info, 'METADATA'), 'w', encoding='utf-8') as fh:
        fh.write(f'Metadata-Version: 2.1\nName: {dist}\nVersion: 1.0\n')
    with open(os.path.join(site, f'{module}.py'), 'w', encoding='utf-8') as fh:
        fh.write('HEALED = True\n')


def write_requirements(tmpd, text, name='requirements.txt'):
    path = os.path.join(tmpd, name)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)
    return path


def orphan(venv):
    """Simulate the incident: an interpreter swap means the interpreter's
    own lib/pythonX.Y no longer holds the old site-packages. Rename the
    tree to a foreign minor version and rewrite pyvenv.cfg's version, so
    bin/python stays present and runnable while every installed
    distribution silently falls off sys.path — exactly the state the old
    presence check waved through."""
    lib_dir = os.path.dirname(site_packages(venv))
    os.rename(lib_dir, os.path.join(os.path.dirname(lib_dir), 'python9.9'))
    cfg = os.path.join(venv, 'pyvenv.cfg')
    with open(cfg, encoding='utf-8') as fh:
        text = fh.read()
    text = re.sub(r'(?m)^version = .+$', 'version = 9.9.0', text)
    with open(cfg, 'w', encoding='utf-8') as fh:
        fh.write(text)


class TestVenvHealthyProbe(unittest.TestCase):
    """The probe's decision matrix — every case is a real venv fixture."""

    def test_accepts_venv_that_can_see_and_import_every_distribution(self):
        with tempfile.TemporaryDirectory() as tmpd:
            venv = make_venv(tmpd)
            add_fake_dist(venv, 'flaskish', 'flaskish')
            req = write_requirements(tmpd, 'flaskish>=1.0,<2\n')
            proc = lib(dry_stubs(tmpd), 'venv_healthy', venv, req)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, '')

    def test_rejects_orphaned_venv_after_interpreter_swap(self):
        # THE regression case: interpreter present and runnable, packages
        # invisible. The old `[ ! -d ]` guard passed this venv straight
        # through and the service crash-looped.
        with tempfile.TemporaryDirectory() as tmpd:
            venv = make_venv(tmpd)
            add_fake_dist(venv, 'flaskish', 'flaskish')
            req = write_requirements(tmpd, 'flaskish>=1.0,<2\n')
            self.assertEqual(lib(dry_stubs(tmpd), 'venv_healthy',
                                 venv, req).returncode, 0)
            orphan(venv)
            proc = lib(dry_stubs(tmpd), 'venv_healthy', venv, req)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('flaskish', proc.stderr)

    def test_rejects_dangling_interpreter(self):
        # bin/python pointing at an interpreter the image removed (the
        # pinned-python variant of the same orphaning) must fail too.
        with tempfile.TemporaryDirectory() as tmpd:
            venv = make_venv(tmpd)
            add_fake_dist(venv, 'flaskish', 'flaskish')
            py = os.path.join(venv, 'bin', 'python')
            os.remove(py)
            os.symlink(os.path.join(tmpd, 'gone-python3.14'), py)
            req = write_requirements(tmpd, 'flaskish\n')
            proc = lib(dry_stubs(tmpd), 'venv_healthy', venv, req)
            self.assertEqual(proc.returncode, 1)
            self.assertIn('missing or not executable', proc.stderr)

    def test_rejects_import_failure_with_metadata_present(self):
        # Distribution visible but unimportable (broken install) — the
        # import smoke is not redundant with the metadata lookup.
        with tempfile.TemporaryDirectory() as tmpd:
            venv = make_venv(tmpd)
            add_fake_dist(venv, 'brokenpkg', 'brokenpkg')
            os.remove(os.path.join(site_packages(venv), 'brokenpkg.py'))
            req = write_requirements(tmpd, 'brokenpkg==1.0\n')
            proc = lib(dry_stubs(tmpd), 'venv_healthy', venv, req)
            self.assertEqual(proc.returncode, 2)
            self.assertIn('import failed', proc.stderr)

    def test_rejects_missing_requirements_file(self):
        with tempfile.TemporaryDirectory() as tmpd:
            venv = make_venv(tmpd)
            proc = lib(dry_stubs(tmpd), 'venv_healthy',
                       venv, os.path.join(tmpd, 'nope.txt'))
            self.assertEqual(proc.returncode, 1)

    def test_rejects_requirements_without_distribution_names(self):
        # Comment-only file: nothing to prove -> refuse to trust it.
        with tempfile.TemporaryDirectory() as tmpd:
            venv = make_venv(tmpd)
            req = write_requirements(tmpd, '# just comments\n\n')
            proc = lib(dry_stubs(tmpd), 'venv_healthy', venv, req)
            self.assertEqual(proc.returncode, 1)
            self.assertIn('no distribution names', proc.stderr)

    def test_names_derived_from_requirements_formatting(self):
        # Version specifiers, extras, comments and option lines all parse
        # out to bare distribution names; the smoke import maps the dist
        # name up to the first '-' (argon2-cffi -> argon2 style).
        with tempfile.TemporaryDirectory() as tmpd:
            venv = make_venv(tmpd)
            add_fake_dist(venv, 'flaskish', 'flaskish')
            add_fake_dist(venv, 'dash-pkg', 'dash')
            req = write_requirements(
                tmpd, '# stack\nflaskish>=1.0,<2\ndash-pkg[ssh]>=1\n'
                      '-e ./not-a-dist\n')
            proc = lib(dry_stubs(tmpd), 'venv_healthy', venv, req)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_probe_body_has_no_hardcoded_package_list(self):
        # The probe is built from requirements.txt at call time; pin that
        # the venv_healthy() body itself never names a stack package.
        body = re.search(r'venv_healthy\(\) \{.*?\n\}', read('venv_lib.sh'),
                         re.DOTALL).group(0)
        for pkg in ('flask', 'cheroot', 'waitress', 'argon2'):
            self.assertNotIn(pkg, body.lower(),
                             'probe must derive names from requirements.txt')

    def test_probe_writes_no_bytecode(self):
        # The probe may run as root against the service user's venv; -B
        # must keep the tree byte-untouched (no root-owned __pycache__).
        with tempfile.TemporaryDirectory() as tmpd:
            venv = make_venv(tmpd)
            add_fake_dist(venv, 'flaskish', 'flaskish')
            req = write_requirements(tmpd, 'flaskish\n')
            self.assertEqual(lib(dry_stubs(tmpd), 'venv_healthy',
                                 venv, req).returncode, 0)
            self.assertFalse(os.path.isdir(os.path.join(site_packages(venv),
                                                        '__pycache__')))


class TestEnsureVenvDryRun(unittest.TestCase):
    """Step 2/8's decision (scripted echo stubs): announcements and
    command plans, without executing anything."""

    def test_orphaned_venv_announces_rebuild_and_recreates(self):
        with tempfile.TemporaryDirectory() as tmpd:
            venv = make_venv(tmpd)
            add_fake_dist(venv, 'flaskish', 'flaskish')
            req = write_requirements(tmpd, 'flaskish\n')
            orphan(venv)
            proc = lib(dry_stubs(tmpd), 'ensure_venv', venv, req)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('-> rebuilding', proc.stdout)
            self.assertIn('[dry-run] rm -rf ' + venv, proc.stdout)
            self.assertIn('python3 -m venv ' + venv, proc.stdout)
            # dry-run: nothing was actually deleted.
            self.assertTrue(os.path.isdir(os.path.join(venv, 'lib',
                                                       'python9.9')))

    def test_healthy_venv_is_kept_untouched(self):
        with tempfile.TemporaryDirectory() as tmpd:
            venv = make_venv(tmpd)
            add_fake_dist(venv, 'flaskish', 'flaskish')
            req = write_requirements(tmpd, 'flaskish\n')
            marker = os.path.join(site_packages(venv), 'flaskish.py')
            proc = lib(dry_stubs(tmpd), 'ensure_venv', venv, req)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn('rebuilding', proc.stdout)
            self.assertNotIn('rm -rf', proc.stdout)
            self.assertTrue(os.path.isfile(marker))

    def test_fresh_missing_venv_creates_quietly(self):
        # First install stays byte-comparable: no probe noise, no rebuild
        # notice — just the create line (the old `[ ! -d ]` fresh path).
        with tempfile.TemporaryDirectory() as tmpd:
            req = write_requirements(tmpd, 'flaskish\n')
            proc = lib(dry_stubs(tmpd), 'ensure_venv',
                       os.path.join(tmpd, 'venv'), req)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn('rebuilding', proc.stdout)
            self.assertNotIn('venv_healthy', proc.stderr)
            self.assertIn('python3 -m venv', proc.stdout)

    def test_homeless_pinning_and_cache_env_survive_the_move(self):
        # The create must still go through as_user with the pinned HOME
        # and cache dir (tests/test_service_user.py TestHomelessVenv).
        with tempfile.TemporaryDirectory() as tmpd:
            proc = lib(dry_stubs(tmpd), 'ensure_venv',
                       os.path.join(tmpd, 'venv'), os.path.join(tmpd, 'r'))
            self.assertIn('[dry-run] sudo -u pcsvc env'
                          f' HOME={tmpd} XDG_CACHE_HOME={tmpd}/.cache'
                          f' python3 -m venv {tmpd}/venv', proc.stdout)


class TestEnsureVenvHealsForReal(unittest.TestCase):
    """run()/as_user() stubs that actually execute, inside the temp dir."""

    def test_orphaned_venv_is_recreated_and_importable_after_repair(self):
        with tempfile.TemporaryDirectory() as tmpd:
            venv = make_venv(tmpd)
            add_fake_dist(venv, 'flaskish', 'flaskish')
            req = write_requirements(tmpd, 'flaskish\n')
            orphan(venv)
            proc = lib(real_stubs(tmpd), 'ensure_venv', venv, req)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # Rebuilt: interpreter back, orphaned tree gone.
            self.assertTrue(os.path.exists(os.path.join(venv, 'bin', 'python')))
            self.assertFalse(os.path.isdir(os.path.join(venv, 'lib',
                                                        'python9.9')))
            # The heal alone cannot satisfy the requirement (that is the
            # pip step's job); emulate pip, then the panel would start.
            add_fake_dist(venv, 'flaskish', 'flaskish')
            proc = lib(real_stubs(tmpd), 'venv_healthy', venv, req)
            self.assertEqual(proc.returncode, 0, proc.stderr)


class TestInstallerWiring(unittest.TestCase):
    """Text pins: install.sh must actually use the healed path, and the
    old presence-only guards must be gone wherever they trusted a venv."""

    def test_installer_preflights_and_sources_venv_lib(self):
        script = read('install.sh')
        self.assertIn('Missing $SOURCE_DIR/venv_lib.sh', script)
        self.assertIn('. "$SOURCE_DIR/venv_lib.sh"', script)

    def test_step_two_probes_before_trusting_the_venv(self):
        script = read('install.sh')
        start = script.index('== 2/8 venv')
        end = script.index('== 2b/8', start)
        section = script[start:end]
        self.assertIn('ensure_venv "$INSTALL_DIR/.venv"'
                      ' "$INSTALL_DIR/requirements.txt"', section)
        # Repair step unchanged: pip still runs inline right after.
        self.assertIn('"$INSTALL_DIR/.venv/bin/pip" install -r'
                      ' "$INSTALL_DIR/requirements.txt"', section)

    def test_presence_checks_no_longer_sole_guards(self):
        # Regression pins: neither the directory check (step 2/8) nor the
        # bare -x check (recovery CLI) may stand anywhere again.
        script = read('install.sh')
        self.assertNotIn('[ ! -d "$INSTALL_DIR/.venv" ]', script)
        self.assertNotIn('[ ! -x "${INSTALL_DIR}/.venv/bin/python" ]', script)

    def test_recovery_cli_uses_the_probe(self):
        script = read('install.sh')
        start = script.index('== 5b/8 recovery CLI')
        end = script.index('== 5c/8', start)
        section = script[start:end]
        self.assertIn('elif ! venv_healthy "${INSTALL_DIR}/.venv"'
                      ' "${INSTALL_DIR}/requirements.txt"; then', section)

    def test_lib_documents_incident_and_no_pin_choice(self):
        lib_text = read('venv_lib.sh')
        self.assertIn('2026-11-14', lib_text)
        self.assertIn('crash-looped', lib_text)
        # Why not pin python3.14: the old interpreter is removed at reboot.
        self.assertIn('python3.14', lib_text)
        self.assertIn('Self-heal is the correct pattern', lib_text)


if __name__ == '__main__':
    unittest.main()
