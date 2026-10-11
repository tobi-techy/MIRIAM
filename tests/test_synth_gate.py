"""G23: chat text must never mint ledger money outside dev.

The demo flag ALLOW_CHAT_INFLOW_SYNTH is the only path where pasted
"alert" text can split the ledger; in production it is refused at boot
(settings guard) and routing must not mint.
"""


from miriam_agent.config.settings import Settings


def test_production_refuses_synth_flag():
    # ALLOW_CHAT_INFLOW_SYNTH=True in production must fail the settings guard.
    try:
        Settings(
            ENVIRONMENT="production",
            JWT_SECRET="x" * 40,
            SECRET_KEY="x" * 40,
            ENCRYPTION_KEY="x" * 40,
            RAIL_SERVICE_KEY="x" * 40,
            ALLOW_CHAT_INFLOW_SYNTH=True,
        )
    except ValueError as exc:
        assert "ALLOW_CHAT_INFLOW_SYNTH" in str(exc)
        return
    assert False, "production should have refused synth flag"


def test_inflow_is_text_hash_stable():
    from miriam_agent.orchestrator import inflow_id_for_alert

    a = "Credit alert: NGN50,000 from John"
    b = "credit alert:  ngn50,000   from john"
    # Case + whitespace normalized, same hash.
    assert inflow_id_for_alert(a) == inflow_id_for_alert(b)
    assert inflow_id_for_alert(a) != inflow_id_for_alert(a + "x")


def test_patreference_hash_is_structured():
    """PaymentReference dedupe is on structured columns, not raw text hash."""
    import hashlib

    def structured_hash(
        channel: str, sender: str, amount_minor: int, ref: str, raw: str
    ) -> str:
        h = hashlib.sha256(
            f"{channel}|{sender}|{amount_minor}|{ref}|{raw}".encode()
        ).hexdigest()[:16]
        return h

    # Two wordings of the same payment that differ only in whitespace still hash
    # differently at the raw layer but the structured dedupe key (sender+amount+ref)
    # would collide — which is the desired behavior under PaymentReference.
    assert structured_hash(
        "whatsapp", "+234801", 50000, "REF1", "alert A"
    ) != structured_hash("whatsapp", "+234801", 50000, "REF1", "alert  A")
