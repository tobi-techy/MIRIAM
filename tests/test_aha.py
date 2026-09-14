"""Tests for aha-moment detection (spec v1.1 §50-§51).

The plan engine engineers the moment; ``utils.aha`` marks when it lands.
Detection is exact-phrase and word-bounded so generic enthusiasm never
registers and filler exclamations stay silent.
"""

from __future__ import annotations

from miriam_agent.utils.aha import aha_kind, detect_aha


def test_recognition_phrases():
    for text in [
        "wait, that's actually true",
        "you're right, and i never saw it that way",
        "you are exactly right",
        "so that's why i always run out",
        "that's literally me",
        "now it makes sense",
    ]:
        signal = detect_aha(text)
        assert signal is not None, text
        assert signal.kind == "recognition", text


def test_insight_phrases():
    for text in [
        "i never realized the trigger was the stress buys",
        "i didn't realise the fees were that big",
        "interesting how the fixed costs eat everything",
    ]:
        signal = detect_aha(text)
        assert signal is not None, text
        assert signal.kind == "insight", text


def test_standalone_reframe_exclamation():
    for text in ["damn", "wow", "huh", "wait", "hold on", "oh wait."]:
        signal = detect_aha(text)
        assert signal is not None, text
        assert signal.kind == "reframe", text


def test_filler_exclamations_are_not_aha():
    # Exclamations used as filler connectors are not lightbulb moments.
    assert detect_aha("wait, one more thing") is None
    assert detect_aha("wow that's a long list") is None
    assert detect_aha("hmm, not sure") is None
    assert detect_aha("but wait, then the fees change") is None


def test_relief_phrases():
    signal = detect_aha("that was way easier than i thought")
    assert signal is not None
    assert signal.kind == "relief"


def test_aha_kind_returns_empty_string_when_none():
    assert aha_kind("sure, send it over") == ""
    assert aha_kind("Yes, set it up") == ""
    assert aha_kind("") == ""


def test_aha_signal_carries_the_firing_phrase():
    signal = detect_aha("that's actually true, it changes everything")
    assert signal is not None
    assert signal.phrase == "that's actually true"
    assert signal.text == "that's actually true, it changes everything"


def test_recognition_outranks_reframe():
    # "wait, that's actually it" is a recognition, not a filler exclamation.
    signal = detect_aha("wait, that's actually it")
    assert signal is not None
    assert signal.kind == "recognition"


def test_case_insensitive():
    signal = detect_aha("THAT'S ACTUALLY TRUE")
    assert signal is not None
    assert signal.kind == "recognition"
