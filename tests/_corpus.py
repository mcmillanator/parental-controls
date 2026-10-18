"""Resolve implementation source by *home module*, not by filename.

Four test modules read the runtime modules as source rather than importing
them (`test_deployment`, `test_budget_locked`, `test_qa_time_tracking`,
`test_ui_assets`). Before the app/budgets split they could hardcode
``read('app.py')``; once a function lives in its own concern module that
pins the test to a filename it no longer cares about.

These helpers answer "where does this live?" instead, so a test can follow a
move without changing a single assertion. See RESTRUCTURE_PLAN.md §1.2 / §7
risk R4.

Stdlib only (`ast` + `pathlib`), no Flask, no network.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Thin WSGI entrypoints hold no render logic and no helpers; they are not
# part of the runtime corpus a selector/argv scan should read.
NOT_RUNTIME = frozenset({'wsgi.py', 'wsgi_agent.py'})


def runtime_modules():
    """Top-level runtime module filenames, sorted (``'app.py'``, ...).

    Same set `tools/check_structure.py` measures: the installed modules
    minus the two thin entrypoints, and minus `tools/` (measurement code,
    not shipped behaviour).
    """
    return sorted(p.name for p in ROOT.glob('*.py')
                  if p.name not in NOT_RUNTIME)


def module_path(name):
    """Path of a runtime module by file name or by stem (``'app'``)."""
    if not name.endswith('.py'):
        name += '.py'
    return ROOT / name


def read_source(name):
    """Text of one runtime module by file name or by stem."""
    return module_path(name).read_text(encoding='utf-8')


def runtime_source():
    """Every runtime module's text, joined — the moved-code corpus.

    A superset of what any single module held before the split, so a scan
    for render markup, argv shapes or selectors keeps resolving wherever
    the code now lives.
    """
    return '\n'.join(read_source(name) for name in runtime_modules())


def parse_module(name):
    """(filename, source, ast.Module) for one runtime module."""
    src = read_source(name)
    return module_path(name).name, src, ast.parse(src, filename=name)


def find_function(name, prefer=()):
    """Locate `def <name>` in whichever runtime module defines it.

    Returns (filename, source, node). `prefer` is a filename (or iterable
    of filenames) searched first — needed because `agent.py` deliberately
    MIRRORS a dozen `app.py` helpers ("mirroring `app.run_lock_command`"),
    so a bare search would happily resolve `lock_user_sessions` to the
    remote agent instead of the panel. Raises LookupError naming what was
    searched when the definition is gone: a loud failure beats a test that
    silently greps an empty string.
    """
    order = [prefer] if isinstance(prefer, str) else list(prefer)
    order += [n for n in runtime_modules() if n not in order]
    for filename in order:
        mod_name, src, tree = parse_module(filename)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == name:
                return mod_name, src, node
    raise LookupError(f'no runtime module defines {name}() (searched '
                      f'{", ".join(order)})')


def function_source(name, prefer=()):
    """Exact source text of one function, wherever it is defined."""
    filename, src, node = find_function(name, prefer)
    text = ast.get_source_segment(src, node)
    if text is None:  # pragma: no cover - ast always recovers a segment
        lines = src.splitlines()
        text = '\n'.join(lines[node.lineno - 1:node.end_lineno])
    return text


def function_body_lines(name, prefer=(), terminators=('def ', '# ---')):
    """The lines *after* `def <name>` up to the next top-level statement.

    The repo's other static-scan style (test_qa_time_tracking slices the
    body textually). Same shape, home module resolved for you.
    """
    filename, _src, node = find_function(name, prefer)
    lines = read_source(filename).splitlines()
    out = []
    for line in lines[node.lineno:]:
        if line.startswith(terminators):
            break
        out.append(line)
    return out