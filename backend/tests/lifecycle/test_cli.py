import json
from types import SimpleNamespace

from app.lifecycle import __main__ as lifecycle_cli


def test_backup_all_creates_one_archive_per_book_and_honors_recent_backups(
    tmp_path, monkeypatch, capsys
):
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "backups"

    class FakeCatalog:
        def __init__(self, _path):
            pass

        def list_books(self):
            return [{"book_id": "a"}, {"book_id": "b"}]

        def close(self):
            pass

    calls = []

    def fake_backup(_data_dir, book_id, output):
        calls.append(book_id)
        output.write_text("archive", encoding="utf-8")
        return SimpleNamespace(archive=output)

    monkeypatch.setattr(lifecycle_cli, "Catalog", FakeCatalog)
    monkeypatch.setattr(lifecycle_cli, "backup_book", fake_backup)
    assert lifecycle_cli.main([
        "backup-all", "--data-dir", str(data_dir), "--output-dir", str(output_dir), "--keep", "2"
    ]) == 0
    body = json.loads(capsys.readouterr().out)
    assert calls == ["a", "b"]
    assert len(body["archives"]) == 2

    calls.clear()
    assert lifecycle_cli.main([
        "backup-all", "--data-dir", str(data_dir), "--output-dir", str(output_dir),
        "--min-age-hours", "24",
    ]) == 0
    body = json.loads(capsys.readouterr().out)
    assert calls == []
    assert body["skipped"] == ["a", "b"]
