-- LIT-38: durable work/cost boundaries, encrypted BYOK fields, annotations, and audit foundations.
CREATE TABLE provider_credentials (
  owner_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  id                   UUID NOT NULL DEFAULT gen_random_uuid(),
  provider             TEXT NOT NULL,
  masked_label         TEXT NOT NULL,
  ciphertext           BYTEA NOT NULL,
  encrypted_data_key   BYTEA NOT NULL,
  encryption_algorithm TEXT NOT NULL,
  key_version          TEXT NOT NULL,
  nonce                BYTEA NOT NULL,
  metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  rotated_at           TIMESTAMPTZ,
  disabled_at          TIMESTAMPTZ,
  deleted_at           TIMESTAMPTZ,
  PRIMARY KEY (owner_id, id),
  CHECK (octet_length(ciphertext) > 0),
  CHECK (octet_length(encrypted_data_key) > 0),
  CHECK (octet_length(nonce) > 0),
  CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE UNIQUE INDEX ux_provider_credentials_owner_label_live
  ON provider_credentials (owner_id, provider, masked_label) WHERE deleted_at IS NULL;

CREATE TABLE provider_model_settings (
  owner_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  provider          TEXT NOT NULL,
  capability        TEXT NOT NULL CHECK (capability IN ('extraction', 'synthesis', 'embedding', 'judge')),
  credential_id     UUID,
  model             TEXT NOT NULL,
  base_url          TEXT,
  embedding_dimension INTEGER CHECK (embedding_dimension IS NULL OR embedding_dimension > 0),
  embedding_space   TEXT,
  settings          JSONB NOT NULL DEFAULT '{}'::jsonb,
  enabled           BOOLEAN NOT NULL DEFAULT true,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, id),
  UNIQUE (owner_id, provider, capability),
  FOREIGN KEY (owner_id, credential_id)
    REFERENCES provider_credentials(owner_id, id),
  CHECK (jsonb_typeof(settings) = 'object')
);

CREATE INDEX ix_provider_settings_owner_credential
  ON provider_model_settings (owner_id, credential_id) WHERE credential_id IS NOT NULL;

CREATE TABLE jobs (
  owner_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  book_id           UUID,
  book_incarnation  UUID,
  credential_id     UUID,
  kind              TEXT NOT NULL,
  state             TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'leased', 'running', 'succeeded', 'failed', 'cancelled')),
  idempotency_key   TEXT NOT NULL,
  payload_metadata  JSONB NOT NULL DEFAULT '{}'::jsonb,
  attempt_count     INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts      INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
  priority          INTEGER NOT NULL DEFAULT 0,
  run_after         TIMESTAMPTZ NOT NULL DEFAULT now(),
  cancellation_requested_at TIMESTAMPTZ,
  sanitized_error   JSONB,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at      TIMESTAMPTZ,
  PRIMARY KEY (owner_id, id),
  UNIQUE (owner_id, idempotency_key),
  FOREIGN KEY (owner_id, book_id, book_incarnation)
    REFERENCES books(owner_id, id, incarnation),
  FOREIGN KEY (owner_id, credential_id)
    REFERENCES provider_credentials(owner_id, id),
  CHECK (jsonb_typeof(payload_metadata) = 'object'),
  CHECK (sanitized_error IS NULL OR jsonb_typeof(sanitized_error) = 'object'),
  CHECK ((book_id IS NULL) = (book_incarnation IS NULL)),
  CHECK (attempt_count <= max_attempts)
);

CREATE INDEX ix_jobs_owner_claim
  ON jobs (owner_id, state, priority DESC, run_after, created_at)
  WHERE state IN ('pending', 'leased', 'running');

CREATE TABLE job_attempts (
  owner_id          UUID NOT NULL,
  job_id            UUID NOT NULL,
  attempt_no        INTEGER NOT NULL CHECK (attempt_no > 0),
  worker_id         TEXT NOT NULL,
  lease_token_digest BYTEA NOT NULL,
  leased_at         TIMESTAMPTZ NOT NULL,
  lease_expires_at  TIMESTAMPTZ NOT NULL,
  heartbeat_at      TIMESTAMPTZ,
  started_at        TIMESTAMPTZ,
  finished_at       TIMESTAMPTZ,
  outcome           TEXT CHECK (outcome IN ('succeeded', 'failed', 'expired', 'cancelled')),
  sanitized_error   JSONB,
  PRIMARY KEY (owner_id, job_id, attempt_no),
  FOREIGN KEY (owner_id, job_id) REFERENCES jobs(owner_id, id) ON DELETE CASCADE,
  CHECK (lease_expires_at > leased_at),
  CHECK (sanitized_error IS NULL OR jsonb_typeof(sanitized_error) = 'object')
);

CREATE INDEX ix_job_attempts_owner_lease
  ON job_attempts (owner_id, lease_expires_at) WHERE finished_at IS NULL;

CREATE TABLE cost_ledger (
  owner_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  book_id           UUID,
  book_incarnation  UUID,
  job_id            UUID,
  chapter_ordinal   INTEGER CHECK (chapter_ordinal IS NULL OR chapter_ordinal > 0),
  phase             TEXT NOT NULL CHECK (phase IN ('extraction', 'synthesis', 'embedding', 'judge')),
  provider          TEXT,
  model             TEXT,
  input_tokens      BIGINT CHECK (input_tokens IS NULL OR input_tokens >= 0),
  output_tokens     BIGINT CHECK (output_tokens IS NULL OR output_tokens >= 0),
  usd               NUMERIC(20,10) CHECK (usd IS NULL OR usd >= 0),
  idempotency_key   TEXT NOT NULL,
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, id),
  UNIQUE (owner_id, idempotency_key),
  FOREIGN KEY (owner_id, book_id, book_incarnation)
    REFERENCES books(owner_id, id, incarnation),
  FOREIGN KEY (owner_id, job_id) REFERENCES jobs(owner_id, id),
  CHECK ((book_id IS NULL) = (book_incarnation IS NULL))
);

CREATE INDEX ix_cost_ledger_owner_time ON cost_ledger (owner_id, recorded_at, id);
CREATE INDEX ix_cost_ledger_owner_book
  ON cost_ledger (owner_id, book_id, book_incarnation) WHERE book_id IS NOT NULL;

CREATE TABLE cost_reservations (
  owner_id              UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  id                    UUID NOT NULL DEFAULT gen_random_uuid(),
  book_id               UUID,
  book_incarnation      UUID,
  job_id                UUID,
  chapter_ordinal       INTEGER CHECK (chapter_ordinal IS NULL OR chapter_ordinal > 0),
  phase                 TEXT NOT NULL CHECK (phase IN ('extraction', 'synthesis', 'embedding', 'judge')),
  provider              TEXT,
  model                 TEXT,
  reserved_input_tokens BIGINT NOT NULL CHECK (reserved_input_tokens >= 0),
  reserved_output_tokens BIGINT NOT NULL CHECK (reserved_output_tokens >= 0),
  reserved_usd          NUMERIC(20,10) NOT NULL CHECK (reserved_usd >= 0),
  actual_input_tokens   BIGINT CHECK (actual_input_tokens IS NULL OR actual_input_tokens >= 0),
  actual_output_tokens  BIGINT CHECK (actual_output_tokens IS NULL OR actual_output_tokens >= 0),
  actual_usd            NUMERIC(20,10) CHECK (actual_usd IS NULL OR actual_usd >= 0),
  state                 TEXT NOT NULL DEFAULT 'reserved'
                        CHECK (state IN ('reserved', 'settled', 'released', 'reconciled')),
  idempotency_key       TEXT NOT NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  settled_at            TIMESTAMPTZ,
  PRIMARY KEY (owner_id, id),
  UNIQUE (owner_id, idempotency_key),
  FOREIGN KEY (owner_id, book_id, book_incarnation)
    REFERENCES books(owner_id, id, incarnation),
  FOREIGN KEY (owner_id, job_id) REFERENCES jobs(owner_id, id),
  CHECK ((book_id IS NULL) = (book_incarnation IS NULL))
);

CREATE INDEX ix_cost_reservations_owner_open
  ON cost_reservations (owner_id, created_at, id) WHERE state = 'reserved';

CREATE TABLE highlights (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  anchor            JSONB NOT NULL,
  color             TEXT,
  selected_text     TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at        TIMESTAMPTZ,
  PRIMARY KEY (owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation)
    REFERENCES books(owner_id, id, incarnation) ON DELETE CASCADE,
  CHECK (jsonb_typeof(anchor) = 'object')
);

CREATE INDEX ix_highlights_owner_book
  ON highlights (owner_id, book_id, book_incarnation, created_at) WHERE deleted_at IS NULL;

CREATE TABLE annotations (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  highlight_id      UUID,
  anchor            JSONB NOT NULL,
  body              TEXT NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at        TIMESTAMPTZ,
  PRIMARY KEY (owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation)
    REFERENCES books(owner_id, id, incarnation) ON DELETE CASCADE,
  FOREIGN KEY (owner_id, book_id, book_incarnation, highlight_id)
    REFERENCES highlights(owner_id, book_id, book_incarnation, id),
  CHECK (jsonb_typeof(anchor) = 'object')
);

CREATE INDEX ix_annotations_owner_book
  ON annotations (owner_id, book_id, book_incarnation, created_at) WHERE deleted_at IS NULL;

CREATE TABLE bookmarks (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  anchor            JSONB NOT NULL,
  label             TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at        TIMESTAMPTZ,
  PRIMARY KEY (owner_id, book_id, book_incarnation, id),
  FOREIGN KEY (owner_id, book_id, book_incarnation)
    REFERENCES books(owner_id, id, incarnation) ON DELETE CASCADE,
  CHECK (jsonb_typeof(anchor) = 'object')
);

CREATE INDEX ix_bookmarks_owner_book
  ON bookmarks (owner_id, book_id, book_incarnation, created_at) WHERE deleted_at IS NULL;

CREATE TABLE audit_events (
  owner_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  actor_kind        TEXT NOT NULL,
  action            TEXT NOT NULL,
  target_kind       TEXT NOT NULL,
  target_id         UUID,
  result            TEXT NOT NULL,
  request_id        UUID,
  metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
  occurred_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, id),
  CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE INDEX ix_audit_events_owner_time
  ON audit_events (owner_id, occurred_at, id);
