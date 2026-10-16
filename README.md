# Mailkeep

Mailkeep is a local-first email preservation and Obsidian indexing tool. It
downloads exact message bytes over IMAPS without marking them read, stores a
content-addressed archive, creates a managed reading note, and moves a Gmail
message out of the source label only after every required local stage verifies.

The authoritative behavior and safety model are in
[`specs/mailkeep-v1.md`](specs/mailkeep-v1.md). Mailkeep is MIT licensed; see
[`LICENSE`](LICENSE).

## Safety boundary

Mailkeep v1 never permanently expunges mail. An explicitly enabled
`prune --execute` can move a fully reverified Gmail message to Gmail Trash after
retention. Gmail may later purge Trash according to its own policy.

Integrity verification proves that the current local files match the recorded
hashes. It does **not** prove that an independent backup exists. V1 intentionally
has no backup gate; arrange and test backup separately before enabling deletion.

AI enrichment is metadata only. It cannot make an archive valid and is never a
deletion gate. Hosted redaction is risk reduction, not anonymization: prose may
still reveal sensitive meaning, and attachment bytes or extracted attachment
text are never submitted. Local attachment extraction is also not a deletion
gate; unsupported or failed extraction remains visible and retryable.

## Requirements

- Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).
- macOS is fully supported, including the LaunchAgent scheduling helpers.
- Linux runs the core commands and is exercised in CI; the launchd helpers are
  macOS-only.
- Windows is not supported.

## Quick start

```sh
git clone https://github.com/supernifty/mailkeep
cd mailkeep
uv sync
cp config.example.yaml config.yaml
```

Edit `config.yaml` for your account and paths. Apply the configured source label
(for example `Mailkeep`) to a test message in Gmail first; Mailkeep only sees
messages inside that label. Then supply the Gmail app password through the
environment or an explicit git-ignored `env_file`:

```sh
export MAILKEEP_GMAIL_PASSWORD="your-app-password"
```

Never put passwords or API keys in YAML. Create the archive parent directory if
it does not exist yet, and run the first sequence:

```sh
mkdir -p mailkeep-data
uv run mailkeep doctor
uv run mailkeep scan
uv run mailkeep archive --dry-run
uv run mailkeep archive
uv run mailkeep verify
uv run mailkeep prune --dry-run
```

Keep `deletion_enabled: false` until those results and your independent backup
have been reviewed.

To install Mailkeep as a standalone command instead of working from a checkout:

```sh
uv tool install git+https://github.com/supernifty/mailkeep
# or: pipx install git+https://github.com/supernifty/mailkeep
mailkeep --config config.yaml doctor
```

## Enrichment (optional)

Enrichment adds classification, document type, and evidence-backed extraction
fields to notes. It is metadata only and never a deletion gate. The example
configuration uses `provider: none`, which needs no extra service or key:

- `none` — no AI calls (default).
- `ollama` — a local model served by Ollama, with `privacy: local`.
- `jev` — the hosted Typesafe backend. Install the optional dependency with
  `uv sync --extra jev` and set `MAILKEEP_JEV_API_KEY`. Hosted submissions use
  redacted body text only.

## Commands

```sh
uv run mailkeep --config config.yaml doctor
uv run mailkeep --config config.yaml scan
uv run mailkeep --config config.yaml archive --dry-run
uv run mailkeep --config config.yaml archive
uv run mailkeep --config config.yaml verify
uv run mailkeep --config config.yaml status --json
uv run mailkeep --config config.yaml extract-attachments --dry-run
uv run mailkeep --config config.yaml regenerate-notes --dry-run
uv run mailkeep --config config.yaml organize-notes --dry-run
uv run mailkeep --config config.yaml setup-dashboard --dry-run
uv run mailkeep --config config.yaml prune --dry-run
```

`archive --dry-run` fetches with `BODY.PEEK[]` and calculates exact identities
and destination paths without filesystem, database, or server mutation. It does
not call AI unless `--with-enrichment` is supplied.

Pruning requires exactly one of `--dry-run` or `--execute`. Execution also
requires `deletion_enabled: true`, the elapsed retention interval, complete
local verification, one unambiguous Gmail `X-GM-MSGID` match, and a newly
fetched raw SHA-256 match.

Obsidian notes are organized by message date under `Mailkeep/YYYY/MM/`, with
messages lacking a reliable date under `Mailkeep/undated/`. Upgrade an existing
flat note directory by reviewing `organize-notes --dry-run` and then running
`organize-notes --execute`; note contents and personal regions are moved without
being regenerated.

Obsidian front matter is intentionally concise: date, sender, recipient,
subject, non-empty classification fields, and non-empty configured extraction
fields such as `amount` and `due_date`. Archive IDs, hashes, paths, parser
details, and other preservation provenance remain in SQLite and archive
manifests rather than cluttering Properties. `regenerate-notes --execute`
upgrades existing managed notes locally while preserving personal regions and
without making Gmail or AI calls.

Create an optional live Obsidian dashboard by reviewing and then executing:

```sh
uv run mailkeep --config config.yaml setup-dashboard --dry-run
uv run mailkeep --config config.yaml setup-dashboard --execute
```

This creates missing `Index.md` and `Mailkeep.base` files in the configured
Mailkeep folder. Obsidian Bases discovers new reading notes automatically, so
archive runs do not rewrite the dashboard. Enable the Bases core plugin if the
embedded view does not render. Dashboard files are user-owned after creation:
rerunning setup preserves existing files byte-for-byte. Use the Base for
searching and filtering, but do not edit Mailkeep-managed property cells;
property edits can fail `mailkeep verify` and may be replaced by note
regeneration.

Non-dry-run `archive` and `prune --execute` runs append a one-line entry to
`Mailkeep/Activity.md` in the vault, grouped by year. Archive entries report
newly archived messages, byte deduplication, failures, and the running total;
prune entries report messages moved to Gmail Trash, newly recorded absent, or
blocked by safety gates. Idle runs and dry runs write nothing, and existing
content is never rewritten. The behavior is specified in
[`specs/activity-log-v1.md`](specs/activity-log-v1.md).

For a periodic scheduler, run `scripts/mailkeep-periodic.sh`. It uses the
checkout's `.venv/bin/mailkeep` and `config.yaml`, archives first, and runs
`prune --execute` only if archiving succeeds. Override those paths with
`MAILKEEP_BIN` or `MAILKEEP_CONFIG` when needed. Scheduler output should be
captured so failures are visible; the script does not install a schedule.
Messages still inside the retention interval are normal no-ops. A mature
message blocked by another prune safety gate makes the wrapper fail.

### Daily macOS schedule

The anonymous LaunchAgent template runs the wrapper at 09:00 local time. Its
installer renders checkout-specific paths only into the local user Library,
then loads the generic `local.mailkeep.daily` label:

```sh
./scripts/install-launchd.sh
```

The installer writes logs below `~/Library/Logs/Mailkeep/` by default. Keeping
launchd's output files outside protected content directories avoids a scheduler
startup failure before Mailkeep can emit its own diagnostics. Set
`MAILKEEP_LOG_DIR` while installing to use another location, for example:

```sh
MAILKEEP_LOG_DIR="$HOME/Library/Logs/another-mailkeep-instance" ./scripts/install-launchd.sh
```

It does not run immediately when loaded. Inspect its registration with
`launchctl print "gui/$(id -u)/local.mailkeep.daily"`. Use
`launchctl kickstart -k "gui/$(id -u)/local.mailkeep.daily"` only when
a live archive-and-prune run is intended. Secrets continue to come from the
configured `env_file`; none are stored in the LaunchAgent.

For a concise read-only view of the service state, last log activity and exit
result, next scheduled run, archive and processing counts, attachment outcomes,
log paths, and manual trigger command, run:

```sh
./scripts/mailkeep-daily-status.sh
```

Launchd does not expose a durable last-run timestamp, so the helper explicitly
labels that time as inferred from the newest scheduler-log modification time.
Archive statistics use a non-mutating SQLite snapshot. They are reported as
unavailable while a non-empty write-ahead log indicates possible active writes.

## First live validation

Live Gmail and Jev validation is not part of the local test suite. Use a
disposable labelled message and proceed in this order:

1. `doctor`
2. `scan`
3. `archive --dry-run`
4. `archive`
5. `verify`
6. `prune --dry-run`

Keep deletion disabled until those results and your independent backup have
been reviewed.

## Data and backup

Objects live below `archive_root/objects/` and notes below the configured vault
folder. SQLite uses WAL mode. A live database backup must use SQLite's backup
API or include the database, `-wal`, and `-shm` files as one consistent unit.

Attachments are retained only in the archive. V1 records their metadata but
also extracts local text from text-layer PDFs and `text/plain` by default.
Original links remain visible for every attachment. Scanned/image-only PDFs,
images, Office formats, and OCR are recorded as unsupported or non-text in this
phase. Archive-relative paths are authoritative; absolute `file://` note links
are a Mac convenience and need note regeneration if the archive moves.

Phase-1 extraction behavior, provenance, isolation, limits, caching, and note
representation are specified in
[`specs/attachment-content-v1.md`](specs/attachment-content-v1.md). Deferred OCR,
Office, and attachment-aware enrichment requirements are recorded in
[`specs/future-attachment-content.md`](specs/future-attachment-content.md).

## Development

Feature work is specification-first; see
[`CONTRIBUTING.md`](CONTRIBUTING.md). Run:

```sh
mkdir -p tmp
TMPDIR="$PWD/tmp" uv run pytest
```

Temporary test artifacts belong under `tmp/` and should be removed after
verification. The test suite runs on macOS and Linux through GitHub Actions.
