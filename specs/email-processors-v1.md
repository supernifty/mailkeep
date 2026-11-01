# Email processors and RescueTime weekly history, version 1

Status: Implemented and enabled locally on 2026-11-01. The user confirmed
Australia/Melbourne and exclusive-end date boundaries during implementation.

## Purpose and feasibility

Process selected preserved emails with custom Python code, producing structured
records and useful historical views. The first processor extracts total
distracting time from RescueTime weekly reports and produces a weekly table
and graph in Obsidian.

This is a moderate extension of Mailkeep rather than a new ingestion system.
The current service already preserves and verifies messages, SQLite supports
numbered migrations, and attachment extraction demonstrates an independent
derived-processing stage. The main work is reliable matching and extraction,
versioned results, duplicate/correction handling, and recoverable rendering.
Adding a second processor should require its module, schema, configuration,
and fixtures, without another change to archive orchestration or core tables.

Use a small, explicit processor interface first. A plugin marketplace, arbitrary
script execution, and a general workflow engine would add unnecessary scope.
All commands, modules, tables, and configuration below are proposed interfaces.

## Scope and boundaries

- Operate on verified local archives. RescueTime mail must first enter the
  existing configured source label and be archived through normal Mailkeep.
  A Gmail rule can supply that label; this feature does not create that rule.
- Automatically process newly archived matching mail, and support local
  backfill of older archives, including mail already moved to the processed label.
- Store validated, typed records in SQLite with links to their source emails.
- Generate CSV, Markdown tables, and static PNG graphs from accepted records.
- Keep processors optional and disabled unless explicitly configured.
- Do not use AI to infer numbers or report dates in the initial processor.
- Do not fetch RescueTime links, images, dashboards, or API data. Attachments,
  OCR, external writes, notifications, and automatic configuration discovery
  are outside v1.

## Registration and configuration

Ship the first processor in `src/mailkeep/processors/rescuetime.py`, registered
as `rescuetime-weekly`. A registry maps IDs to built-in implementations.
Adding a trusted custom installed module is supported through the same API:
an explicit `module:factory` reference in configuration, imported only in the
worker during execution. Never import code from an email, attachment, discovered
directory, or remotely supplied URL. No package installation happens at runtime.

Each configured instance has a unique stable ID, an implementation, matching
rules, a series ID, and processor-specific options validated before execution.
Example configuration using the inspected sender and subject suffix. The
timezone remains an operator setting to confirm against the RescueTime account:

```yaml
processors:
  enabled: true
  instances:
    - id: personal-rescuetime
      implementation: rescuetime-weekly
      series_id: personal
      match:
        from_addresses: [support@rescuetime.com]
        subject_contains: [RescueTime Weekly Summary]
      options:
        report_timezone: Australia/Melbourne
        metric: total_distracting_time
        layout: weekly-v1
```

Matching requires exactly one parsed From mailbox, an exact case-insensitive
address match, and at least one configured case-insensitive subject substring.
Empty sender or subject lists are configuration errors. Multiple instances can
match one message; each runs independently. Display names never establish a match.
The processor additionally validates report structure and metric labels.
Matching identifies candidates; it does not authenticate the sender or establish
the truth of the report. Forwarded reports and quoted report content are not
supported initially.

The sender, subject suffix, layout, and metric mapping below are established from
one representative report; other historical layouts remain unverified. `series_id` identifies
one reporting account; separate accounts require separate series. Instance IDs,
series IDs, and processor IDs use safe slugs and cannot supply filesystem paths.

## Processor interface and execution

A processor declares `id`, `api_version`, `processor_version`, `schema_version`,
an options schema, and an output dataset schema. It implements a pure operation:

```text
process(message, options) → success(records, evidence) | unsupported(reason)
```

`message` contains the archive ID and raw SHA-256, decoded subject/addresses/date,
the existing reading-body Markdown, and decoded non-attachment plain-text and
HTML MIME parts identified by part ordinal. The original raw bytes remain the
authority. Layout-sensitive extraction uses the MIME parts rather than the
potentially flattened reading body. MIME decoding warnings are provided and a
decoding defect affecting a metric or date prevents a successful extraction.

The host selects candidates from headers without loading custom code. A worker
receives bounded JSON input and returns bounded JSON output. The host validates
the declared schema, evidence, dates, numeric limits, and duplicate keys before
committing anything. A processor receives no database connection, provider,
credentials, configuration for other features, or output path. Rendering and
storage are host responsibilities, not plugin callbacks. API v1 supports typed
scalar record fields and a time-series view declaration naming a date field,
numeric value field, unit, and series key; it has no arbitrary rendering hooks.

Launch workers directly with the current Python interpreter and no shell.
Use a minimal environment without Mailkeep credential variables, a workspace
scratch directory, captured output, a parent timeout (10 seconds), bounded
input/output (5 MiB/1 MiB), and available OS CPU/memory/file limits. Disable
network access in the worker as a guard against accidental use, following the
attachment-worker pattern. These restrictions are not a security sandbox:
installed Python code must be trusted and can otherwise run as the user.
No claim of safe execution of hostile plugins is made.

## Lifecycle integration

After original preservation verification, invoke enabled matching processors
before enrichment. Catch and record worker, validation, and persistence failures
per processor; continue the existing archive flow. Commit successful records
before attempting their report generation. Render each affected dataset once
at the end of the archive batch, while holding the existing data lock.

Processor results, rendering, and their verification are separate from
`complete_verified`, managed-message-note integrity, provider movement, retention,
and pruning. A processor failure must not prevent an otherwise valid email from
being archived or moved. It must be visible in command JSON, stderr warnings,
and processor status; it must never look like a successful zero-valued report.
The normal archive exit behavior remains based on existing archive outcomes.

Because movement can succeed when extraction fails, recovery scans local
archives rather than relying on mail remaining in the source label. Existing
archives are not automatically backfilled when enabling an instance.
Disabling an instance stops new extraction but retains its data and reports.

Exclude the new processor configuration from `enrichment_fingerprint()` so
enabling or changing a processor does not invalidate existing body-only AI
enrichment caches. Do not rewrite original archive metadata or managed email
notes to add processor output. Existing user-owned `Index.md` and `Mailkeep.base`
remain untouched; users may link the new report from their dashboard.

## Storage, provenance, and repeatability

Add the next numbered migration, using shared tables rather than per-plugin DDL:

- `processor_attempts`: instance and implementation IDs, versions, series ID,
  archive ID, raw hash, options hash, input-contract version, cache key,
  start/end timestamps, outcome, bounded diagnostics, and optional result ID.
  Attempts are append-only, including explicit retries and cache reuse.
- `processor_results`: immutable successful result sets, validated records,
  evidence, schema version, and result hash. JSON payloads use canonical encoding;
  dataset schemas define field types and host validation.
- `processor_selections`: the current successful result per instance/archive,
  plus explicit conflict resolutions and supersession history.
- `processor_reports`: last published generation, selected-data fingerprint,
  renderer version, file hashes, publication time, and stale/failure state.

Cache key = archive ID/raw hash + instance ID + implementation identity/version
+ API/input-contract/schema versions + extraction-options hash. The options
hash includes series identity and matching rules as well as processor options.
Changing account/series or candidate selection must not reuse an old selection.
Output paths and cosmetic graph settings do not invalidate extraction. Custom implementations
must bump their version whenever extraction behavior changes; editing code without
a version bump is unsupported. A successful unchanged key is reused. Unchanged
unsupported or failed attempts are skipped unless explicitly retried. New
versions/options create new attempts and retain the previous evidence.

A replacement result and its current-selection update commit in one transaction.
If an upgrade fails, the last accepted value remains available with a visible
stale-parser warning. It must not be silently removed or presented as updated.
Disabling or removing a configuration does not delete its historical selections.

For RescueTime the logical row key is `(instance_id, series_id, period_start,
period_end_exclusive, metric)`. Repeated delivery of identical raw bytes reuses the archive.
Different messages with the same row key and value become one observation with
multiple source links; they are never summed. Different values for the same key
produce a conflict: exclude that week's value from the graph, show the candidates
in a conflicts section, and require explicit selection of a successful result.
Never assume the newest arrival is the corrected report. Preserve the resolution
reason, timestamp, and all source records. Non-identical overlapping report
periods within one series are also conflicts, not independent weekly points.

## RescueTime extraction contract

### Inspected example and expected result

On 2026-11-01, directly inspected the user-supplied `.eml` locally without
modifying or copying it into the repository. It is multipart/alternative with
plain-text and HTML representations. Both contain the same goal and actual value.
The parsed sender is `RescueTime Team <support@rescuetime.com>`; the subject ends
with `RescueTime Weekly Summary` and begins with an unrelated category duration.
The message Date is `2026-10-31 23:06:44 +0000`; do not use the filename's local
timestamp as the reporting date.

The relevant content is:

```text
Your weekly summary
October 25, 2026 - November  1, 2026

YOUR GOALS FOR THE WEEK
Less than 10h 30m on All Distracting Time
All day, every day
17h 8m
2h 26m avg per day
```

The user explicitly identifies `17h 8m` as the target. Extract the actual achieved
duration in the goal named `All Distracting Time`, not the goal threshold or daily
average. This is an already aggregated source metric; do not sum distracting
subcategories or derive it from productive percentages. Require the qualifier
`All day, every day`; a restricted-hours goal does not establish the whole-week
total. Ignore the unrelated `All Productive Time` goal, total tracked time,
application durations, and the subject's category duration.

For `weekly-v1`, prefer the plain-text goal block bounded by its heading and the
next goal/section or footer. In HTML, locate the corresponding goal heading and
actual-duration field within the same goal block; never select an arbitrary
duration from a document-wide text search. Normalize whitespace and line endings,
including double spaces in the displayed date, without discarding field boundaries.
If both MIME alternatives expose a recognized goal, require their values and
periods to agree. The inspected example has one such HTML goal heading.

Expected record values:

```json
{
  "series_id": "personal",
  "period_start": "2026-10-25",
  "period_end_exclusive": "2026-11-01",
  "reported_period": "October 25, 2026 - November  1, 2026",
  "metric": "total_distracting_time",
  "distracting_seconds": 61680,
  "reported_label": "All Distracting Time",
  "reported_value": "17h 8m",
  "precision_seconds": 60
}
```

`report_timezone` is added from configuration; this example does not establish
the account timezone; the user subsequently confirmed Australia/Melbourne.
The displayed endpoints are seven calendar days apart.
The user confirmed interpreting them as start-inclusive/end-exclusive
weekly boundaries, rather than eight inclusive dates. Preserve `reported_period`
as evidence and explicitly label report end columns as exclusive. This convention
is operator-confirmed rather than explicitly stated in the message.

The real report contains personal activity and account links. Use synthetic
fixtures reproducing just the relevant structure, retaining the supplied target
duration as an acceptance example; do not commit the original email, personal
activity list, tracking links, or local source path. Additional historical
formats, alternate goal schedules, and locale variants remain unverified.

Each accepted report produces exactly one record:

| Field | Meaning |
| --- | --- |
| `series_id` | Configured reporting account |
| `period_start` | First included calendar date, ISO `YYYY-MM-DD` |
| `period_end_exclusive` | Exclusive end boundary; operator-confirmed convention above |
| `reported_period` | Exact displayed date-range text |
| `report_timezone` | Configured reporting timezone, not inferred from machine locale |
| `metric` | `total_distracting_time` |
| `distracting_seconds` | Non-negative integer duration |
| `reported_label` | Exact source metric label |
| `reported_value` | Exact displayed duration |
| `precision_seconds` | Resolution of the source duration, e.g. 60 for whole minutes |

Evidence identifies the MIME part and bounded source text for the duration and
period, the extraction rule used, and the original date strings. Raw hashes,
archive IDs, versions, attempt timestamps, and source-note paths are stored as
provenance rather than repeated as user-facing table columns.

Rules:

1. Extract the actual achieved duration for the `All Distracting Time` goal with
   the qualifier `All day, every day`, following the inspected layout above. Do not
   confuse it with total logged time, productive time, changes from last week,
   daily subtotals, goal thresholds, daily averages, or percentages. Other layouts
   exposing only component categories are unsupported until separately specified.
2. Accept fixture-backed compact whole-minute durations: `4h 35m`, `4h`, or
   `35m`. Full words, decimal hours, or clock notation require explicit rules
   and tests before support. Use exact decimal/integer conversion and reject
   invalid, negative, ambiguous, or physically impossible values. Zero is valid
   only when explicitly reported. Preserve source rounding; never imply measured
   precision finer than the email provides.
3. Obtain the reporting period from explicit report text. Resolve omitted years
   only with a documented, tested rule using the message date, including
   December/January rollover. Never substitute arrival date or guess “last week”.
   The exclusive end must be seven calendar days after the start, with duration no greater
   than its timezone-aware elapsed length (including daylight-saving transitions).
4. Prefer the MIME representation proven reliable by fixtures; if both supported
   representations contain the metric, require agreement. Duplicate responsive
   HTML blocks with identical values may collapse; differing values are ambiguous.
5. A recognized candidate with an unknown layout, missing total, missing period,
   ambiguous label, or contradictory evidence records `unsupported` with a
   specific reason and contributes no row. Unexpected exceptions, timeouts, or
   invalid worker output record `failed`. Neither outcome creates a zero.

## Table and graph

Publish a dedicated report under
`<notes_dir>/Reports/<instance_id>/Index.md` with a linkable Markdown table and
embedded local PNG. Use a static plotting library such as Matplotlib; add and
lock that dependency during implementation. No Obsidian community plugin or
remote plotting service is required. CSV is available beside each graph for reuse.

The report shows the reporting timezone, metric definition, generation time,
coverage, stale/failure warnings, unresolved conflicts, and a newest-first table:
week start, week end (exclusive), distracting time (`4 h 35 min`), and source-note link(s).
Missing managed notes fall back to links to preserved `.eml` files. Links are
properly encoded and resolved from current note paths whenever reports rebuild.
Escape email-derived text and Markdown control characters.

The graph orders weeks chronologically, labels the y-axis “Distracting time
(hours)”, starts it at zero, and plots reported duration divided by 3,600.
Missing/conflicted weeks remain gaps; do not connect a line across them, infer
zeros, interpolate, or claim a trend. A single point is valid. With no accepted
rows show an explanatory report and no misleading empty plot. The CSV includes
period dates, seconds, precision, timezone, metric, and source archive IDs;
neutralize spreadsheet formula prefixes in any exported text field.

Report files are fully Mailkeep-managed, with ownership markers; personal
commentary belongs in a separate note. Refuse to overwrite an unowned target
or unexpected symlink. Publish CSV and PNG in an immutable generation directory,
then atomically replace `Index.md` linking that complete generation last.
Record publication in SQLite after the index replacement. On interruption,
reconcile using the index generation ID/hashes; a database/file mismatch is
reported stale and can be repaired by rendering again. Retain the preceding
generation until the next successful render and remove only owned obsolete
generations. Never expose an index referencing partially written outputs.

## Proposed CLI and verification

```text
mailkeep processors list
mailkeep processors status [--processor INSTANCE_ID]
mailkeep process-emails --processor INSTANCE_ID --dry-run [--archive-id ID]
mailkeep process-emails --processor INSTANCE_ID --execute [--archive-id ID] [--retry-failed]
mailkeep render-reports --processor INSTANCE_ID --dry-run
mailkeep render-reports --processor INSTANCE_ID --execute
mailkeep processors resolve --processor INSTANCE_ID --result-id ID --reason TEXT --execute
mailkeep processors verify [--processor INSTANCE_ID]
```

`--processor` names a configured instance. Backfill/render require exactly one
mode. Dry-run uses read-only SQLite, creates no locks/directories/migrations,
imports no custom code, and does not parse bodies or render. It reports header
matches, cache disposition, pending versions, and likely actions, not extracted
values. A missing database, unknown archive/instance/result, incompatible schema,
or invalid flags returns an actionable error without mutation. A successful
extraction command renders affected reports; render-only never reruns extraction.
`--retry-failed` retries both failed and unsupported attempts for the selected
current keys. Conflict resolution selects an existing candidate, not an edited
number, then rebuilds the affected report.

These commands use no Gmail/IMAP, Jev, Ollama, or other network call. Mutating
commands acquire the existing data lock before migrations, planning, and writes;
candidate selection is rechecked while locked. Explicit processing/rendering
commands return 1 for selected extraction/render failures, unsupported candidates,
or unresolved conflicts, 2 for invalid configuration/usage, and 0 for successful
work or an unchanged/no-match run. Status includes counts, last successful
attempt, current version/options, stale selections, conflicts, and report age.

Processor verification checks referential integrity, result hashes/schema,
source hashes, selections, and report manifests, and exits nonzero for corrupt
results or stale/missing claimed outputs. Recorded extraction failures are shown
as status warnings. Keep this distinct from `mailkeep verify` and pruning so a
missing graph cannot alter preservation or retention eligibility.

## Implementation sequence and acceptance criteria

1. Use the inspected example to build synthetic fixtures for the established
   metric and matching rules. The date-boundary convention and account timezone
   have now been confirmed by the user.
2. Add the registry/protocol, strict configuration, independent fingerprints,
   worker, migration, transaction/cache logic, and local processing/status CLI.
3. Add the deterministic RescueTime parser and duplicate/conflict resolution.
4. Add report generation, archive integration, separate verification, and docs.
5. Verify with isolated local fixtures, then plan a local backup and explicit
   single-message trial before enabling scheduled processing/backfill.

Acceptance requires:

- The supplied example yields `17h 8m` = 61,680 seconds = 1,028 minutes, from
  `All Distracting Time`, in agreement across plain text and HTML. It must not
  select the `10h 30m` goal threshold, `2h 26m` daily average, productive goal,
  total tracked time, or subject duration. Fixture variants change the actual
  value and goal threshold independently to prevent hard-coded extraction.
- Missing, duplicated contradictory, restricted-hours, or renamed distracting
  goals produce an explicit unsupported result; no whole-week total is invented.
- Fixture-backed plain-text/HTML extraction, duplicate responsive markup,
  MIME decoding defects, duration rounding, explicit zero, missing/ambiguous
  values, unknown layouts, spoofed display names, and nonmatching mail.
- Correct report dates across year rollover, leap years, timezone/DST boundaries,
  late arrival, invalid periods, and impossible durations.
- Repeated runs and duplicate deliveries yield one logical weekly value;
  equivalent reports retain all sources; disagreements/overlaps create visible
  conflicts, and explicit resolutions are durable and auditable.
- Parser/options upgrades preserve history, failed upgrades retain a visibly
  stale previous value, retries are deliberate, and rendering can rebuild from
  stored records without rerunning plugins or enrichment.
- Worker timeout/crash/malformed response, invalid schema, partial transactions,
  concurrent commands, render failures, and interrupted publication recover
  without partial records or broken current report links.
- Dry-run is byte-for-byte read-only, including an unmigrated database; custom
  code is not imported. Disabled configuration preserves current behavior and
  enrichment cache identity. Processor failures never change archive gates.
- Table/CSV/graph agree on dates, units, values, source references, conflicts,
  and missing weeks. Verify zero/one/many rows and personal/unowned file safety.
- Run the full existing suite plus meaningful feature tests, migration and
  package-content checks, and whitespace checks. Inspect the generated report
  Markdown/CSV and graph via filesystem access to the Obsidian directory;
  opening the Obsidian app is not part of automated verification.

Implementation is authorized. Live enablement requires an isolated trial and
local backup/verification; timezone/date confirmation is complete. Gmail
actions and scheduled backfill are not part of implementation validation.

## Implementation interface details

Custom installed plugins provide a `module:factory` plus a declarative definition
in their instance configuration: ID, API/processor/schema versions, scalar field
schemas for options and records, and a time-series view. The worker checks that
the factory's definition agrees with configuration. The host validates types and
evidence without importing custom code. Required fields and optional defaults
are explicit; unknown fields are rejected. The bundled RescueTime example supplies
its definition through the built-in registry and is disabled by default.

Current-result selections and conflict resolutions have separate append-only
tables. A conflict resolution identifies the complete connected overlap group;
any newly introduced candidate invalidates that resolution so new disagreements
cannot be silently hidden. Exact duplicate observations use all source links.
Changing series identity or matching rules makes previous selections ineligible;
extraction-only upgrades can retain a visibly stale previous result.

Execution scratch is under workspace `tmp/codex/processors/` when available,
otherwise under an explicitly configured workspace scratch root. It never uses
the system temporary directory. Worker output uses size-limited regular files,
not unbounded subprocess pipes. Read-only CLI commands never import custom code.

## Obsidian historical RescueTime inputs

The user requested a single continuous report containing historical values and
future email observations, with historical inputs stored only in Obsidian. Do
not create synthetic archives or import historical observations into SQLite.

An optional user-owned `Historical data.md` in the RescueTime report directory
contains a marked table of supplied date, normalised exclusive week-end date,
and decimal distracting hours. Preserve supplied labels and one-decimal source
precision. Per user direction, Saturday labels advance one day to Sunday; Sunday
labels remain unchanged. Reject other weekdays or inconsistent normalisation.
Week starts are seven calendar days before normalised exclusive ends; the
configured timezone applies. Validate unique dates, finite nonnegative durations
within their timezone-aware week, and whole-second precision/conversion.

The renderer reads this local note and includes its content hash in the report
fingerprint. Its absence preserves ordinary email-only behaviour. It is not
rewritten or deleted by report generation. Malformed input produces a visible
report failure and retains the previously published report. The normal archive
flow continues as before. Changing the note makes report verification stale until
the report is rebuilt. No schema migration or new import command is required.

Merge by normalised week-end date. Prefer the precise email value whenever an
email-derived observation exists for that week, retain links to both sources,
and never plot two points for the same week. If the email value does not round
to the supplied historical value at source precision, show a reconciliation
warning while retaining the preferred email value. Existing unresolved email
conflicts must not be hidden by historical input. Missing weeks remain gaps.

The report identifies imported values as decimal hours and links them to the
historical source note; it must not imply minute-level measurement precision.
The report table, CSV, and graph cover the same observations. CSV includes source
type, original label, and the historical value alongside any preferred email
value. Source archive IDs are blank for history-only observations. Automatic
archive-time and explicit report rebuilding must retain the historical data.

Acceptance: mixed date formats and Saturday/Sunday normalisation; source precision;
rounding agreement and disagreement; historical-only and email-overlapping data;
idempotent report rebuilding; no changes to archive counts or parser result counts;
source edits detected by verification; malformed notes preserve the previous
report; missing-week gaps; no duplicate points; existing tests pass.

## Smoothed weekly trend

Weekly graphs overlay a four-week trailing arithmetic mean in a contrasting
orange line, keeping the original observations visible in muted blue. Label both
series in a legend and explain the smoother in the report. The average uses
exactly four contiguous accepted weekly observations, including explicit zeros;
it starts at the fourth observation and restarts after any missing/conflicted
week. Do not interpolate, bridge gaps, use partial windows, or change the stored
values/table/CSV. Non-weekly datasets retain their existing graph behaviour.
Increment the renderer version so existing reports rebuild. Tests verify known
averages and date alignment, zero values, short series, and resets across gaps.
