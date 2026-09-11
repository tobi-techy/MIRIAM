"""Custom exceptions for Miriam Financial Agent."""


class MiriamError(Exception):
    """Base exception for all Miriam errors."""

    def __init__(self, message: str = "An error occurred", details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class AgentError(MiriamError):
    """Raised when the agent fails to process a message."""


class FinancialError(MiriamError):
    """Raised when a financial operation fails."""


class IntegrationError(MiriamError):
    """Raised when an external integration fails."""


class PolicyError(MiriamError):
    """Raised when an action violates a safety policy."""


class SafetyError(MiriamError):
    """Raised when a safety check fails."""


class ValidationError(MiriamError):
    """Raised when input validation fails."""


class SecurityError(MiriamError):
    """Raised when a security check fails."""


class AuthenticationError(MiriamError):
    """Raised when authentication fails."""


class AuthorizationError(MiriamError):
    """Raised when authorization fails."""


class RateLimitError(MiriamError):
    """Raised when rate limit is exceeded."""


class ToolExecutionError(MiriamError):
    """Raised when a tool fails to execute."""


class MemoryError(MiriamError):
    """Raised when a memory operation fails."""


class ConfigurationError(MiriamError):
    """Raised when configuration is invalid or missing."""
