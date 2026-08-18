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

from pydantic import BaseModel, ConfigDict


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
    canonical_name: str           # the fullest/most formal name; REUSE a roster canonical verbatim
    type: EntityType              # character | place | faction | object
    aliases: list[str]            # other surface forms used THIS chapter (nicknames, patronymics, …)
    matched_roster: bool          # True iff the model linked this to a roster entry
    state: str | None             # optional short note on situation as of this chapter -> entity_state


class Relationship(BaseModel):
    model_config = ConfigDict(extra="forbid")
    src: str                      # canonical_name of the source entity
    dst: str                      # canonical_name of the target entity
    rel_type: RelType             # family | love | rivalry | allegiance | social | other
    label: str                    # human label e.g. 'father of', 'eldest son of'


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    participants: list[str]       # canonical_names of entities involved


class Theme(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str | None


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chapter_summary: str          # 2-4 sentence factual recap of THIS chapter only -> chapter_summaries
    entities: list[Entity]
    relationships: list[Relationship]
    events: list[Event]
    themes: list[Theme]
