-- LIT-44: close the durable ingestion job state/lease/error contract.
ALTER TABLE jobs
  ADD CONSTRAINT jobs_kind_ck
    CHECK (kind = 'ingest_book'),
  ADD CONSTRAINT jobs_idempotency_key_ck
    CHECK (idempotency_key ~ '^[a-z0-9][a-z0-9:._-]{0,255}$'),
  ADD CONSTRAINT jobs_terminal_timestamp_ck
    CHECK ((state IN ('succeeded', 'failed', 'cancelled')) = (completed_at IS NOT NULL)),
  ADD CONSTRAINT jobs_failure_shape_ck
    CHECK (
      sanitized_error IS NULL OR (
        sanitized_error ? 'code'
        AND sanitized_error ? 'retryable'
        AND jsonb_typeof(sanitized_error->'code') = 'string'
        AND sanitized_error->>'code' IN (
          'attempts_exhausted','budget_exceeded','cancelled','internal_error',
          'invalid_model_output','provider_rejected','provider_unavailable',
          'source_integrity','source_missing'
        )
        AND jsonb_typeof(sanitized_error->'retryable') = 'boolean'
        AND sanitized_error - ARRAY['code', 'retryable'] = '{}'::jsonb
      )
    ),
  ADD CONSTRAINT jobs_payload_has_no_credentials_ck
    CHECK (
      NOT (payload_metadata ?| ARRAY[
        'credential', 'credential_id', 'api_key', 'secret', 'token', 'access_token'
      ])
    ),
  ADD CONSTRAINT jobs_ingest_chapter_count_ck
    CHECK (
      kind <> 'ingest_book' OR (
        payload_metadata ? 'chapter_count'
        AND
        jsonb_typeof(payload_metadata->'chapter_count') = 'number'
        AND (payload_metadata->>'chapter_count') ~ '^[1-9][0-9]{0,8}$'
      )
    );

ALTER TABLE job_attempts
  ADD CONSTRAINT job_attempts_lease_digest_ck
    CHECK (octet_length(lease_token_digest) = 32),
  ADD CONSTRAINT job_attempts_worker_id_ck
    CHECK (worker_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
  ADD CONSTRAINT job_attempts_finish_shape_ck
    CHECK ((finished_at IS NULL) = (outcome IS NULL)),
  ADD CONSTRAINT job_attempts_failure_shape_ck
    CHECK (
      sanitized_error IS NULL OR (
        sanitized_error ? 'code'
        AND sanitized_error ? 'retryable'
        AND jsonb_typeof(sanitized_error->'code') = 'string'
        AND sanitized_error->>'code' IN (
          'attempts_exhausted','budget_exceeded','cancelled','internal_error',
          'invalid_model_output','provider_rejected','provider_unavailable',
          'source_integrity','source_missing'
        )
        AND jsonb_typeof(sanitized_error->'retryable') = 'boolean'
        AND sanitized_error - ARRAY['code', 'retryable'] = '{}'::jsonb
      )
    );

CREATE UNIQUE INDEX ux_job_attempts_one_active_lease
  ON job_attempts (owner_id, job_id)
  WHERE finished_at IS NULL;

CREATE INDEX ix_jobs_global_claim
  ON jobs (priority DESC, run_after, created_at, id)
  WHERE state = 'pending' AND cancellation_requested_at IS NULL;
