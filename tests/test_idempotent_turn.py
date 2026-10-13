"""One provider message is one turn: across a restart and across replicas.

The record lives in Redis, so these tests give two ``TurnStore`` instances (two
replicas, or a process before and after a restart) one shared fake Redis and
count how many times a turn really ran.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from test_money_layers_route_live import USER_ID, _ledger, _wire, await_sync

from miriam_agent.api import chat, idempotency
from miriam_agent.api.idempotency import TurnStore, idempotent_post
from miriam_agent.api.main import app
from miriam_agent.turn_routing import SourcedRequest


class FakeRedis:
    """The three commands ``TurnStore`` uses, with Redis's NX and TTL meaning."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}
        self.broken = False

    async def set(self, name: str, value: str, *, nx: bool = False, ex=None) -> Any:
        if self.broken:
            raise ConnectionError("redis down")
        if nx and name in self.data:
            return None
        self.data[name] = value
        self.ttls[name] = ex
        return True

    async def get(self, name: str) -> Any:
        if self.broken:
            raise ConnectionError("redis down")
        return self.data.get(name)

    async def delete(self, *names: str) -> Any:
        if self.broken:
            raise ConnectionError("redis down")
        for name in names:
            self.data.pop(name, None)
            self.ttls.pop(name, None)


def _use(monkeypatch, store: TurnStore | None) -> None:
    monkeypatch.setattr(idempotency, "get_turn_store", lambda: store)


def _count_turns(monkeypatch) -> list[str]:
    ran: list[str] = []
    real = chat._prepare_turn

    async def counting(**kwargs: Any) -> Any:
        ran.append(kwargs["body"].message)
        return await real(**kwargs)

    monkeypatch.setattr(chat, "_prepare_turn", counting)
    return ran


def _post(client: TestClient, message_id: str | None, text: str = "send 5k to Ada"):
    body: dict[str, Any] = {"message": text}
    if message_id is not None:
        body["message_id"] = message_id
    response = client.post(
        "/api/v1/chat", headers={"Authorization": "Bearer test-token"}, json=body
    )
    assert response.status_code == 200, response.text
    return response.json()


def _challenges(store: Any) -> dict[str, Any]:
    ledger = await_sync(store.load(USER_ID))
    return dict(getattr(ledger, "challenges", {}) or {})


def test_a_replayed_message_is_one_turn_and_one_challenge(monkeypatch) -> None:
    ledger_store, rail, _memory = _wire(monkeypatch, ledger=_ledger(spendable="90000"))
    redis = FakeRedis()
    _use(monkeypatch, TurnStore(redis))
    ran = _count_turns(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)

    first = _post(client, "whatsapp:wamid.A1")
    again = _post(client, "whatsapp:wamid.A1")

    assert len(ran) == 1, "the model/orchestrator ran twice for one message id"
    assert again["confirm_id"] == first["confirm_id"]
    assert again["replayed"] is True
    assert len(_challenges(ledger_store)) == 1
    assert rail.moves == []
    key = f"miriam:turn:{USER_ID}:whatsapp:wamid.A1"
    assert redis.ttls[key] == idempotency.DONE_TTL_SECONDS


def test_the_same_message_after_a_restart_is_still_one_turn(monkeypatch) -> None:
    ledger_store, _rail, _memory = _wire(monkeypatch, ledger=_ledger(spendable="90000"))
    redis = FakeRedis()
    ran = _count_turns(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)

    _use(monkeypatch, TurnStore(redis))
    first = _post(client, "whatsapp:wamid.B1")

    _use(monkeypatch, TurnStore(redis))  # a new process: nothing in memory
    again = _post(client, "whatsapp:wamid.B1")

    assert len(ran) == 1
    assert again["confirm_id"] == first["confirm_id"]
    assert len(_challenges(ledger_store)) == 1


def test_the_same_message_on_two_replicas_is_one_turn(monkeypatch) -> None:
    ledger_store, _rail, _memory = _wire(monkeypatch, ledger=_ledger(spendable="90000"))
    redis = FakeRedis()
    replica_a, replica_b = TurnStore(redis), TurnStore(redis)
    ran = _count_turns(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)

    _use(monkeypatch, replica_a)
    _post(client, "whatsapp:wamid.C1")
    _use(monkeypatch, replica_b)
    _post(client, "whatsapp:wamid.C1")
    _use(monkeypatch, replica_a)
    _post(client, "whatsapp:wamid.C1")

    assert len(ran) == 1
    assert len(_challenges(ledger_store)) == 1


def _slow_turn_app(runs: list[int]) -> FastAPI:
    router = APIRouter()

    @idempotent_post(router, "/turn")
    async def turn(body: SourcedRequest, user: Any = Depends(lambda: _User())) -> Any:
        runs.append(1)
        return {"response": "fresh"}

    mini = FastAPI()
    mini.include_router(router)
    return mini


async def _post_async(mini: FastAPI, message_id: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=mini)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.post("/turn", json={"message_id": message_id})


def test_a_replay_waits_for_the_running_turn_and_returns_its_reply(
    monkeypatch,
) -> None:
    """The first caller hung up (timeout, then retry): the retry delivers it."""
    redis = FakeRedis()
    key = "miriam:turn:u-1:wamid.D1"
    redis.data[key] = "processing"
    _use(monkeypatch, TurnStore(redis))
    runs: list[int] = []
    mini = _slow_turn_app(runs)

    async def scenario() -> httpx.Response:
        waiting = asyncio.create_task(_post_async(mini, "wamid.D1"))
        await asyncio.sleep(0.4)
        assert not waiting.done(), "the replay answered while the first still ran"
        redis.data[key] = '{"response": "first reply"}'
        return await waiting

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.json() == {"response": "first reply", "replayed": True}
    assert runs == [], "the model ran for a replay"


def test_a_replay_runs_the_turn_when_the_first_one_gives_up(monkeypatch) -> None:
    redis = FakeRedis()
    key = "miriam:turn:u-1:wamid.D2"
    redis.data[key] = "processing"
    _use(monkeypatch, TurnStore(redis))
    runs: list[int] = []
    mini = _slow_turn_app(runs)

    async def scenario() -> httpx.Response:
        waiting = asyncio.create_task(_post_async(mini, "wamid.D2"))
        await asyncio.sleep(0.4)
        redis.data.pop(key)  # the first delivery failed and released its claim
        return await waiting

    response = asyncio.run(scenario())

    assert response.json() == {"response": "fresh"}
    assert runs == [1]


def test_a_replay_runs_anyway_after_the_wait_budget(monkeypatch) -> None:
    redis = FakeRedis()
    redis.data["miriam:turn:u-1:wamid.D3"] = "processing"
    _use(monkeypatch, TurnStore(redis))
    monkeypatch.setattr(idempotency, "IN_FLIGHT_WAIT_SECONDS", 0.3)
    runs: list[int] = []
    mini = _slow_turn_app(runs)

    response = asyncio.run(_post_async(mini, "wamid.D3"))

    assert response.json() == {"response": "fresh"}
    assert runs == [1]


def test_two_simultaneous_deliveries_run_one_turn() -> None:
    """Both replicas receive the message at once: exactly one turn runs."""
    redis = FakeRedis()
    runs: list[int] = []
    router = APIRouter()

    @idempotent_post(router, "/turn")
    async def turn(body: SourcedRequest, user: Any = Depends(lambda: _User())) -> Any:
        runs.append(1)
        await asyncio.sleep(0.05)
        return {"response": "hello"}

    mini = FastAPI()
    mini.include_router(router)
    stores = [TurnStore(redis), TurnStore(redis)]
    picker = iter(stores)
    original = idempotency.get_turn_store
    idempotency.get_turn_store = lambda: next(picker)  # type: ignore[assignment]
    try:

        async def both() -> list[Any]:
            transport = httpx.ASGITransport(app=mini)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://t"
            ) as client:
                return await asyncio.gather(
                    client.post("/turn", json={"message_id": "wamid.E1"}),
                    client.post("/turn", json={"message_id": "wamid.E1"}),
                )

        responses = asyncio.run(both())
    finally:
        idempotency.get_turn_store = original

    assert all(r.status_code == 200 for r in responses)
    assert len(runs) == 1
    assert sorted(bool(r.json().get("replayed")) for r in responses) == [False, True]


class _User:
    id = "u-1"


def test_a_failed_turn_releases_its_claim_so_the_retry_runs(monkeypatch) -> None:
    redis = FakeRedis()
    _use(monkeypatch, TurnStore(redis))
    router = APIRouter()
    attempts: list[int] = []

    @idempotent_post(router, "/turn")
    async def turn(body: SourcedRequest, user: Any = Depends(lambda: _User())) -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("model blew up")
        return {"response": "ok"}

    mini = FastAPI()
    mini.include_router(router)
    client = TestClient(mini, raise_server_exceptions=False)

    assert client.post("/turn", json={"message_id": "wamid.F1"}).status_code == 500
    retry = client.post("/turn", json={"message_id": "wamid.F1"})

    assert retry.status_code == 200 and retry.json() == {"response": "ok"}
    assert len(attempts) == 2


def test_redis_down_fails_open(monkeypatch) -> None:
    ledger_store, _rail, _memory = _wire(monkeypatch, ledger=_ledger(spendable="90000"))
    redis = FakeRedis()
    redis.broken = True
    _use(monkeypatch, TurnStore(redis))
    ran = _count_turns(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)

    out = _post(client, "whatsapp:wamid.G1")

    assert out["confirm_id"]
    assert len(ran) == 1
    assert len(_challenges(ledger_store)) == 1


def test_no_message_id_means_no_dedupe(monkeypatch) -> None:
    _wire(monkeypatch, ledger=_ledger(spendable="90000"))
    redis = FakeRedis()
    _use(monkeypatch, TurnStore(redis))
    ran = _count_turns(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)

    _post(client, None, "hello")
    _post(client, None, "hello")

    assert len(ran) == 2
    assert redis.data == {}


def test_the_same_id_for_two_users_is_two_turns(monkeypatch) -> None:
    redis = FakeRedis()
    _use(monkeypatch, TurnStore(redis))
    router = APIRouter()
    ran: list[str] = []
    current = {"id": "a"}

    class _Who:
        @property
        def id(self) -> str:
            return current["id"]

    @idempotent_post(router, "/turn")
    async def turn(body: SourcedRequest, user: Any = Depends(lambda: _Who())) -> Any:
        ran.append(user.id)
        return {"response": user.id}

    mini = FastAPI()
    mini.include_router(router)
    client = TestClient(mini)

    assert client.post("/turn", json={"message_id": "wamid.H1"}).json() == {
        "response": "a"
    }
    current["id"] = "b"
    assert client.post("/turn", json={"message_id": "wamid.H1"}).json() == {
        "response": "b"
    }
    assert ran == ["a", "b"]


@pytest.mark.parametrize("bad", ["", "   "])
def test_a_blank_message_id_is_not_a_key(bad: str, monkeypatch) -> None:
    redis = FakeRedis()
    _use(monkeypatch, TurnStore(redis))
    router = APIRouter()
    ran: list[int] = []

    @idempotent_post(router, "/turn")
    async def turn(body: SourcedRequest, user: Any = Depends(lambda: _User())) -> Any:
        ran.append(1)
        return {"response": "ok"}

    mini = FastAPI()
    mini.include_router(router)
    client = TestClient(mini)
    client.post("/turn", json={"message_id": bad})
    client.post("/turn", json={"message_id": bad})
    assert len(ran) == 2 and redis.data == {}
