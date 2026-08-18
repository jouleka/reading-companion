# ADR 0016 — Non-English source books use Unicode-safe identity and an English companion contract

**Status:** **Accepted** (2026-07-14; LIT-23)

## Context

The reader already renders EPUB Unicode text, and real embedding providers accept Unicode, but several
spoiler-critical paths assumed Latin words: entity normalization removed selected diacritics, future-name
checks used `[A-Za-zÀ-ÿ]`, and legacy EPUB2 structural signals were English-only. The OPF
`dc:language` value was discarded, so extraction and recap generation could not state a stable output
language or preserve source-name spelling deliberately.

This is a safety issue as well as an internationalization issue. A future Cyrillic, Greek, or CJK name
that is invisible to deterministic tokenization can pass the recap gate. Conversely, guessing language
or transliterating names can create identity drift and misleading companion data.

## Decision

1. **Persist declared source language, never guess it.** Import normalizes the first OPF
   `dc:language` to a conservative lowercase BCP-47-shaped tag (`RU_ru` → `ru-ru`). Missing or malformed
   metadata becomes the explicit value `und`. The value is stored in schema v5, included in
   `atoms.json`, served by import/manifest APIs, and cross-checked among a freshly segmented source,
   the immutable manifest, and `memory.db`.
2. **Unicode is the identity/tokenization baseline.** Identity normalization is NFC plus Unicode
   case-folding and whitespace normalization. It no longer strips `ï` or `ü`, so canonically equivalent
   spellings merge while distinct diacritics remain distinct. Spoiler, grounding, and event-binding
   tokenizers consume Unicode letter/mark runs. Cased scripts retain capitalization-based proper-name
   rules; multi-letter runs in scripts without case are treated as name material in the deterministic
   blocklist (the safe failure direction is over-block/regenerate).
3. **Legacy front-matter support is conservative.** Modern EPUB3 `epub:type` remains authoritative.
   The EPUB2 fallback recognizes bounded Russian and Chinese/Japanese cast-list heads plus common
   Russian/CJK chapter/divider labels; it does not use language as authority to discard arbitrary
   pages. Weak or unknown structure remains body content rather than destructive loss.
4. **The application language remains English.** For a declared non-English source, extraction writes
   summaries/events/relationships/states/themes in English and preserves proper names in source spelling.
   Recap/“right now” systems use a narrower English-companion-prose version of the same contract.
   English and `und` sources retain byte-identical historical prompts and recap cache identities;
   declared non-English languages are part of recap prompt/cache identity.
5. **Embeddings remain provider-native.** Source strings pass through unchanged Unicode to the selected
   embedding backend. The lexical offline stub also retains Unicode alphanumeric material; no
   transliteration layer or separate embedding space is introduced.
6. **Recovery is additive.** Schema v5 adds `book_meta.content_language NOT NULL DEFAULT 'und'`.
   Existing v4 stores migrate to `und`; full portable archives from v3/v4 and legacy v2 archives are
   reconstructed at their declared shape and forward-migrated only in restore staging.

## Safety and integrity consequences

- Book language is advisory context, never a spoiler authority. It cannot change atoms, bookmark
  projection, completion receipts, visibility, referential closure, grounding thresholds, or judge
  behavior.
- Non-Latin canonical names and aliases participate in the same future-entity blocklist as Latin names.
- The language field is outside the atom hash because it does not change chapter geometry, but
  manifest/store/source disagreement fails closed before ingestion or serving derived views.
- Existing English/Karamazov prompt bytes and caches do not churn.

## Explicit limits

- This does not translate the React shell or provide user-selectable UI/output locales.
- `und` is not inferred from script; a non-English EPUB with missing/malformed language metadata keeps
  historical prompt behavior.
- The legacy EPUB2 vocabulary is intentionally bounded, not a general multilingual front/back-matter
  classifier. EPUB3 semantic types remain the reliable cross-language mechanism.
- Deterministic generated-prose event/tense grammars remain English, matching the required English
  companion output. Arbitrary-language generated recaps are therefore out of scope.
- CJK text without spaces can make reader-parity evidence under-inclusive; that over-blocks a generated
  name rather than permitting a future-name leak.

## Validation

Synthetic EPUB/API tests cover normalized/malformed language tags, Russian cast/part/chapter structure,
schema v4 migration, source/manifest/store persistence, and v2–v4 portable recovery. Identity and gate
tests cover NFC-equivalent names, preserved diacritics, multilingual role epithets, and Cyrillic/Greek/CJK
future names. Frontend tests cover clickable names from cased and uncased scripts, and the embedding stub
is pinned to retain distinct non-Latin inputs. No provider call or production-store mutation is required.
