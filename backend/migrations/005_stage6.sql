-- Stage 6: read-only rollup views for the dashboards. Safe to run more than once.
CREATE OR REPLACE VIEW v_club_stats AS
SELECT c.id AS club_id, c.name AS club_name, c.campus_id, c.advisor_id, c.capacity,
  (SELECT count(*) FROM club_memberships m
     WHERE m.club_id = c.id AND m.status = 'approved') AS members,
  (SELECT count(*) FROM club_days d
     WHERE d.club_id = c.id AND d.status <> 'planned') AS club_days,
  (SELECT max(d.day_date) FROM club_days d
     WHERE d.club_id = c.id AND d.status <> 'planned') AS last_club_day,
  (SELECT count(*) FROM attendance a
     JOIN checkin_sessions s ON s.id = a.session_id
     JOIN club_days d ON d.id = s.club_day_id
     WHERE d.club_id = c.id) AS attendance_records,
  (SELECT count(*) FROM submissions sb
     JOIN club_days d ON d.id = sb.club_day_id
     WHERE d.club_id = c.id) AS submissions,
  (SELECT round(avg((SELECT sum(x.value::numeric) FROM jsonb_each_text(e.scores) AS x)), 2)
     FROM evaluations e
     JOIN submissions sb ON sb.id = e.submission_id
     JOIN club_days d ON d.id = sb.club_day_id
     WHERE d.club_id = c.id) AS avg_score,
  (SELECT count(*) FROM certificates ce
     JOIN club_days d ON d.id = ce.ref_id
     WHERE ce.ref_type = 'club_day' AND ce.revoked_at IS NULL AND d.club_id = c.id) AS certificates
FROM clubs c;

CREATE OR REPLACE VIEW v_student_stats AS
SELECT u.id AS student_id, u.name, u.student_code, u.campus_id,
  (SELECT count(*) FROM attendance a WHERE a.student_id = u.id) AS attendance_records,
  (SELECT count(*) FROM submissions s WHERE s.student_id = u.id) AS submissions,
  (SELECT count(*) FROM registrations r WHERE r.student_id = u.id) AS event_registrations,
  (SELECT count(*) FROM certificates ce
     WHERE ce.student_id = u.id AND ce.revoked_at IS NULL) AS certificates
FROM users u WHERE u.role = 'student';
