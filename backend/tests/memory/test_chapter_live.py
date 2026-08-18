"""chapter_live (ADR 0007 D-A3) — the key-only guarded existence read the LIT-6 pipeline uses to fail
loud on a changed-content re-ingest (sibling of chapter_is_ingested). Read-only; ingestion fact, not a
story fact, so it is not a BookmarkView funnel read."""
from app.memory.store import Store


def test_chapter_live_reports_existence_independent_of_content_hash(tmp_path):
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        assert mem.chapter_live("b:c1.xhtml") is False                 # not yet ingested
        mem.add_chapter("b:c1.xhtml", revealed_at=1, href="c1.xhtml", content_hash="h1")
        assert mem.chapter_live("b:c1.xhtml") is True                  # live at h1
        assert mem.chapter_is_ingested("b:c1.xhtml", "DIFFERENT") is False   # not at this hash
        assert mem.chapter_live("b:c1.xhtml") is True                  # but still live (any hash)
        mem.retract_chapter("b:c1.xhtml")
        assert mem.chapter_live("b:c1.xhtml") is False                 # retracted -> not live
