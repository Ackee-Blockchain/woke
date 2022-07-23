import logging
from typing import Any, List, Optional, Union

from woke.ast.enums import ContractKind
from woke.ast.ir.abc import IrAbc
from woke.ast.ir.declaration.abc import DeclarationAbc
from woke.ast.ir.declaration.contract_definition import ContractDefinition
from woke.ast.ir.declaration.function_definition import FunctionDefinition
from woke.ast.ir.declaration.modifier_definition import ModifierDefinition
from woke.ast.ir.declaration.variable_declaration import VariableDeclaration
from woke.ast.ir.expression.identifier import Identifier
from woke.ast.ir.expression.member_access import MemberAccess
from woke.ast.ir.meta.identifier_path import IdentifierPath
from woke.ast.ir.statement.inline_assembly import InlineAssembly
from woke.ast.ir.type_name.user_defined_type_name import UserDefinedTypeName
from woke.ast.nodes import AstNodeId
from woke.lsp.common_structures import (
    DocumentUri,
    PartialResultParams,
    Position,
    Range,
    StaticRegistrationOptions,
    SymbolKind,
    SymbolTag,
    TextDocumentPositionParams,
    TextDocumentRegistrationOptions,
    WorkDoneProgressOptions,
    WorkDoneProgressParams,
)
from woke.lsp.context import LspContext
from woke.lsp.lsp_data_model import LspModel
from woke.lsp.utils import path_to_uri, position_within_range, uri_to_path

logger = logging.getLogger(__name__)


class TypeHierarchyOptions(WorkDoneProgressOptions):
    pass


class TypeHierarchyRegistrationOptions(
    TextDocumentRegistrationOptions, TypeHierarchyOptions, StaticRegistrationOptions
):
    pass


class TypeHierarchyPrepareParams(TextDocumentPositionParams, WorkDoneProgressParams):
    pass


class TypeHierarchyItemData(LspModel):
    ast_node_id: int
    cu_hash: str
    uri: DocumentUri


class TypeHierarchyItem(LspModel):
    name: str
    """
    The name of this item.
    """
    kind: SymbolKind
    """
    The kind of this item.
    """
    tags: Optional[List[SymbolTag]]
    """
    Tags for this item.
    """
    detail: Optional[str]
    """
    More detail for this item, e.g. the signature of a function.
    """
    uri: DocumentUri
    """
    The resource identifier of this item.
    """
    range: Range
    """
    The range enclosing this symbol not including leading/trailing whitespace
    but everything else, e.g. comments and code.
    """
    selection_range: Range
    """
    The range that should be selected and revealed when this symbol is being
    picked, e.g. the name of a function. Muse be contained by the
    [`range`](#TypeHierarchyItem.range).
    """
    data: TypeHierarchyItemData
    """
    A data entry field that is preserved between a type hierarchy prepare and
    supertypes or subtypes requests. It could also be used to identify the
    type hierarchy in the server, helping improve the performance on
    resolving supertypes and subtypes.
    """


class TypeHierarchySupertypesParams(WorkDoneProgressParams, PartialResultParams):
    item: TypeHierarchyItem


class TypeHierarchySubtypesParams(WorkDoneProgressParams, PartialResultParams):
    item: TypeHierarchyItem


def _get_node_symbol_kind(
    node: Union[
        ContractDefinition, FunctionDefinition, ModifierDefinition, VariableDeclaration
    ]
) -> SymbolKind:
    if isinstance(node, ContractDefinition):
        if node.kind == ContractKind.CONTRACT:
            return SymbolKind.CLASS
        elif node.kind == ContractKind.INTERFACE:
            return SymbolKind.INTERFACE
        elif node.kind == ContractKind.LIBRARY:
            return SymbolKind.MODULE
        else:
            assert False, f"Unknown contract kind {node.kind}"
    elif isinstance(node, FunctionDefinition):
        if isinstance(node.parent, ContractDefinition):
            return SymbolKind.METHOD
        else:
            return SymbolKind.FUNCTION
    elif isinstance(node, ModifierDefinition):
        return SymbolKind.FUNCTION
    elif isinstance(node, VariableDeclaration):
        return SymbolKind.VARIABLE
    else:
        assert False, f"Unknown node type {type(node)}"


async def prepare_type_hierarchy(
    context: LspContext, params: TypeHierarchyPrepareParams
) -> Union[List[TypeHierarchyItem], None]:
    logger.debug(
        f"Type hierarchy for file {params.text_document.uri} at position {params.position} requested"
    )
    await context.compiler.output_ready.wait()

    path = uri_to_path(params.text_document.uri).resolve()

    if path not in context.compiler.interval_trees:
        return None

    tree = context.compiler.interval_trees[path]

    byte_offset = context.compiler.get_byte_offset_from_line_pos(
        path, params.position.line, params.position.character
    )
    intervals = tree.at(byte_offset)
    nodes: List[IrAbc] = [interval.data for interval in intervals]
    logger.debug(
        f"Found {len(nodes)} nodes at byte offset {byte_offset}:\n{sorted(nodes, key=lambda x: x.ast_tree_depth)}"
    )
    if len(nodes) == 0:
        return None

    node = max(nodes, key=lambda n: n.ast_tree_depth)
    logger.debug(f"Found node {node}")

    if isinstance(node, DeclarationAbc):
        name_location_range = context.compiler.get_range_from_byte_offsets(
            node.file, node.name_location
        )
        if not position_within_range(params.position, name_location_range):
            return None

    if isinstance(node, (Identifier, MemberAccess)):
        node = node.referenced_declaration
    elif isinstance(node, (IdentifierPath, UserDefinedTypeName)):
        part = node.identifier_path_part_at(byte_offset)
        if part is None:
            return None
        node = part.referenced_declaration
    elif isinstance(node, InlineAssembly):
        external_reference = node.external_reference_at(byte_offset)
        if external_reference is None:
            return None
        node = external_reference.referenced_declaration

    if isinstance(
        node, (ContractDefinition, FunctionDefinition, ModifierDefinition)
    ) or (isinstance(node, VariableDeclaration) and node.overrides is not None):
        return [
            TypeHierarchyItem(
                name=node.canonical_name,
                kind=_get_node_symbol_kind(node),
                tags=None,
                detail=None,
                uri=DocumentUri(path_to_uri(node.file)),
                range=context.compiler.get_range_from_byte_offsets(
                    node.file, node.byte_location
                ),
                selection_range=context.compiler.get_range_from_byte_offsets(
                    node.file, node.name_location
                ),
                data=TypeHierarchyItemData(
                    ast_node_id=node.ast_node_id,
                    cu_hash=node.cu_hash.hex(),
                    uri=DocumentUri(path_to_uri(node.file)),
                ),
            )
        ]

    return None


async def supertypes(
    context: LspContext, params: TypeHierarchySupertypesParams
) -> Union[List[TypeHierarchyItem], None]:
    logger.debug(f"Supertypes for {params.item} requested")
    ast_node_id = params.item.data.ast_node_id
    cu_hash = bytes.fromhex(params.item.data.cu_hash)
    node = context.compiler.ir_reference_resolver.resolve_node(
        AstNodeId(ast_node_id), cu_hash
    )
    assert isinstance(
        node,
        (
            ContractDefinition,
            FunctionDefinition,
            ModifierDefinition,
            VariableDeclaration,
        ),
    )

    type_items = []
    nodes: List[
        Union[
            ContractDefinition,
            FunctionDefinition,
            ModifierDefinition,
            VariableDeclaration,
        ]
    ] = []
    if isinstance(node, ContractDefinition):
        for base_contract in node.base_contracts:
            contract = base_contract.base_name.referenced_declaration
            assert isinstance(contract, ContractDefinition)
            nodes.append(contract)
    elif (
        isinstance(node, (FunctionDefinition, VariableDeclaration))
        and node.base_functions is not None
    ):
        for base_function in node.base_functions:
            nodes.append(base_function)
    elif isinstance(node, ModifierDefinition) and node.base_modifiers is not None:
        for base_modifier in node.base_modifiers:
            nodes.append(base_modifier)

    for node in nodes:
        type_items.append(
            TypeHierarchyItem(
                name=node.canonical_name,
                kind=_get_node_symbol_kind(node),
                tags=None,
                detail=None,
                uri=DocumentUri(path_to_uri(node.file)),
                range=context.compiler.get_range_from_byte_offsets(
                    node.file, node.byte_location
                ),
                selection_range=context.compiler.get_range_from_byte_offsets(
                    node.file, node.name_location
                ),
                data=TypeHierarchyItemData(
                    ast_node_id=node.ast_node_id,
                    cu_hash=node.cu_hash.hex(),
                    uri=DocumentUri(path_to_uri(node.file)),
                ),
            )
        )
    return type_items


async def subtypes(
    context: LspContext, params: TypeHierarchySubtypesParams
) -> Union[List[TypeHierarchyItem], None]:
    logger.debug(f"Subtypes for {params.item} requested")
    ast_node_id = params.item.data.ast_node_id
    cu_hash = bytes.fromhex(params.item.data.cu_hash)
    node = context.compiler.ir_reference_resolver.resolve_node(
        AstNodeId(ast_node_id), cu_hash
    )
    assert isinstance(
        node,
        (
            ContractDefinition,
            FunctionDefinition,
            ModifierDefinition,
            VariableDeclaration,
        ),
    )

    type_items = []
    nodes: List[
        Union[
            ContractDefinition,
            FunctionDefinition,
            ModifierDefinition,
            VariableDeclaration,
        ]
    ] = []
    if isinstance(node, ContractDefinition):
        for child_contract in node.child_contracts:
            nodes.append(child_contract)
    elif isinstance(node, FunctionDefinition) and node.child_functions:
        for child_function in node.child_functions:
            nodes.append(child_function)
    elif isinstance(node, ModifierDefinition):
        for child_modifier in node.child_modifiers:
            nodes.append(child_modifier)

    for node in nodes:
        type_items.append(
            TypeHierarchyItem(
                name=node.canonical_name,
                kind=_get_node_symbol_kind(node),
                tags=None,
                detail=None,
                uri=DocumentUri(path_to_uri(node.file)),
                range=context.compiler.get_range_from_byte_offsets(
                    node.file, node.byte_location
                ),
                selection_range=context.compiler.get_range_from_byte_offsets(
                    node.file, node.name_location
                ),
                data=TypeHierarchyItemData(
                    ast_node_id=node.ast_node_id,
                    cu_hash=node.cu_hash.hex(),
                    uri=DocumentUri(path_to_uri(node.file)),
                ),
            )
        )
    return type_items
