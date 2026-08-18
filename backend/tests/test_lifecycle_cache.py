import threading
import time

from app.lifecycle_cache import RecapRegistry


def _key(book, incarnation, suffix):
    return book, incarnation, suffix


def test_recap_and_failure_lrus_are_bounded_and_book_invalidatable():
    registry = RecapRegistry(max_entries=2, max_failures=2, max_flights=2)
    a1, a2, b1 = _key("a", "inc-a", "1"), _key("a", "inc-a", "2"), _key("b", "inc-b", "1")
    registry.set(a1, {"recap": "a1"})
    registry.set(a2, {"recap": "a2"})
    assert registry.get(a1)["recap"] == "a1"  # a2 is now least recently used
    registry.set(b1, {"recap": "b1"})
    assert registry.get(a2) is None

    registry.mark_failed(a1, at=10.0)
    registry.mark_failed(a2, at=11.0)
    registry.mark_failed(b1, at=12.0)
    assert not registry.failure_recent(a1, ttl=60, now=12.0)
    assert registry.failure_recent(a2, ttl=60, now=12.0)

    registry.invalidate_book("a")
    assert registry.get(a1) is None
    assert not registry.failure_recent(a2, ttl=60, now=12.0)
    assert registry.stats() == {"entries": 1, "failures": 1, "flights": 0}


def test_expired_negative_entry_is_removed():
    registry = RecapRegistry(max_entries=1, max_failures=1, max_flights=1)
    key = _key("a", "inc", "1")
    registry.mark_failed(key, at=10.0)
    assert not registry.failure_recent(key, ttl=5.0, now=15.01)
    assert registry.stats()["failures"] == 0


def test_single_flights_are_bounded_without_splitting_same_key():
    registry = RecapRegistry(max_entries=1, max_failures=1, max_flights=1)
    first_entered = threading.Event()
    release_first = threading.Event()
    same_key_entered = threading.Event()
    other_key_entered = threading.Event()

    def hold_first():
        with registry.flight(_key("a", "inc", "1")):
            first_entered.set()
            release_first.wait(timeout=5)

    def wait_same():
        with registry.flight(_key("a", "inc", "1")):
            same_key_entered.set()

    def wait_other():
        with registry.flight(_key("b", "inc", "1")):
            other_key_entered.set()

    threads = [threading.Thread(target=fn) for fn in (hold_first, wait_same, wait_other)]
    threads[0].start()
    assert first_entered.wait(timeout=5)
    threads[1].start()
    threads[2].start()
    time.sleep(0.05)
    assert registry.stats()["flights"] == 1
    assert not same_key_entered.is_set() and not other_key_entered.is_set()
    release_first.set()
    for thread in threads:
        thread.join(timeout=5)
    assert same_key_entered.is_set() and other_key_entered.is_set()
    assert registry.stats()["flights"] == 0
