ALTER TABLE attachments ADD COLUMN declared_charset TEXT;

CREATE TABLE attachment_extraction_results (
  id INTEGER PRIMARY KEY,
  attachment_sha256 TEXT NOT NULL,
  parser TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  outcome TEXT NOT NULL,
  artifact_relpath TEXT,
  manifest_relpath TEXT,
  output_sha256 TEXT,
  output_size INTEGER,
  page_count INTEGER,
  page_boundaries_json TEXT NOT NULL DEFAULT '[]',
  warnings_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  UNIQUE(attachment_sha256, parser, parser_version, config_hash)
);

CREATE TABLE attachment_extraction_attempts (
  id INTEGER PRIMARY KEY,
  archive_id TEXT NOT NULL REFERENCES archives(archive_id) ON DELETE CASCADE,
  attachment_id INTEGER NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
  attachment_sha256 TEXT NOT NULL,
  parser TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  attempt_number INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  outcome TEXT NOT NULL,
  error TEXT,
  cache_reused INTEGER NOT NULL DEFAULT 0 CHECK (cache_reused IN (0,1)),
  result_id INTEGER REFERENCES attachment_extraction_results(id),
  UNIQUE(attachment_id, parser, parser_version, config_hash, attempt_number)
);

CREATE INDEX attachment_extraction_attempts_archive_idx
  ON attachment_extraction_attempts(archive_id, attachment_id, id);

CREATE TRIGGER attachment_extraction_results_immutable_update
BEFORE UPDATE ON attachment_extraction_results
BEGIN SELECT RAISE(ABORT, 'attachment extraction results are immutable'); END;

CREATE TRIGGER attachment_extraction_results_immutable_delete
BEFORE DELETE ON attachment_extraction_results
BEGIN SELECT RAISE(ABORT, 'attachment extraction results are immutable'); END;

CREATE TRIGGER attachment_extraction_attempts_immutable_update
BEFORE UPDATE ON attachment_extraction_attempts
BEGIN SELECT RAISE(ABORT, 'attachment extraction attempts are immutable'); END;

CREATE TRIGGER attachment_extraction_attempts_immutable_delete
BEFORE DELETE ON attachment_extraction_attempts
BEGIN SELECT RAISE(ABORT, 'attachment extraction attempts are immutable'); END;
