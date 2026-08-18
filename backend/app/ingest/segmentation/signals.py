"""LIT-4 explicit-signal classification (ADR 0001) — front / back / body by EXPLICIT signals only,
NEVER by position. EPUB3 `epub:type` is authoritative and fail-closed; legacy EPUB2/NCX falls back to
heuristic signals. Lifted near-verbatim from spikes/lit-4-segmentation/segment.py (the classifier is
the spoiler-relevant safety surface: a misclassified cast-list = a chapter-1 leak), hardened over three
review passes:
  * pass-1 BLOCKER: the exact-anchored front allowlist let real labels ("Principal Characters in the
    Story", "Introduction by …") default to body = chapter 1 (a LEAK) -> broadened.
  * pass-2 regression: the broadening OVER-stripped real chapters ("Cast Away", "Index Case", in-story
    "Introduction") -> cast patterns END-ANCHORED + front/back heuristics GATED on the ABSENCE of chapter
    evidence (CHLABEL).
  * pass-2b LEAK gaps: an UNAMBIGUOUS cast head ("Dramatis Personae", incl. the 'Personæ' ligature
    Gutenberg ships; "Persons Represented"; "List of Characters") now SHORT-CIRCUITS to front BEFORE the
    CHLABEL gate (so "Dramatis Personae of Book I" can't leak); the ambiguous cast/intro patterns stay
    gated; and an ambiguous front/back strip (intro/preface/appendix) is FLAGGED so an over-strip is
    auditable, never silent.
"""
import posixpath
import re

START_RE = re.compile(r"START OF (THE|THIS) PROJECT GUTENBERG", re.I)
LICENSE_RE = re.compile(
    r"(END OF (THE|THIS) PROJECT GUTENBERG|PROJECT GUTENBERG.{0,6}LICENSE|Section 1\.\s*General Terms)", re.I)
FRONTNAME = re.compile(r"(cover|title.?page|halftitle|^toc|contents|copyright|colophon|imprint)", re.I)
# Exact-phrase front allowlist (kept from the spike for unambiguous standalone labels).
FRONTLABEL = re.compile(
    r"^\s*(contents|table of contents|list of illustrations|illustrations|title page|cover|copyright|"
    r"frontispiece|dedication|acknowledg\w*|note on the text|about the author)\s*$", re.I)
# chapter evidence: a chapter keyword, OR a leading roman/arabic enumerator ("I.", "1)", "IV.").
CHLABEL = re.compile(r"\b(chapter|letter|act|scene|book|part|prologue|epilogue|canto|stave|volume)\b"
                     r"|\b(?:глава|часть|книга|акт|сцена|том)\b"
                     r"|(?:章|章节|卷|部|篇|幕|場|场)"
                     r"|^\s*(?:[ivxlcdm]+|\d+)\s*[.\):]", re.I)
# UNAMBIGUOUS cast-list heads — NEVER a body chapter title, so they classify front even when the label
# also names a structural unit ("Dramatis Personae of Book I", "Persons Represented in Act I"): a LEADING
# match that BYPASSES the chapter-evidence gate (pass-2b). 'personæ' covers the literal Gutenberg drama
# spelling.
FRONT_CAST_STRONG = re.compile(
    r"^\s*(?:"
    r"(?:the\s+)?dramatis\s+person(?:ae|æ)"
    r"|persons?\s+represented"
    r"|list\s+of\s+(?:characters?|persons?|players?)"
    r"|(?:principal|chief|main)\s+(?:characters?|persons?|players?)"
    r"|(?:the\s+)?cast\s+of\s+characters"
    r"|действующие\s+лица|список\s+персонажей"
    r"|登場人物|登场人物|人物表"
    r")(?=\s|\b|$)", re.I)
# END-ANCHORED ambiguous cast phrases (a chapter merely mentioning characters is NOT front) — gated on
# the absence of chapter evidence. Includes the bare cast nouns and the in/of-the-play variants.
_FRONT_EXACT = (
    r"(?:the\s+)?dramatis\s+person(?:ae|æ)"
    r"|(?:the\s+)?persons?\s+(?:in|of)\s+the\s+(?:play|drama|story|novel)"
    r"|characters?\s+(?:in|of)\s+the\s+(?:play|drama|novel|story|book)"
    r"|(?:the\s+)?(?:characters?|persons?|players?)"
    r"|(?:the\s+)?cast(?:\s+of\s+characters)?"
    r"|about\s+the\s+(?:author|translator|edition)"
)
# LEADING: scholarly intros / translator material that naturally carry a trailing subtitle ("by X"). Kept
# front as the spoiler-safe direction; the CHLABEL gate protects "Introduction to Part Two", and a strip
# via this branch is FLAGGED by the segmenter so an over-strip of an in-story "Introduction to X" is
# auditable.
_FRONT_LEAD = (
    r"introduction\b|preface\b|foreword\b"
    r"|(?:an?\s+)?(?:prefatory|editorial|publisher['’]?s?|author['’]?s?)\s+(?:note|introduction|preface)"
    r"|(?:translator|editor)['’]?s?\s+(?:note|introduction|preface|foreword)"
    r"|(?:a\s+)?notes?\s+on\s+the\s+(?:text|translation|edition)"
)
FRONT_LEAD = re.compile(rf"^\s*(?:{_FRONT_LEAD})", re.I)            # the ambiguous (flagged) front branch
FRONT_PREFIX = re.compile(rf"^\s*(?:(?:{_FRONT_EXACT})\s*$|(?:{_FRONT_LEAD}))", re.I)
# Legacy back-matter (epub:type covers EPUB3). 'index' must be the WHOLE label (an "Index" page, not an
# "Index Finger" chapter). 'about the author' lives in FRONT_PREFIX only.
BACK_PREFIX = re.compile(
    r"^\s*(?:afterword|appendix|appendices|endnotes|rear\s*notes|glossary|bibliography|errata"
    r"|index\s*$)", re.I)
# EPUB3 epub:type vocabulary — authoritative front/back/body when present
EPUB_FRONT = {'titlepage', 'halftitlepage', 'imprint', 'dedication', 'epigraph', 'foreword',
              'preface', 'introduction', 'preamble', 'toc', 'cover',
              'dramatis-personae', 'z3998:dramatis-personae'}
EPUB_BACK = {'colophon', 'appendix', 'afterword', 'endnotes', 'rearnotes', 'loi'}
EPUB_BODY = {'chapter', 'part', 'division', 'volume', 'prologue', 'epilogue', 'scene', 'z3998:scene'}

# ADR 0007 D-A8 divider criterion: a label-only Part/Book/section divider, merged into the FOLLOWING
# chapter. Word-count is one discriminator (Karamazov's 20-word "PART I" is a divider; its 1800-word
# "Book II. An Unfortunate Gathering" is a real chapter), AND the WHOLE label must be a BARE structural
# enumerator (pass-1: matching merely the FIRST word over-merged "Book Learning"). pass-2: also accept
# the Victorian "Book the First" idiom, a trailing ':' / em-dash separator, and roman up to ~99.
DIVIDER_MAX_WORDS = 200
_ROMAN = r"(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"   # roman 1..~99 (uses only x/l so "Civil" is not a numeral)
_SPELLED = (r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
            r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth")
DIVIDER_LABEL = re.compile(
    rf"^\s*(part|book|volume|section|canto|stave|часть|книга|том)\b"
    rf"[\s.:—–-]*(the\s+)?(\d+|{_ROMAN}|{_SPELLED})?[.:]?\s*$",
    re.I)


def _has_chapter_evidence(label, heading):
    return bool(CHLABEL.search(label or '') or CHLABEL.search(heading or ''))


def _strong_cast(label, heading):
    return bool(FRONT_CAST_STRONG.match(label or '') or FRONT_CAST_STRONG.match(heading or ''))


def classify(href, text, heading, label, guide, is_nav, etoks):
    """Return 'front' | 'back' | 'body' for one spine doc, by explicit signals only."""
    # EPUB3 epub:type is the authoritative, fail-closed signal when present
    if 'frontmatter' in etoks:
        return 'front'
    if 'backmatter' in etoks:
        return 'back'
    if 'bodymatter' in etoks:
        return 'body'
    if etoks & EPUB_FRONT:
        return 'front'
    if etoks & EPUB_BACK:
        return 'back'
    if etoks & EPUB_BODY:
        return 'body'
    # --- legacy (no epub:type, e.g. EPUB2/NCX): heuristic signals ---
    base = posixpath.basename(href)
    if is_nav:
        return 'front'
    if href in guide:
        return 'front'
    if START_RE.search(text[:4000]):
        return 'front'
    # An unambiguous cast head is never a body chapter -> front BEFORE the chapter-evidence gate, so
    # "Dramatis Personae of Book I" / "Persons Represented in Act I" cannot leak (pass-2b).
    if _strong_cast(label, heading):
        return 'front'
    has_ch = _has_chapter_evidence(label, heading)
    if LICENSE_RE.search(text):
        return 'body' if has_ch else 'back'   # chapter + appended-license stays a chapter
    if FRONTNAME.search(base):
        return 'front'
    # The ambiguous label/heading front/back heuristics are GATED on the ABSENCE of chapter evidence: a
    # doc whose label carries a chapter keyword/enumerator is a CHAPTER and is never stripped.
    if not has_ch:
        if (FRONTLABEL.match(label or '') or FRONTLABEL.match(heading or '')
                or FRONT_PREFIX.match(label or '') or FRONT_PREFIX.match(heading or '')):
            return 'front'
        if BACK_PREFIX.match(label or '') or BACK_PREFIX.match(heading or ''):
            return 'back'
    return 'body'


def is_divider(words, label, heading):
    """A label-only Part/Book/section divider that must merge into the FOLLOWING chapter (D-A8): under
    the word ceiling AND carrying a BARE structural divider label (so a short *titled* chapter like
    "Book Learning" stays its own atom)."""
    if words >= DIVIDER_MAX_WORDS:
        return False
    return bool(DIVIDER_LABEL.match(label or '') or DIVIDER_LABEL.match(heading or ''))


def is_front_label(label, heading):
    """True if a ToC leaf label/heading reads as front-matter (used to drop a cast-list/intro anchor in
    anchor-driven mode, where there is no epub:type). An unambiguous cast head is always front; the
    ambiguous patterns are gated on the absence of chapter evidence (pass-2/2b)."""
    if _strong_cast(label, heading):
        return True
    if _has_chapter_evidence(label, heading):
        return False
    return bool(FRONTLABEL.match(label or '') or FRONT_PREFIX.match(label or '')
                or FRONTLABEL.match(heading or '') or FRONT_PREFIX.match(heading or ''))


def is_ambiguous_front(label, heading):
    """A front strip via the LEADING scholarly-intro branch (introduction/preface/foreword/note) — the
    one front signal that could be an in-story chapter — so the segmenter can FLAG it for audit."""
    return bool(FRONT_LEAD.match(label or '') or FRONT_LEAD.match(heading or ''))


def is_ambiguous_back(label, heading):
    """A back strip via the LEADING appendix/glossary/etc. branch — auditable like the front case."""
    if _has_chapter_evidence(label, heading):
        return False
    return bool(BACK_PREFIX.match(label or '') or BACK_PREFIX.match(heading or ''))
