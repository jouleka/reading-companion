"""Cache and concurrency shortcuts must preserve complete answers and mutation boundaries."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

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


def test_preparation_and_answers_share_reads_but_reset_waits_and_cancel_releases():
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
            async with gate("book", request("POST", "/api/books/book/ask")):
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
