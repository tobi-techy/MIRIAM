"""The boundary on the tools the agent loop may run.

**Money is not decided here, and there are no money rules in this file.**

This module used to hold the money allowlist, the per-transaction and daily
limits, the fraud-pattern heuristics, the approval workflow and a duplicate of
the tool risk scoring. All of it is gone, for two reasons:

* it was unreachable. The allowlist only ever admitted a tool named in the
  money-tool set, and the live registry holds none of them, so every money rule
  here could only ever deny; and
* it was a second answer to "is this allowed" for limits that
  ``hands/limits.py`` now owns alone. Two implementations of the same rule is how
  they drift apart, and this one had already drifted once (see the git history on
  the daily-cap check).

What remains is what this file is actually for: a boundary on the reads the
agent runs. A tool may run only if it is registered, read-only, and its
arguments carry no blocked content.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Arguments that mark a request as something Miriam will not act on. These are
# content checks on a tool's arguments, not money rules: nothing here knows or
# cares what an amount is.
BLOCKED_CATEGORIES: tuple[str, ...] = ("scam", "fraud", "illegal")


class SafetyPolicy:
    """The read-tool boundary.

    One question: may this tool run at all? A money tool never can, because it is
    not registered and the agent loop refuses it before reaching here.
    """

    def __init__(self) -> None:
        self.blocked_categories = BLOCKED_CATEGORIES

    async def validate_action(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user_id: str,
        financial_profile: Any = None,
    ) -> bool:
        """Whether a tool call is allowed to run.

        ``financial_profile`` is accepted because the agent loop has always
        passed it and the call site has no reason to care that this policy no
        longer scores anything with it. It is unused.
        """
        del financial_profile  # kept for the call site; nothing here reads it
        try:
            if not self._is_read_tool(tool_name):
                logger.warning(
                    "Tool is not a registered read; denied",
                    extra={"tool": tool_name, "user_id": user_id},
                )
                return False
            if self._has_blocked_content(arguments):
                logger.warning(
                    "Blocked content in tool arguments",
                    extra={"tool": tool_name, "user_id": user_id},
                )
                return False
            return True
        except Exception:
            logger.error(
                "Error validating action",
                extra={"tool": tool_name, "user_id": user_id},
                exc_info=True,
            )
            return False

    @staticmethod
    def _is_read_tool(tool_name: str) -> bool:
        """Whether the registry holds this tool and it only reads.

        The registry is the source of truth, so this cannot drift from what is
        actually callable: a tool that is not there is denied, and a tool that
        writes is denied.
        """
        try:
            from miriam_agent.tools import ensure_registered

            tool = ensure_registered().get(tool_name)
        except Exception:  # noqa: BLE001 - registry unavailable, stay closed
            return False
        if tool is None:
            return False
        return not (tool.is_mutation or tool.requires_approval)

    def _has_blocked_content(self, arguments: dict[str, Any]) -> bool:
        """Whether a tool's arguments name a blocked category."""
        if not isinstance(arguments, dict):
            return False
        for field in ("description", "category"):
            value = arguments.get(field)
            if not isinstance(value, str) or not value:
                continue
            lowered = value.casefold()
            for blocked in self.blocked_categories:
                if blocked.casefold() in lowered:
                    return True
        return False


__all__ = ["BLOCKED_CATEGORIES", "SafetyPolicy"]
