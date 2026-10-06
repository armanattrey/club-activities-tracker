-- Stage 3: file metadata on submissions. Safe to run more than once.
ALTER TABLE submissions
  ADD COLUMN IF NOT EXISTS original_filename TEXT,
  ADD COLUMN IF NOT EXISTS content_type      TEXT,
  ADD COLUMN IF NOT EXISTS size_bytes        BIGINT,
  ADD COLUMN IF NOT EXISTS sha256            TEXT;