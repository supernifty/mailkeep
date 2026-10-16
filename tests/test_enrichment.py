from __future__ import annotations

import json

import httpx

from mailkeep.enrichment import enrich_jev, enrich_ollama, list_jev_models, redact


def test_redaction_is_typed_stable_and_removes_sensitive_url_query():
    text = "Mail me at a@example.com or a@example.com, call +61 412 345 678; https://x.test/a?token=secretvalue123"
    redacted, _ = redact(text)
    assert redacted.count("<EMAIL_1>") == 2
    assert "412 345" not in redacted
    assert "secretvalue123" not in redacted


def test_ollama_schema_and_evidence_validation(config_factory):
    config = config_factory(enrichment="ollama")
    observed = {}

    def handler(request: httpx.Request):
        observed.update(json.loads(request.content))
        content = {"categories": {"receipt": 0.9}, "document_type": "receipt",
                   "document_type_confidence": 0.95,
                   "entities": [{"field": "amount", "value": "$12.00", "evidence": "$12.00"},
                                {"field": "due_date", "value": "never", "evidence": "invented"}]}
        return httpx.Response(200, json={"model": "exact-build", "message": {"content": json.dumps(content)}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = enrich_ollama(config, "Total is $12.00", client)
    assert observed["options"]["temperature"] == 0
    assert isinstance(observed["format"], dict)
    assert result.model == "exact-build"
    assert len(result.entities) == 1


def test_ollama_rejects_invalid_structure(config_factory):
    config = config_factory(enrichment="ollama")
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"message": {"content": "[]"}}))) as client:
        try:
            enrich_ollama(config, "body", client)
        except ValueError as exc:
            assert "invalid structured" in str(exc)
        else:
            raise AssertionError("invalid result was accepted")


def test_jev_uses_noul_thresholds_and_only_local_extraction_candidates(config_factory):
    config = config_factory(enrichment="ollama").model_copy(update={
        "enrichment": config_factory(enrichment="ollama").enrichment.model_copy(
            update={"provider": "jev", "privacy": "redacted", "api_key_env": "JEV_KEY"})
    })

    class Answer:
        def __init__(self, *, noul=None, choice=None, confidence=None):
            self.noul, self.choice, self.confidence = noul, choice, confidence

    class Response:
        model = "jev-build-123"
        request_id = "request-1"
        answers = {"category_0": Answer(noul=0.8),
                   "document_type": Answer(choice="receipt", confidence=0.9),
                   "extract_0": Answer(choice="$12.00", confidence=0.9)}

    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def system_one(self, **kwargs):
            assert "extract_0" in kwargs["questions"]
            return Response()

    result = enrich_jev(config, "Email a@example.com. Total $12.00", Client)
    assert result.categories == {"receipt": 0.8}
    assert result.document_type == "receipt"
    assert result.entities == ({"field": "amount", "value": "$12.00", "evidence": "$12.00"},)


def test_jev_model_listing_uses_configured_client(config_factory):
    config = config_factory(enrichment="ollama").model_copy(update={
        "enrichment": config_factory(enrichment="ollama").enrichment.model_copy(
            update={"provider": "jev", "model": "jev-latest", "privacy": "redacted",
                    "api_key_env": "JEV_KEY"})
    })

    class Model:
        def __init__(self, name): self.name = name

    class Models:
        def list(self): return type("Response", (), {"models": [Model("jev-latest"), Model("jev-preview")]})()

    class Client:
        def __init__(self, **kwargs): self.models = Models()
        def __enter__(self): return self
        def __exit__(self, *args): pass

    assert list_jev_models(config, Client) == ["jev-latest", "jev-preview"]
