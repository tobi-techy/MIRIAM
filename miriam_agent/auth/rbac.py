"""RBAC (Role-Based Access Control) for Miriam Financial Agent.

Permits tools, endpoints, and money actions based on the user's financial
tier and verified status. The Go backend is the source of truth for
identity; this module adds the agent-specific action permissions.

Permissions come from tool metadata in the registry to avoid drift:
- read-only tools require the ``read`` role
- planning tools require ``plan``
- money-movement (mutation) tools require ``execute`` (verified users)
"""


from miriam_agent.core.exceptions import AuthorizationError

# Roles granted by identity tier (verified status drives capability)
ROLE_LEVELS: dict[str, set[str]] = {
    "guest": {"read"},
    "user": {"read", "plan"},
    "verified": {"read", "plan", "execute"},
    "admin": {"read", "plan", "execute", "admin"},
}

# Tool name -> minimum required permission (filled from registry on import).
# Unknown tools default to "read" (safe default: they may be audit-only).
_TOOL_PERMISSIONS: dict[str, str] = {}


def _load_tool_permissions() -> None:
    """Populate tool permissions from the registered tool metadata."""
    try:
        from miriam_agent.tools import build_tool_registry

        registry = build_tool_registry()
        for tool in registry:
            if tool.is_mutation or tool.requires_approval:
                _TOOL_PERMISSIONS[tool.name] = "execute"
            else:
                _TOOL_PERMISSIONS[tool.name] = "read"
    except Exception:
        # Registry not available (e.g. cold import); fall back to static map.
        _TOOL_PERMISSIONS.update(
            {
                "transfer_funds": "execute",
                "withdraw_funds": "execute",
                "deposit_funds": "execute",
                "execute_strategy": "execute",
            }
        )


_load_tool_permissions()


def can_execute(user_roles: set[str], tool_name: str) -> bool:
    """Check whether the user's roles allow executing the given tool."""
    if tool_name not in _TOOL_PERMISSIONS:
        _load_tool_permissions()
    required = _TOOL_PERMISSIONS.get(tool_name, "read")
    for role in user_roles:
        level = ROLE_LEVELS.get(role, set())
        if required in level:
            return True
    return False


def require_tool_access(user_roles: set[str], tool_name: str) -> None:
    """Raise AuthorizationError if the user cannot execute the tool."""
    if not can_execute(user_roles, tool_name):
        raise AuthorizationError(
            f"User does not have permission to execute '{tool_name}'. "
            "Contact support to verify your account for money actions."
        )


def role_level(role: str) -> str:
    """Resolve effective permission level (highest) for a role."""
    perms = ROLE_LEVELS.get(role, {"read"})
    order = ["read", "plan", "execute", "admin"]
    return next((p for p in reversed(order) if p in perms), "read")
