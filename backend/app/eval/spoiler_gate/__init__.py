"""LIT-8 — the spoiler GATE (ADR 0004 + ADR 0007 D-A9). A first-class app package (NOT under tests/)
because its deterministic safety functions are SHARED by the runtime synthesis / recap-cache paths and
the merge-gate eval, so production enforcement and the eval use IDENTICAL logic."""
from .binding import unsupported_event_bindings
from .cache import cache_key, validity_snapshot
from .grounding import GROUNDING_THRESHOLD, ground_recap
from .rag import FORESHADOW_RE, rag_eval
from .structured import structured_eval
from .synthesis import (
    FLOWING_SYSTEM,
    NOW_SYSTEM,
    PROLEPSIS_RE,
    SYNTH_SYSTEM,
    SpoilerGateError,
    _all_entities_revealed_at,
    _proper_nouns,
    assert_recap_safe,
    delta_facts,
    evolve_prompt,
    flowing_system_for,
    now_prompt,
    now_system_for,
    read_text_upto,
    reveal_correctness_eval,
    score_recap,
    supplied_facts,
    synth_prompt,
)

__all__ = [
    "score_recap",
    "assert_recap_safe",
    "SpoilerGateError",
    "supplied_facts",
    "delta_facts",
    "synth_prompt",
    "evolve_prompt",
    "flowing_system_for",
    "now_prompt",
    "now_system_for",
    "SYNTH_SYSTEM",
    "FLOWING_SYSTEM",
    "NOW_SYSTEM",
    "read_text_upto",
    "structured_eval",
    "rag_eval",
    "reveal_correctness_eval",
    "validity_snapshot",
    "cache_key",
    "ground_recap",
    "GROUNDING_THRESHOLD",
    "unsupported_event_bindings",
    "FORESHADOW_RE",
    "PROLEPSIS_RE",
    "_proper_nouns",
    "_all_entities_revealed_at",
]
