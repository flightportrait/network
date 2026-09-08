CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  callsign TEXT NOT NULL,
  origin TEXT NOT NULL,
  dest TEXT NOT NULL,
  note TEXT,
  handle TEXT,
  key_name TEXT,
  valid_from TEXT
);
CREATE INDEX IF NOT EXISTS ix_submissions_received ON submissions (received_at);
