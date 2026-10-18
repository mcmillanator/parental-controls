#!/usr/bin/env python3
"""Emit RESTRUCTURE_IMPORTS.md — the import-surface contract for the split.

stdlib only (ast + re + pathlib). Run from the worktree root:

    .venv/bin/python tools/import_graph.py > RESTRUCTURE_IMPORTS.md

Covers:
  * every module -> every name it imports from each other module
  * the full `app` / `budgets` facade surface (the contract)
  * every `unittest.mock.patch` / `patch.object` target naming app/budgets
  * every REAL cross-module attribute read (AST, so docstrings/comments and
    shadowed locals are excluded)
  * function-level (deferred) imports — the cycle breaker, must not grow
  * `assert` statements (python -O safety) and the `__main__` entrypoint
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROOT_PY = sorted(p for p in ROOT.glob('*.py') if not p.name.startswith('.'))
TEST_PY = sorted((ROOT / 'tests').glob('*.py'))
LOCAL_MODULES = sorted(p.stem for p in ROOT_PY)
CORE = ('app', 'budgets')


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def nlines(node: ast.AST) -> int:
    ends = [getattr(n, 'end_lineno', node.lineno)
            for n in ast.walk(node) if hasattr(n, 'lineno')]
    return (max(ends) - node.lineno + 1) if ends else 1


def module_imports(tree: ast.Module) -> dict[str, set[str]]:
    """module -> set of names imported FROM it (plain `import x` -> empty set)."""
    out: dict[str, set[str]] = {}

    def note(mod: str, names: set[str]) -> None:
        out.setdefault(mod, set()).update(names)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                note('.' * node.level, set())
                continue
            mod = (node.module or '').split('.')[0]
            if mod in LOCAL_MODULES:
                note(mod, {a.name for a in node.names})
        elif isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split('.')[0]
                if mod in LOCAL_MODULES:
                    note(mod, set())
    return out


def deferred_imports(tree: ast.Module) -> list[tuple[int, str, list[str]]]:
    """Imports that live INSIDE a function body — the cycle breaker."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.ImportFrom) and not sub.level:
                mod = (sub.module or '').split('.')[0]
                if mod in LOCAL_MODULES:
                    found.append((sub.lineno, mod, [a.name for a in sub.names]))
            elif isinstance(sub, ast.Import):
                for alias in sub.names:
                    mod = alias.name.split('.')[0]
                    if mod in LOCAL_MODULES:
                        found.append((sub.lineno, mod, []))
    return sorted(found)


def shadowed_names(tree: ast.Module) -> set[str]:
    """Names bound as function params / locals anywhere in the module."""
    bound: set[str] = set()
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        a = fn.args
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs,
                    *(a.vararg and [a.vararg] or []),
                    *(a.kwarg and [a.kwarg] or [])]:
            bound.add(arg.arg)
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                bound.add(n.id)
    return bound


def bound_in(fn: ast.AST) -> set[str]:
    """Names bound as a parameter or assigned anywhere inside `fn`."""
    bound: set[str] = set()
    a = fn.args  # type: ignore[attr-defined]
    for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs]:
        bound.add(arg.arg)
    if a.vararg:
        bound.add(a.vararg.arg)
    if a.kwarg:
        bound.add(a.kwarg.arg)
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for al in n.names:
                bound.add(al.asname or al.name.split('.')[0])
    return bound


def cross_attrs(tree: ast.Module, path: Path) -> dict[str, set[str]]:
    """REAL `mod.attr` reads where mod is a local module (docstrings excluded).

    A module never counts as reading *itself* (that is the `app` Flask
    instance case inside app.py). A base name shadowed by a parameter or an
    assignment in the enclosing function is skipped —
    `budgets.group_usage_minutes()` binds a local `app` to a dict, and that
    dict lookup is not a module reference.
    """
    me = path.stem
    used: dict[str, set[str]] = {}
    scopes: list[tuple[int, int, frozenset[str]]] = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        end = max((getattr(d, 'end_lineno', fn.lineno) for d in ast.walk(fn)
                   if hasattr(d, 'lineno')), default=fn.lineno)
        scopes.append((fn.lineno, end, frozenset(bound_in(fn))))

    def shadowed_at(lineno: int, base: str) -> bool:
        """True when `base` is a param/local of the innermost function at `lineno`."""
        best_span: int | None = None
        best_names: frozenset[str] = frozenset()
        for start, end, names in scopes:
            if start <= lineno <= end:
                span = end - start
                if best_span is None or span < best_span:
                    best_span, best_names = span, names
        return base in best_names

    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            base = n.value.id
            if base in LOCAL_MODULES and base != me and not shadowed_at(n.lineno, base):
                used.setdefault(base, set()).add(n.attr)
    return used


PATCH_STR_RE = re.compile(
    r"""patch(?:\.object)?\(\s*['"](app|budgets)\.([A-Za-z_]\w*)['"]""")
PATCH_OBJ_ATTR_RE = re.compile(
    r"""patch\.object\(\s*(app|budgets)\s*,\s*['"]([A-Za-z_]\w*)['"]""")
PATCH_OBJ_SUB_RE = re.compile(
    r"""patch\.object\(\s*(app|budgets)\.([A-Za-z_]\w*)""")
# `patch.object(app.budgets, "x")` reaches THROUGH the facade to the real
# budgets module object, so it is really a `budgets.x` patch. Same for
# `patch.object(budgets.pwd, ...)`.
PATCH_OBJ_THROUGH_RE = re.compile(
    r"""patch\.object\(\s*app\.budgets\s*,\s*['"]([A-Za-z_]\w*)['"]""")


def main() -> None:
    L: list[str] = []
    add = L.append
    roots = {p.stem: p.read_text() for p in ROOT_PY}
    trees = {p.stem: ast.parse(p.read_text(), filename=str(p)) for p in ROOT_PY}

    add('# RESTRUCTURE_IMPORTS.md — machine-generated import surface')
    add('')
    add('Generated by `tools/import_graph.py` (stdlib `ast`, no deps).')
    add('Regenerate after **every** migration slice:')
    add('')
    add('```sh')
    add('.venv/bin/python tools/import_graph.py > RESTRUCTURE_IMPORTS.md')
    add('git diff --exit-code RESTRUCTURE_IMPORTS.md')
    add('```')
    add('')
    add('An **empty diff** is the phase-1 guard: the split may relocate code,')
    add('never the set of names other modules and tests bind to. Sections 2-5')
    add('are what the execution phase must preserve byte-for-byte.')
    add('')

    # ---------------------------------------------------------------- 1
    add('## 1. Runtime modules — what each imports from what')
    add('')
    add('| module | lines | local imports |')
    add('|---|---:|---|')
    for mod in LOCAL_MODULES:
        deps = module_imports(trees[mod])
        parts = []
        for dep in sorted(deps):
            names = sorted(deps[dep])
            suffix = ' ({})'.format(', '.join(names)) if names else ' _(module only)_'
            parts.append(f'`{dep}`{suffix}')
        add(f'| `{mod}.py` | {len(roots[mod].splitlines())} | '
            + ('<br>'.join(parts) if parts else '_(none)_') + ' |')
    add('')
    add('Flask-free stdlib-only modules (no third-party imports):')
    for mod in ('budgets', 'auth', 'store', 'audit', 'remote', 'session_agent',
                'focus_feed', 'device_policy', 'adminstore'):
        if mod in roots:
            add(f'- `{mod}.py`')
    add('')

    # ---------------------------------------------------------------- 2
    add('## 2. The `app` / `budgets` facade surface — the contract')
    add('')
    for core in CORE:
        consumers: dict[str, set[str]] = {}
        for mod, tree in trees.items():
            if mod == core:
                continue
            deps = module_imports(tree)
            if core in deps:
                consumers[mod] = deps[core]
        add(f'### 2.{CORE.index(core) + 1} Everything bound FROM `{core}`')
        add('')
        add(f'`{core}` is imported by **{len(consumers)} runtime module(s)**:')
        add('')
        add('| consumer | names |')
        add('|---|---|')
        for mod in sorted(consumers):
            names = sorted(consumers[mod])
            add(f'| `{mod}.py` | '
                + (', '.join(f'`{n}`' for n in names) if names else '_(module only)_')
                + ' |')
        add('')

    # ---------------------------------------------------------------- 3
    add('## 3. Tests — the 40-module blast radius')
    add('')
    hits: dict[str, dict] = {}
    for path in TEST_PY:
        text = path.read_text()
        tree = ast.parse(text, filename=str(path))
        surface: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in CORE:
                surface.setdefault(node.module, set()).update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in CORE:
                        surface.setdefault(alias.name, set())
        if surface:
            hits[path.name] = {m: sorted(v) for m, v in surface.items()}
    add(f'**{len(hits)} of {len(TEST_PY)} test modules import `app`/`budgets`; '
        f'{len(TEST_PY) - len(hits)} import neither.** Only a `from budgets import x`')
    add('line constrains the *definition site* of `x`; a plain `import app` only')
    add('constrains the module object to keep existing with that name.')
    add('')
    add('| test module | lines | surface |')
    add('|---|---:|---|')
    for name in sorted(hits):
        bits = []
        for mod in sorted(hits[name]):
            names = hits[name][mod]
            bits.append(f'`import {mod}`' if not names
                        else f'`from {mod} import ' + ', '.join(names) + '`')
        add(f'| `tests/{name}` | {len((ROOT / "tests" / name).read_text().splitlines())} | '
            + '<br>'.join(bits) + ' |')
    add('')

    # ---------------------------------------------------------------- 4
    add('## 4. `mock.patch` targets naming `app.` / `budgets.` (the big one)')
    add('')
    rows: set[tuple[str, str, str, str]] = set()
    sites = 0
    for path in TEST_PY:
        text = path.read_text()
        for rx in (PATCH_STR_RE, PATCH_OBJ_ATTR_RE, PATCH_OBJ_SUB_RE):
            sites += len(rx.findall(text))
        for m, a in PATCH_STR_RE.findall(text):
            rows.add((path.name, m, a, 'patch("mod.attr")'))
        for m, a in PATCH_OBJ_ATTR_RE.findall(text):
            rows.add((path.name, m, a, 'patch.object(mod, "attr")'))
        for m, a in PATCH_OBJ_SUB_RE.findall(text):
            rows.add((path.name, m, a, 'patch.object(mod.attr)'))
        for a in PATCH_OBJ_THROUGH_RE.findall(text):
            rows.add((path.name, 'budgets', a, 'patch.object(app.budgets, "attr")'))
    by_target: dict[tuple[str, str, str], set[str]] = {}
    for fname, mod, attr, _kind in rows:
        by_target.setdefault((mod, attr, _kind), set()).add(fname)
    add('| patch target | form | test modules |')
    add('|---|---|---|')
    for (mod, attr, kind), files in sorted(by_target.items()):
        add(f'| `{mod}.{attr}` | `{kind}` | {len(files)} — '
            + ', '.join(f'`{f}`' for f in sorted(files)) + ' |')
    add('')
    attr_targets = sorted({(m, a) for m, a, k in by_target
                           if k != 'patch.object(mod.attr)'})
    sub_targets = sorted({(m, a) for m, a, k in by_target
                          if k == 'patch.object(mod.attr)'})
    add(f'**{len(by_target)} distinct patch targets, {sites} patch sites, '
        f'{len({r[0] for r in rows})} of {len(TEST_PY)} test modules affected.**')
    add('')
    add(f'- **{len(attr_targets)} attribute targets** — `patch.object(app, "X")` '
        'and the')
    add('  equivalent `patch.object(app.budgets, "X")`, which reaches through '
        'the facade')
    add('  to the real `budgets` module. These break the moment `X` moves out '
        'of the')
    add('  module the caller lives in, unless the caller resolves `X` through '
        'the')
    add('  facade at call time.')
    add(f'- **{len(sub_targets)} submodule targets** (`patch.object(app.budgets)`, '
        '`patch.object(budgets.pwd)`) —')
    add('  these break if the moved code holds its own binding to the module.')
    add('')
    add('**Rule the split must satisfy:** every one of these targets must still be')
    add('*effective* from where the code now lives. That is measured, not assumed:')
    add('the phase-1 gate is `RESTRUCTURE_IMPORTS.md` showing no diff **and** the')
    add('full container suite matching the recorded baseline.')
    add('')

    # ---------------------------------------------------------------- 5
    add('## 5. Real cross-module attribute reads (AST; docstrings excluded)')
    add('')
    add('| file | reads |')
    add('|---|---|')
    total = 0
    for path in ROOT_PY + TEST_PY:
        tree = trees.get(path.stem) if path.parent == ROOT else ast.parse(
            path.read_text(), filename=str(path))
        used = cross_attrs(tree, path)
        if not used:
            continue
        total += 1
        cells = []
        for base in sorted(used):
            cells.append(f'`{base}.{{' + ', '.join(sorted(used[base])) + '}`')
        add(f'| `{path.relative_to(ROOT)}` | ' + '<br>'.join(cells) + ' |')
    add('')
    add(f'{total} file(s) reach into another local module by attribute. These are '
        'the')
    add('sites the facade-call convention governs; they are the reason a '
        '`from x')
    add('import y` at module import time is banned across a split seam.')
    add('')

    # ---------------------------------------------------------------- 6
    add('## 6. Cycle breaker — function-level (deferred) imports')
    add('')
    deferred = []
    for mod, tree in trees.items():
        for lineno, target, names in deferred_imports(tree):
            deferred.append((mod, lineno, target, names))
    if deferred:
        add('| module:line | imports | why it is deferred |')
        add('|---|---|---|')
        notes = {
            ('budgets', 'focus_feed'): '`focus_feed` imports `budgets` back — '
                                       'a deliberate mutual lazy import',
            ('focus_feed', 'budgets'): '`budgets` imports `focus_feed` back — '
                                       'a deliberate mutual lazy import',
        }
        for mod, lineno, target, names in deferred:
            label = ', '.join(names) if names else '(module)'
            add(f'| `{mod}.py:{lineno}` | `{target}` {label} | '
                + notes.get((mod, target), 'cycle avoidance / optional dep') + ' |')
    else:
        add('_None._')
    add('')
    add('A split must not add module-level edges between these pairs: the mutual')
    add('`budgets` <-> `focus_feed` import only resolves because **both** sides')
    add('defer. Keep them deferred, and keep them off each other\'s import path.')
    add('')

    # ---------------------------------------------------------------- 7
    add('## 7. Entry points, `__main__`, and `python -O`')
    add('')
    add('| property | value |')
    add('|---|---|')
    mains = [m for m, t in trees.items()
             if any(isinstance(n, ast.If) and ast.unparse(n.test).endswith("'__main__'")
                    for n in ast.walk(t))]
    add(f'| modules with an `if __name__ == "__main__"` block | '
        f'{", ".join(f"`{m}`" for m in sorted(mains)) or "_(none)_"} |')
    add(f'| `app.py` `__main__` starts | `threading.Thread(target=bedtime_loop)` '
        'then `app.run(...)` |')
    add('| production WSGI entrypoint | `waitress-serve wsgi:application` -> '
        '`from app import app` |')
    add('| enforcement entrypoint | `python enforce_bedtime_timer.py` -> '
        '`from app import enforce_bedtime, ...` |')
    asserts = {m: sum(1 for n in ast.walk(t) if isinstance(n, ast.Assert))
               for m, t in trees.items()}
    hot = {m: c for m, c in asserts.items() if c}
    add(f'| `assert` statements in runtime modules | '
        f'{sum(asserts.values())} ({", ".join(f"{m}:{c}" for m, c in hot.items()) or "none"}) |')
    add('| `python -O` safety | no `assert` backs production behaviour — an '
        'optimized run is behaviour-identical |')
    add('')

    # ---------------------------------------------------------------- 8
    add('## 8. The `app`-module vs `app`-Flask-instance hazard')
    add('')
    add('`app.py` line 30: `app = Flask(__name__)`. Inside `app.py` the bare name')
    add('`app` means the **Flask instance**; in every other module it means the')
    add('**module**. `budgets.py` compounds it: `group_usage_minutes()` binds a')
    add('local `app` to a dict at line 1219, so `app.get("used_minutes")` in that')
    add('file is a dict lookup, not a module reference.')
    add('')
    add('Consequences for the split (see RESTRUCTURE_PLAN.md §7 risk 4):')
    add('1. Concern modules must NOT do `from app import app` and also `import app` '
        'in the')
    add('   same file — pick `import app` + `app.route(...)` and one name only.')
    add('2. `budgets.py` keeps its module-level `import` set untouched; a new')
    add('   `budgets_*` module that needs a module must say `import budgets` and use')
    add('   `budgets.x` — never rebind the name `app`/`budgets` to anything else.')
    add('3. `app.py` must keep `app.py` (the filename) for `enforce_bedtime_timer`')
    add('   and `install.sh`, which both look for `app.py` next to themselves.')
    add('')

    # ---------------------------------------------------------------- 9
    add('## 9. Definition index of the two giants')
    add('')
    for mod in CORE:
        tree = trees[mod]
        names = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, ast.Assign):
                names += [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.append(node.target.id)
        pub = sorted(n for n in names if not n.startswith('_'))
        priv = sorted(n for n in names if n.startswith('_'))
        add(f'### `{mod}.py` — {len(pub)} public, {len(priv)} private top-level names')
        add('')
        for label, group in (('public', pub), ('private', priv)):
            if not group:
                continue
            add(f'**{label}**')
            add('')
            add('```')
            for i in range(0, len(group), 5):
                add('  '.join(f'{n:<30}' for n in group[i:i + 5]).rstrip())
            add('```')
            add('')

    print('\n'.join(L))


if __name__ == '__main__':
    main()