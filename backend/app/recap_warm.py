"""Coalesced preparation; foreground reading controls which book may start new paid calls."""
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import replace
import threading
from types import SimpleNamespace

from app.deps import book_lifecycle
from app.reader_prepare import PreparationScope, PreparationStale, prepare


class RecapPrewarmer:
    def __init__(self, app):
        self.app = app
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=8)
        self.pending = {}
        self.completed = OrderedDict()
        self.focus = None
        self.active_key = self.active_token = self.active_cancel = None
        self.closed = False
        self.task = asyncio.create_task(self._run(), name="reader-preparation")

    def notify(self, book_id, incarnation, *, force=False, foreground=False):
        self.loop.call_soon_threadsafe(self._schedule, (book_id, incarnation), force, foreground)

    def _focus_queue(self, key):
        drained = []
        while not self.queue.empty():
            drained.append(self.queue.get_nowait())
        for queued in drained:
            if queued == key:
                self.queue.put_nowait(queued)
            elif queued != self.active_key:
                self.pending.pop(queued, None)
        # Defer task_done until the replacement is enqueued: queue.join must not wake early.
        return len(drained)

    def _schedule(self, key, force=False, foreground=False):
        if self.closed:
            return
        drained = 0
        try:
            if foreground:
                self.focus = key
                if self.active_key is not None and self.active_key != key:
                    self.active_cancel.set()
                drained = self._focus_queue(key)
            if self.focus is not None and key != self.focus:
                return  # Ingest/correction notifications cannot steal the reader's focus.
            state = self.app.state.catalog.get_state(key[0])
            if state is None or state["bookmark"] <= 0 or state["ingest_progress"] < state["bookmark"]:
                if key == self.active_key:
                    self.active_cancel.set()
                return  # Wait for completed, grounded ingestion of the latest frontier.
            base = (state["position_epoch"], state["bookmark"])
            prior = self.pending.get(key, self.completed.get(key))
            token = (*base, prior[2] + 1 if force and prior is not None else 0)
            if not force and prior is not None and prior[:2] == base:
                token = prior
            if key == self.active_key:
                if token != self.active_token:
                    self.active_cancel.set()
                elif self.active_cancel.is_set():
                    # A -> B -> A while A's call settles must enqueue a successor even though
                    # its source frontier is unchanged. The successor resumes its checkpoint.
                    token = (*base, token[2] + 1)
            if not force and self.completed.get(key) == token:
                return
            if key in self.pending:
                self.pending[key] = token
            elif not self.queue.full():
                self.pending[key] = token
                self.queue.put_nowait(key)
        finally:
            for _ in range(drained):
                self.queue.task_done()

    async def _run(self):
        while True:
            key = await self.queue.get()
            if key is None:
                self.queue.task_done()
                return
            token = self.pending.get(key)
            cancelled = threading.Event()
            self.active_key, self.active_token, self.active_cancel = key, token, cancelled
            finished = False
            try:
                await asyncio.sleep(self.app.state.settings.reader_prepare_delay_seconds)
                if cancelled.is_set():
                    raise PreparationStale()
                request = SimpleNamespace(app=self.app, recap_preparation=True)
                # Only snapshot acquisition holds a short lease; provider IO holds no request gate.
                async with asynccontextmanager(book_lifecycle)(key[0], request):
                    scope = await asyncio.to_thread(PreparationScope.capture, self.app, *key)
                scope = replace(scope, cancelled=cancelled)
                scope.check(self.app)
                await asyncio.to_thread(prepare, self.app, scope)
                scope.check(self.app, snapshot=True)
                finished = True
            except PreparationStale:
                pass
            except Exception:
                finished = True  # Failed providers do not create an automatic retry/spend loop.
            finally:
                latest = self.pending.pop(key, None)
                self.active_key = self.active_token = self.active_cancel = None
                if finished and not cancelled.is_set():
                    self.completed[key] = token
                    self.completed.move_to_end(key)
                    while len(self.completed) > 128:
                        self.completed.popitem(last=False)
                if latest is not None and latest != token and not self.closed:
                    self._schedule(key, force=True)
                self.queue.task_done()

    async def close(self):
        self.closed = True
        if self.active_cancel is not None:
            self.active_cancel.set()
        while not self.queue.empty():
            key = self.queue.get_nowait()
            if key != self.active_key:
                self.pending.pop(key, None)
            self.queue.task_done()
        self.queue.put_nowait(None)
        await self.task
