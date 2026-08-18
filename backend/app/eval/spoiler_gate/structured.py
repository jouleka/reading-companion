"""LIT-8 vector 1 — structured-read leak eval. Lifted near-verbatim from
``spikes/lit-8-spoiler-eval/harness.py`` (ADR 0007 D-A1 group (a)).

For every bookmark and EVERY ``BookmarkView`` read method, assert no surfaced row OR referenced entity
has ``revealed_at > bookmark`` — re-validating LIT-5 referential closure as a scored eval. Returns
``(reads, leaks, leak_examples)``. The drop-a-filter FALSIFIABILITY probe (monkeypatch the funnel for
one table -> leaks surface) lives in the merge-gate test, mirroring ADR 0004 vector 1.

Named change vs the spike: the catch_me_up rolling-recap is scored WITH ``read_text=read_text_upto(db,
bm)`` (the spike passed none), so reader-parity applies to the hero recap exactly as the D-A9 runtime
gate does. NB this is MORE PERMISSIVE than the spike's no-read_text scoring (parity drops genuinely-
read names), which is correct ONLY because the parity evidence is now strict (mid-clause capitalization,
evidence-gated whitelist — pass-2 F1/F2); a planted leaky rolling-recap test exercises this branch.
"""
from .synthesis import read_text_upto, score_recap


def structured_eval(db, max_bm):
    """For every bookmark and every read method, assert no surfaced row or referenced entity has
    ``revealed_at > bookmark``. Returns (reads, leaks, leak_examples)."""
    reads = leaks = 0
    leak_examples = []
    for bm in range(0, max_bm + 1):
        v = db.view(bm)
        # rows with a direct revealed_at
        rowsets = {
            "characters": v.characters(), "relationships": v.relationships(), "timeline": v.timeline(),
            "themes": v.themes(), "chapter_summaries": v.chapter_summaries(),
        }
        for name, rows in rowsets.items():
            for r in rows:
                reads += 1
                if r["revealed_at"] > bm:
                    leaks += 1
                    leak_examples.append((bm, name, dict(r)))
        # all visible entities (every type), for the per-entity read paths
        visible = list(v.characters())
        for t in ("place", "faction", "object"):
            visible += list(v.entities_of_type(t))
        visible_ids = {c["entity_id"] for c in visible}
        # per-entity paths: aliases_of, current_state, events_for, bio
        for c in visible:
            eid = c["entity_id"]
            for a in v.aliases_of(eid):
                reads += 1
                if a["revealed_at"] > bm:
                    leaks += 1
                    leak_examples.append((bm, "alias", dict(a)))
            st = v.current_state(eid)
            if st:
                reads += 1
                if st["revealed_at"] > bm:
                    leaks += 1
                    leak_examples.append((bm, "current_state", dict(st)))
            for ef in v.events_for(eid):
                reads += 1
                if ef["revealed_at"] > bm:
                    leaks += 1
                    leak_examples.append((bm, "events_for", dict(ef)))
            reads += 1
            if v.bio(eid) is None:                       # a visible entity must have a bio
                leaks += 1
                leak_examples.append((bm, "bio-missing-for-visible", eid))
        # raw_text: a chapter with revealed_at > bm must NOT return text
        for ch in db._audit_all("chapters"):
            reads += 1
            txt = v.raw_text(ch["chapter_key"])
            if ch["revealed_at"] > bm and txt is not None:
                leaks += 1
                leak_examples.append((bm, "raw_text-future", ch["chapter_key"]))
        # referential closure: edge endpoints + event participants must be visible entities
        for e in v.relationships():
            for col in ("src_entity", "dst_entity"):
                reads += 1
                if e[col] not in visible_ids:
                    leaks += 1
                    leak_examples.append((bm, "edge-endpoint-not-visible", e[col]))
        for ev in v.timeline():
            for p in v.participants_of(ev["event_id"]):
                reads += 1
                if p["entity_id"] not in visible_ids:
                    leaks += 1
                    leak_examples.append((bm, "participant-not-visible", p["entity_id"]))
        # catch_me_up()'s rolling recap (the production HERO text) — score it for future leaks
        cmu = v.catch_me_up()
        if cmu.get("recap"):
            sc = score_recap(db, bm, cmu["recap"], read_text=read_text_upto(db, bm))
            if (sc["future_entity_leaks"] or sc["prolepsis_hits"] or sc["ungrounded_sentences"]
                    or sc["unsupported_event_bindings"]):
                leaks += 1                                   # incl. the LIT-25 grounding hard tier —
                leak_examples.append((bm, "catch_me_up-recap-leak", sc))  # a stored untraceable-event
                #                              recap must not green-light vector 1 (review pass-1 F5)
    return reads, leaks, leak_examples
