"""LIT-27 — deterministic high-consequence entity/event-role binding.

LIT-25 proves that recap words occur in the bookmark-bounded facts. That is necessary but not
sufficient: once an unrelated fact contains ``murder``, a recap can reuse the grounded word in a new
claim such as ``Fyodor was murdered``. This module extracts a deliberately narrow semantic signature
``(visible entity, event family, agent|patient)`` from explicit name-bearing clauses and requires each
recap signature to be entailed by a fact signature.

This is not general-purpose NLI. The grammar covers concrete, high-consequence outcomes (death,
homicide, conviction, arrest, imprisonment, exile, execution, suicide, betrayal, shooting, stabbing,
poisoning, strangling, and drowning), active/passive voice, possessive/``of`` nominalizations, and
``by`` agents. Ambiguous pronoun-only and role-only claims remain the LLM judge's job. Failure is in
the safe direction: an unsupported explicit binding regenerates the recap.
"""
import re
from dataclasses import dataclass

from app.eval.spoiler_gate.grounding import split_sentences
from app.ingest.extraction.resolve import ROLE_NOUNS, _norm
from app.unicode_text import word_tokens

# A form maps to a semantic family. Transitives can bind an agent and a patient; intransitives bind
# the named subject as patient; nominals bind possessors / ``of`` patients and ``by`` agents.
_TRANSITIVE = {
    **{w: "homicide" for w in (
        "kill", "kills", "killed", "killing", "murder", "murders", "murdered", "murdering",
        "assassinate", "assassinates", "assassinated", "assassinating", "slay", "slays", "slew",
        "slain",
    )},
    **{w: "conviction" for w in ("convict", "convicts", "convicted", "convicting")},
    **{w: "arrest" for w in ("arrest", "arrests", "arrested", "arresting")},
    **{w: "imprisonment" for w in ("imprison", "imprisons", "imprisoned", "imprisoning")},
    **{w: "exile" for w in ("exile", "exiles", "exiled", "exiling", "banish", "banishes",
                                      "banished", "banishing")},
    **{w: "execution" for w in ("execute", "executes", "executed", "executing")},
    **{w: "betrayal" for w in ("betray", "betrays", "betrayed", "betraying")},
    **{w: "shooting" for w in ("shoot", "shoots", "shot", "shooting")},
    **{w: "stabbing" for w in ("stab", "stabs", "stabbed", "stabbing")},
    **{w: "poisoning" for w in ("poison", "poisons", "poisoned", "poisoning")},
    **{w: "strangling" for w in ("strangle", "strangles", "strangled", "strangling")},
    **{w: "drowning" for w in ("drown", "drowns", "drowned", "drowning")},
    **{w: "hanging" for w in ("hang", "hangs", "hanged", "hung", "hanging")},
}
_INTRANSITIVE = {
    **{w: "death" for w in ("die", "dies", "died", "dying", "dead", "perish", "perishes",
                                      "perished", "perishing")},
}
_NOMINAL = {
    "murder": "homicide", "murdering": "homicide", "killing": "homicide",
    "assassination": "homicide", "death": "death",
    "conviction": "conviction", "convictions": "conviction",
    "arrest": "arrest", "arrests": "arrest",
    "imprisonment": "imprisonment", "exile": "exile", "banishment": "exile",
    "execution": "execution", "executions": "execution",
    "suicide": "suicide", "betrayal": "betrayal",
    "shooting": "shooting", "stabbing": "stabbing", "poisoning": "poisoning",
    "strangling": "strangling", "drowning": "drowning",
}

_PASSIVE = frozenset("was were is are be been being gets get got found declared".split())
_PREDICATE_BRIDGE = frozenset(
    "was were is are be been being gets get got has have had allegedly reportedly supposedly "
    "wrongly brutally secretly suddenly later already finally eventually recently found declared"
    .split()
)
_OBJECT_BRIDGE = frozenset(
    "a an the his her their old young alleged supposed innocent guilty brutally secretly by"
    .split()
)
_POSSESSIVE_BRIDGE = frozenset(
    "s sudden violent brutal recent alleged supposed eventual execution judicial wrongful"
    .split()
)
_COMMIT_FORMS = frozenset("commit commits committed committing".split())
_ARTICLES = frozenset("a an the".split())
_DEATH_ENTAILERS = frozenset({"homicide", "execution", "suicide", "hanging"})
_PAST_PARTICIPLES = frozenset(
    "killed murdered assassinated slain convicted arrested imprisoned exiled banished executed "
    "betrayed shot stabbed poisoned strangled drowned hanged hung"
    .split()
)
_OBJECT_PRONOUNS = frozenset("me him her us them himself herself themselves".split())
_NON_FACTIVE = frozenset(
    "suspect suspects suspected suspicion believe believes believed think thinks thought claim claims "
    "claimed allege alleges alleged allegedly accuse accuses accused accusation suggest suggests "
    "suggested supposedly reportedly possible possibly perhaps maybe may might could rumor rumour"
    .split()
)
_SCOPE_SPLIT = re.compile(r"[;—]|\b(?:but|however|yet)\b", re.I)


@dataclass(frozen=True)
class _Mention:
    start: int
    end: int
    entity_id: object


@dataclass(frozen=True)
class _Signature:
    entity_id: object
    family: str
    role: str


def _tokens(text):
    return word_tokens(_norm((text or "").replace("’", "'")))


def _name_material(words):
    return {word for word in words if len(word) >= 2 and word not in ROLE_NOUNS and word != "s"}


def _entity_index(entities):
    """Build longest surface phrases plus unique name tokens without relational-alias contamination."""
    canonical_owners = {}
    records = []
    for entity in entities or []:
        eid = entity.get("entity_id")
        canonical = _tokens(entity.get("canonical_name", ""))
        if eid is None or not _name_material(canonical):
            continue
        records.append((eid, canonical, list(entity.get("aliases", []))))
        for word in _name_material(canonical):
            canonical_owners.setdefault(word, set()).add(eid)

    # First collect safe identity forms. Exact canonical/alias overlap coalesces extraction duplicates
    # (the real store has three historical Dmitri rows linked by full-name aliases). A relational
    # alias containing role/possessive material is NOT an identity form: "Fyodor's first wife" must
    # not merge the wife into Fyodor or make the token "Fyodor" ambiguous.
    forms_by_entity = {}
    for eid, canonical, aliases in records:
        forms = {tuple(canonical)}
        for alias in aliases:
            words = _tokens(alias)
            material = _name_material(words)
            names_other = any(canonical_owners.get(word, {eid}) - {eid} for word in material)
            relational = bool(set(words) & ROLE_NOUNS or "s" in words)
            if not material or (names_other and relational):
                continue
            forms.add(tuple(words))
        forms_by_entity[eid] = forms

    parent = {eid: eid for eid in forms_by_entity}

    def find(eid):
        while parent[eid] != eid:
            parent[eid] = parent[parent[eid]]
            eid = parent[eid]
        return eid

    def union(left, right):
        lroot, rroot = find(left), find(right)
        if lroot != rroot:
            keep, merge = sorted((lroot, rroot), key=str)
            parent[merge] = keep

    form_owners = {}
    for eid, forms in forms_by_entity.items():
        for form in forms:
            form_owners.setdefault(form, []).append(eid)
    canonical_forms = {}
    for eid, canonical, _aliases in records:
        canonical_forms.setdefault(tuple(canonical), []).append(eid)
    for form, owners in form_owners.items():
        # A multi-token exact full-name overlap is strong identity evidence. A shared one-word alias
        # is not: two real people can share a first name/nickname, and merging them would fail open.
        if len(_name_material(form)) < 2 and len(canonical_forms.get(form, [])) < 2:
            continue
        for other in owners[1:]:
            union(owners[0], other)

    phrases, token_owners = [], {}
    for eid, forms in forms_by_entity.items():
        root = find(eid)
        for form in forms:
            phrases.append((form, root))
            for word in _name_material(form):
                token_owners.setdefault(word, set()).add(root)
    phrases = sorted(set(phrases), key=lambda item: (-len(item[0]), item[0], str(item[1])))
    phrase_lookup = {}
    for phrase, eid in phrases:
        phrase_lookup.setdefault(phrase[0], []).append((phrase, eid))
    token_identity = {}
    for word, owners in token_owners.items():
        if len(owners) == 1:
            token_identity[word] = next(iter(owners))
        else:
            # Keep an ambiguous explicit first name visible to the grammar, but give it an identity
            # no full-name fact can accidentally satisfy. A fact using the same ambiguous surface can
            # still support it; otherwise the recap regenerates rather than guessing which person.
            token_identity[word] = ("ambiguous", *sorted(owners, key=str))
    return phrase_lookup, token_identity


def _mentions(words, index):
    phrase_lookup, token_identity = index
    out, covered = [], set()
    for start, word in enumerate(words):
        for phrase, eid in phrase_lookup.get(word, ()):
            size = len(phrase)
            if tuple(words[start:start + size]) == phrase:
                out.append(_Mention(start, start + size - 1, eid))
                covered.update(range(start, start + size))
    for pos, word in enumerate(words):
        if pos not in covered and word in token_identity:
            out.append(_Mention(pos, pos, token_identity[word]))
    return sorted(set(out), key=lambda mention: (mention.start, mention.end, str(mention.entity_id)))


def _left(mentions, event_pos, max_gap=6):
    candidates = [m for m in mentions if m.end < event_pos and event_pos - m.end - 1 <= max_gap]
    return max(candidates, key=lambda m: m.end, default=None)


def _right(mentions, event_pos, max_gap=6):
    candidates = [m for m in mentions if m.start > event_pos and m.start - event_pos - 1 <= max_gap]
    return min(candidates, key=lambda m: m.start, default=None)


def _add(out, mention, family, role):
    if mention is not None:
        out.add(_Signature(mention.entity_id, family, role))


def _clause_signatures(text, index):
    words = _tokens(text)
    mentions = _mentions(words, index)
    out = set()
    for pos, word in enumerate(words):
        # A suspicion/allegation is not an asserted event. Skip both fact and recap signatures inside
        # that local scope: the allegation can be repeated faithfully, but it must not license a later
        # concrete "X did Y" recap binding.
        if set(words[max(0, pos - 8):pos]) & _NON_FACTIVE:
            continue
        left, right = _left(mentions, pos), _right(mentions, pos)

        if word in _INTRANSITIVE and left is not None:
            bridge = words[left.end + 1:pos]
            if len(bridge) <= 4 and set(bridge) <= _PREDICATE_BRIDGE:
                _add(out, left, _INTRANSITIVE[word], "patient")

        if word in _TRANSITIVE:
            family = _TRANSITIVE[word]
            active = passive = False
            if left is not None:
                bridge = words[left.end + 1:pos]
                if len(bridge) <= 4 and set(bridge) <= _PREDICATE_BRIDGE:
                    passive = bool(set(bridge) & _PASSIVE)
                    # A postpositive/reduced passive has no auxiliary: "Marfa discovers Fyodor
                    # murdered." Past participle + no object signal binds Fyodor as PATIENT, not as
                    # the killer. "Smerdyakov killed him/the man/Fyodor" retains the active reading.
                    after = words[pos + 1:pos + 4]
                    right_bridge = words[pos + 1:right.start] if right is not None else []
                    object_signal = bool(
                        (right is not None and len(right_bridge) <= 4
                         and set(right_bridge) <= _OBJECT_BRIDGE)
                        or set(after) & _OBJECT_PRONOUNS
                        or (after and after[0] in _ARTICLES and len(after) >= 2)
                    )
                    if not passive and word in _PAST_PARTICIPLES and not object_signal:
                        passive = True
                    active = not passive
                    _add(out, left, family, "patient" if passive else "agent")
            if right is not None:
                bridge = words[pos + 1:right.start]
                if len(bridge) <= 4 and set(bridge) <= _OBJECT_BRIDGE:
                    if passive and "by" in bridge:
                        _add(out, right, family, "agent")
                    elif active or left is None:
                        _add(out, right, family, "patient")
            if active and left is not None and set(words[pos + 1:pos + 3]) & {
                    "himself", "herself", "themselves"}:
                _add(out, left, family, "patient")

        if word in _NOMINAL:
            family = _NOMINAL[word]
            if left is not None:
                bridge = words[left.end + 1:pos]
                if bridge and bridge[0] == "s" and set(bridge) <= _POSSESSIVE_BRIDGE:
                    _add(out, left, family, "patient")
                trimmed = [token for token in bridge if token not in _ARTICLES]
                if trimmed and trimmed[-1] in _COMMIT_FORMS and len(trimmed) <= 3:
                    _add(out, left, family, "agent")
            if right is not None:
                bridge = words[pos + 1:right.start]
                if "of" in bridge and len(bridge) <= 4:
                    _add(out, right, family, "patient")
                if "by" in bridge and len(bridge) <= 4:
                    _add(out, right, family, "agent")
    return out


def _extract(text, index):
    found = []
    for sentence in split_sentences(text):
        for clause in (part.strip() for part in _SCOPE_SPLIT.split(sentence)):
            if not clause:
                continue
            for signature in _clause_signatures(clause, index):
                found.append((signature, sentence))
    return found


def _supports(fact, recap):
    if fact == recap:
        return True
    return (fact.entity_id == recap.entity_id and fact.role == recap.role == "patient"
            and recap.family == "death" and fact.family in _DEATH_ENTAILERS)


def unsupported_event_bindings(recap_text, facts):
    """Return explicit recap bindings not entailed by the bookmark-bounded fact bindings."""
    index = _entity_index(facts.get("_entities", []))
    if not index[0]:
        return []
    fact_texts = list(facts.get("chapter_summaries", [])) + list(facts.get("events", []))
    fact_signatures = {signature for text in fact_texts for signature, _sentence in _extract(text, index)}
    hits, seen = [], set()
    for signature, sentence in _extract(recap_text, index):
        if any(_supports(fact, signature) for fact in fact_signatures) or signature in seen:
            continue
        seen.add(signature)
        hits.append({
            "sentence": sentence,
            "entity_id": signature.entity_id,
            "family": signature.family,
            "role": signature.role,
        })
    return hits
