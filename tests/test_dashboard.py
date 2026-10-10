from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import mailkeep.dashboard as dashboard
from mailkeep.dashboard import (
    BASE_NAME,
    INDEX_NAME,
    build_dashboard_base,
    setup_dashboard,
)


def test_dashboard_base_has_four_live_views_and_configured_fields(config_factory):
    config = config_factory().model_copy(update={
        "extraction_fields": (
            "amount", "due_date", "amount", "Bad Field", "subject",
        ),
    })

    value = yaml.safe_load(build_dashboard_base(config))

    assert value["filters"] == {"and": [
        'file.ext == "md"',
        "file.inFolder(this.file.folder)",
        'file.hasProperty("subject")',
    ]}
    assert value["formulas"] == {
        "open": 'link(file.path, icon("mail"))',
        "sender_names": (
            'list(note.from).filter(value.isType("string"))'
            '.map(value.replace(/\\s*<[^>]+>$/, "")).join(", ")'
        ),
    }
    assert list(value["properties"]) == [
        "formula.open", "formula.sender_names", "note.date", "note.to",
        "note.subject", "note.categories", "note.amount", "note.due_date",
    ]
    assert value["properties"]["note.due_date"]["displayName"] == "Due Date"

    views = value["views"]
    assert [view["name"] for view in views] == [
        "Recent", "All mail", "Categories", "Documents",
    ]
    assert views[0]["limit"] == 50
    assert views[0]["sort"] == [{"property": "note.date", "direction": "DESC"}]
    assert "note.to" not in views[0]["order"]
    assert "note.document_type" not in views[0]["order"]
    assert views[0]["order"][:3] == [
        "formula.open", "note.date", "formula.sender_names",
    ]
    assert views[0]["columnSize"] == {"formula.open": 44}
    assert views[1]["summaries"] == {"note.subject": "Filled"}
    assert views[1]["order"][-2:] == ["note.amount", "note.due_date"]
    assert views[2]["filters"] == {"and": ['file.hasProperty("categories")']}
    assert views[2]["groupBy"] == {
        "property": "note.categories", "direction": "ASC",
    }
    assert views[3]["filters"] == {"or": [
        'file.hasProperty("document_type")',
        'file.hasProperty("amount")',
        'file.hasProperty("due_date")',
    ]}
    assert views[3]["groupBy"] == {
        "property": "note.document_type", "direction": "ASC",
    }
    assert all("note.document_type" not in view["order"] for view in views)


def test_dashboard_documents_view_works_without_extraction_fields(config_factory):
    config = config_factory().model_copy(update={"extraction_fields": ()})

    value = yaml.safe_load(build_dashboard_base(config))
    documents = value["views"][3]

    assert documents["filters"] == {
        "or": ['file.hasProperty("document_type")'],
    }
    assert documents["order"] == [
        "formula.open", "note.date", "formula.sender_names", "note.subject",
    ]


def test_setup_dashboard_dry_run_is_read_only_without_database(config_factory):
    config = config_factory()
    assert not config.notes_dir.exists()
    assert not config.paths.database.exists()

    result = setup_dashboard(config, execute=False)

    assert result == [
        {"path": INDEX_NAME, "action": "create"},
        {"path": BASE_NAME, "action": "create"},
    ]
    assert not config.notes_dir.exists()
    assert not config.paths.database.exists()


def test_setup_dashboard_creates_then_preserves_user_owned_files(config_factory):
    config = config_factory()

    first = setup_dashboard(config, execute=True)

    assert first == [
        {"path": INDEX_NAME, "action": "created"},
        {"path": BASE_NAME, "action": "created"},
    ]
    index = config.notes_dir / INDEX_NAME
    base = config.notes_dir / BASE_NAME
    assert "![[Mailkeep.base#Recent]]" in index.read_text(encoding="utf-8")
    assert yaml.safe_load(base.read_text(encoding="utf-8"))["views"][0]["name"] == "Recent"
    assert not config.paths.database.exists()

    index.write_text("# My custom dashboard\n", encoding="utf-8")
    base.write_text("views:\n  - type: table\n    name: Mine\n", encoding="utf-8")
    before = (index.read_bytes(), base.read_bytes())

    second = setup_dashboard(config, execute=True)

    assert second == [
        {"path": INDEX_NAME, "action": "preserve_existing"},
        {"path": BASE_NAME, "action": "preserve_existing"},
    ]
    assert (index.read_bytes(), base.read_bytes()) == before


def test_setup_dashboard_creates_only_a_missing_target(config_factory):
    config = config_factory()
    config.notes_dir.mkdir(parents=True)
    index = config.notes_dir / INDEX_NAME
    index.write_text("custom\n", encoding="utf-8")

    result = setup_dashboard(config, execute=True)

    assert result == [
        {"path": INDEX_NAME, "action": "preserve_existing"},
        {"path": BASE_NAME, "action": "created"},
    ]
    assert index.read_text(encoding="utf-8") == "custom\n"
    assert (config.notes_dir / BASE_NAME).is_file()


def test_setup_dashboard_preserves_existing_symlink_without_following(
    config_factory, tmp_path: Path,
):
    config = config_factory()
    config.notes_dir.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    (config.notes_dir / INDEX_NAME).symlink_to(outside)

    result = setup_dashboard(config, execute=True)

    assert result[0] == {"path": INDEX_NAME, "action": "preserve_existing"}
    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert (config.notes_dir / INDEX_NAME).is_symlink()


def test_setup_dashboard_rolls_back_files_created_by_failed_invocation(
    config_factory, monkeypatch: pytest.MonkeyPatch,
):
    config = config_factory()
    original = dashboard._publish_new
    calls = 0

    def fail_second(path: Path, content: str):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated failure")
        return original(path, content)

    monkeypatch.setattr(dashboard, "_publish_new", fail_second)

    with pytest.raises(OSError, match="simulated failure"):
        setup_dashboard(config, execute=True)

    assert not (config.notes_dir / INDEX_NAME).exists()
    assert not (config.notes_dir / BASE_NAME).exists()


def test_setup_dashboard_requires_existing_vault(config_factory):
    config = config_factory()
    config.paths.obsidian_vault.rmdir()

    with pytest.raises(RuntimeError, match="Obsidian vault does not exist"):
        setup_dashboard(config, execute=True)
