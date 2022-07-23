from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from woke.lsp.context import LspContext
    from woke.lsp.server import LspServer

from woke.compile.solc_frontend import SolcOutputError, SolcOutputErrorSeverityEnum
from woke.lsp.common_structures import (
    Diagnostic,
    DiagnosticSeverity,
    DocumentUri,
    Position,
    Range,
)
from woke.lsp.lsp_data_model import LspModel
from woke.lsp.methods import RequestMethodEnum
from woke.lsp.utils.uri import path_to_uri

logger = logging.getLogger(__name__)


class PublishDiagnosticsParams(LspModel):
    uri: DocumentUri
    """
    The URI for which diagnostic information is reported.
    """
    version: Optional[int] = None
    """
    Optiona the version number of the document the diagnostics are published
    for.
    
    @since 3.15.0
    """
    diagnostics: List[Diagnostic]
    """
    An array of diagnostic information items.
    """


async def diagnostics_loop(server: LspServer, context: LspContext):
    queue: asyncio.Queue = context.diagnostics_queue
    while True:
        file, diagnostics = await queue.get()

        params = PublishDiagnosticsParams(
            uri=DocumentUri(path_to_uri(file)),
            diagnostics=list(diagnostics),
        )
        await server.send_notification(RequestMethodEnum.PUBLISH_DIAGNOSTICS, params)
