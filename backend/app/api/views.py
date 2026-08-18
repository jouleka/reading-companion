"""View routes (ADR 0007 D-A10/D-A11): graph / timeline / notes / search / catch-me-up.

THE CLAMP: every bookmark-taking route computes ``effective_bookmark`` from the requested bookmark,
reader high-water, and this process's contiguous v2-marker-validated frontier ONCE at the route boundary
(typed int >= 0 — anything else is 422 fail-closed), then threads it through BOTH the view read and any
gate/synthesis call (D-A9/D-A10). Catalog progress alone can never authorize legacy partial facts.

LOCK DISCIPLINE (D-A3): the search-query embedding and the recap synthesis LLM call happen OUTSIDE
``store.book()``; only funnel reads + the deterministic gate run under the per-book lock.

CATCH-ME-UP is lazy synthesis (D5) behind the LIT-8 runtime gate: supplied-facts -> large-tier recap
-> ``assert_recap_safe`` (one regenerate on rejection, then a GENERIC 502 — the error channel must
never carry the rejected recap or a future name). Cached in-process by the full Inv-7 key
(book, catalog incarnation, bookmark, validity_snapshot, pinned synth model, prompt version,
atom_set_version) — the
pass-3 churn fixes are what make this cache affordable mid-ingest."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from app.ask import (
    ASK_SYSTEM,
    AskDraft,
    AskRequest,
    AskSafetyError,
    ask_prompt,
    cited_sources,
    draft_text,
    source_facts,
    validate_ask_draft,
)

from app.cost import CostCeilingExceeded, budgeted_completion, budgeted_embedding, pricing_known
from app.deps import (
    book_lifecycle,
    get_catalog,
    get_client,
    get_settings,
    get_store,
    get_worker,
)
from app.eval.spoiler_gate import (
    SpoilerGateError,
    assert_recap_safe,
    cache_key,
    delta_facts,
    evolve_prompt,
    flowing_system_for,
    now_prompt,
    now_system_for,
    read_text_upto,
    score_recap,
    supplied_facts,
    synth_prompt,
    validity_snapshot,
)
from app.eval.spoiler_gate.judge import JudgeUnavailable, judge_recap
from app.ingest.manifest import AtomSetMismatch, assert_matches_store, load_manifest
from app.reading_assist import (
    CLOSEOUT_SYSTEM,
    SELECTION_SYSTEM,
    ChapterCloseoutRequest,
    SelectionActionRequest,
    SelectionDraft,
    chapter_closeout_prompt,
    chapter_passages,
    selection_prompt,
)

router = APIRouter(
    prefix="/api/books/{book_id}", tags=["views"], dependencies=[Depends(book_lifecycle)]
)

RECAP_PROMPT_VERSION = "recap-v3"                 # unchanged novel prompt/cache identity (LIT-29)
NON_NOVEL_RECAP_PROMPT_VERSION = "recap-v4"       # LIT-9 profile-aware neutral wording
RECAP_FAILURE_TTL_S = 60.0                        # negative cache: a double-rejected key 502s without
#                                                   re-paying until the TTL lapses (pass-2 convoy fix)


def _recap_prompt_version(book_type, content_language="und"):
    # Novel generation is byte-identical and deliberately retains its cache key: upgrading LIT-9 must
    # not repurchase Karamazov recaps. Other profiles key by type because their wording differs.
    base = (RECAP_PROMPT_VERSION if book_type == "novel"
            else f"{NON_NOVEL_RECAP_PROMPT_VERSION}:{book_type}")
    return base if content_language in {"und", "en"} or content_language.startswith("en-") \
        else f"{base}:source-{content_language}"

_BM = Query(default=None, ge=0, description="explicit bookmark (scrubber); clamped to the high-water")


class EntityCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_entity_id: StrictInt = Field(ge=1)
    canonical_name: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    bookmark: StrictInt = Field(ge=1)

    @field_validator("canonical_name", "reason")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


def _effective(book_id, requested, catalog, store, settings, worker):
    """The route-boundary prologue: known book -> fail-closed manifest/store check -> the CLAMP."""
    st = catalog.get_state(book_id)
    if st is None:
        raise HTTPException(404, "unknown book")
    try:
        manifest = load_manifest(settings.data_dir, book_id)
        with store.book(book_id) as mem:
            assert_matches_store(manifest, mem)
            durable = mem.completion_frontier(manifest["atoms"])
    except AtomSetMismatch as e:
        raise HTTPException(409, "atom-set mismatch (re-import the book)") from e
    high_water = min(st["bookmark"], durable)
    eff = high_water if requested is None else min(requested, high_water)
    return eff, st, manifest


_IDENTITY_HONORIFICS = {"father", "elder", "abbot", "brother", "mother", "sister"}


def _coalesce_honorific_characters(characters, relationships):
    """Repair legacy split identities in a view without mutating stored evidence.

    Only a two-token honorific plus an exact, unique one-token canonical name is folded; broad
    surname or fuzzy matching would risk merging distinct people.
    """
    single_names = {}
    for character in characters:
        name = character["canonical_name"].strip()
        if len(name.split()) == 1:
            single_names.setdefault(name.casefold(), []).append(character)

    remap = {}
    folded = {}
    for character in characters:
        tokens = character["canonical_name"].strip().split()
        if len(tokens) != 2 or tokens[0].casefold().rstrip(".") not in _IDENTITY_HONORIFICS:
            continue
        candidates = single_names.get(tokens[1].casefold(), [])
        if len(candidates) == 1 and candidates[0]["entity_id"] != character["entity_id"]:
            canonical = candidates[0]
            remap[character["entity_id"]] = canonical["entity_id"]
            folded.setdefault(canonical["entity_id"], []).append(character)

    if not remap:
        return characters, relationships

    merged_characters = []
    for character in characters:
        if character["entity_id"] in remap:
            continue
        variants = folded.get(character["entity_id"], [])
        aliases = list(character.get("aliases", []))
        for variant in variants:
            aliases.append(variant["canonical_name"])
            aliases.extend(variant.get("aliases", []))
            character["revealed_at"] = min(character["revealed_at"], variant["revealed_at"])
        character["aliases"] = list(dict.fromkeys(alias for alias in aliases
                                                   if alias != character["canonical_name"]))
        merged_characters.append(character)

    merged_relationships = []
    seen = set()
    for relationship in relationships:
        relationship["src_entity"] = remap.get(relationship["src_entity"], relationship["src_entity"])
        relationship["dst_entity"] = remap.get(relationship["dst_entity"], relationship["dst_entity"])
        if relationship["src_entity"] == relationship["dst_entity"]:
            continue
        key = (relationship["src_entity"], relationship["dst_entity"], relationship["rel_type"],
               relationship["label"], relationship["revealed_at"], relationship["invalid_at"])
        if key not in seen:
            seen.add(key)
            merged_relationships.append(relationship)
    return merged_characters, merged_relationships


@router.get("/graph")
def graph(book_id: str, bookmark: int | None = _BM, catalog=Depends(get_catalog),
          store=Depends(get_store), settings=Depends(get_settings), worker=Depends(get_worker)):
    eff, _st, _m = _effective(book_id, bookmark, catalog, store, settings, worker)
    with store.book(book_id) as mem:
        v = mem.view(eff)
        chars = []
        for row in v.characters():
            character = dict(row)
            character["aliases"] = [alias["surface_form"] for alias in v.aliases_of(row["entity_id"])]
            chars.append(character)
        rels = [dict(r) for r in v.relationships()]
        chars, rels = _coalesce_honorific_characters(chars, rels)
    return {"as_of_chapter": eff, "characters": chars, "relationships": rels}


@router.get("/memory-corrections")
def memory_corrections(book_id: str, bookmark: int | None = _BM,
                       catalog=Depends(get_catalog), store=Depends(get_store),
                       settings=Depends(get_settings), worker=Depends(get_worker)):
    """Reader-safe provenance: a correction is hidden before its story-time frontier."""
    eff, _st, _manifest = _effective(book_id, bookmark, catalog, store, settings, worker)
    if eff < 1:
        return {"as_of_chapter": eff, "items": []}
    with store.book(book_id) as mem:
        items = mem.entity_correction_history(eff)
    return {"as_of_chapter": eff, "items": items}


@router.post("/memory-corrections")
def correct_memory(book_id: str, body: EntityCorrectionRequest,
                   catalog=Depends(get_catalog), store=Depends(get_store),
                   settings=Depends(get_settings), worker=Depends(get_worker)):
    """Publish a one-for-one identity correction at the reader's exact current frontier."""
    eff, _st, _manifest = _effective(
        book_id, body.bookmark, catalog, store, settings, worker
    )
    if eff != body.bookmark:
        raise HTTPException(409, "reading progress changed; reopen the codex and try again")
    try:
        with store.book(book_id) as mem:
            result = mem.replace_entity(
                body.source_entity_id,
                effective_at=eff,
                canonical_name=body.canonical_name,
                reason=body.reason,
            )
            items = mem.entity_correction_history(eff)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"as_of_chapter": eff, **result, "items": items}


@router.get("/character/{entity_id}")
def character_card(book_id: str, entity_id: int, bookmark: int | None = _BM,
                   catalog=Depends(get_catalog), store=Depends(get_store),
                   settings=Depends(get_settings), worker=Depends(get_worker)):
    """LIT-30 name card: bookmark-clamped identity + ties for a character. A future / unknown entity
    404s (no leak — indistinguishable from nonexistent). Structured data only; no gate/judge (nothing
    is generated). ``entity_id`` is a typed path param (a non-int is 422)."""
    eff, _st, _m = _effective(book_id, bookmark, catalog, store, settings, worker)
    with store.book(book_id) as mem:
        card = _character_card(mem, eff, entity_id)
    if card is None:
        raise HTTPException(404, "unknown character")
    return {"as_of_chapter": eff, **card}


@router.get("/timeline")
def timeline(book_id: str, bookmark: int | None = _BM, catalog=Depends(get_catalog),
             store=Depends(get_store), settings=Depends(get_settings), worker=Depends(get_worker)):
    eff, _st, _m = _effective(book_id, bookmark, catalog, store, settings, worker)
    with store.book(book_id) as mem:
        v = mem.view(eff)
        events = []
        for ev in v.timeline():
            row = dict(ev)
            row["participants"] = [p["canonical_name"] for p in v.participants_of(ev["event_id"])]
            events.append(row)
    return {"as_of_chapter": eff, "events": events}


@router.get("/notes")
def notes(book_id: str, bookmark: int | None = _BM, catalog=Depends(get_catalog),
          store=Depends(get_store), settings=Depends(get_settings), worker=Depends(get_worker)):
    """LIT-31 codex 'the story broken down': per-chapter summary + highlights (who first appears +
    that chapter's events) + the visible cast for name-chip wrapping. Highlights are grouped by
    ``revealed_at`` through the funnel (an entity/event stamped later than eff is never read), so the
    breakdown is spoiler-safe by construction — no gate needed (structured reads only)."""
    eff, _st, _m = _effective(book_id, bookmark, catalog, store, settings, worker)
    with store.book(book_id) as mem:
        v = mem.view(eff)
        titles = {c["chapter_key"]: c["title"] for c in v.chapters()}
        new_by_ch: dict[int, list] = {}
        for e in v.characters():                                  # type='character', revealed_at<=eff
            new_by_ch.setdefault(e["revealed_at"], []).append(
                {"entity_id": e["entity_id"], "name": e["canonical_name"]})
        events_by_ch: dict[int, list] = {}
        for ev in v.timeline():                                   # revealed_at<=eff, order-stable
            events_by_ch.setdefault(ev["revealed_at"], []).append(ev["summary"])
        chapters = [{"chapter_key": s["chapter_key"], "revealed_at": s["revealed_at"],
                     "title": titles.get(s["chapter_key"], ""), "summary": s["summary"],
                     "new_characters": new_by_ch.get(s["revealed_at"], []),
                     "events": events_by_ch.get(s["revealed_at"], [])}
                    for s in v.chapter_summaries()]
        cast = _visible_cast(mem, eff)
    return {"as_of_chapter": eff, "cast": cast, "chapters": chapters}


@router.get("/search")
def search(book_id: str, q: str, k: int = Query(default=5, ge=1, le=50),
           bookmark: int | None = _BM, catalog=Depends(get_catalog), store=Depends(get_store),
           settings=Depends(get_settings), client=Depends(get_client), worker=Depends(get_worker)):
    eff, _st, _m = _effective(book_id, bookmark, catalog, store, settings, worker)
    try:
        qvec = budgeted_embedding(
            catalog, settings, client, book_id, phase="search-embedding", texts=[q]
        ).value[0]                                # OUTSIDE the lock (D-A3 — never IO under it)
    except CostCeilingExceeded as exc:
        raise HTTPException(429, "book cost ceiling reached") from exc
    with store.book(book_id) as mem:
        hits = mem.view(eff).search(qvec, k=k)
    return {"as_of_chapter": eff,
            "hits": [{"score": round(s, 6), "text": t, "revealed_at": rev, "chapter_key": key}
                     for (s, t, rev, key) in hits]}


def _ask_cost_payload(calls):
    return {
        "currency": "USD",
        "usd": f"{sum(call['usd'] for call in calls):.10f}",
        "input_tokens": sum(call["input_tokens"] for call in calls),
        "output_tokens": sum(call["output_tokens"] for call in calls),
        "pricing_known": all(call["pricing_known"] for call in calls),
        "calls": [
            {
                "provider": call["provider"],
                "model": call["model"],
                "usd": f"{call['usd']:.10f}",
            }
            for call in calls
        ],
        "payer": (
            "local offline engine"
            if calls and all(call["provider"] == "stub" for call in calls)
            else "your configured provider account"
        ),
    }


def _public_ask_source(source):
    return {
        "id": source["id"],
        "ordinal": source["ordinal"],
        "chapter_key": source["chapter_key"],
        "href": source["href"],
        "title": source["title"],
        "excerpt": source["text"],
    }


@router.post("/ask")
def ask_the_book(
    book_id: str,
    body: AskRequest,
    catalog=Depends(get_catalog),
    store=Depends(get_store),
    settings=Depends(get_settings),
    client=Depends(get_client),
    worker=Depends(get_worker),
):
    """Answer only from completed, retrieved passages and return chapter-navigable citations."""
    eff, state, manifest = _effective(book_id, body.bookmark, catalog, store, settings, worker)
    eff = min(eff, state["ingest_progress"])
    calls = []

    def remember(result, *, provider, model):
        calls.append({
            "provider": provider,
            "model": model,
            "input_tokens": int(result.usage.get("in", 0) or 0),
            "output_tokens": int(result.usage.get("out", 0) or 0),
            "usd": float(result.usd),
            "pricing_known": pricing_known(model),
        })

    if eff <= 0:
        return {
            "as_of_chapter": 0,
            "insufficient_evidence": True,
            "claims": [],
            "citations": [],
            "cost": _ask_cost_payload(calls),
        }
    try:
        embedded = budgeted_embedding(
            catalog, settings, client, book_id, phase="search-embedding", texts=[body.question]
        )
    except CostCeilingExceeded as exc:
        raise HTTPException(429, "book cost ceiling reached") from exc
    embed_identity = client.embed_identity()
    remember(
        embedded,
        provider="stub" if embed_identity.startswith("stub:") else client.embed_provider,
        model=embed_identity,
    )
    with store.book(book_id) as mem:
        hits = mem.view(eff).search(embedded.value[0], k=6)

    atoms = {atom["key"]: atom for atom in manifest["atoms"]}
    sources = []
    seen = set()
    for _score, text, revealed_at, chapter_key in hits:
        normalized = " ".join((text or "").split())[:2000]
        if not normalized or (chapter_key, normalized) in seen:
            continue
        seen.add((chapter_key, normalized))
        atom = atoms.get(chapter_key, {})
        sources.append({
            "id": len(sources) + 1,
            "ordinal": int(revealed_at),
            "chapter_key": chapter_key,
            "href": atom.get("href", ""),
            "title": atom.get("title", "") or f"Chapter {revealed_at}",
            "text": normalized,
        })
    if not sources:
        return {
            "as_of_chapter": eff,
            "insufficient_evidence": True,
            "claims": [],
            "citations": [],
            "cost": _ask_cost_payload(calls),
        }

    prompt = ask_prompt(body.question, sources)
    for _attempt in range(2):
        try:
            generated = budgeted_completion(
                catalog,
                settings,
                client,
                book_id,
                phase="synthesis",
                system=ASK_SYSTEM,
                user=prompt,
                tier="large",
                schema=AskDraft,
            )
        except CostCeilingExceeded as exc:
            raise HTTPException(429, "book cost ceiling reached") from exc
        remember(generated, provider=client.provider, model=client._model_for("large"))
        try:
            draft = validate_ask_draft(generated.value, sources)
        except (AskSafetyError, ValueError):
            continue
        if draft.insufficient_evidence:
            return {
                "as_of_chapter": eff,
                "insufficient_evidence": True,
                "claims": [],
                "citations": [],
                "cost": _ask_cost_payload(calls),
            }

        answer = draft_text(draft)
        with store.book(book_id) as mem:
            score = score_recap(mem, eff, answer, read_text=read_text_upto(mem, eff))
        if (
            score["future_entity_leaks"]
            or score["prolepsis_hits"]
            or score["unsupported_event_bindings"]
        ):
            continue

        used = cited_sources(draft, sources)

        def judge_complete(system, user, tier="cheap", schema=None):
            result = budgeted_completion(
                catalog,
                settings,
                client,
                book_id,
                phase="judge",
                system=system,
                user=user,
                tier=tier,
                schema=schema,
            )
            remember(result, provider=client.provider, model=client._model_for(tier))
            return result.value, result.usage

        try:
            verdict, _usage = judge_recap(
                client,
                answer,
                source_facts(used),
                tier="cheap",
                complete=judge_complete,
            )
        except CostCeilingExceeded as exc:
            raise HTTPException(429, "book cost ceiling reached") from exc
        except JudgeUnavailable:
            continue
        if verdict["references_future"] or verdict["unsupported_claims"]:
            continue
        return {
            "as_of_chapter": eff,
            "insufficient_evidence": False,
            "claims": [claim.model_dump(mode="json") for claim in draft.claims],
            "citations": [_public_ask_source(source) for source in used],
            "cost": _ask_cost_payload(calls),
        }
    raise HTTPException(502, "answer could not be cleared against the pages you have read")


@router.post("/selection-action")
def selection_action(
    book_id: str,
    body: SelectionActionRequest,
    catalog=Depends(get_catalog),
    store=Depends(get_store),
    settings=Depends(get_settings),
    client=Depends(get_client),
    worker=Depends(get_worker),
):
    """Explain, define, or translate only the reader-supplied visible selection."""
    eff, _state, manifest = _effective(book_id, None, catalog, store, settings, worker)
    if body.atom > min(len(manifest["atoms"]), eff + 1):
        raise HTTPException(422, "selection is outside the current reading position")
    atom = manifest["atoms"][body.atom - 1]
    source = {
        "id": 1,
        "ordinal": body.atom,
        "chapter_key": atom["key"],
        "href": atom.get("href", ""),
        "title": atom.get("title", "") or f"Chapter {body.atom}",
        "text": body.text,
        "cfi": body.cfi,
    }
    calls = []

    def remember(result, *, model):
        calls.append({
            "provider": client.provider,
            "model": model,
            "input_tokens": int(result.usage.get("in", 0) or 0),
            "output_tokens": int(result.usage.get("out", 0) or 0),
            "usd": float(result.usd),
            "pricing_known": pricing_known(model),
        })

    prompt = selection_prompt(body)
    for _attempt in range(2):
        try:
            generated = budgeted_completion(
                catalog,
                settings,
                client,
                book_id,
                phase="synthesis",
                system=SELECTION_SYSTEM,
                user=prompt,
                tier="large",
                schema=SelectionDraft,
            )
        except CostCeilingExceeded as exc:
            raise HTTPException(429, "book cost ceiling reached") from exc
        remember(generated, model=client._model_for("large"))
        try:
            draft = SelectionDraft.model_validate(generated.value)
        except ValueError:
            continue
        if draft.insufficient_evidence:
            return {
                "action": body.action,
                "as_of_chapter": eff,
                "insufficient_evidence": True,
                "text": None,
                "citation": None,
                "cost": _ask_cost_payload(calls),
            }

        def judge_complete(system, user, tier="cheap", schema=None):
            result = budgeted_completion(
                catalog,
                settings,
                client,
                book_id,
                phase="judge",
                system=system,
                user=user,
                tier=tier,
                schema=schema,
            )
            remember(result, model=client._model_for(tier))
            return result.value, result.usage

        try:
            verdict, _usage = judge_recap(
                client,
                draft.text or "",
                source_facts([source]),
                tier="cheap",
                complete=judge_complete,
            )
        except CostCeilingExceeded as exc:
            raise HTTPException(429, "book cost ceiling reached") from exc
        except JudgeUnavailable:
            continue
        if verdict["references_future"] or verdict["unsupported_claims"]:
            continue
        citation = _public_ask_source(source)
        citation["cfi"] = body.cfi
        return {
            "action": body.action,
            "as_of_chapter": eff,
            "insufficient_evidence": False,
            "text": draft.text,
            "citation": citation,
            "cost": _ask_cost_payload(calls),
        }
    raise HTTPException(502, "selection help could not be cleared against the selected passage")


@router.post("/chapter-closeout")
def chapter_closeout(
    book_id: str,
    body: ChapterCloseoutRequest,
    catalog=Depends(get_catalog),
    store=Depends(get_store),
    settings=Depends(get_settings),
    client=Depends(get_client),
    worker=Depends(get_worker),
):
    """Generate cited takeaways from exactly one completed, ingested chapter."""
    eff, state, manifest = _effective(book_id, body.chapter, catalog, store, settings, worker)
    if eff != body.chapter or state["ingest_progress"] < body.chapter:
        raise HTTPException(409, "chapter is not completed and remembered yet")
    atom = manifest["atoms"][body.chapter - 1]
    with store.book(book_id) as mem:
        raw = mem.view(body.chapter).raw_text(atom["key"])
    sources = chapter_passages(
        raw or "",
        ordinal=body.chapter,
        chapter_key=atom["key"],
        href=atom.get("href", ""),
        title=atom.get("title", "") or f"Chapter {body.chapter}",
    )
    calls = []

    def remember(result, *, model):
        calls.append({
            "provider": client.provider,
            "model": model,
            "input_tokens": int(result.usage.get("in", 0) or 0),
            "output_tokens": int(result.usage.get("out", 0) or 0),
            "usd": float(result.usd),
            "pricing_known": pricing_known(model),
        })

    if not sources:
        return {
            "chapter": body.chapter,
            "as_of_chapter": eff,
            "insufficient_evidence": True,
            "claims": [],
            "citations": [],
            "cost": _ask_cost_payload(calls),
        }
    prompt = chapter_closeout_prompt(body.chapter, sources)
    for _attempt in range(2):
        try:
            generated = budgeted_completion(
                catalog,
                settings,
                client,
                book_id,
                phase="synthesis",
                system=CLOSEOUT_SYSTEM,
                user=prompt,
                tier="large",
                schema=AskDraft,
            )
        except CostCeilingExceeded as exc:
            raise HTTPException(429, "book cost ceiling reached") from exc
        remember(generated, model=client._model_for("large"))
        try:
            draft = validate_ask_draft(generated.value, sources)
        except (AskSafetyError, ValueError):
            continue
        if draft.insufficient_evidence:
            return {
                "chapter": body.chapter,
                "as_of_chapter": eff,
                "insufficient_evidence": True,
                "claims": [],
                "citations": [],
                "cost": _ask_cost_payload(calls),
            }
        answer = draft_text(draft)
        with store.book(book_id) as mem:
            score = score_recap(mem, eff, answer, read_text=read_text_upto(mem, eff))
        if (
            score["future_entity_leaks"]
            or score["prolepsis_hits"]
            or score["unsupported_event_bindings"]
        ):
            continue
        used = cited_sources(draft, sources)

        def judge_complete(system, user, tier="cheap", schema=None):
            result = budgeted_completion(
                catalog,
                settings,
                client,
                book_id,
                phase="judge",
                system=system,
                user=user,
                tier=tier,
                schema=schema,
            )
            remember(result, model=client._model_for(tier))
            return result.value, result.usage

        try:
            verdict, _usage = judge_recap(
                client,
                answer,
                source_facts(used),
                tier="cheap",
                complete=judge_complete,
            )
        except CostCeilingExceeded as exc:
            raise HTTPException(429, "book cost ceiling reached") from exc
        except JudgeUnavailable:
            continue
        if verdict["references_future"] or verdict["unsupported_claims"]:
            continue
        return {
            "chapter": body.chapter,
            "as_of_chapter": eff,
            "insufficient_evidence": False,
            "claims": [claim.model_dump(mode="json") for claim in draft.claims],
            "citations": [_public_ask_source(source) for source in used],
            "cost": _ask_cost_payload(calls),
        }
    raise HTTPException(502, "chapter closeout could not be cleared against the completed chapter")


def _visible_cast(mem, eff):
    """The bookmark-bounded cast for the recap's clickable name affordances. Each visible surface form
    (a character's canonical name + each visible alias) paired with its ``entity_id`` (LIT-30), so a
    chip on 'Mitya' and one on 'Dmitri' both resolve to the ONE character card. All ``revealed_at <=
    eff`` — the client wraps ONLY these, so a name it makes clickable can never be a future entity.
    Deduped by name (a clean surface-form -> id map); read under the per-book lock."""
    v = mem.view(eff)
    out, seen = [], set()
    for ch in v.characters():
        eid = ch["entity_id"]
        for name in [ch["canonical_name"], *[a["surface_form"] for a in v.aliases_of(eid)]]:
            if name not in seen:
                seen.add(name)
                out.append({"name": name, "entity_id": eid})
    return out


def _character_card(mem, eff, entity_id):
    """LIT-30 name card for a character AS OF ``eff``: identity (``bio``) + visible ties (relationships
    touching the entity, resolved to the other endpoint's name). Returns None when the entity is not
    visible at ``eff`` (a future / unknown id) so the route can 404 — indistinguishable from
    nonexistent, no leak. Every read is funnel-bounded (``bio`` and ``relationships`` both apply the
    ``revealed_at <= eff`` filter + referential closure), so ties can only reach visible-both-endpoint
    edges — a tie to a not-yet-met character is absent. Structured data only; run under the per-book
    lock. No LLM/gate on this path."""
    v = mem.view(eff)
    bio = v.bio(entity_id)
    if bio is None:
        return None
    id2name = {e["entity_id"]: e["canonical_name"]
               for t in ("character", "place", "faction", "object")
               for e in v.entities_of_type(t)}
    ties, seen = [], set()
    for e in v.relationships():                       # already restricted to visible-both-endpoints,
        if e["src_entity"] == entity_id and e["dst_entity"] in id2name:   # ordered by revealed_at
            other, direction = e["dst_entity"], "out"
        elif e["dst_entity"] == entity_id and e["src_entity"] in id2name:
            other, direction = e["src_entity"], "in"
        else:
            continue
        if other in seen:
            continue                                  # one line per related person — the extraction
        seen.add(other)                               # emits overlapping edges; keep the first (earliest)
        ties.append({"entity_id": other, "name": id2name[other], "rel_type": e["rel_type"],
                     "label": e["label"], "direction": direction})
    return {"entity_id": entity_id, "name": bio["name"], "type": bio["type"],
            "aliases": bio["aliases"], "first_seen": bio["first_seen"],
            "status": bio["state"], "ties": ties}


def _budgeted_runner(catalog, settings, client, book_id, phase):
    def complete(system, user, tier="cheap", schema=None):
        result = budgeted_completion(
            catalog, settings, client, book_id, phase=phase, system=system, user=user,
            tier=tier, schema=schema,
        )
        return result.value, result.usage
    return complete


def _make_now(client, store, catalog, settings, book_id, eff, delta, facts, *, book_type="novel",
              content_language="und"):
    """LIT-29 'right now' one-liner (option b): its OWN small (cheap-tier) model call, spoiler-gated by
    the deterministic gate AND the judge, cost-recorded, fail-SAFE to None. It NEVER blocks the recap —
    a failed / gate-rejected one-liner degrades to absent (spoiler-safety is fail-CLOSED, the one-liner
    is fail-safe-to-absent). LLM calls run OUTSIDE the per-book lock (D-A3); only the deterministic gate
    runs under it. Returns the one-liner text or None."""
    if not delta["chapter_summaries"]:
        return None                                        # nothing newly completed to describe
    try:
        now_text, _usage = _budgeted_runner(
            catalog, settings, client, book_id, "now"
        )(now_system_for(book_type, content_language),
          now_prompt(eff, delta, book_type=book_type), tier="cheap")
    except Exception:
        return None                                        # provider error -> no one-liner (fail-safe)
    with store.book(book_id) as mem:                       # deterministic gate under the lock (no IO)
        try:
            assert_recap_safe(mem, eff, now_text, read_text=read_text_upto(mem, eff))
        except SpoilerGateError:
            return None                                    # spoiler-bearing details stay server-side
    try:                                                   # the judge backstop, OUTSIDE the lock
        verdict, _judge_usage = judge_recap(
            client, now_text, facts, tier="cheap",
            complete=_budgeted_runner(catalog, settings, client, book_id, "now-judge"),
        )
    except (JudgeUnavailable, CostCeilingExceeded):
        return None
    if verdict["references_future"]:
        return None
    return now_text.strip() or None


@router.get("/catch-me-up")
def catch_me_up(book_id: str, request: Request, bookmark: int | None = _BM,
                catalog=Depends(get_catalog), store=Depends(get_store),
                settings=Depends(get_settings), client=Depends(get_client), worker=Depends(get_worker)):
    eff, st, manifest = _effective(book_id, bookmark, catalog, store, settings, worker)
    # the recap can only describe INGESTED chapters: min with the ingest high-water (LIT-12's honest
    # lag — the pending/uningested tail is not recapped), reported as the true as_of.
    eff = min(eff, st["ingest_progress"])
    if eff == 0:
        return {"recap": None, "now": None, "as_of_chapter": 0, "cast_size": 0,
                "open_threads": 0, "cast": [], "cached": False}

    recaps = request.app.state.recaps
    with store.book(book_id) as mem:
        cmu = mem.view(eff).catch_me_up()
        facts = supplied_facts(mem, eff)
        snap = validity_snapshot(mem, eff)
        pinned = mem.pinned_identity() or {}
        book_type = mem.book_profile()["book_type"]
        content_language = mem.content_language()
    synth_model = pinned.get("synth_model") or client._model_for("large")
    incarnation = catalog.get_book(book_id)["incarnation"]
    key = (book_id, incarnation, cache_key(
        book_id,
        eff,
        snap,
        synth_model=synth_model,
        recap_prompt_version=_recap_prompt_version(book_type, content_language),
        atom_set_version=manifest["atom_set_version"],
    ))
    cached = recaps.get(key)
    if cached is not None:
        return {**cached, "cached": True}

    # SINGLE-FLIGHT per cache key (review pass-1): N concurrent cold requests must pay for ONE
    # synthesis. The per-key lock serializes the miss path; losers re-check the cache after acquiring.
    # A double-rejected key is NEGATIVELY cached for RECAP_FAILURE_TTL_S (review pass-2 MEDIUM: the
    # rejection path otherwise re-paid 2 calls per waiter in a serial convoy).
    def _recently_failed():
        return recaps.failure_recent(key, ttl=RECAP_FAILURE_TTL_S)

    if _recently_failed():
        raise HTTPException(502, "recap generation recently failed the spoiler gate; retry shortly")
    with recaps.flight(key):
        cached = recaps.get(key)
        if cached is not None:
            return {**cached, "cached": True}
        if _recently_failed():                    # the flight ahead of us just failed — don't re-pay
            raise HTTPException(502, "recap generation recently failed the spoiler gate; retry shortly")

        # LIT-29 evolve inputs, read under the lock: the DELTA (facts first revealed at eff), the
        # bookmark-bounded CAST (clickable names), and recap(eff-1) from the cache — "the story as last
        # told". recap(N-1) is keyed on snapshot(eff-1): if any <=eff-1 fact changed, its key misses and
        # we fall back to a cumulative synthesis (correct — the prior recap would be stale). The chain
        # warms as the reader progresses (each bookmark finds its predecessor cached).
        with store.book(book_id) as mem:
            delta = delta_facts(mem, eff)
            cast = _visible_cast(mem, eff)
            snap_prev = validity_snapshot(mem, eff - 1) if eff >= 2 else None
        prior_recap = None
        if snap_prev is not None:
            prior_key = (book_id, incarnation, cache_key(
                book_id,
                eff - 1,
                snap_prev,
                synth_model=synth_model,
                recap_prompt_version=_recap_prompt_version(book_type, content_language),
                atom_set_version=manifest["atom_set_version"],
            ))
            prior_recap = (recaps.get(prior_key) or {}).get("recap")

        recap_final = None
        for attempt in range(2):
            # attempt 0: EVOLVE from recap(N-1) + the delta (non-repetitive). If it fails the gate,
            # attempt 1 falls back to the cumulative synthesis — proven grounded, so the reader still
            # gets a recap rather than a 502. The evolve is the optimization; the cumulative is the
            # safety net. BOTH run under FLOWING_SYSTEM (the anti-foreshadow contract stays intact).
            if prior_recap and attempt == 0:
                prompt = evolve_prompt(eff, prior_recap, delta, book_type=book_type)
            else:
                prompt = synth_prompt(eff, facts, book_type=book_type)
            try:
                recap, _usage = _budgeted_runner(
                    catalog, settings, client, book_id, "synthesis"
                )(flowing_system_for(book_type, content_language), prompt, tier="large")
            except CostCeilingExceeded as exc:
                raise HTTPException(429, "book cost ceiling reached") from exc
            with store.book(book_id) as mem:
                try:
                    assert_recap_safe(mem, eff, recap, read_text=read_text_upto(mem, eff))
                except SpoilerGateError:
                    # spoiler-bearing details stay server-side (D-A9 pass-2) — never the response
                    continue
            # THE LLM-JUDGE BACKSTOP (LIT-14, ADR 0004 Vector 3), OUTSIDE the lock (D-A3): the semantic
            # net for a paraphrased future EVENT the deterministic gate can't see (past tense, no future
            # name, no modal). references_future is a HARD reject; a judge outage fails CLOSED (no
            # verdict = unsafe) — both regenerate, then a generic 502. Soft unsupported_claims stay
            # server-side (reader-data-only). The judge sees only the bookmark-bounded facts + the recap.
            try:
                verdict, _judge_usage = judge_recap(
                    client, recap, facts,
                    complete=_budgeted_runner(catalog, settings, client, book_id, "judge"),
                )
            except CostCeilingExceeded as exc:
                raise HTTPException(429, "book cost ceiling reached") from exc
            except JudgeUnavailable:
                continue
            if verdict["references_future"]:
                continue
            recap_final = recap
            break

        if recap_final is not None:
            recaps.clear_failed(key)
            # the 'right now' one-liner — best-effort, fail-safe to absent (never blocks a cleared recap)
            now_text = _make_now(
                client, store, catalog, settings, book_id, eff, delta, facts,
                book_type=book_type, content_language=content_language,
            )
            # READER DATA ONLY (review pass-1 HIGH): future_theme_hits / grounded_rate are gate
            # diagnostics computed from the unfiltered audit hatch — a scrubbed-back client must never
            # receive a FUTURE theme label; they stay out of the payload AND the cache.
            payload = {"recap": recap_final, "now": now_text, "as_of_chapter": eff,
                       "cast_size": cmu["cast_size"], "open_threads": cmu["open_threads"],
                       "cast": cast, "cached": False}
            recaps.set(key, payload)
            return dict(payload)                  # never hand out the cached dict itself
        recaps.mark_failed(key)
    # fail CLOSED with a GENERIC error: no recap text, no names, no details (D-A9 + pass-2 finding)
    raise HTTPException(502, "recap generation failed the spoiler gate; try again later")
