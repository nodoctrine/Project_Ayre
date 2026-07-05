"""Query -> ranked, thresholded chunks. The one retrieval path the CLI and the
chat bridge both call, so they can never rank differently.

Contract for the chat path (plan §4.5): retrieval NEVER raises into a turn. A
missing/unreadable/empty index, or an empty query, returns `[]` -- chat proceeds,
injecting nothing. Only genuine misuse (a caller passing a broken config) surfaces.

Selection pipeline:
  sanitize query -> FTS5 search (top retrieve_k) -> absolute floor (score_threshold)
  -> optional relative trim (score_rel_fraction) -> top inject_n Hits.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import index
from .config import RagConfig

# FTS5 bareword operators: if these reach the MATCH expression unquoted they are
# parsed as syntax. We quote every token anyway, but also drop them as query words
# (a user typing "cats and dogs" means the animals, not a boolean AND).
_FTS_OPERATORS = {"and", "or", "not", "near"}

# Conversational filler words carry ~zero retrieval intent, but OR'd into the MATCH
# and title-weighted x5 they let short pop-culture titles built from filler phrases
# ("Catch Me If You Can", "Tell Me Why") out-score genuine content matches — the
# 2026-07-05 calibration bug: "hey can you tell me about photosynthesis" buried the
# Photosynthesis article below 20 song/movie hits. A small curated set, deliberately
# a code constant like _FTS_OPERATORS (an engineering detail of query hygiene, not a
# user tunable). Words with a strong content reading (e.g. "may" the month, "one" the
# number) are deliberately left OUT — dropping real content beats trimming noise.
_STOPWORDS = {
    # articles / copulas
    "a", "an", "the", "is", "are", "am", "was", "were", "be", "been",
    # prepositions / connectives
    "in", "on", "at", "to", "of", "for", "but", "with", "from", "as", "by", "if", "so",
    # pronouns / determiners
    "you", "your", "me", "my", "we", "us", "they", "he", "she", "his", "her", "him",
    "it", "its", "this", "that", "these", "those",
    # auxiliaries ("don" = the tokenized stem of "don't")
    "can", "could", "will", "would", "should", "might",
    "do", "does", "did", "don", "have", "has", "had",
    # conversational filler / greetings / instruction verbs
    "hey", "hi", "hello", "please", "thanks", "thank", "just",
    "tell", "say", "give", "show", "about",
    # question words
    "what", "who", "which", "where", "when", "why", "how",
}

_TOKEN = re.compile(r"\w+", re.UNICODE)
_MIN_TOKEN_CHARS = 2
_MAX_TOKENS = 32  # cap the OR-expansion so a pasted wall of text stays a cheap query


@dataclass(frozen=True)
class Hit:
    score: float          # raw bm25(): more-negative = better
    title: str
    body: str
    article_id: int
    chunk_ix: int         # 0 = article lead


def sanitize_query(text: str) -> str:
    """Turn a free-text message into a safe FTS5 MATCH expression.

    Lowercase, extract unicode word tokens, drop short tokens + bareword operators +
    conversational stopwords, cap the count, then quote each token and OR them
    together. Quoting means nothing a user types (`*`, `"`, `AND`, `(`) can be parsed
    as FTS5 syntax -- the query is always a plain OR-of-terms. Returns "" when
    nothing usable remains (an all-filler message retrieves nothing, by design)."""
    seen: set[str] = set()
    tokens: list[str] = []
    for raw in _TOKEN.findall(text.lower()):
        if (len(raw) < _MIN_TOKEN_CHARS or raw in _FTS_OPERATORS
                or raw in _STOPWORDS or raw in seen):
            continue
        seen.add(raw)
        tokens.append(raw)
        if len(tokens) >= _MAX_TOKENS:
            break
    if not tokens:
        return ""
    return " OR ".join(f'"{tok}"' for tok in tokens)


def _select(hits: list[tuple], cfg: RagConfig) -> list[tuple]:
    """Apply the absolute floor, optional relative trim, then take top inject_n.

    Scores are negative (better = more negative). Absolute floor keeps `score <=
    threshold`. Relative trim keeps hit i within `score_rel_fraction` of the top
    hit's MAGNITUDE: |score_i| >= |top| * fraction (reads as 'within X% of the best')."""
    if cfg.score_threshold is not None:
        hits = [h for h in hits if h[0] <= cfg.score_threshold]

    if cfg.score_rel_fraction > 0 and hits:
        top_mag = abs(hits[0][0])  # hits are pre-sorted best-first by search()
        floor = top_mag * cfg.score_rel_fraction
        hits = [h for h in hits if abs(h[0]) >= floor]

    return hits[: cfg.inject_n]


def retrieve(query_text: str, cfg: RagConfig, *, root: Path | None = None) -> list[Hit]:
    """Retrieve grounding chunks for `query_text`. Never raises for the common
    'no index / empty query' cases -- returns [] so the chat path is untouched."""
    match_query = sanitize_query(query_text)
    if not match_query:
        return []

    db_path = cfg.resolved_db_path(root)
    if not db_path.exists():
        return []

    try:
        conn = index.open_read(db_path)
    except sqlite3.Error:
        return []
    try:
        rows = index.search(conn, match_query, cfg.retrieve_k, cfg.title_weight)
    except sqlite3.Error:
        # a corrupt or schema-less db reads as 'no results', never an error
        return []
    finally:
        conn.close()

    selected = _select(rows, cfg)
    return [Hit(score=s, title=t, body=b, article_id=aid, chunk_ix=cix)
            for (s, t, b, aid, cix) in selected]


def index_status(cfg: RagConfig, *, root: Path | None = None) -> dict:
    """A non-blocking probe of the index db for the RAG-library status notice and
    the `stats` command. Also the 'is setup done?' probe the v1.0 build flow reuses
    (§8.5). Never raises: any problem is reported as ready=False + a reason."""
    db_path = cfg.resolved_db_path(root)
    status: dict = {
        "enabled": cfg.enabled,
        "corpus_label": cfg.corpus_label,
        "db_path": str(db_path),
        "exists": db_path.exists(),
        "ready": False,
        "article_count": None,
        "chunk_count": None,
        "built_at": None,
        "source_dump": None,
        "error": None,
    }
    if not status["exists"]:
        status["error"] = "no index found"
        return status
    try:
        conn = index.open_read(db_path)
    except sqlite3.Error as exc:
        status["error"] = f"cannot open index: {exc}"
        return status
    try:
        meta = index.read_meta(conn)
        chunk_count = index.count_chunks(conn)
    except sqlite3.Error as exc:
        status["error"] = f"index unreadable: {exc}"
        return status
    finally:
        conn.close()

    status["chunk_count"] = chunk_count
    if meta:
        status["article_count"] = meta.get("article_count")
        status["built_at"] = meta.get("built_at")
        status["source_dump"] = meta.get("source_dump")
        # meta's stored chunk_count wins if present (authoritative build record)
        if meta.get("chunk_count") is not None:
            status["chunk_count"] = meta.get("chunk_count")
    status["ready"] = (status["chunk_count"] or 0) > 0
    if not status["ready"]:
        status["error"] = "index is empty"
    return status
