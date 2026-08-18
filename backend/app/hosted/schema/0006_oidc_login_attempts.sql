-- LIT-40: short-lived, one-time OIDC authorization attempts.
-- This service bootstrap table contains no tenant content and intentionally has no owner_id/RLS:
-- the user is not known until a validated ID token resolves (issuer, subject).
CREATE TABLE oidc_login_attempts (
  state_digest      BYTEA PRIMARY KEY CHECK (octet_length(state_digest) = 32),
  browser_digest    BYTEA NOT NULL CHECK (octet_length(browser_digest) = 32),
  issuer            TEXT NOT NULL,
  code_verifier     TEXT NOT NULL CHECK (length(code_verifier) BETWEEN 43 AND 128),
  nonce             TEXT NOT NULL CHECK (length(nonce) >= 43),
  return_to         TEXT NOT NULL CHECK (left(return_to, 1) = '/' AND left(return_to, 2) <> '//'),
  created_at        TIMESTAMPTZ NOT NULL,
  expires_at        TIMESTAMPTZ NOT NULL,
  CHECK (expires_at > created_at)
);

CREATE INDEX ix_oidc_login_attempts_expiry ON oidc_login_attempts (expires_at);

-- Email is profile data, never an account-linking or authorization key. Different verified
-- (issuer, subject) identities may legitimately report the same address.
DROP INDEX ux_users_email_active;
CREATE INDEX ix_users_email_profile ON users (lower(email))
  WHERE email IS NOT NULL AND deleted_at IS NULL;
