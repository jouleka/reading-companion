"""LIT-8 vector 3 — synthesis over-reach (the deterministic post-generation scorer) + the pinned D-A9
RUNTIME GATE. Lifted near-verbatim from ``spikes/lit-8-spoiler-eval/harness.py`` per ADR 0007 D-A1
group (a): these deterministic functions are SHARED by the eval harness AND the runtime synthesis /
recap-cache paths, so production enforcement and the merge gate use IDENTICAL logic.

Named production changes vs the spike (ADR 0007 D-A9; complete list — review pass-1 asked for it):
  * ``read_text_upto(db, bm)`` reads the reader-parity prose from ``raw_chapters`` THROUGH THE FUNNEL
    (``db.view(bm)``) instead of taking an external ``texts`` dict, so it is structurally bounded to
    chapters with ``revealed_at <= bm`` (it cannot include a future chapter). Verified byte-identical
    output to the spike's on the fixture.
  * ``assert_recap_safe`` is NEW (the D-A9 runtime gate; the spike had no runtime wrapper).
  * ``reveal_correctness_eval`` derives its prose from the store (audit hatch) instead of an external
    ``texts`` dict, and uses FLOOR-CUMULATIVE prose lookup: a stamp at a prose-less ordinal is checked
    against all prose read up to it (the spike's dict-indexing flagged any non-prose ordinal as
    mis-stamped; a mid-book gap would have been a FALSE flag — the floor fixes that while still
    flagging a stamp past the end of / below all prose, which genuinely can never appear on time).
  * ``structured_eval`` passes ``read_text=read_text_upto(db, bm)`` when scoring the catch_me_up
    rolling recap (the spike passed none) — reader-parity applies to the hero recap exactly as the
    runtime gate does, per D-A9; strictly the safe direction and exercised by a planted-leak test.
  * ``cache_key`` gains ``atom_set_version`` (ADR 0007 Inv 7: a renumber must force a cache miss).

THE FORBIDDEN-SET ORACLE (why this reads ``_audit_all``): the whole point of the future-entity check
is to know which entities are revealed ONLY LATER, so a recap that names one can be REJECTED. That
forbidden set is exactly the entities the bookmark funnel HIDES — so the scorer must read ground truth
via the NAMED audit hatch ``db._audit_all("entities")``. Those future names are used ONLY as a
blocklist (compared against the recap's tokens); they NEVER flow into any returned recap or user-facing
output, and on a hard leak the runtime wrapper RAISES (the recap is discarded). This is the sanctioned
third user of ``_audit_all`` (alongside migration/audit tooling), not a view read. It runs under the
per-book lock (inside ``with store.book() as mem:``), so it is concurrency-safe like every other access.
"""
import re

from app.ingest.extraction.resolve import ROLE_NOUNS, _norm
from app.language import english_recap_contract
from app.unicode_text import is_proper_word, proper_words, word_tokens, words

_LEAD_DET = {"the", "a", "an", "this", "that", "these", "those",
             "his", "her", "their", "our", "my", "your"}


def _proper_nouns(text):
    """Lowercased PROPER-NOUN tokens = words Capitalized in the original (excluding leading
    determiners). Keyed on capitalization, not role-noun filtering, so descriptive 'entities' the
    extractor emits ("the older monk who hated Zossima", "the two distant relations") contribute only
    real names ({zossima}, {}) — not common words like "who"/"older"/"two" that match ordinary prose."""
    out = set()
    for word in proper_words(text):
        tok = word.text
        if len(tok) >= 2 and _norm(tok) not in _LEAD_DET:
            out.add(_norm(tok))
    return out


def _match_tokens(text):
    """Proper-noun tokens SUB-tokenized into the recap-word space (split hyphens/apostrophes, len>=2):
    'Eye-Witness' -> {eye, witness}, "Mitya's" -> {mitya}. Forbidden/visible sets must live in the
    SAME token space as recap words or a hyphenated future name is unenforceable (pass-3 F-P3-3) —
    the mirror of reveal_correctness's pass-2 re-tokenization fix."""
    return {w for token in _proper_nouns(text) for w in word_tokens(token, min_length=2)}


# Prolepsis / future-tense tripwire. A grounded "catch me up" recap describes the PAST; clear FUTURE
# modals are a structural tell of a paraphrased FUTURE event the proper-noun check can't see (ADR 0004
# review HIGH #1). Narrowed to future modals so it does NOT fire on past narration that merely uses
# "eventually"/"years later" ("Adelaïda eventually ran off" is past + grounded). Deterministic, hard.
PROLEPSIS_RE = re.compile(
    r"\b(will|shall|would (?:later|soon|eventually|one day|come to|be|become)|going to|about to|"
    r"is (?:destined|going) to|was to (?:be|become)|destined to|fated to|doomed to)\b", re.I)


def reveal_correctness_eval(db):
    """INDEPENDENT signal defeating the circular ground truth (ADR 0004 review MED #5): an entity's name
    should first appear in PROSE at a chapter ordinal <= its ``revealed_at``. If an entity is stamped
    EARLIER than its name ever appears, the extractor mis-stamped it and the spoiler filter would leak
    it — and a self-consistent ``revealed_at``-vs-``revealed_at`` check could never catch that. The
    prose is read from ``raw_chapters`` (the audit hatch — an eval/ground-truth read, not a view read);
    ``revealed_at`` is the chapter ordinal. Returns ``(checked, bad, bad_examples)``."""
    texts = {}
    for r in db._audit_all("raw_chapters"):
        if r["retracted_at"] is None:
            texts.setdefault(r["revealed_at"], r["text"])      # one raw row per ordinal
    cum, acc = {}, ""
    for o in sorted(texts):
        acc += " " + _norm(texts[o])
        cum[o] = set(word_tokens(acc))                          # tokens present in chapters 1..o (with prose)
    checked = bad = 0
    bad_ex = []
    for r in db._audit_all("entities"):
        if r["retracted_at"] is not None:
            continue
        # word-tokenize the proper nouns the SAME way as the prose index (split apostrophes: "Mitya's"
        # -> "mitya") so a tokenization mismatch is not read as a mis-stamp.
        nouns = {w for token in _proper_nouns(r["canonical_name"])
                 for w in word_tokens(token, min_length=2)}
        if not nouns:
            continue                                            # epithet-only "entity" (no name to locate)
        ra = r["revealed_at"]
        # Tokens of ALL prose at ordinals <= ra (floor-cumulative). ra needn't itself hold prose: a
        # stamp at a prose-less/gap ordinal is checked against everything read up to it; a stamp PAST
        # the end of all prose gets the full-prose set; a stamp BELOW all prose gets the empty set.
        # In the latter two cases an unseen name can never appear by its revealed_at and IS flagged
        # (fail-safe — review pass-1 caught an earlier gap-guard that silently SKIPPED these).
        floor = max((o for o in cum if o <= ra), default=None)
        present_by_ra = cum[floor] if floor is not None else set()
        # Pass-2 F1: EVERY token locatable SOMEWHERE in the book's prose must appear by ra — the old
        # ANY-token pass let a mis-stamped 'Fyodor Zossima'@1 be validated by 'fyodor' alone while
        # 'zossima' (first in prose @4) simultaneously poisoned the gate's whitelist. Tokens appearing
        # NOWHERE in the prose (extractor-normalized forms) stay exempt — un-locatable, not mis-stamped;
        # a name with NO locatable token at all is flagged as before.
        all_prose = cum[max(cum)] if cum else set()
        locatable = nouns & all_prose
        target = locatable or nouns
        checked += 1
        if not (target <= present_by_ra):                       # a locatable token is missing by chapter ra
            bad += 1
            bad_ex.append((r["canonical_name"], ra))
    return checked, bad, bad_ex


class SpoilerGateError(RuntimeError):
    """The runtime spoiler gate REJECTED a recap (a hard future-entity / prolepsis leak) or its
    ``read_text`` (not the funnel-bounded prose). The recap MUST be discarded — fail-closed.

    ``str(e)`` is deliberately NAME-FREE: the rejection specifics name FUTURE entities (sometimes a
    more-revealing one than the recap used — pass-2 proved 'the older monk who hated Zossima' for a
    recap that only said 'Zossima'), so an endpoint that echoed the message would itself spoil. The
    specifics live in ``e.details`` for logs/tests — NEVER serialize ``details`` into a response.
    NB (pass-3): locals-dumping formatters (pytest ``--showlocals``, ``format_exception(...,
    capture_locals=True)``, Sentry-style handlers) will still expose ``details`` and the recap
    variable — keep them off any user-visible surface in the endpoints arc."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


def assert_recap_safe(db, effective_bookmark, recap, *, read_text):
    """THE D-A9 runtime gate. Runtime synthesis MUST call this with the clamped, version-current
    ``effective_bookmark`` (computed once at the route boundary) and ``read_text`` = the funnel-bounded
    prose ``read_text_upto(db, effective_bookmark)``.

    ``read_text`` is a REQUIRED keyword-only argument (no default) so an omission fails LOUD (TypeError)
    rather than silently over-blocking. The wrapper independently re-derives the canonical funnel prose
    and REJECTS any ``read_text`` that differs — in particular one covering chapters PAST the bookmark,
    which would make ``score_recap``'s reader-parity fail OPEN (it would drop future-entity tokens the
    reader has not actually read). On any hard leak it RAISES (the recap is discarded); the forbidden
    future names never reach a return value. Must be called inside the per-book lock (``with
    store.book() as mem:``) — ``db`` is that sole-owned MemoryDB."""
    canonical = read_text_upto(db, effective_bookmark)
    if read_text != canonical:
        raise SpoilerGateError(
            f"read_text is not the funnel-bounded prose for effective_bookmark={effective_bookmark}; "
            "it must be exactly read_text_upto(db, effective_bookmark) (read through the funnel under "
            "the lock). A read_text covering chapters past the bookmark would make reader-parity fail "
            "OPEN.")
    sc = score_recap(db, effective_bookmark, recap, read_text=read_text)
    if (sc["future_entity_leaks"] or sc["prolepsis_hits"] or sc["ungrounded_sentences"]
            or sc["unsupported_event_bindings"]):
        raise SpoilerGateError(
            f"recap REJECTED at bookmark {effective_bookmark}: "
            f"{len(sc['future_entity_leaks'])} future-entity leak(s), "
            f"{len(sc['prolepsis_hits'])} prolepsis hit(s), "
            f"{len(sc['ungrounded_sentences'])} ungrounded sentence(s), "
            f"{len(sc['unsupported_event_bindings'])} unsupported event binding(s) — specifics in .details "
            "(spoiler-bearing; never serialize into a response)",
            details={"future_entity_leaks": sc["future_entity_leaks"],
                     "prolepsis_hits": sc["prolepsis_hits"],
                     "ungrounded_sentences": sc["ungrounded_sentences"],
                     "unsupported_event_bindings": sc["unsupported_event_bindings"],
                     "weakly_grounded_sentences": sc["weakly_grounded_sentences"],
                     "future_theme_hits": sc["future_theme_hits"], "bookmark": effective_bookmark})
    return sc


SYNTH_SYSTEM = ("You write a spoiler-safe 'catch me up' recap for a reader. Use ONLY the facts "
                "provided. Describe ONLY what has ALREADY happened. Do NOT add events/characters/"
                "outcomes not in the facts, and do NOT foreshadow, 'set the stage', hint at, build "
                "anticipation for, or describe anything still to come — not even tension about a future "
                "meeting. No forward-looking sentences. Past-tense, grounded in the supplied facts. "
                "(A live close-out caught gpt-4o adding 'sets the stage for an impending gathering' "
                "under the looser prompt; this wording + the LLM-judge hard gate eliminate it.)")


def synth_prompt(bm, facts, *, book_type="novel"):
    """Build the grounded recap prompt from the bookmark-bounded ``supplied_facts(db, bm)`` — ONLY
    these facts, nothing else (the spoiler-safe contract paired with SYNTH_SYSTEM)."""
    if book_type != "novel":
        return (f"Reader is at the end of section {bm}. Write a concise reading recap using ONLY "
                "these facts. Do not force a plot, cast, or chronology where the facts contain none.\n\n"
                f"PEOPLE OR NAMED ENTITIES: {', '.join(facts['characters'])}\n\n"
                "SECTION SUMMARIES:\n- " + "\n- ".join(facts["chapter_summaries"]) + "\n\n"
                "CONCRETE DEVELOPMENTS:\n- " + "\n- ".join(facts["events"][:20]))
    return (f"Reader is at the end of chapter {bm}. Write a 4-6 sentence recap using ONLY these facts.\n\n"
            f"CHARACTERS: {', '.join(facts['characters'])}\n\n"
            "CHAPTER SUMMARIES:\n- " + "\n- ".join(facts["chapter_summaries"]) + "\n\n"
            "KEY EVENTS:\n- " + "\n- ".join(facts["events"][:20]))


# LIT-29: the FLOWING NARRATIVE reframing. Built ON TOP of SYNTH_SYSTEM (a substring, verbatim) so the
# anti-foreshadow spoiler contract the gate + judge assume is never dropped — only the SHAPE of the prose
# changes: from a roster that re-introduces the cast each chapter to the through-line of what happened
# and why. Shared by BOTH the evolve path and the cumulative fallback (the anti-repetition instruction).
FLOWING_SYSTEM = SYNTH_SYSTEM + (
    " Write a CONCISE, flowing recap of what has happened and WHY — the through-line of events and "
    "their causes — not a roster. Assume the reader has already met these characters: do NOT "
    "re-introduce who each person is; carry the story forward. Stay grounded in the WORDING of the "
    "supplied facts — concrete and specific; do NOT embellish with invented interpretation, motive, "
    "or literary flourish beyond what the facts state. Vary the telling so it does not repeat the "
    "same framing chapter after chapter. A few tight, grounded sentences — not an essay.")

NEUTRAL_FLOWING_SYSTEM = (
    "You write a spoiler-safe reading recap. Use ONLY the supplied facts and describe ONLY material "
    "the reader has already completed. Never add later outcomes, outside knowledge, foreshadowing, "
    "or forward-looking claims. The book may be nonfiction, verse, drama, a collection, reference "
    "material, or structurally unusual: summarize what the sections actually contain without "
    "inventing narrative continuity, a stable cast, character arcs, or event sequences. Stay concise and close to the "
    "facts' wording."
)


def flowing_system_for(book_type="novel", content_language="und"):
    base = FLOWING_SYSTEM if book_type == "novel" else NEUTRAL_FLOWING_SYSTEM
    return base + english_recap_contract(content_language)


def evolve_prompt(bm, prior_recap, delta, *, book_type="novel"):
    """LIT-29 evolve (the key move): recap(N) from recap(N-1) — the story as last told — plus ONLY the
    ``delta`` (facts first revealed at N). The instruction is to EXTEND and adjust, NOT restate; this is
    what makes the recap read continuous and removes the per-chapter repetition. The delta sits under the
    CHAPTER SUMMARIES / KEY EVENTS headers the offline stub echoes, so stub output stays grounded. Paired
    with FLOWING_SYSTEM and gated at N exactly like a cumulative recap — recap(N-1) is already
    <=(N-1)-safe and the delta is <=N-safe, and the evolved recap is checked against N's facts."""
    if book_type != "novel":
        events = delta["events"][:20]
        developments = ("CONCRETE DEVELOPMENTS:\n- " + "\n- ".join(events) + "\n\n") if events else ""
        return (
            f"The reading so far, as last summarized through section {bm - 1}:\n\n{prior_recap}\n\n"
            f"Extend it with ONLY the grounded material from section {bm}. Do not force narrative "
            "continuity, a plot, or people into the recap when the new facts do not support them.\n\n"
            + developments
            + "CHAPTER SUMMARIES:\n- " + "\n- ".join(delta["chapter_summaries"])
        )
    new_chars = ", ".join(delta["characters"]) or "(none new this chapter)"
    events = delta["events"][:20]
    ev = ("KEY EVENTS:\n- " + "\n- ".join(events) + "\n\n") if events else ""
    # CHAPTER SUMMARIES comes LAST: the offline stub echoes from that header to the END, so keeping it
    # last means stub output is grounded chapter prose with no trailing prompt scaffolding bleeding in
    # (an empty "KEY EVENTS:" tail otherwise echoed a name-only fragment the grounding gate hard-flagged).
    return (
        f"The story so far, as you last told it (through chapter {bm - 1}):\n\n{prior_recap}\n\n"
        f"Now EXTEND that recap with ONLY the new developments from chapter {bm} below, staying close "
        f"to these facts' own wording — concrete and grounded, without embellishment or invented "
        f"interpretation. Do not restate what you already told, do not re-introduce the characters — "
        f"continue the through-line.\n\n"
        f"NEWLY INTRODUCED: {new_chars}\n\n"
        + ev
        + "CHAPTER SUMMARIES:\n- " + "\n- ".join(delta["chapter_summaries"]))


# LIT-29: the 'right now' one-liner (option b — its own small model call, spoiler-gated + cached). A
# tight present-focused orientation for the sidebar, distinct from the flowing hero recap.
NOW_SYSTEM = ("You write a ONE- or two-sentence 'right now' line for a reader — the immediate situation "
              "as of the latest chapter, from ONLY the supplied facts. Describe only what has already "
              "happened; do NOT foreshadow or hint at anything to come. Grounded, present-focused, no "
              "spoilers. No more than two short sentences.")

NEUTRAL_NOW_SYSTEM = (
    "You write a ONE- or two-sentence orientation for a reader using ONLY the supplied facts from the "
    "section just completed. Describe only material already read; never add later outcomes, outside "
    "knowledge, foreshadowing, or forward-looking claims. State the most useful grounded takeaway "
    "without imposing narrative continuity, characters, or an immediate situation."
)


def now_system_for(book_type="novel", content_language="und"):
    base = NOW_SYSTEM if book_type == "novel" else NEUTRAL_NOW_SYSTEM
    return base + english_recap_contract(content_language)


def now_prompt(bm, delta, *, book_type="novel"):
    """Build the 'right now' one-liner prompt from the latest chapter's facts (the ``delta``), under the
    stub-echoed CHAPTER SUMMARIES header so the offline one-liner is grounded and clears the gate."""
    events = delta["events"][:20]
    heading = "KEY EVENTS" if book_type == "novel" else "CONCRETE DEVELOPMENTS"
    ev = (heading + ":\n- " + "\n- ".join(events) + "\n\n") if events else ""
    # CHAPTER SUMMARIES last so the stub's echo terminates on grounded prose (see evolve_prompt).
    if book_type != "novel":
        return (
            f"Reader has just finished section {bm}. In one or two short sentences, state the most "
            "useful grounded takeaway from this section. Do not invent a current situation or "
            "narrative through-line.\n\n"
            + ev
            + "CHAPTER SUMMARIES:\n- " + "\n- ".join(delta["chapter_summaries"])
        )
    return (
        f"Reader has just finished chapter {bm}. In one or two short sentences, describe the immediate "
        "situation right now, using ONLY these facts.\n\n"
        + ev
        + "CHAPTER SUMMARIES:\n- " + "\n- ".join(delta["chapter_summaries"]))


def _all_entities_revealed_at(db):
    """entity_id -> revealed_at, from the audit hatch (ground truth, ALL chapters). The forbidden-set
    oracle — see the module docstring."""
    return {r["entity_id"]: r["revealed_at"]
            for r in db._audit_all("entities") if r["retracted_at"] is None}


def supplied_facts(db, bm):
    """The bookmark-bounded facts a grounded recap is ALLOWED to use (what the synthesis prompt gets).
    Read through the funnel (``db.view(bm)``), so nothing past the bookmark can be supplied.
    ``aliases`` = the visible surface forms across ALL entity types — NOT injected into the prompt
    (``synth_prompt`` ignores it) but required by the grounding gate's name strip: on the live book
    'Mitya' is the canonical and 'Dmitri' the alias, so a canonical-only strip let 'Dmitri did it.'
    both evade the name-only flag AND ground the ratio (LIT-25 review pass-2 F1/F2 BLOCKERs).
    ``_entities`` is likewise prompt-private and bookmark-bounded; LIT-27 uses its canonical/alias
    groups to bind explicit event roles without consulting future audit data."""
    v = db.view(bm)
    chars, aliases, entities = [], [], []
    for t in ("character", "place", "faction", "object"):
        for e in v.entities_of_type(t):
            surfaces = [a["surface_form"] for a in v.aliases_of(e["entity_id"])]
            aliases += surfaces
            entities.append({"entity_id": e["entity_id"], "canonical_name": e["canonical_name"],
                             "aliases": surfaces})
            if t == "character":
                chars.append(e["canonical_name"])
    summaries = [r["summary"] for r in v.chapter_summaries()]
    events = [r["summary"] for r in v.timeline()]
    return {"characters": chars, "chapter_summaries": summaries, "events": events,
            "aliases": aliases, "_entities": entities}


def delta_facts(db, bm):
    """The facts FIRST revealed AT chapter ``bm`` (``revealed_at == bm``) — the increment the evolving
    recap folds in (LIT-29). A SUBSET of ``supplied_facts(db, bm)``, read through the SAME funnel
    (``db.view(bm)``), so filtering to ``== bm`` can only ever narrow it — it can never widen past the
    bookmark. ``recap(N)`` is synthesized from ``recap(N-1)`` (the story as last told) + this delta,
    so the prose extends and adjusts rather than re-summarizing the whole history each chapter."""
    v = db.view(bm)
    chars = [r["canonical_name"] for r in v.characters() if r["revealed_at"] == bm]
    summaries = [r["summary"] for r in v.chapter_summaries() if r["revealed_at"] == bm]
    events = [r["summary"] for r in v.timeline() if r["revealed_at"] == bm]
    return {"characters": chars, "chapter_summaries": summaries, "events": events}


def read_text_upto(db, bm):
    """ORIGINAL-CASE prose of every chapter with ``revealed_at <= bm``, assembled ONLY through the
    funnel (ADR 0007 D-A9): ``db.view(bm).chapters()`` yields only visible chapters and ``.raw_text()``
    applies the live-chapter semijoin, so this structurally CANNOT include a future chapter. Original
    case is kept so reader-parity can tell a proper noun the reader saw ("Russia") from a mere common
    word ("town")."""
    v = db.view(bm)
    parts = []
    for ch in v.chapters():
        txt = v.raw_text(ch["chapter_key"])
        if txt:
            parts.append((ch["revealed_at"], ch["chapter_key"], txt))
    parts.sort(key=lambda p: (p[0], p[1]))
    return " ".join(p[2] for p in parts)


def _read_proper_strict(read_text):
    """Tokens the reader saw capitalized in a clearly MID-CLAUSE position — preceded (skipping
    whitespace) by a lowercase letter, comma or semicolon. Sentence-initial capitalization is NOT
    proper-noun evidence (pass-2 F2: a future entity whose sole token merely BEGINS sentences in read
    prose — 'But' — must not be reader-parity-dropped; that was fail-open). Under-inclusion only
    over-blocks (fail-safe): a genuine proper noun seen solely sentence-initial/quote-initial falls
    through to rule (b)."""
    out = set()
    for word in words(read_text):
        tok = word.text
        if len(tok) < 2 or not is_proper_word(tok) or _norm(tok) in _LEAD_DET:
            continue
        cased = any(char.lower() != char.upper() for char in tok if char.isalpha())
        if not cased:                                           # scripts without case: exact name run
            out.add(_norm(tok))
            continue
        j = word.start - 1
        while j >= 0 and read_text[j].isspace():
            j -= 1
        if j >= 0 and (read_text[j].islower() or read_text[j] in ",;"):
            out.add(_norm(tok))
    return out


def score_recap(db, bm, recap_text, read_text=None):
    """DETERMINISTIC synthesis over-reach check. Hard signals include:
      - future_entity_leaks: a recap token that names a future-revealed thing — an ENTITY canonical or
        an ALIAS surface form (incl. a late alias of a visible entity — the nickname itself is future
        knowledge; pass-2 F3 proved 'Siberia', a future alias, previously sailed through). Matched
        CASE-INSENSITIVELY (ADR 0004 HIGH #2) but keyed on proper-noun tokens (capitalized in the
        stored name) so common words are not false leaks.
      - prolepsis_hits: future-tense modal language = a likely future-EVENT spoiler the name check
        can't see (ADR 0004 HIGH #1).
      - unsupported_event_bindings: LIT-27's narrow NLI grammar requires each explicit visible-name +
        high-consequence event + semantic-role claim to be entailed by the supplied facts.
    Whitelisting is EVIDENCE-GATED (pass-2 F1/F2): a visible entity's token only whitelists a future
    name if the reader has actually READ that token (a mis-stamped 'Fyodor Zossima'@1 must not disarm
    the gate for 'zossima'), and reader-parity rule (a) accepts only MID-CLAUSE capitalization as
    proper-noun evidence (sentence-initial 'But' is not). Without ``read_text`` the visible-token
    subtraction is ungated (the spike's weaker eval-only mode — the runtime gate always supplies it).
    ``future_theme_hits`` is a SOFT signal (pass-3 F-P3-1): theme names are extractor-authored
    Title-Case labels of common abstract words, and hard-blocking on them rejected grounded recaps
    built from the store's own supplied facts on the live book — the judge reviews them instead.
    grounding_rate is likewise soft; the LLM-judge is the open-domain paraphrase check. HONEST LIMITS
    (fall to the judge, not deterministic-caught): (1) implicit pronoun/coreference bindings and event
    paraphrases outside LIT-27's narrow high-consequence grammar; (2) future theme labels, which are
    deliberately not hard-gated; (3) a future name
    stored with NO capitalized token anywhere (keyed on capitalization so epithets don't forbid
    common words; anomalous extractor output only); (4) rule (a)'s mid-clause evidence can be faked
    by Title-Case HEADINGS inside raw prose (pass-3 F-P3-4: an entity named a common heading word,
    e.g. 'Rid' — contrived, absent from real data, and the reader did see the word capitalized)."""
    rev = _all_entities_revealed_at(db)
    id2name = {r["entity_id"]: r["canonical_name"] for r in db._audit_all("entities")}
    live_aliases = [a for a in db._audit_all("aliases") if a["retracted_at"] is None]
    live_themes = [t for t in db._audit_all("themes") if t["retracted_at"] is None]

    # Every named thing, split by its own revealed_at: <= bm -> candidate whitelist; > bm -> forbidden.
    # Tokens are SUB-tokenized (_match_tokens) into the same space as recap words — a hyphenated /
    # possessive forbidden token ('eye-witness') was otherwise unmatchable (pass-3 F-P3-3, fail-open).
    # Themes go to a SEPARATE soft bucket (pass-3 F-P3-1): theme names are extractor-authored
    # Title-Case LABELS of common abstract words ('Family Tensions'), and hard-folding them rejected
    # grounded recaps built from the store's own supplied facts on the live book.
    visible, named_future, themed_future = set(), {}, {}
    for eid, ra in rev.items():
        nouns = _match_tokens(id2name[eid])
        if ra <= bm:
            visible |= nouns
        else:
            # ROLE_NOUNS filter: a token like "superior"/"monk" (capitalized in "the Superior") is a
            # role/common word, not a distinctive name — never forbid it (else a grounded recap saying
            # "superior" falsely fails).
            for t in nouns - ROLE_NOUNS:
                named_future.setdefault(id2name[eid], set()).add(t)
    for a in live_aliases:                                # aliases carry their OWN revealed_at
        toks = _match_tokens(a["surface_form"])
        if a["revealed_at"] <= bm:
            visible |= toks
        else:
            owner = id2name.get(a["entity_id"], a["surface_form"])
            for t in toks - ROLE_NOUNS:
                named_future.setdefault(owner, set()).add(t)
    for th in live_themes:
        toks = _match_tokens(th["name"])
        if th["revealed_at"] <= bm:
            visible |= toks
        else:
            for t in toks - ROLE_NOUNS:
                themed_future.setdefault(f"theme:{th['name']}", set()).add(t)

    if read_text is not None:
        read_proper = {w for token in _read_proper_strict(read_text)
                       for w in word_tokens(token, min_length=2)}
        read_any = set(word_tokens(_norm(read_text)))            # all words read so far (any case)
        # F1: a visible token whitelists ONLY if the reader actually read it. In a clean store every
        # visible name appears in read prose (reveal-correctness), so this bites only mis-stamps.
        whitelist = {t for t in visible if t in read_any or t in read_proper}
    else:
        read_proper = read_any = None
        whitelist = visible                                          # spike-parity eval-only mode

    def _apply_parity(bucket):
        # READER-PARITY, FAIL-SAFE: drop a future token the reader has ALREADY read — but only when it
        # is safe. (a) a token the reader saw as a MID-CLAUSE capitalized proper noun ("in Russia") =>
        # reader knows that name, drop it. (b) a token seen only as a lowercase common word
        # ("monastery") => drop ONLY if the entity keeps another distinctive token ("Optin Monastery"
        # keeps "optin"). NEVER drop the SOLE distinguishing token of a future entity the reader saw
        # only lowercase or only sentence-initial ("Town", "But") — that would be fail-OPEN.
        out = {n: toks - whitelist for n, toks in bucket.items()}
        out = {n: toks for n, toks in out.items() if toks}
        if read_text is None:
            return out
        for n, toks in list(out.items()):
            keep = set()
            for t in toks:
                if t in read_proper:
                    continue                                        # (a) reader saw the proper name -> drop
                if t in read_any and (toks - {t}) - read_any:        # (b) common word + a distinctive token remains
                    continue                                        #     -> drop the common one
                keep.add(t)
            if keep:
                out[n] = keep
            else:
                del out[n]
        return out

    fut_by_ent = _apply_parity(named_future)
    fut_by_theme = _apply_parity(themed_future)
    future = {t: n for n, toks in fut_by_ent.items() for t in toks}
    theme_future = {t: n for n, toks in fut_by_theme.items() for t in toks}
    facts = supplied_facts(db, bm)
    supplied = _proper_nouns(" ".join(facts["characters"] + facts["chapter_summaries"] + facts["events"]))
    recap_words = set(word_tokens(_norm(recap_text)))             # case-insensitive whole words
    recap_nouns = _proper_nouns(recap_text)
    future_hits = sorted({future[t] for t in future if t in recap_words})
    theme_hits = sorted({theme_future[t] for t in theme_future if t in recap_words})
    prolepsis = sorted({m.group(0).lower() for m in PROLEPSIS_RE.finditer(recap_text)})
    ungrounded = sorted(t for t in recap_nouns if t not in supplied and t not in future)
    from app.eval.spoiler_gate.grounding import ground_recap          # local import: avoids a cycle
    from app.eval.spoiler_gate.binding import unsupported_event_bindings
    grounding = ground_recap(recap_text, facts)
    bindings = unsupported_event_bindings(recap_text, facts)
    return {"bookmark": bm, "future_entity_leaks": future_hits, "prolepsis_hits": prolepsis,
            "future_theme_hits": theme_hits,                          # SOFT (judge-reviewable)
            "ungrounded_sentences": grounding["hard"],                # HARD (LIT-25: essentially no
            #                        factual trace — the untraceable-event class the name check misses)
            "unsupported_event_bindings": bindings,                   # HARD (LIT-27: fact vocabulary
            #                        rebound to the wrong named subject/object or semantic role)
            "weakly_grounded_sentences": grounding["soft"],           # SOFT (partial paraphrase band)
            "ungrounded_name_tokens": ungrounded,
            "grounded_rate": 1 - len(ungrounded) / max(len(recap_nouns), 1)}
