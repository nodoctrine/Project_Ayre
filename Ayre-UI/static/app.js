/* Ayre-UI shell logic was split into six files on 2026-07-05 for readability.
   This file is no longer loaded (see index.html). Its code now lives in, in load order:
     core.js      shared window.Ayre namespace + helpers (theme, nav, esc/el/getJSON, connection)
     visuals.js   reduced-motion, favicon, tendrils, context meter, hardware monitor, telemetry, tips
     chat.js      markdown renderer, RAG sources, chatCtl stream, quick-actions, URL modal, copy-code
     setup.js     renderSystem, Start/Stop + optimizer (startCtl), doctor, health poll
     workspace.js file manager, projects, tool panel, RAG library, skill builder
     settings.js  port, handoff cooldown, memory-warning, clear-memory, memory chip + review
   Kept as a pointer (not deleted) so a cold reader lands here. Safe to remove entirely. */
