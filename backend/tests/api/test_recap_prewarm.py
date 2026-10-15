"""Background preparation must obey the same frontier, reset, cache and deletion rules."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from _epub import three_chapter_book
from test_ingest import InlineExecutor
from app.config import Settings
from app.main import create_app


def test_prewarm_is_cached_and_never_reuses_a_future_or_deleted_book(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, allow_stub=True, recap_prewarm=True,
                        data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    with TestClient(app) as api:
        bid = api.post("/api/books", files={"file": (
            "test.epub", three_chapter_book(), "application/epub+zip")}).json()["book_id"]
        atoms = json.loads((Path(settings.data_dir)/"books"/bid/"atoms.json").read_text())["atoms"]
        incarnation = app.state.catalog.get_book(bid)["incarnation"]
        api.put(f"/api/books/{bid}/position", json={"cfi": "x", "position_epoch": 0,
                                                  "offset": sum(a["char_len"] for a in atoms[:2])+2})
        warmer = app.state.recap_warmer
        api.portal.call(warmer.queue.join)
        recap = api.get(f"/api/books/{bid}/catch-me-up", params={"bookmark": 999})
        assert recap.status_code == 200 and recap.json()["cached"] is True
        assert recap.json()["as_of_chapter"] == 2 and "Corvus" not in recap.text
        original = app.state.client.complete
        calls = []
        def count(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)
        monkeypatch.setattr(app.state.client, "complete", count)
        api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0})
        warmer.notify(bid, incarnation)
        api.portal.call(warmer.queue.join)
        zero = api.get(f"/api/books/{bid}/catch-me-up", params={"bookmark": 999}).json()
        assert zero["as_of_chapter"] == 0 and zero["recap"] is None and not calls
        assert api.delete(f"/api/books/{bid}").status_code == 204
        warmer.notify(bid, incarnation)
        api.portal.call(warmer.queue.join)
        assert not calls and app.state.catalog.get_book(bid) is None
        assert not warmer.pending
