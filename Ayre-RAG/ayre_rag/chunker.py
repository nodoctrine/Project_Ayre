"""Paragraph-aligned chunking of clean article text into grounding units.

A chunk is the unit that gets retrieved, ranked, and injected, so it must be
self-contained prose of a predictable size. Strategy (plan §6b):
  - Split on blank lines into paragraphs; pack whole paragraphs into a chunk until
    the next would exceed `chunk_chars`. Never split mid-paragraph unless forced.
  - A single paragraph longer than `chunk_chars` is split at the nearest sentence
    boundary under the cap (falling back to a word boundary, then a hard cut).
  - A trailing remainder shorter than `min_chunk_chars` is merged into the previous
    chunk rather than left as a runt that dilutes ranking.

`per_article_char_cap` (lead-only mode) is applied by the caller (ingest) BEFORE
chunking, so this stays a pure text -> list[str] function.
"""
from __future__ import annotations

import re

_PARA_SPLIT = re.compile(r"\n\s*\n")
# a sentence end: . ! or ? followed by whitespace (kept simple -- BM25 doesn't need
# perfect sentence segmentation, only a reasonable break point).
_SENTENCE_END = re.compile(r"[.!?]\s")


def _split_long_paragraph(para: str, chunk_chars: int) -> list[str]:
    """Split one over-long paragraph into <= chunk_chars pieces at the best boundary."""
    pieces: list[str] = []
    rest = para
    while len(rest) > chunk_chars:
        window = rest[:chunk_chars]
        sentence_breaks = list(_SENTENCE_END.finditer(window))
        if sentence_breaks:
            cut = sentence_breaks[-1].end()
        else:
            space = window.rfind(" ")
            cut = space + 1 if space > 0 else chunk_chars  # hard cut if no space
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return [p for p in pieces if p]


def chunk(text: str, chunk_chars: int, min_chunk_chars: int) -> list[str]:
    """Return `text` packed into paragraph-aligned chunks of ~`chunk_chars`."""
    if not text:
        return []
    paragraphs = [p.strip() for p in _PARA_SPLIT.split(text) if p.strip()]
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        if len(para) > chunk_chars:
            # flush what we've packed, then split the oversized paragraph on its own
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_paragraph(para, chunk_chars))
            continue
        if current and len(current) + 2 + len(para) > chunk_chars:
            chunks.append(current)
            current = para
        else:
            current = para if not current else current + "\n\n" + para

    if current:
        chunks.append(current)

    # merge a runt trailing chunk into its predecessor (predecessor may end up
    # slightly over chunk_chars -- acceptable, and better than a low-signal fragment)
    if len(chunks) >= 2 and len(chunks[-1]) < min_chunk_chars:
        tail = chunks.pop()
        chunks[-1] = chunks[-1] + "\n\n" + tail

    return chunks
