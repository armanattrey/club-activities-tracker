-- Problem 5 completion: campus approval, calendar details/import and event catalog.
ALTER TABLE clubs
  ADD COLUMN IF NOT EXISTS approval_status TEXT NOT NULL DEFAULT 'approved',
  ADD COLUMN IF NOT EXISTS approved_by BIGINT REFERENCES users(id),
  ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS created_by BIGINT REFERENCES users(id);

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='clubs_approval_status_check') THEN
    ALTER TABLE clubs ADD CONSTRAINT clubs_approval_status_check
      CHECK (approval_status IN ('pending','approved','rejected'));
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_clubs_approval_campus
  ON clubs (campus_id, approval_status, id);

ALTER TABLE activity_plans
  ADD COLUMN IF NOT EXISTS venue TEXT;

CREATE TABLE IF NOT EXISTS club_day_presenters (
  id BIGSERIAL PRIMARY KEY,
  club_day_id BIGINT NOT NULL REFERENCES club_days(id) ON DELETE CASCADE,
  student_id BIGINT NOT NULL REFERENCES users(id),
  title TEXT NOT NULL,
  notes TEXT,
  marked_by BIGINT NOT NULL REFERENCES users(id),
  marked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (club_day_id, student_id)
);

ALTER TABLE events DROP CONSTRAINT IF EXISTS events_type_check;
ALTER TABLE events ADD CONSTRAINT events_type_check
  CHECK (type IN ('gambade','hackathon','cross_road','international_conference','other'));
