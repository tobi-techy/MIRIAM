"""Stage 2 contract tests: Pydantic v1 envelope, versioning, money safety."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

import pytest

from miriam_agent.core.exceptions import ValidationError
from miriam_agent.documents.schemas import DocumentResult


def _envelope(**over):
    base = {
        "schema_version": "1.0",
        "document_id": "doc-1",
        "document_type": "receipt",
        "status": "completed",
        "confidence": 0.9,
        "data": {"merchant": "Shop", "amount": "1500.50", "currency": "NGN"},
        "validation": {"status": "valid", "reconciled": True, "difference": "0.00"},
        "evidence": [],
    }
    base.update(over)
    return base


def test_valid_contract_accepted():
    r = DocumentResult.model_validate(_envelope())
    assert r.schema_version == "1.0"
    assert r.data.amount == "1500.50"


def test_missing_required_field_rejected():
    env = _envelope()
    del env["document_id"]
    with pytest.raises(Exception):
        DocumentResult.model_validate(env)


def test_unknown_major_version_rejected():
    with pytest.raises(Exception):
        DocumentResult.model_validate(_envelope(schema_version="2.0"))


def test_minor_version_accepted():
    r = DocumentResult.model_validate(_envelope(schema_version="1.1"))
    assert r.schema_version == "1.1"


def test_invalid_money_rejected():
    with pytest.raises(Exception):
        DocumentResult.model_validate(
            _envelope(data={"merchant": "X", "amount": "not-money"})
        )


def test_float_money_coerced_to_decimal_string():
    r = DocumentResult.model_validate(_envelope(data={"amount": 1500.5}))
    assert r.data.amount == "1500.5"


def test_confidence_out_of_range_rejected():
    with pytest.raises(Exception):
        DocumentResult.model_validate(_envelope(confidence=1.5))


def test_error_mapping_and_client_paths():
    import asyncio

    import httpx

    from miriam_agent.core.exceptions import (
        AuthorizationError,
        IntegrationError,
        RateLimitError,
    )
    from miriam_agent.documents import errors as doc_errors
    from miriam_agent.documents.client import fetch_document_result
    from miriam_agent.integrations.go_client import GoBackendClient

    assert isinstance(doc_errors.map_http_status(404, "x"), AuthorizationError)
    assert isinstance(doc_errors.map_http_status(429, "x"), RateLimitError)
    assert isinstance(doc_errors.map_http_status(500, "x"), IntegrationError)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/documents/doc-1/result"
        assert request.headers["Authorization"] == "Bearer tok"
        assert request.headers["X-Requested-With"] == "RailApp"
        return httpx.Response(200, json={"data": _envelope()})

    client = GoBackendClient("http://go.test")
    asyncio.get_event_loop().run_until_complete(client._client.aclose())
    client._client = httpx.AsyncClient(
        base_url="http://go.test",
        transport=httpx.MockTransport(handler),
        headers={
            "Content-Type": "application/json",
            "X-Requested-With": "RailApp",
        },
    )
    result = asyncio.get_event_loop().run_until_complete(
        client.get_document_result("tok", "doc-1")
    )
    assert result.document_id == "doc-1"
    assert result.validation is not None and result.validation.status == "valid"

    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "document not found"})

    client._client = httpx.AsyncClient(
        base_url="http://go.test",
        transport=httpx.MockTransport(forbidden),
        headers={"Content-Type": "application/json", "X-Requested-With": "RailApp"},
    )
    with pytest.raises(AuthorizationError):
        asyncio.get_event_loop().run_until_complete(
            client.get_document_result("tok", "other-doc")
        )

    def bad_version(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": _envelope(schema_version="9.9")})

    client._client = httpx.AsyncClient(
        base_url="http://go.test",
        transport=httpx.MockTransport(bad_version),
        headers={"Content-Type": "application/json", "X-Requested-With": "RailApp"},
    )
    with pytest.raises(ValidationError):
        asyncio.get_event_loop().run_until_complete(
            fetch_document_result(client._client, "tok", "doc-1")
        )
    asyncio.get_event_loop().run_until_complete(client.close())


def test_tool_is_read_only_and_registered():
    from miriam_agent.tools import build_tool_registry

    registry = build_tool_registry()
    assert "get_document_result" in registry
    tool = registry._tools["get_document_result"]
    assert tool.is_mutation is False
    assert tool.requires_approval is False
