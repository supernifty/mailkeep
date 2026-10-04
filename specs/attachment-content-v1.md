# Attachment content extraction, phase 1

Status: Approved for implementation

## Scope

Mailkeep extracts local text from preserved attachment bytes after preservation
has verified those bytes. Phase 1 supports text-layer PDFs and `text/plain`.
PDF support is selected by the `%PDF-` content signature rather than filename or
declared MIME type. Plain text uses its declared charset when present and strict
UTF-8 otherwise.

OCR, standalone images, Office files, archive/container formats, and semantic
interpretation of charts or graphics are not supported. Jev remains body-only.

## Configuration and defaults

`attachment_extraction` is optional and defaults to enabled. It contains:

- `enabled: true`;
- `max_pdf_bytes: 26214400` (25 MiB);
- `max_pdf_pages: 100`;
- `max_text_bytes: 2097152` (2 MiB);
- `max_output_bytes: 5242880` (5 MiB UTF-8 per attachment);
- `max_supported_attachments_per_message: 20`;
- `timeout_seconds_per_attachment: 60`;
- `timeout_seconds_per_message: 120`;
- `note_chars_per_attachment: 50000`;
- `note_chars_per_message: 100000`.

Extraction has its own deterministic configuration fingerprint. Adding or
changing extraction configuration must not alter the enrichment fingerprint or
invalidate existing Jev cache entries. Existing configuration files that omit
this section inherit the defaults. Setting `enabled: false` is an operational
escape hatch and records no automatic extraction attempts.

## Archive flow and failure independence

The normal executed archive sequence is:

```text
preserve originals -> verify originals -> extract supported attachments
-> body-only enrichment -> write and verify note -> mark provider processed
```

Extraction is best-effort. Unsupported, encrypted, malformed, oversized,
image-only, blank, timed-out, crashed, resource-limited, and output-limited
attachments receive explicit outcomes. Such outcomes do not prevent note
creation, provider movement, or later pruning and never change preservation
state. Extraction is not part of `complete_verified` or any deletion gate.

The original attachment file is opened read-only. Tests compare its size and
SHA-256 before and after extraction.

## Parser isolation

PDF and text decoding run in a Mailkeep worker subprocess launched directly
with the current project Python interpreter, without a shell. The worker
disables socket creation and applies available OS process limits for address
space, CPU time, output file size, and open descriptors. The parent enforces
per-attachment and per-message elapsed-time limits, captures bounded JSON
output, and converts a timeout, signal, malformed worker response, or nonzero
exit into a recorded non-fatal outcome. No parser performs network requests or
executes embedded PDF actions, scripts, programs, or external resources.

`pypdf>=6.19,<7` is an ordinary locked runtime dependency. It is not vendored
and requires no separately installed executable. PDF text uses pypdf's
fixed-width `layout` extraction mode so horizontal positioning, including
table-like columns, remains readable when the PDF provides usable coordinates.
This preserves visual alignment but does not claim semantic row, column,
header, merged-cell, or table boundaries.

## Derived storage and SQLite

Successful UTF-8 text and a canonical provenance manifest are immutable,
content-addressed files below `archive_root/derived/attachments/`. A manifest
records the attachment SHA-256, parser and version, extraction configuration
hash, outcome, page boundaries where applicable, output SHA-256 and byte size,
and warnings. Atomic staging and rename are used when publishing derived files.

A numbered migration creates extraction result and append-only attempt tables
and records a declared attachment charset. Applying the migration never parses
attachments. Results are uniquely cached by attachment SHA-256, parser,
parser version, and extraction configuration hash, so identical attachment
bytes are parsed once across messages. Every requested extraction still gains
an append-only attempt row identifying whether the result was reused. Parser or
configuration changes select a new cache key and preserve older history.

The latest applicable attempt is the attachment's visible state. Normal runs
do not repeat a recorded failure for the same cache key. `--retry-failed`
explicitly permits a new attempt. Deterministic unsupported and limit outcomes
are reconsidered when the parser version or extraction configuration changes.

## Notes

Every generated note contains a visible `## Attachments` section with a local
file link, display name, media type, size, and SHA-256 for every original
attachment, including unsupported images.

If attachment extraction has a visible state, the note also contains
`## Extracted attachment text`. Successful content is grouped by attachment;
PDF page headings are preserved. Embedded Mailkeep marker strings are escaped.
PDF page text is rendered in a whitespace-preserving Markdown code block;
plain-text attachments remain ordinary text.
At most 50,000 characters per attachment and 100,000 characters per message are
included. When text is truncated, the note says so and links to the complete
derived UTF-8 file. Non-success states are displayed without becoming note or
archive integrity failures.

Regeneration replaces only Mailkeep's front matter and generated region. Text
inside the personal markers is preserved byte-for-byte.

## Commands

```text
mailkeep extract-attachments --dry-run [--archive-id ID]
mailkeep extract-attachments --execute [--archive-id ID] [--retry-failed]
```

The modes are mutually exclusive. `--retry-failed` is valid only with
`--execute`. The command reads only preserved attachment files and makes no
IMAP, Gmail, Jev, Ollama, or other network call. Dry-run reports per-attachment
actions without parsing or regenerating notes. Execute extracts or reuses
results, then deterministically regenerates affected managed notes while
preserving personal content. An unknown archive ID is an error.

## Verification

`mailkeep verify` validates every successful current derived result used by an
attachment: safe paths, file presence, UTF-8 text, output size and SHA-256,
manifest presence and identity, and the result/attachment SHA-256 relationship.
A claimed successful result with a missing or hash-invalid artifact is an
archive-integrity error. Recorded unsupported or failed outcomes are warnings
or status only and do not make verification fail.

## Acceptance criteria

- Repository-owned fixtures cover one-page, multi-page, rotated, Unicode,
  blank, image-only, encrypted, malformed, oversized, misleadingly named,
  duplicate, and nameless PDF attachments.
- Plain-text tests cover declared charsets, strict UTF-8 fallback, malformed
  input, and output truncation.
- Tests cover timeout, worker crash, process-resource failure, the message and
  supported-attachment limits, deterministic cache reuse, retry, and parser or
  configuration invalidation.
- Tests prove original bytes and hashes remain unchanged and Jev receives the
  body only with its existing cache key.
- Archive integration tests cover enabled and disabled extraction, failure
  independence, backfill, note regeneration, personal-region preservation,
  marker escaping, link encoding, note bounds, and image-only attachment links.
- Verification fails for missing or corrupt claimed-success artifacts but not
  for recorded unsupported or failed outcomes.
- The full suite, a real `pypdf` fixture integration, CLI smoke tests, SQLite
  integrity checks, and `mailkeep verify` pass before scheduled archival uses
  the upgraded environment. Live rollout additionally requires a dated
  database backup, integrity check, backfill dry run, one executed backfill,
  regenerated-note inspection, and a final verification.
