-- LIT-46: exactly one explicit provider/model selection per owner and capability.
ALTER TABLE provider_model_settings
  DROP CONSTRAINT provider_model_settings_owner_id_provider_capability_key;

ALTER TABLE provider_model_settings
  ADD CONSTRAINT ux_provider_model_settings_owner_capability UNIQUE (owner_id, capability),
  ADD COLUMN validation_status TEXT NOT NULL DEFAULT 'unchecked'
    CHECK (validation_status IN ('unchecked', 'ready', 'offline', 'invalid')),
  ADD COLUMN validation_error_code TEXT
    CHECK (validation_error_code IS NULL OR validation_error_code IN (
      'invalid_credentials', 'unavailable_model', 'network_error', 'service_error'
    )),
  ADD COLUMN validated_at TIMESTAMPTZ,
  ADD CONSTRAINT ck_provider_model_settings_provider
    CHECK (provider IN ('openai-compatible', 'anthropic', 'offline')),
  ADD CONSTRAINT ck_provider_model_settings_model
    CHECK (model ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$'),
  ADD CONSTRAINT ck_provider_model_settings_offline_shape
    CHECK (
      (provider = 'offline' AND credential_id IS NULL AND base_url IS NULL AND model = 'offline') OR
      (provider <> 'offline' AND credential_id IS NOT NULL AND base_url IS NOT NULL)
    ),
  ADD CONSTRAINT ck_provider_model_settings_anthropic_embedding
    CHECK (NOT (provider = 'anthropic' AND capability = 'embedding')),
  ADD CONSTRAINT ck_provider_model_settings_validation_shape
    CHECK (
      (validation_status = 'invalid' AND validation_error_code IS NOT NULL AND validated_at IS NOT NULL) OR
      (validation_status IN ('ready','offline') AND validation_error_code IS NULL AND validated_at IS NOT NULL) OR
      (validation_status = 'unchecked' AND validation_error_code IS NULL AND validated_at IS NULL)
    ),
  ADD CONSTRAINT ck_provider_model_settings_public_settings
    CHECK (settings = '{}'::jsonb);

DROP INDEX ix_provider_settings_owner_credential;
CREATE INDEX ix_provider_settings_owner_credential
  ON provider_model_settings (owner_id, credential_id) WHERE credential_id IS NOT NULL;

ALTER TABLE jobs DROP CONSTRAINT jobs_state_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_state_check
  CHECK (state IN (
    'waiting_configuration','pending','leased','running','succeeded','failed','cancelled'
  ));
