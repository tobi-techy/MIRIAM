"""Endpoint tests for POST /api/v1/shop/documents/read.

The vision provider and the Jev judge are faked, so the whole HTTP path runs
with no network. The store is swapped for the in-memory one.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from shop_fakes import FakeJudge, FakeProvider, mama_tobi_book, provider_factory
from test_shop_reader import png_bytes

from miriam_agent.api import dependencies
from miriam_agent.api import shop as shop_api
from miriam_agent.api.main import app
from miriam_agent.database.models import User
from miriam_agent.shop.store import InMemoryShopDocumentStore

USER_ID = "user-1"
PATH = "/api/v1/shop/documents/read"
PNG_B64 = base64.b64encode(png_bytes((640, 480))).decode()


def _wire(monkeypatch, *, judge: Any, provider: Any, store: Any = None) -> None:
    user = User()
    user.id = USER_ID
    user.username = "tester"
    user.roles = ["verified"]
    overrides = {
        **app.dependency_overrides,
        dependencies.get_current_user: lambda: user,
        dependencies.get_bearer_token: lambda: "test-token",
    }
    monkeypatch.setattr(app, "dependency_overrides", overrides)
    shop_api.set_store(store or InMemoryShopDocumentStore())
    shop_api.set_reader_overrides(judge=judge, provider_factory=provider)


@pytest.fixture(autouse=True)
def _reset_overrides():
    yield
    shop_api.set_store(None)
    shop_api.set_reader_overrides(clear=True)


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _post(client: TestClient, **body: Any):
    payload = {"file_b64": PNG_B64, "mime": "image/png", "user_id": USER_ID}
    payload.update(body)
    return client.post(PATH, json=payload, headers=_headers())


def _stock_provider() -> FakeProvider:
    return FakeProvider([json.loads(mama_tobi_book().model_dump_json())])


def test_route_is_registered_under_api_v1() -> None:
    # Routers are included lazily, so read the served schema rather than the
    # router list.
    assert PATH in app.openapi()["paths"]


def test_read_returns_the_checked_reply(monkeypatch) -> None:
    _wire(
        monkeypatch,
        judge=FakeJudge(doc_kind="stock_book", difficulty="easy"),
        provider=provider_factory(_stock_provider()),
    )
    response = _post(_client(), message_id="msg-1")
    assert response.status_code == 200
    body = response.json()
    assert body["doc_type"] == "stock_book"
    assert body["doc_id"]
    assert "Total from your prices: ₦286,300" in body["reply_text"]
    assert "Gap ₦31,900" in body["reply_text"]
    assert body["confidence"] == pytest.approx(0.95)


def test_user_id_that_does_not_match_the_subject_is_rejected(monkeypatch) -> None:
    provider = _stock_provider()
    _wire(
        monkeypatch,
        judge=FakeJudge(doc_kind="stock_book"),
        provider=provider_factory(provider),
    )
    response = _post(_client(), user_id="someone-else")
    assert response.status_code == 403
    assert response.json()["detail"] == (
        "user_id does not match the authenticated subject"
    )
    # Nothing was read for the other user's request.
    assert provider.extract_calls == 0


def test_missing_file_is_unprocessable(monkeypatch) -> None:
    _wire(monkeypatch, judge=FakeJudge(), provider=provider_factory(FakeProvider([])))
    response = _post(_client(), file_b64="")
    assert response.status_code == 422


def test_oversized_payload_is_refused_before_reading(monkeypatch) -> None:
    provider = _stock_provider()
    _wire(
        monkeypatch,
        judge=FakeJudge(doc_kind="stock_book"),
        provider=provider_factory(provider),
    )
    response = _post(_client(), file_b64="A" * (28 * 1024 * 1024))
    assert response.status_code == 413
    assert provider.extract_calls == 0


def test_second_call_of_the_same_file_reuses_the_stored_reply(monkeypatch) -> None:
    provider = _stock_provider()
    judge = FakeJudge(doc_kind="stock_book", difficulty="easy")
    _wire(monkeypatch, judge=judge, provider=provider_factory(provider))
    client = _client()
    first = _post(client, message_id="msg-1").json()
    second = _post(client, message_id="msg-2").json()
    assert second["doc_id"] == first["doc_id"]
    assert second["reply_text"] == first["reply_text"]
    assert provider.extract_calls == 1


def test_an_unreadable_page_returns_the_retake_message_not_an_error(
    monkeypatch,
) -> None:
    _wire(
        monkeypatch,
        judge=FakeJudge(doc_kind="stock_book"),
        provider=provider_factory(FakeProvider(fail=True)),
    )
    response = _post(_client())
    assert response.status_code == 200
    body = response.json()
    assert "clearer photo" in body["reply_text"]
    assert body["flags"] == ["failed"]
    assert body["doc_id"] == ""


def test_a_receipt_returns_an_empty_reply_so_go_keeps_its_own_path(
    monkeypatch,
) -> None:
    provider = _stock_provider()
    _wire(
        monkeypatch,
        judge=FakeJudge(doc_kind="receipt"),
        provider=provider_factory(provider),
    )
    response = _post(_client())
    assert response.status_code == 200
    body = response.json()
    assert body["reply_text"] == ""
    assert body["doc_type"] == "receipt"
    assert provider.extract_calls == 0


def test_a_bank_statement_page_is_left_to_the_statement_path(monkeypatch) -> None:
    provider = _stock_provider()
    _wire(
        monkeypatch,
        judge=FakeJudge(doc_kind="bank_statement"),
        provider=provider_factory(provider),
    )
    body = _post(_client()).json()
    assert body["reply_text"] == ""
    assert body["doc_type"] == "bank_statement"
    assert body["flags"] == ["not a shop document (bank_statement)"]


def test_storage_failure_payload_has_no_doc_id(monkeypatch) -> None:
    _wire(
        monkeypatch,
        judge=FakeJudge(doc_kind="other"),
        provider=provider_factory(_stock_provider()),
    )
    body = _post(_client()).json()
    assert body["doc_id"] == ""
    assert body["doc_type"] == "other"
