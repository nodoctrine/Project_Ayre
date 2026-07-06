/* Ayre-UI · setup.js — engine health, the Setup doctor, and launch controls.
   Load order: after chat.js.  CONSUMES Ayre.{root,esc,el,getJSON,textEl,BRIDGE_DOWN}
   plus (lazily) Ayre.{chatCtl,faviconCtl,ctxMeter,setTelemetry,handoffMinTurns}.
   EXPOSES on window.Ayre: renderSystem, renderDoctor, startCtl.
   Owns `lastLlamaUp` as a file-local var (shared renderSystem<->startCtl, same file).
   Contents:
     - renderSystem   fans /api/system health out to the topbar chips + every module
     - startCtl       Start/Stop the engine + optimizer (preset selector + manual
                      override) + the pre-launch fit banner
     - the doctor     renderDoctor + artifactRow: the three-tier presence-check UI
     - bootstrap      initial doctor refresh + the 8s /api/system health poll
   Split from app.js 2026-07-05. */
(function () {
  'use strict';
  var Ayre = window.Ayre;
  var root = Ayre.root, BRIDGE_DOWN = Ayre.BRIDGE_DOWN,
      el = Ayre.el, esc = Ayre.esc, getJSON = Ayre.getJSON, textEl = Ayre.textEl;
  var lastLlamaUp = false;   // /api/system health; renderSystem<->startCtl, same file
  /* ── topbar status chips (live) ── */
  function renderSystem(sys) {
    var llama = document.getElementById('chip-llama');
    var up = sys.llama && sys.llama.healthy;
    lastLlamaUp = !!up;
    if (typeof sys.handoff_min_substantive_turns === 'number') Ayre.handoffMinTurns = sys.handoff_min_substantive_turns;
    if (Ayre.chatCtl) Ayre.chatCtl.setAvailable(lastLlamaUp);
    if (Ayre.faviconCtl) Ayre.faviconCtl.engine(lastLlamaUp);
    if (startCtl) startCtl.syncHealth();
    // Context meter: shape it from config + the live window; it hides when the
    // engine is down (no window) and resets occupancy on the next launch.
    if (Ayre.ctxMeter) {
      Ayre.ctxMeter.setConfig(sys.context);
      if (up) Ayre.ctxMeter.setWindow(sys.llama && sys.llama.n_ctx);
      else Ayre.ctxMeter.engineDown();
    }
    // The right rail (context meter + hardware monitor) rides on engine health.
    var rail = document.getElementById('chatRail');
    if (rail) rail.hidden = !up;
    Ayre.setTelemetry(!!up);
    llama.innerHTML = '<span class="led ' + (up ? 'up' : 'down') + '"></span> <b>llama-server</b> · ' +
      (up ? (':' + sys.llama.port) : 'not running');

    // When a model is loaded, name the ACTIVE one (from llama-server), not the
    // first file on disk. When stopped, report how many are available to pick.
    var model = document.getElementById('chip-model');
    if (up) {
      model.innerHTML = 'model · <b>' + esc((sys.llama && sys.llama.model) || 'model') + '</b> · loaded';
    } else if (sys.has_model) {
      model.innerHTML = 'model · <b>' + sys.models.length + ' available</b>';
    } else {
      model.innerHTML = 'model · <b>none yet</b>';
    }

    var setup = document.getElementById('chip-setup');
    setup.innerHTML = sys.required_ok
      ? 'setup · <b>required OK</b>'
      : 'setup · <b>incomplete</b>';

    // prefill the Settings port field with the live port (unless being edited)
    var portInput = document.getElementById('uiPort');
    if (portInput && document.activeElement !== portInput && sys.ui && sys.ui.port) {
      portInput.value = sys.ui.port;
    }
    // prefill the handoff cooldown field (stored in seconds; displayed in minutes)
    var cooldownInput = document.getElementById('handoffCooldown');
    if (cooldownInput && document.activeElement !== cooldownInput && typeof sys.handoff_cooldown_seconds === 'number') {
      cooldownInput.value = Math.round(sys.handoff_cooldown_seconds / 60);
    }
    // prefill the memory warning threshold field (stored in chars)
    var memWarnInput = document.getElementById('memoryWarningChars');
    if (memWarnInput && document.activeElement !== memWarnInput && typeof sys.memory_warning_chars === 'number') {
      memWarnInput.value = sys.memory_warning_chars;
    }
  }
  /* ── Installer: Start / Stop the engine (POST /api/start, /api/stop) ── */
  var startCtl = (function wireStart() {
    var btn = document.getElementById('startBtn');
    var stopBtn = document.getElementById('stopBtn');
    var msg = document.getElementById('startMsg');
    var sel = document.getElementById('modelSelect');
    var fitHost = document.getElementById('fitNotice');  // pre-launch fit banner
    if (!btn || !msg) return null;
    var STARTING = 'Starting…';
    var busy = false;               // a start OR stop is in flight; freeze gating
    var pollTimer = null;
    var ready = { required_ok: false, has_model: false };  // latest doctor gate
    var lastFitKey = null;          // last /api/fit request signature (de-dupe)

    // ── Optimizer controls (A3): preset selector + manual override ──
    var optBox = document.getElementById('optBox');
    var presetSeg = document.getElementById('presetSeg');
    var presetWhy = document.getElementById('presetWhy');
    var optMsg = document.getElementById('optMsg');
    var manualBox = document.getElementById('manualBox');
    var ovrCtx = document.getElementById('ovrCtx');
    var ovrNgl = document.getElementById('ovrNgl');
    var ovrApply = document.getElementById('ovrApply');
    var ovrClear = document.getElementById('ovrClear');
    // presets/labels/rationale come from config via /api/optimizer (never hardcoded)
    var optState = { presets: [], active: null, override: {} };
    var optEnabled = true;          // mirrors the model dropdown's gate
    var optBusy = false;            // a save is in flight
    var PREVIEW_DEBOUNCE_MS = 350;  // pause after typing before the what-if fit call
    var previewTimer = null;

    function showErr(t) { msg.textContent = t; msg.className = 'portmsg err'; }
    function showOk(t)  { msg.textContent = t; msg.className = 'portmsg ok'; }
    function showInfo(t){ msg.textContent = t; msg.className = 'portmsg'; }

    // One place that derives enabled state from known truth: live llama health
    // (lastLlamaUp) + the doctor gate. Skipped mid-action so a start/stop in
    // flight keeps the controls it locked.
    function applyGating() {
      if (busy) return;
      var up = lastLlamaUp;
      // The dropdown is usable whenever the engine is down + setup is ready (so the
      // user can pick). Start needs that PLUS a real pick (not the placeholder).
      var canConfigure = !up && ready.required_ok && ready.has_model;
      var hasPick = !!(sel && sel.value);  // '' = "Choose a model…" placeholder
      if (sel) sel.disabled = !canConfigure;
      btn.disabled = !(canConfigure && hasPick);
      if (stopBtn) stopBtn.disabled = !up;
      // The optimizer controls (preset + manual override) share the dropdown's
      // gate: editable only while the engine is down and setup is ready.
      setOptEnabled(canConfigure);
      if (up) btn.title = 'llama-server is already running.';
      else if (!ready.required_ok) btn.title = 'Engine/config missing.';
      else if (!ready.has_model) btn.title = 'Add a chat model first.';
      else if (!hasPick) btn.title = 'Choose a model first.';
      else btn.title = 'Launch llama-server';
      if (stopBtn) stopBtn.title = up ? 'Stop llama-server' : 'llama-server isn\'t running.';
      // Keep the pre-launch fit banner in step with health: shown (accurate) when
      // stopped, hidden while a model is loaded. De-duped, so this is cheap.
      refreshFit();
      // Same for the per-preset hardware predictions (tooltips + rationale line).
      refreshPredictions();
    }

    // Poll /api/system until llama reaches the wanted state (up=true after Start,
    // up=false after Stop), then unfreeze and re-gate.
    function pollUntil(up, tries, every) {
      if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
      getJSON('/api/system').then(function (sys) {
        renderSystem(sys);  // refreshes lastLlamaUp (+ chat availability)
        var healthy = !!(sys.llama && sys.llama.healthy);
        if (healthy === up) {
          showOk(up ? 'llama-server is up.' : 'llama-server stopped.');
          busy = false; applyGating(); return;
        }
        if (tries > 0) { pollTimer = setTimeout(function () { pollUntil(up, tries - 1, every); }, every); }
        else { busy = false; applyGating(); showInfo((up ? 'Still loading' : 'Still shutting down') + ' — watch the llama-server chip.'); }
      }).catch(function () {
        if (tries > 0) { pollTimer = setTimeout(function () { pollUntil(up, tries - 1, every); }, every); }
        else { busy = false; applyGating(); }
      });
    }

    function lockForAction(label) {
      busy = true;
      btn.disabled = true; if (sel) sel.disabled = true; if (stopBtn) stopBtn.disabled = true;
      setOptEnabled(false);
      showInfo(label);
    }

    btn.addEventListener('click', function () {
      var v = (sel && sel.value) || '';
      var chosen = (v && v !== '__auto__') ? v : null;  // null -> backend optimizer picks
      lockForAction(STARTING);
      fetch('/api/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(chosen ? { model: chosen } : {})
      })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (res.ok) {
            showOk(res.message);
            if (res.already_running) { busy = false; applyGating(); return; }
            pollUntil(true, 40, 2000);  // cold model load can take a while; ~80s
          } else {
            busy = false; showErr(res.error); applyGating();
          }
        })
        .catch(function (e) { busy = false; showErr(BRIDGE_DOWN); applyGating(); });
    });

    if (stopBtn) stopBtn.addEventListener('click', function () {
      lockForAction('Stopping…');
      fetch('/api/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (res.ok) { showOk(res.message); pollUntil(false, 15, 1000); }
          else { busy = false; showErr(res.error); applyGating(); }
        })
        .catch(function (e) { busy = false; showErr(BRIDGE_DOWN); applyGating(); });
    });

    // Fill the dropdown from the detected models (doctor's `models` list).
    // Default = Auto (value '__auto__'), which defers to the optimizer's tier-aware
    // pick — the same path as `cli start`. Start is enabled immediately so the user
    // can just press it without touching the picker. An explicit prior pick is
    // restored on refresh. The placeholder only appears when no models are detected.
    function populate(models) {
      if (!sel) return;
      var prev = sel.value;
      var list = models || [];
      var hasSelectable = list.some(function(m) { return m.selectable !== false; });
      sel.innerHTML = '';
      var ph = document.createElement('option');
      ph.value = ''; ph.disabled = true;
      ph.textContent = hasSelectable ? 'Choose a model…' : 'No model detected';
      sel.appendChild(ph);
      if (hasSelectable) {
        var auto = document.createElement('option');
        auto.value = '__auto__';
        auto.textContent = 'Auto — recommended for your hardware';
        sel.appendChild(auto);
      }
      list.forEach(function (m) {
        var o = document.createElement('option');
        o.value = m.name; o.textContent = m.name;
        if (m.selectable === false) {
          o.disabled = true;
          o.title = m.reason || 'Not available as a chat model';
        }
        sel.appendChild(o);
      });
      // Default to Auto so Start is ready immediately; restore a prior explicit pick.
      sel.value = hasSelectable ? '__auto__' : '';
      if (prev) {
        for (var i = 0; i < sel.options.length; i++) {
          if (!sel.options[i].disabled && sel.options[i].value === prev) { sel.value = prev; break; }
        }
      }
    }

    // ── Pre-launch hardware-fit banner ──
    // A persistent, full-width banner (not the tiny #startMsg) so an over-budget
    // pick is obvious BEFORE Start and never scrolls away. Stays quiet on an
    // 'unknown' verdict — the optimizer simply couldn't size the model, so warning
    // would be crying wolf. A3: also carries the live tradeoff (GPU %, context,
    // VRAM/RAM vs budget) + the solver's warnings, so a preset/manual choice shows
    // its real cost BEFORE Start.
    function fitBanner(cls, headline, detail, stats, warns) {
      fitHost.innerHTML = '';
      var s = el('div', 'summary ' + cls);
      s.appendChild(el('span', 'sled'));
      var body = el('div', 'stext',
        '<b>' + esc(headline) + '</b>' + (detail ? '<span>' + esc(detail) + '</span>' : ''));
      if (stats) body.appendChild(el('div', 'fstats', esc(stats)));
      (warns || []).slice(0, 4).forEach(function (w) {
        body.appendChild(el('div', 'fwarn', '⚠ ' + esc(w)));
      });
      s.appendChild(body);
      fitHost.appendChild(s);
      fitHost.hidden = false;
    }
    function hideFit() { fitHost.hidden = true; fitHost.innerHTML = ''; }

    function gibs(b) { return (b == null) ? '?' : (b / 1073741824).toFixed(1) + ' GiB'; }

    function renderFit(res, isAuto, isPreview) {
      // 'unknown' + warnings = auto-tune FAILED (probe/GGUF unreadable) and the
      // launch would silently use tier defaults — presets/overrides would look
      // broken with no clue. Surface it. Plain 'unknown' (auto-tune off by
      // config, no warnings) stays quiet as before — that's a chosen mode.
      if (!res || res.verdict === 'unknown') {
        if (res && res.verdict === 'unknown' && (res.warnings || []).length) {
          fitBanner('warn', 'Hardware fit not assessed — launching would use tier defaults.',
                    res.detail || '', '', res.warnings);
        } else { hideFit(); }
        return;
      }
      // On Auto, name the model the optimizer actually chose (document the pick).
      var picked = (isAuto && res.resolved_model)
        ? 'Auto selected ' + res.resolved_model + ' — the recommended model for your hardware.'
        : '';
      // The tradeoff line: what this preset/override buys and costs (solver `fit`).
      var f = res.fit || null;
      var stats = '';
      if (f && f.n_layers_total) {
        var pct = Math.round(100 * (f.n_gpu_layers || 0) / f.n_layers_total);
        stats = f.n_gpu_layers + '/' + f.n_layers_total + ' layers on GPU (' + pct + '%)'
              + ' · ' + Number(f.context_tokens).toLocaleString('en-US') + '-token context'
              + ' · VRAM ' + gibs(f.vram_used_bytes) + ' of ' + gibs(f.vram_budget_bytes)
              + ' · RAM ' + gibs(f.ram_used_bytes) + ' of ' + gibs(f.ram_budget_bytes)
              + (f.preset_label ? ' · ' + f.preset_label + (f.manual ? ' + manual' : '') : '')
              + (isPreview ? ' · preview — Apply to keep it' : '');
      }
      var warns = res.warnings || [];
      if (res.verdict === 'over_budget') {
        var parts = [picked, res.detail, res.suggestion].filter(Boolean);
        fitBanner(res.action === 'refuse' ? 'bad' : 'warn', res.headline, parts.join(' '),
                  stats, warns);
      } else {
        fitBanner('ok', '✓ Fits your hardware — no disk-thrash expected.', picked,
                  stats, warns);
      }
    }

    // Parse a manual input: blank -> null (the preset decides that field).
    function inputVal(inp) {
      var raw = ((inp && inp.value) || '').trim();
      if (!raw) return null;
      var n = parseInt(raw, 10);
      return (isNaN(n) || n < 0) ? null : n;
    }

    function refreshFit(force) {
      if (!fitHost || !sel) return;
      // The fit preview only makes sense BEFORE a launch. While llama is up the
      // running model is consuming VRAM/RAM, so the (free-memory) probe would
      // under-report headroom and wrongly flag the running model as over budget.
      // Start is disabled then anyway -- hide, and re-check once it's stopped.
      if (lastLlamaUp) { hideFit(); lastFitKey = null; return; }
      var v = sel.value || '';
      if (!v) { hideFit(); lastFitKey = null; return; }  // "Choose a model…" placeholder
      var isAuto = (v === '__auto__');     // Auto -> ask /api/fit with no model
      // What-if (A3): manual inputs that differ from the SAVED override preview that
      // exact hypothetical (preview=1 ignores the saved values server-side); when
      // they match, the plain call shows exactly what Start would launch.
      var ctx = inputVal(ovrCtx), ngl = inputVal(ovrNgl);
      var saved = optState.override || {};
      var savedCtx = (saved.context_tokens != null) ? saved.context_tokens : null;
      var savedNgl = (saved.n_gpu_layers != null) ? saved.n_gpu_layers : null;
      var isPreview = (ctx !== savedCtx) || (ngl !== savedNgl);
      var params = [];
      if (!isAuto) params.push('model=' + encodeURIComponent(v));
      if (isPreview) {
        params.push('preview=1');
        if (ctx !== null) params.push('context=' + ctx);
        if (ngl !== null) params.push('n_gpu_layers=' + ngl);
      }
      var url = '/api/fit' + (params.length ? '?' + params.join('&') : '');
      // De-dupe on the full request + saved-state signature, so a preset/override
      // save re-fetches even though the model pick didn't change.
      var key = url + '|' + optState.active + '|' + savedCtx + '|' + savedNgl;
      if (!force && key === lastFitKey) return;
      lastFitKey = key;
      fitBanner('', 'Checking this model\'s fit…', '');  // ~1s: probe + read GGUF
      getJSON(url).then(function (res) {
        if (key !== lastFitKey) return;     // inputs moved on — ignore stale result
        renderFit(res, isAuto, isPreview);
      }).catch(function () {
        if (key !== lastFitKey) return;
        hideFit(); lastFitKey = null;       // allow a retry on the next change
      });
    }
    // On pick: re-gate (enables Start) — applyGating also refreshes the fit banner.
    if (sel) sel.addEventListener('change', applyGating);

    // ── Optimizer controls (A3): preset selector + manual override ──
    // Labels + rationale come straight from config (/api/optimizer). A preset
    // click persists immediately (per-machine); manual values only persist on
    // Apply, so a stray keystroke never sticks — but typing DOES live-preview the
    // resulting fit above. All choices apply at the next Start.
    function optInfo(t) { if (optMsg) { optMsg.textContent = t; optMsg.className = 'portmsg ok'; } }
    function optErr(t)  { if (optMsg) { optMsg.textContent = t; optMsg.className = 'portmsg err'; } }

    function setOptEnabled(on) {
      optEnabled = on;
      if (!optBox) return;
      var els = [ovrCtx, ovrNgl, ovrApply, ovrClear];
      for (var i = 0; i < els.length; i++) { if (els[i]) els[i].disabled = !on; }
      var btns = presetSeg ? presetSeg.querySelectorAll('button') : [];
      for (var j = 0; j < btns.length; j++) btns[j].disabled = !on;
      optBox.title = on ? '' : 'Launch settings unlock when the model is stopped.';
    }

    // Predicted outcome of a preset ON THIS HARDWARE (from /api/optimizer/preview)
    // — the hover text must reflect the detected machine, not just the config
    // rationale. '' until the (async, ~1s/preset) prediction fetch lands.
    function predLine(key) {
      var pd = (optState.pred || {})[key];
      if (!pd || !pd.n_layers_total) return '';
      var pct = Math.round(100 * (pd.n_gpu_layers || 0) / pd.n_layers_total);
      var line = pd.n_gpu_layers + '/' + pd.n_layers_total + ' layers on GPU (' + pct + '%)'
               + ' · ' + Number(pd.context_tokens).toLocaleString('en-US') + '-token context';
      if (pd.verdict === 'over_budget') line += ' · over budget';
      return line;
    }

    // A saved manual override PINS its fields, so presets only affect what's left
    // unset — say so, or preset clicks look broken.
    function overridePinnedNote() {
      var ov = optState.override || {};
      var pinned = [];
      if (ov.context_tokens != null) pinned.push('context');
      if (ov.n_gpu_layers != null) pinned.push('GPU layers');
      if (!pinned.length) return '';
      return '⚠ Your manual override pins ' + pinned.join(' + ')
           + ' — presets only affect unpinned fields. Clear it below to let the preset decide.';
    }

    function renderPresets() {
      if (!presetSeg) return;
      presetSeg.innerHTML = '';
      var active = null;
      optState.presets.forEach(function (p) {
        if (p.key === optState.active) active = p;
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'segbtn' + (p.key === optState.active ? ' on' : '');
        b.textContent = p.label;
        var pl = predLine(p.key);
        b.title = (p.rationale || '') + (pl ? '\n\nOn this machine: ' + pl : '');
        b.setAttribute('role', 'radio');
        b.setAttribute('aria-checked', p.key === optState.active ? 'true' : 'false');
        b.disabled = !optEnabled;
        b.addEventListener('click', function () { pickPreset(p.key); });
        presetSeg.appendChild(b);
      });
      // Surface the WHY (document-tier-reasoning) + the WHAT on this hardware.
      if (presetWhy) {
        var why = (active && active.rationale) || '';
        var apl = active ? predLine(active.key) : '';
        if (apl) why += (why ? '\n' : '') + 'On this machine: ' + apl + '.';
        var note = overridePinnedNote();
        if (note) why += (why ? '\n' : '') + note;
        presetWhy.textContent = why;
      }
    }

    // Fetch the per-preset predictions for the current model pick (cached; skipped
    // while llama is up — the probe would under-report free memory).
    var lastPredKey = null;
    function refreshPredictions() {
      if (!optBox || optBox.hidden || !sel) return;
      if (lastLlamaUp) { lastPredKey = null; return; }
      var v = sel.value || '';
      if (!v) { lastPredKey = null; return; }
      if (v === lastPredKey) return;
      lastPredKey = v;
      var isAuto = (v === '__auto__');
      var url = '/api/optimizer/preview' + (isAuto ? '' : '?model=' + encodeURIComponent(v));
      getJSON(url).then(function (res) {
        if (v !== lastPredKey) return;      // pick moved on — ignore stale result
        if (!res || !res.ok) return;        // tooltips just stay rationale-only
        optState.pred = res.predictions || {};
        renderPresets();
      }).catch(function () { lastPredKey = null; });
    }

    function applyOptState(res) {
      optState.presets = res.presets || [];
      optState.active = res.active_preset || null;
      optState.override = res.override || {};
      renderPresets();
      if (ovrCtx) ovrCtx.value = (optState.override.context_tokens != null) ? optState.override.context_tokens : '';
      if (ovrNgl) ovrNgl.value = (optState.override.n_gpu_layers != null) ? optState.override.n_gpu_layers : '';
      // A saved override should be visible, not buried — open the advanced fold.
      if (manualBox && (optState.override.context_tokens != null || optState.override.n_gpu_layers != null)) {
        manualBox.open = true;
      }
      if (optBox) optBox.hidden = !optState.presets.length;
    }

    function postOptimizer(body, okMsg) {
      if (optBusy) return;
      optBusy = true;
      fetch('/api/optimizer', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          optBusy = false;
          if (!res.ok) { optErr(res.error || 'Could not save.'); return; }
          applyOptState(res);
          optInfo(okMsg);
          refreshFit(true);
        })
        .catch(function () { optBusy = false; optErr(BRIDGE_DOWN); });
    }

    function pickPreset(key) {
      if (!optEnabled || key === optState.active) return;
      var msg = overridePinnedNote()
        ? 'Preset saved — but your manual override still pins its fields (Clear it to let the preset decide).'
        : 'Preset saved — applies at the next Start.';
      postOptimizer({ preset: key }, msg);
    }

    if (optBox) {
      if (ovrApply) ovrApply.addEventListener('click', function () {
        var ctx = inputVal(ovrCtx), ngl = inputVal(ovrNgl);
        var clearing = (ctx === null && ngl === null);
        postOptimizer(
          { manual: clearing ? null : { context_tokens: ctx, n_gpu_layers: ngl } },
          clearing ? 'Override cleared — the preset decides again.'
                   : 'Override saved — applies at the next Start.');
      });
      if (ovrClear) ovrClear.addEventListener('click', function () {
        if (ovrCtx) ovrCtx.value = '';
        if (ovrNgl) ovrNgl.value = '';
        postOptimizer({ manual: null }, 'Override cleared — the preset decides again.');
      });
      // Typing live-previews the resulting fit (debounced) without saving.
      function schedulePreview() {
        if (previewTimer) clearTimeout(previewTimer);
        previewTimer = setTimeout(function () { refreshFit(); }, PREVIEW_DEBOUNCE_MS);
      }
      if (ovrCtx) ovrCtx.addEventListener('input', schedulePreview);
      if (ovrNgl) ovrNgl.addEventListener('input', schedulePreview);
      // Initial load: presets + saved choices from config via the bridge.
      getJSON('/api/optimizer').then(function (res) {
        if (res && res.ok) { applyOptState(res); refreshFit(true); }
      }).catch(function () { /* box stays hidden — Setup still works without it */ });
    }

    // From the live doctor state: refresh the dropdown + the start gate, re-gate.
    function setReady(doc) {
      populate(doc.models);
      ready.required_ok = !!doc.required_ok;
      ready.has_model = !!doc.has_model;
      applyGating();  // also refreshes the pre-launch fit banner
    }
    // Re-gate when llama health changes outside an action (the 8s poll), so Stop
    // tracks a server that came up/went down from the CLI.
    return { setReady: setReady, syncHealth: applyGating };
  })();

  /* ── Setup, driven by the doctor ── */
  function artifactRow(a) {
    var row = el('div', 'row');
    row.appendChild(el('div', 'rt', '<b>' + esc(a.id) + '</b><span>' + esc(a.kind) + ' · ' + esc(a.path) + '</span>'));
    row.appendChild(el('span', 'stat ' + (a.present ? 'ok' : 'miss'), a.present ? 'present' : 'missing'));
    return row;
  }

  function renderDoctor(doc) {
    // summary banner — severity keyed ONLY on the blocking tiers (engine+config,
    // then a model). Missing rerankers never bump severity; they add a sub-note.
    var summaryHost = document.getElementById('setupSummary');
    summaryHost.innerHTML = '';
    var ragTail = doc.rag_ok ? '' : ' RAG reranking is degraded until the rerankers are added.';
    var cls, title, sub;
    if (!doc.required_ok) {
      cls = 'bad';
      title = 'Setup incomplete — engine/config missing';
      sub = doc.required_missing.length + ' required item(s) not present. The engine + config must be present to run anything.';
    } else if (!doc.has_model) {
      cls = 'warn';
      title = 'Ayre is set up — add a model to boot';
      sub = 'Engine + config present. Drop any GGUF chat model into the models folder to launch.' + ragTail;
    } else {
      cls = doc.rag_ok ? 'ok' : 'warn';
      title = 'Ayre is ready' + (doc.rag_ok ? '' : ' — RAG degraded');
      sub = 'Engine + config present and a chat model is detected.' + ragTail;
    }
    var s = el('div', 'summary ' + cls);
    s.appendChild(el('span', 'sled'));
    s.appendChild(el('div', 'stext', '<b>' + esc(title) + '</b><span>' + esc(sub) + '</span>'));
    summaryHost.appendChild(s);

    // required artifacts (engine + config) — red 'missing' is a real block here
    var reqHost = document.getElementById('requiredRows');
    reqHost.innerHTML = '';
    doc.required.forEach(function (a) { reqHost.appendChild(artifactRow(a)); });

    // bundled RAG rerankers — amber 'absent' (degraded), never red
    var ragHost = document.getElementById('ragRows');
    ragHost.innerHTML = '';
    doc.rag.forEach(function (a) {
      var row = el('div', 'row');
      row.appendChild(el('div', 'rt', '<b>' + esc(a.id) + '</b><span>' + esc(a.kind) + ' · ' + esc(a.path) + '</span>'));
      row.appendChild(el('span', 'stat ' + (a.present ? 'ok' : 'degr'), a.present ? 'present' : 'absent'));
      ragHost.appendChild(row);
    });

    // chat model
    var modelHost = document.getElementById('modelRows');
    modelHost.innerHTML = '';
    if (doc.has_model) {
      doc.models.forEach(function (m) {
        if (m.selectable === false) return;  // rerankers shown in RAG section above
        var row = el('div', 'row');
        row.appendChild(el('div', 'rt', '<b>' + esc(m.name) + '</b><span>' + esc(m.path) + '</span>'));
        // Coaching chips (config/coaching.json, attached by the doctor): quant
        // (from the filename) + MoE (from GGUF metadata). Hover explains the
        // speed/quality/VRAM tradeoff.
        [m.quant, m.moe].forEach(function (c) {
          if (!c || !c.label) return;
          var chip = el('span', 'qchip tone-' + (c.tone || 'neutral'), esc(c.label));
          if (c.tip) chip.title = c.tip;   // .title = plain text, no escaping needed
          row.appendChild(chip);
        });
        row.appendChild(el('span', 'stat ok', 'detected'));
        modelHost.appendChild(row);
      });
    } else {
      var row = el('div', 'row');
      row.appendChild(el('div', 'rt', '<b>No chat model yet</b><span>any non-reranker .gguf in the models folder is detected automatically</span>'));
      row.appendChild(el('span', 'stat wait', 'add one'));
      modelHost.appendChild(row);
    }

    // actionable hints (single source of truth: from the doctor)
    var hintHost = document.getElementById('setupHints');
    hintHost.innerHTML = '';
    if (!doc.required_ok && doc.hints && doc.hints.required_missing) {
      hintHost.appendChild(el('div', 'hint', esc(doc.hints.required_missing)));
    } else if (!doc.has_model && doc.hints && doc.hints.add_model) {
      hintHost.appendChild(el('div', 'hint', esc(doc.hints.add_model)));
    }
    // RAG degradation is non-blocking info — show it as a soft note whenever the
    // engine/config are fine but rerankers are absent.
    if (doc.required_ok && !doc.rag_ok && doc.hints && doc.hints.rag_degraded) {
      hintHost.appendChild(el('div', 'note', esc(doc.hints.rag_degraded)));
    }

    // gate the Start button on the same live state
    if (startCtl) startCtl.setReady(doc);
  }

  function refresh() {
    getJSON('/api/system').then(renderSystem).catch(function () {
      document.getElementById('chip-llama').innerHTML = '<span class="led down"></span> <b>llama-server</b> · bridge offline';
      if (Ayre.faviconCtl) Ayre.faviconCtl.engine(false);
    });
    // Offer to inject the most recent handoff file for the active project (if one exists).
    getJSON('/api/handoff/latest').then(function (d) {
      if (d && d.ok && Ayre.chatCtl) Ayre.chatCtl.offerHandoff(d.filename, d.content);
    }).catch(function () {});

  document.addEventListener('ayre:project-changed', function () {
    getJSON('/api/handoff/latest').then(function (d) {
      if (!Ayre.chatCtl) return;
      if (d && d.ok) Ayre.chatCtl.offerHandoff(d.filename, d.content);
      else Ayre.chatCtl.clearHandoff();
    }).catch(function () { if (Ayre.chatCtl) Ayre.chatCtl.clearHandoff(); });
  });
    getJSON('/api/doctor').then(renderDoctor).catch(function (e) {
      document.getElementById('requiredRows').innerHTML =
        '<div class="hint">Could not reach the Ayre-UI bridge (' + esc(e.message) + '). Is the server running?</div>';
    });
  }

  refresh();

  // Keep the llama-server chip + chat availability fresh if the server comes up
  // or goes down outside the UI (e.g. started/stopped from the CLI). Light: only
  // the /api/system ping, not the full doctor sweep.
  setInterval(function () {
    getJSON('/api/system').then(renderSystem).catch(function () {});
  }, 8000);


  Ayre.renderSystem = renderSystem; Ayre.renderDoctor = renderDoctor;
  Ayre.startCtl = startCtl;
})();
