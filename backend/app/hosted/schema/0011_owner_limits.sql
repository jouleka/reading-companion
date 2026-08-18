-- LIT-47: shared per-owner availability and spend policy.
CREATE TABLE owner_limits (
  owner_id                    UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  max_upload_bytes            BIGINT NOT NULL DEFAULT 134217728 CHECK (max_upload_bytes > 0),
  max_library_bytes           BIGINT NOT NULL DEFAULT 5368709120 CHECK (max_library_bytes > 0),
  max_books                   INTEGER NOT NULL DEFAULT 100 CHECK (max_books > 0),
  max_active_jobs             INTEGER NOT NULL DEFAULT 3 CHECK (max_active_jobs > 0),
  requests_per_window         INTEGER NOT NULL DEFAULT 600 CHECK (requests_per_window > 0),
  request_window_seconds      INTEGER NOT NULL DEFAULT 60
                              CHECK (request_window_seconds BETWEEN 1 AND 3600),
  max_provider_concurrency    INTEGER NOT NULL DEFAULT 2
                              CHECK (max_provider_concurrency > 0),
  max_spend_usd               NUMERIC(20,10)
                              CHECK (max_spend_usd IS NULL OR max_spend_usd >= 0),
  updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO owner_limits (owner_id) SELECT id FROM users ON CONFLICT DO NOTHING;

-- A table-owner rewrite rule gives every later OIDC-created user the same explicit defaults without
-- granting the authentication runtime any direct access to owner_limits.
CREATE RULE users_ensure_owner_limits AS ON INSERT TO users DO ALSO
  INSERT INTO owner_limits (owner_id) VALUES (NEW.id);

CREATE TABLE owner_request_windows (
  owner_id          UUID PRIMARY KEY REFERENCES owner_limits(owner_id) ON DELETE CASCADE,
  window_started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  request_count     INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE owner_limits ENABLE ROW LEVEL SECURITY;
ALTER TABLE owner_limits FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_limits_owner_isolation ON owner_limits
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE owner_request_windows ENABLE ROW LEVEL SECURITY;
ALTER TABLE owner_request_windows FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_request_windows_owner_isolation ON owner_request_windows
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

CREATE INDEX ix_jobs_owner_active_limit ON jobs (owner_id, state)
  WHERE state IN ('waiting_configuration','pending','leased','running');
CREATE INDEX ix_source_objects_owner_live_bytes ON source_objects (owner_id, byte_size)
  WHERE deleted_at IS NULL;
