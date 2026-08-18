-- LIT-49: immutable, content-free security audit vocabulary.
ALTER TABLE audit_events
  ADD CONSTRAINT ck_audit_events_actor_kind
    CHECK (actor_kind IN ('owner', 'worker', 'system')),
  ADD CONSTRAINT ck_audit_events_action
    CHECK (action ~ '^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,31}$'),
  ADD CONSTRAINT ck_audit_events_target_kind
    CHECK (target_kind ~ '^[a-z][a-z0-9_]{0,31}$'),
  ADD CONSTRAINT ck_audit_events_result
    CHECK (result IN ('succeeded', 'denied', 'failed')),
  ADD CONSTRAINT ck_audit_events_metadata_keys
    CHECK (
      metadata - ARRAY['reason_code']::text[] = '{}'::jsonb
      AND (
        NOT (metadata ? 'reason_code')
        OR (
          jsonb_typeof(metadata->'reason_code') = 'string'
          AND metadata->>'reason_code' ~ '^[a-z][a-z0-9_]{0,63}$'
        )
      )
    );
