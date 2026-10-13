/* ==========================================================================
   static/js/app.js — progressive enhancement only (REVISED-SPEC.md).

   The panel is server-rendered Flask + Jinja; every save is a plain POST form
   and every destructive action has a server-side endpoint. Nothing here is
   required for the page to work — with JavaScript disabled the pages still
   render, navigate and submit. This file only:

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
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', onReady);
  } else {
    onReady();
  }
})();
