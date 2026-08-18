"""Module C public surface — the extraction package re-exports the pieces the ingestion worker / API
(later modules) consume, so they import from one place."""


def test_package_exports_the_public_surface():
    from app.ingest import extraction as X
    for name in ("Extraction", "EXTRACT_SYSTEM", "extract_user_prompt", "roster_for_prompt",
                 "resolve_one", "resolve_chapter", "chapter_texts", "segment_for_ingest",
                 "content_hash_of", "prepare_chapter", "ingest_chapter", "all_entities"):
        assert hasattr(X, name), f"missing public export: {name}"
