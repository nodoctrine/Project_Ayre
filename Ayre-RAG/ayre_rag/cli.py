"""CLI for Ayre-RAG (component 4). Mirrors `ayre_setup.cli`'s argparse style.

    python -m ayre_rag ingest --dump PATH [--db PATH] [--corpus-label L]
                              [--rebuild] [--limit N] [--per-article-char-cap N]
                              [--progress text|json|none]
    python -m ayre_rag query "TEXT" [--db PATH] [-k N] [--show-query]
    python -m ayre_rag stats [--db PATH]

`query` is the CP1 TUNING TOOL: it prints the raw bm25 score for each top hit so the
user can pick `score_threshold` in config/rag.json by eye (on-corpus queries score
clearly more-negative than off-corpus ones). Scores are always shown; the sign
convention is more-negative = better.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import index
from .config import ConfigError, RagConfig, load_config
from .ingest import IngestError, ingest_dump
from .retrieve import index_status, sanitize_query

_WS = re.compile(r"\s+")


def _resolve_db(args_db: str | None, cfg: RagConfig) -> Path:
    if args_db:
        return Path(args_db).expanduser()
    return cfg.resolved_db_path()


def _snippet(body: str, width: int = 160) -> str:
    text = _WS.sub(" ", body).strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def _load_cfg() -> RagConfig:
    """Strict load so a bad config fails loudly at the CLI (unlike the server path)."""
    try:
        return load_config()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        raise SystemExit(2)


def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = _load_cfg()
    db_path = _resolve_db(args.db, cfg)
    dump_path = Path(args.dump).expanduser()
    try:
        stats = ingest_dump(
            dump_path,
            db_path,
            cfg,
            corpus_label=args.corpus_label,
            rebuild=args.rebuild,
            limit=args.limit,
            per_article_char_cap=args.per_article_char_cap,
            progress_mode=args.progress,
            progress_every=args.progress_every,
        )
    except IngestError as exc:
        print(f"Ingest failed: {exc}", file=sys.stderr)
        return 1
    if args.progress != "json":
        print(f"\nIndex built: {db_path}")
        print(f"  {stats.articles:,} articles · {stats.chunks:,} chunks · "
              f"{stats.elapsed_s:,.1f}s")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    cfg = _load_cfg()
    db_path = _resolve_db(args.db, cfg)
    if not db_path.exists():
        print(f"No index at {db_path}. Build one with `ingest --dump <dump.xml.bz2>`.",
              file=sys.stderr)
        return 1

    match_query = sanitize_query(args.text)
    if not match_query:
        print("Query has no usable search terms (too short / all stopwords).",
              file=sys.stderr)
        return 1
    if args.show_query:
        print(f"FTS5 query: {match_query}\n")

    k = args.k if args.k is not None else cfg.retrieve_k
    conn = index.open_read(db_path)
    try:
        rows = index.search(conn, match_query, k, cfg.title_weight)
    finally:
        conn.close()

    if not rows:
        print("No matches.")
        return 0

    thr = cfg.score_threshold
    print(f"Top {len(rows)} of retrieve_k={cfg.retrieve_k}  "
          f"(threshold={thr if thr is not None else 'null (off)'}, inject_n={cfg.inject_n})")
    print(f"{'rank':>4}  {'bm25':>9}  {'pass':>4}  title")
    for i, (score, title, body, _aid, chunk_ix) in enumerate(rows, start=1):
        passes = "—" if thr is None else ("yes" if score <= thr else "no")
        injected = " *" if i <= cfg.inject_n and (thr is None or score <= thr) else ""
        print(f"{i:>4}  {score:>9.3f}  {passes:>4}  {title} [chunk {chunk_ix}]{injected}")
        print(f"          {_snippet(body)}")
    print("\n  * = would be injected at current config    (bm25: more-negative = better)")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = _load_cfg()
    db_path = _resolve_db(args.db, cfg)
    status = index_status(cfg) if args.db is None else _status_for_path(cfg, db_path)

    print("RAG index status")
    print(f"  db path       : {status['db_path']}")
    print(f"  exists        : {status['exists']}")
    print(f"  ready         : {status['ready']}")
    print(f"  corpus label  : {status['corpus_label']}")
    if status["exists"]:
        size = Path(status["db_path"]).stat().st_size
        print(f"  size on disk  : {size / (1024 * 1024):,.1f} MiB")
    if status["article_count"] is not None:
        print(f"  articles      : {status['article_count']:,}")
    if status["chunk_count"] is not None:
        print(f"  chunks        : {status['chunk_count']:,}")
    if status["built_at"]:
        print(f"  built at      : {status['built_at']}")
    if status["source_dump"]:
        print(f"  source dump   : {status['source_dump']}")
    if status["error"]:
        print(f"  note          : {status['error']}")
    return 0 if status["ready"] or not status["exists"] else 1


def _status_for_path(cfg: RagConfig, db_path: Path) -> dict:
    """index_status against an explicit --db path (overrides config's path)."""
    from dataclasses import replace
    # resolved_db_path honors absolute paths as-is
    return index_status(replace(cfg, index_db_path=str(db_path)))


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy codepage; article titles are unicode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(prog="ayre-rag")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="build the FTS5 index from a Wikipedia dump")
    p_ingest.add_argument("--dump", required=True, help="path to *-pages-articles*.xml.bz2")
    p_ingest.add_argument("--db", default=None, help="index db path; default = config index_db_path")
    p_ingest.add_argument("--corpus-label", default=None, help="human-facing corpus name; default = config corpus_label")
    p_ingest.add_argument("--rebuild", action="store_true", help="replace an existing index db")
    p_ingest.add_argument("--limit", default=None, type=int, help="stop after N articles (smoke test)")
    p_ingest.add_argument("--per-article-char-cap", dest="per_article_char_cap", default=None, type=int, help="truncate each article to N chars before chunking (0 = full text; default = config)")
    p_ingest.add_argument("--progress", default="text", choices=("text", "json", "none"), help="progress output format")
    p_ingest.add_argument("--progress-every", dest="progress_every", default=1000, type=int, help="emit progress every N articles")
    p_ingest.set_defaults(func=cmd_ingest)

    p_query = sub.add_parser("query", help="run a BM25 query and print ranked chunks + scores (CP1 tuning tool)")
    p_query.add_argument("text", help="the query text")
    p_query.add_argument("--db", default=None, help="index db path; default = config index_db_path")
    p_query.add_argument("-k", default=None, type=int, help="how many hits to show; default = config retrieve_k")
    p_query.add_argument("--show-query", action="store_true", help="also print the sanitized FTS5 MATCH expression")
    p_query.set_defaults(func=cmd_query)

    p_stats = sub.add_parser("stats", help="show the index provenance + size on disk")
    p_stats.add_argument("--db", default=None, help="index db path; default = config index_db_path")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
