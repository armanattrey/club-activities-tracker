-- Stage 5: event check-in, team/result integrity. Safe to run more than once.
ALTER TABLE registrations ADD COLUMN IF NOT EXISTS checked_in_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS ix_registrations_student ON registrations (student_id);
CREATE INDEX IF NOT EXISTS ix_teams_event ON teams (event_id);

-- Team names are unique per event, ignoring case.
CREATE UNIQUE INDEX IF NOT EXISTS uq_team_name_per_event ON teams (event_id, lower(name));

-- A result row is for a team OR a student, never both and never neither,
-- and each team/student appears at most once per event.
DO $$ BEGIN
  ALTER TABLE event_results ADD CONSTRAINT ck_result_one_target
    CHECK ((team_id IS NULL) <> (student_id IS NULL));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS uq_result_team
  ON event_results (event_id, team_id) WHERE team_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_result_student
  ON event_results (event_id, student_id) WHERE student_id IS NOT NULL;