"""Stream a Wikipedia `*-pages-articles*.xml.bz2` dump into the FTS5 index.

Never materializes the raw XML: `bz2` decompresses on the fly and
`xml.etree.iterparse` yields one page at a time, with the parsed element (and the
root's accumulated children) cleared after each page so memory stays flat over the
full ~7M-article enwiki -- the same code path handles Simple English in minutes.

Per page: skip non-article namespaces (ns != 0) and redirects; strip wikitext ->
optional lead-only truncation -> chunk -> batch-insert. A provenance `meta` row and
an FTS `optimize` finish the build.

This module is the SINGLE SOURCE OF TRUTH for ingest (v1.0 prep §8.5): all behavior
is driven by args + `config/rag.json`, progress is emitted structured (text or
line-delimited JSON) so a future "Build index" button can render a progress bar
without scraping, and failure modes raise `IngestError` with an actionable message
instead of a raw traceback.
"""
from __future__ import annotations

import bz2
import dataclasses
import json
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TextIO

from . import index
from .chunker import chunk
from .config import RagConfig
from .wikitext import strip_wikitext

# Rows per insert transaction. Internal performance knob (not a behavior tunable):
# big enough to amortize commit overhead, small enough that a crash loses little.
_BATCH_ROWS = 10_000


class IngestError(Exception):
    """An actionable, user-facing ingest failure (friendly message, no traceback)."""


@dataclass
class IngestStats:
    articles: int = 0        # ns0, non-redirect articles that produced >= 1 chunk
    chunks: int = 0
    pages_scanned: int = 0   # every <page> seen, including skipped
    elapsed_s: float = 0.0

    @property
    def rate(self) -> float:
        return self.articles / self.elapsed_s if self.elapsed_s > 0 else 0.0

    def as_dict(self) -> dict:
        return {"articles": self.articles, "chunks": self.chunks,
                "pages_scanned": self.pages_scanned,
                "elapsed_s": round(self.elapsed_s, 2), "rate": round(self.rate, 1)}


def _local(tag: str) -> str:
    """Local name of a possibly namespaced tag ('{…}page' -> 'page')."""
    return tag.rsplit("}", 1)[-1]


def _page_fields(elem: ET.Element) -> tuple[str | None, str | None, bool, str | None]:
    """Extract (ns, title, is_redirect, text) from a <page> element."""
    ns = title = text = None
    is_redirect = False
    for child in elem:
        lt = _local(child.tag)
        if lt == "ns":
            ns = child.text
        elif lt == "title":
            title = child.text
        elif lt == "redirect":
            is_redirect = True
        elif lt == "revision":
            for rc in child:
                if _local(rc.tag) == "text":
                    text = rc.text
                    break
    return ns, title, is_redirect, text


def _emit(mode: str, out: TextIO, phase: str, stats: IngestStats) -> None:
    if mode == "none":
        return
    if mode == "json":
        out.write(json.dumps({"phase": phase, **stats.as_dict()}) + "\n")
    else:  # text
        out.write(
            f"[{phase}] {stats.articles:,} articles · {stats.chunks:,} chunks · "
            f"{stats.pages_scanned:,} pages scanned · {stats.elapsed_s:,.0f}s · "
            f"{stats.rate:,.0f} art/s\n"
        )
    out.flush()


def ingest_dump(
    dump_path: Path,
    db_path: Path,
    cfg: RagConfig,
    *,
    corpus_label: str | None = None,
    rebuild: bool = False,
    limit: int | None = None,
    per_article_char_cap: int | None = None,
    progress_mode: str = "text",       # "text" | "json" | "none"
    progress_every: int = 1000,
    out: TextIO | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> IngestStats:
    """Build the FTS5 index at `db_path` from `dump_path`. Returns IngestStats.

    Raises IngestError for the actionable cases (missing dump, existing db without
    --rebuild, corrupt bz2/XML). `should_stop` (optional) lets a caller cancel a
    long run cooperatively between pages -- the v1.0 UI hook."""
    out = out or sys.stdout
    corpus_label = corpus_label or cfg.corpus_label
    cap = cfg.per_article_char_cap if per_article_char_cap is None else per_article_char_cap

    if not dump_path.exists():
        raise IngestError(f"Dump not found: {dump_path}\n"
                          f"Download a *-pages-articles*.xml.bz2 from dumps.wikimedia.org.")
    if not dump_path.is_file():
        raise IngestError(f"Dump path is not a file: {dump_path}")

    if db_path.exists():
        if not rebuild:
            raise IngestError(
                f"Index already exists: {db_path}\n"
                f"Re-run with --rebuild to replace it (the index is a rebuildable artifact)."
            )
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            try:
                p.unlink(missing_ok=True)
            except OSError as exc:
                raise IngestError(f"Cannot remove old index {p}: {exc}") from exc

    conn = index.open_write(db_path)
    index.create_schema(conn)

    stats = IngestStats()
    started = time.monotonic()
    batch: list[tuple[str, str, int, int]] = []
    article_id = 0

    def _flush() -> None:
        if batch:
            index.insert_chunks(conn, batch)
            conn.commit()
            batch.clear()

    try:
        with bz2.open(str(dump_path), "rb") as fh:
            context = ET.iterparse(fh, events=("start", "end"))
            _, root = next(context)  # the <mediawiki> root; kept only to .clear() it
            for event, elem in context:
                if event != "end" or _local(elem.tag) != "page":
                    continue

                stats.pages_scanned += 1
                ns, title, is_redirect, text = _page_fields(elem)
                elem.clear()
                root.clear()  # drop the just-parsed page so the tree never grows

                if is_redirect or ns != "0" or not title or not text:
                    continue

                clean = strip_wikitext(text)
                if cap > 0:
                    clean = clean[:cap]
                pieces = chunk(clean, cfg.chunk_chars, cfg.min_chunk_chars)
                if not pieces:
                    continue

                for ix, body in enumerate(pieces):
                    batch.append((title, body, article_id, ix))
                article_id += 1
                stats.articles += 1
                stats.chunks += len(pieces)

                if len(batch) >= _BATCH_ROWS:
                    _flush()
                if stats.articles % progress_every == 0:
                    stats.elapsed_s = time.monotonic() - started
                    _emit(progress_mode, out, "ingest", stats)
                if limit is not None and stats.articles >= limit:
                    break
                if should_stop is not None and should_stop():
                    break

            _flush()
    except (OSError, EOFError) as exc:
        raise IngestError(f"Failed reading the compressed dump {dump_path}: {exc}\n"
                          f"The file may be incomplete or corrupt -- re-download it.") from exc
    except ET.ParseError as exc:
        raise IngestError(f"XML parse error in {dump_path}: {exc}\n"
                          f"The dump may be truncated (still downloading?).") from exc

    stats.elapsed_s = time.monotonic() - started

    config_json = json.dumps(dataclasses.asdict(cfg))
    index.write_meta(
        conn,
        corpus_label=corpus_label,
        source_dump=dump_path.name,
        built_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        article_count=stats.articles,
        chunk_count=stats.chunks,
        config_json=config_json,
    )
    index.optimize(conn)
    conn.close()

    _emit(progress_mode, out, "done", stats)
    return stats
