"""LIT-6 extraction prompts — lifted near-verbatim from the spike
(``spikes/lit-6-extraction/extract_schema.py``). The anti-foreshadow framing and the Russian-name
guidance are LOAD-BEARING (ADR 0003/0004): the system prompt forbids outside knowledge / later events,
and the roster + the "REUSE its exact canonical_name and set matched_roster=true" instruction are what
make the resolver link forward instead of duplicating the cast.

The roster passed in is the bookmark-bounded running cast (earlier chapters only), so the prompt never
sees a bookmark — spoiler-safety stays the store's job (Inv 11).
"""
from app.language import english_companion_contract

_FACTUAL_MEMORY_GUIDANCE = (
    " Build a selective factual memory, not a catalog of every noun. Keep named people, salient "
    "organizations/factions, significant settings, and concrete objects needed to understand the "
    "section's actions or main subject. Do "
    "not promote generic weather or background scenery into entities merely because wording repeats. "
    "Use null for an unstated entity state and empty arrays when a field has no supported content. "
    "States are snapshots at the latest time established in the section, not timeless traits. Direct "
    "consequences of explicit actions are valid unless later text changes them; placing an item in a "
    "container supports its being there at that point. Do not add unprovided duration, ownership, "
    "traits, or motives. Relationships represent explicitly established lasting ties or persistent "
    "structural links. Put one-off visits, loans, handovers, and other actions in events rather than "
    "lasting relationships; put nicknames in aliases. Themes must be concrete topics established by "
    "the text, not inferred symbolism, intentions, or character judgments; leave themes empty when "
    "only atmosphere suggests them. Placing or storing an item does not imply hiding it, secrecy, or "
    "concealment. A shut door does not establish secret intent. Do not add words such as 'briefly' or "
    "'secretly' unless their meaning is supported by the text."
    " Keep event statements atomic. Adjacent events do not establish that they happened in the "
    "same place or that one caused the next. Attribute each reported detail only to its actual "
    "speaker; do not extend a person's report to facts introduced later in a conversation."
)

_FINAL_EXTRACTION_CHECK = (
    "\n\nFINAL SELECTION CHECK: Keep the memory concise and useful. Omit generic background entities "
    "and repetitive scenery events unless they matter to the section's actions or main subject. "
    "Use empty relationship/theme arrays when nothing suitable is established, and null for unstated "
    "states. Put one-off actions in events and nickname links in aliases. State only supported "
    "snapshots and direct consequences, with no invented motives, secrecy, duration, or ownership. "
    "For EVERY new entity, use a name or identifying phrase actually present in the current text "
    "as its canonical name or source-native alias. Do not expand a first name into a remembered "
    "full name or invent descriptive labels combining words from different places. If an unnamed "
    "role has no useful observed name, describe its action in the summary instead of adding an "
    "entity. Do not invent an alias just to make an identity pass this check."
    " A spouse's surname or a famous character's usual name is not an observed surface: if the "
    "source calls someone only 'his wife', use that exact identifying phrase rather than 'Mrs' "
    "plus the husband's surname. If repair flags a name, remove that unobserved form completely "
    "and use an exact source phrase consistently in the identity, states and event participants."
    " The same restriction applies to the chapter summary and EVERY free-text field, not just "
    "entity records."
)

EXTRACT_SYSTEM = (
    "You extract a structured story-memory from ONE chapter of a novel for a spoiler-safe "
    "reading companion. Extract ONLY what this chapter's text states or clearly implies — never "
    "use outside knowledge of the book, and never refer to later events. Output must match the "
    "provided JSON schema exactly. If you translate or transliterate a canonical name, include its "
    "observed source-native spelling among aliases so its identity remains traceable to the text."
) + _FACTUAL_MEMORY_GUIDANCE

_NEUTRAL_SYSTEM = (
    "You extract a structured reading-memory from ONE section of a book for a spoiler-safe "
    "reading companion. Extract ONLY what this section's text states or clearly implies — never "
    "use outside knowledge of the book, and never refer to later sections. The book may be "
    "nonfiction, verse, drama, a collection, reference material, or structurally unusual; do not "
    "assume a plot or stable cast. Output must match the provided JSON schema exactly. If you "
    "translate or transliterate a canonical name, include its observed source-native spelling among "
    "aliases so its identity remains traceable to the text."
) + _FACTUAL_MEMORY_GUIDANCE


def extract_system_for(book_type="novel", content_language="und"):
    """Choose narrative or neutral framing; all profiles share the factual field contract."""
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
            + caution + _FINAL_EXTRACTION_CHECK
        )
    return (
        f"CHAPTER: {title}\n\n"
        f"RUNNING CAST ROSTER (entities already known from earlier chapters — if an entity here "
        f"reappears, REUSE its exact canonical_name and set matched_roster=true so it links instead "
        f"of duplicating):\n{roster_for_prompt(roster)}\n\n"
        f"CHAPTER TEXT:\n{chapter_text}\n\n"
        f"Extract entities (with aliases used this chapter), relationships, events, themes, and a "
        f"2-4 sentence chapter summary. Russian names: only when the chapter text or visible roster "
        f"establishes these forms as the same identity, treat 'Alexey', 'Alyosha', 'Alexey "
        f"Fyodorovitch Karamazov' as ONE entity with one canonical_name and only source/roster-supported "
        f"forms as aliases. Never add a remembered name variant from outside knowledge."
    ) + _FINAL_EXTRACTION_CHECK
