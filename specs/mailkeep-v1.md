# Mailkeep v1 specification

Status: Finalized for v1 implementation

## 1. Purpose

Mailkeep is a local-first email preservation and indexing application. It copies
the exact RFC 5322 message bytes from an IMAP mailbox into a content-addressed
local archive, creates a human-readable Obsidian note, and records all work in
SQLite. Provider-side movement is permitted only after local preservation,
enrichment, note generation, and verification have completed successfully.

The preservation copy is the primary product. Enrichment is optional metadata
and must never decide whether bytes are intact or whether remote mail may be
removed.

## 2. Scope and exclusions

Version 1 supports one configured account. Gmail with an app password is the
first provider with tested label and Trash operations. Generic IMAPS may scan
and preserve messages but may not perform destructive operations.

Version 1 does not permanently expunge Gmail messages, perform OCR, parse Office
attachments, make Jev attachment-aware, support OAuth, support multiple
accounts, run as a daemon, or prove the existence of an independent backup.
Absolute file URLs are a Mac convenience and may become stale if data moves;
archive-relative paths are authoritative.

Phase-1 text-layer PDF and plain-text extraction is specified in
`specs/attachment-content-v1.md`. Office extraction, OCR, and attachment-aware
enrichment remain deferred in `specs/future-attachment-content.md`. Extraction
does not change preservation, provider movement, or deletion gates.

## 3. Safety invariants

1. Fetching message content uses UID commands and `BODY.PEEK[]`; it must not add
   `\\Seen`.
2. `original.eml` contains the exact bytes returned by the provider. Its SHA-256
   is the preservation identity.
3. The archive ID is a namespaced UUID derived from the raw SHA-256. Identical
   bytes share one preservation object. `Message-ID` is advisory: one
   `Message-ID` with distinct bytes produces distinct archives and a diagnostic.
4. Completed archive directories appear atomically. Files are hashed and fsynced
   in an archive-local staging directory before that directory is renamed.
5. Gmail labels may change from source to processed only when preservation,
   enrichment, Markdown, and verification are complete.
6. Deletion eligibility begins at confirmed processing time, never the message
   date.
7. Pruning re-verifies local material and re-fetches the exact Gmail message by
   `X-GM-MSGID`; its SHA-256 must still match. V1 moves the message to Gmail's
   discovered `\\Trash` mailbox and never issues EXPUNGE.
8. AI output, category, confidence, or extracted entity is never a preservation
   or deletion integrity gate.
9. Dry runs do not mutate the filesystem, database, or server. Dry-run
   enrichment is called only when explicitly requested.
10. Credentials are read from named environment variables and never written to
    YAML, SQLite, notes, or logs.
11. Attachment extraction operates only on verified preserved bytes in an
    isolated local worker. Its failure is recorded but is not a preservation,
    provider-movement, enrichment, or deletion gate.

## 4. Architecture

The `mailkeep` package has modules for configuration, IMAP/provider access,
MIME parsing, atomic storage, SQLite state, enrichment, Markdown rendering,
attachment extraction, verification, retention, locking, and CLI orchestration.
Cross-module values use small dataclasses rather than provider response objects.

Mutating commands acquire an exclusive lock beneath the data root. A later
`archive` run reconciles stale staging directories and resumable database
records before discovering new work.

## 5. Configuration

Configuration is YAML and rejects unknown or invalid values. It defines:

- stable `account_id`, provider (`gmail` or `imap`), IMAP host, port, username,
  password environment-variable name, source label, and processed label;
- archive root, Obsidian vault and note folder, SQLite path, and optional env
  file path;
- retention days, per-run message and byte limits, and `deletion_enabled`;
- categories with thresholds, document types and confidence floor, and
  extraction field definitions;
- enrichment provider (`jev`, `ollama`, or `none`), provider endpoint, exact
  model/build, API-key environment-variable name, privacy mode, and timeout.
- attachment extraction enablement and independent PDF, page, text, output,
  attachment-count, elapsed-time, and note-embedding limits.

Paths expand `~` and environment variables. Relative paths resolve relative to
the configuration file. An optional env file is loaded only when explicitly
named and must be git-ignored by the operator. Existing process environment
values win. Gmail and Jev secrets must be present for live commands that need
them. Example configuration and `.env.example` contain placeholders only.

The default hosted privacy mode is `redacted`. Before hosted submission,
recognizable email addresses, telephone numbers, sensitive URL query values,
and long identifier-like values are replaced by stable typed placeholders.
Attachment bytes are never sent. Redaction reduces accidental disclosure but
does not anonymize the meaning of prose.

## 6. Database and lifecycle

Numbered SQL migrations are applied transactionally. Every connection enables
foreign keys, a busy timeout, and WAL. Live backups must use SQLite's backup API
or copy the database together with its WAL and shared-memory files.

The schema carries stable account identity and stores remote occurrences,
preservation objects, attachment manifests, enrichment results, attachment
extraction results and immutable attempts, note metadata, stage attempts,
diagnostics, and immutable transition events.

Lifecycle dimensions are independent:

- preservation: `discovered`, `preserving`, `preserved`,
  `preservation_verified`;
- enrichment and Markdown: `pending`, `complete`, `failed`, `skipped`;
- provider: `source`, `processed`, `trashed`, `absent`.

`complete_verified` is derived: preservation is verified, enrichment is complete
or deliberately skipped because the configured provider is `none`, Markdown is
complete, and the managed Markdown hash verifies. `deletion_eligible` further
requires provider state `processed`, elapsed retention, enabled deletion, and a
Gmail identity. `deleted` means Gmail later reports the message absent; `trashed`
means Mailkeep successfully moved it to Trash.

Every attempt records stage, attempt number, start/end time, outcome, error,
and configuration hash. Transition events are append-only. A failure updates
only its own dimension and never erases a prior success.

Attachment extraction has an independent per-attachment history rather than an
archive completion dimension. Successful or deterministic results are cached by
attachment SHA-256, parser/version, and extraction configuration hash. The
enrichment cache retains its pre-extraction configuration fingerprint so
changing attachment extraction does not invalidate body-only Jev results.

## 7. Discovery and provider behavior

Discovery selects the source mailbox read-only and uses UIDs. It records
account, mailbox, UIDVALIDITY, UID, and, when supported, `X-GM-MSGID`.
UIDVALIDITY changes make old UID locations stale but do not invalidate preserved
objects. Gmail lookup after any move is by `X-GM-MSGID`, not a prior UID.

Gmail support requires advertised `X-GM-EXT-1`. When the server also advertises
the standard `MOVE` capability, Mailkeep moves the message from the source-label
mailbox to the processed-label mailbox with `UID MOVE`; under Gmail's mailbox
mapping this adds the processed label and removes the selected source label as
one server operation while retaining unrelated labels. If `MOVE` is not
advertised, Mailkeep falls back to `X-GM-LABELS`: add the processed label,
confirm it, and then remove the source label. In either path, Mailkeep confirms
exactly one stable Gmail message-ID match in the processed mailbox and no match
in the source mailbox before recording provider state `processed`; an `OK`
command response alone is insufficient. The Trash mailbox is discovered via
the LIST `\\Trash` special-use attribute; it is not guessed from an English
folder name.

Provider adapters expose deterministic operations and a fake adapter is used in
tests to prove PEEK fetching, capability-aware atomic movement, stable-ID
lookup, safe fallback label ordering, ambiguity handling, reconnect behavior,
and no expunge.

## 8. MIME parsing and archive storage

Messages are parsed with Python's modern email policy while retaining the
original bytes separately. Header decoding is best effort and malformed input
must produce a diagnostic rather than overwrite raw data.

For the reading body, prefer the first non-empty `text/plain` part in the
selected multipart alternative. Otherwise sanitize HTML, remove remote images,
remove non-content head, style, script, and hidden elements, and convert the
visible content to Markdown. CSS or script text must never appear in the reading
copy. Layout tables must be flattened into readable text rather than emitted as
enormous Markdown tables; small semantic tables may remain tabular. Remote image
URLs are removed, retaining useful alternative text when present. Quoted history
remains. CID image references are rewritten to archive-local inline files.

All non-body parts, inline parts, and nested `message/rfc822` parts are archived.
Stored filenames begin with a stable MIME-part ordinal, are path-safe, and are
collision resistant. Metadata retains original filename, disposition,
content ID, media type, byte size, and SHA-256. A decoding failure is recorded
and prevents complete verification; it never changes `original.eml`.

The object path is `<archive_root>/objects/<sha[0:2]>/<sha[2:4]>/<archive-id>/`.
It contains `original.eml`, `metadata.json`, and `attachments/`. A staging
directory is a sibling named `.<archive-id>.staging-<token>`. Reconciliation may
remove only a staging directory that matches this strict pattern and has no
active lock; it never deletes a completed object.

After original verification, enabled phase-1 extraction reads these attachment
files without modifying them. Text-layer PDFs are detected by `%PDF-` content
signature and parsed with locked `pypdf`; declared `text/plain` uses its charset
or strict UTF-8. A subprocess and explicit resource limits isolate parser
timeouts, crashes, malformed/encrypted/image-only documents, and excessive
input or output. Successful text and provenance manifests are content-addressed
derived files below `archive_root/derived/attachments/`. See
`specs/attachment-content-v1.md` for exact defaults and outcomes.

## 9. Enrichment

Jev is the example hosted backend and uses the official `typesafe-sdk`.
Multi-label classification asks one `Noul` question per category and applies
the configured threshold. Document type is a `Choice` with an `other` option
and a confidence floor. Extraction first builds deterministic candidates from
local text; Jev may select or validate candidates, but unsupported values remain
absent.

Ollama sends a JSON schema through the documented structured-output `format`
field with temperature zero, validates returned JSON, and retains extracted
values only when each includes source evidence present in the submitted text.
The configured `deepseek-r1:1.5b` model is not treated as adequate for trusted
extraction; an operator must explicitly configure a stronger model.

Results are cached by provider, exact model/build, redacted-input hash, and
configuration hash. SQLite stores these values plus probabilities, request ID,
duration, summarized results, and errors. Provider or validation failure leaves
preservation intact and the remote message in the source label for retry.
Phase 1 submits only the message body; attachment bytes and extracted text are
not included in enrichment.

## 10. Obsidian notes

Notes are stored below the configured Obsidian folder as
`YYYY/MM/YYYY-MM-DD - Sender - Subject - <short-archive-id>.md`, using the
message date. Messages without a reliable date use
`undated/undated - Sender - Subject - <short-archive-id>.md`. Unsafe filename
characters are removed and filename components are bounded. Date folders are
stable organization only. As specified in `specs/obsidian-note-v2.md`, front
matter is deliberately limited to message date, readable sender and recipient
lists, subject, non-empty categories and document type, and configured
evidence-backed extraction fields such as `amount` and `due_date`. Preservation
identities, paths, hashes, extraction provenance, enrichment implementation
details, and entity evidence remain in SQLite and archive manifests rather than
Obsidian Properties.

The generated body includes the message, evidence-backed extracted details, a
visible link for every original attachment, a link to the original email, and,
when attempted, bounded extracted text or its explicit status. Empty sections
are omitted. Embedded text is
limited to 50,000 characters per attachment and 100,000 per message by default;
truncation links to the complete derived text. Extracted marker-like strings
are escaped and personal text remains outside the managed region.

Mailkeep owns the YAML front matter and the body between
`<!-- mailkeep:generated:start -->` and `<!-- mailkeep:generated:end -->`.
`<!-- mailkeep:personal:start -->` and `<!-- mailkeep:personal:end -->` always
remain and their contents are preserved on regeneration. The managed hash is a
hidden HTML comment, not an Obsidian property, and covers canonical front matter
and generated body only, excluding personal text and the hash comment itself.

## 11. Commands

- `mailkeep doctor`: validate config, paths, secret presence, SQLite, provider
  connectivity/capabilities/folders, Gmail extensions, and configured
  enrichment—including availability of the selected hosted model—without
  changing mail.
- `mailkeep scan`: list source candidates and known state, read-only.
- `mailkeep archive [--limit N]`: discover and run or resume the pipeline.
- `mailkeep archive --dry-run [--with-enrichment]`: PEEK-fetch and report exact
  hashes, IDs, paths, and planned actions with no mutations. AI is called only
  with `--with-enrichment`.
- `mailkeep status [--json]`: report counts, pending work, failures, retention,
  and recent activity.
- `mailkeep verify [--archive-id ID]`: check relationships, safe paths, raw and
  attachment hashes, MIME-derived manifest, managed note hash, staging
  inconsistencies, and database orphans.
- `mailkeep organize-notes --dry-run`: verify and report moves required to put
  existing managed notes into the current date hierarchy without mutation.
- `mailkeep organize-notes --execute`: under the data lock, preflight every
  planned move, preserve note bytes and personal regions exactly, move notes,
  and atomically update their database-relative paths. Unsafe paths, failed
  archive verification, or destination collisions prevent execution. The
  operation is rerunnable and already organized notes are no-ops.
- `mailkeep extract-attachments --dry-run [--archive-id ID]`: report local
  extraction, cache-reuse, and skip actions without parsing, note regeneration,
  database mutation, Gmail, or AI calls.
- `mailkeep extract-attachments --execute [--archive-id ID] [--retry-failed]`:
  extract or reuse preserved attachments and regenerate affected managed note
  regions while preserving personal content. This explicit maintenance command
  works even when automatic extraction is disabled.
- `mailkeep regenerate-notes --dry-run|--execute [--archive-id ID]`: locally
  plan or regenerate all managed reading notes from preserved data and cached
  results, preserving personal content and making no provider or AI call.
- `mailkeep prune --dry-run`: list eligible messages and every safety gate.
- `mailkeep prune --execute`: additionally require `deletion_enabled: true`,
  reverify and refetch, then move unambiguous Gmail messages to Trash.
- `scripts/mailkeep-periodic.sh`: run `archive` and, only after it exits
  successfully, run `prune --execute`. The wrapper resolves its default
  `config.yaml` and `.venv/bin/mailkeep` relative to the checkout so it does not
  depend on the scheduler's working directory. `MAILKEEP_CONFIG` and
  `MAILKEEP_BIN` may override those defaults. Missing or unusable inputs fail
  before either command runs, and command output and exit status remain visible
  to the scheduler.
- `ops/launchd/local.mailkeep.daily.plist.in` and
  `scripts/install-launchd.sh`: an anonymous macOS per-user LaunchAgent template
  and installer. The installer renders checkout-specific absolute paths only
  into the local `~/Library/LaunchAgents/local.mailkeep.daily.plist`, invokes
  the periodic wrapper at 09:00 local time without `sudo`, and loads the generic
  `local.mailkeep.daily` label. It does not embed credentials or run at load,
  and appends standard output and error below `mailkeep-data/logs/` by default.
  `MAILKEEP_LOG_DIR` may select another absolute or user-relative log directory
  when installing the local plist, so runtime data can live outside the
  checkout. Tracked files must not contain a username, home-directory path, or
  other machine-specific identity.
- `scripts/mailkeep-daily-status.sh`: inspect the installed LaunchAgent without
  triggering it. Report whether it is loaded or running, its run count and last
  exit result since the current load, the last-run time inferred and explicitly
  labelled from the newest scheduler-log modification time, the next configured
  calendar run in local time, both log paths, and the exact manual `kickstart`
  command. Also summarize the configured database's total archived and
  provider-processed messages, completion states, current attachment-extraction
  outcomes, latest archive time, and historical failed-attempt count. Database
  access uses an immutable SQLite snapshot only when no non-empty WAL is
  present, so the helper never creates sidecar files, migrates a database, or
  reports a knowingly stale snapshot during an active write. Missing plists,
  unloaded jobs, absent logs, never-run jobs, and unavailable archive statistics
  are readable states rather than tracebacks. The helper makes no Gmail, AI,
  prune, or archive mutation call.

Calling `prune` with neither mode or both modes is a usage error. Commands return
zero on full success, 1 on operational or verification failure, and 2 for
configuration or usage errors. Human output never contains secrets; JSON status
is stable and machine-readable. A processed message whose retention interval
has not elapsed is an expected no-op rather than a prune failure. During
`prune --execute`, a retention-mature message blocked by another safety gate is
an operational failure so periodic schedulers can surface it.

## 12. Acceptance criteria

Automated tests must cover:

- plain, HTML, alternative, Unicode, duplicate-name, CID, nested, malformed,
  missing-header, and decoding-failure MIME cases;
- atomic storage and interruption recovery at storage/database boundaries;
- byte deduplication, `Message-ID` collision diagnostics, UIDVALIDITY change,
  reconnect, and manually moved Gmail messages;
- PEEK semantics, no accidental `\\Seen`, no early label changes, and stable
  Gmail-ID lookup;
- redaction, thresholds, schema/evidence rejection, outages, retry/cache, and
  configuration-triggered re-enrichment for mocked Jev and Ollama;
- deterministic note snapshots, encoded links, filename collisions, CID and
  remote-image handling, HTML/CSS/script suppression, and personal-note
  preservation; dated and undated note hierarchy; byte-preserving organization
  of legacy flat notes; dry-run immutability; and collision refusal;
- tampered or missing local data, unsafe paths, stale staging, and database
  orphan verification failures;
- each retention/prune gate, disabled deletion, ambiguity, hash mismatch,
  absent outputs, dry-run immutability, exact Trash targeting, and no expunge.
- periodic wrapper ordering, checkout-relative defaults, supported overrides,
  refusal to prune after an archive failure, and successful no-op handling for
  messages that are not yet retention-mature.
- valid daily LaunchAgent template structure, correctly rendered absolute
  executable and default or overridden log paths, no embedded environment
  secrets, and no username or machine-specific home path in tracked scheduling
  files.
- daily status reporting for loaded, unloaded, never-run, successful, and failed
  jobs; inferred log timestamps and next-run calculation; and read-only
  archive statistics that do not trigger the periodic wrapper or mutate SQLite.

Tests and smoke artifacts use `tmp/codex/` inside the checkout and are removed
after verification. Acceptance requires the automated suite and local CLI smoke
tests to pass. Live Gmail and Jev behavior remains explicitly unverified until
the operator supplies configuration and a disposable test message. The first
live sequence is `doctor`, `scan`, `archive --dry-run`, `archive`, `verify`, then
`prune --dry-run`.

## 13. Operational warning

Mailkeep integrity checks establish that the current local object matches its
recorded hashes. They do not establish that another independent backup exists.
V1 intentionally has no backup gate; operators must arrange and verify backups
separately before enabling deletion.
