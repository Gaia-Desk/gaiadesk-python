"""gaiadesk: drive GaiaDesk desks from Python, through GaiaDesk's native
library when it is installed (``pip install gaiadesk[native]``), else through
gaiadesk-cli. ``GaiaDesk(...).backend`` says which.

    from gaiadesk import GaiaDesk
    gd = GaiaDesk(token_file="~/.config/gaiadesk/bot.token")
    r = gd.exec("123456789", "hostname")
    print(r["exit"], r["stdout"])

Given ``api_key``, it drives desks through GaiaDesk's hosted API instead
(standard library only): ``GaiaDesk(api_key="ak_...", desk_token="gdagt_...")``.
The same operations from a desk itself: ``GaiaDesk(transport="local")`` (code
running on the desk) or ``GaiaDesk(transport="lan", base_url=..., fingerprint=...,
desk_token=...)`` (a desk's LAN gateway, its certificate pinned).

``AsyncGaiaDesk`` is the asyncio twin.
"""

from .aio import AsyncForward, AsyncGaiaDesk
from .client import Forward, GaiaDesk
from ._api import API_FILE_LIMIT, DEFAULT_API_URL, ApiStream, AsyncApiStream
from ._core import TRANSPORTS, Completed, locate_cli
from ._local import (
    FingerprintMismatchError,
    certificate_fingerprint,
    default_local_address,
    local_api_dir,
    local_pipe_name,
    local_socket_path,
    local_token_path,
    normalize_fingerprint,
    pipe_user,
)
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
    "ApiStream",
    "AsyncApiStream",
    "DEFAULT_API_URL",
    "API_FILE_LIMIT",
    "TRANSPORTS",
    "normalize_fingerprint",
    "certificate_fingerprint",
    "local_api_dir",
    "local_socket_path",
    "local_token_path",
    "local_pipe_name",
    "pipe_user",
    "default_local_address",
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
    "FingerprintMismatchError",
    "ConnectionLostError",
    "OperationFailedError",
    "ProtocolError",
    "CommandError",
    "McpError",
]
