"""What one page is, when Go could not say.

The reader is the authority on a page it can see, but only when the caller has
no classification of its own: Go's OCR pass names a stock book or a delivery
note first, and an ``unknown_image`` hint means nobody could. One cheap call
then decides the kind, and nothing is read until it has.
"""

from __future__ import annotations

from miriam_agent.agents.llm import ChatMessage, LLMAPIProvider, get_llmapi_provider
from miriam_agent.shop.reader import (
    PreparedDocument,
    _context_block,
    _data_urls,
    _effort_for,
    _model_for,
)
from miriam_agent.shop.schemas import _Strict


class PageKind(_Strict):
    """What one page is, when Go could not say.

    Only asked for when the caller hands over an explicit ``unknown_image``
    hint: Go's classifier is the authority whenever it has an opinion, so this
    never overrules a page Go already named.
    """

    kind: str = ""
    confidence: float = 0.0


_CLASSIFY_SYSTEM = (
    "You are shown one page from a Nigerian shop. Say which of these it is: "
    "stock_book, invoice, delivery_note, receipt, bank_statement, other. "
    "A stock_book is the shop's own record: rows of items with opening, "
    "supplied, sold, left and a price. An invoice or delivery_note comes from a "
    "supplier and lists what was supplied with amounts and often a subtotal. A "
    "receipt is proof of one payment. A bank_statement is a bank's own list of "
    "transactions. Answer with the kind only, and a confidence from 0 to 1."
)


def _classify_messages(prepared: PreparedDocument, caption: str) -> list[ChatMessage]:
    context = _context_block(prepared, caption)
    user = "What kind of page is this?"
    if context:
        user = f"{user}\n\n{context}"
    return [
        ChatMessage(role="system", content=_CLASSIFY_SYSTEM),
        ChatMessage(role="user", content=user, images=_data_urls(prepared)),
    ]


async def classify_page(
    prepared: PreparedDocument,
    *,
    caption: str = "",
    provider: LLMAPIProvider | None = None,
    tier: str = "small",
) -> tuple[str, float]:
    """Ask one model call what the page is. Used only for an unknown page.

    Cheap on purpose: it runs before a read, and only when the caller has said
    it has no classification of its own. A failure here is not an error the
    owner hears about: the caller keeps its fail-closed answer.
    """
    provider = provider or get_llmapi_provider(_model_for(tier))
    messages = _classify_messages(prepared, caption)
    response = await provider.complete_structured(
        messages,
        PageKind,
        reasoning_effort=_effort_for(tier),
        max_tokens=200,
    )
    page_kind = PageKind.model_validate_json(response.content)
    return page_kind.kind.strip().lower(), float(page_kind.confidence or 0.0)
