"""Ayre-RAG: local BM25 retrieval over a Wikipedia text corpus (component 4, v0).

Pure Python stdlib -- `sqlite3`/FTS5 with `bm25()`, no Rust/Burn, no Kiwix ZIM
(stack decision 2026-07-04; see `RAG_v0_Plan.md` + the `rag-python-stack` memory).
The whole line ships with zero new binaries because FTS5 lives inside the built-in
`sqlite3`.

Ladder: v0 Simple English Wikipedia (this build) -> v0.5 full English Wikipedia
(config flip) -> v1.0 user-pointed corpus. The pipeline is corpus-agnostic; a new
corpus is a different dump + index db path, not new code.

Additive + default-off: nothing here is imported by the running app until the
CP2 chat wiring, and even then it is gated behind `config/rag.json`'s
`enabled: false`. Worst case, Ayre ships exactly as stable as today with a dead flag.
"""
