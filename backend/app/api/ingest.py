"""Ingestion status route (ADR 0007 D-A11): marker-validated progress plus the worker's in-process
status/flags (blocked gates and legacy partial state surface here — never silently)."""
from fastapi import APIRouter, Depends, HTTPException

from app.deps import book_lifecycle, get_catalog, get_settings, get_store, get_worker
from app.ingest.manifest import AtomSetMismatch, assert_matches_store, load_manifest

router = APIRouter(prefix="/api/books/{book_id}", tags=["ingest"])


@router.get("/ingest", dependencies=[Depends(book_lifecycle)])
def ingest_status(book_id: str, catalog=Depends(get_catalog), worker=Depends(get_worker),
                  store=Depends(get_store), settings=Depends(get_settings)):
    st = catalog.get_state(book_id)
    if st is None:
        raise HTTPException(404, "unknown book")
    w = worker.status(book_id)
    try:
        manifest = load_manifest(settings.data_dir, book_id)
        with store.book(book_id) as mem:
            assert_matches_store(manifest, mem)
            durable = mem.completion_frontier(manifest["atoms"])
    except AtomSetMismatch as e:
        raise HTTPException(409, "atom-set mismatch (re-import the book)") from e
    return {"ingest_progress": min(st["ingest_progress"], durable),
            "status": w["status"],
            "flags": w.get("flags") or [], "error": w.get("error")}
