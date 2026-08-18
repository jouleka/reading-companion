"""LIT-5/18/19/20 memory store — the spoiler-safe keystone (ADR 0002 + ADR 0007).

`dal.MemoryDB` / `BookmarkView` are the per-book store + read funnel (safety cores lifted
near-verbatim from the twice-reviewed spike); `store.Store` is the per-book connection owner that
serializes all access under a per-book lock; `migrations` is the forward-only schema runner.
"""
