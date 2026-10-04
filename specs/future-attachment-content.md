# Future requirement: attachment-content parsing and enrichment

Status: Later phases; phase 1 is specified in `attachment-content-v1.md`

## Problem and user need

Important email information often exists only in attachments. Invoices, bills,
statements, bookings, reports, and correspondence may have an almost empty email
body while their amount, due date, reference, and substantive text are stored in
a PDF, Office document, or scanned image. Body-only enrichment therefore cannot
provide a complete Obsidian index.

Mailkeep phase 1 extracts text-layer PDFs and plain text without weakening
exact-byte preservation. This document now covers the deferred formats, OCR,
and attachment-aware enrichment that remain after phase 1.

## Intended behavior

1. Preserve and verify the original attachment bytes before parsing them.
   Extracted text and OCR are derived artifacts; they never replace or modify
   the archived attachment.
2. Extend the phase-1 pipeline to additional text-bearing formats:
   - scanned PDF and common image formats through OCR;
   - DOCX, XLSX, and PPTX without executing macros, formulas, or embedded code;
   - other safe, explicitly supported textual formats.
3. Record an independent extraction lifecycle per attachment, including parser
   name and version, configuration hash, attempt number, timestamps, outcome,
   warnings, and errors.
4. Store extracted content as an archive-local derived artifact with its own
   SHA-256. Preserve structural provenance where the format permits it:
   PDF page, spreadsheet workbook/sheet/cell or range, presentation slide, and
   document section or paragraph.
5. Make attachment text available to classification, document typing, and
   entity extraction. Every attachment-derived entity must identify the source
   attachment and include evidence plus its page/sheet/slide/section location
   when known.
6. Distinguish body-derived and attachment-derived evidence in SQLite and in the
   Obsidian note. The note should summarize useful extracted information and link
   to the original attachment and derived text; it should not indiscriminately
   paste very large documents into the note.
7. Cache extraction by attachment SHA-256, parser/build, and configuration hash
   so byte-identical attachments need not be parsed repeatedly.
8. Permit deterministic note regeneration when a parser or extraction
   configuration changes while preserving the personal-notes region.

## Safety and privacy constraints

- Attachment parsing is downstream of preservation and cannot establish or
  invalidate the integrity of the original `.eml` or attachment bytes.
- Parsers must operate with explicit file-size, expanded-size, page, pixel,
  worksheet, and time limits. Archive/container expansion must defend against
  zip bombs and path traversal.
- Parsing must not execute macros, formulas, JavaScript, embedded programs, or
  external-resource requests. Password-protected, encrypted, malformed, or
  unsupported files must be reported rather than bypassed.
- Local text extraction is preferred. Attachment bytes must never be sent to a
  hosted AI provider by default. Any future hosted attachment processing would
  require a separate explicit opt-in and privacy specification.
- If locally extracted text is sent to hosted enrichment, the configured
  redaction policy applies, and the record must retain the local source hash and
  redacted-input hash without storing secrets.
- OCR confidence and model output are advisory. Neither may contribute to raw
  preservation or remote-deletion integrity decisions.
- Extracted text can contain hostile prompt-like content. It is untrusted data,
  not an instruction to Mailkeep or an enrichment provider.

## Data and Obsidian requirements

The future schema must associate each derived artifact with the archive ID,
attachment ordinal and SHA-256, original filename and media type, parser/build,
extraction configuration hash, derived-text SHA-256, and source locations.

Obsidian front matter or generated content must be able to represent an entity
such as:

```yaml
- field: due_date
  value: 2026-10-18
  evidence: Payment is due by 18 October 2026
  source:
    kind: attachment
    attachment: September invoice.pdf
    attachment_sha256: 4a1f...
    page: 2
```

Large extracted text belongs in an archive-local derived file. Notes should
contain concise summaries, selected evidence, extraction status, and links.

## Acceptance criteria for the future feature

- Fixtures cover native and scanned PDFs, rotated pages, mixed text/OCR pages,
  DOCX, XLSX, PPTX, images, plain text, duplicate attachments, Unicode names,
  misleading extensions, malformed files, encrypted files, and unsupported
  media types.
- Tests prove original attachment bytes and hashes never change during parsing.
- Tests prove macros/formulas are not executed, external resources are not
  fetched, unsafe archive paths are rejected, and configured resource limits
  stop pathological inputs.
- Extracted text and entities retain exact attachment and page/sheet/slide
  provenance; unsupported or low-confidence values remain absent or explicitly
  uncertain.
- Repeated identical attachments reuse cached extraction, while parser or
  configuration changes trigger new derived results without deleting history.
- Obsidian snapshot tests distinguish body and attachment evidence, link both
  original and derived artifacts, bound note size, and preserve personal notes.
- Extraction failure remains independently visible and retryable without
  damaging the preservation object or erasing a prior successful extraction.

## Decisions required before later-phase implementation

- Select and threat-model the concrete Office and OCR libraries within the
  phase-1 worker-isolation boundary.
- Define OCR languages, image/pixel and container expansion limits, table
  representation, and whether spreadsheet formula text or cached displayed
  values should be indexed.
- Define how re-extraction and note regeneration interact with retention for a
  message already in `Mailkeep-Processed`.
