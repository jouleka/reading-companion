"""Bounded process-local recap state (LIT-33).

Positive and negative results are independent LRU maps. Per-key synthesis locks are reference-counted
and removed as soon as their holder/waiters leave; a semaphore caps the number of distinct active
flights without splitting callers for the same key into multiple model requests.

Keys are ``(book_id, catalog_incarnation, digest)``. The incarnation prevents a delete/re-import from
observing an earlier shelf incarnation even before explicit invalidation runs.
"""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
import threading
import time


@dataclass
class _Flight:
    lock: threading.Lock
    refs: int = 0


class RecapRegistry:
    def __init__(self, *, max_entries: int, max_failures: int, max_flights: int):
        for name, value in (
            ("max_entries", max_entries),
            ("max_failures", max_failures),
            ("max_flights", max_flights),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._max_entries = max_entries
        self._max_failures = max_failures
        self._cache = OrderedDict()
        self._failed = OrderedDict()
        self._flights: dict[tuple, _Flight] = {}
        self._gate = threading.Lock()
        self._flight_slots = threading.BoundedSemaphore(max_flights)

    @staticmethod
    def _trim(mapping, limit):
        while len(mapping) > limit:
            mapping.popitem(last=False)

    def get(self, key):
        with self._gate:
            value = self._cache.get(key)
            if value is not None:
                self._cache.move_to_end(key)
            return value

    def set(self, key, value):
        with self._gate:
            self._cache[key] = value
            self._cache.move_to_end(key)
            self._trim(self._cache, self._max_entries)

    def failure_recent(self, key, *, ttl: float, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._gate:
            at = self._failed.get(key)
            if at is None:
                return False
            if now - at >= ttl:
                del self._failed[key]
                return False
            self._failed.move_to_end(key)
            return True

    def mark_failed(self, key, *, at: float | None = None):
        with self._gate:
            self._failed[key] = time.monotonic() if at is None else at
            self._failed.move_to_end(key)
            self._trim(self._failed, self._max_failures)

    def clear_failed(self, key):
        with self._gate:
            self._failed.pop(key, None)

    @contextmanager
    def flight(self, key):
        """Serialize one key and bound the number of distinct active keys.

        The semaphore is acquired only for a new key. A race that discovers another thread created
        the key while waiting releases its unused slot and joins that existing flight.
        """
        reserved_slot = False
        entry = None
        while entry is None:
            with self._gate:
                entry = self._flights.get(key)
                if entry is not None:
                    entry.refs += 1
                    break
            self._flight_slots.acquire()
            reserved_slot = True
            with self._gate:
                entry = self._flights.get(key)
                if entry is None:
                    entry = _Flight(threading.Lock(), refs=1)
                    self._flights[key] = entry
                    reserved_slot = False  # the entry now owns the slot until its final reference exits
                else:
                    entry.refs += 1
            if reserved_slot:
                self._flight_slots.release()
                reserved_slot = False

        try:
            with entry.lock:
                yield
        finally:
            release_slot = False
            with self._gate:
                entry.refs -= 1
                if entry.refs == 0 and self._flights.get(key) is entry:
                    del self._flights[key]
                    release_slot = True
            if release_slot:
                self._flight_slots.release()

    def invalidate_book(self, book_id: str):
        """Drop completed state for one book. Protected routes serialize deletion against recap use."""
        with self._gate:
            for mapping in (self._cache, self._failed):
                for key in [candidate for candidate in mapping if candidate[0] == book_id]:
                    del mapping[key]

    def stats(self):
        with self._gate:
            return {
                "entries": len(self._cache),
                "failures": len(self._failed),
                "flights": len(self._flights),
            }
