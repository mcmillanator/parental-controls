# Flowstep design exports — index

Source: Flowstep exports (React/JSX + one clipboard JSON), to be translated into
this project's Flask + Jinja + vanilla-JS + vendored-Bootstrap design system.

Branch: `feature/design-translation` (NOT to be merged until the owner reviews
and approves it after checking it out).

**All six screens received.** `SPEC.md` in this folder is the authoritative,
self-contained translation spec (structure, every label/text, states and
annotations for all six screens). Work from `SPEC.md`.

| # | Screen | Nav |
|---|--------|-----|
| 1 | Overview / Home (account cards, offline banner, confirm-lock) | Home |
| 2 | Alex time rules (override, bedtime schedule, bonus, countdown, clear, states) | Time |
| 3 | Time budgets (app budgets table→cards, weekly windows, h/m input, states) | Time |
| 4 | Processes (grouped rows, per-PID detail, override/visibility, states) | Activity |
| 5 | Admin console (audit log + admin accounts + remove-confirm dialog + specimens) | Admin |
| 6 | Settings (notifications, local access, devices, a11y notes) | Settings |

## Translation notes (for the implementing agent)

- **Stack mapping:** the exports use `@/components/ui/*` (shadcn/ui), Tailwind
  utility classes and `lucide-react` icons. This project has **no React, no
  Tailwind, no build step** — translate to Jinja templates + the existing
  `static/css/app.css` token/component system + inline SVG icons. Map tokens
  (`bg-card`, `text-foreground`, `bg-primary`, `bg-destructive`, `bg-accent`,
  `bg-secondary`, `bg-muted`, `bg-sidebar`, `border`, `ring`, `muted-foreground`,
  `primary-foreground`, `destructive`, `sidebar`) onto the app's `--pc-*` tokens.
- **Information architecture:** the exports introduce a 5-section shell
  (Overview, Time rules, Activity, Admin, Settings) with a **desktop sidebar**
  and a **phone bottom tab bar**. The current app is Home + Admin with
  Time/Processes inside the user page. Adopting the new IA is part of this work.
- **Must preserve:** every id/class/attr/text pinned by `tests/test_katalon_objects.py`,
  `tests/test_ui_*.py`, `tests/test_process_grouping.py`, `tests/test_proc_usage.py`,
  `tests/test_overrides.py`, `tests/test_durations.py`, `tests/test_time12h.py`
  (see `UI_UX_PLAN.md` §7.4). Update tests/Katalon only where a selector genuinely
  changes, and list each change.
- **Already-shipped behaviours to keep:** 12-hour clock display/inputs, h/m
  duration entry + display, per-app override via the shared dialog, freeze/thaw,
  budget "used since reset" in the Processes table, mobile-first table→card,
  dark+light themes, accessible dialogs.
- **No external assets** (Bootstrap is vendored locally).
