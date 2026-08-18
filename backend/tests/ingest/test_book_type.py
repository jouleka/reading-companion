"""LIT-9 deterministic book-type detection and safe unknown fallback."""
from types import SimpleNamespace

from app.ingest.book_type import DETECTOR_VERSION, detect_book_type


def _chapters(titles, body, *, words=120):
    text = (body + " ") * max(1, words // max(len(body.split()), 1))
    return [
        {"ordinal": index, "title": title, "part_label": "", "text": text}
        for index, title in enumerate(titles, start=1)
    ]


def _result(mode="file-driven", flags=()):
    return SimpleNamespace(mode=mode, flags=flags)


def test_detects_conventional_narrative_without_changing_its_inputs():
    chapters = _chapters(
        [f"Chapter {n}" for n in range(1, 9)],
        "Aldric arrived and Berenice answered. He remembered the road and said that they should wait.",
        words=900,
    )
    before = [dict(chapter) for chapter in chapters]
    profile = detect_book_type(_result(), chapters)
    assert profile.book_type == "novel"
    assert profile.detector_version == DETECTOR_VERSION
    assert chapters == before, "classification is advisory and must never rewrite or drop atoms"


def test_detects_drama_poetry_anthology_nonfiction_and_reference_from_strong_signals():
    drama = detect_book_type(
        _result(),
        _chapters(
            ["Act I, Scene I", "Act I, Scene II", "Act II, Scene I"],
            "HAMLET: Speak. OPHELIA: I listen. Enter the guard. Exit the king.",
            words=500,
        ),
    )
    poetry = detect_book_type(
        _result(),
        _chapters(
            ["Sonnet I", "Sonnet II", "Ode III", "Canto IV"],
            "Moon river silence wind light",
            words=90,
        ),
    )
    anthology = detect_book_type(
        _result(),
        _chapters(
            ["The First Story", "A Winter Tale", "The Second Story", "Another Tale"],
            "A traveler arrived. The account ended here.",
            words=600,
        ),
    )
    nonfiction = detect_book_type(
        _result(),
        _chapters(
            ["Lecture I", "Lecture II", "A History", "Meditation IV"],
            "The account describes evidence and context.",
            words=420,
        ),
    )
    reference = detect_book_type(
        _result(),
        _chapters(
            ["Lesson 1", "Exercise 1", "Lesson 2", "Glossary"],
            "Definition example procedure note exercise",
            words=250,
        ),
    )
    assert [
        drama.book_type,
        poetry.book_type,
        anthology.book_type,
        nonfiction.book_type,
        reference.book_type,
    ] == [
        "drama",
        "poetry",
        "anthology",
        "nonfiction",
        "reference",
    ]


def test_ambiguous_or_conflicting_signals_return_supported_unknown():
    profile = detect_book_type(
        _result(mode="anchor-driven", flags=("unusual navigation",)),
        _chapters(["One", "Two"], "Fragment image table voice", words=40),
    )
    assert profile.book_type == "unknown"
    assert 0.0 <= profile.confidence <= 1.0


def test_stored_evidence_is_bounded_metadata_not_future_content():
    secret_title = "The Culprit Revealed"
    secret_name = "Futurevillain"
    profile = detect_book_type(
        _result(),
        _chapters(["Act I", secret_title], f"ALICE: Wait. {secret_name}: Enter.", words=400),
    )
    encoded = profile.evidence_json()
    assert secret_title not in encoded
    assert secret_name not in encoded
    assert len(profile.signals) <= 12
