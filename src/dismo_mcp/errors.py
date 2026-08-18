"""Domain errors surfaced by the MCP server."""


class DismoMcpError(RuntimeError):
    """Base exception for user-facing dismo-mcp failures."""


class ConfigurationError(DismoMcpError):
    """Raised when the local R runtime is not usable."""


class PathPolicyError(DismoMcpError):
    """Raised when a requested path is outside configured roots."""


class RBridgeError(DismoMcpError):
    """Raised when a whitelisted R operation fails."""

