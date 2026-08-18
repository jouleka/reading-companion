"""LIT-25 — the deterministic sentence-GROUNDING gate (closes ADR 0004 review HIGH #1).

The name check catches future NAMES; the prolepsis tripwire catches future TENSE. The residual both
miss: a paraphrased FUTURE EVENT in PAST tense with no name — 'Dmitri was murdered and wrongly
convicted.' This gate closes it structurally: a spoiler-safe recap is contractually built from ONLY
the supplied facts (SYNTH_SYSTEM), so every recap sentence must be lexically TRACEABLE to those facts.
A sentence whose content words are mostly absent from the facts describes something the facts don't
contain — over-reach at best, a future event at worst — and is a HARD signal.

TWO TIERS (empirically calibrated on live gpt-4o recaps over the real Karamazov store — the LIT-25
close-out): coverage below the HARD floor means essentially NOTHING in the sentence traces to the
facts (pure invention / a foreign event: planted attacks measured 0.00–0.14 after the name strip) →
HARD reject. The band between the floor and the soft threshold is partially-grounded PARAPHRASE —
live characterization sentences ('...known for his chaotic and immoral lifestyle', 0.286) live here,
and ADR 0004 deliberately classifies characterization over-reach as SOFT (judge-reviewable), so this
band REPORTS but does not reject. Grounded live recap sentences measured 0.50–1.00.

LIT-25 HONEST LIMIT (pass-2 measured, plainly): the guarantee here is PER-TOKEN LEXICAL. Once an event word
legitimately enters the facts, a future event REUSING it grounds fully and passes CLEAN — on the live
book 'murder' enters at bm 40 (Zossima's visitor's past confession), so "Fyodor was murdered." passes
at bm 40–54 (the murder is @55) and "Smerdyakov killed him." at bm 60–76, invisible to this lexical
layer. LIT-27's explicit entity/event-role binding now deterministically closes those name-bearing
shapes; implicit pronouns/coreference and general NLI remain the LLM judge's backstop. What IS closed
here: untraceable sentences/clauses, event-stem
and past-tense-ish ungrounded clauses, name-only statements (sentence and clause level), bullet and
abbreviation channels.

Direction of failure: OVER-BLOCK within the hard tier (regenerate), never a leak.
Shared by the eval and ``assert_recap_safe`` (the D-A9 single-implementation rule). Pure — no DB.
"""
import re

from app.ingest.extraction.resolve import ROLE_NOUNS, _norm
from app.unicode_text import proper_words, word_tokens

GROUNDING_THRESHOLD = 0.35                       # below this (and >= the floor): SOFT weakly-grounded
HARD_GROUNDING_FLOOR = 0.15                      # below this: HARD ungrounded (reject)

# Compact function-word list: these words carry no event content and must not dilute or inflate
# coverage. Deliberately small + auditable (not a full NLP stoplist).
STOPWORDS = frozenset("""
a an the this that these those his her its their our my your one two three
i you he she it we they him them us me who whom whose which what
is are was were be been being am do does did done has have had having
will would shall should can could may might must
and or but nor so yet if then than as because while when where after before
of in on at by for with from to into onto over under between through during
not no never also very too more most much many some any all both each even
there here out up down off again once about against
""".split())

_SENT_RE = re.compile(r"[^.!?…\n]+[.!?…]*")
# Honorifics/abbreviations whose trailing dot is NOT a sentence boundary ('Mr. Karamazov' must not
# split into a hard-rejecting 'Mr.' fragment — review pass-1 MEDIUM over-block). 'No' is deliberately
# ABSENT: it ends real sentences, and swallowing that boundary merged a leak into a grounded sentence
# (pass-2 F5 fail-open) — only the numeric 'No. 5' form is protected. Middle initials ('Pyotr A.
# Miusov') are folded so an 'A.' fragment can't name-only-reject a legit recap (pass-2 F6).
_ABBR_RE = re.compile(r"\b(Mr|Mrs|Ms|Dr|St|Mme|Mlle|Prof|Rev|Fr|Sr|Jr|Capt|Gen|Col|Lt)\.")
_NUMREF_RE = re.compile(r"\bNo\.(?=\s*\d)")
_INITIAL_RE = re.compile(r"\b([A-ZÀ-Ý])\.(?=\s+[A-ZÀ-Ý])")

# High-consequence EVENT stems (folded token space). An UNGROUNDED clause carrying one of these is
# the realistic leak shape (review pass-1 BLOCKER: '…grounded material, and X was murdered.' passed
# at 0.889 sentence coverage). Measured at 0.0% hard-FP across 502 grounded live recap sentences.
# Pass-2 F4 additions (the lexicon missed 12/14 realistic leak clauses): the PAST-TENSE rule below is
# the general net; the stems keep tense-less forms ('in murder') covered.
EVENT_STEMS = frozenset("""
murder kill death dead die dying convict trial arrest poison hang execut suicide betray
stab shot shoot drown strangl prison exile corpse funeral grave widow burn ablaze slain assassin
""".split())

# Pass-2 F4 (measured: 0/597 verbatim FP, 2.2% leave-one-out upper bound — over-block only): an
# UNGROUNDED clause whose ungrounded words include a past-tense-ish token is a narrated event the
# facts don't contain. 'died' itself matched no stem (len-3 'die' is exact-only and 'ed' can't fold
# below 3 chars) — the irregular list covers it.
_IRREGULAR_PAST = frozenset("died slain fell fled wept hung hanged sank drowned".split())
# Auxiliary verbs marking a STATEMENT in a name-only clause ('Mitya did it' behind a grounded clause
# — pass-2 F3; measured 0/597 FP vs 17.9% for the naive name-only-clause rule).
_AUX = frozenset("was were is are be been being am do does did has have had will would".split())

# Clause boundaries within a sentence: separating punctuation + coordinating/subordinating joiners
# (newline handled at the sentence level — a bullet recap must not merge into one diluted pseudo-
# sentence, review pass-1).
_CLAUSE_SPLIT_RE = re.compile(r"[;:,()—]| – |\b(?:and|but|while|whereas|yet)\b")


def split_sentences(text):
    text = _ABBR_RE.sub(lambda m: m.group(1), text or "")     # drop the abbreviation dot
    text = _NUMREF_RE.sub("No", text)                         # 'No. 5' -> 'No 5'
    text = _INITIAL_RE.sub(r"\1", text)                       # 'Pyotr A. Miusov' -> 'Pyotr A Miusov'
    return [s.strip() for s in _SENT_RE.findall(text) if s.strip()]


def _clauses(sentence):
    return [c.strip() for c in _CLAUSE_SPLIT_RE.split(sentence) if c and c.strip()]


def _has_event_stem(folded_tokens):
    return any(t == s or (len(s) >= 4 and t.startswith(s)) for t in folded_tokens for s in EVENT_STEMS)


def _raw_words(text):
    return word_tokens(_norm(text or ""))


def _has_past_tense_ungrounded(clause, fact_toks):
    """Pass-2 F4 rule D: an UNFOLDED past-tense-ish word ('ed'-suffix len>=4 or a small irregular
    list) whose folded form the facts don't contain — a narrated, untraceable event."""
    for w in _raw_words(clause):
        if w in STOPWORDS or len(w) < 4:
            continue
        if (w.endswith("ed") or w in _IRREGULAR_PAST) and _fold(w) not in fact_toks:
            return True
    return False


def _proper_subwords(sentence):
    """Folded subword tokens of the CAPITALIZED words in a sentence — the pass-2 fix-B name-only
    detector: a sentence whose non-name content reduces entirely to proper-noun-cased tokens is a
    statement about names ('Mitya did it.' where the nickname grounds via the summaries), not
    traceable prose. Measured 0/597 FP on grounded live populations."""
    caps = [word.text for word in proper_words(sentence or "") if len(word.text) >= 2]
    return content_words(" ".join(caps))


def _fold(w):
    """Light inflection fold so 'runs/ran away' grounds 'ran away' and 'tensions' matches 'tension' —
    reduces paraphrase false-positives without a real stemmer (irregulars stay unmatched; the fail
    direction is over-block). Longest suffix first; never fold below 3 chars."""
    for suf in ("ing", "ed", "es", "ly", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: len(w) - len(suf)]
    return w


def content_words(text):
    return {_fold(w) for w in word_tokens(_norm(text or ""))
            if len(w) >= 2 and w not in STOPWORDS}


def ground_recap(recap_text, facts):
    """Per-sentence grounding of ``recap_text`` against the bookmark-bounded ``supplied_facts`` dict.
    Returns the UNGROUNDED sentences: ``[{sentence, coverage}]`` where coverage = the fraction of the
    sentence's NON-NAME content words present in the facts' content words. Character-name tokens are
    excluded from BOTH sides of the ratio: names are trivially grounded (and future names are the
    entity gate's job), so a name-dense event sentence — 'Fyodor, Dmitri and Ivan were all murdered.'
    — must stand on its EVENT words, not dilute them (5 grounded names would otherwise carry 1
    ungrounded 'murdered' past any threshold). A sentence left with no content words after the name
    strip is name-only (nothing evented) and is skipped; a one-content-word event leak ('He was
    murdered.') is NOT skipped — coverage 0.0 flags it. Returns ``{"hard": [...], "soft": [...]}``
    per the two-tier design above (hard = reject; soft = weakly-grounded, judge-reviewable)."""
    blob = " ".join(list(facts.get("characters", [])) + list(facts.get("chapter_summaries", []))
                    + list(facts.get("events", [])))
    fact_toks = content_words(blob)
    # The name strip includes visible ALIAS surface forms (pass-2 F1/F2: on the live book 'Mitya' is
    # canonical and 'Dmitri' the alias — a canonical-only strip let the alias both evade the name-only
    # flag AND ground the ratio). ROLE/common words are NOT names even when an alias phrase carries
    # them ('the eldest son' must not put 'son' into the strip — it made a grounded 'had three sons'
    # clause flag name-only). The blob keeps its prompt semantics (canonicals only).
    name_toks = content_words(" ".join(list(facts.get("characters", []))
                                       + list(facts.get("aliases", []))))
    name_toks -= {_fold(r) for r in ROLE_NOUNS}
    hard, soft = [], []
    for s in split_sentences(recap_text):
        all_cw = content_words(s)
        cw = all_cw - name_toks
        if not cw:
            # NAME-ONLY sentence: 'Smerdyakov did it.' / 'Fyodor was no more.' carry the whodunit /
            # a death in names + stopwords alone — invisible to every other check (review pass-1
            # HIGH). If a visible name is present, HARD-flag (0/597 grounded live sentences were
            # name-only; over-block just regenerates — and no runtime judge exists yet for a soft
            # tier). A truly empty sentence stays unjudgeable.
            if all_cw & name_toks:
                hard.append({"sentence": s, "coverage": 0.0, "reason": "name-only"})
            continue
        if cw <= _proper_subwords(s):
            # pass-2 fix B: everything left is proper-noun-cased — a statement about NAMES even when
            # the tokens ground ('Mitya did it.' grounds via the summaries) or the nickname was never
            # DB-registered. Measured 0/597 FP.
            hard.append({"sentence": s, "coverage": 0.0, "reason": "name-only"})
            continue
        coverage = len(cw & fact_toks) / len(cw)
        if coverage < HARD_GROUNDING_FLOOR:
            hard.append({"sentence": s, "coverage": round(coverage, 3)})
            continue
        # CLAUSE-DILUTION defense (review pass-1 BLOCKER): a grounded clause must not launder a leak
        # clause. Hard when the clause is essentially untraceable AND carries a high-consequence event
        # stem OR a past-tense-ish ungrounded word (pass-2 F4 — the 26-stem lexicon alone missed 12/14
        # realistic leak clauses), or when it is a NAME-ONLY STATEMENT ('…; Mitya did it.' — pass-2
        # F3: name + auxiliary, measured 0/597 FP vs 17.9% for the naive name-only-clause rule).
        # The generic per-clause low stays soft (naive per-clause hard measured 12-45% FP).
        clause_hit = None
        for c in _clauses(s):
            ccw_all = content_words(c)
            ccw = ccw_all - name_toks
            if not ccw:
                if (ccw_all & name_toks) and (set(_raw_words(c)) & _AUX):
                    clause_hit = {"sentence": s, "coverage": 0.0, "clause": c, "reason": "name-only"}
                    break
                continue
            ccov = len(ccw & fact_toks) / len(ccw)
            if ccov < HARD_GROUNDING_FLOOR and (
                    _has_event_stem(ccw - fact_toks) or _has_past_tense_ungrounded(c, fact_toks)):
                clause_hit = {"sentence": s, "coverage": round(ccov, 3), "clause": c}
                break
        if clause_hit:
            hard.append(clause_hit)
        elif coverage < GROUNDING_THRESHOLD:
            soft.append({"sentence": s, "coverage": round(coverage, 3)})
    return {"hard": hard, "soft": soft}
