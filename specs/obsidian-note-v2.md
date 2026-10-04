# Obsidian reading-note format, version 2

Status: Approved for implementation

## Purpose

Mailkeep's Obsidian note is a concise reading and retrieval copy, not the
authoritative preservation manifest. SQLite, `metadata.json`, the original
message, attachment files, and derived manifests retain technical provenance.
The note should expose only properties useful for Obsidian browsing or queries.

## Property assessment

The five live version-1 notes contain 19 YAML properties. Eleven are internal
identity, configuration, provenance, path, hash, or verification fields. The
nested attachment property accounts for more YAML than any other field while
duplicating the visible attachment section. `document_type`, `entities`, and
attachments are empty in most notes. These properties make the Properties view
harder to scan without improving routine retrieval.

Version 2 front matter contains only:

- `date`: the parsed message date/time when available;
- `from`: a list of readable `Name <address>` strings;
- `to`: a list of readable `Name <address>` strings;
- `subject`;
- `categories`, only when non-empty;
- `document_type`, only when non-empty;
- each configured, evidence-backed extraction field, only when a value exists.

Extraction fields use their configured names as flat properties—for example,
`amount` and `due_date`—rather than the former nested `entities` structure.
Only safe lowercase names containing letters, digits, and underscores are
eligible, and reserved core property names cannot be replaced. The readable
evidence remains in `## Extracted details`.

Empty optional properties are omitted. Internal schema/version, archive ID,
date-source, Message-ID, account, entity evidence, attachment manifests, raw
message hashes and paths, enrichment provider/model, processing time, and the
managed hash are not Obsidian properties. Removing them from the note never
removes them from authoritative storage.

## Generated reading body

The managed body contains, in order when applicable:

1. `## Message` and the readable message body;
2. `## Extracted details` for evidence-backed enrichment entities;
3. `## Attachments` only when attachments exist, with original local links,
   media type, and human-readable size but no displayed SHA-256;
4. `## Extracted attachment text` when extraction was attempted, including
   explicit non-success status and complete-derived-text links when truncated;
5. `## Source` with a local link to the original `.eml`.

Technical identifiers and hashes are intentionally absent from the rendered
reading body. Marker-like untrusted content remains escaped.

## Integrity and compatibility

The managed SHA-256 moves from YAML into a hidden
`<!-- mailkeep:managed-sha256: ... -->` comment. The hash still covers canonical
front matter and the generated region, excluding personal text and the hash
comment itself. Verification accepts legacy YAML hashes until a note is
regenerated. Personal-marker contents are preserved byte-for-byte.

## Regeneration command

```text
mailkeep regenerate-notes --dry-run [--archive-id ID]
mailkeep regenerate-notes --execute [--archive-id ID]
```

The command reads preserved messages plus existing SQLite enrichment and
attachment-extraction results. It performs no Gmail, IMAP, Jev, Ollama, or
other network call. Dry-run reports the affected paths without mutation.
Execute acquires the data lock, verifies preserved originals, regenerates the
managed note format, preserves personal content, updates the stored managed
hash, and verifies every regenerated note. It is rerunnable and an unknown
archive ID is an error.

## Acceptance criteria

- Snapshot tests assert the exact minimal property set, conditional flat
  extraction fields, and omission of empty optional properties.
- Tests prove all removed provenance remains available in SQLite/archive files.
- Evidence-backed entities, original attachment links, original-email links,
  extraction statuses, and truncated-derived links remain readable in the body.
- Legacy notes regenerate to version 2 while preserving personal text exactly.
- Managed-region tampering still fails verification with the hash hidden from
  Obsidian Properties.
- Dry-run is read-only; execute makes no provider or enrichment call.
- Live rollout backs up and integrity-checks SQLite, regenerates all notes,
  verifies them, and confirms the intended property keys without printing
  private values.
