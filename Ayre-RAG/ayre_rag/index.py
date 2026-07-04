"""The FTS5 index: schema, bulk insert, and bm25() search.

Pure stdlib `sqlite3`. The db is a rebuildable derived artifact (built from the
Wikipedia dump), so the write path uses unsafe-but-fast PRAGMAs -- a crash mid-build
just means re-running ingest, never data loss of anything irreplaceable.

Schema (plan §3): one FTS5 virtual table `chunks` (title + body indexed, article_id
+ chunk_ix carried UNINDEXED so a hit can be grouped back to its source article) and
a single-row `meta` table recording provenance for stats + citation.

bm25() sign convention: SQLite returns MORE-NEGATIVE = better match. Queries
`ORDER BY score ASC LIMIT k`; callers apply an absolute floor as `score <= threshold`.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

# Column order matters: bm25() weights are positional (title, body, then the two
# UNINDEXED columns which never match and default to weight 1.0).
_CREATE_CHUNKS = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    title,
    body,
    article_id UNINDEXED,
    chunk_ix   UNINDEXED,
    tokenize = 'porter unicode61'
);
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (
    corpus_label  TEXT,
    source_dump   TEXT,
    built_at      TEXT,
    article_count INTEGER,
    chunk_count   INTEGER,
    config_json   TEXT
);
"""

# Applied on the WRITE connection only -- unsafe on purpose (rebuildable artifact).
_BULK_PRAGMAS = (
    "PRAGMA journal_mode = OFF;",
    "PRAGMA synchronous = OFF;",
    "PRAGMA temp_store = MEMORY;",
    "PRAGMA cache_size = -262144;",  # ~256 MiB page cache (negative = KiB)
)


def open_write(db_path: Path) -> sqlite3.Connection:
    """Open (creating the file) for a bulk build, with the fast/unsafe PRAGMAs set."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    for pragma in _BULK_PRAGMAS:
        conn.execute(pragma)
    return conn


def open_read(db_path: Path) -> sqlite3.Connection:
    """Open read-only via a file: URI so a missing/locked db raises cleanly instead
    of silently creating an empty file. Caller handles the exception as 'no index'."""
    uri = f"file:{db_path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_CHUNKS)
    conn.execute(_CREATE_META)
    conn.commit()


def insert_chunks(conn: sqlite3.Connection, rows: list[tuple[str, str, int, int]]) -> None:
    """Batch-insert (title, body, article_id, chunk_ix) rows. Caller controls batch
    size + transaction boundaries (ingest commits every N rows)."""
    conn.executemany(
        "INSERT INTO chunks (title, body, article_id, chunk_ix) VALUES (?, ?, ?, ?)",
        rows,
    )


def write_meta(
    conn: sqlite3.Connection,
    *,
    corpus_label: str,
    source_dump: str,
    built_at: str,
    article_count: int,
    chunk_count: int,
    config_json: str,
) -> None:
    """Replace the single provenance row (idempotent across re-optimize)."""
    conn.execute("DELETE FROM meta")
    conn.execute(
        "INSERT INTO meta (corpus_label, source_dump, built_at, article_count, "
        "chunk_count, config_json) VALUES (?, ?, ?, ?, ?, ?)",
        (corpus_label, source_dump, built_at, article_count, chunk_count, config_json),
    )
    conn.commit()


def optimize(conn: sqlite3.Connection) -> None:
    """Merge the FTS5 b-tree into a compact, fast-to-query form. Run once at the
    end of ingest (expensive; not per-batch)."""
    conn.execute("INSERT INTO chunks(chunks) VALUES('optimize')")
    conn.commit()


def read_meta(conn: sqlite3.Connection) -> dict | None:
    """The provenance row as a dict, or None if the table is absent/empty."""
    try:
        cur = conn.execute(
            "SELECT corpus_label, source_dump, built_at, article_count, "
            "chunk_count, config_json FROM meta LIMIT 1"
        )
    except sqlite3.OperationalError:
        return None
    row = cur.fetchone()
    if not row:
        return None
    keys = ("corpus_label", "source_dump", "built_at",
            "article_count", "chunk_count", "config_json")
    return dict(zip(keys, row))


def count_chunks(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT count(*) FROM chunks")
    return int(cur.fetchone()[0])


def search(
    conn: sqlite3.Connection,
    match_query: str,
    k: int,
    title_weight: float,
) -> list[tuple[float, str, str, int, int]]:
    """Top-`k` chunks for an FTS5 `match_query`, best (most-negative bm25) first.

    Returns (score, title, body, article_id, chunk_ix) tuples. `match_query` is the
    already-sanitized FTS5 MATCH expression built by retrieve.sanitize_query -- this
    function does NOT sanitize (that's retrieve's job; keeping search dumb makes the
    CLI and the chat path share one code path)."""
    cur = conn.execute(
        "SELECT bm25(chunks, ?, 1.0) AS score, title, body, article_id, chunk_ix "
        "FROM chunks WHERE chunks MATCH ? ORDER BY score ASC LIMIT ?",
        (title_weight, match_query, k),
    )
    return [(float(s), t, b, int(aid), int(cix)) for (s, t, b, aid, cix) in cur.fetchall()]
