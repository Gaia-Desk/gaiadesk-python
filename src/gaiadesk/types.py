"""The JSON gaiadesk-cli prints with ``--json``, field for field (TypedDicts).

Every shape of the CLI's own is GENERATED from its JSON Schema
(``gaiadesk-cli schema --json``) into ``types_generated.py`` and re-exported
here: ``ExecResult``, ``ExecExit``, ``ExecEvent``, ``Error``,
``ErrorEnvelope``, ``Job``, ``JobList``, ``TokenList``, ``AuditLog``,
``VersionInfo``, ... The names this SDK used before are aliases of them
(``JobInfo`` is ``Job``, ``CpSummary`` is ``CopyResult``, ...). Only the
SDK's own shapes are written by hand below.

Results are plain dicts; these types are for editors and type checkers.
Before Python 3.11 this module needs ``typing_extensions`` (a dependency of
the package there); the client itself does not import it.
"""

from __future__ import annotations

from typing import Any, Dict, List

from typing import TypedDict

from .types_generated import *  # noqa: F401,F403
from .types_generated import (
    CopyFailure,
    CopyResult,
    Device,
    DeviceList,
    ExecEvent,
    Job,
    Measurement,
)

# ── The SDK's earlier names for the CLI's shapes ──

DeviceRow = Device
"""One row of ``devices --json``."""
DevicesResult = DeviceList
"""``devices --json``."""
CpFailure = CopyFailure
CpSummary = CopyResult
"""``cp --json``."""
JobInfo = Job
"""A background job (``run``, ``kill``, an entry of ``ps``)."""
# ``stats()`` returns ``StatsReport``; ``DeskStats`` (generated) is the same without ``desk``.
MeasureResult = Measurement
"""``measure --json``."""


# ── The SDK's own shapes ──


ExecStreamEvent = ExecEvent
"""One line of ``exec --json-stream`` (``note`` never reaches the CLI's stdout)."""


class TokenCreateResult(TypedDict, total=False):
    """``token create --json``: ``tokens`` (each with its ``secret``), or with
    ``out`` the entries without it and the ``file`` they were written to.
    (The ``--out`` form is not in the CLI's schema.)"""

    tokens: List[Dict[str, Any]]
    file: str
