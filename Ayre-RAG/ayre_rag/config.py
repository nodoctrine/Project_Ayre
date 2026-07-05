"""Load + validate `config/rag.json` into a typed `RagConfig`.

Variable-first: every RAG tunable lives in `config/rag.json` with a rationale,
never as a literal in code (mirrors how `ayre_setup.config` reads
`config/optimizer.json`). This module is the single place the config is parsed,
so the CLI, the retrieve path, and the server bridge can never disagree on a value.

Locating the config: RAG lives under the Ayre-USB root as a sibling of Setup/UI
(`<root>/Ayre-RAG/ayre_rag/config.py` -> `parents[2]` == `<root>`). An explicit
`AYRE_USB_ROOT` override wins, matching `ayre_setup.platform_layer.ayre_usb_root()`
so the whole app agrees on where the root is.

Two contracts, on purpose:
  - `load_config()` is STRICT -- malformed JSON or an out-of-range value raises
    `ConfigError` with a clear message. The CLI wants that (fail loud on a bad edit).
  - the server bridge calls it inside a try/except and treats ANY failure as
    "RAG off" (a broken rag.json must never break a chat turn) -- same forgiving
    posture the optimizer/coaching loaders use.
A MISSING file is not an error: every key has a safe default and `enabled`
defaults to False, so an absent config is simply a dead flag.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path


class ConfigError(ValueError):
    """Raised for malformed JSON or an out-of-range value in rag.json."""


def app_root() -> Path:
    """The Ayre-USB top-level folder (holds Ayre-RAG / Ayre-Setup / Ayre-UI / config).

    Honors the same `AYRE_USB_ROOT` override as `ayre_setup.platform_layer`; else
    derives from this file: `<root>/Ayre-RAG/ayre_rag/config.py` -> parents[2].
    """
    override = os.environ.get("AYRE_USB_ROOT")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[2]


def config_path() -> Path:
    return app_root() / "config" / "rag.json"


@dataclass(frozen=True)
class RagConfig:
    """Typed, validated view of config/rag.json. Frozen: config is read-only at
    runtime; the CLI writes JSON, not this object."""

    enabled: bool = False
    corpus_label: str = "Simple English Wikipedia"
    # Resolved relative to the app root -> Project_Ayre/Ayre_Corpus/ (INSIDE the tree,
    # gitignored like bin/ python/ models/; a large generated artifact that travels with
    # the repo). Must match config/rag.json's default. See resolved_db_path().
    index_db_path: str = "Ayre_Corpus/simplewiki.db"
    chunk_chars: int = 1200
    min_chunk_chars: int = 200
    title_weight: float = 5.0
    retrieve_k: int = 20
    inject_n: int = 5
    # ABSOLUTE floor on bm25(); None until CP1 calibrates it (None = "inject the top
    # inject_n regardless", so retrieval is observable before the bar is tuned).
    score_threshold: float | None = None
    # Relative trim: 0 = off. Else keep a hit only if within this fraction of the
    # top hit's score magnitude (activated in CP2/v0.5 if injected context is noisy).
    score_rel_fraction: float = 0.0
    # Hard ceiling: injected RAG context never exceeds this fraction of the model's
    # context window (coexists with the context meter).
    context_fraction: float = 0.25
    # 0 = full article text; >0 = truncate each article to N chars before chunking
    # ("lead-only" mode -- the enwiki middle-ground hedge).
    per_article_char_cap: int = 0
    # Whether the "English Wikipedia -- '<title>'" further-reading pointer is
    # available (surfaces only in the retrieved-context preview, not the sources list).
    further_reading: bool = True
    # Retrieved-context preview panel: default OFF, user-enablable in Settings.
    # (The sources list is ALWAYS shown; this governs the raw-chunk preview only.)
    show_retrieved_context: bool = False

    def resolved_db_path(self, root: Path | None = None) -> Path:
        """`index_db_path` resolved to an absolute path against the app root.

        Absolute paths in config are honored as-is; relative paths resolve against
        the app root so the same committed config works from any drive letter."""
        p = Path(self.index_db_path)
        if p.is_absolute():
            return p.resolve()
        return ((root or app_root()) / p).resolve()


# Per-key validation. Each entry: (python type(s), predicate, human message). The
# predicate documents the domain of the value (document-tier-reasoning: the WHY of
# each bound is captured, not just the check).
def _is_num(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


_VALIDATORS: dict[str, tuple] = {
    "enabled": (bool, lambda v: True, "must be true or false"),
    "corpus_label": (str, lambda v: len(v) > 0, "must be a non-empty string"),
    "index_db_path": (str, lambda v: len(v) > 0, "must be a non-empty path string"),
    "chunk_chars": (int, lambda v: v >= 100, "must be an integer >= 100 (a coherent grounding unit)"),
    "min_chunk_chars": (int, lambda v: v >= 0, "must be an integer >= 0"),
    "title_weight": (float, lambda v: v >= 0, "must be a number >= 0 (bm25 column weight)"),
    "retrieve_k": (int, lambda v: v >= 1, "must be an integer >= 1 (candidate pool size)"),
    "inject_n": (int, lambda v: v >= 0, "must be an integer >= 0 (max chunks injected)"),
    "score_threshold": (float, lambda v: True, "must be a number or null (absolute bm25 floor)"),
    "score_rel_fraction": (float, lambda v: 0.0 <= v <= 1.0, "must be a number in [0, 1] (0 = off)"),
    "context_fraction": (float, lambda v: 0.0 < v <= 1.0, "must be a number in (0, 1] (context ceiling)"),
    "per_article_char_cap": (int, lambda v: v >= 0, "must be an integer >= 0 (0 = full text)"),
    "further_reading": (bool, lambda v: True, "must be true or false"),
    "show_retrieved_context": (bool, lambda v: True, "must be true or false"),
}


def _coerce(key: str, raw: object) -> object:
    """Type-check + light coercion for one key. int fields accept an int (not a
    bool); float fields accept int or float; `score_threshold` also accepts null."""
    expected, predicate, msg = _VALIDATORS[key]

    if key == "score_threshold":
        if raw is None:
            return None
        if not _is_num(raw):
            raise ConfigError(f"rag.json: '{key}' {msg}")
        return float(raw)

    if expected is bool:
        if not isinstance(raw, bool):
            raise ConfigError(f"rag.json: '{key}' {msg}")
        value = raw
    elif expected is int:
        # Reject bools (a subclass of int) and floats -- these are count/size knobs.
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ConfigError(f"rag.json: '{key}' {msg}")
        value = raw
    elif expected is float:
        if not _is_num(raw):
            raise ConfigError(f"rag.json: '{key}' {msg}")
        value = float(raw)
    elif expected is str:
        if not isinstance(raw, str):
            raise ConfigError(f"rag.json: '{key}' {msg}")
        value = raw
    else:  # pragma: no cover -- table is exhaustive
        value = raw

    if not predicate(value):
        raise ConfigError(f"rag.json: '{key}' {msg}")
    return value


def config_from_dict(data: dict) -> RagConfig:
    """Build a validated RagConfig from a parsed dict. Unknown keys are ignored
    (forward-compatible); `_comment` / `*_rationale` sibling keys are skipped."""
    if not isinstance(data, dict):
        raise ConfigError("rag.json: top level must be a JSON object")
    known = {f.name for f in fields(RagConfig)}
    values: dict[str, object] = {}
    for key, raw in data.items():
        if key not in known:
            continue  # _comment, *_rationale, and any forward-compat keys
        values[key] = _coerce(key, raw)
    return RagConfig(**values)  # type: ignore[arg-type]


def load_config(path: Path | None = None) -> RagConfig:
    """Strict load. Missing file -> all-default RagConfig (enabled=False, safe).
    Malformed JSON / bad value -> ConfigError."""
    p = path or config_path()
    if not p.exists():
        return RagConfig()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"rag.json: cannot read {p}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"rag.json: invalid JSON in {p}: {exc}") from exc
    return config_from_dict(data)


def load_config_safe(path: Path | None = None) -> RagConfig:
    """Forgiving load for the running app: ANY failure -> an all-default,
    RAG-OFF config so a broken rag.json can never break a chat turn."""
    try:
        return load_config(path)
    except ConfigError:
        return RagConfig()
