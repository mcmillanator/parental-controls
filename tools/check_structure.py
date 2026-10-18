#!/usr/bin/env python3
"""Definition-of-human-friendly checker — the measurement behind RESTRUCTURE_PLAN.md §8.

Promoted from `.scratch/check_structure.py` in slice 0 so the gate can run on
every future change:

    .venv/bin/python tools/check_structure.py          # report
    .venv/bin/python tools/check_structure.py --strict # exit 1 on violations
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_LINES = 500
RUNTIME = sorted(p for p in ROOT.glob('*.py')
                 if p.name not in {'wsgi.py', 'wsgi_agent.py'})
SKIP = {'install.sh'}
WILDCARD = re.compile(r'^\s*from\s+\S+\s+import\s+\*', re.M)


def module_docstring(path: Path) -> str | None:
    tree = ast.parse(path.read_text(), filename=str(path))
    return ast.get_docstring(tree)


def main() -> int:
    violations: list[str] = []
    rows: list[tuple[str, int, str, str]] = []

    for path in sorted(ROOT.rglob('*.py')):
        rel = str(path.relative_to(ROOT))
        if rel.startswith(('.venv', '.scratch', 'tests/', 'systemd/')):
            continue
        text = path.read_text()
        n = len(text.splitlines())
        doc = module_docstring(path)
        doc_ok = bool(doc and doc.strip().splitlines()
                      and len(doc.strip().splitlines()) >= 1)
        flags = []
        if n > MAX_LINES:
            flags.append(f'{n - MAX_LINES} over')
        if not doc_ok:
            flags.append('no module docstring')
        if WILDCARD.search(text):
            flags.append('wildcard import')
        rows.append((rel, n, 'ok' if not flags else ', '.join(flags),
                     'yes' if doc_ok else 'NO'))
        for f in flags:
            violations.append(f'{rel}: {f}')

    # Section-banner usage inside the big files (today's style).
    for rel in ('app.py', 'budgets.py'):
        p = ROOT / rel
        if not p.exists():
            continue
        banners = p.read_text().count('\n# ------')
        rows.append((f'{rel} (section banners)', banners, 'banner count', '-'))

    # Circular imports at module level (AST, local modules only).
    mods = {p.stem: p for p in ROOT.glob('*.py')}
    edges: dict[str, set[str]] = {}
    for stem, p in mods.items():
        tree = ast.parse(p.read_text(), filename=str(p))
        deps: set[str] = set()
        for node in tree.body:  # module level only
            if isinstance(node, ast.ImportFrom) and not node.level:
                m = (node.module or '').split('.')[0]
                if m in mods:
                    deps.add(m)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    m = a.name.split('.')[0]
                    if m in mods:
                        deps.add(m)
        edges[stem] = deps
    cycles: list[str] = []
    for a in sorted(edges):
        for b in sorted(edges[a]):
            if a in edges.get(b, ()):  # a -> b -> a
                cycles.append(f'{a} -> {b} -> {a}')
    for c in sorted(set(cycles)):
        violations.append(f'module-level import cycle: {c}')

    # Route modules must not import each other (after the split).
    views = sorted(m for m in mods if m.startswith('views_'))
    if views:
        for v in views:
            for other in views:
                if other != v and other in edges.get(v, ()):
                    violations.append(f'view module imports view module: {v} -> {other}')

    w1, w2, w3 = 12, 30, 88
    print(f'{"module":<34} {"lines":>6}  {"status":<28} docstring')
    print('-' * w1 * 1 + '-' * 2 + '-' * (w2 - 2) + '-' * (w3 - w2))
    for rel, n, status, doc in rows:
        print(f'{rel:<{w1}} {n:>{w2}}  {status:<{w3 - w2}} {doc}')

    print()
    print(f'graph: {len(mods)} runtime modules, '
          f'{sum(len(v) for v in edges.values())} module-level local edges')
    if cycles:
        print(f'CYCLES: {sorted(set(cycles))}')
    else:
        print('no module-level import cycles')

    # ---- summary -------------------------------------------------------
    big = [r for r in rows if r[1] > MAX_LINES and 'banner' not in r[0]]
    nodoc = [r for r in rows if r[3] == 'NO' and 'banner' not in r[0]]
    print()
    print(f'files over {MAX_LINES} lines: {len(big)} '
          f'({", ".join(f"{r[0]}:{r[1]}" for r in big) or "none"})')
    print(f'files without a module docstring: {len(nodoc)} '
          f'({", ".join(r[0] for r in nodoc) or "none"})')
    print(f'wildcard re-exports: '
          f'{sum(1 for p in mods.values() if WILDCARD.search(p.read_text()))}')
    print(f'total violations: {len(violations)}')

    if violations and '--strict' in sys.argv:
        for v in violations:
            print(f'VIOLATION: {v}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())