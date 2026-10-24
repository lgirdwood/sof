# SPDX-License-Identifier: BSD-3-Clause
"""Data models for SOF Topology2 AST, Classes, Graph Nodes/Edges, and Attributes."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


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
    config: ConfigNode = field(default_factory=lambda: ConfigNode("pipeline", node_type="object"))


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
