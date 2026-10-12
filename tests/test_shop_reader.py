"""Reader tests: two passes, corrections, the read cap. Provider faked."""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import pytest
from PIL import Image
from shop_fakes import FakeProvider, mama_tobi_book, provider_factory, wigmore_invoice

from miriam_agent.shop import preprocess
from miriam_agent.shop.reader import (
    ReadBudget,
    ReadCapExceeded,
    ReadFailed,
    _leaf_annotation,
    read_document,
    read_on_tier,
)
from miriam_agent.shop.schemas import CrossedOut, StockBook, SupplierDocument


def png_bytes(size: tuple[int, int] = (600, 400)) -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGB", size, (250, 246, 236))
    for x in range(0, size[0], 40):
        for y in range(0, size[1], 40):
            image.putpixel((x, y), (10, 10, 10))
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture()
def prepared():
    return preprocess.prepare(png_bytes(), "image/png", "stock book")


def payload(model: StockBook | SupplierDocument) -> dict:
    return json.loads(model.model_dump_json())


async def test_read_on_tier_validates_and_sends_the_image(prepared) -> None:
    provider = FakeProvider([payload(mama_tobi_book())])
    extraction, flags, confidence, model_path = await read_on_tier(
        prepared, StockBook, "small", provider=provider
    )
    assert isinstance(extraction, StockBook)
    assert len(extraction.rows) == 8
    assert confidence == pytest.approx(0.95)
    assert model_path == ["small", "small:verify"]
    assert provider.extract_calls == 1
    assert provider.verify_calls == 1
    # The image really went to the provider, as a data URL.
    assert provider.images
    assert provider.images[0][0].startswith("data:image/png;base64,")
    decoded = base64.b64decode(provider.images[0][0].split(",", 1)[1])
    assert decoded[:8] == b"\x89PNG\r\n\x1a\n"
    assert "verify corrected" not in " ".join(flags)


async def test_verify_correction_lands_on_the_named_cell(prepared) -> None:
    provider = FakeProvider(
        [payload(mama_tobi_book())],
        verify={"corrections": [{"path": "rows[0].sold", "value": "9"}]},
    )
    extraction, flags, _, _ = await read_on_tier(
        prepared, StockBook, "small", provider=provider
    )
    assert isinstance(extraction, StockBook)
    assert extraction.rows[0].sold == 9
    assert "verify corrected 1 cell(s)" in flags


async def test_verify_correction_keeps_money_as_a_decimal_string(prepared) -> None:
    provider = FakeProvider(
        [payload(mama_tobi_book())],
        verify={
            "corrections": [
                {"path": "rows[1].price_naira", "value": "6000"},
                {"path": "written_total_sales_naira", "value": "250000"},
            ]
        },
    )
    extraction, flags, _, _ = await read_on_tier(
        prepared, StockBook, "small", provider=provider
    )
    assert isinstance(extraction, StockBook)
    assert extraction.rows[1].price_naira == "6000"
    assert extraction.written_total_sales_naira == "250000"
    assert "verify corrected 2 cell(s)" in flags


async def test_verify_pointing_at_an_unknown_cell_is_flagged_not_applied(
    prepared,
) -> None:
    provider = FakeProvider(
        [payload(mama_tobi_book())],
        verify={"corrections": [{"path": "rows[99].sold", "value": "9"}]},
    )
    extraction, flags, _, _ = await read_on_tier(
        prepared, StockBook, "small", provider=provider
    )
    assert isinstance(extraction, StockBook)
    assert extraction.rows[0].sold == 3
    assert any("unknown cell" in f for f in flags)


async def test_verify_unavailable_keeps_the_extraction(prepared) -> None:
    provider = FakeProvider(
        [payload(mama_tobi_book())], verify={"corrections": "not a list"}
    )
    extraction, flags, _, _ = await read_on_tier(
        prepared, StockBook, "small", provider=provider
    )
    assert isinstance(extraction, StockBook)
    assert len(extraction.rows) == 8
    assert "verify pass unavailable" in flags


async def test_one_schema_retry_then_success(prepared) -> None:
    provider = FakeProvider(
        ["{not json", payload(mama_tobi_book())],
    )
    extraction, flags, _, _ = await read_on_tier(
        prepared, StockBook, "small", provider=provider
    )
    assert isinstance(extraction, StockBook)
    assert "schema retry" in flags
    assert provider.extract_calls == 2


async def test_two_invalid_answers_fail_closed(prepared) -> None:
    provider = FakeProvider(["{not json", "{still not json"])
    with pytest.raises(ReadFailed):
        await read_on_tier(prepared, StockBook, "small", provider=provider)


async def test_read_cap_stops_a_second_extraction(prepared) -> None:
    budget = ReadBudget(max_extractions=1)
    provider = FakeProvider(["{not json", payload(mama_tobi_book())])
    with pytest.raises(ReadCapExceeded):
        await read_on_tier(
            prepared, StockBook, "small", budget=budget, provider=provider
        )
    assert budget.used == 1


async def test_read_document_uses_the_schema_for_the_kind(prepared) -> None:
    provider = FakeProvider([payload(wigmore_invoice())])
    result = await read_document(
        prepared, "invoice", "small", provider_factory=provider_factory(provider)
    )
    assert isinstance(result.extraction, SupplierDocument)
    assert result.extraction.number == "WGT/INV/2610-0417"
    assert result.reads_used == 1
    assert result.schema_kind == "invoice"


async def test_read_document_falls_back_to_the_other_tier(prepared) -> None:
    small = FakeProvider(fail=True)
    big = FakeProvider([payload(mama_tobi_book())])
    result = await read_document(
        prepared,
        "stock_book",
        "small",
        provider_factory=provider_factory(small, big),
    )
    assert isinstance(result.extraction, StockBook)
    assert result.model_path[0] == "small:failed"
    assert result.degraded is True


async def test_read_document_fails_when_both_tiers_fail(prepared) -> None:
    small = FakeProvider(fail=True)
    big = FakeProvider(fail=True)
    with pytest.raises(ReadFailed):
        await read_document(
            prepared,
            "stock_book",
            "small",
            provider_factory=provider_factory(small, big),
        )


async def test_read_document_does_not_read_a_second_time_after_the_cap(
    prepared,
) -> None:
    budget = ReadBudget(max_extractions=1)
    small = FakeProvider(["{not json", payload(mama_tobi_book())])
    big = FakeProvider([payload(mama_tobi_book())])
    with pytest.raises(ReadCapExceeded):
        await read_document(
            prepared,
            "stock_book",
            "small",
            budget=budget,
            provider_factory=provider_factory(small, big),
        )
    assert big.extract_calls == 0


def test_leaf_annotation_walks_into_nested_models_and_lists() -> None:
    assert _leaf_annotation(StockBook, ["rows", 0, "left"]) is int
    assert _leaf_annotation(StockBook, ["rows", 0, "crossed_out", "new"]) is str
    assert _leaf_annotation(StockBook, ["written_total_sales_naira"]) is str
    assert _leaf_annotation(SupplierDocument, ["lines", 2, "qty"]) is int
    assert _leaf_annotation(StockBook, ["rows", 0, "not_a_field"]) is Any


async def test_a_correction_to_a_crossed_out_cell_is_applied_as_text(
    prepared,
) -> None:
    """A live read hit this: "27" became an int and killed the whole correction.

    The verify pass pointed at ``rows[3].crossed_out.new``, the old coercion
    looked that field up on the root model, found nothing, guessed "int" from the
    digits, and pydantic then rejected the corrected extraction. The correction
    was silently dropped.
    """
    book = mama_tobi_book()
    book.rows[0].crossed_out = CrossedOut(old="12", new="13")
    provider = FakeProvider(
        [payload(book)],
        verify={
            "corrections": [
                {"path": "rows[0].crossed_out.new", "value": "27"},
                {"path": "rows[0].sold", "value": "9"},
            ]
        },
    )
    extraction, flags, _, _ = await read_on_tier(
        prepared, StockBook, "small", provider=provider
    )
    assert isinstance(extraction, StockBook)
    assert extraction.rows[0].crossed_out is not None
    assert extraction.rows[0].crossed_out.new == "27"
    assert extraction.rows[0].sold == 9
    assert "verify corrected 2 cell(s)" in flags
    assert "verify correction rejected by schema" not in flags


async def test_a_correction_value_that_arrives_as_a_json_number_is_accepted(
    prepared,
) -> None:
    book = mama_tobi_book()
    book.rows[0].crossed_out = CrossedOut(old="12", new="13")
    provider = FakeProvider(
        [payload(book)],
        verify={"corrections": [{"path": "rows[0].crossed_out.new", "value": 27}]},
    )
    extraction, flags, _, _ = await read_on_tier(
        prepared, StockBook, "small", provider=provider
    )
    assert isinstance(extraction, StockBook)
    assert extraction.rows[0].crossed_out is not None
    assert extraction.rows[0].crossed_out.new == "27"
    assert "verify pass unavailable" not in flags
    assert "verify corrected 1 cell(s)" in flags


async def test_a_numeric_correction_for_a_count_still_becomes_a_number(
    prepared,
) -> None:
    provider = FakeProvider(
        [payload(mama_tobi_book())],
        verify={"corrections": [{"path": "rows[1].left", "value": "42"}]},
    )
    extraction, _, _, _ = await read_on_tier(
        prepared, StockBook, "small", provider=provider
    )
    assert isinstance(extraction, StockBook)
    assert extraction.rows[1].left == 42


async def test_one_bad_correction_does_not_discard_the_others(prepared) -> None:
    """A live read hit this: the verify pass offered "10 ctn" for an int qty.

    The whole corrected extraction failed validation, so every other correction
    in the same answer was thrown away too. A bad correction is now dropped on
    its own, by name, and the good ones survive.
    """
    provider = FakeProvider(
        [payload(mama_tobi_book())],
        verify={
            "corrections": [
                {"path": "rows[0].sold", "value": "10 ctn"},
                {"path": "rows[1].left", "value": "42"},
                {"path": "rows[2].price_naira", "value": "1800"},
            ]
        },
    )
    extraction, flags, _, _ = await read_on_tier(
        prepared, StockBook, "small", provider=provider
    )
    assert isinstance(extraction, StockBook)
    # The two usable corrections landed.
    assert extraction.rows[1].left == 42
    assert extraction.rows[2].price_naira == "1800"
    assert extraction.rows[0].sold == 3  # unchanged, still the read value
    assert "verify correction dropped: rows[0].sold" in flags
    assert "verify corrected 2 cell(s)" in flags
    assert "verify corrected 3 cell(s)" not in flags
