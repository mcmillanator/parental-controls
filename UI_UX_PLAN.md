# UI/UX Redesign Plan — Bazzite Parental Controls (mobile-first)

Status: **IMPLEMENTED** — the design below landed on `master` in the
mobile-first UI redesign (foundation → Home/User → Admin/a11y, all merged).
Phases P0–P5 are done; **P6** (QA-suite + documentation reconciliation) is
closed by `feature/ui-docs` — docs and QA assets only, no code change. The
design content below is unchanged; see §7.1 for the per-phase status.
Audience for this document: the implementing agents and the project owner.

---

## 0. Design thesis

The panel is a **family status board**, not an enterprise admin console. The
parent almost always reaches it **one-handed on a phone**, usually for a
30-second decision: *"Is my kid still locked? Push bedtime out 30 minutes.
Freeze games."* Every screen should answer a status question **at a glance**,
then put one obvious safe action under the thumb, and keep the dangerous or
irreversible actions (Lock, Emergency logout, Remove admin, Clear schedule)
behind a deliberate confirmation. Because the panel lives on a **home LAN /
Bazzite box that may have no internet**, it must render identically **with zero
external requests** — Bootstrap moves from a CDN into the tree. The wide
desktop tables (budgets, windows, processes, audit) become labeled **card
stacks** on phones and **cards or tables** again on tablets/desktops, without
losing a single existing control or breaking the Katalon selector contract.

Three words drive every decision below: **glanceable, thumb-friendly, offline-safe.**

---

## 1. Method & sources

Ground truth read in full: `templates/{login,change_password,index,user,admin_audit,_insecure_banner}.html`,
`app.py` (routing/auth/CSRF/refresh), `budgets.py` (category + status model),
`tests/test_katalon_objects.py` and `katalon/{objects,test-cases}` (the pinned
selector/expected-text contract), `PLAN.md`, `README.md`. Constraints honored:
Flask + Jinja + vanilla JS + Bootstrap, **no build step / no SPA / no bundler**,
stdlib-only tests, fail-closed, localhost-first, TLS opt-in, children never
authenticate.

Practice surveyed and applied (named, well-established sources):

| Domain | Source | Applied as |
|---|---|---|
| Data tables → mobile | Roman Kamushken, *Data table UI design reference guide* (setproduct, 2026); NN/g tables guidance | Four mobile strategies; we pick **card transformation** for budgets/processes/audit and **priority+ / horizontal-scroll-freeze** for the home list. Semantic `<table>` retained on ≥md. |
| States | *States every data table needs* (default/hover/focus/selected/loading/empty/error/disabled) | §3.3 state matrix; empty-state copy per NN/g *Designing Empty States in Complex Applications* (Kaplan, 2021) |
| Progressive disclosure | IxDF, *Progressive Disclosure* | Advanced per-app overrides/details behind disclosure; one primary action per card |
| Mobile-first | UXPin *Mobile-First Design* (2026); Material 3 | Single column → grid; 44–48 px touch targets; `min-width` source order |
| WCAG 2.2 AA | W3C WCAG 2.2; Deque / Tetralogical / Vision Australia summaries of the 9 new SCs | §5: Target Size 24 px min (& 44 px practical), Focus Not Obscured, Dragging Movements, Consistent Help, Redundant Entry, Accessible Authentication; reduced-motion |
| Dark mode / tokens | Zeroheight *Implementing dark mode with design tokens*; Four Zero Three *Scalable & Accessible Dark Theme* | 3-tier tokens (core → semantic → component); **elevation via surface lightening, not pure-black inversion** |
| Parental-control UX | Apple Screen Time, Google Family Link, Qustodio case study (atico3), Kids360 | Status-first cards; one-tap "+15m"; plain-language "time left"; no jargon |
| Fintech | Neuron UX *Clear status labels*; The UXDA banking best practices | "Used / limit / left" as a spend-vs-limit model; **pending = "applies at next tick"**; destructive-transfer-style confirm |
| Healthcare portal | patient-portal audit/consent visibility convention | Audit log reframed as **Activity history** with plain-language summaries + explicit tamper status |
| IoT / home automation | Appthetics *Smart-home UI patterns*; Home Assistant offline-card practice | Per-user (future per-device) **status card**; explicit **stale/offline** state when the 10 s poll fails |

---

## 2. Design audit of the current UI

### 2.1 Screen inventory (complete — nothing may be lost)

| Route | Template | Purpose | Controls present today |
|---|---|---|---|
| `/login` | `login.html` | Admin app-password login | username, password, submit; insecure banner; error; "Admins only" note |
| `/change-password` | `change_password.html` | Set/rotate app password | current, new (min 12), confirm, submit; forced notice; error; insecure banner |
| `/` | `index.html` | All users + lock status; admin list | user table (Username link, UID, Status+In-Bedtime/Override badges, Bedtime summary, Lock/Unlock, Details); 10 s refresh notice; admin list + Add admin; toast |
| `/user/<u>` | `user.html` | Per-user control | Bedtime (start/end 12 h selects, all-days, 7 day checkboxes, Save, Clear); Bonus (status/until/granted-by, Revoke, +15/30/60/90/120 m shortcuts, custom h+m, Grant custom); Account status (UID/Shell/Home, badges, Lock/Unlock, Emergency logout); Time budgets table (limit h+m, Set, Used, Left, Status + Freeze/Unfreeze + Window-active); Weekly windows (table + enable toggle + Delete) and window-add row (type, category, searchable app picker, days, from/to, allowance, Add); App overrides in its own disclosure — list (+ Clear) and add row (searchable app picker, mode, minutes, Add); Processes by category (expand toggle, name+count+instances, elapsed, Hide, Freeze/Unfreeze, per-app override select+minutes+Set); hidden-processes table (Show); toast; 10 s refresh notice |
| `/admin/audit` | `admin_audit.html` | Audit log + admin accounts | chain status badge; audit table (Seq/Time/Actor/Action/Target/Detail) + Load more; admin table (Username/Tier/Status/Remove/Protected/you) + OTP reveal + Add admin; toast |

### 2.2 Cross-cutting problems (concrete)

1. **CDN dependency breaks the offline promise.** `index.html`, `user.html`,
   `admin_audit.html` each load
   `https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css`
   directly (lines 8 in each). On a home LAN box with no internet the pages
   still *function* (grid/util classes degrade) but the stated "must work
   without internet" goal is violated, and the CDN is a third-party request and
   a supply-chain surface. The other two pages load **no** Bootstrap at all, so
   the same product ships two different visual languages.
2. **CSS is duplicated, drifting, and partly invalid.** Four templates carry a
   copy of the same `* { margin:0 }` + dark-theme block; `login.html` /
   `change_password.html` are a third variant. `user.html` uses
   `border-radius: 8` (**unitless — invalid CSS, renders 0**) on the Bedtime
   (l.190), Account Status (l.281), Budgets (l.303) and Processes (l.421)
   cards, while `index.html` correctly uses `8px`. There is no shared
   stylesheet, so a fix lands in one page and not the others.
3. **Desktop-shaped tables on a phone.** The budgets table is 5 columns, one
   of which packs two number inputs plus a button; the process table is 4
   columns; the audit table is 6 columns. `.table-responsive` only adds a
   horizontal scroll — on a 360 px phone the user must pan sideways and the
   `h`/`m` inputs and `Set` button become cramped targets. The windows table is
   6 columns. None degrade to cards.
4. **Touch targets below guideline.** `.btn-sm` (~22 px tall) and the default
   buttons (`.4rem` padding + `.85rem` font ≈ 27–32 px) and the tiny
   `.btn-group-toggle` / `.btn-hide` sit well under the 44 px practical target
   and even under WCAG 2.2 **2.5.8 (24×24 CSS px)** for `.btn-sm`. Close
   `Set`/`Freeze`/`Hide` buttons on adjacent rows invite mis-taps on a phone.
5. **`window.confirm()` for destructive actions.** Lock, Unlock, Emergency
   logout, Remove admin, Clear window all use blocking `confirm()` — not a
   titled/describable modal, not styleable, not consistent, and it is the only
   "modal" in the product. Emergency logout (terminates sessions) deserves
   stronger, distinct treatment than a routine unlock.
6. **Feedback is toast-only and not accessible.** `#toast` is
   `display:none/block` with no `role`/`aria-live`; errors from a *form*
   (budget save, bedtime save) are shown as a transient toast that can vanish
   before it is read and is not tied to the field. There are no inline field
   errors and no loading states.
7. **Hierarchy is flat and inverted.** On `user.html` the **primary** hero
   concept — is this child locked right now? — is the *third* card, below
   Bedtime and Bonus. On `index.html` the admin list sits in a separate block
   **outside** `.page-wrap` (it closes at l.173; the `#admin-panel` at l.175
   is a sibling of the container), so it is full-bleed and visually detached.
8. **Contrast fails in several tokens** (computed against the surface behind
   them): white on `.badge-override` `#e67e22` ≈ **2.8:1 (fails AA)**;
   white on `.badge-paused` `#e94560` ≈ 3.8:1 and white on `.badge-frozen`
   `#4a90d9` ≈ 3.4:1 (both below 4.5 for normal text); `.refresh-info` `#666`
   on `#16213e` ≈ **2.8:1 (fails)**. Color is used alone to convey state in
   places (status text color).
   *(Historical audit against the pre-translation palette; the shipped values
   were re-sampled from the canvas in §11.6 — see §3.1 for the current tokens.)*
9. **Status is mixed-language and mixed-color.** "Locked / Unlocked / Unknown"
   text color is the only signal in some cells; `Freeze`/`Unfreeze`,
   `Paused`/`Exhausted`/`Frozen`, `Window active`, `In Bedtime`, `Override`,
   `Bonus`, `Unlimited`, `× N` all use ad-hoc chip colors with no shared
   meaning (orange = override *and* nothing else; blue = frozen *and*
   window…). No legend, no icon, no consistent category.
10. **No responsive navigation.** Every page has a different ad-hoc header
    (`index` uses inline `<p>` links; `user`/`admin` use `float:right` spans
    with inline styles; `login` has none). There is no persistent way to move
    between Home / User / Admin / Account on a phone, and the `float:right`
    block collides with the `← Back` link at narrow widths.
11. **No empty/loading/error states.** `index.html` renders an empty `<tbody>`
    if no users; processes show a terse error string; the audit "Load more"
    has no busy state; a failed 10 s refresh silently leaves stale data with no
    "last updated / stale" indicator.
12. **`type="time"` is deliberately forbidden** (`test_time12h` asserts it is
    absent; storage stays 24 h, UI is 12 h). The 12 h control is three
    `<select>`s, one of which has **60 minute options** — usable but heavy on
    mobile. This is a **constraint to preserve**, not a bug to fix here.
13. **Auto-refresh pause is a hidden feature.** "Auto-refresh pauses while form
    inputs are focused (no lost form data)" exists in JS but is invisible to
    the user; a redesign should surface a small "paused while editing" hint.
14. **`autocomplete="off"` on the login form** contradicts the field-level
    `autocomplete="username"`/`current-password` and hurts password managers.

### 2.3 Screen-by-screen notes

- **login / change-password** — visually different from the app; not
  responsive beyond a max-width card; the `_insecure_banner` inline styles
  duplicate the danger token; errors are a raw red box. Keep the auth model
  (app-password only, no PAM, CSRF on change-password, forced-change notice)
  untouched.
- **index** — right shape (a list of users) but the wrong *form* on mobile;
  Status cell carries three concerns (text status + two badges); Bedtime cell
  packs time + days; Actions column mixes two levels of action (primary
  Lock/Unlock vs Details); admin block is detached; no summary.
- **user** — the most important page and the most desktop-shaped: hero status
  buried, six-column windows table, five-column budget table with embedded
  inputs, a process table nested inside another table for details. Long, flat
  scroll with no anchors. The power features (weekly windows, per-app
  overrides, hidden processes, process details) belong behind disclosure.
- **admin_audit** — six-column log is unreadable on a phone; `Detail` can be a
  120-char JSON string; no actor/action/target filters or grouping; chain
  status is good and should stay prominent; OTP reveal box is good and should
  become a proper modal/step with a copy affordance.

---

## 3. Design system

Delivered as **one static stylesheet** `static/css/app.css` (core + semantic +
component layers) plus a vendored Bootstrap CSS (§6.3). No build step: the file
is hand-written/committed. Dark is the default; a light theme is available via
tokens and `prefers-color-scheme` (see open question Q1).

### 3.1 Tokens (3-tier)

**Tier 1 — core palette** (names are stable; values are the only thing a theme
swaps). **Values re-sampled from the live Flowstep canvas** (§11.6): the design
is a warm cream/ivory + amber theme, so the warm family is mapped onto the
original names (`--pc-green-500` now holds the amber the "primary" role uses).

```
--pc-ink-900:#1f1c17  --pc-ink-800:#2a251c  --pc-ink-700:#241f18
--pc-ink-600:#322c22  --pc-ink-500:#3d3628  --pc-ink-400:#4a4132
--pc-navy-700:#57492f  --pc-navy-800:#3a3226
--pc-green-500:#c47f17  --pc-green-600:#a5650f   /* canvas amber = primary */
--pc-red-500:#d92d20    --pc-red-700:#7a1f16
--pc-amber-400:#c47f17  --pc-amber-700:#8a5a12
--pc-violet-500:#6a4c93 --pc-orange-500:#e08a2b --pc-blue-500:#4a7d8c
--pc-slate-100:#f4efe4  --pc-slate-400:#c4b8a2  --pc-slate-500:#b3a68d
--pc-slate-600:#6a6255
--pc-white:#ffffff     --pc-black:#000000
```

**Tier 2 — semantic tokens** (`:root` = dark; `[data-theme="light"]` overrides).
These are what components use; nothing uses Tier 1 directly. The canvas only
specifies the light (cream) theme; the dark theme is its warm counterpart.

```
--bg:            #241f18;            /* page */
--surface:       #2a251c;            /* card / table */
--surface-2:     #383126;            /* zebra, nested */
--surface-3:     #423a2d;            /* hover */
--surface-raised:#322c22;            /* dialog (elevation via border/shadow) */
--border:        #4a4132;
--border-strong: #5c5140;
--text:          #f4efe4;
--text-muted:    #c4b8a2;            /* AA on surface + surface-2 */
--text-faint:    #b3a68d;            /* AA on surface + surface-2 */
--link:          #e0a94a;
--focus-ring:    #e0a94a;            /* 2px + 2px offset */

/* warm-palette roles the canvas introduces */
--primary-bg:#c47f17 --primary-fg:#ffffff --primary-hover:#d68c1e
--accent-bg:#3d4a45  --accent-fg:#eaf3f0   /* sage active-nav / Protected */
--sidebar-bg:#2a2419                       /* sidebar is a distinct surface */

/* status foregrounds chosen to pass AA on their chip background */
--ok-fg:#eafaf1;   --ok-bg:#2c6e4f;
--danger-fg:#ffffff; --danger-bg:#d92d20;
--danger-soft-fg:#ffb3ac; --danger-soft-bg:#4a201c;  /* pale Locked pill */
--warn-fg:#ffffff;  --warn-bg:#8a5a12;
--info-fg:#f6dfae;  --info-bg:#4a3a1c;
--override-fg:#ffffff; --override-bg:#a5650f;
--bedtime-fg:#e6def7; --bedtime-bg:#4a3d63;
--bonus-fg:#ffffff; --bonus-bg:#a5650f;
--neutral-fg:#f4efe4; --neutral-bg:#423a2d;
```

**Tier 3 — component tokens** derive from tier 2 (`--btn-bg`, `--chip-bg`,
`--card-radius`, `--nav-h`, `--touch`, …).

**Type scale** (rem, 16 px root): `--fs-xs:.75` (chips, meta), `--fs-sm:.85`,
`--fs-base:1`, `--fs-lg:1.15`, `--fs-xl:1.4`, `--fs-2xl:1.75` (page title).
Family stays the system stack already in use; no webfont (offline).

**Spacing** (4 px base): `--sp-1:4 --sp-2:8 --sp-3:12 --sp-4:16 --sp-5:24
--sp-6:32 --sp-8:48`.

**Radius:** `--r-sm:6px --r-md:10px --r-lg:14px --r-pill:999px`.
(the invalid `border-radius: 8` strings are removed).

**Elevation** (dark mode = *surface lightening* + border, per Zeroheight /
Four Zero Three — never a glow on pure black):
`--el-0: none; --el-1: 0 1px 2px rgba(0,0,0,.4); --el-2: 0 6px 20px rgba(0,0,0,.5)`
`.card{border:1px solid var(--border)}` gives the primary separation.

**Motion:** `--dur:140ms --ease:cubic-bezier(.2,.6,.2,1)`; all transitions
wrapped in `@media (prefers-reduced-motion: reduce){*{transition:none!important;animation:none!important}}`.

**Light theme** — the canvas palette (sampled, see §11.6): `--bg:#faf7f0
--surface:#ffffff --surface-2:#f4efe4 --surface-3:#eae3d4 --border:#e4dcc9
--border-strong:#cec2a8 --text:#1f1c17 --text-muted:#6a6255 --text-faint:#75695a
--link:#8a5a12 --focus-ring:#8a5a12`, amber primary `#c47f17` on white, sage
accent `#cfe0da`/`#1b4a44`, sidebar `#f4efe4`, canvas Online/Unlocked pill
`#d7ebd7`/`#003a05`, soft Locked pill `#fbe3e0`/`#8f1d15`, destructive
`#d92d20`, status text tokens `--ok-text:#14602f --warn-text:#8a5a12
--danger-text:#b3261e --info-text:#7a4f0a`.

### 3.2 Component inventory

| Component | Notes / current mapping |
|---|---|
| **App bar** | Sticky top; brand (below 768 px only — from tablet up the brand lives in the sidebar), account menu (Change password / Log out) and Help as `<details>` popovers, theme toggle (no JS framework) |
| **Bottom tab bar** | 4-tab bar **Home · Time · Activity · Admin** + an **Account** `<details>` menu (Settings / Change password / Log out); `--touch` 44 px; `aria-current` on active (§11). Hidden from 768 px up except on the phone-first Overview shell |
| **Skip link** | "Skip to content" first focusable |
| **Button** `.pc-btn` variants `--primary` (green: Unlock, Save, Set, Add), `--danger` (Lock, Clear, Remove, Emergency logout), `--warn` (Freeze), `--neutral` (Hide, secondary), `--ghost` (Load more, Details). Sizes: default 44 px; `--sm` **min 32 px** with 8 px spacing (fixes 2.5.8); `--icon`. |
| **Chip / badge** `.pc-chip` variants `ok/danger/warn/info/override/bedtime/neutral/bonus`. Always **icon + text + color** (never color alone). |
| **Status pill** | Large (`--fs-sm`, pill radius) status + optional icon; used on user cards: Locked / Unlocked / Unknown / In Bedtime / Bonus |
| **Card** `.pc-card` = surface + `--r-md` + border + `--sp-4`; header/body/footer; optional `aria-labelledby` |
| **Table** `.pc-table` (semantic, ≥md): `<th scope>`, sticky header, right-aligned numerics, single separators |
| **Table→card** `.pc-table--stack` + `data-label` (§4.3) |
| **Accordion / disclosure** `.pc-disclosure` — native `<details><summary>` so it works without JS; used for per-app override editors and hidden processes |
| **Form field** `.pc-field` label + control + `--hint` + `--error`/`aria-describedby`; inputs `.pc-input`; duration input `.pc-duration` (two number inputs + unit labels, `inputmode="numeric"`); 12 h time control `.pc-time` (3 selects — preserved per §2.12) |
| **Toast** `.pc-toast` with `role="status"` `aria-live="polite"` (success) / `role="alert"` (error); stacked; dismiss button; auto-hide only for success |
| **Dialog** `.pc-dialog` — native `<dialog>` + `showModal()` (focus trap for free) with `--danger` styling for irreversible actions |
| **Empty state** `.pc-empty` icon + one-line status + one action (NN/g pattern) |
| **Skeleton** `.pc-skeleton` rows for first load / Load more |
| **Summary strip** `.pc-summary` KPI row on Home (e.g. "2 locked · 1 in bedtime · 1 paused") |
| **Banner** `.pc-banner--danger` for insecure-LAN (replaces inline-styled partial) |
| **Progress** `.pc-meter` used/time bar on budget cards (fintech spend-vs-limit) |

### 3.3 State matrix (every interactive component)

| Component | hover | focus-visible | active | disabled | empty | loading | error | stale |
|---|---|---|---|---|---|---|---|---|
| Button | bg +6% | 2 px ring | −1 px / darker | 45% + `not-allowed`, out of tab order | — | `aria-busy` + inline spinner, label kept | — | — |
| Chip | — | ring if interactive | — | muted | — | — | — | — |
| Card | el-1 lift | — | — | muted | `.pc-empty` | skeleton | error text + Retry | "stale" + last-updated |
| Table row | bg shift | row focus ring | — | muted | "No users/entries" row | skeleton rows | inline error row | dim + banner |
| Field | border brighten | ring | — | muted | placeholder | spinner in field | red border + message via `aria-describedby`, `aria-invalid` | — |
| Toast | — | ring on dismiss | — | — | — | — | `role=alert` | — |
| Dialog | — | trapped focus | — | — | — | — | inline | — |
| Table→card | el-1 | card ring | — | muted | empty card | skeleton card | error card | stale chip |

---

## 4. Mobile-first layouts

### 4.1 Breakpoints & targets

`--bp-sm:576px`, `--bp-md:768px`, `--bp-lg:1024px`, `--bp-xl:1200px`.
Base styles target **360 px** (single column, `--touch:44px`, `--fs-base:16px`
to avoid iOS zoom-on-focus for the number inputs). Bootstrap grid is already
present; we lean on it but keep critical layout in `app.css` so pages do not
depend on utility classes being fetched. Content max-width `--content:880px`
(forms/user) and `--content-wide:1120px` (home/audit).

### 4.2 Navigation model

> **Superseded by §11 (design translation).** The 5-section IA replaced the
> original 3-item phone bar: the phone bottom bar is now **Home · Time ·
> Activity · Admin** plus an Account menu, and ≥960 px screens carry a desktop
> sidebar. The original model is kept below for history.

- **Phone (<md):** sticky **app bar** (title + account `<details>` menu) + sticky
  **bottom tab bar**: `Home` (`/`) · `Admin` (`/admin/audit`) · `Account`
  (menu: Change password, Log out). Back navigation is a labelled
  `← Back to users` link at the top of `user.html`. All targets ≥44 px.
- **Tablet/desktop (≥md):** no bottom bar; the app bar shows the same three
  destinations inline plus the account menu; a `← Back` link stays on the user
  page. Active destination marked `aria-current="page"`.
- Layout convention: header/partials in `templates/_appbar.html` and
  `templates/_tabbar.html`, both pulled into a shared `templates/base.html`
  (see §7). Login/change-password use a centered `.pc-auth` shell (no nav).

### 4.3 Table → card pattern (the core mobile fix)

One mechanism, reused everywhere:

```html
<table class="pc-table pc-table--stack">
  <thead><tr><th scope="col">Category</th><th scope="col">Limit</th>…</tr></thead>
  <tbody>
    <tr>
      <td data-label="Category">Games</td>
      <td data-label="Limit">…inputs…</td>
      <td data-label="Used">45m</td>
      <td data-label="Left">45m</td>
      <td data-label="Status">…chips + Freeze…</td>
    </tr>
  </tbody>
</table>
```

CSS (in `app.css`):

```css
@media (max-width: 767.98px){
  .pc-table--stack thead { position:absolute; clip:rect(0 0 0 0); }      /* keep SR labels */
  .pc-table--stack tr { display:block; background:var(--surface); border:1px solid var(--border);
                        border-radius:var(--r-md); margin-bottom:var(--sp-3); padding:var(--sp-3); }
  .pc-table--stack td { display:grid; grid-template-columns:9rem 1fr; gap:var(--sp-3);
                        border:0; padding:var(--sp-1) 0; }
  .pc-table--stack td::before { content:attr(data-label); color:var(--text-muted); font-weight:600; }
}
```

Why this over the alternatives: horizontal-scroll keeps comparison but asks
thumb panning and cramps embedded inputs; hiding columns loses controls we must
keep; card transformation is "few records read individually" (home list, budgets
per category, windows, one process group) which is exactly our data. The
**semantic table stays in the DOM** (`thead`/`th scope` retained, visually
hidden via `clip`) so screen readers still get row/column relationships — the
setproduct/W3C guidance to prefer native semantics over ARIA grid roles.

The **audit log** additionally keeps an inline "Seq · Time" header and a
collapsed `Detail` on mobile (grouped by day with a sticky date header).

### 4.4 Wireframes (phone → tablet → desktop)

#### Home (`/`) — phone
```
┌──────────────────────────────────┐
│ ☰ Parental Controls        ⟨you⟩ │  app bar (sticky)
│ Manage this Bazzite system       │
├──────────────────────────────────┤
│ ⚠ Insecure LAN — enable TLS      │  banner (only if insecure)
├──────────────────────────────────┤
│ 2 locked · 1 in bedtime · 1 paused│  summary strip
├──────────────────────────────────┤
│ ┌ alice ──────────────[ 🔒 LOCKED]┐
│ │ In Bedtime · Bonus 1h 30m      │
│ │ Bedtime 9:00 PM–7:00 AM        │
│ │ Mon Tue Wed Thu Fri            │
│ │ [  Unlock  ]   [ Details › ]   │
│ └────────────────────────────────┘
│ ┌ bob ──────────────[ 🔓 UNLOCKED]┐
│ │ Bedtime 10:00 PM–6:30 AM · Daily│
│ │ [  Lock  ]     [ Details › ]    │
│ └────────────────────────────────┘
│  (admin list: collapsed disclosure)
│ ┌▸ Admins (2) ────────────────┐
├──────────────────────────────────┤
│  Home    |   Admin   |  Account  │  bottom tabs (sticky, 44px)
└──────────────────────────────────┘
```
- Tablet (≥768): 2-up card grid; summary strip full width.
- Desktop (≥1024): `.pc-table` (Username, UID, Status, Bedtime, Actions) with
  the admin list in a right rail or below in a normal-width card.

#### User details (`/user/<u>`) — phone
```
┌──────────────────────────────────┐
│ ← Back to users                  │
│ alice                    [ 🔒 ]  │  hero: who + live status pill
│ ⚠ In Bedtime · Bonus 1h 30m       │
│ [ Unlock ]   [ ⋯ ]               │  primary + overflow (Emergency logout)
├──────────────────────────────────┤
│ ┌ Bedtime ───────────── 9p–7a  ▸ ┐│  disclosure, summary in header
│ └────────────────────────────────┘
│ ┌ Bonus time ──────── 1h 30m ▸   ┐│
│ │ [–15m..] +15m +30m +1h +1h30 +2h│
│ │ custom [h][m] [Grant] [Revoke] │
│ └────────────────────────────────┘
│ ┌ Time budgets ──────────────────┐
│ │ ┌ Games ───────────────[  OK ] │
│ │ │ Limit [1]h [30]m [Set]       │
│ │ │ Used 45m   Left 45m  [meter] │
│ │ │ [ Freeze ]                   │
│ │ └──────────────────────────────┘
│ │ ┌ Browsers … (repeat) ─────────┐
│ └────────────────────────────────┘
│ ┌ Weekly windows & overrides ▸ ──┐│
│ ┌ Processes ▸ ───────────────────┐│
│ ┌ Hidden processes (3) ▸ ────────┐│
└──────────────────────────────────┘
```
- Tablet (≥768): Bedtime/Bonus in a 2-col row; budgets as a real table again.
- Desktop (≥1024): two-column page — left column Account + Bedtime + Bonus,
  right column Budgets + Windows + Processes; process detail stays an inline
  expandable row (`tr.group-detail`).

#### Budget card (table→card, detail)
```
┌ Games ─────────────────────[  OK ]┐
│ Limit (per day) [ 1 ]h [ 30 ]m [Set]│
│ Used  45m        Left  45m         │
│ ▓▓▓▓▓▓▓▓▓▓░░░░░░  50%              │
│ [ Freeze ]        (Window active)  │
└────────────────────────────────────┘
Frozen state → [ ⏸ Frozen — out of time ] [ Unfreeze ]
Exhausted    → [ ⏸ Exhausted ] [ Freeze ]
```

#### Process group (card + accordion detail)
```
┌ ▸ firefox  ×3   (3 instances)      │
│   Used 12m                          │
│   [ Hide ] [ Freeze ]  Override ▾   │
└─────────────────────────────────────┘
expanded (native <details>):
│ PID  Command          Used    Category
│ 9001 firefox          12m     browsers
```

#### Audit (phone)
```
┌ Audit Log ────────────────────────┐
│ ✅ Audit chain intact             │
│ Showing 100 (limit 100)           │
│ ─ Oct 11 ─────────────────────    │
│ ┌ seq 412 · 10:50 PM ────────────┐
│ │ actor mom · action lock        │
│ │ target alice                   │
│ │ detail {"reason":"manual"}     │
│ └────────────────────────────────┘
│ [ Load more ]                     │
└───────────────────────────────────┘
```

#### Login / change password
```
┌──────────────────────────┐
│      Parental Controls   │
│ ⚠ insecure banner        │
│ [ error inline           │
│   aria-live=assertive ]  │
│ Admin username [ ______ ]│  44px controls
│ App password   [ ______ ]│
│ [      Log in       ]    │
│ Admins only. App password│
└──────────────────────────┘
```

---

## 5. Screen-by-screen redesign specs — every control mapped

> Rule: **no control is removed.** New behaviour is limited to layout,
> disclosure, labeling, and confirmation. IDs/classes pinned by the Katalon
> contract are preserved literally (§7.4).

### 5.1 `login.html`
| Control | New placement |
|---|---|
| username `#username` | `.pc-auth` card, first field, `autocomplete="username"` (drop form `autocomplete=off`) |
| password `#password` | second field, `autocomplete="current-password"` |
| Log in button | full-width `.pc-btn--primary`, 44px |
| insecure banner | `.pc-banner--danger` partial (same include) |
| error | inline `.pc-field__error` with `role="alert"`, keep `.error` class |
| "Admins only…" note | `.pc-hint` |

### 5.2 `change_password.html`
`#current`, `#new`, `#confirm` in the same card; forced-change notice becomes
an `.pc-banner--info` (keep `.info` class for the `.info` selector); the min-12
rule moves to a `--hint` under `#new` with live `aria-describedby`; errors
inline. Keep `.error`.

### 5.3 `index.html`
| Current | New |
|---|---|
| `h1`, `.subtitle`, "Logged in as" links | app bar (`_appbar.html`) + `.pc-summary` strip; links move into the account menu/tabs |
| user table (Username/UID/Status/Bedtime/Actions) | `.pc-table--stack`; on phone each `<tr>` = user card (§4.4). Keep `#row-{{ user.username }}` and `#row-… .btn-details` |
| Username link | card title link → `/user/<u>` |
| UID | small meta line in card / table column |
| Status + `In Bedtime`/`Override` badges | status **pill** (icon+text+color) + chips; keep status classes `status-locked/unlocked/unknown`; keep `.badge-bedtime`/`.badge-override` |
| Bedtime start–end + days | "Bedtime 9:00 PM–7:00 AM · Mon–Fri" line; keep `|twelve` formatting |
| Lock / Unlock conditional buttons | primary action on the card; keep `.btn-lock`/`.btn-unlock` and `onclick="lockUser/unlockUser"` |
| Details button | secondary outline; keep `.btn-details`, target `#row-… .btn-details` |
| `#admin-panel`/`#admin-list`/`#new-admin-username`/Add admin | move **inside** the content container as a collapsed `.pc-disclosure`; keep all three ids |
| `#toast` | `.pc-toast` with `role=status` + stack; keep id `#toast` |
| `.refresh-info` | "Status updates automatically every 10 seconds" kept verbatim (pinned text) + a **stale badge** when a poll fails |
| `window.confirm` for lock/unlock | `.pc-dialog` (danger styling for Lock) |

### 5.4 `user.html`
| Current | New |
|---|---|
| `a.back-link` | kept; label "← Back to users" |
| float-right nav | app bar / tabs (no `float`) |
| Bedtime card (start/end `time_select`, all-days, 7 day checkboxes, Save, Clear) | hero-adjacent `.pc-disclosure` card, summary in the `<summary>`; forms unchanged; keep `name="start_*"/"end_*"/"day_N"/"all_days"`, `#all_days_checkbox`, `.day-checkbox`, `.btn-bedtime-save`, `.btn-bedtime-clear`; keep `>12h` formatting and **no `type=time`** |
| Bonus status `#bonus-status`, `.badge-bonus`, `.btn-lock` (Revoke) | inside Bonus disclosure; keep ids/classes; shortcut button **labels unchanged** (+15m/+30m/+1h/+1h30/+2h/Grant custom/Revoke — xpath-pinned) |
| `#bonus-hours` `#bonus-minutes` + Grant custom | `.pc-duration`; keep ids |
| Account Status (`#account-status`, `#account-status-text`, `#account-badges`, `.status-*`, `.badge-*`) | hero card at top: UID/Shell/Home as meta; keep every id/class; actions `#account-actions` with `.btn-lock`/`.btn-unlock`/`.btn-logout` |
| Lock/Unlock | primary; `.pc-dialog` confirm |
| Emergency logout `.btn-logout` | overflow `⋯` menu → dedicated danger dialog ("Terminates ALL sessions — type the username to confirm" / Redundant-Entry-friendly copy); keep class |
| Budgets table (Category, Limit`#budget-<cat>-hours`/`-minutes`+Set, `#budget-used-<cat>`, `#budget-left-<cat>`, `#budget-status-<cat>` + Freeze/Unfreeze + Window) | `.pc-table--stack`; on phone = budget cards with `.pc-meter`; keep every id incl. `#budget-status-<cat> .btn-freeze/.btn-unlock`; status chips keep text "OK"/"Frozen"/"Paused"/"Exhausted" (pinned glob texts) and `<span title="Set a limit first">OK</span>` |
| Weekly windows table (Target/Days/Time/Allowance/Enabled/Delete) | disclosure; phone cards; keep `.window-row`, `data-window-id`, `.window-enabled`, `onchange="toggleWindow"`, and Delete (`clearWindow`) |
| window-add row (type, category, app, days, `time_select` from/to, allowance, Add) | `.pc-field`s in a card; keep `#window-target-type`, `#window-category-target`, `#window-app-target`, `.window-day`, `#window-allowance`, `saveWindow()`; keep `time_select('window_start'/'window_end', …)` |
| App overrides `<ul id="app-override-list">` + Clear | `.pc-list`; keep `data-override-row`, `data-comm`, `clearOverride` |
| override add row (comm, mode select `.override-mode`, minutes, Add) | `.pc-field` card; keep ids/`addAppOverride`; `installed-apps` datalist kept |
| Processes `#process-section`, per-category `h3`, `.group-row`, `.btn-group-toggle[aria-expanded/aria-controls]`, `.group-chevron`, `.process-name`, `.badge-count`, `.btn-hide`, freeze/unfreeze, `.override-mode`, `.override-minutes`, `applyOverride` | `.pc-table--stack` + per-group `.pc-disclosure`; JS detail builder keeps `<thead><tr><th>PID</th>…` (pinned "PID") and `tr.group-detail .process-name`; keep category `h3` text e.g. "Games (2)" (pinned) |
| hidden processes `#hidden-procs`, `.btn-show-hidden`, `.group-row`, "Show hidden (N groups, M processes)" | kept; `.pc-disclosure`; preserve exact button string |
| `.refresh-info`, `#toast` | as Home |
| auto-refresh pause | visible `.pc-hint` "Updates paused while you edit" shown when a field is focused |

### 5.5 `admin_audit.html`
| Current | New |
|---|---|
| back link, float-right nav | app bar/tabs |
| `#audit-section`, `#chain-status`, chain chips | keep `#audit-section`; chain chips become `.pc-chip--ok/--danger` (keep classes `.chain-intact`/`.chain-broken`) |
| audit table `#audit-table` (Seq/Time/Actor/Action/Target/Detail) | `.pc-table--stack`; day-grouped cards on phone; `data-at` upgrade kept; keep `#audit-table`, `.audit-row`, `.audit-time`, `.audit-detail` |
| `#audit-count`, `#audit-limit`, `#load-more` | kept; Load more gets `aria-busy` + skeleton; "Load more entries" text kept |
| `#admin-section`, `#admin-table`, `.admin-row`, Tier/Status/Actions, `.badge-protected`, Remove | `.pc-table--stack`; keep ids/classes; Remove → `.pc-dialog` |
| `#onetime-password`, `#otp-user`, `#otp-value`, "I've saved it" | OTP becomes a `<dialog>` (.pc-dialog) with copy button; keep ids and the alert box markup for the `.otp-box` style |
| `#new-admin-username`, Add admin, `#admin-add` | collapsed `.pc-disclosure`; keep ids (test_admin_console asserts `id="admin-add"` absent for non-wheel — preserve that conditional) |
| `#toast` | as Home |

---

## 6. Accessibility & offline-asset strategy

### 6.1 WCAG 2.2 AA commitments
- **1.4.3 / 1.4.11 contrast:** all text ≥4.5:1, UI borders ≥3:1 (tokens in §3.1
  fix the four failures: badge-override 2.8, badge-paused 3.8, badge-frozen 3.4,
  `.refresh-info` 2.8). State is **never color alone** — chips carry an icon and
  text ("🔒 Locked", "⏸ Frozen").
- **2.4.11 Focus Not Obscured:** sticky app bar / bottom bar use
  `scroll-margin-top` on headings; focused controls auto-scroll clear; dialogs
  are native `<dialog>` (no focus behind).
- **2.5.8 Target Size (Minimum):** interactive targets ≥24×24 CSS px minimum,
  44 px practical; `.btn-sm` raised from ~22 px to ≥32 px with 8 px spacing.
- **2.4.7 / focus appearance:** a consistent `:focus-visible` ring
  (2 px `--focus-ring` + 2 px offset) on every interactive element.
- **2.5.7 Dragging (AAA, adopted anyway):** no drag interactions are
  introduced; the 12 h time control stays click/select.
- **3.3.7 Redundant Entry:** Emergency-logout confirm pre-fills/reads the
  username rather than asking to retype except where a deliberate barrier is
  wanted; forms keep browser autofill.
- **3.3.8 Accessible Authentication:** login stays app-password + password
  manager friendly (drop `autocomplete=off`), no puzzle/CAPTCHA.
- **4.1.3 Status Messages:** toast uses `role="status"`/`role="alert"` +
  `aria-live`; the 10 s refresh announces changes politely
  (`aria-live="polite"` on a visually-hidden region, throttled, paused when the
  tab is hidden and when an input is focused).
- **2.3.3 / reduced-motion:** all transitions disabled under
  `prefers-color-scheme`/`prefers-reduced-motion`.
- **1.4.3 dark mode:** dark theme uses **surface lightening + borders**, not
  pure-black inversion, per dark-theme token practice.
- Keyboard: native `<details>` disclosures and `<dialog>` give correct
  keyboard behaviour without custom JS; the process expand button keeps
  `aria-expanded`/`aria-controls`.

### 6.2 Offline / no-internet states (IoT-style)
- Every live region shows a **"last updated HH:MM"**; a failed poll flips a
  subtle **"stale — retrying"** chip and keeps the last good data (never blank).
- Forms retain the existing "pause refresh while editing" behaviour and now
  *say so*.
- Network/`fetch` errors use `role=alert` and offer Retry; first load uses
  `.pc-skeleton` rows.
- Multi-device (Phase 5) reserves a **device chip** on each user card and a
  per-device status line, so a future offline device reads "Device: kitchen —
  offline" without a layout change.

### 6.3 Vendored Bootstrap vs CDN — **recommendation: vendor locally**

**Vendor** Bootstrap 5.3.3 (`bootstrap.min.css`), committed under
`static/vendor/bootstrap/`, and link it with `url_for('static', ...)`. Rationale:

- The panel is explicitly a **home LAN / localhost** tool that "must work
  without internet"; a CDN link is a hard external dependency and a privacy
  leak on every page load. When the LAN box is offline, a CDN failure changes
  the rendering and logs console errors.
- The project has **no build step**, so a vendored dist file is the natural
  artifact: commit the pinned minified CSS once, bump it deliberately.
- Version pinning stays identical (both approaches pin 5.3.3); vendoring also
  removes CDN availability/tampering as variables. Subresource Integrity (SRI)
  protects a *CDN* fetch, not an offline one.
- Serve it via Flask's existing `/static` endpoint (already exempt from the
  must-change redirect via `_MUST_CHANGE_EXEMPT = {…, 'static'}`); no auth
  change, no new route.
- **Do not** vendor `bootstrap.bundle.js`. Modals/tooltips are replaced with
  native `<dialog>` and `<details>`, so only CSS is needed — smaller surface,
  no jQuery/Popper, and better a11y.
- Keep our own `static/css/app.css` **after** Bootstrap so tokens/overrides win;
  a homeserver has no cache-warming benefit from splitting files.

Optional hardening (owner to decide): serve with a long `Cache-Control` and a
filename that carries the version, plus a `tests/test_ui_assets.py` assertion
that no template contains `http://`/`https://` asset URLs.

---

## 7. Implementation plan

### 7.1 Phasing (each phase = its own commit, tests green at every step)

**Phase status (2026-10-12): P0–P5 merged to `master`; P6 closed by
`feature/ui-docs`.** Zero Katalon selector changes were needed (§7.4).

**Phase 0 — Guardrails (no visual change). — ✅ merged**
Add `tests/test_ui_assets.py`: (a) no template references `cdn.jsdelivr.net` or
any absolute `http(s)://` asset; (b) `static/vendor/bootstrap/bootstrap.min.css`
and `static/css/app.css` exist and are non-empty; (c) every app template
contains `<meta name="viewport">`. This test fails on the *current* tree and
passes from Phase 1 — it is the objective "offline promise" check.

**Phase 1 — Assets + shared shell (no behaviour change). — ✅ merged**
- Add `static/vendor/bootstrap/bootstrap.min.css` (vendored 5.3.3).
- Add `static/css/app.css` (tokens + component CSS from §3).
- Add `templates/base.html` (head, viewport, `app.css`, banner, toast container,
  app bar/tab bar, `{% block content %}`), `templates/_appbar.html`,
  `templates/_tabbar.html`, `templates/_confirm_dialog.html`,
  `templates/_toast.html`, `templates/_icons.html` (inline SVG sprite).
- Rewrite `_insecure_banner.html` to `.pc-banner--danger` (keep the
  `insecure_lan` conditional).
- Convert `index.html`, `user.html`, `admin_audit.html` to `{% extends
  "base.html" %}`; **do not move any id/class yet** — only lift the shared
  head/nav/toast/banner. Confirm the render is byte-comparable for the tested
  fragments.

**Phase 2 — Home card layout. — ✅ merged**

`.pc-summary`, user cards with `.pc-table--stack`,
move admin list inside the container, dialog confirmations for Lock/Unlock.
Preserve `#row-…`, `.btn-details`, `.btn-lock/.btn-unlock`, `#admin-panel`,
`#admin-list`, `#new-admin-username`, `.refresh-info`, `#toast`.

**Phase 3 — User details. — ✅ merged**

Reorder to hero (status/actions) → disclosures;
budget `.pc-table--stack` + `.pc-meter`; process `.pc-table--stack` +
per-group `<details>`; windows/overrides disclosures. JS renderers
(`rowHtml`/`processHtml`/`groupDetailHtml`, `renderBudgets`) updated to emit
`data-label`s and the same ids.

**Phase 4 — Admin console. — ✅ merged**

Audit table→cards + day grouping, dialog Remove,
`<dialog>` OTP, Load-more busy state.

**Phase 5 — A11y + light theme + docs. — ✅ merged** (the README/PLAN
documentation half is completed as part of P6 below)

Focus rings, `aria-live` refresh,
stale/last-updated, reduced-motion, light-theme tokens, update `README.md`
(assets note) and add a row for `UI_UX_PLAN.md` to `PLAN.md`'s Files table.

**Phase 6 — QA suite reconciliation. — ✅ done (`feature/ui-docs`, docs + QA
assets only).** Ran `tests/test_katalon_objects.py` after the full redesign:
**green, zero selector changes** (the validator corpus now also covers
`templates/_partials/`, so a selector that moved into the shell — e.g. `#toast`
— still resolves). No `katalon/objects` selector needed updating;
`katalon/README.md` gained the new navigation instructions.

### 7.2 File-level change list

| File | Action |
|---|---|
| `static/css/app.css` | **new** — all tokens/components/states (§3) |
| `static/vendor/bootstrap/bootstrap.min.css` | **new** — vendored 5.3.3 |
| `templates/base.html` | **new** — shared shell |
| `templates/_appbar.html`, `_tabbar.html` | **new** |
| `templates/_confirm_dialog.html`, `_toast.html`, `_icons.html` | **new** |
| `templates/_insecure_banner.html` | edit — token banner |
| `templates/index.html` | edit — extends base, cards, move admin panel |
| `templates/user.html` | edit — hero + disclosures + stack tables + JS renderers |
| `templates/admin_audit.html` | edit — stack + dialogs |
| `templates/login.html`, `change_password.html` | edit — auth shell + inline errors + tokens |
| `tests/test_ui_assets.py` | **new** |
| `tests/test_durations.py`, `test_time12h.py`, `test_process_grouping.py`, `test_overrides.py`, `test_admin_console.py`, `test_auto_hide_names.py`, `test_freeze_buttons.py` | edit **only if** a pinned literal moves (§7.4) |
| `katalon/objects/*`, `katalon/README.md` | edit **only if** a selector must change |
| `README.md`, `PLAN.md` | docs — assets note + Files-table row |

**No** changes to `app.py` routes/contracts, `budgets.py`, `install.sh`,
`systemd/*`, `sudoers-*`, or any auth/session logic.

### 7.3 Keeping the stdlib suite green

The suite is stdlib `unittest`; several tests assert **exact markup substrings**
(not just behaviour). Treat these as a contract and preserve them:

- `test_durations.py`: `id="budget-{{ cat }}-hours"`,
  `id="budget-{{ cat }}-minutes"`, `min="0" max="59" step="1" id="budget-{{ cat }}-minutes"`,
  `assertNotIn('id="budget-{{ cat }}"')`, the JS `budget-' + category + '-hours'`,
  `hours * 60 + mins`, `'&minutes=' + encodeURIComponent(total)`.
- `test_time12h.py`: `assertNotIn('type="time"')`; `user.bedtime.start|twelve`,
  `until[11:16]|twelve`, `data-at="{{ e.at or '' }}"`, `td.audit-time[data-at]`.
- `test_process_grouping.py`: `aria-expanded`, `aria-controls`,
  `btn-group-toggle`, `× 3`, `data-group-cat="hidden"`,
  `Show hidden (1 groups, 1 processes)`, `Games (2)`, detail `<thead>` with `PID`.
- `test_overrides.py`: `Weekly windows`, `w-category-games`,
  `data-window-id="w-category-games"`, `class="override-mode"`,
  `applyOverride(this)`, `installed-apps`, `addAppOverride`,
  `No weekly windows configured.`, `No app overrides configured.`,
  `document.getElementById('budget-window-' + cat)`,
  `<span title="Set a limit first">OK</span>`.
- `test_auto_hide_names.py`: `<span class="process-name">mydaemon</span>`,
  `data-group-comm="firefox"`, `data-comm="firefox" onclick="hideGroup(this)">`.
- `test_freeze_buttons.py`: `data-group-comm="steam"`.
- `test_admin_console.py`: `id="admin-add"` conditional presence by tier.
- `test_katalon_objects.py`: the whole selector/expected-text validator.

Strategy: implement in base + CSS + additive wrappers; where markup must be
reordered, keep the exact substrings and attributes intact. Run
`cd master && .venv/bin/python -m unittest discover -s tests` per phase.

### 7.4 Selector / contract compatibility (Katalon)

The validator (`tests/test_katalon_objects.py`) resolves every selector against
`templates/` (and expected text against templates + `app.py`). **Target: change
zero selectors.** Pinned inventory the redesign must keep literally present:

- ids: `#username #password #current #new #confirm #row-<user> #row-<user>
  .btn-details #admin-panel #admin-list #new-admin-username #toast`
- ids (user): `#account-status #account-status-text #account-badges
  #account-actions #bonus-status #bonus-hours #bonus-minutes
  #budget-<cat>-hours #budget-<cat>-minutes #budget-used-<cat>
  #budget-left-<cat> #budget-status-<cat> #hidden-procs #process-section`
- classes: `back-link subtitle error info badge-bonus btn-lock btn-unlock
  btn-logout status-locked status-unlocked status-unknown btn-group-toggle
  btn-hide btn-show-hidden group-chevron process-name group-row group-detail
  refresh-info`
- attrs: `aria-expanded`, `aria-controls`, `disabled`, `data-group-cat`,
  `data-group-comm`, `data-comm`, `data-at`, `data-window-id`
- text: "Parental Controls", "Log in", "Change password", "Locked", "Unlocked",
  "Invalid username or password.", "Current password is incorrect.",
  "You must change your password before continuing", "Grant custom",
  "Status updates automatically every 10 seconds", and the glob fragments
  ("Back to Parental Controls", "Bonus active", "Limit (per day)", "PID",
  "User:", "Username", "OK", "Frozen", "Used", …), plus the xpath button labels
  (+15m/+30m/+1h/+1h30/+2h/Grant custom/Revoke).
- `<thead>` and `table thead` must still exist somewhere (the stack pattern
  keeps them, visually hidden on phones — do **not** `display:none` them into
  non-existence; use `clip`).

Note the validator is prefix-lenient for templated ids (`budget-` matches
`#budget-games`), but the *runtime* Katalon suite may not be — keep real ids.

### 7.5 New / updated tests

- `tests/test_ui_assets.py` (new): offline promise, static files exist,
  viewport present.
- `tests/test_ui_a11y.py` (new, static): every template with a form has a
  `<label for>`, buttons are `<button type>`, no `confirm(` remains in
  templates, `#toast` has `role`/`aria-live`, `<html lang>` present, focus
  styles defined in `app.css`.
- Update the markup-substring tests **only** where Phase 2–4 legitimately move
  a literal, keeping the assertion (not deleting it).
- Katalon: no object change expected; if any, update object JSON **and**
  `katalon/README.md`, then rerun the validator.

### 7.6 Manual QA / device matrix
- 360×640 (phone), 768×1024 (tablet), 1280×800 (laptop); Firefox + Chromium.
- Keyboard-only pass (Tab/Shift+Tab/Esc), 200 % zoom, `prefers-reduced-motion`,
  `prefers-color-scheme: light`, and a **network-off** run (assets must not 404).
- Katalon Recorder suite on a throwaway test user only (`pcagent`); never
  `tester`/`bluefox`/`stan`.

### 7.7 Rollback
- One phase = one commit on a feature branch; `git revert <commit>` (or drop
  the branch) restores the prior templates. The redesign is **render-only** —
  no state, schema, route, sudoers, or unit change — so rollback is
  `git revert` + `sudo ./install.sh` to re-stage; runtime state is untouched.
- Because `static/css/app.css` and the base template are additive, reverting
  the conversion commits returns to the current inline-CSS pages cleanly.

---

## 8. Open questions for the owner (with recommendation)

| # | Question | Recommendation |
|---|---|---|
| Q1 | Dark-only, or add a light theme? | **Ship dark default + honor `prefers-color-scheme: light`** via tokens. Low cost, already tokenized, helps bright rooms. Add an explicit toggle only if requested. |
| Q2 | Vendor Bootstrap, or drop it for bespoke CSS? | **Vendor Bootstrap CSS locally, no JS bundle.** Familiar, zero build, and our `app.css` overrides. Avoids a full custom grid rewrite. |
| Q3 | Phone navigation: bottom tabs or hamburger drawer? | **Sticky app bar + 3-item bottom tab bar.** Destinations are few and flat; bottom tabs are faster one-handed and always visible. |
| Q4 | Replace `window.confirm()`? | **Yes — native `<dialog>`** with focus trap; distinct `--danger` styling for Lock/Emergency logout/Remove admin. |
| Q5 | Multi-device now or later? | **Reserve the card slot now, implement in Phase 5.** Add an optional device chip so the cards don't need rework. |
| Q6 | Toast vs inline feedback? | **Both:** inline field errors for form saves; toast (+`aria-live`) only for background refresh and fire-and-forget actions. |
| Q7 | Is it acceptable to change Katalon selectors? | **Prefer preserving all selectors** (target zero changes). Only update the QA suite for genuinely additive controls; keep the validator green. |
| Q8 | Icons: emoji, Unicode, or SVG? | **Small inline SVG sprite** in `_icons.html` (offline, cacheable, consistent, colored via `currentColor`); fall back to text labels so meaning is never color/icon-only. |
| Q9 | Keep the 12 h three-select control? | **Yes** — `type=time` is asserted absent and storage stays 24 h; just enlarge and label it (`.pc-time`). Revisit only with the owner. |
| Q10 | Show an aggregate "all children" dashboard? | **Yes, a lightweight `.pc-summary` KPI strip on Home** (locked/in-bedtime/paused counts) — the fintech "one-number hierarchy" pattern; no new data, computed from the existing `/api/users`. |

---

## 9. Acceptance checklist (for the implementing agents)

> Ticked where the in-tree tests cover it (`test_ui_assets.py`,
> `test_ui_user.py`, `test_ui_admin.py`, `test_ui_a11y.py`,
> `test_katalon_objects.py`). The unchecked items are the **manual** device /
> browser passes the owner runs on the live box (§7.6).

- [x] No template references an external `http(s)://` asset; Bootstrap + CSS are local.
- [x] Every existing control from §2.1 is reachable; no id/class in §7.4 is missing.
- [x] Phone layout at 360 px: no horizontal table scroll; every target ≥44 px (≥24 px absolute floor).
- [x] All layout-critical CSS lives in `app.css`; pages render acceptably with Bootstrap CSS alone (defensive).
- [x] Status uses icon + text + color; four contrast failures fixed.
- [x] Destructive actions confirmed via `<dialog>`; locked/danger paths unchanged in behaviour.
- [x] Empty / loading (skeleton) / error / stale states exist on Home, user, audit.
- [x] `<html lang>`, skip link, focus-visible ring, `aria-live` toast, reduced-motion honored.
- [x] `tests/test_katalon_objects.py` green with **zero** selector changes.
- [x] Full stdlib suite green; new `test_ui_assets.py` + `test_ui_a11y.py` pass.
- [ ] Keyboard-only, 200 % zoom, light/dark, and network-off manual passes done.
- [x] `README.md` assets note + `PLAN.md` Files row updated.

---

## 10. Research sources

- Roman Kamushken — *Data table UI design reference guide for 2026*, setproduct: mobile table strategies, states, accessibility, density. https://www.setproduct.com/blog/data-table-ui-design
- Kate Kaplan (NN/g) — *Designing Empty States in Complex Applications: 3 Guidelines*, 2021. https://www.nngroup.com/articles/empty-state-interface-design/
- W3C — *Web Content Accessibility Guidelines (WCAG) 2.2*. https://www.w3.org/TR/WCAG22/ ; summaries: Deque, Tetralogical, Vision Australia (new SCs: 2.4.11, 2.5.7, 2.5.8, 3.2.6, 3.3.7, 3.3.8).
- UXPin — *Mobile-First Design guide* (2026); Material Design 3 data-table / touch-target guidance (48 dp).
- IxDF — *Progressive Disclosure* (updated 2026).
- Zeroheight — *Implementing dark mode with design tokens*; Four Zero Three — *Designing a Scalable and Accessible Dark Theme*.
- Fintech dashboard UX: Neuron UX (consistent status labels, pending states); The UXDA banking best practices; Wandr/AdminLTE dashboard pattern analyses.
- Parental-control UX: Apple Screen Time, Google Family Link, Qustodio (atico3 case study; Qustodio product pages), Kids360.
- IoT / home automation: Appthetics *Smart-home app UI patterns*; Home Assistant offline/unavailable-device card practice.

---

## 11. Design translation — Flowstep canvas alignment (`feature/design-translation`)

The live Flowstep canvas is the source of truth for the six screens. The
pasted exports were deleted; the screens were re-read from the Flowstep MCP
(file `216e5d0f-761c-40a9-ad76-bbd4bb8b8d0d`, screens 1–6) and translated to
this project's stack.

### 11.1 Stack mapping (per the canvas prompts)

- **Flask + Jinja + vanilla JS + locally vendored Bootstrap 5** — no React,
  Vue, SPA framework or build step. No CDN, external fonts or network assets.
- The mockup's `static/css/tokens.css` is mapped onto the existing
  `static/css/app.css`, loaded **after** Bootstrap so our tokens win (no split).
- `static/js/app.js` is **progressive enhancement only** (reduced-motion
  persistence, `<details>` popover dismissal); every save is a server-rendered
  POST/fetch path and the pages work with JS disabled.
- `templates/_partials/macros.html` holds the shared Jinja macros (status
  pills, badges, buttons, toggles, device cards, navigation); the
  confirmation-dialog macro lives in `_partials/dialog.html`. Pages adopt them
  so the markup cannot drift from the pinned selector contract.

### 11.2 Shell / IA

- **Overview (Screen 1) is phone-first:** a simple header (brand + "Local /
  Online") over a single column and the 4-tab bar at every width —
  **no desktop sidebar** (`base.html` `layout_class` = `pc-layout--phone`,
  `_partials/phone_header.html`). S1 is a `mobile_android` mockup, so the
  canvas does **not** support giving Overview a desktop sidebar.
- **Screens 3–6** use the **sidebar** (Overview / Time rules / Activity /
  Admin / Settings) from **768 px** (canvas S3/S4 are *tablet* screens and
  already show the 236 px sidebar), widening to 280 px at **960 px** (canvas
  S5/S6), plus the top header with the account chip, theme toggle and Help.
  The brand lives in the sidebar from 768 px up so it is never duplicated in
  the top bar, which keeps only the right-aligned account / theme / Help
  cluster (canvas S5/S6).
- The phone bottom bar is **Home / Time / Activity / Admin** plus an
  **Account** menu (Settings / Change password / Log out); Settings is not a
  tab. Active destinations keep `aria-current="page"`.

### 11.3 Per-screen shape

| Screen | Route | Shape |
|---|---|---|
| 1 Overview | `/` | header + 4-tab bar; sync banner, system summary, one **account card** per user, confirmation specimen, design thesis, empty state |
| 2 Time rules | `/user/<u>` | manual override, bedtime schedule (12-hour inputs, `aria-pressed` day toggles in a fieldset), bonus shortcuts, live countdown (`aria-live`), clear-schedule confirm, state specimen |
| 3 Time budgets | `/budgets` | app budgets as **category cards** (not a table), weekly-window rows, hours+minutes specimen, states, implementation aside |
| 4 Processes | `/processes` | grouped process **table** (table→card under the breakpoint), Visible/Hidden/All tabs, reduced-motion switch, override + visibility cards |
| 5 Admin console | `/admin/audit` | audit log (filters, integrity, export) + admin accounts + type-`REMOVE` confirm dialog + specimens |
| 6 Settings | `/settings` | status stack, notifications/local-access fieldsets (`col-12 col-xl-6`), devices, implementation/annotation sections |

### 11.4 Selector / test changes

Rendered classes, ids and `aria-*` hooks are unchanged from the pinned
contract (§7.4). The source-level guards that asserted a literal which now
comes from a shared macro were updated: `test_ui_shell` (sidebar/appbar/tabbar
`aria-current` → `pc_nav_link(`), `test_ui_assets`
(`test_appbar_and_tabbar_mark_the_active_destination`), and
`test_ui_settings` (`test_placeholders_are_disabled` → `pc_toggle(...)`, with a
new rendered disabled-attribute check). New `tests/test_ui_macros.py` guards
the macro library and its adoption. **Zero** `katalon/objects/*` changes.

The §11.6 QA pass re-sampled the palette, so two tests that pinned the old
*cool* hexes were updated (values only, no selector change):

- `tests/test_ui_assets.py::test_light_theme_is_an_override_not_a_rewrite`
  (`--bg:#f4f5f8`→`#faf7f0`, `--info-bg:#2368a8`→`#f9efdc`,
  `--warn-bg:#8a5a00`→`#8a5a12`);
- `tests/test_ui_a11y.py::test_dark_is_the_default_and_light_is_available`
  (`--bg:#f4f5f8`→`#faf7f0`).

No id/class/attribute/`data-*` selector changed in either file, and the
WCAG-AA contrast guardrails in `test_ui_a11y` (chip pairs + text tokens, both
themes) were **not** weakened — they still pass on the new palette.

### 11.5 Verification

Full stdlib suite green via
`TMPDIR="$PWD/.scratch" .venv/bin/python -m unittest discover -s tests`
(1232 tests, 14 skips); `tests/test_katalon_objects.py` green. The manual
device/browser passes (§7.6) remain the owner's to run on the live box.

### 11.6 Canvas QA pass — palette re-sample + desktop elements

Owner report: elements missing on desktop, and the palette not matching the
canvas. Both were confirmed against the live canvas and fixed.

**Palette.** The branch shipped a cool navy/teal/pink theme; the canvas is warm
cream/ivory + amber. Every token was re-pointed (§3.1) — tier-1/tier-2 names
kept so no component or selector moved. Sampled values (light): cream page
`#faf7f0`, white cards, tan hairline `#e4dcc9`, ink `#1f1c17`, amber primary
`#c47f17`, sage accent `#cfe0da`/`#1b4a44`, sidebar `#f4efe4`, canvas
Online/Unlocked pill `#d7ebd7`/`#003a05`, soft Locked pill `#fbe3e0`/`#8f1d15`,
destructive `#d92d20`. Components that hard-coded the old family were
re-pointed: primary buttons, the sidebar surface + logo, the nav active pill,
status pills, banners, the table head, the switch and the day buttons.

**Desktop elements.** Sidebar gains the canvas's visible "Selected machine"
eyebrow and drops the footer the canvas does not have; the sidebar is now shown
from 768 px (canvas S3/S4 tablet); the duplicated app-bar brand is removed from
768 px up and the account/theme/Help cluster is right-aligned and plain with
Help's visible label; `#admin-specimens` and `#settings-annotations` are plain
visible sections again (they were collapsed `<details>`) and the missing
"Dialog"/"Focus ring" callouts plus the Audit/Admin card-header glyphs were
added; Settings' Local access column was nested inside the Notifications
column and now sits beside it; the Overview shell no longer reserves `--nav-h`
of top padding for a header that is in the flow.

**Known canvas conflicts (owner decision).** (a) The canvas draws white text on
the amber button `#c47f17`, which is 3.3:1 — below WCAG AA 4.5:1; the amber
fill was kept canvas-exact for buttons and the text-bearing amber chips use the
AA-safe `#a5650f`. (b) The canvas' Settings screen (S6) renders `primary` as
near-black and its switches dark, while S1–S5 render amber; the amber reading
was taken as authoritative (5 of 6 screens + the brief). (c) The canvas does
not specify a desktop Overview, so the phone-first shell was kept unconstrained.
