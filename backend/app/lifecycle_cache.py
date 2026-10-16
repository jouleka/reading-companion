"""Bounded recap state, with an optional compact checkpoint of cleared positive recaps.

Positive and negative results are independent LRU maps. Per-key synthesis locks are reference-counted
and removed as soon as their holder/waiters leave; a semaphore caps the number of distinct active
flights without splitting callers for the same key into multiple model requests.

Keys are ``(book_id, catalog_incarnation, digest)``. The incarnation prevents a delete/re-import from
observing an earlier shelf incarnation even before explicit invalidation runs.
"""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
import time


@dataclass
class _Flight:
    lock: threading.Lock
    refs: int = 0


class RecapRegistry:
    def __init__(self, *, max_entries: int, max_failures: int, max_flights: int, persist_path=None):
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
        self._path = Path(persist_path) if persist_path is not None else None
        self._load()

    @staticmethod
    def _encoded(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    @staticmethod
    def _valid_recap(value):
        return (isinstance(value, dict) and isinstance(value.get("recap"), str)
                and 0 < len(value["recap"]) <= 20000
                and (value.get("now") is None or isinstance(value["now"], str))
                and type(value.get("as_of_chapter")) is int and value["as_of_chapter"] > 0
                and all(type(value.get(k)) is int and value[k] >= 0
                        for k in ("cast_size", "open_threads"))
                and isinstance(value.get("cast"), list) and len(value["cast"]) <= 2000
                and all(isinstance(c, dict) and isinstance(c.get("name"), str)
                        and type(c.get("entity_id")) is int for c in value["cast"])
                and value.get("cached") is False)

    def _load(self):
        if self._path is None:
            return
        try:
            with self._path.open("rb") as stream:
                raw = stream.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                return
            checkpoint = json.loads(raw)
            rows = checkpoint["rows"]
            if (checkpoint["version"] != 1 or not isinstance(rows, list)
                    or len(rows) > self._max_entries
                    or hashlib.sha256(self._encoded(rows)).hexdigest() != checkpoint["sha256"]):
                return
            for key, value in rows:
                if (not isinstance(key, list) or len(key) != 3
                        or not all(isinstance(k, str) for k in key) or not self._valid_recap(value)):
                    return
            self._cache.update((tuple(key), value) for key, value in rows)
        except (OSError, ValueError, TypeError, KeyError):
            pass  # A damaged or old cache is a miss, never a partially trusted recap.

    def _save(self):
        if self._path is None:
            return
        temporary = self._path.with_suffix(".tmp")
        try:
            # Only this registry's cleared recap payloads may be persisted. Answers and failures
            # use process-local registries. One bounded file avoids accumulating per-run artifacts.
            rows = [[list(k), v] for k, v in self._cache.items() if self._valid_recap(v)]
            while rows:
                raw = self._encoded({"version": 1, "rows": rows,
                                     "sha256": hashlib.sha256(self._encoded(rows)).hexdigest()})
                if len(raw) <= 2 * 1024 * 1024:
                    break
                rows.pop(0)
            if not rows:
                raw = self._encoded({"version": 1, "rows": [],
                                    "sha256": hashlib.sha256(self._encoded([])).hexdigest()})
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(raw)
            os.replace(temporary, self._path)
        except (OSError, ValueError, TypeError):
            pass  # Availability must not depend on the optional checkpoint being writable.
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

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
            self._save()

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
            self._save()

    def stats(self):
        with self._gate:
            return {
                "entries": len(self._cache),
                "failures": len(self._failed),
                "flights": len(self._flights),
            }
