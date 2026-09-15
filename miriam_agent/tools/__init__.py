"""Tool registry and definitions for Miriam Financial Agent."""

# Imported for its registration side effects only: importing this module
# registers the investment (Glider Agent API) tools on the shared singleton
# registry.
from miriam_agent.agents.tools import RiskLevel, Tool, ToolRegistry, get_registry
from miriam_agent.tools import investment_definitions  # noqa: F401
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
