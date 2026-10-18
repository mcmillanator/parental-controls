"""Reading `ps`: process snapshots and per-process CPU deltas.

`parse_cpu_seconds` parses a ps TIME column, `read_cmdline` enriches a pid
from /proc, `list_user_processes` takes one read-only snapshot for a user and
`compute_deltas` turns consecutive snapshots into CPU-time deltas. No state,
no cgroup, no privileged path.

Moved verbatim out of budgets.py by RESTRUCTURE_PLAN.md slice 2. The
`subprocess.run` call resolves `budgets.subprocess` at call time, so
`mock.patch.object(budgets.subprocess, 'run')` (the guard between a unit test
and a real shell-out) still reaches it — see the cross-seam convention in
budgets_guards.py.
"""

# ---------------------------------------------------------------------------
# Process listing (read-only ps + /proc cmdline enrichment)
# ---------------------------------------------------------------------------

def parse_cpu_seconds(value):
    """Parse ps TIME ([[dd-]hh:]mm:ss) into float seconds. None if invalid."""
    try:
        rest = value.strip()
        days = 0
        if '-' in rest:
            days_part, rest = rest.split('-', 1)
            days = int(days_part)
        fields = [int(f) for f in rest.split(':')]
        if len(fields) == 3:
            h, m, s = fields
        elif len(fields) == 2:
            h, m, s = 0, fields[0], fields[1]
        else:
            return None
        return float(days * 86400 + h * 3600 + m * 60 + s)
    except (ValueError, AttributeError):
        return None


def read_cmdline(pid):
    """Full command line from /proc (readable cross-user); '' on failure."""
    try:
        with open(f'/proc/{int(pid)}/cmdline', 'rb') as f:
            raw = f.read()
        parts = [p.decode('utf-8', 'replace') for p in raw.split(b'\0') if p]
        return ' '.join(parts)
    except (OSError, ValueError):
        return ''


def list_user_processes(username):
    """Read-only snapshot: [{pid, comm, etime, cpu_seconds, args, cmdline}]."""
    procs = []
    import budgets
    try:
        result = budgets.subprocess.run(
            ['ps', '-u', username, '-o', 'pid,comm,etime,time,args',
             '--cols', '1000'],
            capture_output=True, text=True, timeout=10
        )
    except Exception:
        return procs
    lines = result.stdout.strip().split('\n') if result.stdout.strip() else []
    for line in lines[1:]:  # skip header
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        cpu = parse_cpu_seconds(parts[3])
        args = parts[4] if len(parts) > 4 else ''
        cmdline = read_cmdline(pid) or args
        procs.append({
            'pid': pid,
            'comm': parts[1],
            'etime': parts[2],
            'cpu_seconds': cpu if cpu is not None else 0.0,
            'args': args,
            'cmdline': cmdline,
        })
    return procs


def compute_deltas(last_cpu, procs):
    """CPU-time deltas since the previous tick.

    last_cpu: {pid_str: {'comm': str, 'cpu': float}} from the prior tick.
    Returns (deltas, new_last_cpu) where deltas maps pid -> delta seconds
    (>= 0). First sightings and PID-reuse (comm changed) yield delta 0 —
    we only bill CPU we actually observed accumulate.
    """
    deltas = {}
    new_last = {}
    for proc in procs:
        pid = str(proc['pid'])
        cur = float(proc.get('cpu_seconds') or 0.0)
        prev = last_cpu.get(pid)
        if prev is not None and prev.get('comm') == proc.get('comm'):
            delta = max(0.0, cur - float(prev.get('cpu', 0.0)))
        else:
            delta = 0.0
        deltas[proc['pid']] = delta
        new_last[pid] = {'comm': proc.get('comm'), 'cpu': cur}
    return deltas, new_last
