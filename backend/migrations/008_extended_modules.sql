-- Optional Problem 5 modules: inter-college duty leave, budgets, and galleries.
ALTER TABLE events
  ADD COLUMN IF NOT EXISTS is_inter_college BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS approved_by BIGINT REFERENCES users(id),
  ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS club_budget_requests (
  id BIGSERIAL PRIMARY KEY,
  club_id INT NOT NULL REFERENCES clubs(id),
  requester_id BIGINT NOT NULL REFERENCES users(id),
  title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 3 AND 160),
  description TEXT NOT NULL CHECK (length(trim(description)) BETWEEN 3 AND 4000),
  amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
  currency CHAR(3) NOT NULL DEFAULT 'INR',
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  decided_by BIGINT REFERENCES users(id),
  decided_at TIMESTAMPTZ,
  decision_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_budget_requests_club_status
  ON club_budget_requests (club_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS duty_leave_requests (
  id BIGSERIAL PRIMARY KEY,
  event_id BIGINT NOT NULL REFERENCES events(id),
  student_id BIGINT NOT NULL REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  decided_by BIGINT REFERENCES users(id),
  decided_at TIMESTAMPTZ,
  decision_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (event_id, student_id)
);
CREATE INDEX IF NOT EXISTS ix_duty_leave_campus_queue
  ON duty_leave_requests (student_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS event_media (
  id BIGSERIAL PRIMARY KEY,
  event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  uploaded_by BIGINT NOT NULL REFERENCES users(id),
  object_key TEXT NOT NULL UNIQUE,
  original_filename TEXT NOT NULL,
  content_type TEXT,
  size_bytes BIGINT NOT NULL CHECK (size_bytes > 0),
  sha256 TEXT NOT NULL,
  consent_to_publish BOOLEAN NOT NULL DEFAULT false,
  moderation_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (moderation_status IN ('pending','approved','rejected')),
  moderation_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_event_media_gallery
  ON event_media (event_id, moderation_status, consent_to_publish, created_at DESC);
