"""Exact bounded lexical neighbors and cross-chapter continuity; no model or book fixtures."""
from collections import Counter

import pytest

from app.ask import AskRetrievalLimit, retrieve_ask_sources
from app.ask_context import complete_ask_context
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


@pytest.mark.parametrize("direction", ["before", "after"])
def test_selecting_only_a_lexical_neighbor_completes_back_to_the_anchor_naming_the_defendant(direction):
    decision = 'The foreman gave the jurors\' answer: "Liable."'
    protest = "The defendant Aster protested the verdict."
    text = "soft " * 840 + decision + " still" * 210 + " " + protest + " soft" * 350
    raw, ordinals = {"decision": text}, {"decision": 1}
    question = "What verdict was announced for Aster?"
    sources = retrieve_ask_sources(
        question, bookmark=1, chapters=_chapters(raw, ordinals), read_chapter=raw.get,
        semantic_hits=[], atoms=_atoms(raw), candidate_pool=True, known_names=["Aster"],
    )
    windows = list(passage_windows(text))
    index = next(index for index, (_start, _end, passage) in enumerate(windows) if protest in passage)
    anchor = next(source for source in sources if source["text"] == windows[index][2])
    neighbor_text = windows[index - 1 if direction == "before" else index + 1][2]
    neighbor = next(source for source in sources if source["text"] == neighbor_text)
    assert "Aster" not in neighbor["text"] and "Aster" in anchor["text"]
    assert anchor["id"] in neighbor["context_ids"] and neighbor["id"] in anchor["context_ids"]
    if direction == "before":
        assert decision in neighbor["text"] and protest not in neighbor["text"]
    completed = complete_ask_context(question, [neighbor], sources, ["Aster"])
    assert completed[0] == neighbor and anchor in completed
    assert len(completed) <= 6 and len({source["id"] for source in completed}) == len(completed)
    assert all(source in sources for source in completed)
    assert complete_ask_context(question, [neighbor], sources, ["Aster"]) == completed
    _assert_exact_pool(sources, raw, ordinals, 1)


def test_first_person_semantic_seed_adds_retained_opening_before_remote_attribution():
    literal = "I kept the coins sewn inside a cloth pouch at my throat. "
    quotient, remainder = divmod(1250 - len(literal), 5)
    padding = "hush " * quotient
    if remainder:
        padding = "hush " * (quotient - 1) + "hush" + "x" * remainder + " "
    prefix = literal + padding
    assert len(prefix) == 1250
    attribution = "Aster said the account was his own."
    text = prefix + "I told the story. " + "hush " * 1000 + attribution + " hush" * 400
    windows = list(passage_windows(text))
    seed = windows[1][2]
    assert seed.startswith("I ") and literal.strip() not in seed
    raw, ordinals = {"account": text}, {"account": 1}
    question = "Where did Aster keep the coins?"
    sources = retrieve_ask_sources(
        question, bookmark=1, chapters=_chapters(raw, ordinals), read_chapter=raw.get,
        semantic_hits=[(.9, seed, 1, "account")], atoms=_atoms(raw), candidate_pool=True,
        known_names=["Aster"],
    )
    by_id = {source["id"]: source for source in sources}
    direct = next(source for source in sources if source["text"] == seed)
    opening = next(source for source in sources if source["text"] == windows[0][2])
    remote_id = next(source_id for source_id in direct["context_ids"]
                     if attribution in by_id[source_id]["text"])
    assert opening["id"] in direct["context_ids"] and direct["id"] in opening["context_ids"]
    assert direct["context_ids"].index(opening["id"]) < direct["context_ids"].index(remote_id)
    completed = complete_ask_context(question, [direct], sources, ["Aster"])
    ids = [source["id"] for source in completed]
    assert opening in completed and ids.index(opening["id"]) < ids.index(remote_id)
    assert literal.strip() in opening["text"]
    assert len(completed) <= 6 and len(set(ids)) == len(ids)
    _assert_exact_pool(sources, raw, ordinals, 1)


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
