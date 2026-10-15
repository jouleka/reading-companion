"""One bounded query/canary embedding batch checked against a book's stored model space."""
from dataclasses import replace

from app.cost.limits import budgeted_embedding
from app.llm import versioning


def budgeted_query_embedding(catalog, settings, client, book_id, *, pinned, texts,
                             phase="search-embedding"):
    """Run outside Store.book(); caller snapshots the pin under its short database session.

    A configuration mismatch fails before provider IO. The fixed canary shares the query's one
    provider request, so a same-name space change cannot silently rank old vectors. Its real usage
    remains included in the reservation, ledger, and returned cost.
    """
    if not pinned or not pinned.get("embed_model"):
        raise versioning.ModelMigrationRequired([("embed_model", versioning.FORCE_RE_EMBED)])
    versioning.assert_compatible_config(pinned, client, check_extractor=False)
    versioning.assert_compatible_identity(pinned, pinned, check_extractor=False)
    result = budgeted_embedding(
        catalog, settings, client, book_id, phase=phase,
        texts=[*texts, versioning.EMBED_CANARY_TEXT],
    )
    vectors = result.value
    if len(vectors) != len(texts) + 1:
        raise versioning.ModelMigrationRequired([("embed_model", versioning.FORCE_RE_EMBED)])
    current = {
        "embed_model": client.embed_identity(), "embed_dim": len(vectors[-1]),
        "embed_canary": vectors[-1],
    }
    versioning.assert_compatible_identity(pinned, current, check_extractor=False)
    if any(len(vector) != pinned["embed_dim"] for vector in vectors[:-1]):
        raise versioning.ModelMigrationRequired([("embed_model", versioning.FORCE_RE_EMBED)])
    return replace(result, value=vectors[:-1])
