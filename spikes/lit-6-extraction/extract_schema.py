#!/usr/bin/env python3
"""LIT-6 — the per-chapter extraction CONTRACT (schema-validated output) and how it maps
onto the LIT-5 bitemporal tables. Stdlib only (no pydantic/instructor available — this is
the same shape one would express as Pydantic models for Instructor in the real build).

The model, given a chapter's text AND the running cast roster, emits ONE JSON object. Every
piece maps to a LIT-5 table; the chapter ordinal becomes `revealed_at` at ingest time so the
extractor never has to know about bookmarks or spoilers — the store enforces that.
"""

# JSON Schema handed to the structured-output model AND used to validate before ingest.
EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["chapter_summary", "entities", "relationships", "events", "themes"],
    "properties": {
        "chapter_summary": {
            "type": "string",
            "description": "2-4 sentence factual recap of THIS chapter only -> chapter_summaries.",
        },
        "entities": {
            "type": "array",
            "description": "Every named character/place/faction/object that appears -> entities(+aliases,+entity_state).",
            "items": {
                "type": "object",
                "additionalProperties": False,
                # NB: every property is listed in `required` (state nullable) so OpenAI/OpenRouter/
                # Groq STRICT json_schema mode accepts it — strict rejects optional-not-in-required.
                "required": ["canonical_name", "type", "aliases", "matched_roster", "state"],
                "properties": {
                    "canonical_name": {
                        "type": "string",
                        "description": "The fullest/most formal name. If this entity is already in the "
                                       "roster, REUSE the roster's exact canonical_name verbatim.",
                    },
                    "type": {"type": "string", "enum": ["character", "place", "faction", "object"]},
                    "aliases": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Other surface forms used for this entity in THIS chapter "
                                       "(nicknames, patronymics, epithets, pronún-name variants).",
                    },
                    "matched_roster": {
                        "type": "boolean",
                        "description": "True if this is the SAME entity as a roster entry (you linked it); "
                                       "false if newly introduced this chapter.",
                    },
                    "state": {
                        "type": ["string", "null"],
                        "description": "Optional: a short note on the entity's situation as of this "
                                       "chapter (location/condition/role) -> entity_state.",
                    },
                },
            },
        },
        "relationships": {
            "type": "array",
            "description": "Directed relationships revealed THIS chapter -> edges.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["src", "dst", "rel_type", "label"],
                "properties": {
                    "src": {"type": "string", "description": "canonical_name of the source entity"},
                    "dst": {"type": "string", "description": "canonical_name of the target entity"},
                    "rel_type": {"type": "string",
                                 "enum": ["family", "love", "rivalry", "allegiance", "social", "other"]},
                    "label": {"type": "string", "description": "human label e.g. 'father of', 'eldest son of'"},
                },
            },
        },
        "events": {
            "type": "array",
            "description": "Plot beats that happen THIS chapter -> events(+event_participants).",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["summary", "participants"],
                "properties": {
                    "summary": {"type": "string"},
                    "participants": {"type": "array", "items": {"type": "string"},
                                     "description": "canonical_names of entities involved"},
                },
            },
        },
        "themes": {
            "type": "array",
            "description": "Motifs/themes surfacing THIS chapter -> themes.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "description"],   # strict-compat (description nullable)
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": ["string", "null"]},
                },
            },
        },
    },
}

# How each output field lands in the LIT-5 schema (documentation + sanity reference).
TABLE_MAP = {
    "chapter_summary": "chapter_summaries(kind='chapter')",
    "entities[].canonical_name/type": "entities(canonical_name,type,revealed_at=ordinal)",
    "entities[].aliases[]": "aliases(entity_id,surface_form,revealed_at)",
    "entities[].state": "entity_state(entity_id,status_json,revealed_at)",
    "relationships[]": "edges(src_entity,dst_entity,rel_type,label,revealed_at)",
    "events[]": "events(summary,revealed_at) + event_participants(event_id,entity_id)",
    "themes[]": "themes(name,description,revealed_at)",
}


_ENT_TYPES = {"character", "place", "faction", "object"}
_REL_TYPES = {"family", "love", "rivalry", "allegiance", "social", "other"}


def validate(obj):
    """Structural validator (stdlib stand-in for jsonschema) — the ONLY guard on the stub and any
    non-strict provider path, so it enforces the enums + array types the schema advertises, not just
    key presence. Returns (ok, errors)."""
    errs = []
    if not isinstance(obj, dict):
        return False, ["root is not an object"]
    for k in EXTRACTION_SCHEMA["required"]:
        if k not in obj:
            errs.append(f"missing required key: {k}")
    if not isinstance(obj.get("chapter_summary", ""), str):
        errs.append("chapter_summary must be a string")
    for e in obj.get("entities", []):
        if not isinstance(e, dict) or "canonical_name" not in e or "type" not in e:
            errs.append(f"bad entity: {e!r}")
            continue
        if e["type"] not in _ENT_TYPES:
            errs.append(f"bad entity type {e.get('type')!r} for {e.get('canonical_name')!r}")
        if not isinstance(e.get("aliases", []), list):
            errs.append(f"aliases not a list for {e.get('canonical_name')!r}")
    for r in obj.get("relationships", []):
        if not all(k in r for k in ("src", "dst", "rel_type", "label")):
            errs.append(f"bad relationship: {r!r}")
        elif r["rel_type"] not in _REL_TYPES:
            errs.append(f"bad rel_type {r.get('rel_type')!r}")
    for ev in obj.get("events", []):
        if "summary" not in ev or not isinstance(ev.get("participants", []), list):
            errs.append(f"bad event: {ev!r}")
    return (not errs), errs


def roster_for_prompt(roster):
    """Render the bookmark-bounded running cast roster for injection into the extract prompt."""
    if not roster:
        return "(none yet — this is the first chapter)"
    lines = []
    for r in roster:
        al = f" (aka {', '.join(r['aliases'])})" if r.get("aliases") else ""
        lines.append(f"- {r['canonical_name']} [{r['type']}]{al}")
    return "\n".join(lines)


EXTRACT_SYSTEM = (
    "You extract a structured story-memory from ONE chapter of a novel for a spoiler-safe "
    "reading companion. Extract ONLY what this chapter's text states or clearly implies — never "
    "use outside knowledge of the book, and never refer to later events. Output must match the "
    "provided JSON schema exactly."
)


def extract_user_prompt(title, roster, chapter_text):
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
