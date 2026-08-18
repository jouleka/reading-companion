#!/usr/bin/env python3
"""LIT-6 — hand-labeled GOLD alias clusters for The Brothers Karamazov, Book I, chapters I-V.

Written from the source text + knowledge of the novel, INDEPENDENTLY of any system output, to
score entity-resolution precision/recall. Each cluster is one true entity; the listed surface
forms are the variants that appear in these chapters (the spoiler-safe slice). The hard cases the
metric targets:
  - FRAGMENTATION (recall): Alyosha = Alexey = Alexey Fyodorovitch Karamazov must collapse to ONE.
  - OVER-MERGE (precision): the two wives share the patronymic 'Ivanovna' but are DIFFERENT people;
    the three brothers all end '...Fyodorovitch Karamazov' but are DIFFERENT people.
"""

GOLD = {
    "fyodor":   ["Fyodor Pavlovitch Karamazov", "Fyodor Pavlovitch"],
    "dmitri":   ["Dmitri Fyodorovitch Karamazov", "Dmitri Fyodorovitch", "Dmitri", "Mitya"],
    "ivan":     ["Ivan Fyodorovitch Karamazov", "Ivan Fyodorovitch", "Ivan"],
    "alyosha":  ["Alexey Fyodorovitch Karamazov", "Alexey Fyodorovitch", "Alexey", "Alyosha"],
    "adelaida": ["Adelaida Ivanovna Miusov", "Adelaida Ivanovna", "Adelaïda Ivanovna Miüsov", "Adelaïda Ivanovna"],
    "sofya":    ["Sofya Ivanovna"],
    "miusov":   ["Pyotr Alexandrovitch Miusov", "Pyotr Alexandrovitch", "Miusov", "Pyotr Alexandrovitch Miüsov", "Miüsov"],
    "grigory":  ["Grigory", "Grigory Vassilyevitch", "old Grigory"],
    "zosima":   ["the elder Zossima", "Zossima", "Father Zossima", "Father Zosima", "Zosima"],
    "yefim":    ["Yefim Petrovitch Polenov", "Yefim Petrovitch"],
}


def _norm(s):
    return " ".join(s.lower().replace("‐", "-").replace("ï", "i").replace("ü", "u").split())


# normalized surface form -> gold cluster id
_LOOKUP = {}
for cid, forms in GOLD.items():
    for f in forms:
        _LOOKUP[_norm(f)] = cid


def gold_id(name, aliases=None):
    """Map an extracted entity (canonical_name + aliases) to a gold cluster id by EXACT
    normalized match against the curated surface forms, or None if it is not one of the
    labeled main characters (minor characters are excluded from the score). Exact-only on
    purpose: loose substring containment wrongly absorbed distinct minor entities like
    'Mitya's grandmother' -> dmitri or 'the Miüsovs' -> miusov, manufacturing fake
    fragmentation. The gold forms below are curated to cover the real surface forms."""
    for c in [name] + list(aliases or []):
        cid = _LOOKUP.get(_norm(c))
        if cid:
            return cid
    return None
