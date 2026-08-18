"""Unicode-aware normalization and word iteration shared by identity and spoiler gates."""
from dataclasses import dataclass
import unicodedata


@dataclass(frozen=True)
class Word:
    text: str
    start: int
    end: int


def normalize_text(value):
    return " ".join(unicodedata.normalize("NFC", value or "").casefold().split())


def words(value):
    """Yield maximal Unicode letter/mark/number runs while preserving source spans and casing."""
    text = value or ""
    start = None
    for index, char in enumerate(text):
        is_word = unicodedata.category(char)[:1] in {"L", "M", "N"}
        if is_word and start is None:
            start = index
        elif not is_word and start is not None:
            yield Word(text[start:index], start, index)
            start = None
    if start is not None:
        yield Word(text[start:], start, len(text))


def word_tokens(value, *, min_length=1):
    return [normalize_text(word.text) for word in words(value)
            if len(word.text) >= min_length
            and any(unicodedata.category(char).startswith("L") for char in word.text)]


def is_proper_word(value):
    """Capitalized for cased scripts; any multi-character letter run for scripts without case."""
    letters = [char for char in value if unicodedata.category(char).startswith("L")]
    if not letters:
        return False
    cased = [char for char in letters if char.lower() != char.upper()]
    if cased:
        first = cased[0]
        return first.isupper() or first.istitle()
    return len(letters) >= 2


def proper_words(value):
    """Yield proper-name word parts, retaining lowercase parts of hyphen/apostrophe compounds."""
    text = value or ""
    previous = None
    compound_is_proper = False
    for word in words(text):
        separator = text[previous.end:word.start] if previous is not None else ""
        linked = bool(separator) and all(char in "-'’‐" for char in separator)
        current_is_proper = is_proper_word(word.text)
        if not linked:
            compound_is_proper = current_is_proper
        elif current_is_proper:
            compound_is_proper = True
        if current_is_proper or (linked and compound_is_proper):
            yield word
        previous = word
