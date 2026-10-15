"""Run the tested local hybrid profile without copying credentials or existing reading data."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from app.config import Settings
from app.main import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--frontend-dist", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--windows-credential", action="store_true")
    args = parser.parse_args()
    settings = Settings(
        _env_file=str(args.env_file.resolve()), deployment_mode="local", allow_stub=False,
        data_dir=str(args.data_dir.resolve()), frontend_dist_dir=str(args.frontend_dist.resolve()),
        jev_enabled=True, jev_windows_credential=args.windows_credential,
        jev_selection_enabled=True, jev_evidence_enabled=True, jev_recap_enabled=False,
        jev_partial_evidence_enabled=True,
        jev_extraction_enabled=True,
        recap_prewarm=True, recap_disk_cache=True, reader_answer_cache=True,
        reader_prepare_enabled=True, reader_reuse_enabled=True, reader_fast_mode=False,
        cost_max_usd_per_book=1.0,
    )
    uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, access_log=False,
                log_level="warning")


if __name__ == "__main__":
    main()
