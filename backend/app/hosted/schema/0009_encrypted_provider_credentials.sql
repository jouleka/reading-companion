-- LIT-45: the predeclared credential fields become a strict envelope-encryption contract.
DROP INDEX IF EXISTS ux_provider_credentials_owner_label_live;

CREATE INDEX ix_provider_credentials_owner_live
  ON provider_credentials (owner_id, created_at, id) WHERE deleted_at IS NULL;

ALTER TABLE provider_credentials
  ADD CONSTRAINT ck_provider_credentials_provider_identifier
    CHECK (provider ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
  ADD CONSTRAINT ck_provider_credentials_masked_label
    CHECK (char_length(masked_label) BETWEEN 5 AND 32),
  ADD CONSTRAINT ck_provider_credentials_encryption_algorithm
    CHECK (encryption_algorithm = 'AES-256-GCM/AES-256-GCM'),
  ADD CONSTRAINT ck_provider_credentials_key_version
    CHECK (key_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
  ADD CONSTRAINT ck_provider_credentials_nonce
    CHECK (octet_length(nonce) = 12),
  ADD CONSTRAINT ck_provider_credentials_live_envelope
    CHECK (
      deleted_at IS NOT NULL OR (
        octet_length(ciphertext) >= 17 AND
        octet_length(encrypted_data_key) = 60
      )
    ),
  ADD CONSTRAINT ck_provider_credentials_deleted_destroyed
    CHECK (
      deleted_at IS NULL OR (
        disabled_at IS NOT NULL AND
        octet_length(ciphertext) = 1 AND
        octet_length(encrypted_data_key) = 1
      )
    );
