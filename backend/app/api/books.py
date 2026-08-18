"""Shelf routes (ADR 0007 D-A11): list / import / delete / serve EPUB bytes.

IMPORT is the one place the atom set is authored: segment the upload, persist ``source.epub`` +
``atoms.json`` (the D-A10 atom-set manifest), create the per-book memory.db, then shelve in the
catalog LAST (the catalog row is the commit point — orphaned files without a row are harmless and a
retry overwrites them). ``book_id`` is content-derived (sha256 of the bytes), so a re-import of the
same file maps to the same id and the catalog's uniqueness makes it a 409 — never a silent renumber
of an existing store (D-A10)."""
import hashlib
import os

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.deps import book_lifecycle, get_catalog, get_settings, get_store, get_worker
from app.ingest.book_type import detect_book_type
from app.ingest.extraction.chapter_text import segment_for_ingest
from app.ingest.manifest import (
    AtomSetMismatch,
    assert_matches_store,
    build_manifest,
    load_manifest,
    write_manifest,
)
from app.ingest.segmentation.epub_segmenter import EpubDrmError

router = APIRouter(prefix="/api/books", tags=["books"])


@router.get("")
def list_books(catalog=Depends(get_catalog)):
    return [
        {"book_id": b["book_id"], "title": b["title"], "author": b["author"],
         "source": b["source"], "added_at": b["added_at"]}
        for b in catalog.list_books()
    ]


@router.post("", status_code=201)
def import_book(file: UploadFile, catalog=Depends(get_catalog), store=Depends(get_store),
                settings=Depends(get_settings)):
    # UploadFile is spooled, but an unbounded read still lets a hostile request allocate arbitrary
    # process memory. Read one byte past the policy limit so the boundary is deterministic.
    blob = file.file.read(settings.epub_max_upload_bytes + 1)
    if len(blob) > settings.epub_max_upload_bytes:
        raise HTTPException(413, "EPUB upload exceeds the configured size limit")
    if not blob:
        raise HTTPException(422, "empty upload")
    file_hash = hashlib.sha256(blob).hexdigest()
    book_id = "bk" + file_hash[:12]
    try:
        result, chapters = segment_for_ingest(blob, book_id)
    except EpubDrmError as e:
        raise HTTPException(422, "DRM-protected EPUBs are not supported; import a DRM-free EPUB") from e
    except Exception as e:                                   # corrupt zip/OPF fails LOUD upstream
        raise HTTPException(422, f"not a readable EPUB: {type(e).__name__}") from e
    if result.mode == "none" or not chapters:
        raise HTTPException(422, "no body chapters detected in this EPUB")
    profile = detect_book_type(result, chapters)

    title = result.title or (file.filename or book_id).rsplit(".", 1)[0]
    author = result.author
    book_dir = os.path.join(settings.data_dir, "books", book_id)
    os.makedirs(book_dir, exist_ok=True)
    with open(os.path.join(book_dir, "source.epub"), "wb") as f:
        f.write(blob)
    manifest = build_manifest(book_id, result, chapters)
    write_manifest(settings.data_dir, manifest)
    with store.book(
        book_id,
        meta=dict(title=title, author=author, source="upload", file_hash=file_hash),
    ) as mem:
        mem.set_book_profile(**profile.as_dict())            # advisory metadata; never changes atoms
        mem.set_content_language(result.content_language)
    try:
        catalog.add_book(
            book_id, title=title, author=author, source="upload", file_hash=file_hash,
        )
    except ValueError as e:                                  # same content already shelved
        raise HTTPException(409, f"book already imported as {book_id!r}") from e
    return {"book_id": book_id, "title": title, "author": author, "mode": result.mode,
            "atoms": len(manifest["atoms"]), "flags": manifest["flags"],
            "atom_set_version": manifest["atom_set_version"],
            "content_language": manifest["content_language"],
            "book_profile": profile.as_dict()}


@router.get("/{book_id}/manifest", dependencies=[Depends(book_lifecycle)])
def atom_manifest(book_id: str, catalog=Depends(get_catalog), store=Depends(get_store),
                  settings=Depends(get_settings)):
    """The reader's atom map (LIT-13 / ADR 0008): sections map to atoms by href so the client can
    compute the monotonic char offset (sum of prior char_len + fraction*current). Self-verified AND
    store-cross-checked, fail-closed like every manifest consumer (D-A10) — the label frontier below
    must never be applied over a different atom set than the bookmark was earned under. Like every
    bookmark route this CLAMPS server-side: title/part_label are content (a real chapter title can
    name a death) and are served only up to the chapter being read (bookmark+1); href/char_len are
    structural and stay complete. NB: title/part_label sit OUTSIDE the atom_set_version hash — the
    clamp, not the hash, is the label spoiler control."""
    if catalog.get_book(book_id) is None:
        raise HTTPException(404, "unknown book")
    try:
        m = load_manifest(settings.data_dir, book_id)
        with store.book(book_id) as mem:
            assert_matches_store(m, mem)
            profile = mem.book_profile()
            content_language = mem.content_language()
    except AtomSetMismatch as e:
        raise HTTPException(409, "atom-set mismatch (re-import the book)") from e
    # a missing reading_state row (hand-corrupted catalog) degrades to minimum visibility, never 500
    visible = (catalog.high_water(book_id) or 0) + 1
    return {"book_id": book_id, "atom_set_version": m["atom_set_version"], "mode": m["mode"],
            "book_profile": profile,
            "content_language": content_language,
            "atoms": [{"ordinal": a["ordinal"], "href": a["href"],
                       "title": a["title"] if a["ordinal"] <= visible else "",
                       "part_label": a["part_label"] if a["ordinal"] <= visible else "",
                       "char_len": a["char_len"]}
                      for a in m["atoms"]]}


@router.get("/{book_id}/epub", dependencies=[Depends(book_lifecycle)])
def epub_bytes(book_id: str, catalog=Depends(get_catalog), settings=Depends(get_settings)):
    """Streams the ORIGINAL epub — full text including unread chapters. Reader-parity BY DESIGN
    (ADR 0008): the client is the reading engine and needs the book's own prose; spoiler protection
    lives entirely in the DERIVED views/recap, which are server-clamped, never in withholding the
    book from its reader."""
    if catalog.get_book(book_id) is None:
        raise HTTPException(404, "unknown book")
    path = os.path.join(settings.data_dir, "books", book_id, "source.epub")
    if not os.path.exists(path):
        raise HTTPException(404, "source EPUB not on disk")
    return FileResponse(path, media_type="application/epub+zip")


@router.delete(
    "/{book_id}", status_code=204, dependencies=[Depends(book_lifecycle)]
)
def delete_book(book_id: str, request: Request, catalog=Depends(get_catalog),
                store=Depends(get_store), worker=Depends(get_worker)):
    """Remove from the shelf and invalidate process-local state.

    On-disk source/memory files remain preserved for LIT-24. LIT-33 closes the idle Store handle and
    drops segmentation/recap entries so a later re-import starts under its new catalog incarnation.
    """
    # The async dependency has acquired the request gate first; preserve request -> lifecycle order.
    with worker.book_lifecycle(book_id):
        if catalog.get_book(book_id) is None:
            raise HTTPException(404, "unknown book")
        catalog.remove_book(book_id)
        worker.invalidate_book(book_id)
        request.app.state.recaps.invalidate_book(book_id)
        store.evict(book_id)
