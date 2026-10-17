/* ==========================================================================
   static/js/app.js — progressive enhancement only (REVISED-SPEC.md).

   The panel is server-rendered Flask + Jinja; every page renders and
   navigates with JavaScript disabled, and nothing in THIS file is required
   for a page to work. No-JS action coverage is not uniform (UI_UX_PLAN.md
   §11.1, §11.8): the Overview is a complete plain-POST path — every account
   action and the admin add/remove are <form method="post"> submissions to
   their /overview/* twin endpoints (PRG + flash) — while the budgets,
   processes, admin-audit and per-user (time-rules) pages submit their
   mutations via page-script fetch + a CSRF header against /api/*, so with
   JavaScript disabled those pages render and navigate but those actions do
   NOT submit. (Admin sign-out is a plain POST to /logout on every page.)
   Converging the remaining pages onto plain-POST twins is an open, deferred
   owner option, not shipped. This file only:

     * remembers the "Reduced motion" switch (Processes) across visits and
       re-applies the `pc-reduced-motion` body class on load;
     * closes an open <details> popover (account / help menus, tab-bar menu)
       on an outside click or Escape, so keyboard and pointer users can
       dismiss it without a framework.

   No build step, no external asset, no dependency.
   ========================================================================== */
(function () {
  'use strict';

  var REDUCED_KEY = 'pc-reduced-motion';

  function prefersReduced() {
    return !!(window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function readReduced() {
    try {
      var stored = localStorage.getItem(REDUCED_KEY);
      if (stored === '1') { return true; }
      if (stored === '0') { return false; }
    } catch (e) { /* localStorage unavailable: fall back to the OS setting */ }
    return prefersReduced();
  }

  function applyReduced(on) {
    if (document.body) {
      document.body.classList.toggle('pc-reduced-motion', !!on);
    }
    var el = document.getElementById('reduced-motion');
    if (el && el.checked !== !!on) { el.checked = !!on; }
  }

  // Persist + apply the switch (called from processes.html's onchange).
  window.pcSetReducedMotion = function (on) {
    try { localStorage.setItem(REDUCED_KEY, on ? '1' : '0'); } catch (e) { /* ignore */ }
    applyReduced(on);
  };

  // --- Live countdowns -----------------------------------------------------
  // ONE 1 Hz ticker drives every countdown element on any page (the Time rules
  // "Live countdown" card and the Overview account cards). Each element carries
  // `data-countdown-seconds`, seeded server-side from app.countdown_state() —
  // this only decrements that number and writes HH:MM:SS, it never derives the
  // value. A page poll re-seeds the attribute (the Overview rebuilds the cards
  // every 10s; the user page's refreshUserLive rewrites it), which re-syncs the
  // readout so it cannot drift.
  function formatCountdown(seconds) {
    var total = Math.max(0, Math.floor(Number(seconds) || 0));
    function pad(n) { return (n < 10 ? '0' : '') + n; }
    return pad(Math.floor(total / 3600)) + ':' +
      pad(Math.floor((total % 3600) / 60)) + ':' + pad(total % 60);
  }

  window.pcFormatCountdown = formatCountdown;

  function tickCountdowns() {
    var nodes = document.querySelectorAll('[data-countdown-seconds]');
    Array.prototype.forEach.call(nodes, function (node) {
      var left = parseInt(node.getAttribute('data-countdown-seconds'), 10);
      if (isNaN(left)) { return; }
      left = left > 0 ? left - 1 : 0;
      node.setAttribute('data-countdown-seconds', String(left));
      node.textContent = formatCountdown(left);
    });
  }

  window.pcTickCountdowns = tickCountdowns;

  // Dismiss an open popover <details> on outside click / Escape.
  function closeOpenMenus(except) {
    var open = document.querySelectorAll('details.pc-menu[open], details.pc-tabmenu[open]');
    Array.prototype.forEach.call(open, function (node) {
      if (node !== except) { node.removeAttribute('open'); }
    });
  }

  function onReady() {
    applyReduced(readReduced());
    document.addEventListener('click', function (event) {
      var inside = event.target.closest && event.target.closest('details.pc-menu, details.pc-tabmenu');
      closeOpenMenus(inside);
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') { closeOpenMenus(null); }
    });
    // Tick only when the page actually has a countdown element; the Overview
    // and the Time rules page do, the auth pages do not.
    if (document.querySelector('[data-countdown-seconds]')) {
      setInterval(tickCountdowns, 1000);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', onReady);
  } else {
    onReady();
  }
})();
