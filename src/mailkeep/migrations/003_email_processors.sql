CREATE TABLE processor_results (
  id INTEGER PRIMARY KEY,
  instance_id TEXT NOT NULL,
  archive_id TEXT NOT NULL REFERENCES archives(archive_id),
  implementation TEXT NOT NULL,
  definition_json TEXT NOT NULL,
  series_id TEXT NOT NULL,
  identity_hash TEXT NOT NULL,
  options_hash TEXT NOT NULL,
  raw_sha256 TEXT NOT NULL,
  cache_key TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  result_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX processor_results_key ON processor_results(cache_key);
CREATE TABLE processor_attempts (
  id INTEGER PRIMARY KEY,
  instance_id TEXT NOT NULL,
  archive_id TEXT NOT NULL REFERENCES archives(archive_id),
  cache_key TEXT NOT NULL,
  definition_json TEXT NOT NULL,
  series_id TEXT NOT NULL,
  identity_hash TEXT NOT NULL,
  options_hash TEXT NOT NULL,
  raw_sha256 TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK(outcome IN ('success','unsupported','failed','cached')),
  error TEXT,
  result_id INTEGER REFERENCES processor_results(id)
);
CREATE INDEX processor_attempts_key ON processor_attempts(cache_key,id);
CREATE TABLE processor_selections (
  id INTEGER PRIMARY KEY,
  instance_id TEXT NOT NULL,
  archive_id TEXT NOT NULL REFERENCES archives(archive_id),
  result_id INTEGER NOT NULL REFERENCES processor_results(id),
  selected_at TEXT NOT NULL
);
CREATE INDEX processor_selections_current ON processor_selections(instance_id,archive_id,id);
CREATE TABLE processor_resolutions (
  id INTEGER PRIMARY KEY,
  instance_id TEXT NOT NULL,
  group_hash TEXT NOT NULL,
  result_id INTEGER NOT NULL REFERENCES processor_results(id),
  reason TEXT NOT NULL,
  selected_at TEXT NOT NULL
);
CREATE TABLE processor_reports (
  instance_id TEXT PRIMARY KEY,
  generation TEXT NOT NULL,
  data_hash TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  published_at TEXT NOT NULL,
  error TEXT
);
