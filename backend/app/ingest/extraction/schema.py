"""LIT-6 extraction CONTRACT as Pydantic models — the single schema source of truth (ADR 0007 D-A5).

Mirrors the spike's ``EXTRACTION_SCHEMA`` (``spikes/lit-6-extraction/extract_schema.py``) field-for-field
so the same per-chapter object maps onto the LIT-5 tables, but expressed as Pydantic models with
``str``-subclass enums and ``ConfigDict(extra="forbid")`` so:

  * it is the object passed to ``LLMClient.complete(..., schema=Extraction)``. These models are AUTHORED
    to be OpenAI-strict-compatible — ``ConfigDict(extra="forbid")`` emits ``additionalProperties:false``
    and every field is non-optional (nullable where optional), so the RAW ``model_json_schema()`` already
    satisfies strict mode and the SDK's ``to_strict_json_schema`` accepts it UNCHANGED (an idempotent
    no-op for this schema, not a repair). The transform is the safety net for the GENERAL case where a
    raw Pydantic schema is not strict (ADR 0007 P2-3); here the schema is built to need none;
  * the validated instance is handed downstream **as a dict via ``model_dump(mode="json")``**, so
    ``str``-enums serialize to their string values and ``resolve.py`` / ``pipeline.py`` / the gate keep
    their dict-of-strings contract UNCHANGED (a plain ``Enum`` would dump members — P2-4).

Every property is listed (no Pydantic defaults that would let a field go missing) to mirror the spike's
strict ``required`` set; the two NULLABLE optionals (``state``, ``description``) are required-but-nullable.
"""
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class EntityType(str, Enum):
    character = "character"
    place = "place"
    faction = "faction"
    object = "object"


class RelType(str, Enum):
    family = "family"
    love = "love"
    rivalry = "rivalry"
    allegiance = "allegiance"
    social = "social"
    other = "other"


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    canonical_name: str = Field(description=(
        "Name of a salient person, organization/faction, significant setting, or concrete object "
        "needed to understand the section. Reuse a known identity's canonical name. Do not catalog "
        "generic background nouns. A NEW identity needs an exact phrase present in this section "
        "as its name or source-native alias. Use an observed description such as 'his wife' when "
        "no personal name is given; do not infer a spouse's name or import a remembered full name."
    ))
    type: EntityType              # character | place | faction | object
    aliases: list[str] = Field(description=(
        "Observed alternate names of this same identity, including a source-native surface when "
        "needed to ground a translated/transliterated canonical name. Known links may come from the "
        "prior visible roster. Never invent nicknames or conflate distinct people."
    ))
    matched_roster: bool = Field(description="True only for the same identity as a prior visible roster entry.")
    state: str | None = Field(description=(
        "Concise snapshot at the latest time supported by this section, or null if unstated. Direct "
        "results of explicit actions are valid until later text changes them: an item placed in a "
        "container is in it at that point. No timeless persistence is implied. Do not invent intent, "
        "duration, ownership, or traits, and do not retell unrelated event history here."
    ))


class Relationship(BaseModel):
    model_config = ConfigDict(extra="forbid")
    src: str                      # canonical_name of the source entity
    dst: str                      # canonical_name of the target entity
    rel_type: RelType             # family | love | rivalry | allegiance | social | other
    label: str = Field(description=(
        "Concise text-supported tie, preferably an enduring relationship such as family or "
        "membership. Put one-off visits, loans and handovers in events, and nicknames in aliases."
    ))


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(description=(
        "One concrete, relevant action or development stated by the section, preserving who did "
        "what, negation and uncertainty. Omit repetitive background descriptions."
    ))
    participants: list[str] = Field(description=(
        "Canonical names from extracted entities or the prior roster that are actually involved in "
        "this event. Do not invent identities or treat every name mentioned nearby as a participant."
        " A person merely discussed, invited or expected is not thereby a participant in the "
        "conversation; list its actual speakers/actors instead."
    ))


class Theme(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(description=(
        "A concrete main topic established by this text, not an inferred motive, moral judgment or "
        "symbolic reading. The themes array may be empty."
    ))
    description: str | None = Field(description=(
        "Concise text-grounded explanation of this topic, or null. Placement/storage alone is not "
        "evidence of hiding or secrecy. Do not add intentions or special significance."
    ))


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chapter_summary: str = Field(description=(
        "A concise factual recap of this section's salient content only. Preserve direction and "
        "negation; omit invented motives, future developments, and repetitive atmosphere."
    ))
    entities: list[Entity] = Field(description=(
        "Select meaningful identities and settings/objects needed for the section's main content, "
        "not every scenery noun. Empty is valid when there are no salient entities."
    ))
    relationships: list[Relationship] = Field(description=(
        "Explicit lasting ties or persistent structural links. Prefer events for one-off actions. "
        "Use an empty array when the section establishes no suitable relationship."
    ))
    events: list[Event] = Field(description=(
        "Relevant actions or developments, without repetitive scenery beats. Empty is valid."
    ))
    themes: list[Theme] = Field(description=(
        "Concrete main topics established by the section. Do not force thematic interpretation; "
        "return an empty array when only atmosphere, objects or isolated actions suggest one."
    ))
