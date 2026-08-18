import asyncio
from types import SimpleNamespace
import uuid

import pytest

from app.hosted.provider_runtime import budgeted_hosted_completion
from app.hosted.tenant.models import OwnerId


class RecordingRepository:
    def __init__(self):
        self.events = []
        self.reservation_id = uuid.uuid4()

    async def reserve_provider_call(self, owner_id, book_id, **values):
        self.events.append(("reserve", owner_id, book_id, values))
        return {"id": self.reservation_id}

    async def settle_provider_call(self, owner_id, reservation_id, **values):
        self.events.append(("settle", owner_id, reservation_id, values))


class RecordingClient:
    _is_native_openai = True

    def __init__(self, events, *, failure=None):
        self.events = events
        self.failure = failure

    def complete(self, _system, _user, **_kwargs):
        self.events.append(("provider",))
        if self.failure:
            raise self.failure
        return {"answer": "grounded"}, {"in": 11, "out": 3}


def _settings():
    return SimpleNamespace(
        cost_max_input_tokens_per_call=10_000,
        cost_max_output_tokens_per_call=1_200,
    )


def _setting():
    return {
        "id": str(uuid.uuid4()),
        "credential_id": str(uuid.uuid4()),
        "updated_at": "2026-07-21T12:00:00+00:00",
        "provider": "openai-compatible",
        "model": "gpt-4o-mini",
    }


def test_hosted_completion_reserves_before_provider_io_and_settles_actual_usage():
    repository = RecordingRepository()
    client = RecordingClient(repository.events)
    owner_id = OwnerId(uuid.uuid4())
    book_id = uuid.uuid4()

    result = asyncio.run(budgeted_hosted_completion(
        repository,
        owner_id,
        book_id,
        _settings(),
        _setting(),
        client,
        phase="synthesis",
        system="Use only evidence.",
        user="Evidence",
    ))

    assert [event[0] for event in repository.events] == ["reserve", "provider", "settle"]
    assert result.usage == {"in": 11, "out": 3}
    assert repository.events[-1][-1]["input_tokens"] == 11
    assert repository.events[-1][-1]["output_tokens"] == 3


def test_hosted_completion_conservatively_settles_a_failed_provider_call():
    repository = RecordingRepository()
    client = RecordingClient(repository.events, failure=RuntimeError("provider failed"))

    with pytest.raises(RuntimeError, match="provider failed"):
        asyncio.run(budgeted_hosted_completion(
            repository,
            OwnerId(uuid.uuid4()),
            uuid.uuid4(),
            _settings(),
            _setting(),
            client,
            phase="judge",
            system="Review.",
            user="Answer and evidence",
        ))

    assert [event[0] for event in repository.events] == ["reserve", "provider", "settle"]
    reserve = repository.events[0][-1]
    settle = repository.events[-1][-1]
    assert settle["input_tokens"] == reserve["reserved_input_tokens"]
    assert settle["output_tokens"] == reserve["reserved_output_tokens"]
