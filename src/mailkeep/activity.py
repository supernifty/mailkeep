from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config


ACTIVITY_NAME = "Activity.md"
ACTIVITY_HEADER = "# Mailkeep activity"


def activity_path(config: Config) -> Path:
    return config.notes_dir / ACTIVITY_NAME


def archive_entry(results: list[dict[str, Any]], total_archives: int) -> str | None:
    processed = [item for item in results if item.get("outcome") == "processed"]
    failed = [item for item in results if item.get("outcome") == "failed"]
    if not processed and not failed:
        return None
    deduplicated = sum(1 for item in processed if item.get("deduplicated"))
    summary = f"archived {len(processed)}"
    if deduplicated:
        summary += f" ({deduplicated} deduplicated)"
    if failed:
        summary += f", {len(failed)} failed"
    return f"{summary}; {total_archives} total"


def prune_entry(output: list[dict[str, Any]]) -> str | None:
    moved = sum(1 for item in output if item.get("action") == "moved_to_trash")
    absent = sum(1 for item in output if item.get("action") == "recorded_deleted")
    blocked = sum(
        1 for item in output
        if "gates" in item
        and item["gates"].get("retention_elapsed")
        and not item.get("eligible")
    )
    if not (moved or absent or blocked):
        return None
    parts: list[str] = []
    if moved:
        parts.append(f"moved {moved} to Gmail Trash")
    if absent:
        parts.append(f"recorded {absent} absent")
    if blocked:
        parts.append(f"{blocked} blocked by safety gates")
    return "; ".join(parts)


def append_activity(config: Config, entry: str, now: datetime | None = None) -> Path:
    moment = now or datetime.now()
    path = activity_path(config)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if not text.strip():
        text = f"{ACTIVITY_HEADER}\n"
    line = f"- {moment:%Y-%m-%d}: {entry}."
    _write_atomic(path, _insert_entry(text, moment.year, line))
    return path


def _insert_entry(text: str, year: int, line: str) -> str:
    heading = f"## {year}"
    lines = text.rstrip("\n").split("\n")
    if heading in lines:
        start = lines.index(heading)
        end = next(
            (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
            len(lines),
        )
        insert_at = end
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, line)
    else:
        lines.extend(["", heading, "", line])
    return "\n".join(lines) + "\n"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
