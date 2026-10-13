"""Shop document reading: handwritten stock books, invoices and delivery notes.

Public entry point is :func:`miriam_agent.shop.service.read_shop_document`,
called by ``POST /api/v1/shop/documents/read``.
"""

from __future__ import annotations

from miriam_agent.shop.service import ShopReadResult, read_shop_document

__all__ = ["ShopReadResult", "read_shop_document"]
