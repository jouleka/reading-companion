-- LIT-39: a completion receipt is proof only for the exact chapter content it completed.
ALTER TABLE chapters
  ADD CONSTRAINT uq_chapters_owner_content_identity
  UNIQUE (owner_id, book_id, book_incarnation, id, content_hash);

ALTER TABLE ingested_chapters
  ADD CONSTRAINT fk_receipts_owner_content_identity
  FOREIGN KEY (owner_id, book_id, book_incarnation, chapter_id, content_hash)
  REFERENCES chapters (owner_id, book_id, book_incarnation, id, content_hash)
  DEFERRABLE INITIALLY DEFERRED;
