"""RAG grounding bridge (component 4, v0): config, toggles, and per-turn injection.

Retrieves BM25 hits from the local Wikipedia FTS5 index and splices an EPHEMERAL user-role
reference block before the current turn. Defensive by design: a missing/broken Ayre-RAG
package degrades to "RAG unavailable" (all uses guard on _RAG_AVAILABLE) and injection NEVER
raises into the chat path.
"""
from __future__ import annotations

from .settings import _load_user_settings, _save_user_settings
from .llama import tokenize_text, _llama_props

# RAG (component 4, v0). Import is defensive: RAG is additive + default-off, so a
# missing/broken Ayre-RAG package must degrade to "RAG unavailable", never break the
# UI. All later uses guard on `_RAG_AVAILABLE`.
try:
    from ayre_rag import inject as rag_inject  # noqa: E402
    from ayre_rag import retrieve as rag_retrieve  # noqa: E402
    from ayre_rag.config import RagConfig, load_config_safe as _load_rag_config  # noqa: E402
    import dataclasses as _dataclasses  # noqa: E402
    _RAG_AVAILABLE = True
except Exception:  # noqa: BLE001 -- any import failure just disables RAG
    _RAG_AVAILABLE = False

def _rag_config() -> "RagConfig | None":
    """The active RagConfig: rag.json defaults with the per-machine toggle overrides
    applied. None when the RAG package is unavailable (import failed)."""
    if not _RAG_AVAILABLE:
        return None
    cfg = _load_rag_config()  # forgiving: a broken rag.json -> all-default, RAG off
    overlay = _load_user_settings().get("rag", {}) or {}
    changes: dict = {}
    if "enabled" in overlay:
        changes["enabled"] = bool(overlay["enabled"])
    if "show_retrieved_context" in overlay:
        changes["show_retrieved_context"] = bool(overlay["show_retrieved_context"])
    return _dataclasses.replace(cfg, **changes) if changes else cfg


def _set_rag_toggle(key: str, enabled: bool) -> None:
    """Persist a RAG toggle (`enabled` | `show_retrieved_context`) per-machine."""
    data = _load_user_settings()
    data.setdefault("rag", {})[key] = bool(enabled)
    _save_user_settings(data)


def _rag_state() -> dict:
    """RAG library section state: live index status + the effective toggle values.
    Never raises -- index_status is a non-blocking probe (three-tier-doctor posture)."""
    if not _RAG_AVAILABLE:
        return {"available": False, "enabled": False, "ready": False,
                "error": "RAG package not installed."}
    cfg = _rag_config()
    status = rag_retrieve.index_status(cfg)
    status["available"] = True
    status["show_retrieved_context"] = cfg.show_retrieved_context
    return status


def _rag_token_counter(base: str):
    """A str->int token counter for context_fraction enforcement, backed by
    llama-server's EXACT /tokenize (the same tokenizer the model uses) so the ceiling
    is real, not estimated. Falls back to a conservative chars-per-token estimate on
    any hiccup so grounding never fails just because a count was unavailable."""
    def count(text: str) -> int:
        res = tokenize_text(text)  # POST /tokenize; {"ok":True,"count":N} or {"ok":False}
        if res.get("ok") and isinstance(res.get("count"), int):
            return res["count"]
        return max(1, len(text) // 4)
    return count


def _maybe_inject_rag(messages: list, query_text: str, base: str) -> "object | None":
    """If RAG is enabled + a query exists, retrieve grounding and splice a user-role
    reference block into `messages` immediately before the last user message. Returns
    the Injection (for the sources event) or None when nothing was injected.

    SECURITY: injection is EPHEMERAL -- `messages` is a per-request list (rebuilt from the
    browser's payload every turn), so the block reaches the model this turn only and
    is never persisted into stored history. NEVER raises into the chat path -- any
    failure just skips grounding (retrieval already swallows the common cases)."""
    if not _RAG_AVAILABLE or not query_text:
        return None
    try:
        cfg = _rag_config()
        if cfg is None or not cfg.enabled:
            return None
        hits = rag_retrieve.retrieve(query_text, cfg)
        if not hits:
            return None
        n_ctx = _llama_props(base).get("n_ctx")
        injection = rag_inject.build_injection(
            hits, cfg, n_ctx=n_ctx, count_tokens=_rag_token_counter(base))
        if not injection.text:
            return None
        # Splice before the LAST user message (the real current turn).
        insert_at = len(messages)
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                insert_at = i
                break
        messages.insert(insert_at, {"role": "user", "content": injection.text})
        return injection
    except Exception:  # noqa: BLE001 -- grounding is best-effort; never break a turn
        return None
