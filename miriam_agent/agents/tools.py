"""Tool registry for Miriam Financial Agent.

Each tool is a self-contained unit: a name, description, JSON Schema for
argument validation, safety metadata (risk level, mutation flag, approval
requirement), and an async handler.

The registry serves three consumers:
  1. The LLM - via ``llm_schemas()`` which returns OpenAI-style function
     definitions for tool calling.
  2. The agent execution loop - via ``execute()`` which validates args,
     checks safety, and runs the handler with telemetry.
  3. The audit system - every execution is logged.
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from miriam_agent.core.exceptions import (
    IntegrationError,
    ToolExecutionError,
    ValidationError,
)
from miriam_agent.observability.correlation import current_trace_id


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


Handler = Callable[..., Awaitable[dict[str, Any]]]


@dataclass
class Tool:
    """A single tool the agent can invoke."""

    name: str
    description: str
    args_schema: dict[str, Any]
    handler: Handler
    category: str = "general"
    risk_level: RiskLevel = RiskLevel.LOW
    is_mutation: bool = False
    requires_approval: bool = False
    allow_auto_execute: bool = True
    tags: list[str] = field(default_factory=list)

    def to_llm_schema(self) -> dict[str, Any]:
        """Return an OpenAI-style function definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_schema,
            },
        }


def validate_args(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
    """Validate tool arguments against the tool's JSON Schema."""
    required = [r for r in tool.args_schema.get("required", []) if r not in args]
    properties = tool.args_schema.get("properties", {})

    if required:
        raise ValidationError(
            f"Missing required arguments for '{tool.name}': {', '.join(required)}"
        )

    for key, spec in properties.items():
        if key not in args:
            continue
        value = args[key]
        expected = spec.get("type")
        if expected == "number" and isinstance(value, str):
            try:
                value = float(value)
                args[key] = value
            except ValueError:
                raise ValidationError(
                    f"Argument '{key}' for '{tool.name}' must be type 'number', "
                    f"got '{value!r}'"
                )
        elif expected == "integer" and isinstance(value, str):
            try:
                value = int(float(value))
                args[key] = value
            except ValueError:
                raise ValidationError(
                    f"Argument '{key}' for '{tool.name}' must be type 'integer', "
                    f"got '{value!r}'"
                )
        if expected and not _type_matches(value, expected):
            raise ValidationError(
                f"Argument '{key}' for '{tool.name}' must be type '{expected}', "
                f"got '{type(value).__name__}'"
            )
    return args


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


class ToolRegistry:
    """Central registry of all agent tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._observers: list[Callable[[str, dict[str, Any]], None]] = []

    def register(self, tool: Tool | None = None, **fields: Any) -> Tool:
        """Register a tool. Duplicate names raise ValueError.

        Accepts a ``Tool`` instance positionally, a dict of Tool fields, or
        the fields as keyword arguments.
        """
        if tool is None:
            tool = Tool(**fields)
        elif not isinstance(tool, Tool):
            tool = Tool(**tool)
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool
        return tool

    def register_decorator(
        self,
        name: str,
        description: str,
        args_schema: dict[str, Any],
        category: str = "general",
        risk_level: RiskLevel = RiskLevel.LOW,
        is_mutation: bool = False,
        requires_approval: bool = False,
        allow_auto_execute: bool = True,
        tags: list[str] | None = None,
    ):
        """Decorator for registering a handler function as a tool."""

        def decorator(fn: Handler) -> Handler:
            self.register(
                Tool(
                    name=name,
                    description=description,
                    args_schema=args_schema,
                    handler=fn,
                    category=category,
                    risk_level=risk_level,
                    is_mutation=is_mutation,
                    requires_approval=requires_approval,
                    allow_auto_execute=allow_auto_execute,
                    tags=tags or [],
                )
            )
            return fn

        return decorator

    def get(self, name: str) -> Tool | None:
        """Look up a tool by name."""
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        """Return all registered tool names."""
        return sorted(self._tools.keys())

    def list_by_category(self, category: str) -> list[Tool]:
        """Return tools in a given category."""
        return [t for t in self._tools.values() if t.category == category]

    def llm_schemas(
        self,
        include_only: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return LLM-ready function schemas, optionally filtered."""
        exclude = set(exclude or [])
        result = []
        for name in sorted(self._tools):
            if include_only is not None and name not in include_only:
                continue
            if name in exclude:
                continue
            result.append(self._tools[name].to_llm_schema())
        return result

    def auto_execute_names(self) -> set[str]:
        """Names of tools safe to auto-execute without user confirmation."""
        return {
            name
            for name, tool in self._tools.items()
            if tool.allow_auto_execute and not tool.is_mutation
        }

    def stage_confirm_names(self) -> set[str]:
        """Names of tools that must be staged for user confirmation."""
        return {
            name
            for name, tool in self._tools.items()
            if tool.is_mutation or tool.requires_approval
        }

    def add_observer(self, observer: Callable[[str, dict[str, Any]], None]) -> None:
        """Add a telemetry/audit observer called after each execution."""
        self._observers.append(observer)

    def _notify(self, tool_name: str, result: dict[str, Any]) -> None:
        for observer in self._observers:
            try:
                observer(tool_name, result)
            except Exception:
                pass

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate and execute a tool with telemetry.

        ``context`` carries per-request state such as user_id so handlers
        do not need it inside their own args.
        """
        tool = self._tools.get(name)
        if tool is None:
            raise ToolExecutionError(f"Unknown tool: '{name}'")

        # One id for the whole request, so the trace, the audit row, and the
        # message the user gets can be joined up (observability.correlation).
        trace_id = current_trace_id()
        start = time.perf_counter()
        try:
            validated = validate_args(tool, args or {})
            result = await tool.handler(validated, context or {})
        except ValidationError:
            raise
        except IntegrationError:
            raise
        except ToolExecutionError:
            raise
        except Exception as e:
            elapsed = time.perf_counter() - start
            self._notify(
                name,
                {
                    "status": "error",
                    "error": str(e),
                    "elapsed": elapsed,
                    "trace_id": trace_id,
                    "_context": context or {},
                    "_args": args or {},
                },
            )
            raise ToolExecutionError(f"Tool '{name}' failed: {e}")

        elapsed = time.perf_counter() - start
        if not isinstance(result, dict):
            result = {"result": result}
        result.setdefault("_tool_name", name)
        result.setdefault("_risk_level", tool.risk_level.value)
        result.setdefault("_is_mutation", tool.is_mutation)
        # Correlation id for this request, so a tool result can be tied back to
        # the message that caused it (see observability.correlation).
        if trace_id:
            result.setdefault("_trace_id", trace_id)
        self._notify(
            name,
            {
                "status": "success",
                "elapsed": elapsed,
                "result": result,
                "trace_id": trace_id,
                "_context": context or {},
                # The validated call arguments (e.g. amount, recipient).
                # Needed so the audit observer can record what actually
                # moved, not just that some tool ran -- previously the
                # audit log had no way to know an amount even existed.
                "_args": validated,
            },
        )
        return result

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):
        return iter(self._tools.values())


# Singleton registry shared across the process.
_default_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    """Get the process-wide default tool registry."""
    global _default_registry
    if _default_registry is None:
        _default_registry = ToolRegistry()
    return _default_registry
