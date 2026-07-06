"""The HTTP request handler: routing for the static shell + the ~33-route JSON API.

AyreUIHandler maps GET/POST/DELETE routes to the helper clusters (memory, projects, skills,
tools, RAG, llama, launch, hwmon). State-changing routes pass an Origin CSRF guard first
(_origin_ok); the chat turn is delegated to chat.chat_proxy. HTTP/1.0 (no keep-alive) is a
load-bearing constraint -- see the F3 note in the class.
"""
from __future__ import annotations

import base64
import datetime
import json
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ayre_setup.preflight import run_doctor
from ayre_setup.config import reranker_items, models_dir

from .paths import STATIC_DIR
from .uiport import _ui_config, validate_ui_port, save_ui_port
from .llama import _llama_health, _context_meter_config, tokenize_text
from .hwmon import _telemetry_state
from .launch import (fit_check, optimizer_state, preset_predictions,
                     save_optimizer_settings, start_llama, stop_llama)
from .memory import (_memory_state, _clear_memory, _set_memory_enabled,
                     _memory_draft_content, _promote_draft, _discard_draft,
                     _memory_warning_chars, _save_memory_warning_chars)
from .rag_bridge import _rag_state, _set_rag_toggle, _RAG_AVAILABLE
from .tools import (_tools_state, _set_tool_enabled, _TOOL_META, _set_write_confirm,
                    _confirm_pending_write, _deny_pending_write, _handoff_cooldown,
                    _handoff_min_turns, _save_handoff_cooldown, _HANDOFF_FILENAME_RE)
from .projects import (_list_projects, _active_project, _create_project,
                       _set_active_project, _workspace_file_list, _workspace_upload,
                       _workspace_delete, _project_path)
from .skills import (_load_skills, _save_skills, _sanitize_skill_field,
                     _skills_max_count, _SKILL_TITLE_MAX_WORDS, _SKILL_DESC_MAX_WORDS)
from .chat import chat_proxy

def _system_state(bound_port: int | None = None) -> dict:
    report = run_doctor()
    ui = _ui_config()
    if bound_port is not None:
        # Report the port actually being served (the truth the browser is on),
        # which can differ from the configured value under a --port override.
        ui = {**ui, "port": bound_port}
    return {
        "ui": ui,
        "llama": _llama_health(),
        "context": _context_meter_config(),
        "required_ok": report.required_ok,
        "has_model": report.has_model,
        "models": (
            [{"name": p.name, "selectable": True} for p in report.models]
            + [
                {"name": r["file"], "selectable": False, "reason": r["reason"]}
                for r in reranker_items()
                if (models_dir() / r["file"]).exists()
            ]
        ),
        "handoff_cooldown_seconds": _handoff_cooldown(),
        "handoff_min_substantive_turns": _handoff_min_turns(),
        "memory_warning_chars": _memory_warning_chars(),
    }


class AyreUIHandler(BaseHTTPRequestHandler):
    server_version = "AyreUI/0.1"

    # NOTE (F3): this handler relies on the stdlib default protocol_version = "HTTP/1.0"
    # (no keep-alive -- each connection serves one request, then closes). Several
    # handlers respond WITHOUT draining the request body (the do_POST/do_DELETE 404
    # fall-throughs, /api/memory/clear, /api/memory/draft/discard, /api/stop). That is
    # safe ONLY because the connection closes afterward, so leftover body bytes are
    # discarded with the socket. Do NOT set protocol_version = "HTTP/1.1" (to enable
    # keep-alive) without first making EVERY handler drain its request body -- otherwise
    # an undrained body desyncs the next request on the reused connection (request
    # smuggling). See Security_Patch_Devlog.md (API sweep, F3).

    # --- helpers ---------------------------------------------------------
    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # These static assets (index.html / app.js / app.css) are LOCAL and MUTABLE --
        # they change every time the app is updated. BaseHTTPRequestHandler sends no
        # Last-Modified/ETag, so with no cache directive the browser applies heuristic
        # caching and can execute a STALE app.js after an update (e.g. a fresh index.html
        # paired with an old, un-gated app.js). Match the JSON/SSE handlers' no-store so a
        # reload always runs the current build. Cost is nil -- these are served over loopback.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _resolve_static(self, rel: str) -> Path | None:
        """Map a relative path to a file under STATIC_DIR, rejecting traversal."""
        rel = rel.lstrip("/") or "index.html"
        candidate = (STATIC_DIR / rel).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return None  # escaped the static root
        return candidate

    def _origin_ok(self) -> bool:
        """CSRF guard for state-changing requests (POST/DELETE). The bridge has NO
        authentication, so 'did this request come from our own UI?' is enforced via
        the Origin header. A cross-site page's fetch carries its own Origin
        (e.g. https://evil.com); ours carries our loopback origin. Fail-safe:
          - Origin present + matches our loopback origin -> allow.
          - Origin present + mismatched -> block (the cross-site-fetch attack).
          - Origin absent -> allow. A browser CANNOT suppress Origin on a cross-origin
            request, so an absent Origin is a non-browser client (curl, tests), never
            the threat this guards. This also means our own same-origin UI is never
            blocked, regardless of per-browser Origin quirks."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = self.server.server_address[1]
        allowed = {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
        return origin in allowed

    # --- routing ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (stdlib casing)
        route = self.path.split("?", 1)[0]
        if route == "/api/doctor":
            self._send_json(run_doctor().to_dict())
            return
        if route == "/api/system":
            self._send_json(_system_state(bound_port=int(self.server.server_address[1])))
            return
        if route == "/api/telemetry":
            self._send_json(_telemetry_state())
            return
        if route == "/api/fit":
            # ?model=<detected .gguf>; absent -> the tier's auto-pick. Read-only.
            # A3 what-if: &preview=1 (+ optional &context=N&n_gpu_layers=K) evaluates
            # exactly those manual values, ignoring the saved override.
            q = parse_qs(urlparse(self.path).query)
            model = (q.get("model") or [None])[0]
            if model is not None:
                model = model.strip() or None
            preview = (q.get("preview") or [""])[0] in ("1", "true")
            def _qint(name):
                raw = (q.get(name) or [""])[0].strip()
                if not raw:
                    return None
                try:
                    return int(raw)
                except ValueError:
                    return None
            self._send_json(fit_check(model, context=_qint("context"),
                                      n_gpu_layers=_qint("n_gpu_layers"),
                                      preview=preview))
            return
        if route == "/api/optimizer":
            self._send_json(optimizer_state())
            return
        if route == "/api/optimizer/preview":
            # ?model=<detected .gguf>; absent -> the auto-pick. One solve per preset.
            q = parse_qs(urlparse(self.path).query)
            model = (q.get("model") or [None])[0]
            if model is not None:
                model = model.strip() or None
            self._send_json(preset_predictions(model))
            return
        if route == "/api/memory":
            self._send_json(_memory_state())
            return
        if route == "/api/rag":
            self._send_json({"ok": True, **_rag_state()})
            return
        if route == "/api/memory/draft":
            content = _memory_draft_content()
            self._send_json({"ok": True, "content": content or "",
                             "has_draft": content is not None,
                             "char_count": len(content) if content else 0})
            return
        if route == "/api/tools":
            self._send_json({"ok": True, "tools": _tools_state()})
            return
        if route == "/api/projects":
            self._send_json({
                "ok": True,
                "projects": _list_projects(),
                "active": _active_project(),
            })
            return
        if route == "/api/skills":
            self._send_json({"ok": True, "skills": _load_skills()})
            return
        if route == "/api/workspace/files":
            q = parse_qs(urlparse(self.path).query)
            project = (q.get("project") or [None])[0]
            self._send_json({"ok": True, "files": _workspace_file_list(project)})
            return
        if route == "/api/handoff/latest":
            try:
                proj_path = _project_path(create=False)  # GET: must not create the dir (F1)
                files = ([f for f in proj_path.iterdir()
                          if f.is_file() and _HANDOFF_FILENAME_RE.match(f.name)]
                         if proj_path.is_dir() else [])
                if not files:
                    self._send_json({"ok": False})
                else:
                    latest = max(files, key=lambda f: f.name)
                    content = latest.read_text(encoding="utf-8", errors="replace")
                    self._send_json({"ok": True, "filename": latest.name, "content": content})
            except (OSError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)})
            return
        # Static: "/" -> index.html; "/static/<f>" -> STATIC_DIR/<f>.
        if route in ("/", "/index.html"):
            rel = "index.html"
        elif route.startswith("/static/"):
            rel = route[len("/static/"):]
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        target = self._resolve_static(rel)
        if target is None or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        self._send_file(target)

    def do_POST(self) -> None:  # noqa: N802 (stdlib casing)
        if not self._origin_ok():
            self._send_json({"ok": False, "error": "Cross-origin request blocked."},
                            HTTPStatus.FORBIDDEN)
            return
        route = self.path.split("?", 1)[0]
        if route == "/api/chat":
            chat_proxy(self)
            return
        if route == "/api/memory/toggle":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                enabled = bool(payload.get("enabled", not _memory_enabled()))
            except (ValueError, json.JSONDecodeError):
                enabled = not _memory_enabled()
            _set_memory_enabled(enabled)
            self._send_json({"ok": True, **_memory_state()})
            return
        if route == "/api/memory/clear":
            self._send_json(_clear_memory())
            return
        if route == "/api/rag/toggle":
            # Toggle either RAG on/off (`enabled`) or the retrieved-context preview
            # (`show_retrieved_context`), persisted per-machine in user_settings.
            if not _RAG_AVAILABLE:
                self._send_json({"ok": False, "error": "RAG is unavailable."})
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                key = payload.get("key", "enabled")
                enabled = payload.get("enabled")
            except (ValueError, json.JSONDecodeError):
                self._send_json({"ok": False, "error": "Bad request."}, HTTPStatus.BAD_REQUEST)
                return
            if key not in ("enabled", "show_retrieved_context"):
                self._send_json({"ok": False, "error": f"Unknown toggle: {key!r}"})
                return
            if not isinstance(enabled, bool):
                self._send_json({"ok": False, "error": "enabled must be a boolean"})
                return
            _set_rag_toggle(key, enabled)
            self._send_json({"ok": True, **_rag_state()})
            return
        if route == "/api/memory/draft/promote":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                content = payload.get("content", "")
            except (ValueError, json.JSONDecodeError):
                self._send_json({"ok": False, "error": "Bad request."}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(content, str):
                self._send_json({"ok": False, "error": "content must be a string."},
                                HTTPStatus.BAD_REQUEST)
                return
            self._send_json(_promote_draft(content))
            return
        if route == "/api/memory/draft/discard":
            self._send_json(_discard_draft())
            return
        if route == "/api/tools/toggle":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                name = payload.get("name", "")
                enabled = payload.get("enabled")
            except (ValueError, json.JSONDecodeError):
                self._send_json({"ok": False, "error": "Bad request."}, HTTPStatus.BAD_REQUEST)
                return
            if name not in _TOOL_META:
                self._send_json({"ok": False, "error": f"Unknown tool: {name!r}"})
                return
            if not isinstance(enabled, bool):
                self._send_json({"ok": False, "error": "enabled must be a boolean"})
                return
            _set_tool_enabled(name, enabled)
            self._send_json({"ok": True, "tools": _tools_state()})
            return
        if route == "/api/tools/write-confirm":
            # Toggle the Write-File confirmation gate (default ON). Separate from the
            # tool's enable/disable toggle: this controls Allow/Deny-before-write.
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                enabled = payload.get("enabled")
            except (ValueError, json.JSONDecodeError):
                self._send_json({"ok": False, "error": "Bad request."}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(enabled, bool):
                self._send_json({"ok": False, "error": "enabled must be a boolean"})
                return
            _set_write_confirm(enabled)
            self._send_json({"ok": True, "tools": _tools_state()})
            return
        if route == "/api/write/confirm":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                token = payload.get("token", "")
            except (ValueError, json.JSONDecodeError):
                self._send_json({"ok": False, "error": "Bad request."}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(token, str) or not token:
                self._send_json({"ok": False, "error": "token is required."}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(_confirm_pending_write(token))
            return
        if route == "/api/write/deny":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                token = payload.get("token", "")
            except (ValueError, json.JSONDecodeError):
                self._send_json({"ok": False, "error": "Bad request."}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(token, str) or not token:
                self._send_json({"ok": False, "error": "token is required."}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(_deny_pending_write(token))
            return
        if route == "/api/projects":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                name = payload.get("name", "")
            except (ValueError, json.JSONDecodeError):
                self._send_json({"ok": False, "error": "Bad request."}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(name, str) or not name.strip():
                self._send_json({"ok": False, "error": "name is required."})
                return
            result = _create_project(name)
            if result.get("ok"):
                result["projects"] = _list_projects()
                result["active"] = _active_project()
            self._send_json(result)
            return
        if route == "/api/projects/active":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                name = payload.get("name", "")
            except (ValueError, json.JSONDecodeError):
                self._send_json({"ok": False, "error": "Bad request."}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(name, str) or not name.strip():
                self._send_json({"ok": False, "error": "name is required."})
                return
            projects = _list_projects()
            if not any(p["name"] == name for p in projects):
                self._send_json({"ok": False, "error": f"Project '{name}' not found."})
                return
            _set_active_project(name)
            self._send_json({"ok": True, "active": name})
            return
        if route == "/api/skills":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except (ValueError, json.JSONDecodeError):
                self._send_json({"ok": False, "error": "Bad request."}, HTTPStatus.BAD_REQUEST)
                return
            title = (payload.get("title") or "").strip()
            description = (payload.get("description") or "").strip()
            workflow = (payload.get("workflow") or "").strip()
            skill_id = payload.get("id") or None
            if not title:
                self._send_json({"ok": False, "error": "title is required."})
                return
            if len(title.split()) > _SKILL_TITLE_MAX_WORDS:
                self._send_json({"ok": False, "error":
                                 f"Title must be {_SKILL_TITLE_MAX_WORDS} words or fewer."})
                return
            if not description:
                self._send_json({"ok": False, "error": "description is required."})
                return
            if len(description.split()) > _SKILL_DESC_MAX_WORDS:
                self._send_json({"ok": False, "error":
                                 f"Description must be {_SKILL_DESC_MAX_WORDS} words or fewer."})
                return
            if not workflow:
                self._send_json({"ok": False, "error": "workflow is required."})
                return
            # Structure-safety pass: skill text is injected into the system prompt,
            # so no field may be able to forge the prompt's delimiters.
            title, err = _sanitize_skill_field(title, single_line=True)
            if err:
                self._send_json({"ok": False, "error": f"Title: {err}"})
                return
            description, err = _sanitize_skill_field(description, single_line=True)
            if err:
                self._send_json({"ok": False, "error": f"Description: {err}"})
                return
            workflow, err = _sanitize_skill_field(workflow, single_line=False)
            if err:
                self._send_json({"ok": False, "error": f"Workflow: {err}"})
                return
            skills = _load_skills()
            # Duplicate titles would make invocation ambiguous (title IS the
            # invocation key), so reject them case-insensitively.
            for s in skills:
                if s.get("id") != skill_id and s.get("title", "").lower() == title.lower():
                    self._send_json({"ok": False, "error":
                                     f"A skill named “{s.get('title')}” already exists."})
                    return
            if not skill_id and len(skills) >= _skills_max_count():
                self._send_json({"ok": False, "error":
                                 (f"Skill limit reached ({_skills_max_count()}). Every skill's "
                                  "title and description ride along in every chat turn, so the "
                                  "count is capped — delete one you no longer use first.")})
                return
            if skill_id:
                updated = False
                for s in skills:
                    if s.get("id") == skill_id:
                        s["title"] = title
                        s["description"] = description
                        s["workflow"] = workflow
                        updated = True
                        break
                if not updated:
                    self._send_json({"ok": False, "error": f"Skill not found: {skill_id!r}"})
                    return
            else:
                skills.append({
                    "id": str(int(time.time() * 1000)),
                    "title": title,
                    "description": description,
                    "workflow": workflow,
                })
            _save_skills(skills)
            self._send_json({"ok": True, "skills": skills})
            return
        if route == "/api/workspace/upload":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                name = payload.get("name", "")
                b64 = payload.get("content_b64", "")
            except (ValueError, json.JSONDecodeError):
                self._send_json({"ok": False, "error": "Bad upload request."}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(name, str) or not isinstance(b64, str):
                self._send_json({"ok": False, "error": "Missing name or content."}, HTTPStatus.BAD_REQUEST)
                return
            try:
                data = base64.b64decode(b64)
            except Exception:
                self._send_json({"ok": False, "error": "Invalid base64 content."}, HTTPStatus.BAD_REQUEST)
                return
            project = payload.get("project") if isinstance(payload.get("project"), str) else None
            self._send_json(_workspace_upload(name, data, project or None))
            return
        if route == "/api/tokenize":
            # {"text": <draft>} -> {"ok", "count"}; powers the pre-send projection.
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                text = payload.get("text")
            except (ValueError, json.JSONDecodeError):
                self._send_json({"ok": False, "error": "Bad tokenize request."}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(text, str):
                self._send_json({"ok": False, "error": "No text to tokenize."}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(tokenize_text(text))
            return
        if route == "/api/optimizer":
            # A3: persist the preset choice and/or the manual override (per-machine,
            # via Ayre-Setup's half of user_settings.json). Body contract is in
            # save_optimizer_settings; response = the fresh optimizer state.
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except (ValueError, json.JSONDecodeError):
                self._send_json({"ok": False, "error": "Bad request."}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(payload, dict):
                self._send_json({"ok": False, "error": "Send a JSON object."},
                                HTTPStatus.BAD_REQUEST)
                return
            self._send_json(save_optimizer_settings(payload))
            return
        if route == "/api/start":
            model = None
            force = False
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    m = payload.get("model")
                    if isinstance(m, str) and m.strip():
                        model = m.strip()
                    force = bool(payload.get("force"))
            except (ValueError, json.JSONDecodeError):
                model = None
            self._send_json(start_llama(model, force=force))
            return
        if route == "/api/stop":
            result = stop_llama()
            if result.get("ok") and result.get("was_running"):
                port = int(self.server.server_address[1])
                print(f"Ayre: model stopped and unloaded. Ayre is still running at "
                      f"http://localhost:{port} -- press Ctrl+C here only to fully quit Ayre.")
            self._send_json(result)
            return
        if route == "/api/dump-chat":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except (ValueError, json.JSONDecodeError):
                self._send_json({"ok": False, "error": "Bad request."}, HTTPStatus.BAD_REQUEST)
                return
            msgs = payload.get("messages", [])
            # Validate shape up front (F2): a non-list, or non-dict items, would make
            # m.get() below raise AttributeError -- not caught by the write except, so
            # it would drop the connection. Reject a bad container; skip bad items.
            if not isinstance(msgs, list):
                self._send_json({"ok": False, "error": "messages must be a list."},
                                HTTPStatus.BAD_REQUEST)
                return
            try:
                now = datetime.datetime.now()
                filename = now.strftime("contextlimit-chatlog-%Y-%m-%d_%H-%M.md")
                lines = [
                    "# Ayre Chat Log — Context Limit Reached\n\n",
                    f"Saved: {now.strftime('%Y-%m-%d %H:%M')}  \n",
                    "This log was saved automatically when the context limit was reached.\n\n---\n\n",
                ]
                for m in msgs:
                    if not isinstance(m, dict):
                        continue  # skip malformed entries rather than crash
                    role = str(m.get("role", "")).capitalize()
                    content = str(m.get("content", ""))
                    lines.append(f"**{role}:**\n\n{content}\n\n---\n\n")
                ((_project_path()) / filename).write_text("".join(lines), encoding="utf-8")
                self._send_json({"ok": True, "filename": filename})
            except (OSError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)})
            return
        if route == "/api/handoff-cooldown":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                seconds = int(payload.get("seconds"))
            except (ValueError, TypeError, json.JSONDecodeError):
                self._send_json({"ok": False, "error": "Send {seconds: <number>}."})
                return
            if not (30 <= seconds <= 3600):
                self._send_json({"ok": False, "error": "Cooldown must be between 30 and 3600 seconds."})
                return
            _save_handoff_cooldown(seconds)
            self._send_json({"ok": True, "seconds": seconds})
            return
        if route == "/api/memory/warning-threshold":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                chars = int(payload.get("chars"))
            except (ValueError, TypeError, json.JSONDecodeError):
                self._send_json({"ok": False, "error": "Send {chars: <number>}."})
                return
            if not (200 <= chars <= 50000):
                self._send_json({"ok": False, "error": "Warning threshold must be between 200 and 50,000 characters."})
                return
            _save_memory_warning_chars(chars)
            self._send_json({"ok": True, "chars": chars})
            return
        if route != "/api/ui-port":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        # Parse {"port": <int>}
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
            port = int(payload.get("port"))
        except (ValueError, TypeError, json.JSONDecodeError):
            self._send_json({"ok": False, "error": "Enter a 4-digit port number."})
            return

        current_port = int(self.server.server_address[1])
        err = validate_ui_port(port, current_port=current_port)
        if err:
            self._send_json({"ok": False, "error": err})
            return
        try:
            save_ui_port(port)
        except OSError as exc:
            self._send_json({"ok": False, "error": f"Could not save the setting: {exc}"})
            return

        same = port == current_port
        self._send_json({
            "ok": True,
            "port": port,
            "url": f"http://localhost:{port}/",
            "needs_restart": not same,
            "message": (
                "Already serving on this port."
                if same
                else f"Saved. Restart Ayre, then open http://localhost:{port}/"
            ),
        })

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._origin_ok():
            self._send_json({"ok": False, "error": "Cross-origin request blocked."},
                            HTTPStatus.FORBIDDEN)
            return
        route = self.path.split("?", 1)[0]
        if route.startswith("/api/skills/"):
            skill_id = route[len("/api/skills/"):]
            if not skill_id:
                self._send_json({"ok": False, "error": "Missing skill ID."}, HTTPStatus.BAD_REQUEST)
                return
            skills = _load_skills()
            new_skills = [s for s in skills if s.get("id") != skill_id]
            if len(new_skills) == len(skills):
                self._send_json({"ok": False, "error": f"Skill not found: {skill_id!r}"})
                return
            _save_skills(new_skills)
            self._send_json({"ok": True, "skills": new_skills})
            return
        if route == "/api/workspace/file":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                name = payload.get("name", "")
                project = payload.get("project")
            except (ValueError, json.JSONDecodeError):
                self._send_json({"ok": False, "error": "Bad request."}, HTTPStatus.BAD_REQUEST)
                return
            project = project if isinstance(project, str) and project.strip() else None
            self._send_json(_workspace_delete(str(name), project))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, fmt: str, *args) -> None:
        # Quiet by default; one concise line per request to stderr.
        sys.stderr.write("ayre-ui %s\n" % (fmt % args))
