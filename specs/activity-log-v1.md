# Mailkeep activity log, version 1

Status: Approved for implementation

## Purpose

Operators running Mailkeep on a schedule need a durable, human-readable record
of what each run actually did. SQLite holds complete provenance, but it lives
outside the Obsidian vault and is not convenient to read day to day. A short
activity log beside the reading notes makes preservation and retention actions
visible at a glance.

## Scope

- A non-dry-run `archive` run appends at most one entry.
- A `prune --execute` run appends at most one entry.
- Dry runs and every other command (`doctor`, `scan`, `status`, `verify`,
  `extract-attachments`, `regenerate-notes`, `organize-notes`,
  `setup-dashboard`) never write to the activity log.
- There is no new configuration and no new CLI flag; the log is always
  maintained when the commands above run.

## File and format

- Path: `<obsidian_vault>/<obsidian_folder>/Activity.md`, the same folder as
  the reading notes and dashboard.
- The file is Markdown. Mailkeep creates it with the heading
  `# Mailkeep activity` when it first needs to append.
- Entries are grouped under `## YYYY` year headings using the local calendar
  year of the entry, and each entry is one line:
  `- YYYY-MM-DD: <summary>.` with the local calendar date.
- Mailkeep only appends. Existing content is never rewritten, reordered, or
  removed, so operator annotations elsewhere in the file survive.
- An entry is added to the current year's section. If that section does not
  exist, a new section is appended at the end of the file.
- Writes are atomic: content is written to a temporary sibling and renamed over
  the target, so an interrupted run cannot leave a partial entry.

### Archive entry

Written when at least one candidate reached `processed` or `failed`. Idle runs
(no candidates, or only `deferred` outcomes) write nothing.

```text
- 2026-10-13: archived 3 (1 deduplicated), 1 failed; 112 total.
```

- `archived N`: messages that reached provider state `processed` in this run.
- `(N deduplicated)`: subset whose bytes already existed in the archive;
  omitted when zero.
- `, N failed`: messages that ended with outcome `failed`; omitted when zero.
- `N total`: total archives in SQLite after the run.

### Prune entry

Written by `prune --execute` when at least one message was moved to Gmail
Trash, newly recorded absent, or retention-mature but blocked by a safety gate.
Otherwise nothing is written.

```text
- 2026-10-13: moved 2 to Gmail Trash; 1 recorded absent; 1 blocked by safety gates.
```

`prune --dry-run` never writes.

## Failure handling

The activity log is auxiliary. A failure to write it must not change the
outcome or exit status of an archive or prune run. The failure is reported as a
single warning on standard error.

## Acceptance criteria

Automated tests must cover:

- the first archive creates `Activity.md` with the header, current-year
  section, and a one-line entry containing processed, deduplicated, failed, and
  total counts;
- an idle archive and `prune --dry-run` leave the log absent or unchanged;
- `prune --execute` records moved-to-Trash, newly absent, and blocked counts;
- entries append within an existing year section, and a new year adds a new
  section without touching earlier content or operator annotations;
- writes are atomic and leave no temporary files behind.

## Exclusions

- No rotation, truncation, or per-entry detail beyond one line.
- No entry for maintenance commands, or for prune runs that only re-check
  messages without mutation or blocking.
- No activity log inside the archive root or the checkout; it is an Obsidian
  reading surface only.
