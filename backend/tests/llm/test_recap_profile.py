"""A short recap draft may be fast; its independent structured reviewers must stay unchanged."""
from copy import copy
import json

import httpx

from app.ask import AskVerdict
from app.llm.client import LLMClient


def test_recap_draft_profile_does_not_lower_review_or_shared_client_reasoning():
    bodies = []

    def reply(request):
        body = json.loads(request.content)
        bodies.append(body)
        content = (json.dumps({"references_future": False, "unsupported_claims": [], "missing_details": []})
                   if "response_format" in body else "Nell carried a box.")
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 1, "model": "gpt-6-luna",
            "service_tier": "default", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        })

    with httpx.Client(transport=httpx.MockTransport(reply)) as transport:
        shared = LLMClient(env={"OPENAI_API_KEY": "unit-test-only"}, cheap_model="gpt-6-luna",
                           large_model="gpt-6-luna", http_client=transport, max_retries=0)
        draft = copy(shared)
        draft.recap_generation_profile = True
        text, _ = draft.complete("system", "source", tier="large")
        assert text == "Nell carried a box."
        draft.complete("system", "source", schema=AskVerdict)
        shared.complete("system", "source", tier="large")
    assert [body["reasoning_effort"] for body in bodies] == ["none", "medium", "medium"]
    assert all(body["service_tier"] == "default" for body in bodies)
    assert not hasattr(shared, "recap_generation_profile")
