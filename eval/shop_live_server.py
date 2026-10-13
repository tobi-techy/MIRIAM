"""Serve the shop read endpoint with the real reader, for a live end-to-end run.

Everything on the read path is real: the Go-minted per-user JWT is decoded and
its subject enforced, the page is classified and routed, the vision model reads
it, every number is checked in plain code, and the reply is composed from the
checked values. Only the document store is process-local, so the run needs no
Postgres.

    LLMAPI_API_KEY=... JWT_SECRET=<the same secret Go mints with> \
        .venv/bin/python eval/shop_live_server.py

Then, from the Go repo:

    PYTHON_AGENT_URL=http://127.0.0.1:8099 JWT_SECRET=<the same secret> \
        SHOP_READ_USER_ID=<rail user id> go run ./scripts/shop_read_smoke -file page.png
"""

from __future__ import annotations

import logging
import os

import uvicorn

from miriam_agent.api import shop as shop_api
from miriam_agent.api.main import app
from miriam_agent.shop.store import InMemoryShopDocumentStore


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    shop_api.set_store(InMemoryShopDocumentStore())
    host = os.getenv("SHOP_LIVE_HOST", "127.0.0.1")
    port = int(os.getenv("SHOP_LIVE_PORT", "8099"))
    logging.getLogger(__name__).info("serving the shop reader on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
