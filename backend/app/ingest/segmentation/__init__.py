"""LIT-4 EPUB chapter segmentation (ADR 0001) + the ADR-0007 D-A8 divider-merge. Produces the
POST-MERGE chapter atoms (`revealed_at` units) that LIT-5 (store) and LIT-12 (frontier) consume."""
from app.ingest.segmentation.epub_segmenter import segment_epub
from app.ingest.segmentation.models import ChapterAtom, SegmentResult

__all__ = ["segment_epub", "ChapterAtom", "SegmentResult"]
