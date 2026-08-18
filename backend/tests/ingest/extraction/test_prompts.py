"""Module C / prompts.py — the extraction prompts lifted near-verbatim from the spike (ADR 0007 D-A5;
the anti-foreshadow + Russian-name guidance is load-bearing per ADR 0003/0004, so the test pins the
specific phrasings rather than just non-emptiness)."""
from app.ingest.extraction.prompts import (
    EXTRACT_SYSTEM, extract_system_for, extract_user_prompt, roster_for_prompt,
)


def test_system_prompt_is_anti_foreshadow_and_schema_bound():
    s = EXTRACT_SYSTEM.lower()
    assert "spoiler-safe" in s
    assert "only what this chapter" in s
    assert "never use outside knowledge" in s
    assert "never refer to later events" in s


def test_roster_for_prompt_empty():
    assert roster_for_prompt([]) == "(none yet — this is the first chapter)"
    assert roster_for_prompt(None) == "(none yet — this is the first chapter)"


def test_roster_for_prompt_renders_canonical_type_and_aliases():
    roster = [
        {"canonical_name": "Alexey Fyodorovitch Karamazov", "type": "character",
         "aliases": ["Alyosha", "Alexey"]},
        {"canonical_name": "the monastery", "type": "place", "aliases": []},
    ]
    out = roster_for_prompt(roster)
    assert "- Alexey Fyodorovitch Karamazov [character] (aka Alyosha, Alexey)" in out
    assert "- the monastery [place]" in out
    assert "(aka" not in out.split("\n")[1]                  # no empty alias clause for the placeless entry


def test_user_prompt_threads_title_roster_text_and_link_instruction():
    roster = [{"canonical_name": "Fyodor Pavlovitch Karamazov", "type": "character", "aliases": []}]
    p = extract_user_prompt("Chapter II", roster, "Once upon a time in Skotoprigonyevsk.")
    assert "CHAPTER: Chapter II" in p
    assert "Fyodor Pavlovitch Karamazov" in p                # the roster is injected
    assert "Once upon a time in Skotoprigonyevsk." in p      # the chapter text is injected
    # the roster-link instruction (anti-duplication) and the Russian-name guidance are load-bearing
    assert "REUSE its exact canonical_name" in p
    assert "matched_roster=true" in p
    assert "Alyosha" in p and "Alexey Fyodorovitch Karamazov" in p
    assert "ONE entity" in p


def test_novel_profile_preserves_the_existing_prompt_byte_for_byte():
    roster = [{"canonical_name": "Aldric", "type": "character", "aliases": []}]
    assert extract_system_for("novel") == EXTRACT_SYSTEM
    assert extract_user_prompt("Chapter I", roster, "Aldric arrived.", book_type="novel") == (
        extract_user_prompt("Chapter I", roster, "Aldric arrived.")
    )


def test_non_novel_prompt_does_not_invent_plot_or_characters():
    system = extract_system_for("reference").lower()
    prompt = extract_user_prompt(
        "Lesson 4",
        [],
        "Torque equals force times distance.",
        book_type="reference",
    ).lower()
    assert "section" in system and "novel" not in system
    assert "leave irrelevant arrays empty" in prompt
    assert "do not invent" in prompt
    assert "people" in prompt and "topics" in prompt


def test_non_english_source_keeps_names_in_source_spelling_but_derived_text_in_english():
    system = extract_system_for("novel", content_language="ru").lower()
    prompt = extract_user_prompt(
        "Глава I", [], "Алёша вернулся домой.", content_language="ru"
    ).lower()
    assert "source language is ru" in system
    assert "write summaries" in system and "english" in system
    assert "preserve proper names in their source spelling" in system
    assert "алёша вернулся домой" in prompt
    assert extract_system_for("novel", content_language="en") == EXTRACT_SYSTEM
    assert extract_system_for("novel", content_language="und") == EXTRACT_SYSTEM
