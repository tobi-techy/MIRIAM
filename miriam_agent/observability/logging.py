"""Structured logging setup for Miriam Financial Agent."""

import logging
import os
import sys

import structlog


def setup_logging(level: str = "INFO") -> None:
    """Configure structured logging with structlog."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            (
                structlog.dev.ConsoleRenderer()
                if sys.stderr.isatty()
                else structlog.processors.JSONRenderer()
            ),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    # The TypeSafe SDK logs full request/response bodies at DEBUG. Keep that
    # off unless an operator explicitly asks for TYPESAFE_LOG_LEVEL=debug.
    if not os.getenv("TYPESAFE_LOG_LEVEL"):
        logging.getLogger("typesafe_sdk").setLevel(logging.INFO)
