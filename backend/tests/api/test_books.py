"""Module E / shelf routes: import (segment -> manifest + source.epub + memory.db + catalog),
serve EPUB bytes, delete. Import is the ONLY place the atom set is authored: atoms.json carries the
atom_set_version the D-A10 version check keys on."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from _epub import epub_ncx, three_chapter_book
from app.config import Settings
from app.main import create_app


@pytest.fixture
def env(tmp_path):
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c, settings


def _import(c, blob=None, name="book.epub"):
    blob = blob or three_chapter_book()
    return c.post("/api/books", files={"file": (name, blob, "application/epub+zip")})


def test_import_shelves_the_book_with_manifest_and_store(env):
    c, settings = env
    r = _import(c)
    assert r.status_code == 201, r.text
    body = r.json()
    bid = body["book_id"]
    assert body["atoms"] == 3 and body["mode"] == "file-driven"
    assert body["book_profile"]["book_type"] == "novel"
    # on the shelf
    shelf = c.get("/api/books").json()
    assert [b["book_id"] for b in shelf] == [bid]
    # manifest persisted with a version + one entry per atom, ordinals 1..N
    mpath = os.path.join(settings.data_dir, "books", bid, "atoms.json")
    with open(mpath, encoding="utf-8") as f:
        manifest = json.load(f)
    assert manifest["atom_set_version"]
    assert [a["ordinal"] for a in manifest["atoms"]] == [1, 2, 3]
    assert all(a["char_len"] > 0 for a in manifest["atoms"])
    # per-book store + source bytes exist
    assert os.path.exists(os.path.join(settings.data_dir, "books", bid, "memory.db"))
    assert os.path.exists(os.path.join(settings.data_dir, "books", bid, "source.epub"))


def test_import_uses_bounded_epub_title_and_author_instead_of_the_upload_filename(env):
    c, _settings = env
    blob = epub_ncx(
        [("c1.xhtml", "Chapter I", "Chapter I", "Alyosha returned home. " * 20)],
        title="The Brothers Karamazov",
        author="Fyodor Dostoyevsky",
    )

    imported = _import(c, blob=blob, name="source.epub")

    assert imported.status_code == 201, imported.text
    assert imported.json()["title"] == "The Brothers Karamazov"
    assert imported.json()["author"] == "Fyodor Dostoyevsky"
    shelf = c.get("/api/books").json()
    assert [(book["title"], book["author"]) for book in shelf] == [
        ("The Brothers Karamazov", "Fyodor Dostoyevsky")
    ]


def test_import_falls_back_to_upload_filename_when_epub_title_is_absent(env):
    c, _settings = env
    blob = epub_ncx(
        [("c1.xhtml", "Chapter I", "Chapter I", "Aldric arrived. " * 20)],
        title=None,
    )

    imported = _import(c, blob=blob, name="my-book.epub")

    assert imported.status_code == 201, imported.text
    assert imported.json()["title"] == "my-book"
    assert imported.json()["author"] is None


def test_import_detects_and_serves_a_non_novel_profile(env):
    c, _settings = env
    blob = epub_ncx([
        ("p1.xhtml", "Sonnet I", "Sonnet I", "Moon river silence wind light " * 20),
        ("p2.xhtml", "Sonnet II", "Sonnet II", "Stone bird evening rain " * 20),
        ("p3.xhtml", "Ode III", "Ode III", "Sea cloud breath dawn " * 20),
        ("p4.xhtml", "Canto IV", "Canto IV", "Field star shadow sleep " * 20),
    ])
    imported = _import(c, blob=blob, name="poems.epub")
    assert imported.status_code == 201, imported.text
    profile = imported.json()["book_profile"]
    assert profile["book_type"] == "poetry"
    assert profile["detector_version"]

    manifest = c.get(f"/api/books/{imported.json()['book_id']}/manifest").json()
    assert manifest["book_profile"] == profile
    assert "Sonnet II" not in str(profile), "profile metadata must not echo future headings"


def test_import_persists_and_serves_normalized_content_language(env):
    c, settings = env
    blob = epub_ncx(
        [("c1.xhtml", "Глава 1", "Глава 1", "Алёша вернулся домой. " * 20)],
        language="RU_ru",
    )
    imported = _import(c, blob=blob, name="roman.epub")
    assert imported.status_code == 201, imported.text
    bid = imported.json()["book_id"]
    assert imported.json()["content_language"] == "ru-ru"
    manifest = c.get(f"/api/books/{bid}/manifest").json()
    assert manifest["content_language"] == "ru-ru"
    with open(os.path.join(settings.data_dir, "books", bid, "atoms.json"), encoding="utf-8") as f:
        assert json.load(f)["content_language"] == "ru-ru"


def test_epub_bytes_round_trip(env):
    c, _ = env
    blob = three_chapter_book()
    bid = _import(c, blob).json()["book_id"]
    r = c.get(f"/api/books/{bid}/epub")
    assert r.status_code == 200
    assert r.content == blob                                 # byte-identical for the reader engine


def test_reimport_of_the_same_file_is_rejected(env):
    # D-A10: a re-import never silently renumbers an existing store -> 409
    c, _ = env
    blob = three_chapter_book()
    assert _import(c, blob).status_code == 201
    assert _import(c, blob).status_code == 409


def test_import_of_a_non_epub_is_rejected(env):
    c, _ = env
    r = _import(c, blob=b"this is not a zip at all")
    assert r.status_code == 422


def test_import_rejects_an_oversized_upload_before_parsing(tmp_path):
    settings = Settings(
        _env_file=None,
        allow_stub=True,
        data_dir=str(tmp_path / "data"),
        epub_max_upload_bytes=128,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        r = _import(c, blob=three_chapter_book())
        assert r.status_code == 413
        assert r.json()["detail"] == "EPUB upload exceeds the configured size limit"
        assert c.get("/api/books").json() == []
    assert not os.path.exists(os.path.join(settings.data_dir, "books"))


def test_import_stream_body_limit_does_not_require_content_length(tmp_path):
    settings = Settings(
        _env_file=None,
        allow_stub=True,
        data_dir=str(tmp_path / "data"),
        epub_max_upload_bytes=128,
    )
    app = create_app(settings)

    boundary = "lit11-boundary"

    def chunks():
        yield (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"book.epub\"\r\nContent-Type: application/epub+zip\r\n\r\n"
        ).encode()
        for _ in range(70):
            yield b"x" * 1024
        yield f"\r\n--{boundary}--\r\n".encode()

    with TestClient(app) as c:
        r = c.post(
            "/api/books",
            content=chunks(),
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
        assert r.status_code == 413
        assert r.json()["detail"] == "EPUB upload exceeds the configured size limit"


def test_import_explains_the_drm_free_boundary(env):
    c, _ = env
    blob = bytearray(three_chapter_book())
    central = blob.find(b"PK\x01\x02")
    assert central >= 0
    flags = int.from_bytes(blob[central + 8:central + 10], "little") | 0x1
    blob[central + 8:central + 10] = flags.to_bytes(2, "little")
    r = _import(c, blob=bytes(blob))
    assert r.status_code == 422
    assert r.json()["detail"] == (
        "DRM-protected EPUBs are not supported; import a DRM-free EPUB"
    )


def test_delete_removes_from_shelf(env):
    c, _ = env
    bid = _import(c).json()["book_id"]
    assert c.delete(f"/api/books/{bid}").status_code == 204
    assert c.get("/api/books").json() == []
    assert c.get(f"/api/books/{bid}/epub").status_code == 404
    assert c.delete(f"/api/books/{bid}").status_code == 404


def test_delete_invalidates_process_caches_and_reimport_uses_new_incarnation(tmp_path):
    settings = Settings(
        _env_file=None,
        allow_stub=True,
        data_dir=str(tmp_path / "data"),
        store_max_handles=2,
        segmentation_cache_max_entries=2,
        recap_cache_max_entries=2,
        recap_failure_max_entries=2,
        recap_max_inflight=1,
    )
    app = create_app(settings)
    blob = three_chapter_book()
    with TestClient(app) as c:
        bid = _import(c, blob).json()["book_id"]
        old_incarnation = app.state.catalog.get_book(bid)["incarnation"]
        app.state.worker._segmented(bid, old_incarnation)
        app.state.recaps.set((bid, old_incarnation, "old"), {"recap": "stale"})
        app.state.recaps.mark_failed((bid, old_incarnation, "failed"))
        assert bid in app.state.store._handles
        assert app.state.worker.segmentation_cache_size() == 1

        assert c.delete(f"/api/books/{bid}").status_code == 204
        assert bid not in app.state.store._handles
        assert app.state.worker.segmentation_cache_size() == 0
        assert app.state.recaps.stats() == {"entries": 0, "failures": 0, "flights": 0}

        assert _import(c, blob).status_code == 201
        new_incarnation = app.state.catalog.get_book(bid)["incarnation"]
        assert new_incarnation != old_incarnation
        app.state.worker._segmented(bid, new_incarnation)
        assert list(app.state.worker._segcache) == [(bid, new_incarnation)]


def test_manifest_labels_are_clamped_to_the_frontier(env):
    """Spoiler-safe BY CONSTRUCTION: chapter titles are content (a real title can name a death), so
    the manifest serves title/part_label only up to the chapter currently being read (bookmark+1).
    href/char_len stay complete for every atom — they are structural and the offset map needs them."""
    c, settings = env
    bid = _import(c).json()["book_id"]
    body = c.get(f"/api/books/{bid}/manifest").json()
    # fresh book (bookmark 0): only the chapter being read (atom 1) shows its labels
    assert body["atoms"][0]["title"] == "Chapter I"
    assert body["atoms"][1]["title"] == "" and body["atoms"][1]["part_label"] == ""
    assert body["atoms"][2]["title"] == "" and body["atoms"][2]["part_label"] == ""
    assert all(a["href"] and a["char_len"] > 0 for a in body["atoms"])
    # complete chapter 1 -> chapter 2's label becomes visible; chapter 3 stays clamped
    mpath = os.path.join(settings.data_dir, "books", bid, "atoms.json")
    with open(mpath, encoding="utf-8") as f:
        ch1_len = json.load(f)["atoms"][0]["char_len"]
    c.put(f"/api/books/{bid}/position", json={"cfi": "x", "offset": ch1_len})
    body = c.get(f"/api/books/{bid}/manifest").json()
    assert body["atoms"][1]["title"] == "Chapter II"
    assert body["atoms"][2]["title"] == ""


class _Inline:
    """Deterministic same-thread executor (the test_ingest pattern)."""

    def submit(self, fn, *a, **kw):
        fn(*a, **kw)

    def shutdown(self, wait=True):
        pass


def test_manifest_route_cross_checks_the_store(tmp_path):
    """Pass-2 finding: /manifest was the ONE manifest consumer skipping the D-A10 store cross-check.
    A self-consistently REWRITTEN atoms.json (keys swapped, version recomputed — self-verification
    passes) must 409 once any chapter is ingested: a label frontier earned under one numbering must
    never be applied over a different atom set."""
    from app.ingest.manifest import _version
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=_Inline())
    with TestClient(app) as c:
        bid = _import(c).json()["book_id"]
        mpath = os.path.join(settings.data_dir, "books", bid, "atoms.json")
        with open(mpath, encoding="utf-8") as f:
            m = json.load(f)
        c.put(f"/api/books/{bid}/position",
              json={"cfi": "x", "offset": m["atoms"][0]["char_len"]})     # ch1 ingested inline
        m["atoms"][0]["key"], m["atoms"][1]["key"] = m["atoms"][1]["key"], m["atoms"][0]["key"]
        m["atom_set_version"] = _version(m["atoms"])                      # self-check now PASSES
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(m, f)
        assert c.get(f"/api/books/{bid}/manifest").status_code == 409


def test_manifest_survives_a_missing_reading_state_row(env):
    """A corrupt catalog (books row without its reading_state row) must not 500 the label clamp; it
    degrades to the minimum visibility (fail-SAFE for a spoiler system)."""
    import sqlite3
    c, settings = env
    bid = _import(c).json()["book_id"]
    con = sqlite3.connect(os.path.join(settings.data_dir, "catalog.db"))
    con.execute("DELETE FROM reading_state WHERE book_id=?", (bid,))
    con.commit()
    con.close()
    r = c.get(f"/api/books/{bid}/manifest")
    assert r.status_code == 200
    assert r.json()["atoms"][0]["title"] != "" and r.json()["atoms"][1]["title"] == ""


def test_epub_of_unknown_book_404s(env):
    c, _ = env
    assert c.get("/api/books/nope/epub").status_code == 404


def test_manifest_route_serves_the_atom_map(env):
    """LIT-13 (ADR 0008): the reader maps sections -> atoms via the import-time manifest to compute
    the monotonic char offset. Exposes atoms + atom_set_version; fail-closed like every manifest
    consumer."""
    import json
    import os
    c, settings = env
    bid = _import(c).json()["book_id"]
    r = c.get(f"/api/books/{bid}/manifest")
    assert r.status_code == 200
    body = r.json()
    assert body["atom_set_version"]
    assert [a["ordinal"] for a in body["atoms"]] == [1, 2, 3]
    assert all(set(a) >= {"ordinal", "href", "title", "part_label", "char_len"} for a in body["atoms"])
    assert c.get("/api/books/nope/manifest").status_code == 404
    # fail-closed on tamper (the D-A10 discipline applies here too)
    mpath = os.path.join(settings.data_dir, "books", bid, "atoms.json")
    with open(mpath, encoding="utf-8") as f:
        m = json.load(f)
    m["atoms"][0]["char_len"] = 1
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(m, f)
    assert c.get(f"/api/books/{bid}/manifest").status_code == 409
