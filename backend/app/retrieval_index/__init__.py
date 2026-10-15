"""Explicit, budgeted local full-source semantic indexing; never invoked by normal Ask reads."""

from .backfill import IndexRecoveryRequired, backfill_source_index

__all__ = ["IndexRecoveryRequired", "backfill_source_index"]
