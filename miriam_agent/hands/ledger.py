"""Layer 1 - HANDS. The ledger: balances by sleeve.

Deterministic code only. No LLM, no JEV.

The ledger is the source of truth for what the user has. It is organised into
four sleeves -- ``spendable``, ``savings``, ``yield``, ``locked`` -- and every
mutation goes through :meth:`Ledger.credit`, :meth:`Ledger.debit` or
:meth:`Ledger.move_internal`, which are the only three functions in the process
that write a balance. Callers pass an idempotency key; a key that has already
been processed returns the original receipt instead of moving money twice.

Persistence is behind :class:`LedgerStore`. The in-process store is the default
and is what the tests and the CLI use. The Redis store is what makes a
confirmation survive the request that created it, and it detects a lost update
with the ledger's ``version`` counter rather than pretending to be atomic.
"""

from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from miriam_agent.hands.audit import Receipt

logger = logging.getLogger(__name__)

Sleeve = Literal["spendable", "savings", "yield", "locked"]
SLEEVES: tuple[str, ...] = ("spendable", "savings", "yield", "locked")

MovementKind = Literal["inflow", "outflow", "internal", "reserve", "unlock"]

CENTS = Decimal("0.01")

# How far back the velocity window looks.
WINDOW_DAYS = 30


class LedgerError(Exception):
    """A ledger operation that the ledger itself refused."""


class LedgerConflictError(LedgerError):
    """Another writer changed the ledger since it was loaded."""


class LedgerUnavailable(LedgerError):
    """The ledger could not be reached, so nothing was read and nothing moved.

    Raised instead of guessing. A money store that invents an empty balance when
    its backing store is down is worse than one that says it does not know: the
    user would hear "you have nothing" and the ledger would disagree.
    """


def money(value: Any) -> Decimal:
    """Coerce anything numeric to a two-place Decimal.

    Money is Decimal everywhere in this package and converted only at the
    boundary, so a float never gets to round a balance.
    """
    if isinstance(value, Decimal):
        return value.quantize(CENTS, rounding=ROUND_HALF_UP)
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


def _now() -> datetime:
    return datetime.now(UTC)


class Track(BaseModel):
    """The income split ratios, e.g. the 70/30 book.

    ``yield`` is the share routed to a yield partner. Ratios are percentages of
    the inflow and must sum to exactly 100, so a split always reconciles.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = "70/30"
    spend: Decimal = Decimal("70")
    save: Decimal = Decimal("30")
    yield_: Decimal = Decimal("0")

    @model_validator(mode="after")
    def _sums_to_100(self) -> Track:
        total = self.spend + self.save + self.yield_
        if total != Decimal("100"):
            raise ValueError(f"track ratios must sum to 100, got {total}")
        return self

    def ratios(self) -> dict[str, str]:
        return {
            "spend": str(self.spend),
            "save": str(self.save),
            "yield": str(self.yield_),
        }


class Bill(BaseModel):
    """A bill that is coming, with how long until it lands."""

    model_config = ConfigDict(extra="forbid")

    name: str
    amount: Decimal
    due_in_days: int


class RentFirst(BaseModel):
    """Rent protected before anything else.

    ``reserved`` is money already moved out of ``spendable`` into ``locked``.
    It stays visible here as well so STATE can say what the reserve is for.
    """

    model_config = ConfigDict(extra="forbid")

    required: Decimal = Decimal("0")
    reserved: Decimal = Decimal("0")
    due_in_days: int | None = None

    @property
    def gap(self) -> Decimal:
        """Rent still owed that has not been reserved yet."""
        return max(Decimal("0"), self.required - self.reserved)


class Movement(BaseModel):
    """One line of history. Amounts are always positive; direction is the kind."""

    model_config = ConfigDict(extra="forbid")

    kind: MovementKind
    amount: Decimal
    sleeve: str
    to_sleeve: str | None = None
    counterparty: str = ""
    category: str = ""
    ref: str = ""
    at: datetime


class Last30d(BaseModel):
    """Velocity over the window: what came in, what left, and where it leaked."""

    model_config = ConfigDict(extra="forbid")

    inflow: Decimal = Decimal("0")
    spend: Decimal = Decimal("0")
    leak_by_category: dict[str, str] = Field(default_factory=dict)


class Challenge(BaseModel):
    """A pending confirmation for one exact action.

    The user taps CONFIRM with this id. The id -- never a chat word -- is what
    makes a confirmation real, and it is bound to the amount, counterparty,
    sleeve and user below so a confirmed challenge cannot be replayed for a
    different action. Voice may only print these stored fields; it never invents
    a figure to narrate.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str = ""
    action: str
    amount: Decimal
    counterparty: str = ""
    destination: str = ""
    sleeve: str = "spendable"
    decision_id: str = ""
    reasons: list[str] = Field(default_factory=list)
    created_at: datetime
    expires_at: datetime
    status: Literal["pending", "consumed", "expired", "declined"] = "pending"

    def is_open(self, at: datetime) -> bool:
        return self.status == "pending" and at < self.expires_at


class PendingInflow(BaseModel):
    """An inflow that has been seen and not yet classified or split."""

    model_config = ConfigDict(extra="forbid")

    id: str
    amount: Decimal
    source_raw: str = ""
    classified_as: str | None = None


class Ledger(BaseModel):
    """One user's balances, policy state and history."""

    model_config = ConfigDict(extra="forbid")

    user_id: str
    currency: str = "NGN"
    sleeves: dict[str, Decimal] = Field(
        default_factory=lambda: {name: Decimal("0") for name in SLEEVES}
    )
    track: Track = Field(default_factory=Track)
    bills_upcoming: list[Bill] = Field(default_factory=list)
    rent_first: RentFirst = Field(default_factory=RentFirst)
    movements: list[Movement] = Field(default_factory=list)
    receipts: list[Receipt] = Field(default_factory=list)
    challenges: dict[str, Challenge] = Field(default_factory=dict)
    pending_inflow: PendingInflow | None = None
    # idempotency key -> receipt id. The presence of a key is the whole
    # idempotency mechanism: a repeated inflow or transfer finds its key here
    # and replays the receipt instead of moving money again.
    processed: dict[str, str] = Field(default_factory=dict)
    version: int = 0

    def balance(self, sleeve: str) -> Decimal:
        return self.sleeves.get(sleeve, Decimal("0"))

    def credit(self, sleeve: str, amount: Decimal) -> None:
        """Add money to a sleeve. Only an inflow or an internal move calls this."""
        self._require_known(sleeve)
        self.sleeves[sleeve] = self.balance(sleeve) + money(amount)

    def debit(self, sleeve: str, amount: Decimal) -> None:
        """Remove money from a sleeve, refusing to go negative."""
        self._require_known(sleeve)
        amount = money(amount)
        if amount > self.balance(sleeve):
            raise LedgerError(
                f"{sleeve} holds {self.balance(sleeve)}, cannot debit {amount}"
            )
        self.sleeves[sleeve] = self.balance(sleeve) - amount

    def move_internal(self, from_sleeve: str, to_sleeve: str, amount: Decimal) -> None:
        """Move money between the user's own sleeves. Never leaves the user."""
        self.debit(from_sleeve, amount)
        self.credit(to_sleeve, amount)

    def record_movement(self, movement: Movement) -> None:
        self.movements.append(movement)

    def remember_receipt(self, receipt: Receipt) -> None:
        """Keep the receipt, and remember its idempotency key if it has one.

        The journal is deliberately not trimmed. An idempotency key only guards
        anything while the receipt proving it was used is still here, so dropping
        old receipts would quietly turn a replay into a second movement. The
        durable audit rows live in the audit store; this is the ledger's own
        record of what it did, and STATE carries only the most recent few.
        """
        self.receipts.append(receipt)
        if receipt.idempotency_key:
            self.processed.setdefault(receipt.idempotency_key, receipt.id)

    def receipt_for(self, idempotency_key: str) -> Receipt | None:
        receipt_id = self.processed.get(idempotency_key)
        if receipt_id is None:
            return None
        for receipt in self.receipts:
            if receipt.id == receipt_id:
                return receipt
        return None

    def window(self, *, days: int = WINDOW_DAYS, at: datetime | None = None) -> Last30d:
        """Inflow, spend and leak-by-category over the velocity window."""
        now = at or _now()
        cutoff = now - timedelta(days=days)
        inflow = Decimal("0")
        spend = Decimal("0")
        leaks: dict[str, Decimal] = {}
        for movement in self.movements:
            if movement.at < cutoff:
                continue
            if movement.kind == "inflow":
                inflow += movement.amount
            elif movement.kind == "outflow":
                spend += movement.amount
                if movement.category:
                    leaks[movement.category] = (
                        leaks.get(movement.category, Decimal("0")) + movement.amount
                    )
        return Last30d(
            inflow=money(inflow),
            spend=money(spend),
            leak_by_category={
                name: str(money(value)) for name, value in sorted(leaks.items())
            },
        )

    def touch(self) -> None:
        """Bump the version. Called once per committed mutation."""
        self.version += 1

    @staticmethod
    def _require_known(sleeve: str) -> None:
        if sleeve not in SLEEVES:
            raise LedgerError(f"unknown sleeve {sleeve!r}; known: {', '.join(SLEEVES)}")


def new_ledger(user_id: str, *, currency: str = "NGN") -> Ledger:
    """An empty ledger for a user who has none yet."""
    return Ledger(user_id=user_id, currency=currency)


class LedgerStore(Protocol):
    """Where a ledger lives between calls."""

    async def load(self, user_id: str) -> Ledger | None: ...

    async def save(self, ledger: Ledger) -> None: ...


class InMemoryLedgerStore:
    """Process-local store. The default, and what the tests use.

    Handing out copies rather than the stored object means a caller cannot
    mutate the ledger by accident: a change only lands when ``save`` is called,
    which is also when the version is checked.
    """

    def __init__(self) -> None:
        self._ledgers: dict[str, Ledger] = {}

    async def load(self, user_id: str) -> Ledger | None:
        stored = self._ledgers.get(user_id)
        return stored.model_copy(deep=True) if stored is not None else None

    async def save(self, ledger: Ledger) -> None:
        # Equality, not "strictly newer": two writers can hold the same version,
        # and a `>` check lets both through, so the second overwrites the first.
        stored = self._ledgers.get(ledger.user_id)
        if stored is not None and stored.version != ledger.version:
            raise LedgerConflictError(
                f"ledger {ledger.user_id} moved on (stored v{stored.version} != "
                f"v{ledger.version}); reload and retry"
            )
        ledger.touch()
        self._ledgers[ledger.user_id] = ledger.model_copy(deep=True)

    def seed(self, ledger: Ledger) -> None:
        """Install a ledger directly. Tests and fixtures only."""
        self._ledgers[ledger.user_id] = copy.deepcopy(ledger)


class RedisLedgerStore:
    """Shared ledger for multi-worker deployments.

    Redis is the ledger. It is not an optimisation: the ledger is single-writer
    per user, and the ``version`` counter turns a lost update into a loud
    :class:`LedgerConflictError` instead of a silent overwrite.

    When Redis is unreachable the store raises :class:`LedgerUnavailable`, which
    the orchestrator turns into a typed refusal the user can be told. It does
    **not** silently fall back to process memory, because with more than one
    worker that means two divergent ledgers, and a user whose money lives in two
    places has no ledger at all.

    ``single_process=True`` is the one exception, and it has to be chosen
    explicitly: a single instance with no replicas can keep serving money from an
    in-process store during an outage without the divergence problem, so the
    fallback is allowed there and logs loudly when it engages.
    """

    _KEY = "miriam:ledger:"

    def __init__(self, redis_url: str | None = None, *, single_process: bool = False):
        self.redis_url = redis_url
        self.single_process = single_process
        self._redis: Any = None
        self._fallback = InMemoryLedgerStore()
        self._degraded_logged = False

    async def _client(self) -> Any:
        if self._redis is None:
            import redis.asyncio as aioredis

            from miriam_agent.config.settings import get_settings

            self._redis = aioredis.from_url(
                self.redis_url or get_settings().REDIS_URL, decode_responses=True
            )
        return self._redis

    def _degrade(self, exc: Exception) -> None:
        if not self._degraded_logged:
            logger.error(
                "ledger store is degraded: Redis is unavailable (%s). Serving "
                "from a process-local ledger because MONEY_SINGLE_PROCESS is on. "
                "This is only safe with one worker.",
                exc,
            )
            self._degraded_logged = True

    def _unavailable(self, exc: Exception, operation: str) -> LedgerUnavailable:
        logger.error(
            "ledger store is unavailable (%s during %s); nothing was read and "
            "nothing moved",
            exc,
            operation,
        )
        return LedgerUnavailable(f"the ledger store is unavailable: {exc}")

    async def load(self, user_id: str) -> Ledger | None:
        try:
            client = await self._client()
            raw = await client.get(f"{self._KEY}{user_id}")
        except Exception as exc:  # noqa: BLE001 - availability, not correctness
            if self.single_process:
                self._degrade(exc)
                return await self._fallback.load(user_id)
            raise self._unavailable(exc, "load") from exc
        if raw is None:
            # A ledger written while degraded lives only in the fallback.
            return await self._fallback.load(user_id)
        return Ledger.model_validate_json(raw)

    # Compare-and-set, run by Redis as one atomic script.
    #
    # A GET-then-SET from the client is not enough: two writers can both read
    # version N, both pass the check, and the later SET silently overwrites the
    # earlier one — movements and idempotency keys included. This writes only
    # when the stored version still equals the one the caller loaded, and returns
    # the stored version when it does not (-1 means the write landed).
    #
    # A missing key counts as version 0, so two writers racing to create the same
    # ledger cannot both win either.
    _CAS = """
local raw = redis.call('GET', KEYS[1])
local stored_version = 0
if raw then
  local ok, decoded = pcall(cjson.decode, raw)
  if not ok or type(decoded) ~= 'table' or decoded.version == nil then
    return -2
  end
  stored_version = tonumber(decoded.version) or 0
end
local expected = tonumber(ARGV[2])
if stored_version ~= expected then
  return stored_version
end
redis.call('SET', KEYS[1], ARGV[1])
return -1
"""

    async def save(self, ledger: Ledger) -> None:
        try:
            client = await self._client()
            key = f"{self._KEY}{ledger.user_id}"
            expected = ledger.version
            candidate = ledger.model_copy(update={"version": expected + 1})
            stored_version = await client.eval(
                self._CAS, 1, key, candidate.model_dump_json(), str(expected)
            )
            if stored_version == -2:
                raise LedgerUnavailable(
                    f"ledger {ledger.user_id} is stored in a form this code "
                    "cannot read; refusing to overwrite it"
                )
            if stored_version != -1:
                raise LedgerConflictError(
                    f"ledger {ledger.user_id} moved on (stored v{stored_version} "
                    f"!= v{expected}); reload and retry"
                )
            # Only now, once the write is known to have landed.
            ledger.version = candidate.version
        except LedgerConflictError:
            raise
        except LedgerUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - availability, not correctness
            if self.single_process:
                self._degrade(exc)
                await self._fallback.save(ledger)
                return
            raise self._unavailable(exc, "save") from exc


__all__ = [
    "SLEEVES",
    "Bill",
    "Challenge",
    "InMemoryLedgerStore",
    "Last30d",
    "Ledger",
    "LedgerConflictError",
    "LedgerError",
    "LedgerStore",
    "Movement",
    "PendingInflow",
    "RedisLedgerStore",
    "RentFirst",
    "Track",
    "money",
    "new_ledger",
]
