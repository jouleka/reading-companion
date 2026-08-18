-- LIT-38: service extensions and hosted identity/account persistence.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE users (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  display_name   TEXT NOT NULL,
  email          TEXT,
  status         TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'suspended', 'deleting', 'deleted')),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at     TIMESTAMPTZ,
  UNIQUE (id, status)
);

CREATE UNIQUE INDEX ux_users_email_active
  ON users (lower(email)) WHERE email IS NOT NULL AND deleted_at IS NULL;

CREATE TABLE external_identities (
  owner_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  id             UUID NOT NULL DEFAULT gen_random_uuid(),
  issuer         TEXT NOT NULL,
  subject        TEXT NOT NULL,
  email          TEXT,
  email_verified BOOLEAN NOT NULL DEFAULT false,
  linked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_login_at  TIMESTAMPTZ,
  PRIMARY KEY (owner_id, id),
  UNIQUE (owner_id, issuer, subject),
  -- OIDC identity is the deliberate account-boundary uniqueness exception: one external
  -- identity may never be linked to two internal owners.
  UNIQUE (issuer, subject)
);

CREATE INDEX ix_external_identities_owner
  ON external_identities (owner_id, issuer, subject);

CREATE TABLE sessions (
  owner_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  session_digest    BYTEA NOT NULL,
  csrf_digest       BYTEA NOT NULL,
  oidc_issuer       TEXT,
  user_agent_hash   BYTEA,
  ip_prefix_hash    BYTEA,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at        TIMESTAMPTZ NOT NULL,
  rotated_at        TIMESTAMPTZ,
  revoked_at        TIMESTAMPTZ,
  PRIMARY KEY (owner_id, id),
  -- Session bootstrap is the other deliberate account-boundary exception: the opaque cookie
  -- digest must resolve to exactly one owner before app.owner_id exists.
  UNIQUE (session_digest),
  UNIQUE (owner_id, session_digest),
  CHECK (expires_at > created_at)
);

CREATE INDEX ix_sessions_owner_live
  ON sessions (owner_id, expires_at) WHERE revoked_at IS NULL;

-- Service-scoped capability metadata contains no tenant content or secret material.
CREATE TABLE provider_capabilities (
  provider             TEXT NOT NULL,
  capability_version   TEXT NOT NULL,
  capabilities         JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (provider, capability_version),
  CHECK (jsonb_typeof(capabilities) = 'object')
);
