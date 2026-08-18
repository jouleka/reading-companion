-- LIT-56: owner-scoped, deletion-aware lexical search over source-book prose.
CREATE TABLE book_search_documents (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  ordinal           INTEGER NOT NULL CHECK (ordinal > 0),
  href              TEXT NOT NULL,
  title             TEXT NOT NULL DEFAULT '',
  part_label        TEXT NOT NULL DEFAULT '',
  content           TEXT NOT NULL,
  char_start        BIGINT NOT NULL CHECK (char_start >= 0),
  char_end          BIGINT NOT NULL CHECK (char_end > char_start),
  search_vector     TSVECTOR GENERATED ALWAYS AS (
    to_tsvector('simple', title || ' ' || part_label || ' ' || content)
  ) STORED,
  PRIMARY KEY (owner_id, book_id, book_incarnation, ordinal),
  FOREIGN KEY (owner_id, book_id, book_incarnation)
    REFERENCES books(owner_id, id, incarnation) ON DELETE CASCADE
);

CREATE INDEX ix_book_search_documents_owner_vector
  ON book_search_documents USING GIN (search_vector);

ALTER TABLE book_search_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE book_search_documents FORCE ROW LEVEL SECURITY;
CREATE POLICY book_search_documents_owner_isolation ON book_search_documents
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());
