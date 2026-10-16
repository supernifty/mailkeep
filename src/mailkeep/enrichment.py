from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from .config import Config
from .models import EnrichmentResult


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")
URL_RE = re.compile(r"https?://[^\s<>()]+", re.I)
LONG_ID_RE = re.compile(r"\b(?=[A-Za-z0-9_-]{16,}\b)(?=.*\d)[A-Za-z0-9_-]+\b")


def redact(text: str) -> tuple[str, dict[str, str]]:
    replacements: dict[str, str] = {}
    counters: dict[str, int] = {}

    def replace(kind: str, value: str) -> str:
        key = f"{kind}:{value}"
        if key not in replacements:
            counters[kind] = counters.get(kind, 0) + 1
            replacements[key] = f"<{kind}_{counters[kind]}>"
        return replacements[key]

    text = EMAIL_RE.sub(lambda m: replace("EMAIL", m.group()), text)
    text = PHONE_RE.sub(lambda m: replace("PHONE", m.group()), text)

    def redact_url(match: re.Match[str]) -> str:
        value = match.group()
        base, marker, _query = value.partition("?")
        return base + (marker + replace("URL_PARAMS", value) if marker else "")

    text = URL_RE.sub(redact_url, text)
    text = LONG_ID_RE.sub(lambda m: replace("IDENTIFIER", m.group()), text)
    return text, replacements


def input_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def candidate_values(text: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {
        "amount": re.findall(r"(?:[$€£]\s?\d[\d,]*(?:\.\d{2})?|\b\d[\d,]*\.\d{2}\s?(?:AUD|USD|EUR|GBP)\b)", text),
        "date": re.findall(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b", text),
    }
    return {key: list(dict.fromkeys(found))[:20] for key, found in values.items() if found}


def no_enrichment(config: Config, text: str) -> EnrichmentResult:
    return EnrichmentResult(provider="none", model="none", input_hash=input_hash(text),
                            config_hash=config.fingerprint())


def list_jev_models(config: Config, client_factory: Callable[..., Any] | None = None) -> list[str]:
    from typesafe_sdk import TypeSafeClient

    factory = client_factory or TypeSafeClient
    kwargs: dict[str, Any] = {"timeout": config.enrichment.timeout_seconds}
    key = config.enrichment_key()
    if key:
        kwargs["api_key"] = key
    if config.enrichment.endpoint:
        kwargs["base_url"] = config.enrichment.endpoint
    with factory(**kwargs) as client:
        response = client.models.list()
    return [str(model.name) for model in response.models]


def enrich_ollama(config: Config, text: str, client: httpx.Client | None = None) -> EnrichmentResult:
    start = time.monotonic()
    fields = list(config.extraction_fields)
    schema = {
        "type": "object",
        "properties": {
            "categories": {"type": "object", "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1}},
            "document_type": {"type": ["string", "null"]},
            "document_type_confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
            "entities": {"type": "array", "items": {"type": "object", "properties": {
                "field": {"type": "string", "enum": fields or ["none"]},
                "value": {"type": "string"}, "evidence": {"type": "string"}},
                "required": ["field", "value", "evidence"], "additionalProperties": False}},
        },
        "required": ["categories", "document_type", "document_type_confidence", "entities"],
        "additionalProperties": False,
    }
    prompt = "Extract only supported values. Evidence must be an exact substring. JSON schema: " + json.dumps(schema)
    payload = {"model": config.enrichment.model, "stream": False, "format": schema,
               "options": {"temperature": 0},
               "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": text}]}
    own = client is None
    client = client or httpx.Client(timeout=config.enrichment.timeout_seconds)
    try:
        response = client.post((config.enrichment.endpoint or "http://localhost:11434").rstrip("/") + "/api/chat", json=payload)
        response.raise_for_status()
        outer = response.json()
        raw = json.loads(outer["message"]["content"])
    finally:
        if own:
            client.close()
    if not isinstance(raw, dict) or not isinstance(raw.get("entities"), list):
        raise ValueError("Ollama returned an invalid structured response")
    entities = tuple(entity for entity in raw["entities"]
                     if isinstance(entity, dict) and entity.get("field") in fields
                     and isinstance(entity.get("evidence"), str) and entity["evidence"] in text)
    categories = {name: float(score) for name, score in raw.get("categories", {}).items()
                  if name in config.categories and float(score) >= config.categories[name]}
    confidence = raw.get("document_type_confidence")
    document_type = raw.get("document_type")
    if document_type not in config.document_types or confidence is None or float(confidence) < config.document_type_confidence:
        document_type = None
    return EnrichmentResult(
        provider="ollama", model=str(outer.get("model", config.enrichment.model)),
        categories=categories, document_type=document_type,
        document_type_confidence=float(confidence) if confidence is not None else None,
        entities=entities, input_hash=input_hash(text), config_hash=config.fingerprint(),
        duration_ms=int((time.monotonic() - start) * 1000), raw=raw,
    )


def enrich_jev(config: Config, text: str, client_factory: Callable[..., Any] | None = None) -> EnrichmentResult:
    from typesafe_sdk import Choice, Noul, TypeSafeClient

    redacted, _ = redact(text)
    questions: dict[str, Any] = {
        f"category_{index}": Noul(instructions=f"Does this email belong to category {name!r}?")
        for index, name in enumerate(config.categories)
    }
    type_key = "document_type"
    if config.document_types:
        questions[type_key] = Choice(
            instructions="What type of document is this?",
            criteria={name: None for name in (*config.document_types, "other")},
        )
    candidates = candidate_values(redacted)
    extraction_questions: dict[str, tuple[str, list[str]]] = {}
    for index, field in enumerate(config.extraction_fields):
        values = candidates.get(field, candidates.get("date", []) if "date" in field else [])
        if not values:
            continue
        key_name = f"extract_{index}"
        extraction_questions[key_name] = (field, values)
        questions[key_name] = Choice(
            instructions=f"Which locally detected candidate is the supported value for {field!r}?",
            criteria={**{value: None for value in values}, "unsupported": None},
        )
    if not questions:
        return EnrichmentResult(provider="jev", model=config.enrichment.model,
                                input_hash=input_hash(redacted), config_hash=config.fingerprint())
    factory = client_factory or TypeSafeClient
    key = config.enrichment_key()
    previous = os.environ.get("TYPESAFE_API_KEY")
    if key:
        os.environ["TYPESAFE_API_KEY"] = key
    start = time.monotonic()
    try:
        kwargs: dict[str, Any] = {"timeout": config.enrichment.timeout_seconds}
        if key:
            kwargs["api_key"] = key
        if config.enrichment.endpoint:
            kwargs["base_url"] = config.enrichment.endpoint
        with factory(**kwargs) as client:
            response = client.system_one(state={"email": redacted}, questions=questions,
                                         model=config.enrichment.model)
    finally:
        if key and previous is None:
            os.environ.pop("TYPESAFE_API_KEY", None)
        elif key:
            os.environ["TYPESAFE_API_KEY"] = previous
    answers = getattr(response, "answers", getattr(response, "choices", {}))
    categories: dict[str, float] = {}
    for index, (name, threshold) in enumerate(config.categories.items()):
        answer = answers[f"category_{index}"]
        score = float(getattr(answer, "noul", getattr(answer, "probability", 0.0)))
        if score >= threshold:
            categories[name] = score
    document_type = None
    confidence = None
    if type_key in answers:
        answer = answers[type_key]
        choice = getattr(answer, "choice", None)
        confidence = getattr(answer, "confidence", getattr(answer, "probability", None))
        if choice in config.document_types and (confidence is None or float(confidence) >= config.document_type_confidence):
            document_type = choice
    entities: list[dict[str, str]] = []
    for key_name, (field, values) in extraction_questions.items():
        choice = getattr(answers[key_name], "choice", None)
        if choice in values:
            entities.append({"field": field, "value": choice, "evidence": choice})
    return EnrichmentResult(
        provider="jev", model=str(getattr(response, "model", config.enrichment.model)),
        categories=categories, document_type=document_type,
        document_type_confidence=float(confidence) if confidence is not None else None,
        entities=tuple(entities),
        request_id=getattr(response, "request_id", None), input_hash=input_hash(redacted),
        config_hash=config.fingerprint(), duration_ms=int((time.monotonic() - start) * 1000),
    )


def enrich(config: Config, text: str) -> EnrichmentResult:
    if config.enrichment.provider == "none":
        return no_enrichment(config, text)
    if config.enrichment.provider == "ollama":
        return enrich_ollama(config, text)
    return enrich_jev(config, text)
