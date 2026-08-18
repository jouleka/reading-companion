"""Small, deterministic helpers for EPUB content-language metadata."""
import re


_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


def normalize_content_language(value):
    """Return a conservative, lowercase BCP-47-shaped tag, or ``und`` when absent/malformed."""
    if not isinstance(value, str):
        return "und"
    tag = value.strip().replace("_", "-")
    if len(tag) > 63 or not _LANGUAGE_TAG.fullmatch(tag):
        return "und"
    return tag.lower()


def is_english_content(value):
    return normalize_content_language(value).split("-", 1)[0] == "en"


def english_companion_contract(content_language):
    """Prompt suffix for non-English sources; English/unknown keeps historical prompts byte-stable."""
    language = normalize_content_language(content_language)
    if language == "und" or is_english_content(language):
        return ""
    return (
        f" The source language is {language}. Write summaries, events, relationships, states, and "
        "themes in English. Preserve proper names in their source spelling. Do not transliterate or "
        "translate names unless the text itself supplies an alias."
    )


def english_recap_contract(content_language):
    """Narrow generated-prose contract for recap calls over non-English source facts."""
    language = normalize_content_language(content_language)
    if language == "und" or is_english_content(language):
        return ""
    return (
        f" The source language is {language}. Write the companion prose in English while preserving "
        "proper names in their source spelling; do not transliterate or translate a name unless the "
        "supplied facts already provide that alias."
    )
