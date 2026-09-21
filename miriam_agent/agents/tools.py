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

import re
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
from miriam_agent.observability.metrics import record_tool_execution


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
    """Validate tool arguments against the tool's JSON Schema.

    Enforces the subset of JSON Schema this codebase actually declares:
    required presence (an explicit ``null`` counts as absent), type, ``enum``,
    numeric bounds (``minimum`` / ``maximum`` / ``exclusiveMinimum`` /
    ``exclusiveMaximum``), string length and ``pattern``.

    None of this may be left to the model: a negative or zero transfer amount,
    an invalid enum value, or a path-traversal string in an id used to pass
    through untouched and reach the Go ledger. Everything declared at the
    registration site is checked here, before the value can reach policy or an
    HTTP call.
    """
    required = [
        r
        for r in tool.args_schema.get("required", [])
        if r not in args or args.get(r) is None
    ]
    properties = tool.args_schema.get("properties", {})

    if required:
        raise ValidationError(
            f"Missing required arguments for '{tool.name}': {', '.join(required)}"
        )

    for key, spec in properties.items():
        if key not in args or args[key] is None:
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
        _validate_constraints(tool.name, key, value, spec)
    return args


def _validate_constraints(
    tool_name: str, key: str, value: Any, spec: dict[str, Any]
) -> None:
    """Enforce the constraint keywords declared on a property schema."""
    enum = spec.get("enum")
    if enum is not None and value not in enum:
        raise ValidationError(
            f"Argument '{key}' for '{tool_name}' must be one of {list(enum)}, "
            f"got '{value!r}'"
        )

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for keyword, ok in (
            ("minimum", lambda v, b: v >= b),
            ("maximum", lambda v, b: v <= b),
            ("exclusiveMinimum", lambda v, b: v > b),
            ("exclusiveMaximum", lambda v, b: v < b),
        ):
            bound = spec.get(keyword)
            if bound is not None and not ok(value, bound):
                raise ValidationError(
                    f"Argument '{key}' for '{tool_name}' violates {keyword}="
                    f"{bound} (got {value!r})"
                )

    if isinstance(value, str):
        pattern = spec.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            raise ValidationError(
                f"Argument '{key}' for '{tool_name}' has an invalid format"
            )
        min_length = spec.get("minLength")
        if min_length is not None and len(value) < min_length:
            raise ValidationError(
                f"Argument '{key}' for '{tool_name}' must be at least "
                f"{min_length} characters"
            )
        max_length = spec.get("maxLength")
        if max_length is not None and len(value) > max_length:
            raise ValidationError(
                f"Argument '{key}' for '{tool_name}' must be at most "
                f"{max_length} characters"
            )


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

    def unregister(self, name: str) -> bool:
        """Remove a tool. Returns whether one was there.

        Used to take money tools out of the live registry: the only writer of a
        balance is ``miriam_agent.hands``, so a tool that calls a rail has no
        business being reachable from a chat turn.
        """
        return self._tools.pop(name, None) is not None

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
        """Return LLM-ready function schemas, optionally filtered.

        Read-only tools only, always. A mutation is never offered to a model,
        regardless of what the registry happens to hold: the model's only job is
        to answer, and ``hands/`` is the only thing that moves money. Filtering
        here means a tool that writes a sleeve cannot be called even if someone
        registers one by mistake.
        """
        exclude = set(exclude or [])
        result = []
        for name in sorted(self._tools):
            if include_only is not None and name not in include_only:
                continue
            if name in exclude:
                continue
            tool = self._tools[name]
            if tool.is_mutation or tool.requires_approval:
                continue
            result.append(tool.to_llm_schema())
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
            record_tool_execution(name, "error")
            raise ToolExecutionError(f"Tool '{name}' failed: {e}")

        elapsed = time.perf_counter() - start
        record_tool_execution(name, "success")
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
