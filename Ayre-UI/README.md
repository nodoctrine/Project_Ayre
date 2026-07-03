# Ayre-UI (component 5)

The single persistent shell — install **and** daily use in one app, different
sections via nav. Plain HTML/CSS/JS, no build step; a stdlib-only Python bridge
serves it and exposes a small JSON API. Offline, loopback-only, no CDN — the same
posture as Ayre-Setup, so it runs in the clean VM.

## What's built (v1 bones)

The **bones**, per the UI bones-vs-skin rule:

- **Persistent shell** — topbar, nav rail, five sections (Chat / Setup /
  Updater / Tools / Settings), no flash-and-close.
- **Theme system** — six runtime-swappable themes; every color/font/effect is a
  CSS variable (`static/app.css`). Office Dark is the default; the signature
  gunmetal/orange/cyan palette is the **Ayre** theme.
- **Setup section is LIVE** — it renders the real two-tier doctor from
  Ayre-Setup (`/api/doctor`): which required files are present/missing, whether a
  chat model was added, and the doctor's own actionable hints. No mock data.
- **Topbar chips are LIVE** (`/api/system`) — whether llama-server is answering
  right now, model presence, and setup status.

Sections whose backends aren't built yet (Chat, Updater, Tools, Settings) show
honest "backend not built yet" placeholders — they fill in as those components
land. The polished per-section *skin* lives in the internal UI prototype for
reference; port authored copy in as each component is built.

## The UI ↔ backend seam

`ayre_ui` talks to Python over **HTTP + JSON** — the same shape llama-server
already speaks. A native webview wrapper later is a platform-seam swap, not a
rewrite. The bridge reaches into Ayre-Setup in exactly one place (it adds the
sibling `Ayre-Setup/` folder to `sys.path` to import `ayre_setup`).

    ayre_ui/
      server.py    stdlib http.server bridge: serves static/ + /api/{doctor,system}
      cli.py       `python -m ayre_ui` — launch + open the browser
    static/
      index.html   the shell (topbar, nav, five sections)
      app.css      vendored theme system + layout
      app.js       theme switch, nav, live Setup/topbar rendering

## Usage

Run from this directory (so the `ayre_ui` package is importable):

    python -m ayre_ui                # serve + open the browser
    python -m ayre_ui --no-browser   # serve only (headless VM)
    python -m ayre_ui --port 9000    # override host/port

Host/port default to `config/runtime.json -> ui` (variable-first; `localhost:2500`),
distinct from llama-server's `:8080` so the two never collide. `AYRE_USB_ROOT`
overrides root detection (the USB may mount anywhere).

## Choosing the UI port (Settings)

**Settings → Connection** lets the user pick any 4-digit port (1000–9999) if 2500
is taken. On save the port is validated and **bind-probed** (real "that one's
free / taken" feedback) and rejected if it collides with llama-server's port.
The choice is written to a **machine-local overlay** —
`config/user_settings.json` (gitignored; rides along on the USB copy, never dirties
the committed `runtime.json`) — and read on top of the default at launch. Changing
the port needs a **restart** (the browser is on the old port); the UI says so and
shows the new address. This overlay is the persistence home for future Settings
(persona, theme, toggle defaults).

## API

- `GET  /api/doctor` — the live three-tier presence check (`DoctorReport.to_dict()`).
- `GET  /api/system` — UI address (effective + default port), llama-server health, model/setup status.
- `POST /api/ui-port` — `{ "port": <1000-9999> }` → validate + bind-probe + persist to the user-settings overlay. Returns `{ ok, message | error, url, needs_restart }`.
