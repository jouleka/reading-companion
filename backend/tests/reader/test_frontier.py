"""LIT-12 frontier — lifted verbatim (pure) from spikes/lit-12-frontier/frontier.py per ADR 0007 D-A1
group (a). These pin the contract the segmenter's atoms must satisfy (and that LIT-13 will consume)."""
import pytest

from app.reader import frontier as F


def test_chapter_bounds_are_contiguous_cumulative():
    assert F.chapter_bounds([10, 5, 20]) == [(0, 10), (10, 15), (15, 35)]


def test_zero_length_atom_is_rejected():
    with pytest.raises(ValueError):
        F.chapter_bounds([10, 0, 5])                            # a zero-length atom would leak (see D20)


def test_assert_aligned_passes_on_contiguous_ordinals():
    bounds = F.chapter_bounds([3, 3, 3])
    F.assert_aligned(bounds, [1, 2, 3])                         # no raise


def test_assert_aligned_rejects_a_count_match_with_a_gap():
    bounds = F.chapter_bounds([3, 3, 3])
    with pytest.raises(ValueError):
        F.assert_aligned(bounds, [1, 2, 4])                    # same count, non-contiguous -> reject


def test_assert_aligned_rejects_a_count_mismatch():
    bounds = F.chapter_bounds([3, 3])
    with pytest.raises(ValueError):
        F.assert_aligned(bounds, [1, 2, 3])


def test_cfi_to_bookmark_counts_completed_chapters():
    bounds = F.chapter_bounds([10, 10, 10])
    assert F.cfi_to_bookmark(0, bounds) == 0                    # at the very start, nothing complete
    assert F.cfi_to_bookmark(10, bounds) == 1                   # exactly at ch1 end -> ch1 complete
    assert F.cfi_to_bookmark(15, bounds) == 1                   # mid ch2 -> ch2 pending
    assert F.cfi_to_bookmark(30, bounds) == 3                   # at/after the end -> all complete


def test_bookmark_high_water_is_monotonic():
    bounds = F.chapter_bounds([10, 10, 10])
    assert F.bookmark_high_water(2, 5, bounds) == 2             # paging back never lowers the high-water
