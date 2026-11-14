/* ==========================================================================
   static/js/calendar.js — the /calendar page (CALENDAR_DESIGN.md 'UI slice').

   Vanilla ES module, no build step, matching the app.js conventions: page
   wiring lives here, the vendored FullCalendar 6.1.22 global bundle
   (static/vendor/fullcalendar/) renders the grid. No CDN, no network except
   same-origin /api/schedule calls.

   Data flow:
     * GET /api/schedule?user       -> block cache (series definitions)
     * GET /api/schedule/events     -> expanded occurrences for the visible
                                       range (+ the derived bonus band)
     * POST /api/schedule           -> create/edit the WHOLE series (v1 has
                                       no per-occurrence exceptions)
     * DELETE /api/schedule/<id>    -> remove the series (confirmed)

   Polling choice: refresh on every interaction + a gentle 60 s timer — the
   grid has minute resolution and mutations refetch immediately; the 10 s
   contract belongs to the Overview status poll and is untouched here. The
   timer pauses while the editor dialog is open (never clobber half-typed
   input) and on hidden tabs; `nowIndicator` is client-side so the marker
   moves between polls without network.

   Fail closed: a failed events fetch shows #calendar-error — a display
   problem is never allowed to read as "no restrictions". A rejected inline
   move/resize reverts the grid by refetching and shows the API's message
   (overlap conflicts included) in the notice strip, not in a vanishing alert.
   ========================================================================== */
const CFG = JSON.parse(document.getElementById('calendar-config').textContent);
const CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

const REFRESH_MS = 60000;          // gentle grid poll (see header note)
const BEDTIME = 'bedtime';
const DAY_CODES = CFG.dayCodes;    // MO..SU == date.weekday() order (schedule.py)

// Palette: bedtime reuses the app's --bedtime-* tokens' colours so the grid
// reads like the chips elsewhere; categories get distinct mid-tone hues that
// keep contrast in both themes; the derived bonus band is striped blue.
const PALETTE = {
  bedtime: { bg: '#3b2f63', fg: '#ddd6fe' },
  games:   { bg: '#92400e', fg: '#fef3c7' },
  browsers:{ bg: '#155e75', fg: '#cffafe' },
  media:   { bg: '#9d174d', fg: '#fce7f3' },
  chat:    { bg: '#3f6212', fg: '#ecfccb' },
  other:   { bg: '#3f3f46', fg: '#e4e4e7' },
  bonus:   { bg: '#2563eb', fg: '#ffffff' },
};

let calendar = null;
let blocksById = new Map();   // id -> stored block (series truth)
let invalidIds = [];          // stored-but-unreadable blocks (fail-closed)
let editorState = null;       // {mode:'create'|'edit', block?, occStart?}
let pollTimer = null;

/* ---------------- helpers ---------------- */

const pad2 = n => String(n).padStart(2, '0');
function localDate(d) {  // 'YYYY-MM-DD' from a Date in the *browser* zone
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
}
function localDateTime(d) {
  return localDate(d) + 'T' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':00';
}
function hhmm(d) { return pad2(d.getHours()) + ':' + pad2(d.getMinutes()); }
function hm(minutes) {
  const m = parseInt(minutes, 10) || 0;
  const h = Math.floor(m / 60);
  return h ? (h + 'h' + (m % 60 ? ' ' + (m % 60) + 'm' : '')) : m + 'm';
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function toast(msg, err) { if (window.pcToast) { window.pcToast(msg, err ? 'error' : 'success'); } }

function setError(message) {
  const banner = document.getElementById('calendar-error');
  if (!banner) { return; }
  const text = document.getElementById('calendar-error-text');
  if (text) {
    text.textContent = message
      ? 'Calendar could not refresh: ' + message + '. Restrictions still enforce exactly as configured — this is a display problem, not a permissions change.'
      : 'The schedule could not be loaded. Restrictions still enforce exactly as configured — this is a display problem, not a permissions change.';
  }
  banner.hidden = !message;
}
function setNotice(html) {  // trusted innerHTML: callers pass escaped strings
  const strip = document.getElementById('calendar-notice');
  const text = document.getElementById('calendar-notice-text');
  if (!strip || !text) { return; }
  text.innerHTML = html || '';
  strip.hidden = !html;
}

async function api(path, options) {
  const resp = await fetch(path, options);
  let data = null;
  try { data = await resp.json(); } catch (e) { /* non-JSON error page */ }
  if (!resp.ok || !data || data.success !== true) {
    const msg = (data && data.error) ? data.error : ('HTTP ' + resp.status);
    const err = new Error(msg);
    err.data = data;
    throw err;
  }
  return data;
}

/* ---------------- data loading ---------------- */

async function loadBlocks() {
  const url = '/api/schedule?username=' + encodeURIComponent(CFG.username);
  const data = await api(url);
  const view = (data.schedule || {})[CFG.username] || null;
  blocksById = new Map();
  invalidIds = [];
  if (view) {
    for (const b of (view.events || [])) { blocksById.set(b.id, b); }
    invalidIds = view.invalid || [];
  }
  renderInvalidNotice();
}

function renderInvalidNotice() {
  if (!invalidIds.length) { setNotice(''); return; }
  setNotice('<svg class="pc-chip__icon" aria-hidden="true"><use href="#pc-i-alert"/></svg> '
    + invalidIds.length + ' stored rule' + (invalidIds.length > 1 ? 's are' : ' is')
    + ' unreadable (' + esc(invalidIds.join(', ')) + ') and are <strong>not enforced</strong>. '
    + 'Remove them with Delete (calendar id shown) or re-create them.');
}

function targetLabel(target) {
  if (target === BEDTIME) { return 'Bedtime'; }
  if (target.indexOf('app:') === 0) {
    const comm = target.slice(4);
    const cat = (CFG.appCategories || {})[comm] || 'other';
    return comm + ' · ' + (CFG.categoryLabels[cat] || cat);
  }
  return CFG.categoryLabels[target] || target;
}
function paletteFor(target) {
  if (target === BEDTIME) { return PALETTE.bedtime; }
  if (target.indexOf('app:') === 0) {
    return PALETTE[(CFG.appCategories || {})[target.slice(4)] || 'other'];
  }
  return PALETTE[target] || PALETTE.other;
}
function isCategoryTarget(target) { return target !== BEDTIME && target.indexOf('app:') !== 0; }

function toCalendarEvent(row) {
  const pal = row.derived ? PALETTE.bonus : paletteFor(row.target);
  let title;
  if (row.derived) {
    title = 'Bonus time';
  } else if (row.target === BEDTIME) {
    title = 'Bedtime';
  } else if (row.mode === 'permitted') {
    title = targetLabel(row.target) + ' · unlimited';
  } else {
    title = targetLabel(row.target) + (row.allowance_minutes ? ' · ' + hm(row.allowance_minutes) : '');
  }
  const ev = {
    id: row.block_id + '@' + row.start,
    title: title,
    start: row.start,
    end: row.end,
    backgroundColor: pal.bg,
    borderColor: pal.bg,
    textColor: pal.fg,
    classNames: row.derived ? ['pc-cal-event', 'pc-cal-event--bonus']
      : (row.target === BEDTIME ? ['pc-cal-event', 'pc-cal-event--bedtime'] : ['pc-cal-event']),
    editable: !row.derived,
    startEditable: !row.derived,
    durationEditable: !row.derived,
    display: 'block',
    extendedProps: {
      blockId: row.block_id,
      derived: !!row.derived,
      target: row.target,
      occStart: row.start,
      occEnd: row.end,
    },
  };
  return ev;
}

async function fetchEvents(info) {
  const params = new URLSearchParams({
    username: CFG.username,
    start: localDate(info.start),
    end: localDate(info.end),
  });
  const data = await api('/api/schedule/events?' + params.toString());
  return (data.events || []).map(toCalendarEvent);
}

async function refreshGrid(revertOnError) {
  try {
    await loadBlocks();
    if (calendar) { calendar.refetchEvents(); }
    setError('');
  } catch (e) {
    if (revertOnError && calendar) { calendar.refetchEvents(); }
    setError(e.message);
  }
}

/* ---------------- series moves (drag / resize) ---------------- */

function minutesOf(hhmmStr) {
  const parts = String(hhmmStr || '').split(':');
  if (parts.length !== 2) { return null; }
  const h = parseInt(parts[0], 10), m = parseInt(parts[1], 10);
  if (isNaN(h) || isNaN(m)) { return null; }
  return h * 60 + m;
}
function addDays(isoDate, days) {
  const d = new Date(isoDate + 'T12:00:00');
  d.setDate(d.getDate() + days);
  return localDate(d);
}

async function commitSeries(block, patch, what) {
  const merged = Object.assign({}, block, patch);
  const body = new URLSearchParams({
    username: CFG.username,
    id: merged.id,
    target: merged.target,
    preset: merged.preset,
    dtstart: merged.dtstart,
    start: merged.start,
    end: merged.end,
    enabled: merged.enabled ? '1' : '0',
  });
  if (merged.target !== BEDTIME && merged.mode) { body.set('mode', merged.mode); }
  if (merged.allowance_minutes != null && merged.allowance_minutes !== '') {
    body.set('allowance_minutes', String(merged.allowance_minutes));
  }
  if (merged.until) { body.set('until', merged.until); }
  try {
    await api('/api/schedule', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRF-Token': CSRF },
      body: body.toString(),
    });
    toast(what, false);
    await refreshGrid(true);
  } catch (e) {
    // Rejected (overlap / validation): revert the grid to the stored truth.
    setNotice('');
    await refreshGrid(true);
    toast(what + ' rejected: ' + e.message, true);
  }
}

/* Recurring series shifted by whole days: DAILY/ONE need no preset change
   (the dtstart shift already carries them); weekday-bearing presets rotate
   their BYDAY set so the series lands where the parent dragged it. */
function bydayPreset(freq, days) {
  const codes = Array.from(new Set(days)).sort((a, b) => a - b)
    .map(i => DAY_CODES[i]).join(',');
  return freq + ':BYDAY=' + codes;
}
function rotatePreset(preset, dayShift) {
  const shift = ((dayShift % 7) + 7) % 7;
  if (!shift) { return null; }
  const text = String(preset || '');
  if (text === 'ONE' || text === 'DAILY') { return null; }
  if (text === 'SCHOOL') {
    return bydayPreset('WEEKLY', [0, 1, 2, 3, 4].map(d => (d + shift) % 7));
  }
  const m = text.match(/^(WEEKLY|BIWEEKLY):BYDAY=([A-Z,]+)$/);
  if (!m) { return null; }
  const days = m[2].split(',').map(c => DAY_CODES.indexOf(c)).filter(i => i >= 0)
    .map(d => (d + shift) % 7);
  return bydayPreset(m[1], days);
}

async function onSeriesChanged(event, deltaMin, resized) {
  const p = event.extendedProps;
  const block = blocksById.get(p.blockId);
  if (!block || p.derived) { await refreshGrid(true); return; }
  const oldStartMin = minutesOf(block.start);
  const oldEndMin = minutesOf(block.end);
  if (oldStartMin === null || oldEndMin === null) { await refreshGrid(true); return; }
  const patch = {};
  if (!resized) {
    const dayShift = Math.floor(deltaMin / 1440);
    const timeShift = deltaMin - dayShift * 1440;
    patch.start = hhmmOfMin(oldStartMin + timeShift);
    patch.end = hhmmOfMin(oldEndMin + timeShift);
    if (dayShift) {
      // Recurring series: shift the anchor (dtstart) by whole days; ONE's
      // dtstart IS the day, so the same rule works for every preset.
      patch.dtstart = addDays(block.dtstart, dayShift);
      const rotated = rotatePreset(block.preset, dayShift);
      if (rotated) { patch.preset = rotated; }
    }
  } else {
    patch.end = hhmmOfMin(oldEndMin + deltaMin);
  }
  const verb = resized ? 'Resize' : 'Move';
  await commitSeries(block, patch, verb + ' applied to the whole series');
}
function hhmmOfMin(total) {
  const m = ((total % 1440) + 1440) % 1440;
  return pad2(Math.floor(m / 60)) + ':' + pad2(m % 60);
}

/* ---------------- editor dialog ---------------- */

function $(id) { return document.getElementById(id); }

function parsePresetToForm(preset) {
  const text = String(preset || 'ONE');
  if (text === 'DAILY') { return { repeat: 'daily', days: [] }; }
  if (text === 'SCHOOL') { return { repeat: 'school', days: [] }; }
  if (text === 'ONE') { return { repeat: 'one', days: [] }; }
  const m = text.match(/^(WEEKLY|BIWEEKLY):BYDAY=([A-Z,]+)$/);
  if (m) {
    const days = m[2].split(',').map(c => DAY_CODES.indexOf(c)).filter(i => i >= 0);
    return { repeat: m[1] === 'WEEKLY' ? 'weekly' : 'biweekly', days: days };
  }
  return { repeat: 'one', days: [] };
}

function buildPreset() {
  const repeat = $('block-repeat').value;
  if (repeat === 'daily') { return 'DAILY'; }
  if (repeat === 'school') { return 'SCHOOL'; }
  if (repeat === 'one') { return 'ONE'; }
  const days = selectedDays().sort((a, b) => a - b);
  const codes = days.map(i => DAY_CODES[i]).join(',');
  if (!codes) { return null; }
  return (repeat === 'weekly' ? 'WEEKLY:BYDAY=' : 'BIWEEKLY:BYDAY=') + codes;
}

function selectedDays() {
  return Array.from(document.querySelectorAll('.block-day:checked')).map(c => parseInt(c.value, 10));
}

function setTimeSelect(prefix, hhmmStr) {
  const mins = minutesOf(hhmmStr);
  const hourSel = document.querySelector(`[name="${prefix}_hour"]`);
  const minSel = document.querySelector(`[name="${prefix}_minute"]`);
  const merSel = document.querySelector(`[name="${prefix}_meridiem"]`);
  if (!hourSel || !minSel || !merSel) { return; }
  if (mins === null) {
    hourSel.value = ''; minSel.value = ''; merSel.value = '';
    return;
  }
  let h = Math.floor(mins / 60);
  const mer = h >= 12 ? 'PM' : 'AM';
  h = h % 12; if (h === 0) { h = 12; }
  hourSel.value = String(h);
  minSel.value = pad2(mins % 60);
  merSel.value = mer;
}
function readTimeSelect(prefix) {
  const hour = document.querySelector(`[name="${prefix}_hour"]`).value;
  const minute = document.querySelector(`[name="${prefix}_minute"]`).value;
  const meridiem = document.querySelector(`[name="${prefix}_meridiem"]`).value;
  if (hour === '' || minute === '' || meridiem === '') { return null; }
  let h24 = parseInt(hour, 10) % 12;
  if (meridiem === 'PM') { h24 += 12; }
  return pad2(h24) + ':' + pad2(parseInt(minute, 10));
}

function showEditorError(message) {
  const el = $('block-editor-error');
  if (!el) { return; }
  el.textContent = message || '';
  el.hidden = !message;
}

function openEditor(opts) {
  editorState = opts;
  const dlg = $('pc-block-editor');
  showEditorError('');
  const block = opts.block || null;
  $('block-delete').hidden = !block;
  $('pc-block-editor-title').textContent = block ? 'Edit series' : 'New block';

  let target = BEDTIME, mode = 'capped', allowance = '', enabled = true;
  let startDate = opts.dtstart || localDate(new Date());
  let startTime = opts.start || '', endTime = opts.end || '';
  let repeat = 'one', days = [], until = '';
  if (block) {
    target = block.target;
    mode = block.mode || 'capped';
    allowance = block.allowance_minutes == null ? '' : String(block.allowance_minutes);
    enabled = !!block.enabled;
    startDate = block.dtstart;
    const parsed = parsePresetToForm(block.preset);
    repeat = parsed.repeat; days = parsed.days;
    until = block.until || '';
  }

  // Target select: bedtime | category | app. App target stored as app:<comm>.
  const targetSel = $('block-target');
  if (target === BEDTIME || isCategoryTarget(target)) {
    targetSel.value = target;
    $('block-app').value = '';
  } else if (target.indexOf('app:') === 0) {
    targetSel.value = 'app';
    $('block-app').value = target.slice(4);
  } else {
    targetSel.value = 'other';
  }
  for (const r of document.querySelectorAll('[name="block-mode"]')) { r.checked = (r.value === mode); }
  $('block-allowance-hours').value = allowance === '' ? '' : String(Math.floor(parseInt(allowance, 10) / 60));
  $('block-allowance').value = allowance === '' ? '' : String(parseInt(allowance, 10) % 60);
  setTimeSelect('block_start', startTime);
  setTimeSelect('block_end', endTime);
  $('block-repeat').value = repeat;
  for (const c of document.querySelectorAll('.block-day')) { c.checked = days.includes(parseInt(c.value, 10)); }
  $('block-until').value = until;
  $('block-enabled').checked = enabled;
  editorState.dtstart = startDate;
  $('block-editor-sub').textContent =
    (block ? 'Changes apply to the WHOLE series (' + block.id + ').'
           : 'Starting ' + startDate)
    + (repeat === 'one' ? '' : ' — repeats apply from this anchor day.');

  syncEditorVisibility();
  dlg.showModal();
}

function syncEditorVisibility() {
  const target = $('block-target').value;
  const isBedtime = target === BEDTIME;
  const isApp = target === 'app';
  $('block-app-field').hidden = !isApp;
  $('block-mode-field').hidden = isBedtime;
  const mode = (document.querySelector('[name="block-mode"]:checked') || {}).value || 'capped';
  $('block-allowance-row').hidden = isBedtime || mode !== 'capped';
  const repeat = $('block-repeat').value;
  $('block-days-field').hidden = !(repeat === 'weekly' || repeat === 'biweekly');
}

function closeEditor() {
  const dlg = $('pc-block-editor');
  if (dlg.open) { dlg.close(); }
  editorState = null;
}

async function submitEditor(event) {
  event.preventDefault();
  if (!editorState) { return; }
  showEditorError('');
  const targetSel = $('block-target').value;
  let target = targetSel;
  if (targetSel === 'app') {
    const comm = $('block-app').value.trim();
    if (!comm) { showEditorError('Enter an app command (letters, digits, dot, dash, underscore).'); return; }
    target = 'app:' + comm;
  }
  const start = readTimeSelect('block_start');
  const end = readTimeSelect('block_end');
  if (!start || !end) { showEditorError('Pick a start and an end time.'); return; }
  const repeat = $('block-repeat').value;
  const preset = buildPreset();
  if (!preset) { showEditorError('Pick at least one day for this repeat.'); return; }
  const mode = (document.querySelector('[name="block-mode"]:checked') || {}).value || 'capped';
  let allowance = '';
  if (target !== BEDTIME && mode === 'capped') {
    const h = parseInt($('block-allowance-hours').value || '0', 10) || 0;
    const m = parseInt($('block-allowance').value || '0', 10) || 0;
    allowance = String(h * 60 + m);
  }
  const body = new URLSearchParams({
    username: CFG.username,
    target: target,
    preset: preset,
    dtstart: editorState.dtstart || localDate(new Date()),
    start: start,
    end: end,
    enabled: $('block-enabled').checked ? '1' : '0',
  });
  if (editorState.block) { body.set('id', editorState.block.id); }
  if (target !== BEDTIME) { body.set('mode', mode); }
  if (allowance !== '') { body.set('allowance_minutes', allowance); }
  const until = $('block-until').value;
  if (until) { body.set('until', until); }
  try {
    const data = await api('/api/schedule', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRF-Token': CSRF },
      body: body.toString(),
    });
    closeEditor();
    toast((data.message || 'Block saved'), false);
    await refreshGrid(true);
  } catch (e) {
    // Overlap / validation errors surface IN the modal (the API message
    // carries the conflicting block id), never as a vanishing alert.
    showEditorError(e.message);
  }
}

async function deleteSeries() {
  if (!editorState || !editorState.block) { return; }
  const block = editorState.block;
  const ok = await window.pcConfirm({
    title: 'Delete this series?',
    body: 'Removes every occurrence of "' + targetLabel(block.target)
      + '" (' + block.id + '). Enforcement stops at the next tick.',
    confirmLabel: 'Delete series',
    danger: true,
  });
  if (!ok) { return; }
  const path = '/api/schedule/' + encodeURIComponent(block.id)
    + '?username=' + encodeURIComponent(CFG.username);
  try {
    const data = await api(path, {
      method: 'DELETE',
      headers: { 'X-CSRF-Token': CSRF },
    });
    closeEditor();
    toast(data.message || 'Series deleted', false);
    await refreshGrid(true);
  } catch (e) {
    showEditorError('Delete failed: ' + e.message);
  }
}

/* ---------------- bonus band hint ---------------- */

function showBonusHint() {
  setNotice('<svg class="pc-chip__icon" aria-hidden="true"><use href="#pc-i-clock"/></svg> '
    + 'This band is <strong>bonus time</strong>: live bedtime state, not a stored '
    + 'series, so it is read-only here. Grant or revoke it with the bonus dialog on '
    + '<a href="/user/' + encodeURIComponent(CFG.username) + '">' + esc(CFG.username)
    + '’s Time rules page</a>.');
}

/* ---------------- boot ---------------- */

function startPolling() {
  stopPolling();
  pollTimer = setInterval(() => {
    if (editorState) { return; }                    // never clobber open edits
    if (document.hidden) { return; }                // gentle: tabs in background rest
    refreshGrid(false);
  }, REFRESH_MS);
}
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

function boot() {
  const el = document.querySelector('[data-pc-calendar]');
  if (!el || typeof FullCalendar === 'undefined') { return; }
  calendar = new FullCalendar.Calendar(el, {
    initialView: 'timeGridWeek',
    height: 'auto',
    firstDay: 1,                       // school-week anchor: Mon
    nowIndicator: true,
    slotMinTime: '00:00:00',
    slotMaxTime: '24:00:00',
    expandRows: true,
    headerToolbar: {
      left: 'prev,next today',
      center: 'title',
      right: 'timeGridWeek,dayGridMonth',
    },
    buttonText: { today: 'Today', timeGridWeek: 'Week', dayGridMonth: 'Month' },
    editable: true,
    eventStartEditable: true,
    eventDurationEditable: true,
    selectMirror: true,
    dayMaxEventRows: 4,
    events: async (info, success, failure) => {
      try {
        const evs = await fetchEvents(info);
        setError('');
        success(evs);
      } catch (e) {
        setError(e.message);            // fail closed: explicit banner, never "empty = free"
        failure(e.message);
      }
    },
    select: (info) => {
      const allDay = info.allDay;
      openEditor({
        mode: 'create',
        dtstart: localDate(info.start),
        start: allDay ? '' : hhmm(info.start),
        end: allDay ? '' : hhmm(info.end > info.start ? info.end : info.start),
      });
    },
    eventDrop: (info) => {
      const p = info.event.extendedProps;
      if (p.derived) { info.revert(); return; }
      const oldStart = new Date(p.occStart);
      const deltaMin = Math.round((info.event.start - oldStart) / 60000);
      onSeriesChanged(info.event, deltaMin, false);
    },
    eventResize: (info) => {
      const p = info.event.extendedProps;
      if (p.derived) { info.revert(); return; }
      const oldEnd = new Date(p.occEnd);
      const deltaMin = Math.round((info.event.end - oldEnd) / 60000);
      onSeriesChanged(info.event, deltaMin, true);
    },
    eventClick: (info) => {
      info.jsEvent.preventDefault();
      const p = info.event.extendedProps;
      if (p.derived) { showBonusHint(); return; }
      const block = blocksById.get(p.blockId);
      if (!block) { refreshGrid(true); return; }
      openEditor({ mode: 'edit', block: block, dtstart: block.dtstart });
    },
  });
  calendar.render();

  $('block-target').addEventListener('change', syncEditorVisibility);
  $('block-repeat').addEventListener('change', syncEditorVisibility);
  for (const r of document.querySelectorAll('[name="block-mode"]')) {
    r.addEventListener('change', syncEditorVisibility);
  }
  $('block-cancel').addEventListener('click', closeEditor);
  $('block-editor-form').addEventListener('submit', submitEditor);
  $('pc-block-editor').addEventListener('cancel', () => { editorState = null; });
  window.pcCalendarRetry = () => { refreshGrid(false); };
  window.pcCalendarTargetChanged = syncEditorVisibility;
  window.pcCalendarRepeatChanged = syncEditorVisibility;
  window.pcCalendarDeleteSeries = deleteSeries;
  window.switchCalendarUser = function (name) {
    const u = new URL(window.location.href);
    u.searchParams.set('user', name);
    window.location.href = u.toString();
  };

  refreshGrid(false);
  startPolling();
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) { refreshGrid(false); }
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
