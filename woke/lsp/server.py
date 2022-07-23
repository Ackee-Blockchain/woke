import asyncio
import logging
import traceback
import uuid
from copy import deepcopy
from pathlib import Path
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    NoReturn,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
)

from pydantic.error_wrappers import ValidationError

from ..config import WokeConfig
from .common_structures import (
    ConfigurationItem,
    ConfigurationParams,
    CreateFilesParams,
    DeleteFilesParams,
    DidChangeConfigurationParams,
    DocumentFilter,
    InitializedParams,
    InitializeError,
    InitializeParams,
    LogMessageParams,
    LogTraceParams,
    MessageType,
    ProgressParams,
    RenameFilesParams,
    SetTraceParams,
    ShowMessageParams,
    WorkDoneProgressBegin,
    WorkDoneProgressCreateParams,
    WorkDoneProgressEnd,
    WorkDoneProgressReport,
)
from .context import LspContext
from .document_sync import (
    DidChangeTextDocumentParams,
    DidCloseTextDocumentParams,
    DidOpenTextDocumentParams,
    DidSaveTextDocumentParams,
    TextDocumentSyncKind,
    TextDocumentSyncOptions,
    WillSaveTextDocumentParams,
)
from .exceptions import LspError
from .features.code_lens import CodeLensOptions, CodeLensParams, code_lens
from .features.definition import DefinitionParams, definition
from .features.diagnostic import diagnostics_loop
from .features.document_link import (
    DocumentLinkOptions,
    DocumentLinkParams,
    document_link,
)
from .features.document_symbol import DocumentSymbolParams, document_symbol
from .features.implementation import ImplementationParams, implementation
from .features.references import ReferenceParams, references
from .features.rename import (
    PrepareRenameParams,
    RenameOptions,
    RenameParams,
    prepare_rename,
    rename,
)
from .features.type_definition import TypeDefinitionParams, type_definition
from .features.type_hierarchy import (
    TypeHierarchyPrepareParams,
    TypeHierarchySubtypesParams,
    TypeHierarchySupertypesParams,
    prepare_type_hierarchy,
    subtypes,
    supertypes,
)
from .lsp_data_model import LspModel
from .methods import RequestMethodEnum
from .protocol_structures import (
    CancelParams,
    ErrorCodes,
    NotificationMessage,
    RequestMessage,
    ResponseError,
    ResponseMessage,
)
from .rpc_protocol import RpcProtocol
from .server_capabilities import (
    FileOperationFilter,
    FileOperationPattern,
    FileOperationPatternKind,
    FileOperationRegistrationOptions,
    InitializeResult,
    PositionEncodingKind,
    ServerCapabilities,
    ServerCapabilitiesWorkspace,
    ServerCapabilitiesWorkspaceFileOperations,
    WorkspaceFoldersServerCapabilities,
)
from .utils import is_relative_to
from .utils.uri import uri_to_path

logger = logging.getLogger(__name__)

ConfigPath = Tuple[Union[str, int], ...]


class LspServer:
    __initialized: bool
    __cli_config: WokeConfig
    __workspaces: Dict[Path, LspContext]
    __user_config: Optional[WokeConfig]
    __main_workspace: Optional[LspContext]
    __workspace_path: Optional[Path]
    __protocol: RpcProtocol
    __run: bool
    __request_id_counter: int
    __sent_requests: Dict[Union[int, str], asyncio.Event]
    __message_responses: Dict[Union[int, str], ResponseMessage]
    __running_tasks: Set[asyncio.Task]

    __method_mapping: Dict[str, Tuple[Callable, Optional[Type[LspModel]]]]
    __notification_mapping: Dict[str, Tuple[Callable, Optional[Type[LspModel]]]]

    def __init__(
        self,
        config: WokeConfig,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        self.__initialized = False
        self.__cli_config = config
        self.__workspaces = {}
        self.__user_config = None
        self.__main_workspace = None
        self.__workspace_path = None
        self.__protocol = RpcProtocol(reader, writer)
        self.__run = True
        self.__request_id_counter = 0
        self.__sent_requests = {}
        self.__message_responses = {}
        self.__running_tasks = set()

        self.__method_mapping = {
            RequestMethodEnum.INITIALIZE: (self._initialize, InitializeParams),
            RequestMethodEnum.SHUTDOWN: (self._shutdown, None),
            RequestMethodEnum.DOCUMENT_LINK: (
                self._workspace_route,
                DocumentLinkParams,
            ),
            RequestMethodEnum.PREPARE_TYPE_HIERARCHY: (
                self._workspace_route,
                TypeHierarchyPrepareParams,
            ),
            RequestMethodEnum.TYPE_HIERARCHY_SUPERTYPES: (
                self._workspace_route,
                TypeHierarchySupertypesParams,
            ),
            RequestMethodEnum.TYPE_HIERARCHY_SUBTYPES: (
                self._workspace_route,
                TypeHierarchySubtypesParams,
            ),
            RequestMethodEnum.REFERENCES: (self._workspace_route, ReferenceParams),
            RequestMethodEnum.DOCUMENT_SYMBOL: (
                self._workspace_route,
                DocumentSymbolParams,
            ),
            RequestMethodEnum.DEFINITION: (self._workspace_route, DefinitionParams),
            RequestMethodEnum.TYPE_DEFINITION: (
                self._workspace_route,
                TypeDefinitionParams,
            ),
            RequestMethodEnum.IMPLEMENTATION: (
                self._workspace_route,
                ImplementationParams,
            ),
            RequestMethodEnum.CODE_LENS: (self._workspace_route, CodeLensParams),
            RequestMethodEnum.PREPARE_RENAME: (
                self._workspace_route,
                PrepareRenameParams,
            ),
            RequestMethodEnum.RENAME: (self._workspace_route, RenameParams),
        }

        self.__notification_mapping = {
            RequestMethodEnum.INITIALIZED: (self._initialized, InitializedParams),
            RequestMethodEnum.CANCEL_REQUEST: (self._cancel_request, CancelParams),
            RequestMethodEnum.PROGRESS: (self._progress, ProgressParams),
            RequestMethodEnum.LOG_TRACE: (self._log_trace, LogTraceParams),
            RequestMethodEnum.SET_TRACE: (self._set_trace, SetTraceParams),
            RequestMethodEnum.TEXT_DOCUMENT_DID_OPEN: (
                self._text_document_did_open,
                DidOpenTextDocumentParams,
            ),
            RequestMethodEnum.TEXT_DOCUMENT_DID_CHANGE: (
                self._workspace_route,
                DidChangeTextDocumentParams,
            ),
            RequestMethodEnum.TEXT_DOCUMENT_WILL_SAVE: (
                self._workspace_route,
                WillSaveTextDocumentParams,
            ),
            RequestMethodEnum.TEXT_DOCUMENT_DID_SAVE: (
                self._workspace_route,
                DidSaveTextDocumentParams,
            ),
            RequestMethodEnum.TEXT_DOCUMENT_DID_CLOSE: (
                self._workspace_route,
                DidCloseTextDocumentParams,
            ),
            RequestMethodEnum.WORKSPACE_DID_CHANGE_CONFIGURATION: (
                self._workspace_did_change_configuration,
                DidChangeConfigurationParams,
            ),
            RequestMethodEnum.WORKSPACE_DID_CREATE_FILES: (
                self._workspace_did_create_files,
                CreateFilesParams,
            ),
            RequestMethodEnum.WORKSPACE_DID_RENAME_FILES: (
                self._workspace_did_rename_files,
                RenameFilesParams,
            ),
            RequestMethodEnum.WORKSPACE_DID_DELETE_FILES: (
                self._workspace_did_delete_files,
                DeleteFilesParams,
            ),
        }

    def _task_done_callback(self, task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:

            def _callback(task: asyncio.Task) -> None:
                for t in self.__running_tasks:
                    t.cancel()

            logger.exception(e)
            try:
                t = asyncio.create_task(
                    self.log_message(traceback.format_exc(), MessageType.ERROR)
                )
                t.add_done_callback(_callback)
            except Exception as e:
                logger.exception(e)

                for task in self.__running_tasks:
                    task.cancel()
        finally:
            self.__running_tasks.remove(task)

    def create_task(self, coroutine: Coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self.__running_tasks.add(task)
        task.add_done_callback(self._task_done_callback)
        return task

    async def run(self) -> None:
        task = self.create_task(self._main_task())
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _main_task(self) -> None:
        messages_queue = asyncio.Queue()
        self.create_task(self._messages_loop(messages_queue))

        try:
            while self.__run:
                message = await self.__protocol.receive()
                if isinstance(message, ResponseMessage):
                    await self._handle_response(message)
                else:
                    await messages_queue.put(message)
        except ConnectionError:
            pass

        for task in self.__running_tasks:
            task.cancel()

    async def _messages_loop(self, queue: asyncio.Queue) -> NoReturn:
        while True:
            message = await queue.get()
            if isinstance(message, RequestMessage):
                await self._handle_message(message)
            elif isinstance(message, NotificationMessage):
                await self._handle_notification(message)
            else:
                raise Exception("Unknown message type")

    async def send_request(self, method: RequestMethodEnum, params: Any = None) -> Any:
        request = RequestMessage(
            jsonrpc="2.0", id=self.__request_id_counter, method=method, params=params
        )
        self.__sent_requests[request.id] = asyncio.Event()
        self.__request_id_counter += 1

        logger.debug(f"Sending request:\n{request}")
        await self.__protocol.send(request)
        await self.__sent_requests[request.id].wait()
        self.__sent_requests.pop(request.id)
        response = self.__message_responses.pop(request.id)

        if response.error is not None:
            raise LspError(
                response.error.code, response.error.message, response.error.data
            )
        return response.result

    async def send_notification(
        self, method: str, params: Optional[Any] = None
    ) -> None:
        notification = NotificationMessage(
            jsonrpc="2.0",
            method=method,
            params=params,
        )
        logger.debug(f"Sending notification:\n{notification}")
        await self.__protocol.send(notification)

    async def log_message(self, message: str, type: MessageType) -> None:
        params = LogMessageParams(
            type=type,
            message=message,
        )
        await self.send_notification(RequestMethodEnum.WINDOW_LOG_MESSAGE, params)

    async def show_message(self, message: str, type: MessageType) -> None:
        params = ShowMessageParams(
            type=type,
            message=message,
        )
        await self.send_notification(RequestMethodEnum.WINDOW_SHOW_MESSAGE, params)

    async def progress_begin(
        self,
        title: str,
        message: Optional[str] = None,
        percentage: Optional[int] = None,
        cancellable: Optional[bool] = None,
    ) -> Optional[str]:
        token = str(uuid.uuid4())
        params = WorkDoneProgressCreateParams(token=token)
        try:
            await self.send_request(
                RequestMethodEnum.WINDOW_WORK_DONE_PROGRESS_CREATE, params
            )
        except LspError:
            return None

        params = ProgressParams(
            token=token,
            value=WorkDoneProgressBegin(
                kind="begin",
                title=title,
                message=message,
                percentage=percentage,
                cancellable=cancellable,
            ),
        )
        await self.send_notification(RequestMethodEnum.PROGRESS, params)
        return token

    async def progress_report(
        self,
        token: str,
        message: Optional[str] = None,
        percentage: Optional[int] = None,
        cancellable: Optional[bool] = None,
    ) -> None:
        params = ProgressParams(
            token=token,
            value=WorkDoneProgressReport(
                kind="report",
                message=message,
                percentage=percentage,
                cancellable=cancellable,
            ),
        )
        await self.send_notification(RequestMethodEnum.PROGRESS, params)

    async def progress_end(self, token: str, message: Optional[str] = None) -> None:
        params = ProgressParams(
            token=token,
            value=WorkDoneProgressEnd(
                kind="end",
                message=message,
            ),
        )
        await self.send_notification(RequestMethodEnum.PROGRESS, params)

    async def _handle_message(self, request: RequestMessage) -> None:
        logger.info(f"Message received: {request}")

        # Init before request needed
        if request.method != RequestMethodEnum.INITIALIZE and not self.__initialized:
            response = self._serve_error(
                request,
                ErrorCodes.ServerNotInitialized,
                "Server has not been initialized",
            )
            await self.__protocol.send(response)
            return

        # Handling request
        try:
            response = await self._serve_response(request)
        except LspError as e:
            response = self._serve_error(request, e.code, e.message)
        await self.__protocol.send(response)

    async def _handle_notification(self, notification: NotificationMessage) -> None:
        logger.info(f"Notification received: {notification}")

        if not self.__initialized and notification.method != RequestMethodEnum.EXIT:
            return

        try:
            n, params_type = self.__notification_mapping[notification.method]
        except KeyError:
            logger.error(
                f"Incoming notification type '{notification.method}' not implemented."
            )
            raise NotImplementedError()

        if params_type is not None:
            await n(params_type.parse_obj(notification.params))
        else:
            await n(None)

    async def _handle_response(self, response: ResponseMessage) -> None:
        logger.info(f"Response received: {response}")

        if response.id is None:
            logger.error(f"Response without id: {response}")
            return

        try:
            self.__message_responses[response.id] = response
            self.__sent_requests[response.id].set()
        except KeyError:
            logger.error(
                f"Received response with id {response.id} but no such request was sent."
            )

    async def _serve_response(self, request: RequestMessage) -> ResponseMessage:
        try:
            m, params_type = self.__method_mapping[request.method]
        except KeyError:
            logger.error(f"Incoming method type '{request.method}' not implemented.")
            raise NotImplementedError()

        if params_type is not None:
            response = await m(params_type.parse_obj(request.params))
        else:
            response = await m(None)

        response_message = ResponseMessage(
            jsonrpc="2.0", id=request.id, result=response, error=None
        )
        logger.info(f"Serving response: {response_message}")
        return response_message

    @staticmethod
    def _serve_error(
        request: RequestMessage, error_code: int, msg: str
    ) -> ResponseMessage:
        response_error = ResponseError(code=error_code, message=msg, data=None)
        response_message = ResponseMessage(
            jsonrpc="2.0", id=request.id, error=response_error, result=None
        )
        logger.warning(f"Serving error response: {response_message}")
        return response_message

    async def _initialize(self, params: InitializeParams) -> InitializeResult:
        if self.__initialized:
            raise LspError(ErrorCodes.InvalidRequest, "Server already initialized")

        if params.workspace_folders is not None:
            if len(params.workspace_folders) != 1:
                raise LspError(
                    ErrorCodes.RequestFailed,
                    "Multi-root workspaces are not supported.",
                    InitializeError(retry=False),
                )
            path = uri_to_path(params.workspace_folders[0].uri).resolve(strict=True)
        elif params.root_uri is not None:
            path = uri_to_path(params.root_uri).resolve(strict=True)
        elif params.root_path is not None:
            path = Path(params.root_path).resolve(strict=True)
        else:
            path = None

        self.__initialized = True
        self.__workspace_path = path

        solidity_registration = FileOperationRegistrationOptions(
            filters=[
                FileOperationFilter(
                    pattern=FileOperationPattern(
                        glob="**/*.sol",
                        matches=FileOperationPatternKind.FILE,
                    )
                )
            ]
        )

        server_capabilities = ServerCapabilities(
            position_encoding=PositionEncodingKind.UTF16,
            text_document_sync=TextDocumentSyncOptions(
                open_close=True, change=TextDocumentSyncKind.INCREMENTAL
            ),
            document_link_provider=DocumentLinkOptions(
                resolve_provider=False,
            ),
            type_hierarchy_provider=True,
            references_provider=True,
            document_symbol_provider=True,
            workspace=ServerCapabilitiesWorkspace(
                workspace_folders=WorkspaceFoldersServerCapabilities(
                    supported=False,
                ),
                file_operations=ServerCapabilitiesWorkspaceFileOperations(
                    did_create=solidity_registration,
                    did_rename=solidity_registration,
                    did_delete=solidity_registration,
                ),
            ),
            definition_provider=True,
            type_definition_provider=True,
            implementation_provider=True,
            code_lens_provider=CodeLensOptions(
                resolve_provider=False,
            ),
            rename_provider=RenameOptions(
                prepare_provider=True,
            ),
        )
        return InitializeResult(capabilities=server_capabilities, server_info=None)

    async def _cancel_request(self, params: CancelParams) -> None:
        pass

    async def _progress(self, params: ProgressParams) -> None:
        pass

    async def _log_trace(self, params: LogTraceParams) -> None:
        pass

    async def _set_trace(self, params: SetTraceParams) -> None:
        pass

    async def _shutdown(self, params: Any) -> None:
        self.__run = False

    async def _parse_config(
        self, raw_config: dict, workspace_path: Path
    ) -> Tuple[dict, Set[ConfigPath], Set[ConfigPath]]:
        removed_options: Set[ConfigPath] = set()

        def _normalize_config(config: Union[dict, list], config_path: ConfigPath):
            if isinstance(config, dict):
                for k in list(config):
                    v = config[k]
                    if isinstance(v, (dict, list)):
                        _normalize_config(v, config_path + (k,))
                    elif isinstance(v, str) and len(v.strip()) == 0:
                        del config[k]
                        removed_options.add(config_path + (k,))
            else:
                for no, item in enumerate(config):
                    if isinstance(item, (dict, list)):
                        _normalize_config(item, config_path + (no,))
                    elif isinstance(item, str) and len(item.strip()) == 0:
                        config.remove(item)
                        removed_options.add(config_path + (no,))

        _normalize_config(raw_config, tuple())

        run = True
        invalid_options: Set[ConfigPath] = set()
        while run:
            try:
                WokeConfig.fromdict(
                    raw_config,
                    project_root_path=workspace_path,
                    woke_root_path=self.__cli_config.woke_root_path,
                )
                run = False
            except ValidationError as e:
                for error in e.errors():
                    invalid_options.add(error["loc"])
                    invalid_option = raw_config
                    for segment in error["loc"][:-1]:
                        invalid_option = invalid_option[segment]

                    if isinstance(invalid_option, list):
                        invalid_option.remove(error["loc"][-1])
                    elif isinstance(invalid_option, dict):
                        del invalid_option[error["loc"][-1]]
                    else:
                        raise NotImplementedError()
        if len(invalid_options) > 0:
            message = (
                "Failed to parse the following config options, using defaults:\n"
                + "\n".join(
                    f"    woke -> {' -> '.join(str(segment) for segment in option)}"
                    for option in invalid_options
                )
            )
            await self.log_message(message, MessageType.WARNING)

        return raw_config, invalid_options, removed_options

    async def _create_config(self, workspace_path: Path) -> WokeConfig:
        code_config = await self.get_configuration()
        assert isinstance(code_config, list)
        assert len(code_config) == 1
        assert isinstance(code_config[0], dict)
        raw_config = code_config[0]

        raw_config, _, _ = await self._parse_config(raw_config, workspace_path)

        return WokeConfig.fromdict(
            raw_config,
            project_root_path=workspace_path,
            woke_root_path=self.__cli_config.woke_root_path,
        )

    async def _handle_config_change(self, raw_config: dict) -> None:
        for context in self.__workspaces.values():
            raw_config_copy = deepcopy(raw_config)
            (
                raw_config_copy,
                invalid_options,
                removed_options,
            ) = await self._parse_config(
                raw_config_copy, context.config.project_root_path
            )

            changed = context.config.update(
                raw_config_copy, invalid_options.union(removed_options)
            )
            if changed:
                await context.compiler.force_recompile()

    async def _initialized(self, params: InitializedParams) -> None:
        if self.__workspace_path is not None:
            self.__main_workspace = LspContext(
                self, await self._create_config(self.__workspace_path)
            )
            self.__workspaces[self.__workspace_path] = self.__main_workspace
            self.__main_workspace.run(True)

    async def _workspace_did_change_configuration(
        self, params: DidChangeConfigurationParams
    ) -> None:
        logger.debug(f"Received configuration change: {params}")
        if "woke" in params.settings:
            await self._handle_config_change(params.settings["woke"])

    async def _workspace_route(self, params: Any) -> Any:
        if isinstance(
            params, (TypeHierarchySupertypesParams, TypeHierarchySubtypesParams)
        ):
            uri = params.item.data.uri
        else:
            uri = params.text_document.uri
        path = uri_to_path(uri)
        matching_workspaces = [
            workspace
            for workspace in self.__workspaces.values()
            if is_relative_to(path, workspace.config.project_root_path)
        ]
        assert len(matching_workspaces) == 1
        context = matching_workspaces[0]

        if isinstance(params, DocumentLinkParams):
            return await document_link(context, params)
        elif isinstance(params, TypeHierarchyPrepareParams):
            return await prepare_type_hierarchy(context, params)
        elif isinstance(params, TypeHierarchySupertypesParams):
            return await supertypes(context, params)
        elif isinstance(params, TypeHierarchySubtypesParams):
            return await subtypes(context, params)
        elif isinstance(params, ReferenceParams):
            return await references(context, params)
        elif isinstance(params, DocumentSymbolParams):
            return await document_symbol(context, params)
        elif isinstance(params, DefinitionParams):
            return await definition(context, params)
        elif isinstance(params, TypeDefinitionParams):
            return await type_definition(context, params)
        elif isinstance(params, ImplementationParams):
            return await implementation(context, params)
        elif isinstance(params, CodeLensParams):
            return await code_lens(context, params)
        elif isinstance(params, PrepareRenameParams):
            return await prepare_rename(context, params)
        elif isinstance(params, RenameParams):
            return await rename(context, params)
        elif isinstance(params, DidChangeTextDocumentParams):
            return await self._text_document_did_change(context, params)
        elif isinstance(params, WillSaveTextDocumentParams):
            return await self._text_document_will_save(context, params)
        elif isinstance(params, DidSaveTextDocumentParams):
            return await self._text_document_did_save(context, params)
        elif isinstance(params, DidCloseTextDocumentParams):
            return await self._text_document_did_close(context, params)
        else:
            raise NotImplementedError(f"Unhandled request: {type(params)}")

    async def _text_document_did_open(self, params: DidOpenTextDocumentParams) -> None:
        path = uri_to_path(params.text_document.uri)
        matching_workspaces = [
            workspace
            for workspace in self.__workspaces.values()
            if is_relative_to(path, workspace.config.project_root_path)
        ]
        assert len(matching_workspaces) <= 1

        if len(matching_workspaces) == 0:
            context = LspContext(self, await self._create_config(path.parent))
            self.__workspaces[path.parent] = context
            context.run(False)
        else:
            context = matching_workspaces[0]

        await context.compiler.add_change(params)

    @staticmethod
    async def _text_document_did_change(
        context: LspContext, params: DidChangeTextDocumentParams
    ) -> None:
        await context.compiler.add_change(params)

    @staticmethod
    async def _text_document_will_save(
        context: LspContext, params: WillSaveTextDocumentParams
    ) -> None:
        pass

    @staticmethod
    async def _text_document_did_save(
        context: LspContext, params: DidSaveTextDocumentParams
    ) -> None:
        pass

    async def _text_document_did_close(
        self, context: LspContext, params: DidCloseTextDocumentParams
    ) -> None:
        await context.compiler.add_change(params)

        if context != self.__main_workspace:
            path = uri_to_path(params.text_document.uri)
            await context.diagnostics_queue.put((path, set()))
        # TODO: remove the workspace from the dict of workspaces (if not main workspace and no other files are open)

    async def _workspace_did_create_files(self, params: CreateFilesParams) -> None:
        assert self.__main_workspace is not None
        await self.__main_workspace.compiler.add_change(params)

    async def _workspace_did_rename_files(self, params: RenameFilesParams) -> None:
        assert self.__main_workspace is not None
        await self.__main_workspace.compiler.add_change(params)

    async def _workspace_did_delete_files(self, params: DeleteFilesParams) -> None:
        assert self.__main_workspace is not None
        await self.__main_workspace.compiler.add_change(params)

    async def get_configuration(self) -> None:
        params = ConfigurationParams(
            items=[
                ConfigurationItem(
                    section="woke",
                )
            ]
        )
        return await self.send_request(
            RequestMethodEnum.WORKSPACE_CONFIGURATION, params
        )
