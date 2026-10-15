"""Bounded best-effort recap preparation after durable ingestion, using the normal safe route."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from app.deps import book_lifecycle


class RecapPrewarmer:
    def __init__(self, app):
        self.app = app
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=8)
        self.pending = set()
        self.closed = False
        self.task = asyncio.create_task(self._run(), name="recap-preparation")

    def notify(self, book_id, incarnation):
        # Called by an ingestion thread. The queue and pending set belong to the lifespan loop.
        self.loop.call_soon_threadsafe(self._schedule, (book_id, incarnation))

    def _schedule(self, key):
        if self.closed or key in self.pending or self.queue.full():
            return
        self.pending.add(key)
        self.queue.put_nowait(key)

    async def _run(self):
        while True:
            key = await self.queue.get()
            if key is None:
                self.queue.task_done()
                return
            try:
                request = SimpleNamespace(app=self.app)
                async with asynccontextmanager(book_lifecycle)(key[0], request):
                    state = self.app.state
                    book = state.catalog.get_book(key[0])
                    if book is None or book["incarnation"] != key[1]:
                        continue
                    # Recompute the authoritative frontier AFTER acquiring the same request gate
                    # as reset/delete/position. No queued caller-provided bookmark is trusted.
                    from app.api.views import catch_me_up
                    await asyncio.to_thread(
                        catch_me_up, key[0], request, None, state.catalog, state.store,
                        state.settings, state.client, state.worker,
                    )
            except Exception:
                pass  # Normal cache/failure TTL/cost accounting still apply; no automatic retry.
            finally:
                self.pending.discard(key)
                self.queue.task_done()

    async def close(self):
        self.closed = True
        # Drop unpaid queued work; let the single active operation finish before closing stores.
        while not self.queue.empty():
            key = self.queue.get_nowait()
            self.pending.discard(key)
            self.queue.task_done()
        self.queue.put_nowait(None)
        await self.task
