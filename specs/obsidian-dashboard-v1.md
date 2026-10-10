# Obsidian dashboard, version 1

Status: Approved for implementation

## Purpose

Mailkeep can provision an optional dashboard for browsing its derived Obsidian
reading notes. The dashboard is a navigation aid, not authoritative archive
state. It uses Obsidian's native Bases core plugin to query note properties, so
new reading notes appear without regenerating the dashboard or adding work to
the archive lifecycle.

The dashboard consists of two files below the configured Obsidian folder:

- `Index.md`, the human-facing landing page; and
- `Mailkeep.base`, the dynamic Base embedded by the landing page.

Both files become user-owned as soon as they are created. Mailkeep never
overwrites, verifies, regenerates, or removes them.

## Command and safety contract

The local-only command is:

```text
mailkeep setup-dashboard --dry-run
mailkeep setup-dashboard --execute
```

Exactly one mode is required. The command loads the normal Mailkeep
configuration to locate `paths.obsidian_vault` and `paths.obsidian_folder`, but
does not require or open the archive database and makes no Gmail, IMAP,
enrichment, attachment-extraction, or other network call.

Dry-run performs no filesystem mutation. It reports one JSON item for each
dashboard file, with its path relative to the configured Obsidian folder and an
action of `create` or `preserve_existing`.

Execute creates the configured Obsidian folder when necessary and publishes
each missing file without overwriting an existing path. Existing regular
files, directories, and symbolic links are not read or followed; they are
reported as `preserve_existing`. A newly created file is published atomically.
If the two-file operation fails after creating one target, Mailkeep removes
only files created by that invocation and leaves all pre-existing paths
untouched. Successful creation changes the action to `created`.

Created and preserved files are successful outcomes. Filesystem failures return
exit status 1; configuration and invalid CLI usage return 2. Routine
`archive`, `verify`, `regenerate-notes`, and `organize-notes` operations neither
create nor inspect the dashboard.

Mailkeep does not enable or modify Obsidian plugins. Documentation tells the
operator to enable the Bases core plugin if the embedded view does not render.

## Landing page

`Index.md` contains:

1. a short explanation that Mailkeep notes are derived reading copies and the
   preserved message remains available from each note's Source link;
2. an explicit warning that displayed properties are Mailkeep-managed and
   editing cells can make `mailkeep verify` fail or be replaced by note
   regeneration;
3. brief guidance to use the Base toolbar for view switching, filtering, and
   search;
4. a link to open `Mailkeep.base` directly; and
5. one `![[Mailkeep.base#Recent]]` embed.

The page has no Mailkeep managed-region markers or managed hash.

## Base query and properties

The Base applies these filters to every view:

- `file.ext == "md"`;
- `file.inFolder(this.file.folder)`; and
- `file.hasProperty("subject")`.

Using `this.file.folder` keeps the Base portable when the configured Mailkeep
folder changes. Requiring `subject` includes every Mailkeep-generated reading
note, including undated notes, while excluding `Index.md`.

The Base defines an icon-only link formula for opening each note and a sender
formula that removes a trailing ` <address>` from each `from` value while
leaving address-only senders readable. It defines readable display names for
the formulas plus `date`, `to`, `subject`, and `categories`. `document_type`
remains available to the Documents filter and grouping but is not displayed as
a table column. The Base also defines a column for each configured extraction
field that matches `[a-z][a-z0-9_]*` and is not one of the reserved reading-note
properties. Extraction-field display names replace underscores with spaces and
use title case. Unsafe or reserved configured names are omitted, matching the
reading-note property contract.

The Base does not calculate financial totals. Values such as `amount` remain
display strings rather than assumed numeric data.

## Views

The first view is the default.

### Recent

- table view;
- newest message `date` first;
- limit 50 results; and
- columns: a narrow mail-icon note link, date, sender names, subject, and
  categories.

Missing dates sort after dated messages.

### All mail

- table view with no result limit;
- newest message `date` first;
- columns: a narrow mail-icon note link, date, sender names, to, subject,
  categories, and all eligible configured extraction fields; and
- a `Filled` summary on `subject`, providing the displayed message count.

### Categories

- table view filtered to notes that have `categories`;
- grouped by `categories` in ascending order;
- newest message `date` first; and
- columns: a narrow mail-icon note link, date, sender names, subject, and
  categories.

### Documents

- table view filtered to notes that have `document_type` or at least one
  eligible configured extraction field;
- grouped by `document_type` in ascending order, including an empty group for
  notes selected only by an extraction field;
- newest message `date` first; and
- columns: a narrow mail-icon note link, date, sender names, subject, and all
  eligible configured extraction fields. Document type appears only in group
  headings.

When no extraction fields are eligible, the Documents filter and columns use
`document_type` alone.

## User ownership and upgrades

Mailkeep does not compare an existing dashboard with its built-in template.
Rerunning setup reports existing targets as `preserve_existing` and does not
read them. This protects custom views and landing-page edits byte-for-byte.

To restore a missing default file, the operator moves or deletes that file and
reruns setup. A later Mailkeep release must not silently upgrade a dashboard
created by this version; any migration requires a separately specified,
explicit operation.

## Acceptance criteria

- Parser tests require exactly one setup mode.
- Dry-run is byte-for-byte read-only and works when the database is absent.
- Execute creates valid UTF-8 Markdown and valid Base YAML at the two fixed
  relative paths.
- The Base has the global filters, four views, first-view ordering, columns,
  compact link and sender formulas, sorts, grouping, limit, summary, and dynamic
  safe extraction fields specified above.
- Execute is rerunnable and never reads, follows, or overwrites existing
  dashboard targets.
- Partial-existing cases create only the missing target.
- A simulated second-file failure removes only a first file created by that
  invocation.
- Existing managed message notes, SQLite state, archive objects, and provider
  state are unchanged by both modes.
- The full automated suite, package build/content inspection, and whitespace
  checks pass.
- Live rollout runs dry-run before execute, renders the four views in Obsidian,
  confirms the current notes are queryable, and confirms a subsequently added
  qualifying note appears without either dashboard file changing.
