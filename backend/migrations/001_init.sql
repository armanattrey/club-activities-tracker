-- Runs automatically on first boot of an empty Postgres volume.
-- To re-run after changes: docker compose down -v && docker compose up --build

CREATE TABLE campus (
  id   SERIAL PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE users (
  id           BIGSERIAL PRIMARY KEY,
  campus_id    INT REFERENCES campus(id),
  role         TEXT NOT NULL CHECK (role IN ('student','coordinator','advisor','campus_admin','super_admin')),
  student_code TEXT UNIQUE,
  email        TEXT NOT NULL UNIQUE,
  name         TEXT NOT NULL,
  pw_hash      TEXT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_users_campus_role ON users (campus_id, role);

CREATE TABLE clubs (
  id         SERIAL PRIMARY KEY,
  campus_id  INT NOT NULL REFERENCES campus(id),
  name       TEXT NOT NULL,
  advisor_id BIGINT REFERENCES users(id),
  capacity   INT NOT NULL CHECK (capacity > 0),
  rules      JSONB NOT NULL DEFAULT '{}',
  UNIQUE (campus_id, name)
);
CREATE INDEX ix_clubs_campus ON clubs (campus_id);

CREATE TABLE club_coordinators (
  club_id INT    NOT NULL REFERENCES clubs(id) ON DELETE CASCADE,
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  PRIMARY KEY (club_id, user_id)
);

CREATE TABLE club_memberships (
  id         BIGSERIAL PRIMARY KEY,
  student_id BIGINT NOT NULL REFERENCES users(id),
  club_id    INT    NOT NULL REFERENCES clubs(id),
  type       TEXT   NOT NULL CHECK (type IN ('primary','secondary')),
  status     TEXT   NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  decided_by BIGINT REFERENCES users(id),
  decided_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- No duplicate live request for the same club (rejected rows may re-apply).
CREATE UNIQUE INDEX uq_membership_student_club
  ON club_memberships (student_id, club_id) WHERE status IN ('pending','approved');
-- Limit: 1 primary + 1 secondary per student (pending counts, so no stacking requests).
CREATE UNIQUE INDEX uq_membership_one_per_type
  ON club_memberships (student_id, type) WHERE status IN ('pending','approved');
CREATE INDEX ix_membership_club_status ON club_memberships (club_id, status);

-- ---------- Club Day ----------
CREATE TABLE club_days (
  id                  BIGSERIAL PRIMARY KEY,
  club_id             INT  NOT NULL REFERENCES clubs(id),
  day_date            DATE NOT NULL,
  title               TEXT,
  status              TEXT NOT NULL DEFAULT 'planned'
    CHECK (status IN ('planned','approved','announced','open','submissions','evaluating','closed')),
  submission_deadline TIMESTAMPTZ,
  created_by          BIGINT REFERENCES users(id)
);
CREATE INDEX ix_club_days_club_date   ON club_days (club_id, day_date);
CREATE INDEX ix_club_days_status_date ON club_days (status, day_date);  -- scheduler scans

CREATE TABLE activity_plans (
  id          BIGSERIAL PRIMARY KEY,
  club_day_id BIGINT NOT NULL UNIQUE REFERENCES club_days(id),
  body        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  approved_by BIGINT REFERENCES users(id)
);

-- ---------- Check-in ----------
CREATE TABLE checkin_sessions (
  id           BIGSERIAL PRIMARY KEY,
  club_day_id  BIGINT NOT NULL REFERENCES club_days(id),
  secret       TEXT NOT NULL,              -- HMAC key for the rotating QR token
  opens_at     TIMESTAMPTZ NOT NULL,
  closes_at    TIMESTAMPTZ NOT NULL,
  late_after   TIMESTAMPTZ NOT NULL,       -- check-ins after this are flagged late
  geo_lat      DOUBLE PRECISION,
  geo_lng      DOUBLE PRECISION,
  geo_radius_m INT
);
CREATE INDEX ix_checkin_sessions_day ON checkin_sessions (club_day_id);

CREATE TABLE attendance (
  id                BIGSERIAL PRIMARY KEY,
  student_id        BIGINT NOT NULL REFERENCES users(id),
  session_id        BIGINT NOT NULL REFERENCES checkin_sessions(id),
  checked_in_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  is_late           BOOLEAN NOT NULL DEFAULT false,
  geo_flag          BOOLEAN NOT NULL DEFAULT false,   -- flagged, never blocked
  corrected         BOOLEAN NOT NULL DEFAULT false,
  correction_reason TEXT,
  UNIQUE (student_id, session_id)          -- the real double-check-in guard
);
CREATE INDEX ix_attendance_session ON attendance (session_id);

-- ---------- Submissions & evaluation ----------
CREATE TABLE submissions (
  id           BIGSERIAL PRIMARY KEY,
  club_day_id  BIGINT NOT NULL REFERENCES club_days(id),
  student_id   BIGINT NOT NULL REFERENCES users(id),
  kind         TEXT NOT NULL CHECK (kind IN ('file','link','photo')),
  object_key   TEXT,                       -- MinIO/S3 key for files and photos
  url          TEXT,                       -- for links
  submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  is_late      BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX ix_submissions_day_student ON submissions (club_day_id, student_id);

CREATE TABLE rubric_criteria (
  id        SERIAL PRIMARY KEY,
  key       TEXT NOT NULL UNIQUE,
  max_score INT  NOT NULL DEFAULT 5
);
INSERT INTO rubric_criteria (key) VALUES ('content'), ('presentation'), ('participation');

CREATE TABLE evaluations (
  id            BIGSERIAL PRIMARY KEY,
  submission_id BIGINT NOT NULL REFERENCES submissions(id),
  evaluator_id  BIGINT NOT NULL REFERENCES users(id),
  scores        JSONB  NOT NULL,           -- {"content": 4, "presentation": 3, ...}
  comment       TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (submission_id, evaluator_id)
);

-- ---------- Events ----------
CREATE TABLE events (
  id        BIGSERIAL PRIMARY KEY,
  campus_id INT NOT NULL REFERENCES campus(id),
  type      TEXT NOT NULL CHECK (type IN ('gambade','hackathon','cross_road')),
  name      TEXT NOT NULL,
  capacity  INT NOT NULL CHECK (capacity > 0),
  team_size INT,
  starts_at TIMESTAMPTZ
);

CREATE TABLE registrations (
  id         BIGSERIAL PRIMARY KEY,
  event_id   BIGINT NOT NULL REFERENCES events(id),
  student_id BIGINT NOT NULL REFERENCES users(id),
  status     TEXT NOT NULL DEFAULT 'registered',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (event_id, student_id)
);

CREATE TABLE teams (
  id       BIGSERIAL PRIMARY KEY,
  event_id BIGINT NOT NULL REFERENCES events(id),
  name     TEXT NOT NULL
);

CREATE TABLE team_members (
  team_id    BIGINT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  event_id   BIGINT NOT NULL REFERENCES events(id),
  student_id BIGINT NOT NULL REFERENCES users(id),
  PRIMARY KEY (team_id, student_id),
  UNIQUE (event_id, student_id)            -- nobody can be in two teams for one event
);

CREATE TABLE event_results (
  id         BIGSERIAL PRIMARY KEY,
  event_id   BIGINT NOT NULL REFERENCES events(id),
  team_id    BIGINT REFERENCES teams(id),
  student_id BIGINT REFERENCES users(id),
  rank       INT,
  notes      TEXT
);

-- ---------- Certificates ----------
CREATE TABLE certificates (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id   BIGINT NOT NULL REFERENCES users(id),
  kind         TEXT NOT NULL CHECK (kind IN ('participation','achievement')),
  ref_type     TEXT NOT NULL,              -- 'club_day' | 'event'
  ref_id       BIGINT NOT NULL,
  content_hmac TEXT NOT NULL,              -- HMAC over canonical fields
  pdf_sha256   TEXT,                       -- hash of the actual PDF bytes
  object_key   TEXT,
  issued_by    BIGINT REFERENCES users(id),
  issued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at   TIMESTAMPTZ,
  UNIQUE (student_id, kind, ref_type, ref_id)
);

-- ---------- Notifications (outbox) & audit ----------
CREATE TABLE notifications (
  id      BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  kind    TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}',
  send_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  sent_at TIMESTAMPTZ
);
CREATE INDEX ix_notifications_pending ON notifications (send_at) WHERE sent_at IS NULL;

CREATE TABLE audit_log (
  id           BIGSERIAL PRIMARY KEY,
  actor_id     BIGINT REFERENCES users(id),
  action       TEXT NOT NULL,
  entity       TEXT NOT NULL,
  entity_id    TEXT NOT NULL,
  before_state JSONB,
  after_state  JSONB,
  reason       TEXT,
  at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Corrections must always carry a reason.
  CONSTRAINT correction_needs_reason
    CHECK (action NOT LIKE '%.correct' OR (reason IS NOT NULL AND length(trim(reason)) > 0))
);
CREATE INDEX ix_audit_entity ON audit_log (entity, entity_id);