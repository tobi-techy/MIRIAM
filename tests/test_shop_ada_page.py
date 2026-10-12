"""The page off Ada's counter: two blocks, one with no opening column.

Read live from the owner's iPhone on 12/10/2026, it produced ten false "does not
add up" rows. The November block has no opening column; the reader copied
"quantity received" into opening, and the check then accused the page of
arithmetic it never wrote. These tests pin the fix: an absent column stays null,
a balance is carried forward by code from the same item's earlier row, and a row
with nothing to carry is asked about, never accused.
"""

from __future__ import annotations

import json

from miriam_agent.shop.checks import check_stock_book, same_item
from miriam_agent.shop.reply import stock_book_reply
from miriam_agent.shop.schemas import StockBook
from tests.shop_fakes import ada_stock_book


def _reply(book: StockBook) -> str:
    return stock_book_reply(book, check_stock_book(book), [])


def test_every_october_row_adds_up() -> None:
    """October has the opening column, and every row in it is internally sound."""
    report = check_stock_book(ada_stock_book())
    failing = [c.name for c in report.checks if not c.passed]
    assert failing == [
        # Eva Water and Milo are the only two November rows the book gets
        # wrong, once October's closing is carried forward.
        "stock_left:Eva Water (75cl)",
        "stock_left:Milo (400g)",
    ]
    assert report.rows_failing == 2
    assert report.missing_opening == ["Indomie (Chicken)"]


def test_november_openings_come_from_october_not_from_the_other_columns() -> None:
    """The exact figures the owner checked by hand against the paper."""
    report = check_stock_book(ada_stock_book())
    messages = {c.name: c.message for c in report.checks if not c.passed}
    assert (
        messages["stock_left:Eva Water (75cl)"] == "160 + 80 - 30 = 210, book says 190"
    )
    assert messages["stock_left:Milo (400g)"] == "40 + 25 - 10 = 55, book says 50"
    # The carry is October's closing, not November's own quantity received.
    assert "30 + 30 - 10" not in " ".join(messages.values())


def test_the_reply_asks_for_the_opening_it_was_never_shown() -> None:
    """One question, in the period's own words, for the one row that has no
    earlier balance to carry: Indomie (Chicken) never appears in October."""
    reply = _reply(ada_stock_book())
    assert "Ada's General Trading, October and November 2025. I read 24 items." in reply
    assert "Eva Water (75cl) (160 + 80 - 30 = 210, book says 190)." in reply
    assert "Milo (400g) (40 + 25 - 10 = 55, book says 50)." in reply
    assert "What did you start November with for Indomie (Chicken)?" in reply
    # The other eight November rows are not mentioned as failures at all.
    for quiet in ("Peak Milk (500g)", "Golden Morn", "Soya Beans", "Dettol (750ml)"):
        assert f"{quiet} (" not in reply


def test_a_row_that_does_not_add_up_is_not_confirmed_twice() -> None:
    """It is stated once, under its own heading. "Please confirm" carries only
    what we could not read or were never shown."""
    reply = _reply(ada_stock_book())
    assert "check the numbers, they do not add up" not in reply
    confirm = reply.split("Please confirm:", 1)[1]
    assert "What did you start November with for Indomie (Chicken)?" in confirm
    assert "Eva Water" not in confirm
    assert "Milo (400g)" not in confirm


def test_an_absent_opening_column_stays_null() -> None:
    """A block without the column extracts with no opening, and the row is left
    unchecked rather than filled in from a neighbouring column."""
    book = StockBook.model_validate(
        {
            "shop": "Ada's General Trading",
            "date": "01/11-29/11",
            "rows": [
                {
                    "item": "Eva Water (75cl)",
                    "block": "November",
                    "supplied": 80,
                    "sold": 30,
                    "left": 190,
                }
            ],
        }
    )
    assert book.rows[0].opening is None
    report = check_stock_book(book)
    assert [c.name for c in report.checks] == []
    assert report.missing_opening == ["Eva Water (75cl)"]
    assert report.rows_failing == 0
    assert "1 row(s) with no opening balance" in report.flags


def test_an_opening_is_carried_only_from_the_same_item() -> None:
    """ "Indomie (Chicken)" must never take "Indomie (Curry)"s closing: a carried
    opening from another pack size is a made-up number with extra steps."""
    assert same_item("Indomie (Chicken)", "indomie (chicken)")
    assert not same_item("Indomie (Chicken)", "Indomie (Curry)")
    book = ada_stock_book()
    book.rows[14].item = "Indomie (Curry)"
    report = check_stock_book(book)
    # Same name as October's row: the carry applies and the arithmetic is sound.
    assert report.missing_opening == []
    assert report.rows_failing == 2


def test_the_reader_is_told_never_to_fill_an_absent_column() -> None:
    """The prompt carries the rule, so the model is not left to guess either."""
    from miriam_agent.shop.reader import _EXTRACT_SYSTEM

    assert "never copy quantity received, sold or left into opening" in _EXTRACT_SYSTEM
    assert "every opening in that block is null" in _EXTRACT_SYSTEM
    assert "name every period the page covers in `date`" in _EXTRACT_SYSTEM


def test_the_page_round_trips_through_the_extraction_schema() -> None:
    """The payload a stub provider returns for this page parses as it stands."""
    payload = json.loads(ada_stock_book().model_dump_json())
    again = StockBook.model_validate(payload)
    assert len(again.rows) == 24
    assert [r.opening for r in again.rows[14:]] == [None] * 10
    assert {r.block for r in again.rows} == {"October", "November"}
