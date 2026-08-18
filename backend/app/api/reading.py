"""Position routes (ADR 0007 D-A10 + LIT-12). The reader (LIT-13) reports its CFI plus the monotonic
char OFFSET (order-isomorphic to the CFI — the reader computes it from the CFI + chapter ranges); the
server derives the integer bookmark through the frontier over the import-time atom manifest and
persists it as a monotonic high-water (the catalog's SQL MAX — a backward page never lowers it,
matching ``max(cfi_to_bookmark(cfi), stored_bookmark)``). Every route here FAILS CLOSED on a corrupt /
store-divergent manifest (D-A10's version check made concrete)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictInt

from app.deps import book_lifecycle, get_catalog, get_settings, get_store, get_worker
from app.ingest.manifest import AtomSetMismatch, assert_matches_store, bounds_of, load_manifest
from app.reader import frontier

router = APIRouter(prefix="/api/books/{book_id}", tags=["reading"])


class PositionIn(BaseModel):
    # untrusted input: a real CFI is well under 2 KB — the cap stops unbounded strings from being
    # stored verbatim in reading_state and echoed back (the LIT-11 global body cap is separate)
    cfi: str = Field(max_length=4096)
    # StrictInt: a stringified/boolean/float offset is REJECTED, never coerced — a malformed offset
    # feeding the frontier could otherwise mis-derive the spoiler bookmark (fail-closed, D-A10).
    offset: StrictInt = Field(ge=0)
    # Epoch zero keeps pre-LIT-17 clients compatible until the first explicit reset. Thereafter a
    # missing epoch is deliberately stale, so an old tab cannot undo the user's new pass.
    position_epoch: StrictInt | None = Field(default=None, ge=0, le=2**63 - 1)


class PositionResetIn(BaseModel):
    position_epoch: StrictInt = Field(ge=0, le=2**63 - 1)


def _position_conflict(error: ValueError):
    if "position epoch" in str(error):
        raise HTTPException(
            409, "reading position was reset in another session; reload before continuing"
        ) from error
    raise error


def _load_checked(book_id, catalog, store, settings):
    """Shared fail-closed prologue: known book -> self-verified manifest -> store cross-check (under
    the lock) -> frontier bounds."""
    if catalog.get_book(book_id) is None:
        raise HTTPException(404, "unknown book")
    try:
        manifest = load_manifest(settings.data_dir, book_id)
        with store.book(book_id) as mem:
            assert_matches_store(manifest, mem)
            durable = mem.completion_frontier(manifest["atoms"])
        bounds = bounds_of(manifest)
    except AtomSetMismatch as e:
        raise HTTPException(409, "atom-set mismatch: the stored reading position cannot be safely "
                                 "interpreted (re-import the book)") from e
    return manifest, bounds, durable


@router.get("/position", dependencies=[Depends(book_lifecycle)])
def get_position(book_id: str, catalog=Depends(get_catalog), store=Depends(get_store),
                 settings=Depends(get_settings), worker=Depends(get_worker)):
    manifest, bounds, durable = _load_checked(book_id, catalog, store, settings)
    st = catalog.get_state(book_id)
    progress = min(st["ingest_progress"], durable)
    return {"bookmark": st["bookmark"], "cfi": st["cfi"], "ingest_progress": progress,
            "position_epoch": st["position_epoch"], "atoms": len(manifest["atoms"])}


@router.put("/position", dependencies=[Depends(book_lifecycle)])
def put_position(book_id: str, pos: PositionIn, catalog=Depends(get_catalog),
                 store=Depends(get_store), settings=Depends(get_settings),
                 worker=Depends(get_worker)):
    # One incarnation owns the whole transition: validation, position commit, and enqueue publication.
    # Deletion cannot cross between any of them and let this request bind work to a re-imported row.
    manifest, bounds, _durable = _load_checked(book_id, catalog, store, settings)
    prev = catalog.high_water(book_id)
    bookmark = frontier.bookmark_high_water(prev, pos.offset, bounds)
    try:
        state = catalog.set_position(
            book_id, pos.cfi, bookmark,
            expected_epoch=pos.position_epoch if pos.position_epoch is not None else 0,
        )
    except ValueError as error:
        _position_conflict(error)
    bookmark = state["bookmark"]  # SQL MAX may have observed a newer cross-instance report.
    if bookmark > 0:
        # The worker validates explicit v2 completion markers even when legacy catalog progress
        # already reaches this bookmark; matching durable receipts replay idempotently.
        worker.enqueue(book_id, bookmark)
    return {
        "bookmark": bookmark,
        "cfi": pos.cfi,
        "current_chapter": frontier.current_chapter(pos.offset, bounds),
        "chapter_progress": round(frontier.chapter_progress(pos.offset, bounds), 4),
        "position_epoch": state["position_epoch"],
        "atoms": len(manifest["atoms"]),
    }


@router.post("/position/reset", dependencies=[Depends(book_lifecycle)])
def reset_position(book_id: str, reset: PositionResetIn, catalog=Depends(get_catalog),
                   store=Depends(get_store), settings=Depends(get_settings)):
    """Start a new pass while retaining extracted memory, receipts, and spend history."""
    manifest, _bounds, durable = _load_checked(book_id, catalog, store, settings)
    try:
        state = catalog.reset_position(book_id, expected_epoch=reset.position_epoch)
    except ValueError as error:
        _position_conflict(error)
    return {
        "bookmark": state["bookmark"],
        "cfi": state["cfi"],
        "ingest_progress": min(state["ingest_progress"], durable),
        "position_epoch": state["position_epoch"],
        "atoms": len(manifest["atoms"]),
    }
