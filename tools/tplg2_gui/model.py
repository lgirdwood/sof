# SPDX-License-Identifier: BSD-3-Clause
"""Data models for SOF Topology2 AST, Classes, Graph Nodes/Edges, and Attributes."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


@dataclass
class AttrConstraint:
    """Value constraints for a Class DefineAttribute."""
    valid_values: List[str] = field(default_factory=list)
    tuple_values: List[str] = field(default_factory=list)


@dataclass
class AttrDef:
    """Definition of a Class attribute from DefineAttribute and attributes {} block."""
    name: str
    attr_type: str = "string"
    token_ref: str = ""
    constraints: Optional[AttrConstraint] = None
    is_constructor: bool = False
    is_mandatory: bool = False
    is_immutable: bool = False
    is_deprecated: bool = False
    is_unique: bool = False
    description: str = ""

    @property
    def flags_summary(self) -> str:
        flags = []
        if self.is_constructor:
            flags.append("constructor")
        if self.is_mandatory:
            flags.append("mandatory")
        if self.is_immutable:
            flags.append("immutable")
        if self.is_unique:
            flags.append("unique")
        if self.is_deprecated:
            flags.append("deprecated")
        if self.constraints and self.constraints.valid_values:
            vals = ", ".join(self.constraints.valid_values[:4])
            if len(self.constraints.valid_values) > 4:
                vals += ", ..."
            flags.append(f"[{vals}]")
        return " | ".join(flags)


@dataclass
class ConfigNode:
    """Hierarchical configuration tree node representing attributes and sub-objects."""
    key: str
    value: Any = None
    node_type: str = "scalar"  # "scalar", "object", "array", "section", "define"
    category_info: str = ""
    is_immutable: bool = False
    is_mandatory: bool = False
    is_constructor: bool = False
    valid_values: List[str] = field(default_factory=list)
    origin: str = ""  # "default", "pipeline", "instance", "define"
    children: List["ConfigNode"] = field(default_factory=list)

    def get_child(self, key: str) -> Optional["ConfigNode"]:
        for ch in self.children:
            if ch.key == key:
                return ch
        return None

    def get_scalar(self, key: str, default: Any = None) -> Any:
        ch = self.get_child(key)
        if ch is not None and ch.node_type == "scalar":
            return ch.value
        return default

    def set_scalar(
        self,
        key: str,
        value: Any,
        category_info: str = "",
        origin: str = "instance",
        is_immutable: bool = False,
        is_mandatory: bool = False,
        is_constructor: bool = False,
        valid_values: Optional[List[str]] = None,
    ) -> "ConfigNode":
        ch = self.get_child(key)
        if ch is None:
            ch = ConfigNode(
                key=key,
                value=str(value),
                node_type="scalar",
                category_info=category_info,
                origin=origin,
                is_immutable=is_immutable,
                is_mandatory=is_mandatory,
                is_constructor=is_constructor,
                valid_values=list(valid_values) if valid_values else [],
            )
            self.children.append(ch)
        else:
            ch.value = str(value)
            if category_info:
                ch.category_info = category_info
            if origin:
                ch.origin = origin
            if valid_values:
                ch.valid_values = list(valid_values)
        return ch

    def clone(self) -> "ConfigNode":
        return ConfigNode(
            key=self.key,
            value=self.value,
            node_type=self.node_type,
            category_info=self.category_info,
            is_immutable=self.is_immutable,
            is_mandatory=self.is_mandatory,
            is_constructor=self.is_constructor,
            valid_values=list(self.valid_values),
            origin=self.origin,
            children=[c.clone() for c in self.children],
        )


@dataclass
class TplgClassDef:
    """Represents a Topology2 Class.<category>.<name> definition."""
    category: str  # "Base", "Widget", "Pipeline", "Dai", "PCM", "Control", etc.
    name: str
    source_file: str
    rel_path: str
    description: str = ""
    attributes: Dict[str, AttrDef] = field(default_factory=dict)
    defaults: Dict[str, Any] = field(default_factory=dict)
    constructor_attrs: List[str] = field(default_factory=list)
    unique_attr: str = "instance"
    extends_class: Optional[str] = None
    widgets: List[Dict[str, Any]] = field(default_factory=list)
    routes: List[Dict[str, str]] = field(default_factory=list)
    controls: List[Dict[str, Any]] = field(default_factory=list)
    sub_objects: List[ConfigNode] = field(default_factory=list)
    config_tree: ConfigNode = field(default_factory=lambda: ConfigNode("root", node_type="object"))

    @property
    def full_name(self) -> str:
        return f"Class.{self.category}.{self.name}"


@dataclass
class GraphPort:
    """Input or output connection pin on a GraphNode."""
    id: str
    name: str
    direction: str  # "in" or "out"
    port_type: str = "audio"  # "audio", "control", "host", "dai"
    rel_x: float = 0.0
    rel_y: float = 0.0

    def clone(self) -> "GraphPort":
        return GraphPort(
            id=self.id,
            name=self.name,
            direction=self.direction,
            port_type=self.port_type,
            rel_x=self.rel_x,
            rel_y=self.rel_y,
        )


@dataclass
class GraphNode:
    """Visual and logical node in the Topology2 graph."""
    id: str
    title: str
    subtitle: str
    category: str  # "host", "widget", "mix", "dai", "pcm", "control", "base", "pipeline"
    class_category: str = "Widget"
    class_name: str = ""
    instance_id: str = "1"
    pipeline_id: Optional[int] = None
    pipeline_class: Optional[str] = None
    parent_node_id: Optional[str] = None
    x: float = 0.0
    y: float = 0.0
    width: float = 190.0
    height: float = 86.0
    in_ports: List[GraphPort] = field(default_factory=list)
    out_ports: List[GraphPort] = field(default_factory=list)
    in_fmt_badge: str = ""
    out_fmt_badge: str = ""
    badges: List[str] = field(default_factory=list)
    config: ConfigNode = field(default_factory=lambda: ConfigNode("attributes", node_type="object"))
    class_def: Optional[TplgClassDef] = None
    source_file: str = ""

    def clone(self) -> "GraphNode":
        return GraphNode(
            id=self.id,
            title=self.title,
            subtitle=self.subtitle,
            category=self.category,
            class_category=self.class_category,
            class_name=self.class_name,
            instance_id=self.instance_id,
            pipeline_id=self.pipeline_id,
            pipeline_class=self.pipeline_class,
            parent_node_id=self.parent_node_id,
            x=self.x,
            y=self.y,
            width=self.width,
            height=self.height,
            in_ports=[p.clone() for p in self.in_ports],
            out_ports=[p.clone() for p in self.out_ports],
            in_fmt_badge=self.in_fmt_badge,
            out_fmt_badge=self.out_fmt_badge,
            badges=list(self.badges),
            config=self.config.clone(),
            class_def=self.class_def,
            source_file=self.source_file,
        )


@dataclass
class GraphEdge:
    """Directed route connecting source_node -> sink_node."""
    id: str
    source_node: str
    sink_node: str
    source_port: str = "out0"
    sink_port: str = "in0"
    control: str = ""
    pipeline_id: Optional[int] = None
    is_inter_pipeline: bool = False
    config: ConfigNode = field(default_factory=lambda: ConfigNode("route", node_type="object"))

    def clone(self) -> "GraphEdge":
        return GraphEdge(
            id=self.id,
            source_node=self.source_node,
            sink_node=self.sink_node,
            source_port=self.source_port,
            sink_port=self.sink_port,
            control=self.control,
            pipeline_id=self.pipeline_id,
            is_inter_pipeline=self.is_inter_pipeline,
            config=self.config.clone(),
        )


@dataclass
class PipelineContainer:
    """Visual container grouping nodes that belong to the same pipeline index."""
    pipeline_id: int
    class_name: str
    direction: str = "playback"
    x: float = 0.0
    y: float = 0.0
    width: float = 300.0
    height: float = 160.0
    enabled_by: str = ""
    warning_text: str = ""
    config: ConfigNode = field(default_factory=lambda: ConfigNode("pipeline", node_type="object"))

    def clone(self) -> "PipelineContainer":
        return PipelineContainer(
            pipeline_id=self.pipeline_id,
            class_name=self.class_name,
            direction=self.direction,
            x=self.x,
            y=self.y,
            width=self.width,
            height=self.height,
            enabled_by=self.enabled_by,
            warning_text=self.warning_text,
            config=self.config.clone(),
        )


@dataclass
class CMakeTargetVariant:
    """A concrete topology build target from production/ or development/ CMake files."""
    input_conf: str
    output_name: str
    defines_str: str
    defines: Dict[str, str] = field(default_factory=dict)
    cmake_file: str = ""
    category: str = "production"  # "production" or "development"


@dataclass
class MachineTopologyEntry:
    """Represents a top-level .conf topology file and its CMake target variants."""
    name: str
    conf_path: str
    rel_path: str
    variants: List[CMakeTargetVariant] = field(default_factory=list)


@dataclass
class GraphViewModel:
    """Complete graph model currently displayed in the center canvas."""
    title: str
    subtitle: str = ""
    view_kind: str = "topology"  # "base_class", "module", "pipeline", "topology"
    source_file: str = ""
    defines: Dict[str, str] = field(default_factory=dict)
    nodes: Dict[str, GraphNode] = field(default_factory=dict)
    edges: List[GraphEdge] = field(default_factory=list)
    containers: Dict[int, PipelineContainer] = field(default_factory=dict)
    root_config: ConfigNode = field(default_factory=lambda: ConfigNode("root", node_type="object"))
    dirty: bool = False
    target_variant: Optional[CMakeTargetVariant] = None

    def clone(self) -> "GraphViewModel":
        return GraphViewModel(
            title=self.title,
            subtitle=self.subtitle,
            view_kind=self.view_kind,
            source_file=self.source_file,
            defines=dict(self.defines),
            nodes={nid: n.clone() for nid, n in self.nodes.items()},
            edges=[e.clone() for e in self.edges],
            containers={pid: c.clone() for pid, c in self.containers.items()},
            root_config=self.root_config.clone(),
            dirty=self.dirty,
            target_variant=self.target_variant,
        )


@dataclass
class GraphHistoryEntry:
    """Snapshot of a GraphViewModel and selection state for Undo/Redo."""
    description: str
    vm_snapshot: GraphViewModel
    selected_node_id: Optional[str] = None
    selected_node_ids: Set[str] = field(default_factory=set)
    selected_edge_id: Optional[str] = None
    selected_container_id: Optional[int] = None
    selected_container_ids: Set[int] = field(default_factory=set)


class UndoRedoManager:
    """Bounded Undo/Redo stack managing GraphHistoryEntry snapshots."""

    def __init__(
        self,
        max_depth: int = 100,
        on_history_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        self.max_depth = max_depth
        self.on_history_changed = on_history_changed
        self.undo_stack: List[GraphHistoryEntry] = []
        self.redo_stack: List[GraphHistoryEntry] = []

    def clear(self) -> None:
        self.undo_stack.clear()
        self.redo_stack.clear()
        if self.on_history_changed:
            self.on_history_changed()

    @property
    def can_undo(self) -> bool:
        return bool(self.undo_stack)

    @property
    def can_redo(self) -> bool:
        return bool(self.redo_stack)

    @property
    def undo_description(self) -> Optional[str]:
        return self.undo_stack[-1].description if self.undo_stack else None

    @property
    def redo_description(self) -> Optional[str]:
        return self.redo_stack[-1].description if self.redo_stack else None

    def push_undo_entry(self, entry: GraphHistoryEntry) -> None:
        self.undo_stack.append(entry)
        if len(self.undo_stack) > self.max_depth:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
        if self.on_history_changed:
            self.on_history_changed()

    def undo(self, current_entry: GraphHistoryEntry) -> Optional[Tuple[GraphHistoryEntry, str]]:
        if not self.undo_stack:
            return None
        prev_entry = self.undo_stack.pop()
        action_desc = prev_entry.description
        current_entry.description = action_desc
        self.redo_stack.append(current_entry)
        if self.on_history_changed:
            self.on_history_changed()
        return (prev_entry, action_desc)

    def redo(self, current_entry: GraphHistoryEntry) -> Optional[Tuple[GraphHistoryEntry, str]]:
        if not self.redo_stack:
            return None
        next_entry = self.redo_stack.pop()
        action_desc = next_entry.description
        current_entry.description = action_desc
        self.undo_stack.append(current_entry)
        if self.on_history_changed:
            self.on_history_changed()
        return (next_entry, action_desc)
