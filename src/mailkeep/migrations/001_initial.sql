CREATE TABLE accounts (
  account_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  username TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE archives (
  archive_id TEXT PRIMARY KEY,
  raw_sha256 TEXT NOT NULL UNIQUE,
  object_relpath TEXT NOT NULL UNIQUE,
  message_id TEXT,
  subject TEXT NOT NULL,
  message_date TEXT,
  date_source TEXT NOT NULL,
  preservation_state TEXT NOT NULL DEFAULT 'discovered'
    CHECK (preservation_state IN ('discovered','preserving','preserved','preservation_verified')),
  enrichment_state TEXT NOT NULL DEFAULT 'pending'
    CHECK (enrichment_state IN ('pending','complete','failed','skipped')),
  markdown_state TEXT NOT NULL DEFAULT 'pending'
    CHECK (markdown_state IN ('pending','complete','failed','skipped')),
  note_relpath TEXT,
  note_managed_sha256 TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  config_hash TEXT NOT NULL
);

CREATE INDEX archives_message_id_idx ON archives(message_id);

CREATE TABLE remote_messages (
  id INTEGER PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(account_id),
  mailbox TEXT NOT NULL,
  uidvalidity INTEGER NOT NULL,
  uid INTEGER NOT NULL,
  gmail_msgid TEXT,
  archive_id TEXT REFERENCES archives(archive_id),
  provider_state TEXT NOT NULL DEFAULT 'source'
    CHECK (provider_state IN ('source','processed','trashed','absent')),
  processed_at TEXT,
  trashed_at TEXT,
  last_seen_at TEXT NOT NULL,
  UNIQUE(account_id, mailbox, uidvalidity, uid)
);

CREATE UNIQUE INDEX remote_gmail_identity_idx
  ON remote_messages(account_id, gmail_msgid) WHERE gmail_msgid IS NOT NULL;

CREATE TABLE attachments (
  id INTEGER PRIMARY KEY,
  archive_id TEXT NOT NULL REFERENCES archives(archive_id) ON DELETE CASCADE,
  ordinal TEXT NOT NULL,
  original_name TEXT,
  stored_name TEXT NOT NULL,
  media_type TEXT NOT NULL,
  disposition TEXT,
  content_id TEXT,
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  decode_error TEXT,
  UNIQUE(archive_id, ordinal)
);

CREATE TABLE enrichment_results (
  id INTEGER PRIMARY KEY,
  archive_id TEXT NOT NULL REFERENCES archives(archive_id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  input_sha256 TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  request_id TEXT,
  duration_ms INTEGER NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(archive_id, provider, model, input_sha256, config_hash)
);

CREATE TABLE attempts (
  id INTEGER PRIMARY KEY,
  archive_id TEXT REFERENCES archives(archive_id),
  remote_id INTEGER REFERENCES remote_messages(id),
  stage TEXT NOT NULL,
  attempt_number INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT NOT NULL,
  error TEXT,
  config_hash TEXT NOT NULL
);

CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  archive_id TEXT REFERENCES archives(archive_id),
  remote_id INTEGER REFERENCES remote_messages(id),
  dimension TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TRIGGER events_immutable_update
BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TRIGGER events_immutable_delete
BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;

CREATE TABLE diagnostics (
  id INTEGER PRIMARY KEY,
  archive_id TEXT REFERENCES archives(archive_id),
  code TEXT NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL
);
