"""llama-server proxy: health, /props, /tokenize, and the chat context-meter config.

Best-effort reads of the running engine over loopback (model name, live context window,
exact token counts). Every call fail-soft: a hiccup returns empty/ok:false and callers
degrade (chip falls back, meter hides) rather than break a turn.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from ayre_setup.config import load_runtime

def _llama_props(base: str) -> dict:
    """Best-effort read of llama-server's /props -- the live truth two UI bits need:
    the ACTIVE model's filename (topbar chip, not the first file on disk) and the
    loaded context window `n_ctx` (the chat context meter sizes itself to the REAL
    window, not a tier estimate). Returns {} on any hiccup (endpoint down / shape
    drift); callers degrade gracefully (chip falls back, meter hides)."""
    try:
        with urllib.request.urlopen(f"{base}/props", timeout=1.5) as r:
            data = json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return {}
    out: dict = {}
    name = Path(data.get("model_path") or data.get("model") or "").name
    if name:
        out["model"] = name
    # n_ctx lives under default_generation_settings in current llama.cpp; tolerate a
    # top-level fallback so a shape change degrades (meter hides) rather than breaks.
    gen = data.get("default_generation_settings") or {}
    n_ctx = gen.get("n_ctx") or data.get("n_ctx")
    if isinstance(n_ctx, int) and n_ctx > 0:
        out["n_ctx"] = n_ctx
    return out

# Last good /props read, kept across polls. /props can transiently fail (e.g. it
# queues behind a busy generation and times out the 1.5s read); without this, a
# single miss would drop model/n_ctx from /api/system for one poll and the chat
# meter would zero/hide itself mid-conversation. Reused while llama stays healthy;
# cleared when it goes down (the next load may be a different model/window).
_LAST_PROPS: dict = {}


def _llama_health() -> dict:
    """Is llama-server answering right now? Real state for the topbar chip. When up,
    also reports which model is loaded (the active one, not the inventory) and the
    loaded context window n_ctx (so the chat meter can size itself). A transient
    /props miss reuses the last good read so the meter doesn't flicker to zero."""
    global _LAST_PROPS
    rt = load_runtime()
    host, port = rt.get("host", "127.0.0.1"), rt.get("port", 8080)
    base = f"http://{host}:{port}"
    healthy = False
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=1.5) as r:
            healthy = r.status == 200
    except (urllib.error.URLError, OSError):
        healthy = False
    if healthy:
        props = _llama_props(base)
        if props.get("n_ctx"):
            _LAST_PROPS = props          # complete read -> remember it
        elif _LAST_PROPS.get("n_ctx"):
            props = _LAST_PROPS          # transient miss while up -> reuse last good
    else:
        _LAST_PROPS = {}                 # engine down -> forget (next load may differ)
        props = {}
    return {"host": host, "port": port, "healthy": healthy,
            "model": props.get("model"), "n_ctx": props.get("n_ctx")}

# Chat context-meter knobs (Slice 3 / Context_Management.md). Read from
# config/runtime.json -> context_meter; these defaults keep the meter sane if the
# block is absent on an older config. headroom_fraction = the top slice of the
# loaded window reserved for the handoff summary; zones = the green/yellow/red
# boundaries as a fraction of the USABLE window (total minus headroom).
_CONTEXT_METER_DEFAULTS = {"headroom_fraction": 0.05,
                           "zones": {"yellow_at": 0.70, "red_at": 0.85},
                           # Pre-send warning thresholds (Context_Management.md). chat_* are
                           # fractions of the USABLE window (total minus headroom); live_at is
                           # a fraction of the FULL window (n_ctx) -- the hard single-turn
                           # generation limit. Read by the composer's pre-send projection.
                           "warnings": {"chat_high_at": 0.80, "chat_full_at": 0.95,
                                        "live_at": 0.95}}


def _context_meter_config() -> dict:
    """The meter's shaping knobs (NOT the measurement -- occupancy comes live from
    llama-server token usage). Read each call so a config edit shows up on the next
    poll without restarting the bridge."""
    cfg = load_runtime().get("context_meter", {}) or {}
    zones = cfg.get("zones", {}) or {}
    d_zones = _CONTEXT_METER_DEFAULTS["zones"]
    warns = cfg.get("warnings", {}) or {}
    d_warns = _CONTEXT_METER_DEFAULTS["warnings"]
    return {
        "headroom_fraction": float(cfg.get("headroom_fraction",
                                           _CONTEXT_METER_DEFAULTS["headroom_fraction"])),
        "zones": {"yellow_at": float(zones.get("yellow_at", d_zones["yellow_at"])),
                  "red_at": float(zones.get("red_at", d_zones["red_at"]))},
        "warnings": {"chat_high_at": float(warns.get("chat_high_at", d_warns["chat_high_at"])),
                     "chat_full_at": float(warns.get("chat_full_at", d_warns["chat_full_at"])),
                     "live_at": float(warns.get("live_at", d_warns["live_at"]))},
    }


def tokenize_text(text: str) -> dict:
    """Count the tokens in `text` EXACTLY via llama-server's /tokenize -- powers the
    composer's pre-send projection (Slice 3b): the browser shows the real token cost
    of a draft against the usable window before sending, instead of guessing. Input
    is what the user controls and what arrives in lumps (a big paste), so counting it
    exactly is the useful half (the reply length is unknowable in advance). Read-only
    and fail-soft: any hiccup returns ok:false and the UI falls back to a rough
    character estimate rather than blocking the send."""
    rt = load_runtime()
    base = f"http://{rt.get('host', '127.0.0.1')}:{rt.get('port', 8080)}"
    body = json.dumps({"content": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/tokenize", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return {"ok": False}
    tokens = data.get("tokens")
    if not isinstance(tokens, list):
        return {"ok": False}
    return {"ok": True, "count": len(tokens)}
