-- LIT-53: versioned cross-device resume state with an explicit rewind epoch.
ALTER TABLE reading_state
  ADD COLUMN current_offset BIGINT NOT NULL DEFAULT 0 CHECK (current_offset >= 0),
  ADD COLUMN high_water_offset BIGINT NOT NULL DEFAULT 0 CHECK (high_water_offset >= 0),
  ADD COLUMN position_version BIGINT NOT NULL DEFAULT 0 CHECK (position_version >= 0),
  ADD COLUMN last_client_id UUID,
  ADD COLUMN last_client_sequence BIGINT CHECK (last_client_sequence > 0),
  ADD COLUMN last_opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD CONSTRAINT reading_state_offset_order_ck CHECK (high_water_offset >= current_offset),
  ADD CONSTRAINT reading_state_client_clock_ck CHECK (
    (last_client_id IS NULL AND last_client_sequence IS NULL)
    OR (last_client_id IS NOT NULL AND last_client_sequence IS NOT NULL)
  );

CREATE INDEX ix_reading_state_owner_last_opened
  ON reading_state (owner_id, last_opened_at DESC, book_id);
