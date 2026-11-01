from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScalarField(Strict):
    type: Literal["string", "integer", "number", "boolean", "date"]
    required: bool = True
    default: Any = None


class TimeSeries(Strict):
    start: str
    end_exclusive: str
    value: str
    unit: str
    scale: float = Field(default=1, gt=0, allow_inf_nan=False)
    label: str
    metric: str
    period_days: int = Field(default=7, ge=1, le=366)


class Definition(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    api_version: Literal[1] = 1
    processor_version: str = Field(min_length=1, max_length=64)
    schema_version: str = Field(min_length=1, max_length=64)
    options: dict[str, ScalarField]
    fields: dict[str, ScalarField]
    view: TimeSeries

    @model_validator(mode="after")
    def valid_view(self):
        for name in (*self.options, *self.fields):
            if not name.isidentifier() or not name.isascii() or name.startswith("_"):
                raise ValueError("schema fields must be public ASCII identifiers")
        if self.fields.get(self.view.start, ScalarField(type="string")).type != "date":
            raise ValueError("view start must name a date field")
        if self.fields.get(self.view.end_exclusive, ScalarField(type="string")).type != "date":
            raise ValueError("view end must name a date field")
        if self.fields.get(self.view.value, ScalarField(type="string")).type not in ("integer", "number"):
            raise ValueError("view value must name a numeric field")
        return self


class Plugin(Protocol):
    definition: Definition

    def process(self, message: dict, options: dict) -> dict: ...


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_fields(values: dict, schema: dict[str, ScalarField]) -> dict:
    if not isinstance(values, dict) or set(values) - set(schema):
        raise ValueError("unexpected record/option fields")
    result = dict(values)
    for name, field in schema.items():
        if name not in result:
            if field.required:
                raise ValueError(f"missing field: {name}")
            result[name] = field.default
        value = result[name]
        valid = {
            "string": lambda: type(value) is str,
            "integer": lambda: type(value) is int,
            "number": lambda: type(value) in (int, float) and math.isfinite(value),
            "boolean": lambda: type(value) is bool,
            "date": lambda: type(value) is str and date.fromisoformat(value).isoformat() == value,
        }[field.type]
        try:
            accepted = valid()
        except (ValueError, TypeError):
            accepted = False
        if not accepted:
            raise ValueError(f"invalid {field.type} field: {name}")
    return result


def registry() -> dict[str, Definition]:
    from .rescuetime import DEFINITION
    return {"rescuetime-weekly": DEFINITION}


def normalized(text: str) -> str:
    return " ".join(text.split())
