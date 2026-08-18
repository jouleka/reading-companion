"""Vector primitives shared by the reference ranker and the sqlite-vec production backend.

``rank`` remains deliberately DB-free: it is the exact cosine reference implementation used for
fallback, parity tests, and final scoring. The DAL owns sqlite-vec loading, index synchronization, and
the spoiler-safe SQL prefilter because only the DAL may engage the fact-table authorizer.

sqlite-vec 0.1.9 performs exact KNN within metadata constraints; those constraints prevent spoiler
post-filtering, but this release does not provide a sublinear ANN performance claim.
"""

import json
import math


VEC0_TABLE = "chunks_vec"
VEC0_SHADOW_TABLES = {
    "chunks_vec_info",
    "chunks_vec_chunks",
    "chunks_vec_rowids",
    "chunks_vec_vector_chunks00",
    "chunks_vec_metadatachunks00",
    "chunks_vec_metadatachunks01",
    "chunks_vec_metadatachunks02",
    "chunks_vec_metadatachunks03",
    "chunks_vec_metadatachunks04",
    "chunks_vec_metadatachunks05",
    "chunks_vec_metadatachunks06",
    "chunks_vec_metadatatext00",
    "chunks_vec_metadatatext01",
    "chunks_vec_metadatatext06",
}
VEC0_COLUMNS = (
    "rowid",
    "embedding",
    "book_id",
    "chapter_key",
    "revealed_at",
    "retracted",
    "chapter_revealed_at",
    "chapter_retracted",
    "embed_model",
)
VEC0_INDEX_SCHEMA_VERSION = 1
FLOAT32_TIE_EPS = 1e-4
NULL_MODEL = ""


def load_extension(connection):
    """Load the bundled sqlite-vec extension without leaving extension loading enabled."""
    try:
        import sqlite_vec
    except ImportError as exc:  # pragma: no cover - dependency is mandatory in packaged installs
        raise RuntimeError("sqlite-vec is required for the configured vector backend") from exc
    try:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
    except Exception as exc:
        raise RuntimeError("sqlite-vec extension could not be loaded") from exc
    finally:
        try:
            connection.enable_load_extension(False)
        except AttributeError:  # pragma: no cover - unsupported sqlite build already failed above
            pass
    return connection.execute("SELECT vec_version()").fetchone()[0]


def normalize(value):
    """Return a finite unit vector. L2 over unit vectors has the same ordering as cosine."""
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("embedding must be a non-empty vector")
    try:
        result = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise ValueError("embedding values must be numeric") from exc
    if not all(math.isfinite(item) for item in result):
        raise ValueError("embedding values must be finite")
    norm = math.sqrt(sum(item * item for item in result))
    if not norm or not math.isfinite(norm):
        raise ValueError("embedding must have a finite non-zero norm")
    return [item / norm for item in result]


def serialize(value):
    """Serialize a normalized vector in sqlite-vec's float32 wire format."""
    try:
        import sqlite_vec
    except ImportError as exc:  # pragma: no cover - dependency is mandatory in packaged installs
        raise RuntimeError("sqlite-vec is required for vector serialization") from exc
    return sqlite_vec.serialize_float32(normalize(value))


def create_table_sql(dimensions):
    if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions < 1:
        raise ValueError("vec0 dimensions must be a positive integer")
    return (
        f"CREATE VIRTUAL TABLE {VEC0_TABLE} USING vec0("
        f"embedding float[{dimensions}], "
        "book_id text, chapter_key text, revealed_at integer, retracted integer, "
        "chapter_revealed_at integer, chapter_retracted integer, embed_model text)"
    )


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def rank(rows, query_vec, k):
    """Rank already-FILTERED candidate rows by cosine similarity to query_vec, returning the top k as
    the proven 4-tuples (cosine, text, revealed_at, chapter_key).

    `rows`: an iterable of mappings carrying 'vec' (a JSON float[] string, as stored in chunks.vec),
    'text', 'revealed_at', 'chapter_key'. Owns the in-Python dim-mismatch skip (defense-in-depth),
    the cosine, the deterministic tie-break (cosine desc, then chapter_key desc), and the top-k
    truncation. It does NOT filter for spoilers — that is the caller's funnel responsibility."""
    try:
        nq = normalize(query_vec)
    except (ValueError, TypeError):
        return []
    scored = []
    for r in rows:
        try:
            v = json.loads(r["vec"])
            nv = normalize(v)
        except (ValueError, TypeError):
            continue  # corrupt vec -> skip one row, don't tear down RAG for the book
        if len(nv) != len(nq):
            continue  # not comparable -> skip (defense-in-depth)
        c = _cosine(nq, nv)
        if not math.isfinite(c):
            continue  # NaN/inf (corrupt vector) must never out-rank a valid result
        scored.append((c, r["text"], r["revealed_at"], r["chapter_key"]))
    scored.sort(reverse=True, key=lambda t: (t[0], t[3]))
    return scored[:k]
