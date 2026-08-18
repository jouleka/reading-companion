-- LIT-38: tenant-owned library, reader state, bitemporal memory, receipts, and pgvector.
CREATE TABLE books (
  owner_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  incarnation       UUID NOT NULL DEFAULT gen_random_uuid(),
  title             TEXT NOT NULL,
  author            TEXT,
  source_kind       TEXT,
  source_id         TEXT,
  file_hash         TEXT,
  schema_version    INTEGER NOT NULL CHECK (schema_version > 0),
  content_language  TEXT NOT NULL DEFAULT 'und',
  book_type         TEXT NOT NULL DEFAULT 'unknown',
  extractor_model   TEXT,
  synthesis_model   TEXT,
  embedding_model   TEXT,
  embedding_dimension INTEGER CHECK (embedding_dimension IS NULL OR embedding_dimension > 0),
  embedding_space   TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at        TIMESTAMPTZ,
  PRIMARY KEY (owner_id, id),
  UNIQUE (owner_id, id, incarnation),
  UNIQUE (owner_id, incarnation),
  CHECK (file_hash IS NULL OR file_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX ix_books_owner_live ON books (owner_id, created_at, id) WHERE deleted_at IS NULL;

CREATE TABLE source_objects (
  owner_id          UUID NOT NULL,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  storage_provider  TEXT NOT NULL,
  storage_key       TEXT NOT NULL,
  media_type        TEXT NOT NULL,
  byte_size         BIGINT NOT NULL CHECK (byte_size >= 0),
  sha256            TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  encryption_key_id TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  verified_at       TIMESTAMPTZ,
  deleted_at        TIMESTAMPTZ,
  PRIMARY KEY (owner_id, id),
  UNIQUE (owner_id, storage_provider, storage_key),
  FOREIGN KEY (owner_id, book_id, book_incarnation)
    REFERENCES books(owner_id, id, incarnation) ON DELETE CASCADE
);

CREATE INDEX ix_source_objects_owner_book
  ON source_objects (owner_id, book_id, book_incarnation) WHERE deleted_at IS NULL;

CREATE TABLE reading_state (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  bookmark          INTEGER NOT NULL DEFAULT 0 CHECK (bookmark >= 0),
  high_water_cfi    TEXT,
  current_cfi       TEXT,
  atom_set_version  INTEGER NOT NULL DEFAULT 1 CHECK (atom_set_version > 0),
  position_epoch    BIGINT NOT NULL DEFAULT 0 CHECK (position_epoch >= 0),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, book_id, book_incarnation),
  FOREIGN KEY (owner_id, book_id, book_incarnation)
    REFERENCES books(owner_id, id, incarnation) ON DELETE CASCADE
);

CREATE TABLE reader_preferences (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  theme             TEXT NOT NULL DEFAULT 'paper',
  font_family       TEXT,
  font_size         NUMERIC(6,2),
  line_height       NUMERIC(5,2),
  page_width        INTEGER,
  preferences       JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, book_id, book_incarnation),
  FOREIGN KEY (owner_id, book_id, book_incarnation)
    REFERENCES books(owner_id, id, incarnation) ON DELETE CASCADE,
  CHECK (jsonb_typeof(preferences) = 'object')
);

CREATE TABLE chapters (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  chapter_key       TEXT NOT NULL,
  revealed_at       INTEGER NOT NULL CHECK (revealed_at > 0),
  href              TEXT,
  fragment          TEXT,
  title             TEXT,
  part_label        TEXT,
  kind              TEXT NOT NULL DEFAULT 'body',
  content_hash      TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  schema_version    INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
  extractor_version TEXT,
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  retracted_at      TIMESTAMPTZ,
  PRIMARY KEY (owner_id, book_id, book_incarnation, id),
  UNIQUE (owner_id, book_id, book_incarnation, chapter_key),
  FOREIGN KEY (owner_id, book_id, book_incarnation)
    REFERENCES books(owner_id, id, incarnation) ON DELETE CASCADE
);

CREATE UNIQUE INDEX ux_chapters_owner_live_ordinal
  ON chapters (owner_id, book_id, book_incarnation, revealed_at)
  WHERE retracted_at IS NULL;

CREATE TABLE ingested_chapters (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  chapter_id        UUID NOT NULL,
  content_hash      TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  extractor_model   TEXT,
  input_tokens      BIGINT CHECK (input_tokens IS NULL OR input_tokens >= 0),
  output_tokens     BIGINT CHECK (output_tokens IS NULL OR output_tokens >= 0),
  usd               NUMERIC(20,10) CHECK (usd IS NULL OR usd >= 0),
  completed_at      TIMESTAMPTZ NOT NULL,
  retracted_at      TIMESTAMPTZ,
  PRIMARY KEY (owner_id, book_id, book_incarnation, chapter_id),
  UNIQUE (owner_id, book_id, book_incarnation, chapter_id, content_hash),
  FOREIGN KEY (owner_id, book_id, book_incarnation, chapter_id)
    REFERENCES chapters(owner_id, book_id, book_incarnation, id) ON DELETE CASCADE
);

CREATE INDEX ix_receipts_owner_frontier
  ON ingested_chapters (owner_id, book_id, book_incarnation, completed_at)
  WHERE retracted_at IS NULL;

CREATE TABLE chapter_summaries (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  source_chapter_id UUID NOT NULL,
  kind              TEXT NOT NULL DEFAULT 'chapter',
  summary           TEXT NOT NULL,
  revealed_at       INTEGER NOT NULL CHECK (revealed_at > 0),
  invalid_at        INTEGER,
  schema_version    INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
  extractor_version TEXT,
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  retracted_at      TIMESTAMPTZ,
  PRIMARY KEY (owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    DEFERRABLE INITIALLY DEFERRED,
  CHECK (invalid_at IS NULL OR invalid_at > revealed_at)
);

CREATE UNIQUE INDEX ux_summaries_owner_live
  ON chapter_summaries (owner_id, book_id, book_incarnation, source_chapter_id, kind)
  WHERE retracted_at IS NULL;

CREATE TABLE entities (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  source_chapter_id UUID NOT NULL,
  canonical_name    TEXT NOT NULL,
  entity_type       TEXT NOT NULL CHECK (entity_type IN ('character', 'place', 'faction', 'object')),
  revealed_at       INTEGER NOT NULL CHECK (revealed_at > 0),
  invalid_at        INTEGER,
  schema_version    INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
  extractor_version TEXT,
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  retracted_at      TIMESTAMPTZ,
  PRIMARY KEY (owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    DEFERRABLE INITIALLY DEFERRED,
  CHECK (invalid_at IS NULL OR invalid_at > revealed_at)
);

CREATE INDEX ix_entities_owner_visibility
  ON entities (owner_id, book_id, book_incarnation, revealed_at, invalid_at)
  WHERE retracted_at IS NULL;

CREATE TABLE aliases (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  entity_id         UUID NOT NULL,
  source_chapter_id UUID NOT NULL,
  surface_form      TEXT NOT NULL,
  revealed_at       INTEGER NOT NULL CHECK (revealed_at > 0),
  invalid_at        INTEGER,
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  retracted_at      TIMESTAMPTZ,
  PRIMARY KEY (owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation, entity_id)
    REFERENCES entities(owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    DEFERRABLE INITIALLY DEFERRED,
  CHECK (invalid_at IS NULL OR invalid_at > revealed_at)
);

CREATE INDEX ix_aliases_owner_entity_visibility
  ON aliases (owner_id, book_id, book_incarnation, entity_id, revealed_at, invalid_at)
  WHERE retracted_at IS NULL;

CREATE TABLE edges (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  source_chapter_id UUID NOT NULL,
  src_entity_id     UUID NOT NULL,
  dst_entity_id     UUID NOT NULL,
  relationship_type TEXT NOT NULL,
  label             TEXT,
  revealed_at       INTEGER NOT NULL CHECK (revealed_at > 0),
  invalid_at        INTEGER,
  schema_version    INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
  extractor_version TEXT,
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  retracted_at      TIMESTAMPTZ,
  PRIMARY KEY (owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation, src_entity_id)
    REFERENCES entities(owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation, dst_entity_id)
    REFERENCES entities(owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    DEFERRABLE INITIALLY DEFERRED,
  CHECK (src_entity_id <> dst_entity_id),
  CHECK (invalid_at IS NULL OR invalid_at > revealed_at)
);

CREATE INDEX ix_edges_owner_visibility
  ON edges (owner_id, book_id, book_incarnation, revealed_at, invalid_at)
  WHERE retracted_at IS NULL;

CREATE TABLE events (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  source_chapter_id UUID NOT NULL,
  order_idx         INTEGER NOT NULL CHECK (order_idx >= 0),
  summary           TEXT NOT NULL,
  kind              TEXT,
  revealed_at       INTEGER NOT NULL CHECK (revealed_at > 0),
  invalid_at        INTEGER,
  schema_version    INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
  extractor_version TEXT,
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  retracted_at      TIMESTAMPTZ,
  PRIMARY KEY (owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    DEFERRABLE INITIALLY DEFERRED,
  CHECK (invalid_at IS NULL OR invalid_at > revealed_at)
);

CREATE INDEX ix_events_owner_visibility
  ON events (owner_id, book_id, book_incarnation, revealed_at, invalid_at, order_idx)
  WHERE retracted_at IS NULL;

CREATE TABLE event_participants (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  event_id          UUID NOT NULL,
  entity_id         UUID NOT NULL,
  source_chapter_id UUID NOT NULL,
  role              TEXT,
  revealed_at       INTEGER NOT NULL CHECK (revealed_at > 0),
  invalid_at        INTEGER,
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  retracted_at      TIMESTAMPTZ,
  PRIMARY KEY (owner_id, book_id, book_incarnation, event_id, entity_id),
  FOREIGN KEY (owner_id, book_id, book_incarnation, event_id)
    REFERENCES events(owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation, entity_id)
    REFERENCES entities(owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    DEFERRABLE INITIALLY DEFERRED,
  CHECK (invalid_at IS NULL OR invalid_at > revealed_at)
);

CREATE INDEX ix_participants_owner_visibility
  ON event_participants (owner_id, book_id, book_incarnation, event_id, revealed_at, invalid_at)
  WHERE retracted_at IS NULL;

CREATE TABLE themes (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  source_chapter_id UUID NOT NULL,
  name              TEXT NOT NULL,
  description       TEXT,
  revealed_at       INTEGER NOT NULL CHECK (revealed_at > 0),
  invalid_at        INTEGER,
  schema_version    INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
  extractor_version TEXT,
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  retracted_at      TIMESTAMPTZ,
  PRIMARY KEY (owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    DEFERRABLE INITIALLY DEFERRED,
  CHECK (invalid_at IS NULL OR invalid_at > revealed_at)
);

CREATE INDEX ix_themes_owner_visibility
  ON themes (owner_id, book_id, book_incarnation, revealed_at, invalid_at)
  WHERE retracted_at IS NULL;

CREATE TABLE entity_state (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  entity_id         UUID NOT NULL,
  source_chapter_id UUID NOT NULL,
  status            JSONB NOT NULL,
  revealed_at       INTEGER NOT NULL CHECK (revealed_at > 0),
  invalid_at        INTEGER,
  schema_version    INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
  extractor_version TEXT,
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  retracted_at      TIMESTAMPTZ,
  PRIMARY KEY (owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation, entity_id)
    REFERENCES entities(owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    DEFERRABLE INITIALLY DEFERRED,
  CHECK (jsonb_typeof(status) = 'object'),
  CHECK (invalid_at IS NULL OR invalid_at > revealed_at)
);

CREATE INDEX ix_entity_state_owner_visibility
  ON entity_state (owner_id, book_id, book_incarnation, entity_id, revealed_at, invalid_at)
  WHERE retracted_at IS NULL;

CREATE TABLE entity_corrections (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  source_chapter_id UUID NOT NULL,
  correction_kind   TEXT NOT NULL CHECK (correction_kind IN ('split', 'merge', 'replace')),
  source_entity_ids JSONB NOT NULL,
  target_entity_ids JSONB NOT NULL,
  assignments       JSONB NOT NULL,
  reason            TEXT,
  revealed_at       INTEGER NOT NULL CHECK (revealed_at > 0),
  schema_version    INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  retracted_at      TIMESTAMPTZ,
  PRIMARY KEY (owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    DEFERRABLE INITIALLY DEFERRED,
  CHECK (jsonb_typeof(source_entity_ids) = 'array'),
  CHECK (jsonb_typeof(target_entity_ids) = 'array'),
  CHECK (jsonb_typeof(assignments) = 'object')
);

CREATE INDEX ix_corrections_owner_visibility
  ON entity_corrections (owner_id, book_id, book_incarnation, revealed_at)
  WHERE retracted_at IS NULL;

CREATE TABLE chunks (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  chapter_id        UUID NOT NULL,
  revealed_at       INTEGER NOT NULL CHECK (revealed_at > 0),
  text              TEXT NOT NULL,
  content_hash      TEXT,
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  retracted_at      TIMESTAMPTZ,
  PRIMARY KEY (owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation, chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    DEFERRABLE INITIALLY DEFERRED,
  CHECK (content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX ix_chunks_owner_visibility
  ON chunks (owner_id, book_id, book_incarnation, revealed_at, chapter_id)
  WHERE retracted_at IS NULL;

CREATE TABLE chunk_embeddings (
  owner_id             UUID NOT NULL,
  book_id              UUID NOT NULL,
  book_incarnation     UUID NOT NULL,
  chunk_id             UUID NOT NULL,
  embedding_model      TEXT NOT NULL,
  embedding_dimension  INTEGER NOT NULL CHECK (embedding_dimension > 0),
  embedding_space      TEXT NOT NULL,
  distance_metric      TEXT NOT NULL CHECK (distance_metric IN ('cosine', 'l2', 'inner_product')),
  embedding            VECTOR NOT NULL,
  recorded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  retracted_at         TIMESTAMPTZ,
  PRIMARY KEY (
    owner_id, book_id, book_incarnation, chunk_id,
    embedding_model, embedding_dimension, embedding_space, distance_metric
  ),
  FOREIGN KEY (owner_id, book_id, book_incarnation, chunk_id)
    REFERENCES chunks(owner_id, book_id, book_incarnation, id) ON DELETE CASCADE,
  CHECK (vector_dims(embedding) = embedding_dimension)
);

-- B-tree eligibility index only. ANN indexes over an unfiltered multi-owner/multi-space corpus are
-- intentionally absent; the safe exact prefilter/rank function is added in migration 0004.
CREATE INDEX ix_embeddings_owner_space
  ON chunk_embeddings (
    owner_id, book_id, book_incarnation, embedding_model, embedding_dimension, embedding_space,
    distance_metric, chunk_id
  ) WHERE retracted_at IS NULL;
