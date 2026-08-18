-- LIT-50: resumable, owner-bound local library imports.
CREATE TABLE local_library_migrations (
  owner_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  source_book_id    TEXT NOT NULL,
  source_checksum   TEXT NOT NULL CHECK (source_checksum ~ '^[0-9a-f]{64}$'),
  plan_checksum     TEXT NOT NULL CHECK (plan_checksum ~ '^[0-9a-f]{64}$'),
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  source_object_id  UUID NOT NULL,
  status            TEXT NOT NULL CHECK (status IN ('importing','complete','rolling_back')),
  report            JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(report)='object'),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at      TIMESTAMPTZ,
  PRIMARY KEY (owner_id, source_book_id),
  UNIQUE (owner_id, source_checksum),
  UNIQUE (owner_id, book_id, book_incarnation),
  UNIQUE (owner_id, source_object_id)
);

ALTER TABLE local_library_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE local_library_migrations FORCE ROW LEVEL SECURITY;
CREATE POLICY local_library_migrations_owner_isolation ON local_library_migrations
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());
