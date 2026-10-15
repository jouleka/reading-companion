"""Exact bounded lexical neighbors and cross-chapter continuity; no model or book fixtures."""
from collections import Counter

import pytest

from app.ask import AskRetrievalLimit, retrieve_ask_sources
from app.eval.spoiler_gate.grounding import content_words
from app.retrieval_passages import normalize_source, passage_windows


def _chapters(raw, ordinals):
    return [{"chapter_key": key, "revealed_at": ordinals[key]} for key in raw]


def _atoms(raw):
    return {key: {"href": f"{key}.xhtml", "title": f"Section {key}"} for key in raw}


def _assert_exact_pool(sources, raw, ordinals, bookmark):
    assert len(sources) <= 24
    assert [source["id"] for source in sources] == list(range(1, len(sources) + 1))
    by_id = {source["id"]: source for source in sources}
    for source in sources:
        assert source["text"] in normalize_source(raw[source["chapter_key"]])
        assert 0 < len(source["text"]) <= 1500
        assert source["ordinal"] == ordinals[source["chapter_key"]] <= bookmark
        assert source["href"] == f"{source['chapter_key']}.xhtml"
        assert source["title"] == f"Section {source['chapter_key']}"
        for context_id in source["context_ids"]:
            assert context_id != source["id"]
            assert by_id[context_id]["chapter_key"] == source["chapter_key"]
            assert by_id[context_id]["ordinal"] == source["ordinal"]
        for context_id in source["continuity_ids"]:
            assert by_id[context_id]["ordinal"] == source["ordinal"] - 1
            assert normalize_source(raw[by_id[context_id]["chapter_key"]]).endswith(by_id[context_id]["text"])


def test_deep_lexical_answer_retains_its_zero_overlap_predecessor_despite_wrong_semantic_seeds():
    attribution = "The foreman rose from the jurors' bench and gave their decision."
    answer = 'Aster heard the verdict: "Acquitted."'
    target = "soft " * 840 + attribution + " still" * 210 + " " + answer + " soft" * 350
    raw = {f"d{index}": f"Aster waited beside the archive in district {index}. " * 45
           for index in range(1, 7)}
    raw.update({f"f{index}": f"Aster watched a lantern at post {index}." for index in range(10, 41)})
    raw["decision"] = target
    raw["future"] = "Aster heard an entirely different decision in the future."
    ordinals = {key: int(key[1:]) for key in raw if key[0] in {"d", "f"} and key[1:].isdigit()}
    ordinals.update(decision=93, future=94)
    hits = [(.95 - index / 100, next(passage_windows(raw[f"d{index}"]))[2], index, f"d{index}")
            for index in range(1, 7)]
    loaded = Counter()

    def read(key):
        assert key != "future", "future text must never be loaded to construct context"
        loaded[key] += 1
        return raw[key]

    question = "What verdict was pronounced for Aster?"
    sources = retrieve_ask_sources(
        question, bookmark=93, chapters=_chapters(raw, ordinals), read_chapter=read,
        semantic_hits=hits, atoms=_atoms(raw), candidate_pool=True, known_names=["Aster"],
    )
    direct = next(source for source in sources if answer in source["text"])
    before = [source for source in sources if attribution in source["text"]]
    assert answer not in target[:2000] and before
    assert all(attribution not in source["text"] for source in sources if answer in source["text"])
    assert any(source["id"] in direct["context_ids"] for source in before)
    assert any(not content_words(question) & content_words(source["text"]) for source in before)
    assert all(loaded[key] == 1 for key in raw if key != "future")
    assert len(sources) == 24, "context must survive a full pool before lower-relevance fill"
    _assert_exact_pool(sources, raw, ordinals, 93)


def test_chapter_opening_links_only_to_an_exact_retained_immediately_prior_tail_without_rereads():
    raw = {
        "opening": "Her household called her the youngest child. " + "An ordinary morning. " * 100,
        "prior": "A quiet corridor. " * 350 + "Aster's younger sister was called Neris.",
        "future": "A future development must never become continuity evidence.",
    }
    ordinals = {"prior": 3, "opening": 4, "future": 5}
    seed = next(passage_windows(raw["opening"]))[2]
    loaded = Counter()

    def read(key):
        assert key != "future"
        loaded[key] += 1
        return raw[key]

    kwargs = dict(bookmark=4, chapters=_chapters(raw, ordinals), read_chapter=read,
                  semantic_hits=[(.9, seed, 4, "opening"), (.8, raw["future"], 5, "future")],
                  atoms=_atoms(raw), candidate_pool=True, known_names=["Aster", "Neris"])
    sources = retrieve_ask_sources("What relation links Aster and Neris?", **kwargs)
    opening = next(source for source in sources if source["chapter_key"] == "opening" and source["text"] == seed)
    assert len(opening["continuity_ids"]) == 1
    tail = next(source for source in sources if source["id"] == opening["continuity_ids"][0])
    assert tail["chapter_key"] == "prior" and tail["ordinal"] == 3
    assert "Aster's younger sister was called Neris." in tail["text"]
    assert tail["id"] not in opening["context_ids"], "cross-chapter continuity is a separate edge kind"
    assert loaded == {"opening": 1, "prior": 1}
    _assert_exact_pool(sources, raw, ordinals, 4)
    assert retrieve_ask_sources("What relation links Aster and Neris?", **kwargs) == sources


@pytest.mark.parametrize("prior_ordinal,prior_text", [(2, "Aster's sister was Neris."), (3, "")])
def test_missing_or_empty_immediate_prior_chapter_cannot_be_replaced_with_an_older_chapter(
    prior_ordinal, prior_text,
):
    raw = {"prior": prior_text, "opening": "Her household called her the youngest child."}
    ordinals = {"prior": prior_ordinal, "opening": 4}
    sources = retrieve_ask_sources(
        "What relation links Aster and Neris?", bookmark=4, chapters=_chapters(raw, ordinals),
        read_chapter=raw.get, semantic_hits=[(.9, raw["opening"], 4, "opening")],
        atoms=_atoms(raw), candidate_pool=True,
    )
    opening = next(source for source in sources if source["chapter_key"] == "opening")
    assert opening["continuity_ids"] == []
    _assert_exact_pool(sources, raw, ordinals, 4)


def test_nonopening_lexical_anchor_does_not_create_cross_chapter_continuity():
    raw = {"prior": "Aster's sister was called Neris.",
           "later": "A quiet afternoon. " * 260 + "Aster and Neris met at the doorway."}
    ordinals = {"prior": 3, "later": 4}
    sources = retrieve_ask_sources(
        "Where did Aster meet Neris?", bookmark=4, chapters=_chapters(raw, ordinals),
        read_chapter=raw.get, semantic_hits=[], atoms=_atoms(raw), candidate_pool=True,
    )
    direct = next(source for source in sources if "met at the doorway" in source["text"])
    assert direct["continuity_ids"] == []
    _assert_exact_pool(sources, raw, ordinals, 4)


def test_opening_links_to_an_already_retained_tail_even_after_three_other_tails_are_generated():
    raw = {"prior": "An ordinary afternoon. " * 180 + "Aster's younger sister was called Neris.",
           "opening": "Her household called her the youngest child."}
    ordinals = {"prior": 3, "opening": 4}
    hits = []
    for ordinal in (6, 8, 10):
        before, opening = f"p{ordinal - 1}", f"s{ordinal}"
        raw[before] = f"The preceding section {ordinal - 1} concerned ordinary weather."
        raw[opening] = f"The next discussion {ordinal} concerned ordinary lamps."
        ordinals[before], ordinals[opening] = ordinal - 1, ordinal
        hits.append((.99 - ordinal / 100, raw[opening], ordinal, opening))
    hits.append((.5, raw["opening"], 4, "opening"))
    sources = retrieve_ask_sources(
        "What relation links Aster and Neris?", bookmark=10, chapters=_chapters(raw, ordinals),
        read_chapter=raw.get, semantic_hits=hits, atoms=_atoms(raw), candidate_pool=True,
        known_names=["Aster", "Neris"],
    )
    opening = next(source for source in sources if source["chapter_key"] == "opening")
    assert opening["continuity_ids"]
    prior = next(source for source in sources if source["id"] == opening["continuity_ids"][0])
    assert prior["chapter_key"] == "prior" and "Aster's younger sister was called Neris." in prior["text"]
    _assert_exact_pool(sources, raw, ordinals, 10)


def test_future_ordinal_is_rejected_before_loading_even_when_it_is_a_semantic_seed():
    loaded = []

    def read(key):
        loaded.append(key)
        assert key == "visible"
        return "Aster read the record."

    sources = retrieve_ask_sources(
        "What did Aster read?", bookmark=1,
        chapters=[{"chapter_key": "future", "revealed_at": 2},
                  {"chapter_key": "visible", "revealed_at": 1}],
        read_chapter=read, semantic_hits=[(.99, "Future evidence.", 2, "future")],
        atoms={"visible": {"href": "visible.xhtml", "title": "Section visible"}}, candidate_pool=True,
    )
    assert loaded == ["visible"] and all(source["ordinal"] == 1 for source in sources)
    assert all(source["continuity_ids"] == [] for source in sources)


def test_context_cache_cannot_bypass_existing_character_or_chapter_scan_ceilings(monkeypatch):
    import app.ask as ask

    raw = {"one": "Aster read a long record. " * 4, "two": "Neris read another record. " * 4}
    ordinals = {"one": 1, "two": 2}
    monkeypatch.setattr(ask, "MAX_ASK_SCAN_CHARS", 150)
    with pytest.raises(AskRetrievalLimit, match="text exceeds"):
        retrieve_ask_sources("What did Aster and Neris read?", bookmark=2,
                             chapters=_chapters(raw, ordinals), read_chapter=raw.get,
                             semantic_hits=[], atoms=_atoms(raw), candidate_pool=True)
    monkeypatch.setattr(ask, "MAX_ASK_SCAN_CHARS", 1000)
    monkeypatch.setattr(ask, "MAX_ASK_SCAN_CHAPTERS", 1)
    loaded = []
    with pytest.raises(AskRetrievalLimit, match="chapter count"):
        retrieve_ask_sources("What did Aster and Neris read?", bookmark=2,
                             chapters=_chapters(raw, ordinals),
                             read_chapter=lambda key: loaded.append(key) or raw[key],
                             semantic_hits=[], atoms=_atoms(raw), candidate_pool=True)
    assert loaded == ["one"]


def test_legacy_six_source_path_does_not_gain_context_metadata_or_exceed_its_cap():
    raw = {str(index): f"Aster and Neris read record {index}." for index in range(1, 15)}
    ordinals = {key: int(key) for key in raw}
    sources = retrieve_ask_sources(
        "What did Aster and Neris read?", bookmark=14, chapters=_chapters(raw, ordinals),
        read_chapter=raw.get, semantic_hits=[], atoms=_atoms(raw), candidate_pool=False,
    )
    assert len(sources) == 6
    assert all("continuity_ids" not in source and "context_ids" not in source for source in sources)
