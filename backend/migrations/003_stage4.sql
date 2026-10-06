-- Stage 4: certificate generation state. Safe to run more than once.
ALTER TABLE certificates
  ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending','generating','ready','failed')),
  ADD COLUMN IF NOT EXISTS snapshot JSONB,
  ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS revoke_reason TEXT;

CREATE INDEX IF NOT EXISTS ix_certificates_pending
  ON certificates (issued_at) WHERE status = 'pending';