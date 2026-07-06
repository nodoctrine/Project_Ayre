/* Ayre-UI · core.js — shared window.Ayre namespace + foundation helpers.
   Load order (index.html): core -> visuals -> chat -> setup -> workspace -> settings.
   EXPOSES on window.Ayre: root, BRIDGE_DOWN, el, esc, getJSON, textEl, handoffMinTurns.
   CONSUMES: nothing — this file is the root of the dependency order.
   Every other app-*.js re-aliases these off window.Ayre at its top, so call sites
   stay bare (esc(...), el(...), getJSON(...)). Vendored: offline, no CDN, no build step.
   SECURITY: esc() and textEl() are the two XSS guards for untrusted model/corpus text.
   Split from app.js 2026-07-05. */
window.Ayre = {};
(function () {
  'use strict';
  var Ayre = window.Ayre;
  var root = document.documentElement;
  // One friendly, actionable line for "Ayre's local server is unreachable" — every
  // same-origin fetch failure in this page means exactly that.
  var BRIDGE_DOWN = 'Ayre\'s server isn\'t running — relaunch it (Start Ayre.cmd, or ' +
    'python -m ayre_ui), then reload this page.';
  /* ── theme switcher ── */
  var tbtns = document.querySelectorAll('.theme-switch button');
  (function () {
    var cur = root.getAttribute('data-theme');
    tbtns.forEach(function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-set') === cur ? 'true' : 'false'); });
  }());
  tbtns.forEach(function (b) {
    b.addEventListener('click', function () {
      var t = b.getAttribute('data-set');
      root.setAttribute('data-theme', t);
      localStorage.setItem('ayre-theme', t);
      tbtns.forEach(function (x) { x.setAttribute('aria-pressed', x === b ? 'true' : 'false'); });
    });
  });

  /* ── nav / section switching ── */
  var navs = document.querySelectorAll('.nav-btn');
  var views = document.querySelectorAll('.view');
  navs.forEach(function (n) {
    n.addEventListener('click', function () {
      var id = n.getAttribute('data-view');
      navs.forEach(function (x) { x.setAttribute('aria-current', x === n ? 'true' : 'false'); });
      views.forEach(function (v) { v.classList.toggle('active', v.id === 'view-' + id); });
      document.dispatchEvent(new CustomEvent('ayre:nav', { detail: { view: id } }));
    });
  });

  /* ── tiny helpers ── */
  function el(tag, cls, html) {
    var d = document.createElement(tag);
    if (cls) d.className = cls;
    if (html != null) d.innerHTML = html;
    return d;
  }
  // SECURITY (XSS): escapes the 5 HTML-significant chars. Quotes are escaped too (not
  // just &<>) so esc() is safe in ATTRIBUTE contexts (e.g. data-url="...") and not only
  // in element text — without this a model-authored link URL containing a " could break
  // out of the attribute and inject an event handler. See Security_Patch_Devlog.md #4.
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]; }); }
  function getJSON(url) {
    return fetch(url, { headers: { 'Accept': 'application/json' } }).then(function (r) {
      if (!r.ok) throw new Error(url + ' -> ' + r.status);
      return r.json();
    });
  }
  // SECURITY (untrusted text): a div/span/etc. whose text is set via textContent — the
  // ONLY safe path for untrusted corpus text (article titles + chunk bodies can contain
  // < > &). el()'s third arg is innerHTML, so it must never carry corpus text.
  function textEl(tag, cls, text) {
    var d = document.createElement(tag);
    if (cls) d.className = cls;
    if (text != null) d.textContent = text;
    return d;
  }
  /* ── Online / offline indicator (rail foot) ──
     Offline = green (good: private), Online = amber (heads-up: connected).
     Uses navigator.onLine + browser events — no outbound requests. */
  (function wireConnectStatus() {
    var el = document.getElementById('connectStatus');  // NOTE: local DOM node; intentionally shadows the el() helper in this IIFE
    if (!el) return;
    function update() {
      el.innerHTML = navigator.onLine
        ? '<b class="online">online</b>'
        : '<b>offline</b>';
    }
    window.addEventListener('online', update);
    window.addEventListener('offline', update);
    update();
  })();

  /* ── namespace exports (read by visuals/chat/setup/workspace/settings.js) ── */
  Ayre.root = root; Ayre.BRIDGE_DOWN = BRIDGE_DOWN;
  Ayre.el = el; Ayre.esc = esc; Ayre.getJSON = getJSON; Ayre.textEl = textEl;
  Ayre.handoffMinTurns = 1;   // checkIfEmpty floor; overwritten from /api/system (setup.js)
})();
