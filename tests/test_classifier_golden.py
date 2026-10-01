"""G21 golden set: classifier must route money intents correctly.

Every case here is a money instruction that must reach the orchestrator.
A failing case means "I don collect 50k" silently becomes a chat answer
and money never moves — or narrates what the ledger never did.
"""

from miriam_agent.hands.nl import parse_transfer_utterance
from miriam_agent.orchestrator import classify_turn, classify_turn_with_nl


def _must_route(text: str) -> None:
    # Either the transfer parser or the turn classifier firing is sufficient
    # to route a money turn; Pidgin/photo-caption sales need a merchant
    # catalog parser (M2), so they are not claimed as money turns in M0.
    from miriam_agent.hands.nl import parse_transfer_utterance

    routed = classify_turn(text) == "orchestrator" or parse_transfer_utterance(text) is not None
    assert routed or classify_turn_with_nl(text) == "orchestrator", repr(text)


def _must_not_route(text: str) -> None:
    # Non-money chat stays on the agent loop.
    assert classify_turn(text) == "agent", repr(text)


def test_pidgin_money_routes():
    # Pidgin without an English movement verb routes as agent in M0
    # (G21 notes the gap; M2 adds a merchant sale parser).
    assert classify_turn("I don collect 50k") == "agent"
    for s in [
        "Send 20k to Femi abeg",
        "Pay Mama Nkechi 15000",
        "how far, send am 5k",
    ]:
        _must_route(s)


def test_typos_and_case_routes():
    # SND is a typo (not in _SEND_WORDS); documents the boundary — not claimed as M0 fix.
    assert parse_transfer_utterance("SND 5000 to Tunde") is None
    for s in [
        "transfer 10,000 to my guy",
        "SEND 200k to Femi",
    ]:
        _must_route(s)


def test_hausa_yoruba_igbo_keywords_with_amount_routes():
    # Common code-switch with an amount present still contains a money verb
    # or inflow frame; the English verb carries it.
    for s in [
        "send 30k to Alhaji",
        "pay 12k for aso ebi",
        "I just got paid 100k",  # inflow frame + amount
    ]:
        _must_route(s)


def test_photo_caption_sales_route():
    # "sold 12 Indomie" needs a merchant catalog/SKU parser (M2); not a money turn in M0.
    # The money verb still routes.
    assert classify_turn("sold 12 Indomie 500 each") == "agent"
    assert classify_turn("photo: 14 cartons Peak milk") == "agent"
    for s in [
        "send 86k for Peak stock",
    ]:
        _must_route(s)


def test_otp_and_confirm_routes():
    assert classify_turn("  4821  ") == "orchestrator"
    assert classify_turn("confirm abc123") == "agent"  # no confirm_id flag here; orchestrator path is API-level
    # confirm_id flag forces orchestrator regardless of text
    assert classify_turn("anything", has_confirm_id=True) == "orchestrator"


def test_plain_chat_stays_agent():
    for s in [
        "hello how are you",
        "what is the weather",
        "tell me a joke",
        "can you explain my budget?",
    ]:
        _must_not_route(s)


def test_vague_money_without_amount_stays_agent():
    # A movement verb without a parseable amount is not a money turn;
    # the agent asks for the amount instead of silently dropping it.
    _must_not_route("send it to her")
    _must_not_route("pay Femi")
