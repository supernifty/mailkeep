from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccountConfig(StrictModel):
    account_id: str = Field(min_length=1)
    provider: Literal["gmail", "imap"] = "gmail"
    host: str = Field(min_length=1)
    port: int = Field(default=993, ge=1, le=65535)
    username: str = Field(min_length=1)
    password_env: str = Field(min_length=1)
    source_label: str = "Mailkeep"
    processed_label: str = "Mailkeep-Processed"

    @model_validator(mode="after")
    def distinct_labels(self) -> "AccountConfig":
        if self.source_label == self.processed_label:
            raise ValueError("source_label and processed_label must differ")
        return self


class PathsConfig(StrictModel):
    archive_root: Path
    obsidian_vault: Path
    obsidian_folder: Path = Path("Mailkeep")
    database: Path


class LimitsConfig(StrictModel):
    messages_per_run: int = Field(default=100, ge=1)
    bytes_per_run: int = Field(default=1_073_741_824, ge=1)


class EnrichmentConfig(StrictModel):
    provider: Literal["jev", "ollama", "none"] = "jev"
    model: str = "jev-latest"
    endpoint: str | None = None
    api_key_env: str | None = None
    privacy: Literal["redacted", "local"] = "redacted"
    timeout_seconds: float = Field(default=30, gt=0, le=300)

    @model_validator(mode="after")
    def provider_requirements(self) -> "EnrichmentConfig":
        if self.provider == "jev" and not self.api_key_env:
            raise ValueError("Jev enrichment requires api_key_env")
        if self.provider == "ollama" and self.privacy != "local":
            raise ValueError("Ollama enrichment requires privacy: local")
        return self


class AttachmentExtractionConfig(StrictModel):
    enabled: bool = True
    max_pdf_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    max_pdf_pages: int = Field(default=100, ge=1)
    max_text_bytes: int = Field(default=2 * 1024 * 1024, ge=1)
    max_output_bytes: int = Field(default=5 * 1024 * 1024, ge=1)
    max_supported_attachments_per_message: int = Field(default=20, ge=1)
    timeout_seconds_per_attachment: float = Field(default=60, gt=0, le=600)
    timeout_seconds_per_message: float = Field(default=120, gt=0, le=3600)
    note_chars_per_attachment: int = Field(default=50_000, ge=0)
    note_chars_per_message: int = Field(default=100_000, ge=0)

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"enabled", "note_chars_per_attachment",
                                                        "note_chars_per_message"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class Config(StrictModel):
    schema_version: Literal[1] = 1
    account: AccountConfig
    paths: PathsConfig
    env_file: Path | None = None
    retention_days: int = Field(default=90, ge=0)
    deletion_enabled: bool = False
    limits: LimitsConfig = LimitsConfig()
    categories: dict[str, float] = {}
    document_types: tuple[str, ...] = ()
    document_type_confidence: float = Field(default=0.7, ge=0, le=1)
    extraction_fields: tuple[str, ...] = ()
    attachment_extraction: AttachmentExtractionConfig = AttachmentExtractionConfig()
    enrichment: EnrichmentConfig = EnrichmentConfig(
        provider="jev", api_key_env="MAILKEEP_JEV_API_KEY"
    )

    @field_validator("categories")
    @classmethod
    def valid_thresholds(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not name or threshold < 0 or threshold > 1 for name, threshold in value.items()):
            raise ValueError("category names must be nonempty and thresholds in [0, 1]")
        return value

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"env_file"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def enrichment_fingerprint(self) -> str:
        """Retain the pre-attachment-feature cache key used by existing rows."""
        payload = self.model_dump(
            mode="json", exclude={"env_file", "attachment_extraction"}
        )
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @property
    def notes_dir(self) -> Path:
        return self.paths.obsidian_vault / self.paths.obsidian_folder

    def password(self) -> str | None:
        return os.environ.get(self.account.password_env)

    def enrichment_key(self) -> str | None:
        name = self.enrichment.api_key_env
        return os.environ.get(name) if name else None


def _expand_path(value: str | Path, base: Path) -> Path:
    expanded = Path(os.path.expandvars(str(value))).expanduser()
    return expanded if expanded.is_absolute() else (base / expanded).resolve()


def _load_env(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"configured env file does not exist: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid env file line in {path}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "a").isalnum():
            raise ValueError(f"invalid environment variable name in {path}")
        os.environ.setdefault(key, value.strip().strip("'\""))


def load_config(path: str | Path) -> Config:
    config_path = Path(path).expanduser().resolve()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a mapping")
    base = config_path.parent
    paths = raw.get("paths")
    if isinstance(paths, dict):
        for key in ("archive_root", "obsidian_vault", "database"):
            if key in paths:
                paths[key] = _expand_path(paths[key], base)
        if "obsidian_folder" in paths:
            folder = Path(os.path.expandvars(str(paths["obsidian_folder"]))).expanduser()
            if folder.is_absolute() or ".." in folder.parts:
                raise ValueError("obsidian_folder must be a safe relative path")
            paths["obsidian_folder"] = folder
    if raw.get("env_file") is not None:
        raw["env_file"] = _expand_path(raw["env_file"], base)
        _load_env(raw["env_file"])
    return Config.model_validate(raw)
