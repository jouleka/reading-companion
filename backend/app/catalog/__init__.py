"""LIT-18 global catalog — the shelf + mutable reading_state + cost_ledger (ADR 0002 D14, ADR 0007).

`catalog.Catalog` owns the single global `catalog.db` (one connection, global lock, busy_timeout). The
per-book `memory.db` files are immutable; the MUTABLE reading position (bookmark/cfi/ingest_progress)
lives here and is passed INTO `MemoryDB.view(bookmark)`. bookmark/ingest_progress are monotonic
high-water marks (the spoiler frontier must not regress).
"""
