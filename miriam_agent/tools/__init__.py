"""Tool registry and definitions for Miriam Financial Agent."""

# Imported for their registration side effects only: importing these modules
# registers the investment (Glider Agent API) tools and the money plan tool on
# the shared singleton registry.
from miriam_agent.agents.tools import RiskLevel, Tool, ToolRegistry, get_registry
from miriam_agent.tools import (  # noqa: F401  # pyright: ignore[reportUnusedImport]
    funding_definitions,
    glider_sleeve,
    glider_strategy_reads,
    investment_definitions,
    money_definitions,
    vault_definitions,
)
from miriam_agent.tools.definitions import build_tool_registry

__all__ = [
    "RiskLevel",
    "Tool",
    "ToolRegistry",
    "get_registry",
    "build_tool_registry",
]


def ensure_registered() -> ToolRegistry:
    """Ensure all tool definitions are registered and return the registry."""
    return build_tool_registry()
