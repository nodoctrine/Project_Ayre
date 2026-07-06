/* Ayre-UI · visuals.js — ambient/rail visuals, motion state, telemetry.
   Load order: after core.js.  CONSUMES Ayre.{root,esc,el,getJSON,textEl,BRIDGE_DOWN}.
   EXPOSES on window.Ayre: reduceMotion, faviconCtl, ctxTendrils, ctxMeter, hwMon,
   setTelemetry  (read lazily by chat.js and setup.js).
   Contents (in file order; reduceMotion is defined FIRST because favicon/tendrils/
   meter subscribe to it at construction):
     - reduceMotion        shared System/On/Off motion state (localStorage; others subscribe)
     - faviconCtl          live browser-tab status glyph (idle/engine/thinking)
     - ctxTendrils         ambient tendril field painted behind the chat
     - ctxMeter            the CHAT (retained) + LIVE (active) context-occupancy bars
     - Appearance toggles  thinking-visual on/off + Reduce Motion tri-state
     - hwMon + telemetry   hardware monitor; the poll runs ONLY while the engine is up
     - tip ticker          rolling tips along the bottom of the chat (motion-gated)
   Split from app.js 2026-07-05. */
(function () {
  'use strict';
  var Ayre = window.Ayre;
  var root = Ayre.root, BRIDGE_DOWN = Ayre.BRIDGE_DOWN,
      el = Ayre.el, esc = Ayre.esc, getJSON = Ayre.getJSON, textEl = Ayre.textEl;
  /* ── Chat context meter (live; Slice 3a) ──
     A vertical bar beside the chat showing ACTUAL token occupancy of the loaded
     window (from llama-server usage), not an estimate. Zones (green/yellow/red)
     are measured against the USABLE window (total minus the reserved handoff
     headroom); the striped bracket at the top of the bar is that reserved space.
     The handoff flow the red zone primes lands in a later sub-slice (3c). */
  /* ── Reduced-motion state (shared) ──
     One resolved source of truth for "should motion be quiet right now?", read by
     every animated surface. The choice is a per-client preference in localStorage:
       'system' (default) — follow the OS prefers-reduced-motion query
       'on'   — force motion reduced, regardless of the OS
       'off'  — force motion allowed, regardless of the OS
     The resolved boolean is stamped as data-reduce-motion="reduce" on :root, so the
     CSS-driven animations (status-dot pulse, streaming cursor, working stat) can
     quiet through one attribute selector; the JS-driven surfaces (favicon ring,
     tendril field, tip ticker) subscribe() and quiet themselves. Override works in
     EITHER direction because the OS query is consulted ONLY in 'system' mode — a bare
     @media query could never honor a force-'off'. */
  var reduceMotion = (function () {
    var KEY = 'ayre.reduceMotion';
    var mq = (window.matchMedia) ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    var subs = [];
    var mode = 'system';
    try { mode = localStorage.getItem(KEY) || 'system'; } catch (e) { /* private mode -> default */ }
    if (mode !== 'on' && mode !== 'off') mode = 'system';

    function resolved() {
      if (mode === 'on') return true;
      if (mode === 'off') return false;
      return !!(mq && mq.matches);
    }
    function apply() {
      var r = resolved();
      if (r) document.documentElement.setAttribute('data-reduce-motion', 'reduce');
      else document.documentElement.removeAttribute('data-reduce-motion');
      for (var i = 0; i < subs.length; i++) { try { subs[i](r); } catch (e) { /* keep going */ } }
    }
    // Re-resolve if the OS preference flips (only changes anything in 'system' mode,
    // but re-applying in any mode is harmless and idempotent).
    if (mq) {
      var onChange = function () { apply(); };
      if (mq.addEventListener) mq.addEventListener('change', onChange);
      else if (mq.addListener) mq.addListener(onChange);   // older engines
    }
    apply();   // stamp :root immediately at startup (before any subscribers exist)

    return {
      mode: function () { return mode; },
      isReduced: function () { return resolved(); },
      set: function (m) {
        if (m !== 'on' && m !== 'off') m = 'system';
        mode = m;
        try { localStorage.setItem(KEY, m); } catch (e) { /* private mode -> in-memory only */ }
        apply();
      },
      // fn(reducedBool) runs now with the current state and again on every change.
      subscribe: function (fn) { subs.push(fn); try { fn(resolved()); } catch (e) { /* ignore */ } }
    };
  })();

  /* ── Browser tab favicon (live status) ──
     Solid --signal (theme's cyan) while the engine is up and idle, --amber while
     down. While a turn is generating, a ring breathes outward from the core and
     fades — the same outward-pulse shape as the topbar .dot chip's box-shadow
     animation and the tendril visual's breathing core, just drawn to canvas
     since a favicon can't run a CSS animation. A 150ms interval (not rAF) steps
     the breath: ~16 redraws per 2.4s cycle (matching the .dot's cadence) is
     plenty smooth for a tab-strip icon without redrawing 60x/sec. */
  var faviconCtl = (function wireFavicon() {
    var link = document.getElementById('favicon');
    if (!link) return null;
    var cv = document.createElement('canvas'); cv.width = cv.height = 32;
    var ctx = cv.getContext('2d');
    var engineUp = false, thinking = false, timer = null, t0 = 0, reduced = false;
    var PERIOD_MS = 2400, STEP_MS = 150;   // cadence matched to the .dot chip's pulse

    function colorVar(name, fallback) {
      try {
        var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
        return v || fallback;
      } catch (e) { return fallback; }
    }

    // phase: null for a plain solid dot (idle/down); 0..1 for one breath cycle
    // of the outward ring (0 = ring at the core's edge, full-ish opacity;
    // 1 = ring expanded and faded to nothing).
    function draw(phase) {
      ctx.clearRect(0, 0, 32, 32);
      var color = engineUp ? colorVar('--signal', '#3ddbe6') : colorVar('--amber', '#e0a13a');
      if (phase != null) {
        ctx.globalAlpha = 0.45 * (1 - phase);
        ctx.strokeStyle = color; ctx.lineWidth = 2.5;
        ctx.beginPath(); ctx.arc(16, 16, 10 + phase * 8, 0, 6.2832); ctx.stroke();
        ctx.globalAlpha = 1;
      }
      ctx.fillStyle = color;
      ctx.beginPath(); ctx.arc(16, 16, 9, 0, 6.2832); ctx.fill();
      link.href = cv.toDataURL('image/png');
    }

    function sync() {
      if (thinking && !reduced) {          // reduced motion: skip the breathing ring, hold the solid dot
        if (!timer) {
          t0 = Date.now();
          timer = setInterval(function () {
            draw(((Date.now() - t0) % PERIOD_MS) / PERIOD_MS);
          }, STEP_MS);
        }
      } else {
        if (timer) { clearInterval(timer); timer = null; }
        draw(null);
      }
    }

    reduceMotion.subscribe(function (r) { reduced = r; sync(); });   // quiet/restore the breathing ring live
    return {
      engine: function (up) { engineUp = !!up; sync(); },
      thinking: function (active) { thinking = !!active; sync(); }
    };
  })();

  /* ── Thinking visual (ambient centered tendril field, behind the chat) ──
     A reuse of the Ayre splash: tendrils radiating from a breathing core node, drawn
     from the dead center of the chat column, BEHIND the message boxes (CSS: negative
     z-index in the column's isolated stacking context). Driven live by the meter —
     COUNT grows with active context (the think), REACH grows with total chat — and by
     think-time: both expand the longer a single turn runs (see `grow`). It animates
     ONLY during a turn and fades to transparency at rest; the rAF loop fully stops
     when idle, pauses on tab-blur, and stays dark while motion is reduced (applyGate).
     The widget is always built (so a force-'off' Reduce Motion can revive it even when
     the OS prefers reduced motion) — actual rendering is gated, not the construction. */
  var ctxTendrils = (function wireTendrils() {
    var canvas = document.getElementById('ctxTendril');
    if (!canvas || !canvas.getContext) return null;
    var ctx = canvas.getContext('2d');
    var W = canvas.width, H = canvas.height, CX = W / 2, CY = H / 2;
    var MAXR = Math.min(CX, CY) - 6;        // longest a tendril can reach (px)
    var GROW_TAU = 14;                      // seconds; how fast the field expands as the model keeps thinking (eased 0→1)

    var SIGNAL = '#3ddbe6';
    function refreshColors() {
      try {
        var s = getComputedStyle(document.documentElement).getPropertyValue('--signal').trim();
        if (s) SIGNAL = s;
      } catch (e) { /* keep last */ }
    }
    refreshColors();

    // Fixed pool of jagged tendrils; we reveal a prefix (count) and draw each to a
    // fraction of its length (reach). Golden-angle spread so any prefix looks even.
    var MAX = 30, pool = [];
    for (var k = 0; k < MAX; k++) {
      var angle = k * 2.39996 + (Math.random() - 0.5) * 0.25;
      var segs = 7 + (Math.random() * 4 | 0);
      var zig = 3 + Math.random() * 6, cos = Math.cos(angle), sin = Math.sin(angle);
      var px = -sin, py = cos, pts = [{ x: CX, y: CY }];
      for (var s = 1; s <= segs; s++) {
        var t = s / segs, d = t * MAXR;
        var z = ((s % 2 === 0) ? 1 : -1) * zig * (0.4 + Math.random() * 0.6) * (0.3 + t * 0.7);
        pts.push({ x: CX + cos * d + px * z, y: CY + sin * d + py * z });
      }
      pool.push({ pts: pts, width: 0.6 + Math.random() * 1.0, phase: Math.random() * 40,
                  dashOn: 2 + Math.random() * 3, dashOff: 4 + Math.random() * 6 });
    }

    var on = false, rafId = null, lastT = 0, t0 = -1, offset = 0;
    var env = 0, turnActive = false;                 // env: 0 at rest, eases to 1 mid-turn
    var aTar = 0, cTar = 0, aCur = 0, cCur = 0;       // active/chat targets + eased values
    var grow = 0, turnStartTs = -1;                   // grow: 0→1 think-time expansion; turnStartTs: rAF ts this turn began
    var pref = false, reduced = false;                // pref = Appearance toggle; reduced = resolved reduced-motion (render only when pref && !reduced)

    function breath(time) { return 0.5 - 0.5 * Math.cos(time / 3 * Math.PI * 2); }

    function drawTendril(td, reach, alpha) {
      var n = td.pts.length, maxPts = Math.max(2, Math.ceil(n * reach));
      ctx.strokeStyle = SIGNAL; ctx.lineWidth = td.width; ctx.globalAlpha = alpha;
      ctx.lineCap = 'round'; ctx.setLineDash([td.dashOn, td.dashOff]);
      ctx.lineDashOffset = -(offset * 3 + td.phase);
      ctx.beginPath(); ctx.moveTo(td.pts[0].x, td.pts[0].y);
      for (var i = 1; i < maxPts && i < n; i++) ctx.lineTo(td.pts[i].x, td.pts[i].y);
      if (maxPts < n) {                                // partial last segment
        var frac = n * reach - Math.floor(n * reach), a = td.pts[maxPts - 1], b = td.pts[maxPts];
        if (b) ctx.lineTo(a.x + (b.x - a.x) * frac, a.y + (b.y - a.y) * frac);
      }
      ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha = 1;
    }

    function frame(ts) {
      if (t0 < 0) t0 = ts;
      var time = (ts - t0) / 1000, dt = Math.min(0.05, time - lastT); lastT = time;
      offset += dt * 12;
      env += ((turnActive ? 1 : 0) - env) * Math.min(1, dt * 4);
      aCur += (aTar - aCur) * Math.min(1, dt * 5);
      cCur += (cTar - cCur) * Math.min(1, dt * 5);

      ctx.clearRect(0, 0, W, H);
      if (!turnActive && env < 0.02) { rafId = null; return; }   // faded out: stop burning frames

      var b = breath(time);
      // Think-time growth: the field EXPANDS the longer the model runs this turn —
      // an eased 0→1 ramp over ~GROW_TAU seconds of continuous generation, decaying
      // back to 0 as the turn ends (env fades alongside it).
      if (turnActive && turnStartTs < 0) turnStartTs = ts;
      var thinkSecs = (turnActive && turnStartTs >= 0) ? (ts - turnStartTs) / 1000 : 0;
      var growTar = turnActive ? (1 - Math.exp(-thinkSecs / GROW_TAU)) : 0;
      grow += (growTar - grow) * Math.min(1, dt * 3);
      // COUNT (how many tendrils) rises with the active "think" AND with think-time;
      // REACH (how far they extend) rises with total chat AND with think-time. sqrt
      // curve so even a light think shows some tendrils.
      var countF = Math.sqrt(Math.max(0, Math.min(1, aCur + 0.45 * grow))) * MAX;
      var count = Math.ceil(countF);
      var reach = Math.min(1, 0.30 + 0.30 * Math.max(0, Math.min(1, cCur)) + 0.55 * grow);

      ctx.save(); ctx.shadowColor = SIGNAL; ctx.shadowBlur = 4;
      for (var i = 0; i < count; i++) {
        var lead = (i === count - 1) ? (countF - (count - 1)) : 1; // newest tendril fades in
        if (lead > 0) drawTendril(pool[i], reach, env * (0.26 + 0.30 * b) * lead);
      }
      ctx.shadowBlur = 0; ctx.restore();

      // breathing core node (white aura + cyan/blue pupil)
      var auraR = 4 + b * 10, g = ctx.createRadialGradient(CX, CY, 0, CX, CY, auraR);
      g.addColorStop(0, 'rgba(255,255,255,' + ((0.10 + b * 0.16) * env) + ')');
      g.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(CX, CY, auraR, 0, 6.2832); ctx.fill();
      ctx.globalAlpha = env * (0.7 + b * 0.2); ctx.fillStyle = '#fff';
      ctx.beginPath(); ctx.arc(CX, CY, 2.4 + b * 0.8, 0, 6.2832); ctx.fill();
      ctx.globalAlpha = env; ctx.fillStyle = SIGNAL;
      ctx.beginPath(); ctx.arc(CX, CY, 1.4, 0, 6.2832); ctx.fill();
      ctx.globalAlpha = 1;

      rafId = requestAnimationFrame(frame);
    }

    function ensureRunning() {
      if (on && rafId == null && !document.hidden) { lastT = 0; t0 = -1; rafId = requestAnimationFrame(frame); }
    }
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) { if (rafId != null) { cancelAnimationFrame(rafId); rafId = null; } }
      else if (on && (turnActive || env > 0.02)) ensureRunning();
    });

    // Rendering requires BOTH the Appearance toggle (pref) AND motion not being
    // reduced — the tendril toggle and the global Reduce Motion setting are independent
    // gates that AND together, so either one off keeps the field dark.
    function applyGate() {
      var want = pref && !reduced;
      if (want === on) return;
      on = want;
      if (!on) { if (rafId != null) { cancelAnimationFrame(rafId); rafId = null; } ctx.clearRect(0, 0, W, H); }
      else { refreshColors(); if (turnActive || env > 0.02) ensureRunning(); }
    }
    reduceMotion.subscribe(function (r) { reduced = r; applyGate(); });

    return {
      // The Appearance toggle sets the user's preference; render stays gated by reduced-motion.
      enabled: function (yes) { pref = !!yes; applyGate(); },
      setData: function (active, chat) {
        aTar = Math.max(0, Math.min(1, active || 0));
        cTar = Math.max(0, Math.min(1, chat || 0));
      },
      start: function () { turnActive = true; turnStartTs = -1; ensureRunning(); },  // turn began: bloom + restart think-time growth
      stop: function () { turnActive = false; ensureRunning(); }                     // turn ended: fade out
    };
  })();

  var ctxMeter = (function wireMeter() {
    var host = document.getElementById('ctxMeter');
    var bar = document.getElementById('ctxBar');                 // Bar 1: CHAT (retained)
    var fill = document.getElementById('ctxFill');
    var barA = document.getElementById('ctxBarActive');          // Bar 2: LIVE (active)
    var fillA = document.getElementById('ctxFillActive');
    var headEl = document.getElementById('ctxHeadroom');
    var headEl2 = barA ? barA.querySelector('.ctx-headroom') : null;
    var pctEl = document.getElementById('ctxPct');
    var tokEl = document.getElementById('ctxTok');
    var warnEl = document.getElementById('ctxWarn');
    if (!host || !bar || !fill) return null;

    var nCtx = 0;        // loaded context window (tokens), from /props
    // Two quantities, two bars (both on the same window scale):
    //   retained — what the conversation CARRIES FORWARD between turns (≈ the next
    //              request's prompt). Grows slowly; this is "how full is my chat".
    //   active   — what the PENDING response is using right now, thinking included
    //              (≈ prompt + completion). Spikes during a think, then EMPTIES to 0
    //              at rest once the turn completes — nothing is in flight between turns,
    //              so the LIVE bar reads empty while CHAT keeps the cumulative footprint.
    var retained = 0, active = 0;
    var firstPrompt = -1;  // this turn's round-1 prompt_tokens (exact retained base); -1 = unseen
    var cfg = { headroom_fraction: 0.05, zones: { yellow_at: 0.70, red_at: 0.85 },
                warnings: { chat_high_at: 0.80, chat_full_at: 0.95, live_at: 0.95 } };
    // Live mid-turn estimate for `active`: an exact usage chunk only arrives when a
    // round FINISHES, so we count streamed tokens (~1 per delta) to move it live,
    // then raise to the exact total when a chunk lands. Estimate while talking.
    var streaming = false, liveBase = 0, liveCount = 0, ticks = 0;

    function fmt(n) { return Math.round(n).toLocaleString('en-US'); }
    function estTokens(s) { return Math.max(1, Math.ceil((s || '').length / 4)); }
    function zoneOf(frac) {
      return frac >= cfg.zones.red_at ? 'red' : (frac >= cfg.zones.yellow_at ? 'yellow' : 'green');
    }

    function render() {
      if (!nCtx) { host.hidden = true; return; }
      host.hidden = false;
      var H = cfg.headroom_fraction;          // reserved fraction (top of the bars)
      var U = Math.max(0.01, 1 - H);          // usable fraction (bottom of the bars)
      var usableTok = Math.max(1, Math.round(nCtx * U));
      var ya = cfg.zones.yellow_at, ra = cfg.zones.red_at;

      // Zone cut-points + headroom bracket. Set on the host so BOTH bars inherit
      // (CSS custom properties cascade); height as % of the FULL bar, from the bottom.
      host.style.setProperty('--green-top', (ya * U * 100) + '%');
      host.style.setProperty('--yellow-top', (ra * U * 100) + '%');
      host.style.setProperty('--usable-top', (U * 100) + '%');
      if (headEl) headEl.style.height = (H * 100) + '%';
      if (headEl2) headEl2.style.height = (H * 100) + '%';

      // Bar 1 — CHAT (retained). Fill capped at the usable boundary so the reserved
      // handoff bracket always reads as protected.
      var fracChat = retained / usableTok;
      fill.style.height = (Math.min(retained / nCtx, U) * 100) + '%';
      bar.setAttribute('data-zone', zoneOf(fracChat));

      // Bar 2 — LIVE (active). Same scale; its own zone colour so it can glow amber/
      // red during a heavy think while CHAT stays green.
      var fracActive = active / usableTok;
      if (fillA) fillA.style.height = (Math.min(active / nCtx, U) * 100) + '%';
      if (barA) barA.setAttribute('data-zone', zoneOf(fracActive));

      // Tendrils style reads the same two fractions: count = active, reach = chat.
      if (ctxTendrils) ctxTendrils.setData(fracActive, fracChat);

      // Headline readout + handoff warning track CHAT (the conversation's real
      // footprint) — a transient LIVE spike must not nag the user to hand off.
      var pct = Math.min(100, Math.round(fracChat * 100));
      host.setAttribute('data-zone', zoneOf(fracChat));
      pctEl.textContent = pct + '%';
      tokEl.textContent = fmt(retained) + ' / ' + fmt(usableTok);

      if (warnEl) {
        if (zoneOf(fracChat) === 'red') {
          warnEl.hidden = false;
          warnEl.textContent = 'Context is running low — ' + pct + '% of the usable ' +
            'window. Older turns may soon be cut off. Press Handoff → to summarise ' +
            'this session to memory and continue in a fresh one.';
        } else {
          warnEl.hidden = true;
        }
      }
    }

    return {
      // Shaping knobs from /api/system (config). Measurement is separate.
      setConfig: function (c) {
        if (c && typeof c.headroom_fraction === 'number') {
          var w = c.warnings || {};
          cfg = { headroom_fraction: c.headroom_fraction,
                  zones: { yellow_at: (c.zones && c.zones.yellow_at) || 0.70,
                           red_at: (c.zones && c.zones.red_at) || 0.85 },
                  warnings: { chat_high_at: w.chat_high_at || 0.80,
                              chat_full_at: w.chat_full_at || 0.95,
                              live_at: w.live_at || 0.95 } };
        }
        render();
      },
      // The loaded window (tokens), from /props. While the engine is up a missing
      // read arrives as 0 (e.g. /props queued behind a busy generation and timed
      // out) -- keep the last known window then, rather than zeroing the meter
      // mid-conversation. A genuinely different positive window means a new model
      // loaded: reset occupancy. Never clobber mid-turn (the open SSE proves the
      // engine is alive even if a health poll momentarily stumbles).
      setWindow: function (n) {
        if (streaming) return;
        n = (n | 0) || 0;
        if (!n) return;                 // transient unknown while up -> keep last
        if (n !== nCtx) { nCtx = n; retained = active = 0; }
        render();
      },
      // Engine is actually down: hide the meter and clear occupancy so the next
      // launch starts clean. Ignored mid-turn (a transient health blip during heavy
      // generation must not wipe a live bar).
      engineDown: function () {
        if (streaming) return;
        nCtx = 0; retained = active = 0; render();
      },
      // Pre-send projection (Slice 3b): given the EXACT token count of the composer
      // draft, where would sending it land vs the usable window? Projected against
      // RETAINED occupancy (what actually carries forward). null when there's no
      // window to project against (engine down / meter hidden).
      // Three independent send-time limits, all read off the same projected occupancy
      // (retained + draft) but against different denominators (Context_Management.md,
      // thresholds in config):
      //   chatFrac = projected / USABLE window (total minus handoff headroom)
      //   liveFrac = projected / FULL window (n_ctx) — the hard single-turn generation limit
      // Verdict = the highest-severity threshold crossed: live > chat_full > chat_high.
      //   live      — prompt nearly fills the full window; no room to generate (blocking modal)
      //   chat_full — usable window nearly exhausted; handoff due (blocking modal + log save)
      //   chat_high — high usage; advisory only (inline + soft confirm)
      project: function (draftTokens) {
        if (!nCtx) return null;
        var usable = Math.max(1, Math.round(nCtx * (1 - cfg.headroom_fraction)));
        var d = Math.max(0, draftTokens | 0);
        var projected = retained + d;
        var chatFrac = projected / usable;
        var liveFrac = projected / nCtx;
        var w = cfg.warnings;
        var verdict;
        if (liveFrac >= w.live_at) verdict = 'live';
        else if (chatFrac >= w.chat_full_at) verdict = 'chat_full';
        else if (chatFrac >= w.chat_high_at) verdict = 'chat_high';
        else verdict = 'ok';
        return { window: nCtx, usable: usable, committed: retained, draft: d, projected: projected,
                 chatFrac: chatFrac, liveFrac: liveFrac,
                 chatPct: Math.min(999, Math.round(chatFrac * 100)),
                 livePct: Math.min(999, Math.round(liveFrac * 100)),
                 verdict: verdict };
      },
      // EXACT counts from a usage chunk. `prompt` (round-1 only) anchors RETAINED;
      // `total` raises ACTIVE. Mid-turn we only let active RISE — snapping down to a
      // lower exact value looks like a bug (the char/4 estimate runs ahead of actual).
      setUsage: function (prompt, total) {
        if (typeof total !== 'number' || total < 0) return;
        if (streaming) {
          if (typeof prompt === 'number' && prompt >= 0 && firstPrompt < 0) firstPrompt = prompt;
          if (total > active) active = total;
        } else {
          active = total;                 // stray at-rest chunk: reflect it on LIVE
        }
        render();
      },
      // A turn just started: CHAT (retained) holds; LIVE baselines at retained plus a
      // rough size for the new user message, then climbs live as tokens stream in.
      beginTurn: function (userText) {
        streaming = true; liveCount = 0; ticks = 0; firstPrompt = -1;
        liveBase = retained + estTokens(userText);
        active = liveBase; render();
        if (ctxTendrils) ctxTendrils.start();
        if (faviconCtl) faviconCtl.thinking(true);
      },
      // One streamed token (reasoning or answer) — climb LIVE's estimate, throttled.
      // max() so a prior exact usage chunk is never undercut by the estimate.
      tick: function () {
        if (!streaming) return;
        liveCount += 1; active = Math.max(active, liveBase + liveCount);
        if ((++ticks % 6) === 0) render();
      },
      // N streamed tokens at once (e.g. a reasoning chunk spanning many tokens).
      tickN: function (n) {
        if (!streaming) return;
        n = Math.max(1, n | 0);
        liveCount += n; active = Math.max(active, liveBase + liveCount);
        ticks += n; render();
      },
      // Stream ended cleanly. RETAINED going forward = round-1 prompt (the exact
      // retained context + this user message) + the answer that's kept in history;
      // the turn's reasoning/tool tokens are NOT retained. LIVE then EMPTIES to 0 —
      // no generation is in flight at rest, so the active bar reads empty while CHAT
      // holds the cumulative footprint. No usage/prompt seen (older llama build) ->
      // fall back to the live estimate for retained before emptying LIVE.
      finishTurn: function (answerText) {
        if (streaming) {
          streaming = false;
          if (firstPrompt >= 0) retained = firstPrompt + estTokens(answerText || '');
          else if (active > retained) retained = active;
          active = 0;
        }
        render();
        if (ctxTendrils) ctxTendrils.stop();
        if (faviconCtl) faviconCtl.thinking(false);
      },
      // Turn errored: drop it; CHAT unchanged, LIVE empties to 0 (nothing in flight).
      abortTurn: function () {
        streaming = false; active = 0; render();
        if (ctxTendrils) ctxTendrils.stop();
        if (faviconCtl) faviconCtl.thinking(false);
      },
      // Estimated word budget for the handoff note based on the reserved headroom.
      wordBudget: function () {
        if (!nCtx) return 600;
        var headroomTok = Math.round(nCtx * cfg.headroom_fraction);
        return Math.max(150, Math.min(800, Math.round(headroomTok * 0.6)));
      }
    };
  })();

  /* ── Thinking-visual toggle (Settings → Appearance) ──
     Per-client preference in localStorage: 'on' (default) or 'off'. Controls the
     ambient tendril field that blooms behind the chat while the model generates.
     Independent of the global Reduce Motion setting: the field renders only when this
     toggle is On AND motion isn't reduced (the tendril module's applyGate ANDs them).
     When no canvas is available the choice is forced off and the On button disabled so
     the toggle stays honest. The two context bars always show. */
  (function wireThinkingVisual() {
    var btns = Array.prototype.slice.call(document.querySelectorAll('.viz-switch button'));
    var KEY = 'ayre.thinkingVisual';
    var canViz = !!ctxTendrils;

    function apply(state) {
      if (state !== 'off' && !canViz) state = 'off';
      if (ctxTendrils) ctxTendrils.enabled(state === 'on');
      btns.forEach(function (b) {
        b.setAttribute('aria-pressed', b.getAttribute('data-viz') === state ? 'true' : 'false');
      });
      try { localStorage.setItem(KEY, state); } catch (e) { /* private mode -> in-memory only */ }
    }

    btns.forEach(function (b) {
      if (!canViz && b.getAttribute('data-viz') === 'on') {
        b.disabled = true;
        b.title = 'Unavailable — this browser can’t draw the visual (no canvas support).';
      }
      b.addEventListener('click', function () { if (!b.disabled) apply(b.getAttribute('data-viz')); });
    });

    var saved = 'on';
    try { saved = localStorage.getItem(KEY) || 'on'; } catch (e) { /* default on */ }
    apply(saved);
  })();

  /* ── Reduce Motion tri-state (Settings → Appearance) ──
     Drives the shared reduceMotion module: System / On / Off. The module persists the
     choice, stamps :root, and notifies every animated surface; here we only paint the
     button states. System changes never alter the mode, so no subscription is needed. */
  (function wireMotionSwitch() {
    var btns = Array.prototype.slice.call(document.querySelectorAll('.motion-switch button'));
    if (!btns.length) return;
    function paint(mode) {
      btns.forEach(function (b) {
        b.setAttribute('aria-pressed', b.getAttribute('data-motion') === mode ? 'true' : 'false');
      });
    }
    btns.forEach(function (b) {
      b.addEventListener('click', function () {
        var m = b.getAttribute('data-motion');
        reduceMotion.set(m);
        paint(m);
      });
    });
    paint(reduceMotion.mode());
  })();

  /* ── Chat hardware monitor (offload split + temperatures) ──
     A small panel under the context meter: how many of the model's layers run on
     the GPU vs the CPU (the split the optimizer chose for this machine), with each
     component's live temperature beneath its bar. Fed by GET /api/telemetry. */
  var hwMon = (function wireHwMon() {
    var host = document.getElementById('hwMon');
    if (!host) return null;
    var gpuVal = document.getElementById('hwGpuVal');
    var cpuVal = document.getElementById('hwCpuVal');
    var gpuFill = document.getElementById('hwGpuFill');
    var cpuFill = document.getElementById('hwCpuFill');
    var gpuTemp = document.getElementById('hwGpuTemp');
    var cpuTemp = document.getElementById('hwCpuTemp');
    var note = document.getElementById('hwNote');
    var gpuLoad = document.getElementById('hwGpuLoad');
    var cpuLoad = document.getElementById('hwCpuLoad');
    var gpuLoadFill = document.getElementById('hwGpuLoadFill');
    var cpuLoadFill = document.getElementById('hwCpuLoadFill');

    // Colour the temperature as it climbs (protect-end-user-hardware): a glanceable
    // amber/red cue. Thresholds are deliberately conservative for sustained load.
    function setTemp(elT, c) {
      if (typeof c === 'number') {
        elT.textContent = c + '°C';
        elT.className = 'hw-temp' + (c >= 90 ? ' hot' : (c >= 80 ? ' warn' : ''));
      } else {
        elT.textContent = '—'; elT.className = 'hw-temp';  // unavailable on this box
      }
    }

    // Live utilization: one chip's percent, or "—" until a reading exists (GPU when
    // nvidia-smi is absent; CPU on its first sample, before the delta baseline). The
    // bar fades while idle so a busy chip stands out at a glance.
    function setLoad1(valEl, fillEl, pct) {
      if (typeof pct === 'number') {
        valEl.textContent = pct + '%';
        fillEl.style.width = Math.max(0, Math.min(100, pct)) + '%';
      } else {
        valEl.textContent = '—';
        fillEl.style.width = '0%';
      }
    }

    function setLoad(l) {
      setLoad1(gpuLoad, gpuLoadFill, l && l.gpu_pct);
      setLoad1(cpuLoad, cpuLoadFill, l && l.cpu_pct);
    }

    function setOffload(o) {
      var total = o && o.n_layers_total;
      var ngl = (o && typeof o.n_gpu_layers === 'number') ? o.n_gpu_layers : null;
      if (!total) {  // nothing known yet
        gpuVal.textContent = cpuVal.textContent = '—';
        gpuFill.style.width = cpuFill.style.width = '0%';
        if (note) note.hidden = true;
        return;
      }
      if (ngl === null) {  // total known, split not (model wasn't launched by Ayre)
        gpuVal.textContent = cpuVal.textContent = '? / ' + total;
        gpuFill.style.width = cpuFill.style.width = '0%';
        if (note) { note.hidden = false; note.textContent = 'Split unknown — start the model from Ayre to measure it.'; }
        return;
      }
      ngl = Math.max(0, Math.min(total, ngl));
      var cpu = total - ngl;
      var gp = Math.round(ngl / total * 100);
      gpuVal.textContent = ngl + ' / ' + total + ' (' + gp + '%)';
      cpuVal.textContent = cpu + ' / ' + total + ' (' + (100 - gp) + '%)';
      gpuFill.style.width = gp + '%';
      cpuFill.style.width = (100 - gp) + '%';
      if (note) note.hidden = true;
    }

    return {
      update: function (tel) {
        if (!tel || !tel.up) { host.hidden = true; return; }
        host.hidden = false;
        setOffload(tel.offload);
        var t = tel.temps || {};
        setTemp(gpuTemp, t.gpu_c);
        setTemp(cpuTemp, t.cpu_c);
        setLoad(tel.load);
      },
      hide: function () { host.hidden = true; }
    };
  })();

  /* Telemetry poll for the hardware monitor. Runs ONLY while the engine is up, so
     we never spawn nvidia-smi / PowerShell at idle; temps refresh a little faster
     than the 8s system poll. Started/stopped from renderSystem as health flips. */
  var TELEMETRY_MS = 5000;
  var telemetryTimer = null;
  function pollTelemetry() {
    getJSON('/api/telemetry').then(function (tel) {
      if (hwMon) hwMon.update(tel);
    }).catch(function () {});
  }
  function setTelemetry(up) {
    if (up && !telemetryTimer) {
      pollTelemetry();
      telemetryTimer = setInterval(pollTelemetry, TELEMETRY_MS);
    } else if (!up && telemetryTimer) {
      clearInterval(telemetryTimer); telemetryTimer = null;
      if (hwMon) hwMon.hide();
    }
  }

  /* ── Tip ticker ──────────────────────────────────────────────────────────
     Scrolls tips across the bottom of the chat view. Each tip enters from the
     right, hangs briefly when its start reaches the left edge, then scrolls off.
     Source of truth for tip content: the project design notes. */
  (function wireTipTicker() {
    var TIPS = [
      'Memory must be saved manually — say "Add to memory that:" to propose a note for your review. Ayre will not save it otherwise.',
      'A model\'s quant level (the Q-number in its filename) trades quality for size: Q4 is the balanced default, lower is smaller/faster but can be incoherent, higher is better but heavier. Hover a model in Setup for details.',
    ];

    var SPEED_IN  = 90;    // px/s — scroll from right edge to left edge
    var SPEED_OUT = 80;    // px/s — scroll from left edge off-screen
    var HANG_MS   = 2200;  // ms to hold at the left edge before scrolling away
    var GAP_MS    = 600;   // ms pause between tips

    var track = document.getElementById('tipTrack');
    var textEl = document.getElementById('tipText');
    if (!track || !textEl) return;

    var STATIC_HOLD_MS = 7000;   // reduced-motion: how long each tip holds before swapping (no slide)

    var idx = 0;
    var cycle = 0;               // bumped each next(); stale scheduled steps compare and bail
    var reduced = false;
    var started = false;

    function next() {
      var mine = ++cycle;
      textEl.textContent = TIPS[idx % TIPS.length];
      idx++;

      if (reduced) {
        // Reduced motion: keep rotating tips, but swap them in place with no slide.
        textEl.style.transition = 'none';
        textEl.style.left = '0';
        setTimeout(function () { if (mine === cycle) next(); }, STATIC_HOLD_MS);
        return;
      }

      var cw = track.offsetWidth;
      var tw = textEl.offsetWidth;
      if (!cw || !tw) { setTimeout(function () { if (mine === cycle) next(); }, 500); return; }

      // Snap to right edge (no transition)
      textEl.style.transition = 'none';
      textEl.style.left = cw + 'px';

      // Flush layout so the transition-none takes effect before we re-add transition
      void textEl.offsetWidth;

      // Phase 1: scroll to left edge
      var phase1 = Math.round(cw / SPEED_IN * 1000);
      textEl.style.transition = 'left ' + phase1 + 'ms linear';
      textEl.style.left = '0';

      setTimeout(function () {
        if (mine !== cycle) return;
        // Phase 2: hang at left edge (no transform change needed)
        setTimeout(function () {
          if (mine !== cycle) return;
          // Phase 3: scroll off left
          var phase3 = Math.round(tw / SPEED_OUT * 1000);
          textEl.style.transition = 'left ' + phase3 + 'ms linear';
          textEl.style.left = '-' + tw + 'px';

          setTimeout(function () { if (mine === cycle) next(); }, phase3 + GAP_MS);
        }, HANG_MS);
      }, phase1);
    }

    // React to Reduce Motion flips at runtime. Entering reduced mid-slide: snap the
    // current tip to rest (bumping `cycle` cancels the in-flight scroll steps) and
    // restart the rotation in static mode. Leaving reduced simply lets the next cycle
    // scroll again. The first launch is deferred to `started` so this immediate call
    // (subscribe fires now with the current state) can't double-start the loop.
    reduceMotion.subscribe(function (r) {
      var was = reduced;
      reduced = r;
      if (!started) return;
      if (r && !was) {
        cycle++;
        textEl.style.transition = 'none';
        textEl.style.left = '0';
        setTimeout(next, GAP_MS);
      }
    });

    started = true;
    setTimeout(next, reduced ? GAP_MS : 1800);  // brief delay so the UI settles before the first tip
  })();

  Ayre.reduceMotion = reduceMotion; Ayre.faviconCtl = faviconCtl;
  Ayre.ctxTendrils = ctxTendrils; Ayre.ctxMeter = ctxMeter;
  Ayre.hwMon = hwMon; Ayre.setTelemetry = setTelemetry;
})();
