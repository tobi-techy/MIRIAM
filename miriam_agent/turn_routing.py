"""Which entrypoint owns a turn: the orchestrator (money) or the agent.

Extracted from ``orchestrator.py`` (blob-ratchet split). The classifier reads
the user's text only and never calls a model: which layer owns a turn must not
be a judgement a model can be talked out of. ``orchestrator`` re-exports these
names, so importers are unchanged.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, model_validator

# ---------------------------------------------------------------------------
# Which entrypoint owns this turn
# ---------------------------------------------------------------------------

TurnRoute = Literal["orchestrator", "agent"]

# Where a turn's text came from. The caller states it; the router never guesses
# it from the words. ``user`` is typed text, ``voice`` is the sender's own words
# transcribed. ``vision`` (read off a photo) and ``system`` (written by the
# platform) are text nobody typed, so they can never be an instruction.
MessageSource = Literal["user", "voice", "vision", "system"]

_MONEY_ELIGIBLE_SOURCES = frozenset({"user", "voice"})


def can_carry_money_intent(source: str) -> bool:
    """Whether text from this source may be routed to the money path.

    Fails closed: a source this module does not name is not eligible.
    """
    return source in _MONEY_ELIGIBLE_SOURCES


class SourcedRequest(BaseModel):
    """The ingress fields every turn request shares: where its text came from.

    The caller states ``message_source``. Absent means typed, so a caller that
    predates the field is unchanged; a value outside ``MessageSource`` is a 422.
    A confirmation or a signature is a user act: carrying one from generated
    text is refused at the boundary.
    """

    message_source: MessageSource = "user"

    @model_validator(mode="after")
    def _settlement_is_a_user_act(self) -> SourcedRequest:
        settles = (
            getattr(self, "confirm_id", "").strip()
            or getattr(self, "signed_tx", "").strip()
        )
        if settles and not can_carry_money_intent(self.message_source):
            raise ValueError("a confirmation cannot come from generated text")
        return self

    @property
    def money_eligible(self) -> bool:
        return can_carry_money_intent(self.message_source)


# Words that ask for money to move. Deliberately the same vocabulary the parser
# in hands/transfer.py understands, so a turn is never routed here for a phrase
# the parser would then ignore.
_MOVE_WORDS = frozenset(
    {
        "send",
        "transfer",
        "pay",
        "give",
        "move",
        "lock",
        "unlock",
        "freeze",
        "release",
        "reserve",
    }
)

# Words that ask for money to move into the stock sleeve. Deliberately the
# same vocabulary the parser in hands/invest.py understands, so a turn is
# never routed here for a phrase the parser would then ignore. Single-name
# tickers are not here: they are a later verb, not the sleeve.
_INVEST_WORDS = frozenset({"invest", "stocks", "sleeve"})

# Words that ask for a single-name sleeve transaction or a sleeve rebalance.
# Deliberately the same vocabulary hands/orders.py understands, so a turn is
# never routed here for a phrase the parser would then ignore. A bare ticker
# alone is not enough: the parser needs a side, an amount, and a symbol.
_ORDER_WORDS = frozenset({"buy", "sell", "long", "short", "trim", "dump"})
_REBALANCE_WORDS = frozenset({"rebalance", "re-balance", "rebal"})
_SLEEVE_NOUNS = frozenset({"sleeve", "stocks", "stock", "portfolio"})

# NGN <-> crypto funding. "buy"/"sell" plus a crypto/naira word routes to the
# orchestrator when an amount is present, mirroring hands/funding.py.
_FUND_WORDS = frozenset({"buy", "sell"})
_CRYPTO_WORDS = frozenset(
    {
        "bitcoin",
        "btc",
        "usdt",
        "usdc",
        "crypto",
        "naira",
        "ngn",
        "top up",
        "top-up",
        "fund",
        "add money",
        "onramp",
        "offramp",
        "cash out",
        "cashout",
    }
)

# Frames that ask whether something is affordable. A judgement question, not a
# statement of fact, so it goes to Judgment rather than to the answer path.
_AFFORD_FRAMES = (
    "can i afford",
    "can i buy",
    "can i get",
    "could i afford",
    "could i buy",
    "should i buy",
    "do i have enough",
    "is it affordable",
    "afford",
)

# Track changes. The track is the income split, so changing it is a money
# decision and never a chat answer.
_TRACK_FRAMES = (
    "change my track",
    "change the track",
    "set my track",
    "set the track",
    "switch to ",
    "change my split",
    "update my split",
    "change my ratios",
)

# Inflow alerts pasted into chat, e.g. "Credit alert: NGN420,000.00 from ACME".
# The authoritative inflow path is the rail webhook; this is for a forwarded
# alert. The derived id is stable, so re-pasting the same alert splits once.
_INFLOW_FRAMES = (
    "credit alert",
    "debit alert",
    "you received",
    "you have received",
    "credited with",
    "inflow of",
    "payment received",
    "salary alert",
    # Payday language typed by hand. The rail webhook is authoritative for real
    # money; a pasted "i just got paid 100" splits the same 70/30 book so the
    # demo loop and manual top-ups behave like the rail.
    "got paid",
    "payday",
)

_AMOUNT_HINT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?", re.UNICODE)


def looks_like_inflow_alert(text: str) -> bool:
    """Whether a message is a forwarded inflow alert rather than an order.

    A message that also names a movement verb is an instruction, not an alert,
    so it belongs to the utterance path.
    """
    lowered = (text or "").casefold()
    if not any(frame in lowered for frame in _INFLOW_FRAMES):
        return False
    words = set(re.findall(r"[a-z']+", lowered))
    return not (words & _MOVE_WORDS)


# The classifier reads the user's text only. It never calls a model: which layer
# owns a turn must not be a judgement a model can be talked out of.
def classify_turn_with_nl(
    text: str, *, has_confirm_id: bool = False, source: str = "user"
) -> TurnRoute:
    """Route money intents on deterministic hands/nl.py verdict (G21).

    Falls back to money-turn routing when the nl parser produces a structured
    action. Low-confidence/None returns ``agent`` so judgment asks rather than
    silently dropping a Pidgin/photo-caption sale.
    """
    if not can_carry_money_intent(source):
        return "agent"
    if has_confirm_id:
        return "orchestrator"
    try:
        from miriam_agent.hands.nl import parse_transfer_utterance

        if parse_transfer_utterance(text) is not None:
            return "orchestrator"
    except Exception:
        pass
    return classify_turn(text, has_confirm_id=has_confirm_id, source=source)


def classify_turn(
    text: str, *, has_confirm_id: bool = False, source: str = "user"
) -> TurnRoute:
    """Decide whether a turn is a money turn.

    Money turns belong to the orchestrator, where the only path is
    Hands -> Judgment -> Hands -> Voice. Everything else is answerable and goes
    to the agent loop, which cannot move money at all.

    Text that nobody typed (read off a photo, written by the platform) is never
    a money turn, whatever it says: that is decided by ``source`` before the
    words are read.
    """
    if not can_carry_money_intent(source):
        return "agent"
    if has_confirm_id:
        return "orchestrator"

    lowered = (text or "").strip().casefold()
    if not lowered:
        return "agent"

    if any(frame in lowered for frame in _TRACK_FRAMES):
        return "orchestrator"
    if any(frame in lowered for frame in _AFFORD_FRAMES):
        return "orchestrator"

    words = set(re.findall(r"[a-z']+", lowered))
    has_amount = bool(_AMOUNT_HINT_RE.search(lowered))

    if looks_like_inflow_alert(lowered) and has_amount:
        return "orchestrator"
    # A movement verb with an amount is a money turn. A movement verb without one
    # ("send it to her") is not parseable, so the agent answers it and the user
    # gets a question instead of a silent no-op.
    if has_amount and words & _MOVE_WORDS:
        return "orchestrator"
    if has_amount and words & _INVEST_WORDS:
        return "orchestrator"
    # A buy/sell of a ticker, or a sleeve rebalance, is a money turn. Both need
    # an amount (or the sleeve word) to be parseable; a bare ticker with no verb
    # is a question and goes to the agent.
    if has_amount and ((words & _ORDER_WORDS) and words & _SLEEVE_NOUNS):
        return "orchestrator"
    if words & _REBALANCE_WORDS and words & _SLEEVE_NOUNS:
        return "orchestrator"
    if "rent" in words and has_amount and words & {"reserve", "protect", "cover"}:
        return "orchestrator"
    # NGN <-> crypto funding: buy/sell + crypto/naira word + amount.
    if (
        has_amount
        and words & _FUND_WORDS
        and (words & _CRYPTO_WORDS or "top up" in lowered or "add money" in lowered)
    ):
        return "orchestrator"
    # A bare OTP (4-8 digits) answers a pending Paj challenge.
    if re.fullmatch(r"\s*\d{4,8}\s*", text or ""):
        return "orchestrator"
    return "agent"


def inflow_id_for_alert(text: str) -> str:
    """A stable id for an inflow alert pasted into chat.

    Two pastes of the same alert must split once, so the id is derived from the
    alert rather than minted per turn. The rail webhook path uses the rail's own
    payment id instead.
    """
    digest = hashlib.sha256(" ".join((text or "").split()).casefold().encode())
    return f"alert_{digest.hexdigest()[:16]}"
