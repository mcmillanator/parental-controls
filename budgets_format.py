"""Canonical duration display: parse a `ps` ETIME, render whole minutes.

`etime_to_minutes` parses a raw ps ETIME value into whole minutes,
`format_minutes`/`format_hm` render whole-minute durations the way parents
read them, and `group_by_comm` aggregates process dicts per comm with them.
Pure and stdlib-only — no store, no cgroup, no privileged path.

Moved verbatim out of budgets.py by RESTRUCTURE_PLAN.md slice 1; the facade
re-exports every name here by name, so `budgets.format_hm` keeps working.
"""

def etime_to_minutes(etime):
    """Parse a raw ps ETIME value into whole minutes (floored).

    Handles 'HH:MM:SS', 'MM:SS' (sub-hour ps output) and 'D-HH:MM:SS'
    forms. Returns None when the value cannot be parsed. Whole minutes
    only (seconds never surface) — matches the display convention.
    """
    try:
        days = 0
        rest = (etime or '').strip()
        if not rest:
            return None
        if '-' in rest:
            days_part, rest = rest.split('-', 1)
            days = int(days_part)
        fields = [int(f) for f in rest.split(':')]
        if len(fields) == 3:
            hours, minutes, seconds = fields
        elif len(fields) == 2:
            hours, minutes, seconds = 0, fields[0], fields[1]
        else:
            return None
        return max(0, days * 1440 + hours * 60 + minutes + seconds // 60)
    except (ValueError, AttributeError):
        return None


def format_minutes(total_minutes):
    """Format whole minutes as '3d 2h 5m' / '2h 5m' / '5m' / '0m'.

    Mirrors the shape of app.format_etime but takes minutes, so group
    aggregates never string-compare formatted times.
    """
    total = int(total_minutes)
    days, rem = divmod(total, 1440)
    hours, minutes = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f'{days}d')
    if hours:
        parts.append(f'{hours}h')
    if minutes or not parts:
        parts.append(f'{minutes}m')
    return ' '.join(parts)


def format_hm(minutes):
    """Format a whole-minute duration as '1h 30m' / '45m' / '2h' / '0m'.

    The canonical user-facing duration formatter (budgets, allowances,
    bonus time): parents set and read these in hours+minutes, never
    converting units themselves. Unlike ``format_minutes`` above — which
    renders elapsed process age and adds a ``d`` component for runs past
    a day — budget-scale durations carry no days part.

    Pure and stdlib-only. ``None``, a non-number, or a negative value
    returns ``''`` so each call site decides its own empty placeholder
    (``'—'`` where a missing limit means "none set").
    """
    try:
        total = int(minutes)
    except (TypeError, ValueError):
        return ''
    if total < 0:
        return ''
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f'{hours}h {mins}m'
    if hours:
        return f'{hours}h'
    return f'{mins}m'


def group_by_comm(procs):
    """Group proc dicts by comm into group dicts.

    Input: [proc dict] with fields per list_user_processes (pid, comm,
    etime, ...) plus a 'category' when known (group_by_category sets it
    for visible procs; app.py annotates hidden procs before grouping).

    Output (non-empty groups only, sorted by comm for determinism):
      {comm, display_name, count, members (sorted by pid asc),
       max_minutes, max_elapsed, categories, uniform_category}

    display_name is the first (lowest-PID) member's annotated
    display_name, falling back to the comm when members carry none —
    so group rows render a friendly app name where one is known.

    max_minutes is the max over members of etime_to_minutes(etime) —
    computed from raw minutes, never by string-comparing formatted time.
    max_elapsed is that max formatted once (whole minutes only); it is
    '' when no member's etime parses.
    """
    buckets = {}
    for proc in procs:
        comm = proc.get('comm') or ''
        buckets.setdefault(comm, []).append(proc)
    out = []
    for comm in sorted(buckets):
        members = sorted(buckets[comm], key=lambda m: m.get('pid', 0))
        minutes = [m for m in
                   (etime_to_minutes(mem.get('etime')) for mem in members)
                   if m is not None]
        max_minutes = max(minutes) if minutes else None
        cats = sorted({m.get('category') for m in members
                       if m.get('category')})
        out.append({
            'comm': comm,
            'display_name': members[0].get('display_name') or comm,
            'count': len(members),
            'members': members,
            'max_minutes': max_minutes,
            'max_elapsed': (format_minutes(max_minutes)
                            if max_minutes is not None else ''),
            'categories': cats,
            'uniform_category': len(cats) <= 1,
        })
    return out
