"""Module C / schema.py (ADR 0007 D-A5) — Pydantic models + str-subclass enums are the single schema
source of truth. They are AT LEAST AS STRICT as the spike's ``extract_schema.validate()`` (a strict
SUPERSET of its rejections — the relationship is one-directional, not two-way equivalence: the model
also rejects extra keys / missing fields the spike silently accepted, the intended tightening), they are
OpenAI-strict-compatible, and they preserve the dict-of-strings downstream contract.
"""
import importlib.util
from pathlib import Path

import pytest
from openai.lib._pydantic import to_strict_json_schema

from app.ingest.extraction.schema import (
    Entity, EntityType, Extraction, RelType, Relationship,
)

_REPO = Path(__file__).resolve().parents[4]


def _spike_validate():
    """Load the stdlib spike validator (``spikes/lit-6-extraction/extract_schema.py``) by path so the
    parity assertion is grounded against the ACTUAL reviewed spike, not a copy."""
    path = _REPO / "spikes" / "lit-6-extraction" / "extract_schema.py"
    spec = importlib.util.spec_from_file_location("_spike_extract_schema", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.validate


def _gold_obj():
    """A fully-formed extraction object — exactly the shape ``client.complete(schema=Extraction)``
    emits (every property present, per the OpenAI-strict ``required`` list)."""
    return {
        "chapter_summary": "Fyodor introduces his sons; Alyosha goes to the monastery.",
        "entities": [
            {"canonical_name": "Fyodor Pavlovitch Karamazov", "type": "character",
             "aliases": ["Fyodor Pavlovitch"], "matched_roster": False, "state": "a landowner"},
            {"canonical_name": "the monastery", "type": "place",
             "aliases": [], "matched_roster": False, "state": None},
        ],
        "relationships": [
            {"src": "Fyodor Pavlovitch Karamazov", "dst": "Alexey Fyodorovitch Karamazov",
             "rel_type": "family", "label": "father of"},
        ],
        "events": [
            {"summary": "Alyosha enters the monastery.", "participants": ["Alexey Fyodorovitch Karamazov"]},
        ],
        "themes": [
            {"name": "faith", "description": "Alyosha's religious calling."},
            {"name": "family", "description": None},
        ],
    }


# Adversarial objects that BOTH the spike validator and the Pydantic model must reject — same verdict.
_ADVERSARIAL = [
    pytest.param({**_gold_obj(), "entities": [
        {"canonical_name": "X", "type": "villain", "aliases": [], "matched_roster": False, "state": None}]},
        id="bad-entity-type"),
    pytest.param({**_gold_obj(), "relationships": [
        {"src": "A", "dst": "B", "rel_type": "nemesis", "label": "x"}]}, id="bad-rel-type"),
    pytest.param({k: v for k, v in _gold_obj().items() if k != "chapter_summary"},
                 id="missing-chapter-summary"),
    pytest.param({**_gold_obj(), "chapter_summary": 123}, id="summary-not-a-string"),
    pytest.param({**_gold_obj(), "entities": [
        {"canonical_name": "X", "type": "character", "aliases": "not-a-list",
         "matched_roster": False, "state": None}]}, id="aliases-not-a-list"),
    pytest.param({**_gold_obj(), "entities": [
        {"type": "character", "aliases": [], "matched_roster": False, "state": None}]},
        id="entity-missing-canonical-name"),
    pytest.param({**_gold_obj(), "events": [{"summary": "x", "participants": "nope"}]},
                 id="event-participants-not-a-list"),
]


def test_gold_object_validates_and_round_trips_to_the_same_dict():
    """An accepted object is unchanged by the new validation path: model_dump(mode="json") == input,
    so resolve.py / pipeline.py / the gate keep their dict-of-strings contract (D-A5)."""
    obj = _gold_obj()
    assert _spike_validate()(obj)[0] is True                 # spike accepts
    dumped = Extraction.model_validate(obj).model_dump(mode="json")
    assert dumped == obj                                     # no transformation, no added/dropped keys


@pytest.mark.parametrize("obj", _ADVERSARIAL)
def test_adversarial_objects_are_rejected_by_both_validators(obj):
    """Parity: every adversarial object the spike validator rejects, the Pydantic model also rejects."""
    assert _spike_validate()(obj)[0] is False                # spike rejects
    with pytest.raises(Exception):                           # ValidationError (a subclass)
        Extraction.model_validate(obj)


def test_model_is_strictly_tighter_than_the_spike_validator():
    """Pin the one-directional relationship (not equivalence): there ARE objects the spike validator
    ACCEPTS that the Pydantic model REJECTS — the intended tightening (extra='forbid' + full fields)."""
    spike = _spike_validate()
    tighter = [
        {**_gold_obj(), "hallucinated_top_key": "x"},                       # extra top-level key
        {**_gold_obj(), "themes": [{"description": "no name"}]},            # theme missing 'name'
        {**_gold_obj(), "entities": [                                       # entity missing matched_roster
            {"canonical_name": "X", "type": "character", "aliases": [], "state": None}]},
    ]
    for obj in tighter:
        assert spike(obj)[0] is True                          # spike accepts (lenient)
        with pytest.raises(Exception):
            Extraction.model_validate(obj)                    # the model rejects (strictly tighter)


def test_str_enums_serialize_to_their_string_values():
    """A plain Enum would dump members, breaking the TEXT-column contract (P2-4). str-subclass enums
    dump to strings."""
    e = Entity(canonical_name="Ivan", type=EntityType.character, aliases=[],
               matched_roster=False, state=None)
    assert e.model_dump(mode="json")["type"] == "character"
    assert isinstance(EntityType.character, str) and EntityType.character == "character"
    r = Relationship(src="A", dst="B", rel_type=RelType.family, label="father of")
    assert r.model_dump(mode="json")["rel_type"] == "family"


def test_extra_keys_are_forbidden():
    """ConfigDict(extra="forbid") — a hallucinated extra field is rejected (a tightening over the spike,
    which silently ignored extras), so an over-eager model output fails closed rather than smuggling a key."""
    with pytest.raises(Exception):
        Entity.model_validate({"canonical_name": "X", "type": "character", "aliases": [],
                               "matched_roster": False, "state": None, "secret": "leak"})


def test_schema_is_openai_strict_compatible():
    """The models are AUTHORED strict-compatible (extra='forbid' + all-required-nullable): every object
    node has additionalProperties:false and EVERY property in `required` (incl. nullable ones). This holds
    on the RAW model_json_schema() too — so to_strict_json_schema is a no-op here, not a repair (the
    honest framing of ADR 0007 P2-3, where the transform is the net for the general non-strict case)."""
    def _walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False
                assert set(node["required"]) == set(node["properties"]), node["properties"].keys()
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    strict = to_strict_json_schema(Extraction)
    _walk(strict)
    _walk(Extraction.model_json_schema())                    # the RAW schema already satisfies strict
    # the nullable optionals (state, description) must remain REQUIRED (nullable), not dropped
    ent = strict["$defs"]["Entity"] if "$defs" in strict else None
    if ent:
        assert "state" in ent["required"]
