"""LIT-10 — bookmark-effective entity split/re-merge corrections.

The correction chapter is itself knowledge: earlier bookmarks must retain the old identity model,
while the corrected identities and reassigned facts appear only at/after the correction frontier.
"""
import json
import sqlite3

import pytest

from app.eval.spoiler_gate.cache import validity_snapshot
from app.memory.store import Store


def _chapters(mem, count=6):
    for ordinal in range(1, count + 1):
        mem.add_chapter(
            f"b:c{ordinal}.xhtml",
            revealed_at=ordinal,
            href=f"c{ordinal}.xhtml",
            content_hash=f"h{ordinal}",
        )


def _by_name(mem, bookmark):
    return {row["canonical_name"]: row for row in mem.view(bookmark).characters()}


def test_split_is_invisible_before_its_reveal_and_reassigns_every_fact_family(tmp_path):
    store = Store(str(tmp_path))
    with store.book("b", meta={"title": "B"}) as mem:
        _chapters(mem)
        merged = mem.add_entity("Alex", "character", 1)
        friend = mem.add_entity("Morgan", "character", 1)
        alias_alex = mem.add_alias(merged, "Lex", 1)
        alias_alexandra = mem.add_alias(merged, "Sandra", 2)
        edge = mem.add_edge(merged, friend, "friendship", "old friends", 1)
        state = mem.add_state(merged, 1, {"note": "the narration treats them as one"})
        event = mem.add_event(
            "Alex attends the gathering.", 2, 1, participants=[(merged, "guest")]
        )

        inventory = mem.entity_correction_inventory([merged], effective_at=3)
        assert {row["alias_id"] for row in inventory["aliases"]} == {
            alias_alex,
            alias_alexandra,
        }
        assert {row["edge_id"] for row in inventory["edges"]} == {edge}
        assert {row["event_id"] for row in inventory["event_participants"]} == {event}
        assert {row["state_id"] for row in inventory["current_states"]} == {state}
        snapshot_before_two = validity_snapshot(mem, 2)
        snapshot_before_three = validity_snapshot(mem, 3)

        result = mem.split_entity(
            merged,
            effective_at=3,
            replacements=[
                {"canonical_name": "Alexander", "type": "character", "state": {"note": "host"}},
                {"canonical_name": "Alexandra", "type": "character", "state": {"note": "guest"}},
            ],
            alias_assignments={alias_alex: [0], alias_alexandra: [1]},
            edge_assignments={edge: [0]},
            event_assignments={event: [1]},
            reason="chapter three reveals that Alex referred to two people",
        )
        alexander, alexandra = result["target_entity_ids"]
        assert validity_snapshot(mem, 2) == snapshot_before_two
        assert validity_snapshot(mem, 3) != snapshot_before_three

        # The correction itself is future knowledge at bookmark two.
        assert set(_by_name(mem, 2)) == {"Alex", "Morgan"}
        assert mem.view(2).bio(merged)["aliases"] == ["Lex", "Sandra"]
        assert mem.view(2).bio(alexander) is None
        assert mem.view(2).bio(alexandra) is None
        assert mem.view(2).bio(merged)["appears_in_events"] == [event]
        assert [r["canonical_name"] for r in mem.view(2).participants_of(event)] == ["Alex"]

        # At the correction chapter the old identity disappears and both replacements become valid.
        assert set(_by_name(mem, 3)) == {"Alexander", "Alexandra", "Morgan"}
        assert mem.view(3).bio(merged) is None
        assert mem.view(3).bio(alexander)["aliases"] == ["Lex"]
        assert mem.view(3).bio(alexandra)["aliases"] == ["Sandra"]
        assert mem.view(3).bio(alexander)["state"] == {"note": "host"}
        assert mem.view(3).bio(alexandra)["state"] == {"note": "guest"}
        assert mem.view(3).bio(alexander)["appears_in_events"] == []
        assert mem.view(3).bio(alexandra)["appears_in_events"] == [event]
        rels = mem.view(3).relationships()
        assert {(r["src_entity"], r["dst_entity"], r["label"]) for r in rels} == {
            (alexander, friend, "old friends")
        }
        assert [r["canonical_name"] for r in mem.view(3).participants_of(event)] == ["Alexandra"]

        correction = mem._audit_all("entity_corrections")[0]
        assert correction["kind"] == "split" and correction["revealed_at"] == 3
        assert json.loads(correction["source_entity_ids_json"]) == [merged]
        assert json.loads(correction["target_entity_ids_json"]) == [alexander, alexandra]
        with pytest.raises(sqlite3.DatabaseError):
            mem._conn.execute("SELECT reason FROM entity_corrections").fetchall()


def test_reader_replace_preserves_past_and_exposes_bounded_provenance(tmp_path):
    store = Store(str(tmp_path))
    with store.book("b", meta={"title": "B"}) as mem:
        _chapters(mem, 3)
        source = mem.add_entity("Mina Harker", "character", 1)
        friend = mem.add_entity("Lucy", "character", 1)
        mem.add_alias(source, "Mrs Harker", 1)
        mem.add_state(source, 1, {"location": "Whitby"})
        mem.add_edge(source, friend, "friendship", "friends", 1)
        event = mem.add_event("Mina writes.", 1, 0, participants=[(source, "writer")])

        result = mem.replace_entity(
            source,
            effective_at=2,
            canonical_name="Wilhelmina Harker",
            reason="The full name is established in the text already read.",
        )
        target = result["target_entity_id"]

        assert set(_by_name(mem, 1)) == {"Lucy", "Mina Harker"}
        assert mem.view(1).bio(target) is None
        assert mem.entity_correction_history(1) == []
        assert set(_by_name(mem, 2)) == {"Lucy", "Wilhelmina Harker"}
        assert mem.view(2).bio(target)["aliases"] == ["Mina Harker", "Mrs Harker"]
        assert mem.view(2).bio(target)["state"] == {"location": "Whitby"}
        assert mem.view(2).bio(target)["appears_in_events"] == [event]
        assert [row["canonical_name"] for row in mem.view(2).participants_of(event)] == [
            "Wilhelmina Harker"
        ]
        history = mem.entity_correction_history(2)
        assert history == [{
            "correction_id": result["correction_id"],
            "kind": "replace",
            "effective_at": 2,
            "source_entities": [{"entity_id": source, "name": "Mina Harker"}],
            "target_entities": [{"entity_id": target, "name": "Wilhelmina Harker"}],
            "reason": "The full name is established in the text already read.",
            "recorded_at": history[0]["recorded_at"],
        }]


def test_split_requires_an_explicit_decision_for_every_active_dependency(tmp_path):
    store = Store(str(tmp_path))
    with store.book("b", meta={"title": "B"}) as mem:
        _chapters(mem, 3)
        merged = mem.add_entity("Taylor", "character", 1)
        other = mem.add_entity("Robin", "character", 1)
        alias = mem.add_alias(merged, "Tay", 1)
        edge = mem.add_edge(merged, other, "kinship", "siblings", 1)
        event = mem.add_event("Taylor arrives.", 1, 1, participants=[(merged, "subject")])

        with pytest.raises(ValueError, match="edge_assignments"):
            mem.split_entity(
                merged,
                effective_at=2,
                replacements=[
                    {"canonical_name": "Taylor One", "type": "character", "state": None},
                    {"canonical_name": "Taylor Two", "type": "character", "state": None},
                ],
                alias_assignments={alias: [0]},
                edge_assignments={},  # every live edge must be assigned or explicitly dropped with []
                event_assignments={event: [1]},
            )

        assert set(_by_name(mem, 2)) == {"Taylor", "Robin"}
        assert mem._audit_all("entity_corrections") == []
        assert mem._audit_all("entities")[0]["invalid_at"] is None
        assert mem.view(2).relationships()[0]["edge_id"] == edge


def test_split_inventory_does_not_consult_a_future_edge_endpoint(tmp_path):
    store = Store(str(tmp_path))
    with store.book("b", meta={"title": "B"}) as mem:
        _chapters(mem, 6)
        merged = mem.add_entity("Taylor", "character", 1)
        future = mem.add_entity("Future Stranger", "character", 5)
        mem.add_edge(merged, future, "mystery", "meets later", 2)

        inventory = mem.entity_correction_inventory([merged], effective_at=3)
        assert inventory["edges"] == []
        targets = mem.split_entity(
            merged,
            effective_at=3,
            replacements=[
                {"canonical_name": "Taylor One", "type": "character", "state": None},
                {"canonical_name": "Taylor Two", "type": "character", "state": None},
            ],
            alias_assignments={},
            edge_assignments={},
            event_assignments={},
        )["target_entity_ids"]

        assert mem.view(2).relationships() == []
        assert mem.view(2).bio(targets[0]) is None
        assert mem.view(5).relationships() == []  # safe under-reveal, never a future-identity leak


def test_merge_preserves_the_pre_reveal_split_and_unifies_later_dependencies(tmp_path):
    store = Store(str(tmp_path))
    with store.book("b", meta={"title": "B"}) as mem:
        _chapters(mem)
        alyosha = mem.add_entity("Alyosha", "character", 1)
        alexey = mem.add_entity("Alexey Karamazov", "character", 2)
        father = mem.add_entity("Fyodor", "character", 1)
        mem.add_alias(alyosha, "Alyosha Karamazov", 1)
        mem.add_alias(alexey, "Alexey", 2)
        mem.add_edge(alyosha, father, "family", "son of", 1)
        mem.add_edge(alexey, father, "family", "son of", 2)  # becomes one deduplicated edge
        event = mem.add_event(
            "The brothers gather.",
            2,
            1,
            participants=[(alyosha, "guest"), (alexey, "guest")],
        )

        result = mem.merge_entities(
            [alyosha, alexey],
            effective_at=4,
            canonical_name="Alexey Fyodorovitch Karamazov",
            state={"note": "the two names are revealed as one identity"},
            reason="the patronymic resolves the duplicate roster entries",
        )
        target = result["target_entity_ids"][0]

        assert set(_by_name(mem, 3)) == {"Alyosha", "Alexey Karamazov", "Fyodor"}
        assert mem.view(3).bio(target) is None
        assert {r["canonical_name"] for r in mem.view(3).participants_of(event)} == {
            "Alyosha",
            "Alexey Karamazov",
        }

        assert set(_by_name(mem, 4)) == {"Alexey Fyodorovitch Karamazov", "Fyodor"}
        bio = mem.view(4).bio(target)
        assert set(bio["aliases"]) == {
            "Alyosha",
            "Alyosha Karamazov",
            "Alexey Karamazov",
            "Alexey",
        }
        assert bio["state"] == {"note": "the two names are revealed as one identity"}
        rels = [r for r in mem.view(4).relationships() if r["src_entity"] == target]
        assert len(rels) == 1 and rels[0]["dst_entity"] == father
        assert [r["canonical_name"] for r in mem.view(4).participants_of(event)] == [
            "Alexey Fyodorovitch Karamazov"
        ]


def test_merge_rejects_cross_type_identity_and_rolls_back(tmp_path):
    store = Store(str(tmp_path))
    with store.book("b", meta={"title": "B"}) as mem:
        _chapters(mem, 3)
        person = mem.add_entity("Jordan", "character", 1)
        place = mem.add_entity("Jordan", "place", 1)
        with pytest.raises(ValueError, match="same type"):
            mem.merge_entities(
                [person, place],
                effective_at=2,
                canonical_name="Jordan",
                state=None,
            )
        assert mem.view(2).bio(person) is not None
        assert mem.view(2).entities_of_type("place")[0]["entity_id"] == place
        assert mem._audit_all("entity_corrections") == []


def test_merge_requires_an_override_for_conflicting_event_roles(tmp_path):
    store = Store(str(tmp_path))
    with store.book("b", meta={"title": "B"}) as mem:
        _chapters(mem, 4)
        left = mem.add_entity("Chris", "character", 1)
        right = mem.add_entity("Christopher", "character", 1)
        event = mem.add_event(
            "Chris sees himself in the record twice.",
            1,
            1,
            participants=[(left, "subject"), (right, "witness")],
        )
        with pytest.raises(ValueError, match="conflicting roles"):
            mem.merge_entities(
                [left, right],
                effective_at=3,
                canonical_name="Christopher",
                state=None,
            )
        assert {row["canonical_name"] for row in mem.view(3).participants_of(event)} == {
            "Chris",
            "Christopher",
        }

        target = mem.merge_entities(
            [left, right],
            effective_at=3,
            canonical_name="Christopher",
            state=None,
            event_roles={event: "subject"},
        )["target_entity_ids"][0]
        assert [row["entity_id"] for row in mem.view(3).participants_of(event)] == [target]


def test_correction_must_be_strictly_later_than_every_source_reveal(tmp_path):
    store = Store(str(tmp_path))
    with store.book("b", meta={"title": "B"}) as mem:
        _chapters(mem, 2)
        entity = mem.add_entity("Sam", "character", 2)
        with pytest.raises(ValueError, match="strictly later"):
            mem.split_entity(
                entity,
                effective_at=2,
                replacements=[
                    {"canonical_name": "Sam One", "type": "character", "state": None},
                    {"canonical_name": "Sam Two", "type": "character", "state": None},
                ],
                alias_assignments={},
                edge_assignments={},
                event_assignments={},
            )

        with pytest.raises(sqlite3.IntegrityError, match="invalid_at"):
            with mem._writer():
                mem._conn.execute(
                    "UPDATE entities SET invalid_at=1 WHERE entity_id=?",
                    (entity,),
                )


def test_split_then_remerge_is_a_valid_identity_history(tmp_path):
    store = Store(str(tmp_path))
    with store.book("b", meta={"title": "B"}) as mem:
        _chapters(mem, 5)
        source = mem.add_entity("Pat", "character", 1)
        split = mem.split_entity(
            source,
            effective_at=2,
            replacements=[
                {"canonical_name": "Pat A", "type": "character", "state": None},
                {"canonical_name": "Pat B", "type": "character", "state": None},
            ],
            alias_assignments={},
            edge_assignments={},
            event_assignments={},
        )["target_entity_ids"]
        merged = mem.merge_entities(
            split,
            effective_at=4,
            canonical_name="Pat A",
            state=None,
        )["target_entity_ids"][0]

        assert set(_by_name(mem, 1)) == {"Pat"}
        assert set(_by_name(mem, 2)) == {"Pat A", "Pat B"}
        assert set(_by_name(mem, 3)) == {"Pat A", "Pat B"}
        assert set(_by_name(mem, 4)) == {"Pat A"}
        assert mem.view(1).bio(merged) is None
        assert [r["kind"] for r in mem._audit_all("entity_corrections")] == ["split", "merge"]
