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


class CoolingDownError(MiriamError):
    """Raised when a strategy rebalance is refused by a 429 cooldown.

    Carries the ``retry_after`` seconds the Go backend asked the caller to wait,
    so the hands layer can present a "try again in X" message rather than a
    generic failure.
    """

    def __init__(self, message: str = "strategy is cooling down", retry_after: int | None = None):
        super().__init__(message, {"retry_after": retry_after})
        self.retry_after = retry_after


class ToolExecutionError(MiriamError):
    """Raised when a tool fails to execute."""


class MemoryError(MiriamError):
    """Raised when a memory operation fails."""


class ConfigurationError(MiriamError):
    """Raised when configuration is invalid or missing."""
