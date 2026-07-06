/* Ayre-UI settings — UI port, handoff cooldown, memory-warning threshold, clear
   memory, and the memory chip + proposed-memory review. Split from app.js
   2026-07-05 (mechanical phase). Reads foundation off window.Ayre. */
(function () {
  'use strict';
  var Ayre = window.Ayre;
  var root = Ayre.root, BRIDGE_DOWN = Ayre.BRIDGE_DOWN,
      el = Ayre.el, esc = Ayre.esc, getJSON = Ayre.getJSON, textEl = Ayre.textEl;
  /* ── Settings: choose the UI port ── */
  (function wirePort() {
    var input = document.getElementById('uiPort');
    var btn = document.getElementById('savePort');
    var msg = document.getElementById('portMsg');
    if (!input || !btn || !msg) return;

    function clearMsg() { msg.textContent = ''; msg.className = 'portmsg'; input.classList.remove('bad'); }
    function showErr(t) { msg.textContent = t; msg.className = 'portmsg err'; input.classList.add('bad'); }
    function showOk(t) { msg.textContent = t; msg.className = 'portmsg ok'; input.classList.remove('bad'); }

    input.addEventListener('input', function () {
      input.value = input.value.replace(/\D/g, '').slice(0, 4);
      clearMsg();
    });

    function save() {
      var port = parseInt(input.value, 10);
      if (!(port >= 1000 && port <= 9999)) { showErr('Enter a 4-digit port (1000–9999).'); return; }
      btn.disabled = true; msg.className = 'portmsg'; msg.textContent = 'Checking…';
      fetch('/api/ui-port', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ port: port })
      }).then(function (r) { return r.json(); }).then(function (res) {
        btn.disabled = false;
        if (res.ok) { showOk(res.message); } else { showErr(res.error); }
      }).catch(function (e) {
        btn.disabled = false; showErr(BRIDGE_DOWN);
      });
    }
    btn.addEventListener('click', save);
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); save(); } });
  })();

  /* ── Settings: handoff cooldown ── */
  (function wireHandoffCooldown() {
    var input = document.getElementById('handoffCooldown');
    var btn = document.getElementById('saveHandoffCooldown');
    var msg = document.getElementById('handoffCooldownMsg');
    if (!input || !btn || !msg) return;

    function clearMsg() { msg.textContent = ''; msg.className = 'portmsg'; input.classList.remove('bad'); }
    function showErr(t) { msg.textContent = t; msg.className = 'portmsg err'; input.classList.add('bad'); }
    function showOk(t) { msg.textContent = t; msg.className = 'portmsg ok'; input.classList.remove('bad'); }

    input.addEventListener('input', function () {
      input.value = input.value.replace(/\D/g, '').slice(0, 2);
      clearMsg();
    });

    function save() {
      var mins = parseInt(input.value, 10);
      if (!(mins >= 1 && mins <= 60)) { showErr('Enter a value between 1 and 60 minutes.'); return; }
      btn.disabled = true;
      fetch('/api/handoff-cooldown', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seconds: mins * 60 })
      }).then(function (r) { return r.json(); }).then(function (res) {
        btn.disabled = false;
        if (res.ok) { showOk('Saved.'); } else { showErr(res.error); }
      }).catch(function () { btn.disabled = false; showErr(BRIDGE_DOWN); });
    }
    btn.addEventListener('click', save);
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); save(); } });
  })();

  /* ── Settings: memory warning threshold ── */
  (function wireMemoryWarningThreshold() {
    var input = document.getElementById('memoryWarningChars');
    var btn = document.getElementById('saveMemoryWarning');
    var msg = document.getElementById('memoryWarningMsg');
    if (!input || !btn || !msg) return;

    function clearMsg() { msg.textContent = ''; msg.className = 'portmsg'; input.classList.remove('bad'); }
    function showErr(t) { msg.textContent = t; msg.className = 'portmsg err'; input.classList.add('bad'); }
    function showOk(t) { msg.textContent = t; msg.className = 'portmsg ok'; input.classList.remove('bad'); }

    input.addEventListener('input', function () {
      input.value = input.value.replace(/\D/g, '').slice(0, 6);
      clearMsg();
    });

    function save() {
      var chars = parseInt(input.value, 10);
      if (!(chars >= 200 && chars <= 50000)) { showErr('Enter a value between 200 and 50,000.'); return; }
      btn.disabled = true;
      fetch('/api/memory/warning-threshold', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chars: chars })
      }).then(function (r) { return r.json(); }).then(function (res) {
        btn.disabled = false;
        if (res.ok) { showOk('Saved.'); } else { showErr(res.error); }
      }).catch(function () { btn.disabled = false; showErr(BRIDGE_DOWN); });
    }
    btn.addEventListener('click', save);
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); save(); } });
  })();

  /* ── Settings: clear saved memory (POST /api/memory/clear) ── */
  (function wireClearMemory() {
    var status = document.getElementById('memorySaved');
    var btn = document.getElementById('clearMemoryBtn');
    var confirmEl = document.getElementById('clearMemoryConfirm');
    var yes = document.getElementById('clearMemoryYes');
    var no = document.getElementById('clearMemoryNo');
    var msg = document.getElementById('clearMemoryMsg');
    if (!status || !btn || !confirmEl || !yes || !no || !msg) return;

    function fmtChars(n) {
      return n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : String(n);
    }
    function clearMsg() { msg.textContent = ''; msg.className = 'portmsg'; }
    function showOk(t) { msg.textContent = t; msg.className = 'portmsg ok'; }
    function showErr(t) { msg.textContent = t; msg.className = 'portmsg err'; }
    function closeConfirm() { confirmEl.hidden = true; }

    // Disable the button when there's nothing to clear, so it never lies.
    function applyState(s) {
      var has = !!(s && s.has_content && s.char_count > 0);
      status.textContent = has
        ? 'Saved notes: ' + fmtChars(s.char_count) + ' chars'
        : (s && s.enabled === false ? 'Memory is off' : 'Saved notes: none yet');
      btn.disabled = !has;
    }
    function refresh() {
      getJSON('/api/memory').then(applyState).catch(function () {
        status.textContent = 'Saved notes: —'; btn.disabled = true;
      });
    }

    btn.addEventListener('click', function () {
      if (btn.disabled) return;
      clearMsg();
      confirmEl.hidden = false;
    });
    no.addEventListener('click', closeConfirm);
    yes.addEventListener('click', function () {
      yes.disabled = true;
      fetch('/api/memory/clear', { method: 'POST', headers: { 'Content-Type': 'application/json' } })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          yes.disabled = false;
          closeConfirm();
          if (res.ok) {
            applyState(res);
            showOk(res.cleared ? 'Memory cleared.' : 'Nothing to clear.');
            // Keep the chat-header chip's char count in sync.
            document.dispatchEvent(new CustomEvent('ayre:memory-changed'));
          } else {
            showErr(res.error || 'Could not clear memory.');
          }
        })
        .catch(function () { yes.disabled = false; closeConfirm(); showErr(BRIDGE_DOWN); });
    });

    // Refresh the saved-size readout whenever the Settings view is opened.
    document.addEventListener('ayre:nav', function (e) {
      if (e.detail && e.detail.view === 'settings') { clearMsg(); refresh(); }
    });
    refresh();
  })();

  /* ── Memory toggle chip + confirm popover ── */
  (function wireMemory() {
    var btn = document.getElementById('memoryToggle');
    var confirmEl = document.getElementById('memoryConfirm');
    var confirmYes = document.getElementById('memoryConfirmYes');
    var confirmNo = document.getElementById('memoryConfirmNo');
    if (!btn) return;

    function fmtChars(n) {
      return n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : String(n);
    }

    function closeConfirm() { if (confirmEl) confirmEl.hidden = true; }

    function applyState(s) {
      if (s.enabled && s.has_content) {
        btn.textContent = 'Memory: on · ' + fmtChars(s.char_count);
      } else if (s.enabled) {
        btn.textContent = 'Memory: on';
      } else {
        btn.textContent = 'Memory: off';
      }
      btn.className = 'memory-toggle' + (s.enabled ? ' on' : '');
      btn.title = s.enabled
        ? 'Memory on' + (s.has_content ? ' · ' + s.char_count + ' chars. Click to disable.' : '. Click to disable.')
        : 'Click to enable persistent memory.';
    }

    function refresh() {
      getJSON('/api/memory').then(applyState).catch(function () {
        btn.textContent = 'Memory: —';
        btn.className = 'memory-toggle';
      });
    }

    btn.addEventListener('click', function () {
      getJSON('/api/memory').then(function (s) {
        if (s.enabled) {
          closeConfirm();
          return fetch('/api/memory/toggle', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: false })
          }).then(function (r) { return r.json(); }).then(applyState);
        }
        if (confirmEl) confirmEl.hidden = false;
      }).catch(function () {});
    });

    if (confirmYes) {
      confirmYes.addEventListener('click', function () {
        closeConfirm();
        fetch('/api/memory/toggle', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: true })
        }).then(function (r) { return r.json(); }).then(applyState).catch(function () {});
      });
    }

    if (confirmNo) confirmNo.addEventListener('click', closeConfirm);

    document.addEventListener('click', function (e) {
      if (confirmEl && !confirmEl.hidden && !confirmEl.contains(e.target) && e.target !== btn) {
        closeConfirm();
      }
    });

    // Re-read memory when it's changed elsewhere (e.g. cleared from Settings).
    document.addEventListener('ayre:memory-changed', refresh);
    // A promoted draft grows confirmed memory — keep the chip char count in sync.
    document.addEventListener('ayre:draft-changed', refresh);

    refresh();
  })();

  /* ── Proposed-memory review: chip badge + edit/approve panel ── */
  (function wireMemoryDraft() {
    var badge = document.getElementById('memoryDraftBadge');
    var panel = document.getElementById('memoryDraftPanel');
    var text = document.getElementById('memoryDraftText');
    var saveBtn = document.getElementById('memoryDraftSave');
    var discardBtn = document.getElementById('memoryDraftDiscard');
    var msg = document.getElementById('memoryDraftMsg');
    if (!badge || !panel || !text || !saveBtn || !discardBtn) return;

    function fmtChars(n) {
      return n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : String(n);
    }
    function clearMsg() { if (msg) { msg.textContent = ''; msg.className = 'portmsg'; } }
    function showErr(t) { if (msg) { msg.textContent = t; msg.className = 'portmsg err'; } }
    function closePanel() { panel.hidden = true; clearMsg(); }

    function refreshBadge() {
      getJSON('/api/memory').then(function (s) {
        if (s && s.has_draft && s.draft_char_count > 0) {
          badge.hidden = false;
          badge.textContent = 'Review memory ✦ ' + fmtChars(s.draft_char_count);
          badge.title = 'Ayre proposed ' + s.draft_char_count + ' chars for memory — click to review.';
        } else {
          badge.hidden = true;
          if (!panel.hidden) closePanel();
        }
      }).catch(function () { badge.hidden = true; });
    }

    function openPanel() {
      clearMsg();
      getJSON('/api/memory/draft').then(function (d) {
        if (!d || !d.has_draft) { refreshBadge(); return; }
        text.value = d.content || '';   // textarea .value is text, never parsed as HTML
        panel.hidden = false;
        text.focus();
      }).catch(function () { showErr(BRIDGE_DOWN); panel.hidden = false; });
    }

    badge.addEventListener('click', function () {
      if (panel.hidden) openPanel(); else closePanel();
    });

    saveBtn.addEventListener('click', function () {
      var content = text.value.trim();
      if (!content) { showErr('Nothing to save — the memory is empty.'); return; }
      saveBtn.disabled = true; discardBtn.disabled = true;
      fetch('/api/memory/draft/promote', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: content })
      }).then(function (r) { return r.json(); }).then(function (res) {
        saveBtn.disabled = false; discardBtn.disabled = false;
        if (res.ok) {
          closePanel();
          document.dispatchEvent(new CustomEvent('ayre:memory-changed'));
          document.dispatchEvent(new CustomEvent('ayre:draft-changed'));
        } else { showErr(res.error || 'Could not save.'); }
      }).catch(function () { saveBtn.disabled = false; discardBtn.disabled = false; showErr(BRIDGE_DOWN); });
    });

    discardBtn.addEventListener('click', function () {
      saveBtn.disabled = true; discardBtn.disabled = true;
      fetch('/api/memory/draft/discard', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }
      }).then(function (r) { return r.json(); }).then(function () {
        saveBtn.disabled = false; discardBtn.disabled = false;
        closePanel();
        document.dispatchEvent(new CustomEvent('ayre:draft-changed'));
      }).catch(function () { saveBtn.disabled = false; discardBtn.disabled = false; showErr(BRIDGE_DOWN); });
    });

    document.addEventListener('click', function (e) {
      if (!panel.hidden && !panel.contains(e.target) && e.target !== badge) closePanel();
    });
    document.addEventListener('ayre:draft-changed', refreshBadge);
    document.addEventListener('ayre:open-draft-review', openPanel);
    refreshBadge();
  })();

})();
