# Flowstep exports — consolidated translation spec (all 6 screens)

This is the authoritative, self-contained translation brief. Raw exports also
live beside this file (`screen-N-*.jsx` / `screen-5-admin.flowstep.json`).

Target stack: **Flask + Jinja + vanilla JS + vendored Bootstrap + `static/css/app.css`
tokens**. No React, no Tailwind, no build step. Map Tailwind/shadcn tokens
(`background foreground card card-foreground popover primary primary-foreground
secondary secondary-foreground muted muted-foreground accent accent-foreground
destructive destructive-foreground border input ring sidebar`) onto the app's
`--pc-*` tokens (see `static/css/app.css`).

## Shared shell (all screens)

- **Desktop (≥960px):** left **sidebar** (280px): brand "Parental Controls";
  a "Selected machine" card (Monitor icon, "Living room PC", `Link` icon +
  "http://parental.local", green dot + "Local / Online"); vertical nav:
  **Overview / Time rules / Activity / Admin / Settings** (active item =
  `bg-accent` + medium weight). Top header (h-14) right-aligned: user chip
  (`CircleUserRound` "Jamie" + `Wheel`), theme toggle (`Sun`), Help
  (`CircleHelp`).
- **Phone (<960px):** the sidebar collapses; primary navigation becomes a
  **bottom tab bar**. (Screens 1–2 show the 4-tab phone bar: Home/Time/
  Activity/Admin; the 5-section IA adds Settings.)
- Every page: `<h1>` + muted subtext, then content cards. Density collapses
  to single column below **960px**; status banners stay first.

## Screen 1 — Overview (Home)
- Header: "Parental Controls" + status dot "Local / Online".
- H1 "Overview", subtext "Parent dashboard".
- **Offline/sync banner**: `WifiOff` + "Last sync 2 min ago · Local connection
  healthy" + link "Retry"; annotation "Offline state: preserve last known status
  and disable destructive actions until fresh confirmation."
- **System summary** card: "Living room PC", "3 human accounts", "1 active
  session", pill "Online".
- **Accounts** (one card each; whole card navigates to the child's Time screen):
  - "Alex · child · locked until 7:30 AM" + pill **Locked**; actions
    **Unlock** (primary), **Lock screen**, **Emergency logout**.
  - "Sam · child · unlocked", "using 1h 42m today" + pill **Unlocked**;
    actions **Lock** (primary), **Lock screen**, **Emergency logout**.
  - "Jamie · wheel admin · always protected" + pill **Protected**; all three
    actions **disabled**; note "wheel admins can never be locked or removed."
- **Confirmation specimen** (highlighted card): "Selected account: Sam",
  "Confirmation required.", checkbox "Confirm lock action", full-width
  **Lock Sam** button.
- **Design thesis** card: "Status first, reversible actions up front,
  destructive actions separated and confirmed."
- **Empty state** card: "No human accounts found" / "Check the machine
  connection" + **Refresh**.

## Screen 2 — Alex time rules (per-child "Time")
- Back link; H1 "Alex time rules"; **child Tabs** (Alex / Sam).
- **Manual override active** card (primary-tinted): pill "Active",
  "28 min remaining · auto re-locks at 8:14 PM", full-width **Revoke override**.
- **Bedtime schedule** card: header + **enable Switch**; grid-2 **Start** /
  **End** (12-hour text inputs, "8:00 PM"/"7:30 AM"); **Days** row of 7 day
  buttons (Mon…Sun); checkbox "Apply to all days"; **Warning** select
  (5/15/30 minutes before); annotation "time uses 12-hour AM/PM and schedule
  edits are reversible"; full-width **Save schedule**.
- **Bonus time** card ("One-tap choices"): radio group **15m 30m 60m 90m 2h**;
  grid-2 **Custom hours** / **Custom minutes**; full-width **Grant bonus time**.
- **Live countdown** card (muted): big mono **00:28:14**, "Desktop warning
  sent", annotation "countdown respects reduced-motion preferences".
- **Clear schedule**: full-width destructive-outline button; hidden
  confirm card "Clear this schedule?" with **Confirm** (destructive) /
  **Cancel**.
- **State specimen** card (collapsed): Loading (skeleton bars), Error
  "Could not save schedule" + Retry, Empty "No bedtime schedule" + Add
  schedule, Disabled "Machine offline, controls unavailable."; annotation
  "keyboard order follows the visual order. Screen-reader labels are provided
  on time inputs."

## Screen 3 — Time budgets (the "Time" section, desktop)
- Sidebar IA active on **Time rules**. Page: eyebrow "Time rules",
  H1 "Time budgets", primary **Save all changes** (`Save` icon).
- Filter row: **User** select (Alex), **Date** select ("Today · Tue 14 May"),
  `RefreshCw` + "Last refreshed 4 min ago".
- **App budgets** card ("Daily limits and current usage for Alex."):
  - Desktop table: columns **Category | Used today | Daily limit | State |
    Action**; rows Games (1h 18m, 2h, "Paused at limit", `<progress>` bar),
    Browsers (42m, 1h, "Active"), Media (2h 05m, 2h, "Paused at limit"),
    Chat (18m, 1h, "Active"), Other (7m, "Unlimited", "Active"); each row has an
    **App override** link under the category and an Action button.
  - **Table→card pattern** (same data as labelled cards under 960px): name +
    "App override" link, state badge, `<dl>` "Used today" / "Daily limit",
    full-width action button. (annotation card: "On narrow widths each row
    becomes a labelled card; preserve order, labels, and actions.")
- **Weekly windows** card ("Recurring allowances for school and weekend
  schedules.") + **Add weekly window** (`Plus`): window rows "School nights
  Mon–Thu · 4:00 PM–8:00 PM · 90m allowance · Remove window", "Weekend
  Fri–Sun · 9:00 AM–9:00 PM · 3h allowance · Remove window"; hidden confirm
  "Remove this weekly window?" **Confirm remove** / **Cancel**.
- **Hours + minutes input** specimen card: Hours (0–23) + Minutes (0–59) with
  visible constraint hints.
- States row: "No app budgets yet" + Add category; skeleton card; error
  "Unable to load budgets" + Retry; "Last refreshed 4 min ago / Stale state";
  "Offline — edits unavailable" + disabled Save.
- **App override modal specimen**: title "App override", subtext
  "Steam override"; rows Steam→Unlimited, Firefox→45m allowance,
  YouTube→Unlimited; **Done** button.

## Screen 4 — Processes (the "Activity" section)
- H1 "Processes", eyebrow "Activity". Right: **Machine** select
  ("Living room PC"), **User** select ("All human accounts"), **Refresh now**.
- Banner: `Clock3` "Last updated 38 seconds ago" + "Refresh" link.
- Toolbar: **Tabs Show: Visible / Hidden / All**; right "Reduced motion" +
  Switch.
- **Process groups** card ("Duplicate processes are grouped by app."; note
  "TIME USED is accumulated foreground usage today."): table columns
  **App | Category | Time used today | State | Override**:
  - Steam (3 processes) ▸ expanded → detail row "PID 4102 · 31m, PID 4124 ·
    26m, PID 4160 · 21m"; Games; 1h 18m; "Paused"; Override button
    "Unlimited".
  - Firefox (2 processes); Browsers; 42m; "Active"; Override "45m".
  - spotify (1 process); Media; 58m; "Active"; Override "None".
  - Discord (4 processes); Chat; 18m; "Active"; Override "None".
- **Override** card: radio Default category budget / Unlimited / Custom
  allowance; custom allowance input "00h 45m"; **Save override**.
- **App visibility** card: **Hide app** / **Show app** (never kill).
- State cards row: Loading / Error "Cannot reach machine · Retry" / Empty
  "No visible apps" / Offline "Offline mode hides live controls."
- Footnote: "Out-of-time apps are paused, not terminated."

## Screen 5 — Admin console (the "Admin" section; desktop wireframe)
- H1 "Admin console", subtext "Changes are recorded locally with
  tamper-evident integrity checks", primary **Export audit log** (`Download`);
  toast specimen "Audit exported".
- Two panels (grid 1 col → 2 col ≥960px):
  - **Audit log** ("Recent administrative changes"): filters **Actor** (All /
    Jamie / Morgan), **Action** (All / Access / Schedule), **Date range**
    (Last 7 days / Last 30 days); table **When | Actor | Action | Target |
    Integrity** with rows:
    - "Today 7:42 PM | Jamie (wheel) | granted 30m bonus | Alex | Verified"
    - "Today 7:18 PM | Jamie (wheel) | froze Games | Alex | Verified"
    - "Yesterday 9:03 PM | Morgan | removed app-only admin | Casey | Verified"
    - "Yesterday 8:55 PM | Jamie (wheel) | changed bedtime schedule | Sam |
      Verified"
    Integrity = **Verified** chips; summary "4 of 4 entries verified"
    (`BadgeCheck`).
  - **Admin accounts** ("Access to this machine"): Jamie (wheel admin,
    "Protected / Cannot remove", pill **Protected**); Morgan (app-only admin,
    "Active / Remove"); Casey (app-only admin, "Disabled / Remove");
    full-width primary **Add app-only admin** (`UserPlus`).
- **Remove-confirm dialog specimen**: title "Remove app-only admin?",
  "Morgan will lose app-only admin access. This action is recorded and cannot
  be undone.", field "Type REMOVE to confirm" + input placeholder REMOVE,
  "Focus trapped · Escape closes", **Cancel** / **Remove admin** (destructive).
- Specimen cards: **Irreversible actions** ("never rely on color, require
  explicit confirmation, and log actor + timestamp."); **Theme specimen**
  (Dark selected / Light available); **Component callouts** (Primary button,
  Status chip, Toast "Audit exported", Dialog, Focus ring).

## Screen 6 — Settings
- H1 "Settings", subtext "Manage notifications, local access, and connected
  devices.", primary **Save settings** (`Save`).
- Status stack: "Last sync 4 min ago" (primary-tinted); muted banner
  "Offline — lock controls require a fresh connection" + disabled
  "Unavailable" (`LockKeyhole`); error banner (role=alert) "Could not reach
  local service · Retry" + **Retry**.
- **Notifications** fieldset: switch "Send pre-lock warnings to child's
  desktop" (+ hint); **Warning lead-time** select (5/15/30 minutes);
  "Warning copy preview" specimen "You have 15 minutes before bedtime";
  **Test notification** (`Send`).
- **Local access** fieldset: "localhost-first URL" read-only
  `http://parental.local`; switch "Optional HTTPS enabled" (+ hint); note
  "LAN access is available when the local service is reachable…";
  "Certificate status" → "Valid until 14 May 2026" (`BadgeCheck`).
- **Devices**: card "Living room PC / Fedora Atomic / Bazzite", online dot,
  "Last heartbeat 12 seconds ago"; dashed card "Other devices —
  Multi-device management is planned" + disabled **Add device** (`Plus`);
  empty card "No devices connected · Check parental.service." (`MonitorOff`).
- Annotation sections: "Responsive behavior annotation" (panels → single column
  at 960px, status banners first); "Accessibility notes" (semantic
  fieldsets/legends; labels never placeholder-only; keyboard order follows
  visual order; WCAG AA contrast; prefers-reduced-motion disables live
  transitions; all actions have text plus icon); "Offline-capable
  implementation note" (server-rendered HTML, minimal vanilla JS, no external
  fonts/icons/assets, safe on localhost).

## Cross-screen requirements

- Keep all already-shipped behaviours: 12-hour clock display + inputs, h/m
  duration entry + display, shared Override dialog, freeze/thaw, budget
  "used since reset" in Processes, table→card, dark+light themes, accessible
  dialogs, no external assets.
- Preserve every pinned selector (UI_UX_PLAN.md §7.4 + the `tests/test_ui_*.py`,
  `test_katalon_objects.py`, `test_process_grouping.py`, `test_proc_usage.py`,
  `test_overrides.py`, `test_durations.py`, `test_time12h.py` contracts).
- New IA (5 sections: Overview / Time rules / Activity / Admin / Settings) is
  part of the work; add routes for the new sections while keeping the existing
  endpoints/behaviour.
