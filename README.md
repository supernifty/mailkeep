# Mailkeep

Mailkeep is a local-first email preservation and Obsidian indexing tool. It
downloads exact message bytes over IMAPS without marking them read, stores a
content-addressed archive, creates a managed reading note, and moves a Gmail
message out of the source label only after every required local stage verifies.

The authoritative behavior and safety model are in
[`specs/mailkeep-v1.md`](specs/mailkeep-v1.md).

## Safety boundary

Mailkeep v1 never permanently expunges mail. An explicitly enabled
`prune --execute` can move a fully reverified Gmail message to Gmail Trash after
retention. Gmail may later purge Trash according to its own policy.

Integrity verification proves that the current local files match the recorded
hashes. It does **not** prove that an independent backup exists. V1 intentionally
has no backup gate; arrange and test backup separately before enabling deletion.

AI enrichment is metadata only. It cannot make an archive valid and is never a
deletion gate. Hosted redaction is risk reduction, not anonymization: prose may
still reveal sensitive meaning, and attachment bytes are never submitted.

## Install

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) are required.

```sh
uv sync --extra dev
cp config.example.yaml config.yaml
```

Set the named secret environment variables directly, or configure an explicit
git-ignored `env_file`. Never put passwords or API keys in YAML.

## Commands

```sh
uv run mailkeep --config config.yaml doctor
uv run mailkeep --config config.yaml scan
uv run mailkeep --config config.yaml archive --dry-run
uv run mailkeep --config config.yaml archive
uv run mailkeep --config config.yaml verify
uv run mailkeep --config config.yaml status --json
uv run mailkeep --config config.yaml organize-notes --dry-run
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

It does not run immediately when loaded. Inspect its registration with
`launchctl print "gui/$(id -u)/local.mailkeep.daily"`. Use
`launchctl kickstart -k "gui/$(id -u)/local.mailkeep.daily"` only when
a live archive-and-prune run is intended. Secrets continue to come from the
configured `env_file`; none are stored in the LaunchAgent.

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
does not parse PDF, Office, image, or OCR content. Archive-relative paths are
authoritative; absolute `file://` note links are a Mac convenience and need note
regeneration if the archive moves.

Attachment-content parsing is a required future capability. Its intended
formats, provenance, security constraints, Obsidian representation, acceptance
criteria, and unresolved lifecycle decisions are recorded in
[`specs/future-attachment-content.md`](specs/future-attachment-content.md).

## Development

Feature work is specification-first. Run:

```sh
mkdir -p tmp/codex
TMPDIR="$PWD/tmp/codex" uv run pytest
```

Temporary test artifacts belong under `tmp/codex/` and should be removed after
verification.
