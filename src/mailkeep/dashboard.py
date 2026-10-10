from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .config import Config
from .notes import PROPERTY_FIELD_RE, RESERVED_PROPERTIES


INDEX_NAME = "Index.md"
BASE_NAME = "Mailkeep.base"


def _property_fields(config: Config) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        field for field in config.extraction_fields
        if PROPERTY_FIELD_RE.fullmatch(field) and field not in RESERVED_PROPERTIES
    ))


def build_dashboard_base(config: Config) -> str:
    extraction_fields = _property_fields(config)
    properties: dict[str, dict[str, str]] = {
        "formula.open": {"displayName": ""},
        "formula.sender_names": {"displayName": "From"},
        "note.date": {"displayName": "Date"},
        "note.to": {"displayName": "To"},
        "note.subject": {"displayName": "Subject"},
        "note.categories": {"displayName": "Categories"},
    }
    for field in extraction_fields:
        properties[f"note.{field}"] = {
            "displayName": field.replace("_", " ").title(),
        }

    extraction_columns = [f"note.{field}" for field in extraction_fields]
    document_filters = [
        'file.hasProperty("document_type")',
        *(f'file.hasProperty("{field}")' for field in extraction_fields),
    ]
    base: dict[str, Any] = {
        "filters": {
            "and": [
                'file.ext == "md"',
                "file.inFolder(this.file.folder)",
                'file.hasProperty("subject")',
            ],
        },
        "formulas": {
            "open": 'link(file.path, icon("mail"))',
            "sender_names": (
                'list(note.from).filter(value.isType("string"))'
                '.map(value.replace(/\\s*<[^>]+>$/, "")).join(", ")'
            ),
        },
        "properties": properties,
        "views": [
            {
                "type": "table",
                "name": "Recent",
                "order": [
                    "formula.open", "note.date", "formula.sender_names",
                    "note.subject", "note.categories",
                ],
                "sort": [{"property": "note.date", "direction": "DESC"}],
                "limit": 50,
                "columnSize": {"formula.open": 44},
            },
            {
                "type": "table",
                "name": "All mail",
                "order": [
                    "formula.open", "note.date", "formula.sender_names",
                    "note.to", "note.subject", "note.categories", *extraction_columns,
                ],
                "sort": [{"property": "note.date", "direction": "DESC"}],
                "summaries": {"note.subject": "Filled"},
                "columnSize": {"formula.open": 44},
            },
            {
                "type": "table",
                "name": "Categories",
                "filters": {"and": ['file.hasProperty("categories")']},
                "groupBy": {"property": "note.categories", "direction": "ASC"},
                "order": [
                    "formula.open", "note.date", "formula.sender_names",
                    "note.subject", "note.categories",
                ],
                "sort": [{"property": "note.date", "direction": "DESC"}],
                "columnSize": {"formula.open": 44},
            },
            {
                "type": "table",
                "name": "Documents",
                "filters": {"or": document_filters},
                "groupBy": {"property": "note.document_type", "direction": "ASC"},
                "order": [
                    "formula.open", "note.date", "formula.sender_names",
                    "note.subject", *extraction_columns,
                ],
                "sort": [{"property": "note.date", "direction": "DESC"}],
                "columnSize": {"formula.open": 44},
            },
        ],
    }
    return yaml.safe_dump(base, allow_unicode=True, sort_keys=False, width=1000)


def build_dashboard_index() -> str:
    return """# Mailkeep

Mailkeep notes are convenient reading copies. The preserved original remains
available from the **Source** link in each message note.

> [!warning] Managed properties
> Use this dashboard to search, filter, and open messages. Do not edit the
> displayed message properties in the Base: Mailkeep verification may reject
> those changes, and note regeneration may replace them.

Use the view menu in the embedded Base to switch between **Recent**,
**All mail**, **Categories**, and **Documents**. The toolbar also provides
filtering and search.

[[Mailkeep.base|Open the full Mailkeep dashboard]]

![[Mailkeep.base#Recent]]
"""


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _publish_new(path: Path, content: str) -> tuple[int, int] | None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            return None
        stat = path.stat(follow_symlinks=False)
        return stat.st_dev, stat.st_ino
    finally:
        temporary.unlink(missing_ok=True)


def setup_dashboard(config: Config, execute: bool) -> list[dict[str, str]]:
    notes_dir = config.notes_dir
    targets = [
        (INDEX_NAME, build_dashboard_index()),
        (BASE_NAME, build_dashboard_base(config)),
    ]
    result = [
        {"path": name, "action": "preserve_existing" if _lexists(notes_dir / name) else "create"}
        for name, _ in targets
    ]
    if not execute:
        return result
    if not config.paths.obsidian_vault.is_dir():
        raise RuntimeError(f"Obsidian vault does not exist: {config.paths.obsidian_vault}")
    notes_dir.mkdir(parents=True, exist_ok=True)

    created: list[tuple[Path, tuple[int, int]]] = []
    try:
        for item, (name, content) in zip(result, targets, strict=True):
            if item["action"] == "preserve_existing":
                continue
            path = notes_dir / name
            identity = _publish_new(path, content)
            if identity is None:
                item["action"] = "preserve_existing"
            else:
                created.append((path, identity))
                item["action"] = "created"
    except BaseException:
        for path, identity in reversed(created):
            try:
                stat = path.stat(follow_symlinks=False)
                if (stat.st_dev, stat.st_ino) == identity:
                    path.unlink()
            except FileNotFoundError:
                pass
        raise
    return result
