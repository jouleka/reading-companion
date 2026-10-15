"""Coalesced preparation while reading; provider IO never holds the position/reset request gate."""
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from types import SimpleNamespace

from app.deps import book_lifecycle
from app.reader_prepare import PreparationScope, prepare


class RecapPrewarmer:
    def __init__(self, app):
        self.app = app
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=8)
        self.pending = {}
        self.completed = OrderedDict()
        self.closed = False
        self.task = asyncio.create_task(self._run(), name="reader-preparation")

    def notify(self, book_id, incarnation, *, force=False):
        self.loop.call_soon_threadsafe(self._schedule, (book_id, incarnation), force)

    def _schedule(self, key, force=False):
        if self.closed:
            return
        state = self.app.state.catalog.get_state(key[0])
        if state is None or state["bookmark"] <= 0:
            return
        base = (state["position_epoch"], min(state["bookmark"], state["ingest_progress"]))
        prior = self.pending.get(key, self.completed.get(key))
        token = (*base, (prior[2] + 1 if force and prior is not None else 0))
        if not force and prior is not None and prior[:2] == base:
            token = prior
        if token[1] <= 0 or (not force and self.completed.get(key) == token):
            return
        if key in self.pending:
            self.pending[key] = token
        elif not self.queue.full():
            self.pending[key] = token
            self.queue.put_nowait(key)

    async def _run(self):
        while True:
            key = await self.queue.get()
            if key is None:
                self.queue.task_done()
                return
            token = None
            try:
                await asyncio.sleep(self.app.state.settings.reader_prepare_delay_seconds)
                token = self.pending.get(key)
                request = SimpleNamespace(app=self.app, recap_preparation=True)
                # Only snapshot acquisition has a short read lease. Position/reset/delete can
                # proceed during model IO; guarded reservations and publication reject stale work.
                async with asynccontextmanager(book_lifecycle)(key[0], request):
                    scope = await asyncio.to_thread(PreparationScope.capture, self.app, *key)
                await asyncio.to_thread(prepare, self.app, scope)
            except Exception:
                pass
            finally:
                latest = self.pending.pop(key, None)
                if token is not None:
                    self.completed[key] = token
                    self.completed.move_to_end(key)
                    while len(self.completed) > 128:
                        self.completed.popitem(last=False)
                if latest is not None and latest != token and not self.closed:
                    self._schedule(key, force=True)
                self.queue.task_done()

    async def close(self):
        self.closed = True
        while not self.queue.empty():
            key = self.queue.get_nowait()
            self.pending.pop(key, None)
            self.queue.task_done()
        self.queue.put_nowait(None)
        await self.task
