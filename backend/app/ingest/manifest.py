"""The per-book ATOM MANIFEST (ADR 0007 D-A10 made concrete). Written ONCE at import from the
segmentation result and immutable thereafter (the MVP has no re-segmentation path; a re-import is
rejected). It is the position layer's source of chapter bounds and the carrier of the
``atom_set_version`` every bookmark-taking route fail-closes on:

  * ``atom_set_version`` = sha256 over the ordered ``(ordinal, chapter_key, char_len)`` triples — any
    renumbering/re-split/length change produces a different version;
  * ``verify_manifest`` re-derives the hash on every load (a hand-edited/corrupt manifest fails loud);
  * the INGESTED-PREFIX cross-check (``assert_matches_store``) compares the store's live chapters
    against the manifest by ``(ordinal, chapter_key)`` — if the store was built from a different atom
    set than the manifest now describes, reads FAIL CLOSED (D-A10: never serve a bookmark derived
    against one numbering over facts stamped under another).

Lives beside ``source.epub`` under ``data/books/<book_id>/atoms.json``.
"""
import hashlib
import json
import os

from app.language import normalize_content_language


class AtomSetMismatch(RuntimeError):
    """The manifest fails self-verification or disagrees with the store — reads must FAIL CLOSED."""


def _version(atom_rows):
    blob = "|".join(f"{a['ordinal']}:{a['key']}:{a['char_len']}" for a in atom_rows)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def build_manifest(book_id, result, chapters):
    """From ``segment_for_ingest``'s (SegmentResult, chapters): the light, position-layer view of the
    atom set (no text — the worker re-segments the stored source.epub and cross-checks)."""
    atoms = [{"ordinal": ch["ordinal"], "key": ch["key"], "href": ch.get("href", ""),
              "title": ch.get("title", ""), "part_label": ch.get("part_label", ""),
              "char_len": len(ch.get("text", "") or "")} for ch in chapters]
    return {
        "book_id": book_id,
        "mode": result.mode,
        "flags": [str(f) for f in result.flags],
        "content_language": normalize_content_language(result.content_language),
        "atoms": atoms,
        "atom_set_version": _version(atoms),
    }


def manifest_path(data_dir, book_id):
    return os.path.join(data_dir, "books", book_id, "atoms.json")


def write_manifest(data_dir, manifest):
    path = manifest_path(data_dir, manifest["book_id"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)                                    # atomic on one filesystem
    return path


def load_manifest(data_dir, book_id):
    """Load + SELF-VERIFY (fail closed on a corrupt/edited manifest — D-A10)."""
    path = manifest_path(data_dir, book_id)
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError) as e:
        raise AtomSetMismatch(f"atom manifest for {book_id!r} is missing/unreadable") from e
    if m.get("atom_set_version") != _version(m.get("atoms", [])):
        raise AtomSetMismatch(f"atom manifest for {book_id!r} fails self-verification "
                              "(atom_set_version does not match its atoms)")
    # Pre-LIT-23 manifests have no language field at all. Preserve that provenance in-memory: an
    # absent field means the old importer made no claim, which is distinct from a current manifest
    # explicitly recording ``und`` after inspecting the EPUB.
    m["_content_language_recorded"] = "content_language" in m
    raw_language = m.get("content_language", "und")
    language = normalize_content_language(raw_language)
    if language != raw_language:
        raise AtomSetMismatch(f"atom manifest for {book_id!r} has malformed content language")
    m["content_language"] = language
    ords = [a["ordinal"] for a in m["atoms"]]
    if ords != list(range(1, len(ords) + 1)):
        raise AtomSetMismatch(f"atom manifest for {book_id!r} ordinals are not contiguous 1..N")
    return m


def bounds_of(manifest):
    """Frontier chapter bounds from the manifest's char lengths (revealed_at order)."""
    from app.reader import frontier
    lens = [a["char_len"] for a in manifest["atoms"]]
    b = frontier.chapter_bounds(lens)
    frontier.assert_aligned(b, [a["ordinal"] for a in manifest["atoms"]])
    return b


def assert_matches_store(manifest, mem):
    """D-A10 fail-closed cross-check, called UNDER the per-book lock: every live chapter the store has
    ingested must exist in the manifest at the SAME (ordinal, chapter_key). A mismatch means the store
    was built from a different atom set -> raise (the route 409s) rather than serve a leak."""
    want = {a["ordinal"]: a["key"] for a in manifest["atoms"]}
    if manifest.get("content_language", "und") != mem.content_language():
        raise AtomSetMismatch("store/manifest content language mismatch (D-A10 fail-closed)")
    for ch in mem.view(len(want)).chapters():                # funnel read; covers all manifest ordinals
        if want.get(ch["revealed_at"]) != ch["chapter_key"]:
            raise AtomSetMismatch(
                f"store/manifest atom mismatch at ordinal {ch['revealed_at']}: the store was built "
                "from a different atom set than the manifest describes (D-A10 fail-closed)")
