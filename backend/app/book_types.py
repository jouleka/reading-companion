"""Shared, versioned LIT-9 book-profile contract.

The profile is advisory presentation/extraction context, never a spoiler authority.  A wrong profile
may change labels or reduce extraction usefulness; it must never widen a bookmark, skip an atom, or
authorize a read.
"""
from dataclasses import asdict, dataclass
import json


BOOK_TYPES = frozenset({
    "novel",
    "anthology",
    "drama",
    "poetry",
    "nonfiction",
    "reference",
    "unknown",
})

# Persisted detector evidence is intentionally a closed vocabulary.  The manifest endpoint exposes
# it before any bookmark-specific read, so accepting arbitrary prose here would create a future-text
# side channel even though the classifier itself emits only aggregate codes.
BOOK_PROFILE_SIGNALS = frozenset({
    "act_scene_titles",
    "speaker_cues",
    "stage_directions",
    "verse_titles",
    "collection_titles",
    "reference_titles",
    "nonfiction_titles",
    "long_prose_sections",
    "short_sections",
    "narrative_verbs",
    "anchor_driven_structure",
    "segmentation_advisories",
    "weak_signals",
    "conflicting_signals",
    "migrated_existing_store",
})


@dataclass(frozen=True)
class BookProfile:
    book_type: str
    confidence: float
    detector_version: str
    signals: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["signals"] = list(self.signals)
        return value

    def evidence_json(self) -> str:
        """Stable, content-free detector evidence safe to persist and back up."""
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


LEGACY_NOVEL_PROFILE = BookProfile(
    book_type="novel",
    confidence=0.0,
    detector_version="legacy-novel-v1",
    signals=("migrated_existing_store",),
)
