from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from mailkeep.config import load_config


def base_config(tmp_path: Path) -> dict:
    return {
        "schema_version": 1,
        "account": {"account_id": "a", "provider": "gmail", "host": "h",
                    "username": "u", "password_env": "PASSWORD",
                    "source_label": "Mailkeep", "processed_label": "Processed"},
        "paths": {"archive_root": "data/archive", "obsidian_vault": "vault",
                  "obsidian_folder": "Notes", "database": "data/db.sqlite3"},
        "enrichment": {"provider": "none", "privacy": "local"},
    }


def test_load_resolves_paths_relative_to_config(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(base_config(tmp_path)), encoding="utf-8")
    config = load_config(path)
    assert config.paths.archive_root == tmp_path / "data/archive"
    assert config.notes_dir == tmp_path / "vault/Notes"


def test_unknown_configuration_is_rejected(tmp_path: Path):
    raw = base_config(tmp_path)
    raw["surprise"] = True
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(path)


def test_unsafe_note_folder_is_rejected(tmp_path: Path):
    raw = base_config(tmp_path)
    raw["paths"]["obsidian_folder"] = "../escape"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="safe relative"):
        load_config(path)


def test_env_file_does_not_override_process_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PASSWORD", "process-value")
    env = tmp_path / ".env"
    env.write_text("PASSWORD=file-value\n", encoding="utf-8")
    raw = base_config(tmp_path)
    raw["env_file"] = ".env"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert load_config(path).password() == "process-value"
