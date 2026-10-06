-- In-app notification inbox and idempotent scheduled reminders.
ALTER TABLE notifications
  ADD COLUMN IF NOT EXISTS dedupe_key TEXT,
  ADD COLUMN IF NOT EXISTS read_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS uq_notifications_dedupe_key
  ON notifications (dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_notifications_user_unread
  ON notifications (user_id, send_at DESC) WHERE read_at IS NULL;
