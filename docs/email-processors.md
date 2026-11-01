# Email processor plugins

Mailkeep can run explicitly configured, trusted Python plugins over verified
archived emails. Plugins return structured records with source evidence;
Mailkeep stores their history and creates Markdown, CSV, and PNG reports.
They do not control Gmail movement, note integrity, or retention.

## Enable the bundled RescueTime example

Add this section to your configuration, confirming the timezone against your
RescueTime account. No plugins run by default.

```yaml
processors:
  enabled: true
  scratch_root: ./tmp/codex/processors
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

The plugin reads the actual achieved duration for **All Distracting Time** in
the report's weekly goals, requiring **All day, every day**. For example, the
actual `17h 8m` becomes 61,680 seconds. It does not use the goal threshold, daily
average, productive time, or the category duration in the subject.

Version 1 supports the inspected English layout, compact whole-minute durations
(`17h 8m`, `17h`, `8m`), and explicit full-year date endpoints separated by seven
days. End dates are exclusive: October 25–November 1 covers the week starting
October 25. The original range and rounding precision remain recorded. Unknown
layouts, restricted-hours goals, absent/contradictory values, and disagreement
between MIME alternatives produce an unsupported result rather than a guessed
number. Separate reporting accounts require separate instances/series.

New matching mail is processed during normal archival. It must first enter
Mailkeep's configured source label; Mailkeep does not create a Gmail filter or
fetch the report's remote links. Enabling a plugin does not backfill old mail.

Before upgrading a live database, make a dated SQLite backup using its backup
API and check its integrity. An execute command applies the additive processor
migration while holding Mailkeep's existing lock. Preserve the archive and notes
through your usual independent backup process as well.

```sh
mailkeep processors list
mailkeep processors status --processor personal-rescuetime

mailkeep process-emails --processor personal-rescuetime --dry-run
mailkeep process-emails --processor personal-rescuetime --execute

mailkeep processors verify --processor personal-rescuetime
```

Dry-run reads verified archives and reports header matches and cache actions;
it does not parse bodies, execute/import custom plugins, migrate the database,
or write files. Use `--archive-id ID` on `process-emails` for a single-message
trial. Execution uses only local archives and no enrichment service or IMAP.
No matching local archives is a successful empty result.

Reports appear at `Mailkeep/Reports/personal-rescuetime/Index.md` below the
configured vault folder. Add your own link to this page from your dashboard.
The report includes a weekly table, source-email links, a graph, and CSV. An
empty dataset shows an explanation without a graph. Missing/conflicted weeks
remain gaps. Email notes and existing dashboard files are not modified.

Weekly graphs also show an orange four-week trailing average over the original
blue values. Each average uses four consecutive reported weeks; it starts at the
fourth point and restarts after gaps, without partial windows or interpolation.
Report directories are Mailkeep-owned; keep personal commentary elsewhere.

## Retry, conflicts, and rebuilding

Existing RescueTime history may be kept entirely in the vault in a user-owned
`Historical data.md` beside the report. The renderer includes it whenever the
report rebuilds; it does not create emails or historical database records.
Historical Saturday labels move to the next Sunday; Sunday labels are unchanged.
The normalised Sunday is the exclusive weekly end. Preserve original labels
and decimal-hour precision in the following marked table:

```markdown
<!-- mailkeep:historical-rescuetime:start -->
| Supplied date | Week end (exclusive) | Distracting hours |
| --- | --- | --- |
| 31-10-26 | 2026-11-01 | 17.1 |
<!-- mailkeep:historical-rescuetime:end -->
```

Dates may use day-first numeric forms, English abbreviated months, or ISO dates.
Duplicate or inconsistent dates and invalid durations are rejected. Source-note
edits make the report stale until it is rebuilt. Email values take precedence for
overlapping weeks, with both sources linked and one point; discrepancies at the
historical precision are shown as reconciliation warnings. Malformed historical
input retains the last published report and warns during automatic archival.
The graph continues to leave missing weeks as gaps. The source note is never
rewritten by Mailkeep and belongs in your vault backup.

Unchanged results are reused. Failed/unsupported keys are retried only explicitly:

```sh
mailkeep process-emails --processor personal-rescuetime --execute --retry-failed
mailkeep render-reports --processor personal-rescuetime --dry-run
mailkeep render-reports --processor personal-rescuetime --execute
```

Render-only commands rebuild files from stored results without rerunning plugins.
Use them after reorganising source notes or repairing a damaged graph. Missing
source notes fall back to the preserved `.eml` link. Never delete source archives
to repair a derived report.

Repeated emails with the same week/value yield one point and retain all sources.
Disagreements and overlapping weeks are shown as conflicts and excluded from the
graph. Choose a candidate result ID shown in the report:

```sh
mailkeep processors resolve --processor personal-rescuetime \
  --result-id 12 --reason "Confirmed corrected report" --execute
```

The reason and candidates are retained. New candidates invalidate a resolution
for the old group. Plugin/configuration upgrades retain previous accepted values
with a stale warning until a replacement succeeds. Changing series or matching
identity excludes old selections from the new series without deleting history.
Disabling plugins stops automatic extraction and preserves existing reports.

Explicit process/render commands exit 1 for extraction failures, unsupported
reports, report failures, or unresolved conflicts; invalid usage/configuration
exits 2. Archive commands keep their existing exit semantics and expose optional
plugin failures in their JSON and stderr. `processors verify` checks results,
source evidence/hashes, report files, and publication freshness independently of
`mailkeep verify` and retention gates.

## Add a trusted custom plugin

The RescueTime example is implemented in
[`rescuetime.py`](../src/mailkeep/processors/rescuetime.py). The small shared API
is in [`api.py`](../src/mailkeep/processors/api.py).

Install your module in the same Python environment as Mailkeep. Its factory
returns an object with `definition` (a `Definition` instance or matching mapping)
and `process(message, options)`. Configure its fully qualified factory:

```yaml
implementation: my_package.weekly_report:create
definition:
  id: weekly-example
  api_version: 1
  processor_version: "1"
  schema_version: "1"
  options: {}
  fields:
    start: {type: date}
    end: {type: date}
    quantity: {type: number}
  view:
    start: start
    end_exclusive: end
    value: quantity
    label: Example quantity
    metric: weekly_quantity
    unit: units
    scale: 1
    period_days: 7
```

This replaces the `implementation` entry and adds `definition` inside a normal
instance with its own ID, series, sender/subject matching, and `options: {}`.
Use precisely the same definition in the factory's returned object; the worker
rejects disagreement. Built-in definitions cannot be overridden.

`message` is a JSON mapping containing archive ID, raw SHA-256, subject, sender
and recipient address mappings, header date, reading-body Markdown, and decoded
non-attachment MIME `parts`. Each part has a one-based `ordinal`, `media_type`,
`text`, and decoding `diagnostics`. Do not claim extraction from defective parts.

Return one of:

```python
{
    "outcome": "success",
    "records": [{"start": "2026-10-25", "end": "2026-11-01", "quantity": 12.5}],
    "evidence": [{"part": 2, "field": "quantity", "text": "12.5 units"}],
}
# or:
{"outcome": "unsupported", "reason": "unrecognised weekly report layout"}
```

Evidence text must occur in the referenced decoded part (visible text for HTML,
with whitespace normalised). Records must match the declared scalar field types,
have nonnegative finite values, and use non-overlapping periods of `period_days`.
Field types are `string`, `integer`, `number`, `boolean`, and ISO `date`.
Fields are required by default; optional fields need a valid typed `default` and
`required: false`. Unknown record/option keys are rejected. The first API supports
scalar time-series records, not arbitrary side-effecting workflows or renderer
callbacks. Implementations must bump `processor_version` when behaviour changes.

Plugins run in a subprocess with a 10-second timeout, 5 MiB request limit, 1 MiB
response limit, bounded regular-file output, available OS resource limits,
network guard, and a minimal environment. Scratch remains beneath the configured
workspace directory and worker directories are cleaned after execution.
This is not an OS security sandbox: installed plugins must be trusted and can
otherwise execute as your user. Never load code from email or attachments.

The full feature contract and acceptance criteria are in
[`email-processors-v1.md`](../specs/email-processors-v1.md).
