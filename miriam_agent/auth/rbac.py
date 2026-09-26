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

# Tool name -> minimum required permission.
#
# A name that is not in here is denied rather than defaulted. It used to default
# to "read", which was harmless while every money tool was registered and
# classified as "execute" -- but the registry no longer holds a money tool, so
# that default would have quietly granted read access to `send_money`. An
# unknown name now requires the tool to be registered before it can be called.
#
# This map used to be filled by importing the tool registry from here, which
# pointed cross-cutting auth at the adapters layer (forbidden by
# docs/ARCHITECTURE-CONTRACT.md §3). The direction is inverted now: the tools
# layer pushes its permissions in through ``register_tool_permissions`` when
# the registry is built, which is the allowed adapters -> cross_cutting edge.
_TOOL_PERMISSIONS: dict[str, str] = {
    # Static floor for cold start, before any registry build: legacy money
    # tool names that no longer exist in the registry stay classified as
    # execute so a stale caller is refused, not mis-graded.
    "transfer_funds": "execute",
    "withdraw_funds": "execute",
    "deposit_funds": "execute",
    "execute_strategy": "execute",
}


def register_tool_permissions(permissions: dict[str, str]) -> None:
    """Record the permission each registered tool requires.

    Called by the tools layer when the registry is built; values are
    ``"execute"`` for mutation/approval tools and ``"read"`` otherwise.
    """
    _TOOL_PERMISSIONS.update(permissions)


def can_execute(user_roles: set[str], tool_name: str) -> bool:
    """Check whether the user's roles allow executing the given tool."""
    required = _TOOL_PERMISSIONS.get(tool_name)
    if required is None:
        # Not a registered tool: nothing to grant access to.
        return False
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
