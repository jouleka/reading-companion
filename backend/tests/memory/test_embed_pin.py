"""Embed-pin enforcement (ADR 0005 / ADR 0007 Inv 6) in the production DAL: once a book pins an
embedding model, a chunk in a DIFFERENT space (model or dim) is REJECTED — so KNN never cosine-compares
across embedding spaces. Plus the late-pin guard: pinning is refused if unstamped chunks already exist.

NB: the back-compat UNPINNED path is retained for the offline stub / CI (ADR 0007 D-A5). The production
"always pin before the first real chunk" guarantee is the ingestion pipeline's responsibility (wired
with LIT-6), not a hard DAL rejection — else the stub eval path would break. These tests cover the
SAFETY property (a mismatched vector cannot enter a pinned book).
"""
import pytest

from app.memory.store import Store


def test_pinned_book_rejects_cross_space_chunk(tmp_path):
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        mem.add_chapter("b:c1.xhtml", revealed_at=1, href="c1.xhtml", content_hash="h1")
        mem.pin_models(embed_model="openai-compatible@x:text-embedding-3-small", embed_dim=3,
                       embed_canary=[1.0, 0.0, 0.0])
        # matching model + dim -> accepted
        mem.add_chunk("b:c1.xhtml", 1, "ok", [0.1, 0.2, 0.3],
                      embed_model="openai-compatible@x:text-embedding-3-small", embed_dim=3)
        # wrong MODEL -> rejected
        with pytest.raises(ValueError):
            mem.add_chunk("b:c1.xhtml", 1, "bad-model", [0.1, 0.2, 0.3],
                          embed_model="other:model", embed_dim=3)
        # wrong DIM -> rejected
        with pytest.raises(ValueError):
            mem.add_chunk("b:c1.xhtml", 1, "bad-dim", [0.1, 0.2],
                          embed_model="openai-compatible@x:text-embedding-3-small", embed_dim=2)


def test_pin_refused_when_unstamped_chunks_exist(tmp_path):
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        mem.add_chapter("b:c1.xhtml", revealed_at=1, href="c1.xhtml", content_hash="h1")
        mem.add_chunk("b:c1.xhtml", 1, "unstamped", [0.1, 0.2, 0.3])   # unpinned -> embed_model NULL
        with pytest.raises(ValueError):
            mem.pin_models(embed_model="openai-compatible@x:text-embedding-3-small", embed_dim=3,
                           embed_canary=[1.0, 0.0, 0.0])


def test_pinned_search_is_same_space_only(tmp_path):
    """A pinned book's search compares only same-(model,dim) chunks; a query in another space returns
    nothing rather than cross-space garbage."""
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        mem.add_chapter("b:c1.xhtml", revealed_at=1, href="c1.xhtml", content_hash="h1")
        mem.pin_models(embed_model="m1", embed_dim=3, embed_canary=[1.0, 0.0, 0.0])
        mem.add_chunk("b:c1.xhtml", 1, "in-space", [1.0, 0.0, 0.0], embed_model="m1", embed_dim=3)
        same = mem.view(1).search([1.0, 0.0, 0.0], k=3)               # default = pinned model
        assert [h[1] for h in same] == ["in-space"]
        cross = mem.view(1).search([1.0, 0.0, 0.0], k=3, embed_model="other-model")
        assert cross == []                                            # cross-space query -> nothing


def test_repin_refused_while_old_space_chunks_live(tmp_path):
    """repin_embedding must fail loud if live chunks under the OLD model remain (else default same-space
    search would silently empty the book's RAG). Correct order: retract old -> repin -> re-embed."""
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        mem.add_chapter("b:c1.xhtml", revealed_at=1, href="c1.xhtml", content_hash="h1")
        mem.pin_models(embed_model="m1", embed_dim=3, embed_canary=[1.0, 0.0, 0.0])
        mem.add_chunk("b:c1.xhtml", 1, "old-space", [1.0, 0.0, 0.0], embed_model="m1", embed_dim=3)
        with pytest.raises(ValueError):                              # m1 chunks still live -> refuse
            mem.repin_embedding("m2", 3, embed_canary=[0.0, 1.0, 0.0])
        mem.retract_chapter("b:c1.xhtml")                            # retract old-space chunk first
        mem.repin_embedding("m2", 3, embed_canary=[0.0, 1.0, 0.0])   # now safe
        assert mem.pinned_identity()["embed_model"] == "m2"
