"""Cache and concurrency shortcuts must preserve complete answers and mutation boundaries."""
import asyncio
from contextlib import asynccontextmanager
import threading
from types import SimpleNamespace

import httpx
import pytest

from test_views import env as env
from app.deps import book_lifecycle
from app.lifecycle_cache import RecapRegistry
from app.cost.limits import usd_of, reserved_usd_of
from app.config import Settings
from app.deps import build_state
from app.llm.client import LLMClient


def test_reader_fast_mode_is_isolated_and_bills_the_fast_premium(tmp_path):
    settings = Settings(_env_file=None, openai_api_key="unit-test-only",
                        llm_cheap_model="gpt-6-luna", llm_large_model="gpt-6-luna",
                        reader_fast_mode=True, data_dir=str(tmp_path / "data"))
    state = build_state(settings)
    try:
        primary, reader = state["client"], state["reader_client"]
        assert primary is not reader and getattr(primary, "service_tier", "default") == "default"
        assert reader.service_tier == "fast" and reader._oai.max_retries == 0
        assert reader.extractor_version() == primary.extractor_version()
        assert reader._embed_oai is primary._embed_oai
        assert reader.embed_identity() == primary.embed_identity()
        assert reader.billing_model("large") == "fast:gpt-6-luna"
        usage = {"in": 1000, "out": 200, "cache_read": 100}
        assert usd_of(reader.billing_model("large"), usage) == 2 * usd_of("gpt-6-luna", usage)
        assert reserved_usd_of(reader.billing_model("large"), usage) == 2 * reserved_usd_of("gpt-6-luna", usage)
    finally:
        state["store"].close()
        state["catalog"].close()
        state["client"]._oai.close()
        state["client"]._embed_oai.close()


def test_fast_wire_keeps_reasoning_and_independent_structured_review():
    import json
    import httpx
    from app.ask import AskVerdict
    bodies = []
    def reply(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 1,
            "model": "gpt-6-luna", "service_tier": "fast",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": '{"references_future":false,"unsupported_claims":[],"missing_details":[]}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        })
    with httpx.Client(transport=httpx.MockTransport(reply)) as transport:
        client = LLMClient(env={"OPENAI_API_KEY": "unit-test-only"}, cheap_model="gpt-6-luna",
                           large_model="gpt-6-luna", http_client=transport, max_retries=0)
        client.service_tier = "fast"
        verdict, usage = client.complete("Review only these sources.", "Untrusted claim data.",
                                         schema=AskVerdict, max_output_tokens=512)
        assert verdict == {"references_future": False, "unsupported_claims": [], "missing_details": []}
        assert usage == {"in": 100, "out": 20}
        assert bodies[0]["reasoning_effort"] == "medium"
        assert bodies[0]["service_tier"] == "fast" and bodies[0]["max_completion_tokens"] == 512
        assert bodies[0]["response_format"]["json_schema"]["strict"] is True
        client._embed_oai.close()


def test_reviewed_answer_cache_is_free_and_frontier_and_reset_scoped(env, monkeypatch):
    api, bid, app, _ = env
    client = app.state.client
    app.state.settings.reader_answer_cache = True
    # Keep the real offline route's deterministic validation. Jev is unused for stub generation.
    client.jev = SimpleNamespace(model="jev-1.13.0", close=lambda: None)
    client.answers = RecapRegistry(max_entries=2, max_failures=1, max_flights=1)
    calls = []
    original = client.complete
    def count(*args, **kwargs):
        calls.append(1)
        fields = getattr(kwargs.get("schema"), "model_fields", {})
        if "insufficient_evidence" in fields:
            fact = "Berenice met Aldric at the forge and they spoke."
            return {"insufficient_evidence": fact not in args[1],
                    "claims": ([{"text": fact, "citation_ids": [1]}]
                               if fact in args[1] else [])}, {"in": 20, "out": 12}
        if "references_future" in fields:
            return {"references_future": False, "unsupported_claims": []}, {"in": 12, "out": 3}
        return original(*args, **kwargs)
    monkeypatch.setattr(client, "complete", count)
    question = {"question": "Where did Berenice meet Aldric?", "bookmark": 999}
    first = api.post(f"/api/books/{bid}/ask", json=question)
    assert first.status_code == 200, first.text
    assert first.json()["claims"] and not first.json()["insufficient_evidence"]
    paid_calls = len(calls)
    second = api.post(f"/api/books/{bid}/ask", json=question).json()
    assert second["cached"] is True and second["claims"] == first.json()["claims"]
    assert second["cost"]["calls"] == [] and len(calls) == paid_calls
    earlier = api.post(f"/api/books/{bid}/ask", json={**question, "bookmark": 1}).json()
    assert earlier["as_of_chapter"] == 1 and "Berenice" not in str(earlier.get("claims"))
    assert len(calls) > paid_calls
    api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0})
    reset = api.post(f"/api/books/{bid}/ask", json=question).json()
    assert reset["as_of_chapter"] == 0 and reset["claims"] == []


@pytest.mark.parametrize("assist_path", ["/ask", "/chapter-closeout"])
def test_preparation_and_answers_share_reads_but_reset_waits_and_cancel_releases(assist_path):
    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(book_request_locks={}))
        gate = asynccontextmanager(book_lifecycle)
        def request(method, path):
            return SimpleNamespace(app=app, method=method, url=SimpleNamespace(path=path))
        preparation = SimpleNamespace(app=app, recap_preparation=True)
        warm_entered, release_warm = asyncio.Event(), asyncio.Event()
        answer_entered, release_answer = asyncio.Event(), asyncio.Event()
        reset_entered = asyncio.Event()
        async def warm():
            async with gate("book", preparation):
                warm_entered.set()
                await release_warm.wait()
        async def ask():
            async with gate("book", request("POST", "/api/books/book" + assist_path)):
                answer_entered.set()
                await release_answer.wait()
        async def reset():
            async with gate("book", request("POST", "/api/books/book/position/reset")):
                reset_entered.set()
        warmer = asyncio.create_task(warm())
        await warm_entered.wait()
        answer = asyncio.create_task(ask())
        # A whole slow recap is deliberately still in flight; the answer must already be admitted.
        await asyncio.wait_for(answer_entered.wait(), timeout=1)
        mutation = asyncio.create_task(reset())
        await asyncio.sleep(0)
        assert not reset_entered.is_set()
        answer.cancel()
        try:
            await answer
        except asyncio.CancelledError:
            pass
        assert not reset_entered.is_set()  # The preparation still protects its snapshot.
        release_warm.set()
        await asyncio.gather(warmer, mutation)
        assert reset_entered.is_set() and not app.state.book_request_locks
    asyncio.run(scenario())


@pytest.mark.parametrize(("method", "suffix"), [
    ("POST", "/position/reset"), ("DELETE", ""), ("POST", "/memory-corrections"),
])
def test_recap_and_progress_share_lifecycle_but_mutations_wait_and_cancellation_cleans_up(
    method, suffix,
):
    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(book_request_locks={}))
        gate = asynccontextmanager(book_lifecycle)

        def request(method, suffix):
            return SimpleNamespace(app=app, method=method,
                                   url=SimpleNamespace(path=f"/api/books/book{suffix}"))

        recap_entered, position_entered = asyncio.Event(), asyncio.Event()
        release_recap, release_position = asyncio.Event(), asyncio.Event()
        second_position_entered, mutation_entered = asyncio.Event(), asyncio.Event()

        async def hold(method, suffix, entered, release):
            async with gate("book", request(method, suffix)):
                entered.set()
                await release.wait()

        async def mutate():
            async with gate("book", request(method, suffix)):
                mutation_entered.set()

        tasks = []
        try:
            recap = asyncio.create_task(hold("GET", "/catch-me-up", recap_entered, release_recap))
            tasks.append(recap)
            await asyncio.wait_for(recap_entered.wait(), timeout=1)
            position = asyncio.create_task(hold("PUT", "/position", position_entered, release_position))
            tasks.append(position)
            await asyncio.wait_for(position_entered.wait(), timeout=1)
            second_position = asyncio.create_task(hold(
                "PUT", "/position", second_position_entered, release_position,
            ))
            tasks.append(second_position)
            await asyncio.sleep(0)
            entry = app.state.book_request_locks["book"]
            assert entry.readers == entry.refcount == 3
            assert not second_position_entered.is_set()  # Progress transitions serialize independently.

            for endpoint in ("/position", "/ingest", "/graph", "/manifest"):
                async with asyncio.timeout(1):
                    async with gate("book", request("GET", endpoint)):
                        assert not release_recap.is_set() and not release_position.is_set()

            second_position.cancel()  # Cancellation while waiting for the progress lock drops its lease.
            await asyncio.gather(second_position, return_exceptions=True)
            assert entry.readers == entry.refcount == 2
            mutation = asyncio.create_task(mutate())
            tasks.append(mutation)
            await asyncio.sleep(0)
            assert entry.lock.locked() and not mutation_entered.is_set()

            # A queued mutation keeps writer preference. Cancel a reader queued behind it without
            # leaking the registry entry or allowing the mutation to cross an active recap.
            queued_reader = asyncio.create_task(hold(
                "GET", "/ingest", asyncio.Event(), asyncio.Event(),
            ))
            tasks.append(queued_reader)
            await asyncio.sleep(0)
            queued_reader.cancel()
            await asyncio.gather(queued_reader, return_exceptions=True)
            assert entry.refcount == 3  # recap + position + mutation
            release_position.set()
            await position
            assert not mutation_entered.is_set()
            recap.cancel()  # Cancellation of an admitted reader also releases the exclusive waiter.
            await asyncio.gather(recap, return_exceptions=True)
            await asyncio.wait_for(mutation, timeout=1)
            assert mutation_entered.is_set() and not app.state.book_request_locks
        finally:
            release_recap.set()
            release_position.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["reset", "delete"])
@pytest.mark.parametrize("experience", ["recap", "reflection"])
def test_slow_recap_does_not_block_reader_status_or_progress_but_lifecycle_mutation_waits(
    env, monkeypatch, mutation, experience,
):
    api, bid, app, atoms = env
    client = app.state.reader_client
    original_complete = client.complete
    entered, release = threading.Event(), threading.Event()

    def slow_recap(system, user, tier="cheap", schema=None, **kwargs):
        if tier == "large" and (schema is None if experience == "recap"
                                else getattr(schema, "__name__", "") == "AskDraft"):
            entered.set()
            assert release.wait(5), "test did not release the simulated recap provider"
        return original_complete(system, user, tier=tier, schema=schema, **kwargs)

    monkeypatch.setattr(client, "complete", slow_recap)
    app.state.recaps.invalidate_book(bid)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as requests:
            recap = asyncio.create_task(
                requests.get(f"/api/books/{bid}/catch-me-up") if experience == "recap" else
                requests.post(f"/api/books/{bid}/chapter-closeout", json={"chapter": 2}))
            mutation_request = None
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                position, ingest, graph = await asyncio.wait_for(asyncio.gather(*[
                    requests.get(f"/api/books/{bid}/{endpoint}")
                    for endpoint in ("position", "ingest", "graph")
                ]), timeout=1)
                assert all(response.status_code == 200 for response in (position, ingest, graph))
                assert position.json()["bookmark"] == ingest.json()["ingest_progress"] == 2
                assert graph.json()["as_of_chapter"] == 2

                progress = await asyncio.wait_for(requests.put(f"/api/books/{bid}/position", json={
                    "cfi": "responsive-progress", "offset": sum(a["char_len"] for a in atoms[:2]) + 2,
                    "position_epoch": 0,
                }), timeout=1)
                assert progress.status_code == 200 and progress.json()["bookmark"] == 2
                assert app.state.catalog.get_state(bid)["cfi"] == "responsive-progress"
                assert not release.is_set() and not recap.done()

                mutation_request = asyncio.create_task(
                    requests.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0})
                    if mutation == "reset" else requests.delete(f"/api/books/{bid}")
                )
                # Observe acquisition of the exclusive gate, rather than infer ordering from timing.
                async with asyncio.timeout(1):
                    while not app.state.book_request_locks[bid].lock.locked():
                        await asyncio.sleep(0)
                assert not mutation_request.done()
                assert app.state.catalog.get_state(bid)["bookmark"] == 2
                release.set()
                recap_response, mutation_response = await asyncio.wait_for(
                    asyncio.gather(recap, mutation_request), timeout=3,
                )
                assert recap_response.status_code == 200, recap_response.text
                assert recap_response.json()["as_of_chapter"] == 2
                assert mutation_response.status_code == (200 if mutation == "reset" else 204)
                if mutation == "reset":
                    assert app.state.catalog.get_state(bid)["position_epoch"] == 1
                    assert app.state.catalog.get_state(bid)["bookmark"] == 0
                else:
                    assert app.state.catalog.get_book(bid) is None
                assert not app.state.book_request_locks
            finally:
                release.set()
                await asyncio.gather(recap, *([mutation_request] if mutation_request else []),
                                     return_exceptions=True)

    # Reuse the app's lifespan loop: lifecycle entries must never cross event loops.
    api.portal.call(scenario)
