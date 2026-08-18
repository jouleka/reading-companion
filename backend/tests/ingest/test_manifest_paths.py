from pathlib import Path

import pytest

from app.ingest.manifest import AtomSetMismatch, manifest_path, source_epub_path


def test_book_storage_paths_accept_local_ids(tmp_path: Path) -> None:
    assert manifest_path(tmp_path, "bkfixture0001") == str(
        tmp_path / "books" / "bkfixture0001" / "atoms.json"
    )
    assert source_epub_path(tmp_path, "bk0123456789ab") == str(
        tmp_path / "books" / "bk0123456789ab" / "source.epub"
    )


@pytest.mark.parametrize(
    "book_id",
    ["", "bk", "../outside", "bk../outside", "bkabc/../../outside", "BK0123456789AB"],
)
def test_book_storage_paths_reject_untrusted_segments(tmp_path: Path, book_id: str) -> None:
    with pytest.raises(AtomSetMismatch, match="malformed book id"):
        manifest_path(tmp_path, book_id)
    with pytest.raises(AtomSetMismatch, match="malformed book id"):
        source_epub_path(tmp_path, book_id)
