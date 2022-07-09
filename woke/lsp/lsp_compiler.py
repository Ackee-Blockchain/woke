from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections import deque
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    Dict,
    Iterable,
    List,
    Mapping,
    NoReturn,
    Set,
    Tuple,
    Union,
)

from woke.compile.exceptions import CompilationError

if TYPE_CHECKING:
    from .server import LspServer

import networkx as nx
from intervaltree import IntervalTree

from woke.ast.ir.meta.source_unit import SourceUnit
from woke.ast.ir.reference_resolver import CallbackParams, ReferenceResolver
from woke.ast.ir.utils import IrInitTuple
from woke.ast.nodes import AstSolc
from woke.compile import SolcOutput, SolcOutputSelectionEnum
from woke.compile.compilation_unit import CompilationUnit
from woke.compile.compiler import SolidityCompiler
from woke.compile.solc_frontend import SolcOutputError, SolcOutputErrorSeverityEnum
from woke.config import WokeConfig
from woke.lsp.document_sync import (
    DidChangeTextDocumentParams,
    DidCloseTextDocumentParams,
    DidOpenTextDocumentParams,
)
from woke.lsp.utils.uri import uri_to_path

from ..svm import SolcVersionManager
from .common_structures import MessageType, Position, Range

logger = logging.getLogger(__name__)


ENCODING = "utf-16-le"


def _binary_search(lines: List[Tuple[bytes, int]], x: int) -> int:
    l = 0
    r = len(lines)

    while l < r:
        mid = l + (r - l) // 2
        if lines[mid][1] < x + 1:
            l = mid + 1
        else:
            r = mid

    return l - 1


def _out_edge_bfs(graph: nx.DiGraph, start: Iterable[Path], out: Set[Path]) -> None:
    queue = deque(start)
    out.update(start)

    while len(queue):
        node = queue.pop()
        for out_edge in graph.out_edges(node):
            from_, to = out_edge
            if to not in out:
                out.add(to)
                queue.append(to)


class LspCompiler:
    __config: WokeConfig
    __svm: SolcVersionManager
    __server: LspServer
    __file_changes_queue: asyncio.Queue
    __diagnostic_queue: asyncio.Queue
    __discovered_files: Set[Path]

    # accessed from the compilation thread
    # full path -> contents
    __files: Dict[Path, str]
    __modified_files: Set[Path]
    __force_compile_files: Set[Path]
    __compiler: SolidityCompiler
    __output_contents: Dict[Path, str]
    __interval_trees: Dict[Path, IntervalTree]
    __source_units: Dict[Path, SourceUnit]
    __line_indexes: Dict[Path, List[Tuple[bytes, int]]]

    __ir_reference_resolver: ReferenceResolver

    __output_ready: asyncio.Event

    def __init__(self, server: LspServer, diagnostic_queue: asyncio.Queue):
        self.__server = server
        self.__file_changes_queue = asyncio.Queue()
        self.__diagnostic_queue = diagnostic_queue
        self.__stop_event = threading.Event()
        self.__discovered_files = set()
        self.__files = dict()
        self.__modified_files = set()
        self.__force_compile_files = set()
        self.__interval_trees = {}
        self.__source_units = {}
        self.__line_indexes = {}
        self.__output_contents = dict()
        self.__output_ready = asyncio.Event()

        self.__ir_reference_resolver = ReferenceResolver()

    async def run(self, config: WokeConfig):
        self.__config = config
        self.__svm = SolcVersionManager(config)
        self.__compiler = SolidityCompiler(config)
        await self.__compilation_loop()

    @property
    def output_ready(self) -> asyncio.Event:
        return self.__output_ready

    @property
    def ir_reference_resolver(self) -> ReferenceResolver:
        return self.__ir_reference_resolver

    @property
    def interval_trees(self) -> Dict[Path, IntervalTree]:
        return self.__interval_trees

    @property
    def source_units(self) -> Dict[Path, SourceUnit]:
        return self.__source_units

    async def add_change(
        self,
        change: Union[
            DidOpenTextDocumentParams,
            DidChangeTextDocumentParams,
            DidCloseTextDocumentParams,
        ],
    ) -> None:
        if not isinstance(
            change, (DidOpenTextDocumentParams, DidCloseTextDocumentParams)
        ):
            self.output_ready.clear()

        await self.__file_changes_queue.put(change)

    async def force_recompile(self) -> None:
        self.__output_ready.clear()
        await self.__file_changes_queue.put(None)

    def get_file_content(self, file: Union[Path, str]) -> str:
        if isinstance(file, str):
            file = uri_to_path(file)
        if file not in self.__output_contents:
            self.__output_contents[file] = file.read_text(encoding="utf-8")
        return self.__output_contents[file]

    def get_line_pos_from_byte_offset(
        self, file: Path, byte_offset: int
    ) -> Tuple[int, int]:
        if file not in self.__line_indexes:
            self.__setup_line_index(file)

        encoded_lines = self.__line_indexes[file]
        line_num = _binary_search(encoded_lines, byte_offset)
        line_data, prefix_sum = encoded_lines[line_num]
        line_offset = byte_offset - prefix_sum
        return line_num, line_offset

    def get_range_from_byte_offsets(
        self, file: Path, byte_offsets: Tuple[int, int]
    ) -> Range:
        start_line, start_column = self.get_line_pos_from_byte_offset(
            file, byte_offsets[0]
        )
        end_line, end_column = self.get_line_pos_from_byte_offset(file, byte_offsets[1])

        return Range(
            start=Position(line=start_line, character=start_column),
            end=Position(line=end_line, character=end_column),
        )

    def get_byte_offset_from_line_pos(self, file: Path, line: int, col: int) -> int:
        if file not in self.__line_indexes:
            self.__setup_line_index(file)

        encoded_lines = self.__line_indexes[file]
        line_bytes, prefix = encoded_lines[line]
        line_offset = len(line_bytes.decode("utf-8")[:col].encode("utf-8"))
        return prefix + line_offset

    def _handle_change(
        self,
        change: Union[
            DidOpenTextDocumentParams,
            DidCloseTextDocumentParams,
            DidChangeTextDocumentParams,
            None,
        ],
    ) -> None:
        if change is None:
            self.__force_compile_files.update(self.__discovered_files)
        elif isinstance(change, DidOpenTextDocumentParams):
            path = uri_to_path(change.text_document.uri).resolve()
            self.__files[path] = change.text_document.text
        elif isinstance(change, DidCloseTextDocumentParams):
            path = uri_to_path(change.text_document.uri).resolve()
        elif isinstance(change, DidChangeTextDocumentParams):
            path = uri_to_path(change.text_document.uri).resolve()
            self.__modified_files.add(path)

            for content_change in change.content_changes:
                start = content_change.range.start
                end = content_change.range.end

                # str.splitlines() removes empty lines => cannot be used
                # str.split() removes separators => cannot be used
                tmp_lines = re.split(r"(\r?\n)", self.__files[path])
                tmp_lines2: List[str] = []
                for line in tmp_lines:
                    if line in {"\r\n", "\n"}:
                        tmp_lines2[-1] += line
                    else:
                        tmp_lines2.append(line)

                lines: List[bytearray] = [
                    bytearray(line.encode(ENCODING)) for line in tmp_lines2
                ]

                if start.line == end.line:
                    line = lines[start.line]
                    line[start.character * 2 : end.character * 2] = b""
                    line[
                        start.character * 2 : start.character * 2
                    ] = content_change.text.encode(ENCODING)
                else:
                    start_line = lines[start.line]
                    end_line = lines[end.line]
                    start_line[start.character * 2 :] = content_change.text.encode(
                        ENCODING
                    )
                    end_line[: end.character * 2] = b""

                    for i in range(start.line + 1, end.line):
                        lines[i] = bytearray(b"")

                self.__files[path] = "".join(line.decode(ENCODING) for line in lines)
        else:
            raise Exception("Unknown change type")

    async def __compile(
        self,
        files: AbstractSet[Path],
        modified_files: Mapping[Path, str],
        full_compile: bool = True,
    ) -> None:
        if full_compile:
            graph = self.__compiler.build_graph(
                self.__discovered_files, modified_files, True
            )
        else:
            graph = self.__compiler.build_graph(files, modified_files, True)

        try:
            compilation_units = self.__compiler.build_compilation_units_maximize(graph)
        except CompilationError as e:
            await self.__server.log_message(str(e), MessageType.ERROR)
            return

        # filter out only compilation units that need to be compiled
        compilation_units = [
            cu for cu in compilation_units if cu.files & (files | modified_files.keys())
        ]
        build_settings = self.__compiler.create_build_settings(
            [SolcOutputSelectionEnum.AST]
        )

        target_versions = []
        for compilation_unit in compilation_units:
            target_version = self.__config.compiler.solc.target_version
            if (
                target_version is not None
                and target_version not in compilation_unit.versions
            ):
                files_str = "\n".join(str(path) for path in compilation_unit.files)
                message = (
                    f"Unable to compile the following files with solc version `{target_version}` set in config files:\n"
                    + files_str
                )
                await self.__server.log_message(message, MessageType.ERROR)
                return
            else:
                # use the latest matching version
                try:
                    target_version = next(
                        version
                        for version in reversed(self.__svm.list_all())
                        if version in compilation_unit.versions
                    )
                except StopIteration:
                    message = (
                        f"Unable to find a matching solc version for the following files:\n"
                        + "\n".join(str(path) for path in compilation_unit.files)
                    )
                    await self.__server.log_message(message, MessageType.ERROR)
                    return
            target_versions.append(target_version)

            if not self.__svm.get_path(target_version).is_file():
                progress_token = await self.__server.progress_begin(
                    "Downloading", f"solc {target_version}", 0
                )
                if progress_token is not None:

                    async def on_progress(downloaded: int, total: int) -> None:
                        assert progress_token is not None
                        await self.__server.progress_report(
                            progress_token,
                            f"solc {target_version}",
                            (100 * downloaded) // total,
                        )

                    await self.__svm.install(target_version, progress=on_progress)
                    await self.__server.progress_end(progress_token)
                else:
                    await self.__svm.install(target_version)

        tasks = []
        for compilation_unit, target_version in zip(compilation_units, target_versions):
            task = asyncio.create_task(
                self.__compiler.compile_unit_raw(
                    compilation_unit,
                    target_version,
                    build_settings,
                )
            )
            tasks.append(task)

        # wait for compilation of all compilation units
        try:
            ret = await asyncio.gather(*tasks)
        except Exception as e:
            for task in tasks:
                task.cancel()
            await self.__server.log_message(str(e), MessageType.ERROR)
            return

        self.__output_contents.update(self.__files)

        errors_per_file: Dict[Path, List[SolcOutputError]] = {}
        files_to_recompile = set(files)
        modified_files_to_recompile = dict(modified_files)
        processed_files: Set[Path] = set()

        for cu, solc_output in zip(compilation_units, ret):
            for file in cu.files:
                errors_per_file[file] = []
                if file in self.__line_indexes:
                    self.__line_indexes.pop(file)

            errored_files: Set[Path] = set()

            for error in solc_output.errors:
                if error.source_location is not None:
                    path = cu.source_unit_name_to_path(error.source_location.file)
                    errors_per_file[path].append(error)

                    if error.severity == SolcOutputErrorSeverityEnum.ERROR:
                        errored_files.add(path)

            _out_edge_bfs(cu.graph, errored_files, errored_files)

            # modified files and files that import modified files (even indirectly)
            recompiled_files: Set[Path] = set()
            _out_edge_bfs(cu.graph, modified_files, recompiled_files)

            for file in errored_files:
                files_to_recompile.discard(file)
                modified_files_to_recompile.pop(file, None)
                # an error occurred during compilation
                # AST still may be provided, but it must NOT be parsed (pydantic model is not defined for this case)
                if file in self.__source_units:
                    self.__ir_reference_resolver.run_destroy_callbacks(file)
                    self.__source_units.pop(file)
                if file in self.__interval_trees:
                    self.__interval_trees.pop(file)

            for source_unit_name, raw_ast in solc_output.sources.items():
                path = cu.source_unit_name_to_path(source_unit_name)
                if path in errored_files or raw_ast.ast is None:
                    continue
                ast = AstSolc.parse_obj(raw_ast.ast)

                self.__ir_reference_resolver.index_nodes(ast, path, cu.blake2b_digest)

                files_to_recompile.discard(path)
                modified_files_to_recompile.pop(path, None)
                if (
                    path in self.__source_units and path not in recompiled_files
                ) or path in processed_files:
                    continue
                processed_files.add(path)

                interval_tree = IntervalTree()
                init = IrInitTuple(
                    path,
                    self.get_file_content(path).encode("utf-8"),
                    cu,
                    interval_tree,
                    self.__ir_reference_resolver,
                )
                self.__ir_reference_resolver.run_destroy_callbacks(path)
                self.__source_units[path] = SourceUnit(init, ast)
                self.__interval_trees[path] = interval_tree

            self.__ir_reference_resolver.run_post_process_callbacks(
                CallbackParams(source_units=self.__source_units)
            )
        for path, errors in errors_per_file.items():
            await self.__diagnostic_queue.put((path, errors))

        if len(files_to_recompile) > 0 or len(modified_files_to_recompile) > 0:
            # avoid infinite recursion
            if (
                files_to_recompile != files
                or modified_files.keys() != modified_files_to_recompile.keys()
                or full_compile
            ):
                await self.__compile(
                    files_to_recompile, modified_files_to_recompile, False
                )

    async def __compilation_loop(self):
        # perform Solidity files discovery
        project_path = self.__config.project_root_path

        for file in project_path.rglob("**/*.sol"):
            if (
                not ({"node_modules", ".woke-build"} & set(file.parts))
                and file.is_file()
            ):
                self.__discovered_files.add(file.resolve())

        # perform initial compilation
        await self.__compile(self.__discovered_files, {})
        self.output_ready.set()

        while True:
            change = await self.__file_changes_queue.get()
            while True:
                self._handle_change(change)
                try:
                    change = self.__file_changes_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            # run the compilation
            if len(self.__force_compile_files) > 0 or len(self.__modified_files) > 0:
                modified_files = {
                    path: self.__files[path] for path in self.__modified_files
                }
                await self.__compile(self.__force_compile_files, modified_files)

                self.__force_compile_files.clear()
                self.__modified_files.clear()

                if self.__file_changes_queue.empty():
                    self.output_ready.set()

    def __setup_line_index(self, file: Path):
        content = self.get_file_content(file)
        tmp_lines = re.split(r"(\r?\n)", content)
        lines: List[str] = []
        for line in tmp_lines:
            if line in {"\r\n", "\n"}:
                lines[-1] += line
            else:
                lines.append(line)

        # UTF-8 encoded lines with prefix length
        encoded_lines: List[Tuple[bytes, int]] = []
        prefix_sum = 0
        for line in lines:
            encoded_line = line.encode("utf-8")
            encoded_lines.append((encoded_line, prefix_sum))
            prefix_sum += len(encoded_line)
        self.__line_indexes[file] = encoded_lines
