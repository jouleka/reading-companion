"""Module E / position routes (ADR 0007 D-A10 + LIT-12): the reader reports (cfi, offset); the server
derives the INTEGER bookmark through the frontier over the import-time atom manifest, persists it as a
monotonic high-water (SQL MAX — never regresses), and FAILS CLOSED when the manifest is corrupt or
disagrees with the store."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from _epub import three_chapter_book
from app.config import Settings
from app.main import create_app


@pytest.fixture
def env(tmp_path):
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings)
    with TestClient(app) as c:
        bid = c.post("/api/books",
                     files={"file": ("b.epub", three_chapter_book(), "application/epub+zip")}
                     ).json()["book_id"]
        yield c, settings, bid


def _manifest(settings, bid):
    with open(os.path.join(settings.data_dir, "books", bid, "atoms.json"), encoding="utf-8") as f:
        return json.load(f)


def test_initial_position_is_zero(env):
    c, _, bid = env
    r = c.get(f"/api/books/{bid}/position")
    assert r.status_code == 200
    body = r.json()
    assert body["bookmark"] == 0 and body["cfi"] is None and body["ingest_progress"] == 0
    assert body["position_epoch"] == 0
    assert body["atoms"] == 3


def test_mid_chapter_position_keeps_the_chapter_pending(env):
    # LIT-12: the in-progress chapter is NOT counted — bookmark = chapters FULLY completed
    c, settings, bid = env
    ch1_len = _manifest(settings, bid)["atoms"][0]["char_len"]
    r = c.put(f"/api/books/{bid}/position", json={"cfi": "epubcfi(/6/2!/4/2)", "offset": ch1_len // 2})
    assert r.status_code == 200
    body = r.json()
    assert body["bookmark"] == 0                    # ch1 only half read -> nothing revealed
    assert body["current_chapter"] == 1
    assert 0.4 < body["chapter_progress"] < 0.6


def test_completing_a_chapter_advances_the_bookmark(env):
    c, settings, bid = env
    ch1_len = _manifest(settings, bid)["atoms"][0]["char_len"]
    body = c.put(f"/api/books/{bid}/position",
                 json={"cfi": "epubcfi(x)", "offset": ch1_len + 5}).json()
    assert body["bookmark"] == 1 and body["current_chapter"] == 2


def test_bookmark_is_monotonic_but_cfi_follows_the_reader(env):
    # D-A10: backward paging NEVER lowers the persisted high-water; cfi is the latest position
    c, settings, bid = env
    ch1_len = _manifest(settings, bid)["atoms"][0]["char_len"]
    c.put(f"/api/books/{bid}/position", json={"cfi": "cfi-far", "offset": ch1_len + 5})
    body = c.put(f"/api/books/{bid}/position", json={"cfi": "cfi-back", "offset": 3}).json()
    assert body["bookmark"] == 1                    # high-water kept
    st = c.get(f"/api/books/{bid}/position").json()
    assert st["bookmark"] == 1 and st["cfi"] == "cfi-back"


def test_position_at_book_end_reveals_everything(env):
    c, settings, bid = env
    total = sum(a["char_len"] for a in _manifest(settings, bid)["atoms"])
    body = c.put(f"/api/books/{bid}/position", json={"cfi": "end", "offset": total}).json()
    assert body["bookmark"] == 3


def test_invalid_offsets_fail_closed(env):
    c, _, bid = env
    for bad in (-1, "abc", 1.5, None, True):
        r = c.put(f"/api/books/{bid}/position", json={"cfi": "x", "offset": bad})
        assert r.status_code == 422, f"offset {bad!r} must be rejected, got {r.status_code}"


def test_oversized_cfi_is_rejected(env):
    """The cfi is untrusted client input: unbounded it is a storage-bloat/DoS vector (stored verbatim
    in reading_state and echoed back). A real CFI is well under 2 KB — cap far above that."""
    c, _, bid = env
    r = c.put(f"/api/books/{bid}/position", json={"cfi": "x" * (4096 + 1), "offset": 0})
    assert r.status_code == 422
    r = c.put(f"/api/books/{bid}/position", json={"cfi": "epubcfi(" + "x" * 2000 + ")", "offset": 0})
    assert r.status_code == 200                     # a generous real-world CFI still fits


def test_unknown_book_404s(env):
    c, _, _ = env
    assert c.get("/api/books/nope/position").status_code == 404
    assert c.put("/api/books/nope/position", json={"cfi": "x", "offset": 1}).status_code == 404


def test_corrupt_manifest_fails_closed(env):
    """The D-A10 forced-mismatch test: tamper with the manifest (changing what the bookmark would be
    derived against) -> every position route FAILS CLOSED (409), never serves a leaky derivation."""
    c, settings, bid = env
    path = os.path.join(settings.data_dir, "books", bid, "atoms.json")
    with open(path, encoding="utf-8") as f:
        m = json.load(f)
    m["atoms"][0]["char_len"] = 1                     # tamper: bounds change, version now stale
    with open(path, "w", encoding="utf-8") as f:
        json.dump(m, f)
    assert c.get(f"/api/books/{bid}/position").status_code == 409
    assert c.put(f"/api/books/{bid}/position", json={"cfi": "x", "offset": 10}).status_code == 409


def test_starting_a_new_pass_rewinds_frontier_and_rejects_stale_position_reports(env):
    c, settings, bid = env
    atoms = _manifest(settings, bid)["atoms"]
    through_two = atoms[0]["char_len"] + atoms[1]["char_len"] + 2
    advanced = c.put(
        f"/api/books/{bid}/position",
        json={"cfi": "old-pass", "offset": through_two, "position_epoch": 0},
    ).json()
    assert advanced["bookmark"] == 2 and advanced["position_epoch"] == 0

    reset = c.post(
        f"/api/books/{bid}/position/reset",
        json={"position_epoch": 0},
    )
    assert reset.status_code == 200
    assert reset.json()["bookmark"] == 0
    assert reset.json()["cfi"] is None
    assert reset.json()["position_epoch"] == 1

    stale = c.put(
        f"/api/books/{bid}/position",
        json={"cfi": "late-old-tab", "offset": through_two, "position_epoch": 0},
    )
    assert stale.status_code == 409
    assert "reset" in stale.json()["detail"]
    assert c.put(
        f"/api/books/{bid}/position",
        json={"cfi": "legacy-old-tab", "offset": through_two},
    ).status_code == 409
    assert c.get(f"/api/books/{bid}/position").json()["bookmark"] == 0

    reread = c.put(
        f"/api/books/{bid}/position",
        json={"cfi": "new-pass", "offset": atoms[0]["char_len"] + 2, "position_epoch": 1},
    )
    assert reread.status_code == 200
    assert reread.json()["bookmark"] == 1


def test_reset_restores_manifest_label_clamp_without_rewriting_atoms(env):
    c, settings, bid = env
    atoms = _manifest(settings, bid)["atoms"]
    total = sum(atom["char_len"] for atom in atoms)
    c.put(
        f"/api/books/{bid}/position",
        json={"cfi": "end", "offset": total, "position_epoch": 0},
    )
    assert all(atom["title"] for atom in c.get(f"/api/books/{bid}/manifest").json()["atoms"])

    c.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0})
    reset_manifest = c.get(f"/api/books/{bid}/manifest").json()["atoms"]
    assert reset_manifest[0]["title"]
    assert all(not atom["title"] for atom in reset_manifest[1:])
    assert [atom["char_len"] for atom in reset_manifest] == [atom["char_len"] for atom in atoms]


def test_position_epoch_rejects_non_integer_and_sqlite_overflow_values(env):
    c, _, bid = env
    for epoch in (True, 2**63):
        response = c.post(
            f"/api/books/{bid}/position/reset", json={"position_epoch": epoch}
        )
        assert response.status_code == 422
