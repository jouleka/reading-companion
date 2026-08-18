"""LIT-6 extraction prompts — lifted near-verbatim from the spike
(``spikes/lit-6-extraction/extract_schema.py``). The anti-foreshadow framing and the Russian-name
guidance are LOAD-BEARING (ADR 0003/0004): the system prompt forbids outside knowledge / later events,
and the roster + the "REUSE its exact canonical_name and set matched_roster=true" instruction are what
make the resolver link forward instead of duplicating the cast.

The roster passed in is the bookmark-bounded running cast (earlier chapters only), so the prompt never
sees a bookmark — spoiler-safety stays the store's job (Inv 11).
"""
from app.language import english_companion_contract

EXTRACT_SYSTEM = (
    "You extract a structured story-memory from ONE chapter of a novel for a spoiler-safe "
    "reading companion. Extract ONLY what this chapter's text states or clearly implies — never "
    "use outside knowledge of the book, and never refer to later events. Output must match the "
    "provided JSON schema exactly."
)

_NEUTRAL_SYSTEM = (
    "You extract a structured reading-memory from ONE section of a book for a spoiler-safe "
    "reading companion. Extract ONLY what this section's text states or clearly implies — never "
    "use outside knowledge of the book, and never refer to later sections. The book may be "
    "nonfiction, verse, drama, a collection, reference material, or structurally unusual; do not "
    "assume a plot or stable cast. Output must match the provided JSON schema exactly."
)


def extract_system_for(book_type="novel", content_language="und"):
    """Novel stays byte-identical; every other/unknown profile uses the neutral contract."""
    base = EXTRACT_SYSTEM if book_type == "novel" else _NEUTRAL_SYSTEM
    return base + english_companion_contract(content_language)


def roster_for_prompt(roster):
    """Render the bookmark-bounded running cast roster for injection into the extract prompt."""
    if not roster:
        return "(none yet — this is the first chapter)"
    lines = []
    for r in roster:
        al = f" (aka {', '.join(r['aliases'])})" if r.get("aliases") else ""
        lines.append(f"- {r['canonical_name']} [{r['type']}]{al}")
    return "\n".join(lines)


def extract_user_prompt(title, roster, chapter_text, *, book_type="novel", content_language="und"):
    if book_type != "novel":
        caution = (
            " This is a collection: reuse a known entity only when this section clearly continues "
            "the same work or identity."
            if book_type == "anthology"
            else ""
        )
        return (
            f"SECTION: {title}\n\n"
            f"KNOWN ENTITIES FROM EARLIER SECTIONS (reuse an exact canonical_name only when the "
            f"text clearly identifies the same entity; otherwise keep identities separate):\n"
            f"{roster_for_prompt(roster)}\n\n"
            f"SECTION TEXT:\n{chapter_text}\n\n"
            "Extract only grounded people, places, organizations/factions, or salient objects; "
            "explicit relationships; concrete actions/developments; topics/themes; and a concise "
            "2-4 sentence section summary. Leave irrelevant arrays empty. Do not invent plot, "
            "characters, relationships, or events merely to fill the schema."
            + caution
        )
    return (
        f"CHAPTER: {title}\n\n"
        f"RUNNING CAST ROSTER (entities already known from earlier chapters — if an entity here "
        f"reappears, REUSE its exact canonical_name and set matched_roster=true so it links instead "
        f"of duplicating):\n{roster_for_prompt(roster)}\n\n"
        f"CHAPTER TEXT:\n{chapter_text}\n\n"
        f"Extract entities (with aliases used this chapter), relationships, events, themes, and a "
        f"2-4 sentence chapter summary. Russian names: treat 'Alexey', 'Alyosha', 'Alexey "
        f"Fyodorovitch Karamazov' as ONE entity with one canonical_name and the rest as aliases."
    )
