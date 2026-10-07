"""gaiadesk: drive GaiaDesk desks from Python, through GaiaDesk's native
library when it is installed (``pip install gaiadesk[native]``), else through
gaiadesk-cli. ``GaiaDesk(...).backend`` says which.

    from gaiadesk import GaiaDesk
    gd = GaiaDesk(token_file="~/.config/gaiadesk/bot.token")
    r = gd.exec("123456789", "hostname")
    print(r["exit"], r["stdout"])

``AsyncGaiaDesk`` is the asyncio twin.
"""

from .aio import AsyncForward, AsyncGaiaDesk
from .client import Forward, GaiaDesk
from ._core import Completed, locate_cli
from .errors import (
    CliNotFoundError,
    CommandError,
    ConnectionLostError,
    GaiaDeskError,
    McpError,
    OperationFailedError,
    ProtocolError,
    RefusedError,
    UnreachableError,
    UsageError,
    error_envelope,
)
from .mcp import MCP_PROTOCOL_VERSION, AsyncMcpClient, McpClient, tool_image, tool_text
from .stream import AsyncCliStream, AsyncJsonExecStream, Chunk, CliStream, Exit, JsonExecStream

__version__ = "0.1.0"

__all__ = [
    "GaiaDesk",
    "AsyncGaiaDesk",
    "Forward",
    "AsyncForward",
    "CliStream",
    "AsyncCliStream",
    "JsonExecStream",
    "AsyncJsonExecStream",
    "Chunk",
    "Exit",
    "Completed",
    "McpClient",
    "AsyncMcpClient",
    "MCP_PROTOCOL_VERSION",
    "tool_text",
    "tool_image",
    "error_envelope",
    "locate_cli",
    "GaiaDeskError",
    "CliNotFoundError",
    "UsageError",
    "RefusedError",
    "UnreachableError",
    "ConnectionLostError",
    "OperationFailedError",
    "ProtocolError",
    "CommandError",
    "McpError",
]
