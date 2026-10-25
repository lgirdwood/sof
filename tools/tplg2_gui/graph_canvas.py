# SPDX-License-Identifier: BSD-3-Clause
"""Hardware-accelerated GTK4 Graph Canvas using Gtk.Snapshot and Gsk.PathBuilder."""

from __future__ import annotations
import math
import os
import subprocess
from typing import Callable, Dict, List, Optional, Set, Tuple

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango

from .layout import (
    CONTAINER_GAP_X,
    CTRL_H_GAP,
    CTRL_TOP_GAP,
    H_GAP,
    _assign_edge_ports,
    _compute_node_geometry,
    _is_node_capture,
    _position_testbench_io_nodes,
    layout_graph,
    update_container_bounds_from_nodes,
)
from .model import (
    ConfigNode,
    GraphEdge,
    GraphHistoryEntry,
    GraphNode,
    GraphPort,
    GraphViewModel,
    PipelineContainer,
    UndoRedoManager,
)
from .parser import Topology2Workspace


def rgba(r: float, g: float, b: float, a: float = 1.0) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.red = r
    c.green = g
    c.blue = b
    c.alpha = a
    return c


# Color palette by node category
CATEGORY_COLORS: Dict[str, Tuple[Gdk.RGBA, Gdk.RGBA]] = {
    "host": (rgba(0.14, 0.42, 0.78, 1.0), rgba(0.11, 0.16, 0.24, 0.96)),
    "widget": (rgba(0.12, 0.56, 0.52, 1.0), rgba(0.11, 0.19, 0.20, 0.96)),
    "mix": (rgba(0.78, 0.43, 0.12, 1.0), rgba(0.22, 0.16, 0.11, 0.96)),
    "dai": (rgba(0.20, 0.62, 0.28, 1.0), rgba(0.11, 0.20, 0.14, 0.96)),
    "pcm": (rgba(0.45, 0.32, 0.78, 1.0), rgba(0.16, 0.13, 0.24, 0.96)),
    "control": (rgba(0.64, 0.30, 0.56, 1.0), rgba(0.20, 0.13, 0.19, 0.96)),
    "control_mixer": (rgba(0.62, 0.26, 0.88, 1.0), rgba(0.18, 0.12, 0.25, 0.96)),
    "control_switch": (rgba(0.86, 0.56, 0.12, 1.0), rgba(0.24, 0.18, 0.10, 0.96)),
    "control_bytes": (rgba(0.12, 0.66, 0.84, 1.0), rgba(0.10, 0.19, 0.24, 0.96)),
    "control_enum": (rgba(0.86, 0.24, 0.42, 1.0), rgba(0.24, 0.11, 0.16, 0.96)),
    "base": (rgba(0.36, 0.44, 0.54, 1.0), rgba(0.14, 0.16, 0.20, 0.96)),
    "pipeline": (rgba(0.25, 0.48, 0.72, 1.0), rgba(0.12, 0.17, 0.23, 0.96)),
    "testbench_io": (rgba(0.15, 0.62, 0.42, 1.0), rgba(0.11, 0.18, 0.16, 0.97)),
}

BG_COLOR = rgba(0.08, 0.09, 0.12, 1.0)
GRID_COLOR = rgba(0.16, 0.18, 0.23, 1.0)
CONTAINER_FILL_PB = rgba(0.14, 0.24, 0.38, 0.22)
CONTAINER_FILL_CAP = rgba(0.14, 0.36, 0.26, 0.22)
CONTAINER_BORDER_PB = rgba(0.28, 0.52, 0.82, 0.48)
CONTAINER_BORDER_CAP = rgba(0.28, 0.72, 0.48, 0.48)
NODE_BORDER_NORMAL = rgba(0.30, 0.35, 0.44, 0.90)
NODE_BORDER_SELECTED = rgba(0.35, 0.78, 1.00, 1.00)
NODE_SHADOW_SELECTED = rgba(0.20, 0.68, 1.00, 0.45)
TEXT_WHITE = rgba(0.95, 0.97, 1.00, 1.0)
TEXT_MUTED = rgba(0.68, 0.74, 0.82, 1.0)
BADGE_BG = rgba(0.18, 0.22, 0.30, 0.95)
BADGE_FG = rgba(0.78, 0.88, 1.00, 1.0)
PORT_IN_COLOR = rgba(0.32, 0.82, 0.96, 1.0)
PORT_OUT_COLOR = rgba(0.98, 0.68, 0.26, 1.0)
PORT_HOVER_COLOR = rgba(1.0, 0.95, 0.40, 1.0)
EDGE_INTRA_COLOR = rgba(0.52, 0.78, 0.96, 0.92)
EDGE_INTER_COLOR = rgba(0.98, 0.68, 0.28, 0.95)
EDGE_SELECTED_COLOR = rgba(0.30, 0.95, 0.65, 1.0)
WIRE_DRAG_COLOR = rgba(1.0, 0.82, 0.32, 0.95)
DRAG_PAUSE_DELAY_MS = 220


class DropOutcomePreview:
    """Visual preview of the outcome if the currently dragged object is dropped at (sx, sy)."""

    def __init__(
        self,
        action_kind: str,  # "join_pipeline" | "create_pipeline" | "reposition"
        headline: str,
        detail: str,
        sx: float,
        sy: float,
        wx: float,
        wy: float,
        target_pid: Optional[int] = None,
        ghost_container: Optional[Tuple[float, float, float, float, str, bool]] = None,
        ghost_node: Optional[Tuple[float, float, float, float, str, str]] = None,
        ghost_wires: Optional[List[Tuple[float, float, float, float, bool]]] = None,
        spliced_edge_id: Optional[str] = None,
    ) -> None:
        self.action_kind = action_kind
        self.headline = headline
        self.detail = detail
        self.sx = sx
        self.sy = sy
        self.wx = wx
        self.wy = wy
        self.target_pid = target_pid
        self.ghost_container = ghost_container
        self.ghost_node = ghost_node
        self.ghost_wires = ghost_wires or []
        self.spliced_edge_id = spliced_edge_id


class TopologyGraphCanvas(Gtk.Widget):
    """Custom GTK4 Widget rendering interactive Topology2 graphs via Gtk.Snapshot."""

    __gtype_name__ = "TopologyGraphCanvas"
    _active_drag_payload: Optional[str] = None

    def __init__(
        self,
        on_selection_changed: Optional[Callable[[Optional[str], Optional[ConfigNode], str], None]] = None,
        on_graph_modified: Optional[Callable[[], None]] = None,
        on_history_changed: Optional[Callable[[], None]] = None,
    ):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_focusable(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)

        self.on_selection_changed = on_selection_changed
        self.on_graph_modified = on_graph_modified
        self.history = UndoRedoManager(on_history_changed=on_history_changed)
        self._suppress_auto_history: bool = False
        self._pending_drag_history_entry: Optional[GraphHistoryEntry] = None

        self.workspace: Optional[Topology2Workspace] = None
        self.vm: Optional[GraphViewModel] = None

        # Camera transform
        self.pan_x: float = 24.0
        self.pan_y: float = 24.0
        self.zoom: float = 1.0
        self._is_fitted: bool = True
        self._last_canvas_size: Tuple[int, int] = (0, 0)
        self._pointer_pos: Optional[Tuple[float, float]] = None

        # Selection state
        self.selected_node_id: Optional[str] = None
        self.selected_node_ids: Set[str] = set()
        self.selected_edge_id: Optional[str] = None
        self.selected_container_id: Optional[int] = None
        self.selected_container_ids: Set[int] = set()
        self.hovered_port: Optional[Tuple[str, str, str]] = None  # (node_id, port_id, direction)
        self.hovered_warning_pid: Optional[int] = None

        # Interaction state
        self._drag_mode: str = "none"  # "none", "pan", "node", "container", "wire"
        self._right_drag_mode: str = "none"  # "none", "pan", "objects"
        self._drag_start_screen: Tuple[float, float] = (0.0, 0.0)
        self._drag_start_world: Tuple[float, float] = (0.0, 0.0)
        self._drag_start_pan: Tuple[float, float] = (0.0, 0.0)
        self._drag_node_initial: Tuple[float, float] = (0.0, 0.0)
        self._drag_container_id: Optional[int] = None
        self._drag_container_initial: Tuple[float, float] = (0.0, 0.0)
        self._drag_children_initial: Dict[str, Tuple[float, float]] = {}
        self._right_drag_nodes_initial: Dict[str, Tuple[float, float]] = {}
        self._right_drag_containers_initial: Dict[int, Tuple[float, float]] = {}
        self._wire_source: Optional[Tuple[str, str, float, float]] = None  # (node_id, port_id, wx, wy)
        self._wire_cursor_world: Tuple[float, float] = (0.0, 0.0)

        # Drag-and-drop & drag-pause outcome preview state
        self._dnd_payload: Optional[str] = None
        self._dnd_screen_pos: Optional[Tuple[float, float]] = None
        self._drag_pause_timer_id: Optional[int] = None
        self.drop_preview: Optional[DropOutcomePreview] = None

        # Compilation diagnostic highlight state (set when double-clicking an error/warning)
        self.diag_highlight_node_id: Optional[str] = None
        self.diag_highlight_container_id: Optional[int] = None
        self.diag_highlight_edge_id: Optional[str] = None
        self.diag_banner_text: Optional[str] = None
        self.diag_banner_severity: str = "error"

        # Pango layout cache: key -> Pango.Layout
        self._layout_cache: Dict[Tuple[str, int, bool], Pango.Layout] = {}

        self._setup_controllers()

    def _setup_controllers(self) -> None:
        # Primary left-click / drag gesture
        click = Gtk.GestureClick.new()
        click.set_button(1)
        click.connect("pressed", self._on_left_pressed)
        self.add_controller(click)

        drag = Gtk.GestureDrag.new()
        drag.set_button(1)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

        # Middle button (button 2) pan gesture
        mid_drag = Gtk.GestureDrag.new()
        mid_drag.set_button(2)
        mid_drag.connect("drag-begin", self._on_pan_begin)
        mid_drag.connect("drag-update", self._on_pan_update)
        mid_drag.connect("drag-end", self._on_pan_end)
        self.add_controller(mid_drag)

        # Right button (button 3) click + drag gesture:
        # Changes pointer cursor on hold and drags selected object(s), or pans whole view if none selected
        right_click = Gtk.GestureClick.new()
        right_click.set_button(3)
        right_click.connect("pressed", self._on_right_pressed)
        right_click.connect("released", self._on_right_released)
        right_click.connect("stopped", lambda _g: self.set_cursor(None))
        self.add_controller(right_click)

        right_drag = Gtk.GestureDrag.new()
        right_drag.set_button(3)
        right_drag.connect("drag-begin", self._on_right_drag_begin)
        right_drag.connect("drag-update", self._on_right_drag_update)
        right_drag.connect("drag-end", self._on_right_drag_end)
        self.add_controller(right_drag)

        # Motion controller for cursor tracking (pointer-centered zoom) & port hover highlight
        motion = Gtk.EventControllerMotion.new()
        motion.connect("enter", self._on_motion_enter)
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)

        # Scroll controller for smooth cursor-centered zoom
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        # Key controller for Delete, Fit (F), Auto-layout (L)
        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_ctrl)

        # DropTarget for dragging Processing Modules or Pipelines from the Left Pane
        drop_target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.COPY)
        drop_target.set_preload(True)
        drop_target.connect("enter", self._on_dnd_enter)
        drop_target.connect("motion", self._on_dnd_motion)
        drop_target.connect("leave", self._on_dnd_leave)
        drop_target.connect("drop", self._on_drop_item)
        self.add_controller(drop_target)

    def _has_selected_objects(self) -> bool:
        if not self.vm:
            return False
        if self.selected_node_id and self.selected_node_id in self.vm.nodes:
            return True
        if any(nid in self.vm.nodes for nid in self.selected_node_ids):
            return True
        if self.selected_container_id is not None and self.selected_container_id in self.vm.containers:
            return True
        if any(pid in self.vm.containers for pid in self.selected_container_ids):
            return True
        return False

    def clear_diagnostic_highlights(self) -> None:
        self.diag_highlight_node_id = None
        self.diag_highlight_container_id = None
        self.diag_highlight_edge_id = None
        self.diag_banner_text = None
        self.queue_draw()

    def set_graph(self, vm: GraphViewModel, workspace: Optional[Topology2Workspace] = None) -> None:
        """Load a new GraphViewModel, auto-layout its nodes, and trigger snapshot redraw."""
        self.workspace = workspace
        self.vm = vm
        self.selected_node_id = None
        self.selected_node_ids.clear()
        self.selected_edge_id = None
        self.selected_container_id = None
        self.selected_container_ids.clear()
        self.hovered_warning_pid = None
        self._pending_drag_history_entry = None
        self.clear_diagnostic_highlights()
        self.history.clear()
        self._layout_cache.clear()
        layout_graph(vm)
        self.fit_to_view()
        if self.on_selection_changed:
            self.on_selection_changed(vm.title, vm.root_config, vm.subtitle)
        self.queue_draw()

    def _capture_history_entry(self, description: str) -> Optional[GraphHistoryEntry]:
        if not self.vm:
            return None
        return GraphHistoryEntry(
            description=description,
            vm_snapshot=self.vm.clone(),
            selected_node_id=self.selected_node_id,
            selected_node_ids=set(self.selected_node_ids),
            selected_edge_id=self.selected_edge_id,
            selected_container_id=self.selected_container_id,
            selected_container_ids=set(self.selected_container_ids),
        )

    def record_undo_snapshot(self, description: str) -> None:
        """Record a snapshot of the current graph state onto the Undo stack before a mutation."""
        if self._suppress_auto_history or not self.vm:
            return
        entry = self._capture_history_entry(description)
        if entry is not None:
            self.history.push_undo_entry(entry)

    def _notify_current_selection(self) -> None:
        if not self.vm or not self.on_selection_changed:
            return
        if self.selected_node_id and self.selected_node_id in self.vm.nodes:
            node = self.vm.nodes[self.selected_node_id]
            self.on_selection_changed(node.title, node.config, node.subtitle)
        elif self.selected_edge_id:
            edge = next((e for e in self.vm.edges if e.id == self.selected_edge_id), None)
            if edge:
                self.on_selection_changed(
                    f"Route: {edge.source_node} ({edge.source_port}) → {edge.sink_node} ({edge.sink_port})",
                    edge.config,
                    "Object.Base.route",
                )
            else:
                self.on_selection_changed(self.vm.title, self.vm.root_config, self.vm.subtitle)
        elif self.selected_container_id is not None and self.selected_container_id in self.vm.containers:
            cont = self.vm.containers[self.selected_container_id]
            self.on_selection_changed(
                f"Pipeline {self.selected_container_id}: {cont.class_name}",
                cont.config,
                f"Direction: {cont.direction}",
            )
        else:
            self.on_selection_changed(self.vm.title, self.vm.root_config, self.vm.subtitle)

    def _restore_history_entry(self, entry: GraphHistoryEntry) -> None:
        self._clear_drop_preview()
        self.vm = entry.vm_snapshot.clone()
        self.selected_node_id = entry.selected_node_id if entry.selected_node_id in self.vm.nodes else None
        self.selected_node_ids = {nid for nid in entry.selected_node_ids if nid in self.vm.nodes}
        self.selected_edge_id = (
            entry.selected_edge_id
            if any(e.id == entry.selected_edge_id for e in self.vm.edges)
            else None
        )
        self.selected_container_id = (
            entry.selected_container_id
            if entry.selected_container_id in self.vm.containers
            else None
        )
        self.selected_container_ids = {pid for pid in entry.selected_container_ids if pid in self.vm.containers}
        self._layout_cache.clear()
        self._notify_current_selection()
        if self.on_graph_modified:
            self.on_graph_modified()
        self.queue_draw()

    def undo(self) -> Optional[str]:
        """Undo the last graph or attribute modification and return its description."""
        if not self.vm or not self.history.can_undo:
            return None
        curr = self._capture_history_entry("")
        if curr is None:
            return None
        res = self.history.undo(curr)
        if res is None:
            return None
        prev_entry, desc = res
        self._restore_history_entry(prev_entry)
        return desc

    def redo(self) -> Optional[str]:
        """Redo the last undone graph or attribute modification and return its description."""
        if not self.vm or not self.history.can_redo:
            return None
        curr = self._capture_history_entry("")
        if curr is None:
            return None
        res = self.history.redo(curr)
        if res is None:
            return None
        next_entry, desc = res
        self._restore_history_entry(next_entry)
        return desc

    def relayout_graph(self) -> None:
        """Record an undo snapshot and re-run Sugiyama DAG auto-layout on the active graph."""
        if not self.vm:
            return
        self.record_undo_snapshot("Auto-layout graph")
        layout_graph(self.vm)
        self.vm.dirty = True
        self.fit_to_view()
        if self.on_graph_modified:
            self.on_graph_modified()
        self.queue_draw()

    def select_node(self, node_id: Optional[str], additive: bool = False) -> None:
        self.selected_edge_id = None
        if node_id is None:
            self.selected_node_id = None
            self.selected_node_ids.clear()
            self.selected_container_id = None
            self.selected_container_ids.clear()
        else:
            if additive:
                if node_id in self.selected_node_ids:
                    self.selected_node_ids.remove(node_id)
                    self.selected_node_id = next(iter(self.selected_node_ids), None)
                else:
                    self.selected_node_ids.add(node_id)
                    self.selected_node_id = node_id
            else:
                self.selected_node_ids = {node_id}
                self.selected_node_id = node_id
                self.selected_container_id = None
                self.selected_container_ids.clear()
        if self.vm and self.selected_node_id and self.selected_node_id in self.vm.nodes:
            node = self.vm.nodes[self.selected_node_id]
            if self.on_selection_changed:
                self.on_selection_changed(node.title, node.config, node.subtitle)
        self.queue_draw()

    def _get_graph_bounds(self) -> Tuple[float, float, float, float]:
        """Return (min_x, min_y, graph_w, graph_h) in world coordinates."""
        if not self.vm or not self.vm.nodes:
            return (0.0, 0.0, 200.0, 150.0)
        min_x = min(n.x for n in self.vm.nodes.values())
        min_y = min(n.y for n in self.vm.nodes.values())
        max_x = max(n.x + n.width for n in self.vm.nodes.values())
        max_y = max(n.y + n.height for n in self.vm.nodes.values())

        for c in self.vm.containers.values():
            min_x = min(min_x, c.x)
            min_y = min(min_y, c.y)
            max_x = max(max_x, c.x + c.width)
            max_y = max(max_y, c.y + c.height)

        graph_w = max(max_x - min_x, 200.0)
        graph_h = max(max_y - min_y, 150.0)
        return (min_x, min_y, graph_w, graph_h)

    def _compute_fit_zoom(self, alloc_w: float, alloc_h: float, graph_w: float, graph_h: float) -> float:
        pad = min(44.0, max(16.0, alloc_w * 0.04), max(16.0, alloc_h * 0.04))
        zx = max(40.0, alloc_w - 2.0 * pad) / graph_w
        zy = max(40.0, alloc_h - 2.0 * pad) / graph_h
        return max(0.08, min(4.0, min(zx, zy)))

    def fit_to_view(self, width: Optional[float] = None, height: Optional[float] = None) -> None:
        """Center and scale camera to fit the active graph."""
        self._is_fitted = True
        if not self.vm or not self.vm.nodes:
            self.pan_x = 30.0
            self.pan_y = 30.0
            self.zoom = 1.0
            self.queue_draw()
            return

        min_x, min_y, graph_w, graph_h = self._get_graph_bounds()
        alloc_w = float(width if width is not None else (self.get_width() or 860))
        alloc_h = float(height if height is not None else (self.get_height() or 600))

        self.zoom = self._compute_fit_zoom(alloc_w, alloc_h, graph_w, graph_h)
        self.pan_x = (alloc_w - graph_w * self.zoom) / 2.0 - min_x * self.zoom
        self.pan_y = (alloc_h - graph_h * self.zoom) / 2.0 - min_y * self.zoom
        self.queue_draw()

    def focus_on_world_rect(self, wx: float, wy: float, ww: float, wh: float) -> None:
        """Center and zoom the canvas camera onto a specific world rectangle `(wx, wy, ww, wh)`."""
        self._is_fitted = False
        alloc_w = float(self.get_width() or 860)
        alloc_h = float(self.get_height() or 600)
        pad_w = max(ww + 220.0, 480.0)
        pad_h = max(wh + 180.0, 320.0)
        self.zoom = max(0.35, min(1.35, min(alloc_w / pad_w, alloc_h / pad_h)))
        cx = wx + ww / 2.0
        cy = wy + wh / 2.0
        self.pan_x = (alloc_w / 2.0) - cx * self.zoom
        self.pan_y = (alloc_h / 2.0) - cy * self.zoom
        self.queue_draw()

    def focus_diagnostic(self, diag: object) -> None:
        """Focus and highlight the failing object on the main graph canvas for a CompileDiagnostic."""
        if not self.vm:
            return
        self.diag_highlight_node_id = None
        self.diag_highlight_container_id = None
        self.diag_highlight_edge_id = None

        sev = getattr(diag, "severity", "error")
        msg = getattr(diag, "message", "")
        sym = getattr(diag, "symbol_or_attr", None)
        tk = getattr(diag, "target_kind", "root")
        tid = getattr(diag, "target_id", None)
        lno = getattr(diag, "line_no", None)
        cno = getattr(diag, "col_no", None)

        loc_tag = f" (line {lno}{f':{cno}' if cno else ''})" if lno else ""
        sym_tag = f" [{sym}]" if sym and sym not in msg else ""
        self.diag_banner_severity = sev
        self.diag_banner_text = f"{'ERROR' if sev == 'error' else 'WARNING'}{loc_tag}{sym_tag}: {msg}"

        if tk == "node" and tid and tid in self.vm.nodes:
            self.diag_highlight_node_id = tid
            self.select_node(tid)
            n = self.vm.nodes[tid]
            self.focus_on_world_rect(n.x, n.y, n.width, n.height)
            return

        if tk == "container" and tid and str(tid).isdigit() and int(tid) in self.vm.containers:
            pid = int(tid)
            self.diag_highlight_container_id = pid
            self.selected_node_id = None
            self.selected_node_ids.clear()
            self.selected_edge_id = None
            self.selected_container_id = pid
            self.selected_container_ids = {pid}
            cont = self.vm.containers[pid]
            if self.on_selection_changed:
                self.on_selection_changed(
                    f"Pipeline {pid}: {cont.class_name}",
                    cont.config,
                    f"Direction: {cont.direction}",
                )
            self.focus_on_world_rect(cont.x, cont.y, cont.width, cont.height)
            return

        if tk == "edge" and tid:
            edge = next((e for e in self.vm.edges if e.id == tid), None)
            if edge:
                self.diag_highlight_edge_id = edge.id
                self.selected_node_id = None
                self.selected_node_ids.clear()
                self.selected_container_id = None
                self.selected_container_ids.clear()
                self.selected_edge_id = edge.id
                if self.on_selection_changed:
                    self.on_selection_changed(
                        f"Route: {edge.source_node} ({edge.source_port}) → {edge.sink_node} ({edge.sink_port})",
                        edge.config,
                        "Object.Base.route",
                    )
                u = self.vm.nodes.get(edge.source_node)
                v = self.vm.nodes.get(edge.sink_node)
                if u and v:
                    min_x = min(u.x, v.x)
                    min_y = min(u.y, v.y)
                    max_x = max(u.x + u.width, v.x + v.width)
                    max_y = max(u.y + u.height, v.y + v.height)
                    self.focus_on_world_rect(min_x, min_y, max_x - min_x, max_y - min_y)
                else:
                    self.queue_draw()
                return

        # Default / Define / Root level error: select topology root config so Define (Active Variables) is shown
        self.select_node(None)
        if self.on_selection_changed:
            self.on_selection_changed(self.vm.title, self.vm.root_config, self.vm.subtitle)

        # If a Define variable corresponds to a specific node or pipeline container on the graph, highlight & pan to it
        if sym:
            def_node = self.vm.root_config.get_child("Define (Active Variables)")
            def_val = str(def_node.get_scalar(sym) or "").strip("'\"") if def_node else ""
            if def_val in self.vm.nodes:
                n = self.vm.nodes[def_val]
                self.diag_highlight_node_id = def_val
                self.focus_on_world_rect(n.x, n.y, n.width, n.height)
                return
            if sym.endswith("_PIPELINE_ID") and def_val.isdigit() and int(def_val) in self.vm.containers:
                pid = int(def_val)
                c = self.vm.containers[pid]
                self.diag_highlight_container_id = pid
                self.focus_on_world_rect(c.x, c.y, c.width, c.height)
                return
            for pid, c in self.vm.containers.items():
                if sym in c.enabled_by:
                    self.diag_highlight_container_id = pid
                    self.focus_on_world_rect(c.x, c.y, c.width, c.height)
                    return

        self.fit_to_view()

    def _handle_canvas_resize(self, new_w: int, new_h: int) -> None:
        """Scale and re-center the graph when the canvas widget is resized."""
        old_w, old_h = self._last_canvas_size
        self._last_canvas_size = (new_w, new_h)
        if not self.vm or not self.vm.nodes:
            return
        if old_w <= 0 or old_h <= 0 or self._is_fitted:
            self.fit_to_view(float(new_w), float(new_h))
            return

        # User has manually zoomed/panned: scale zoom proportionally with canvas resize
        # while keeping the viewport center world point anchored at the new canvas center.
        _, _, graph_w, graph_h = self._get_graph_bounds()
        old_fit = self._compute_fit_zoom(float(old_w), float(old_h), graph_w, graph_h)
        new_fit = self._compute_fit_zoom(float(new_w), float(new_h), graph_w, graph_h)
        scale_ratio = (new_fit / old_fit) if old_fit > 0 else 1.0
        if abs(scale_ratio - 1.0) < 1e-4:
            # Fallback if fit axis didn't change: scale by geometric mean of dimension ratios
            scale_ratio = math.sqrt((new_w / float(old_w)) * (new_h / float(old_h)))

        wx_center, wy_center = self.screen_to_world(old_w / 2.0, old_h / 2.0)
        self.zoom = max(0.08, min(5.0, self.zoom * scale_ratio))
        self.pan_x = (new_w / 2.0) - wx_center * self.zoom
        self.pan_y = (new_h / 2.0) - wy_center * self.zoom

    def screen_to_world(self, sx: float, sy: float) -> Tuple[float, float]:
        return ((sx - self.pan_x) / self.zoom, (sy - self.pan_y) / self.zoom)

    def _get_pango_layout(self, text: str, size_pt: int = 10, bold: bool = False) -> Pango.Layout:
        key = (text, size_pt, bold)
        layout = self._layout_cache.get(key)
        if layout is None:
            layout = self.create_pango_layout(text)
            font_desc = Pango.FontDescription.from_string(
                f"Sans {'Bold ' if bold else ''}{size_pt}"
            )
            layout.set_font_description(font_desc)
            self._layout_cache[key] = layout
        return layout

    # -------------------------------------------------------------------------
    # GTK4 Hardware-Accelerated Snapshot Rendering
    # -------------------------------------------------------------------------

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        iw = self.get_width()
        ih = self.get_height()
        if iw <= 0 or ih <= 0:
            return
        if (iw, ih) != self._last_canvas_size:
            self._handle_canvas_resize(iw, ih)

        w = float(iw)
        h = float(ih)

        # 1. Background fill
        bounds = Graphene.Rect.alloc()
        bounds.init(0.0, 0.0, w, h)
        snapshot.append_color(BG_COLOR, bounds)

        # 2. Subtle grid lines in screen space
        self._snapshot_grid(snapshot, w, h)

        if not self.vm:
            return

        # 3. Push camera transform (pan + zoom)
        snapshot.save()
        pt = Graphene.Point.alloc()
        pt.init(self.pan_x, self.pan_y)
        snapshot.translate(pt)
        snapshot.scale(self.zoom, self.zoom)

        # Compute visible world rectangle for culling
        wx0, wy0 = self.screen_to_world(0.0, 0.0)
        wx1, wy1 = self.screen_to_world(w, h)

        # 4. Draw Pipeline Swimlane Containers
        for pid, cont in self.vm.containers.items():
            if cont.x + cont.width < wx0 or cont.x > wx1 or cont.y + cont.height < wy0 or cont.y > wy1:
                continue
            self._snapshot_container(snapshot, pid, cont)

        # 5. Draw Edges (Cubic Bezier routes via Gsk.PathBuilder)
        for edge in self.vm.edges:
            self._snapshot_edge(snapshot, edge)

        # 6. Draw Active Wire Drag Preview if wiring a route
        if self._drag_mode == "wire" and self._wire_source is not None:
            _, _, sx, sy = self._wire_source
            ex, ey = self._wire_cursor_world
            self._snapshot_bezier_wire(snapshot, sx, sy, ex, ey, WIRE_DRAG_COLOR, width=2.6, dashed=True)

        # 7. Draw Graph Nodes
        for nid, node in self.vm.nodes.items():
            if node.x + node.width < wx0 or node.x > wx1 or node.y + node.height < wy0 or node.y > wy1:
                continue
            self._snapshot_node(
                snapshot,
                node,
                is_selected=(nid == self.selected_node_id or nid in self.selected_node_ids),
            )

        # 7b. Draw Drag-Pause Drop Outcome Preview in World Space (ghost container/node/wires + target highlight)
        if self.drop_preview is not None:
            self._snapshot_drop_preview_world(snapshot, self.drop_preview)

        snapshot.restore()

        # 8. Draw HUD overlay (title, zoom badge, interaction hint)
        self._snapshot_hud(snapshot, w, h)

        # 9. Draw floating hover card when hovering a pipeline yellow warning bang
        if self.hovered_warning_pid is not None and self.hovered_warning_pid in self.vm.containers:
            self._snapshot_warning_tooltip(snapshot, w, h, self.vm.containers[self.hovered_warning_pid])

        # 10. Draw floating Drop Outcome Signal Card in Screen Space after a brief pause during drag
        if self.drop_preview is not None:
            self._snapshot_drop_preview_overlay(snapshot, w, h, self.drop_preview)

        # 11. Draw Compilation Diagnostic Banner at top of canvas when an error/warning is focused
        if self.diag_banner_text:
            self._snapshot_diagnostic_banner(snapshot, w, h)

    def _snapshot_diagnostic_banner(self, snapshot: Gtk.Snapshot, canvas_w: float, _canvas_h: float) -> None:
        if not self.diag_banner_text:
            return
        is_err = self.diag_banner_severity == "error"
        accent_col = rgba(1.0, 0.34, 0.34, 1.0) if is_err else rgba(1.0, 0.82, 0.25, 1.0)
        bg_col = rgba(0.26, 0.08, 0.10, 0.96) if is_err else rgba(0.24, 0.19, 0.06, 0.96)

        lyt = self._get_pango_layout(self.diag_banner_text, size_pt=9, bold=True)
        _, ext = lyt.get_pixel_extents()
        bw = min(canvas_w - 28.0, float(ext.width) + 32.0)
        bh = float(ext.height) + 16.0
        bx = max(14.0, (canvas_w - bw) / 2.0)
        by = 38.0

        rect = Graphene.Rect.alloc()
        rect.init(bx, by, bw, bh)
        rrect = Gsk.RoundedRect()
        rrect.init_from_rect(rect, 8.0)

        snapshot.push_rounded_clip(rrect)
        snapshot.append_color(bg_col, rect)
        bar_rect = Graphene.Rect.alloc()
        bar_rect.init(bx, by, 5.0, bh)
        snapshot.append_color(accent_col, bar_rect)
        snapshot.pop()

        snapshot.append_border(rrect, [1.8, 1.8, 1.8, 1.8], [accent_col, accent_col, accent_col, accent_col])
        snapshot.save()
        pt = Graphene.Point.alloc()
        pt.init(bx + 14.0, by + 8.0)
        snapshot.translate(pt)
        snapshot.append_layout(lyt, TEXT_WHITE)
        snapshot.restore()

    def _snapshot_grid(self, snapshot: Gtk.Snapshot, w: float, h: float) -> None:
        step = max(18.0, 32.0 * self.zoom)
        pb = Gsk.PathBuilder.new()
        x = math.fmod(self.pan_x, step)
        if x < 0:
            x += step
        while x < w:
            pb.move_to(x, 0.0)
            pb.line_to(x, h)
            x += step
        y = math.fmod(self.pan_y, step)
        if y < 0:
            y += step
        while y < h:
            pb.move_to(0.0, y)
            pb.line_to(w, y)
            y += step
        path = pb.to_path()
        stroke = Gsk.Stroke.new(1.0)
        snapshot.append_stroke(path, stroke, GRID_COLOR)

    @staticmethod
    def _parse_pipe_ids(pipes_str: str) -> Set[int]:
        pids: Set[int] = set()
        for token in pipes_str.replace("→", " ").replace(",", " ").split():
            if token.lstrip("-").isdigit():
                pids.add(int(token))
        return pids

    def _get_enabled_tb_pipelines(self) -> Set[int]:
        """Return the set of pipeline IDs enabled by active TestbenchIO radio selections."""
        enabled_pids: Set[int] = set()
        if not self.vm:
            return enabled_pids
        left_enabled = [
            n for n in self.vm.nodes.values()
            if n.class_category == "TestbenchIO"
            and str(n.config.get_scalar("side", "left")).lower() == "left"
            and str(n.config.get_scalar("enabled", "false")).lower() == "true"
        ]
        right_enabled = [
            n for n in self.vm.nodes.values()
            if n.class_category == "TestbenchIO"
            and str(n.config.get_scalar("side", "left")).lower() == "right"
            and str(n.config.get_scalar("enabled", "false")).lower() == "true"
        ]
        if left_enabled:
            for node in left_enabled:
                enabled_pids.update(self._parse_pipe_ids(str(node.config.get_scalar("target_pipelines", ""))))
            for node in right_enabled:
                parent = self.vm.nodes.get(node.parent_node_id or "")
                if parent and parent.pipeline_id is not None:
                    enabled_pids.add(parent.pipeline_id)
        else:
            for node in right_enabled:
                enabled_pids.update(self._parse_pipe_ids(str(node.config.get_scalar("target_pipelines", ""))))
        return enabled_pids

    def _snapshot_container(self, snapshot: Gtk.Snapshot, pid: int, cont: PipelineContainer) -> None:
        rect = Graphene.Rect.alloc()
        rect.init(cont.x, cont.y, cont.width, cont.height)
        rrect = Gsk.RoundedRect()
        rrect.init_from_rect(rect, 12.0)

        is_cap = "capture" in cont.direction.lower()
        is_tb_enabled = pid in self._get_enabled_tb_pipelines()
        is_cont_selected = (self.selected_container_id == pid or pid in self.selected_container_ids)
        has_warn = bool(cont.warning_text)
        fill_col = (
            rgba(0.36, 0.28, 0.10, 0.24)
            if has_warn
            else (CONTAINER_FILL_CAP if is_cap else CONTAINER_FILL_PB)
        )
        border_col = (
            NODE_BORDER_SELECTED
            if is_cont_selected
            else (
                rgba(0.98, 0.78, 0.16, 0.92)
                if has_warn
                else (
                    rgba(0.28, 0.86, 0.56, 0.78)
                    if is_tb_enabled
                    else (CONTAINER_BORDER_CAP if is_cap else CONTAINER_BORDER_PB)
                )
            )
        )

        snapshot.push_rounded_clip(rrect)
        snapshot.append_color(fill_col, rect)
        snapshot.pop()

        bw = 2.2 if (is_cont_selected or is_tb_enabled or has_warn) else 1.4
        if self.diag_highlight_container_id == pid:
            diag_col = (
                rgba(1.0, 0.30, 0.30, 0.98)
                if self.diag_banner_severity == "error"
                else rgba(1.0, 0.82, 0.25, 0.98)
            )
            snapshot.append_border(rrect, [3.2, 3.2, 3.2, 3.2], [diag_col, diag_col, diag_col, diag_col])
        else:
            snapshot.append_border(rrect, [bw, bw, bw, bw], [border_col, border_col, border_col, border_col])

        text_x = cont.x + 14.0
        if has_warn:
            # Render yellow warning bang badge [ ⚠ ] in container header
            bang_rect = Graphene.Rect.alloc()
            bang_rect.init(cont.x + 12.0, cont.y + 8.0, 24.0, 20.0)
            bang_rr = Gsk.RoundedRect()
            bang_rr.init_from_rect(bang_rect, 5.0)
            is_hov_warn = self.hovered_warning_pid == pid
            bang_bg = rgba(1.0, 0.84, 0.18, 1.0) if is_hov_warn else rgba(0.95, 0.74, 0.10, 0.96)
            snapshot.push_rounded_clip(bang_rr)
            snapshot.append_color(bang_bg, bang_rect)
            snapshot.pop()
            bang_bcol = rgba(1.0, 0.95, 0.55, 1.0)
            snapshot.append_border(bang_rr, [1.2, 1.2, 1.2, 1.2], [bang_bcol, bang_bcol, bang_bcol, bang_bcol])

            bang_layout = self._get_pango_layout("⚠", size_pt=9, bold=True)
            snapshot.save()
            bpt = Graphene.Point.alloc()
            bpt.init(cont.x + 17.5, cont.y + 9.0)
            snapshot.translate(bpt)
            snapshot.append_layout(bang_layout, rgba(0.08, 0.08, 0.08, 1.0))
            snapshot.restore()
            text_x = cont.x + 42.0

        # Container header text
        tb_tag = "  •  [TB Active]" if is_tb_enabled else ""
        header_txt = f"Pipeline {pid}: {cont.class_name} ({cont.direction}){tb_tag}"
        layout = self._get_pango_layout(header_txt, size_pt=9, bold=True)
        snapshot.save()
        pt = Graphene.Point.alloc()
        pt.init(text_x, cont.y + 10.0)
        snapshot.translate(pt)
        snapshot.append_layout(layout, rgba(1.0, 0.92, 0.55, 1.0) if has_warn else TEXT_WHITE)
        snapshot.restore()

    def _snapshot_warning_tooltip(
        self,
        snapshot: Gtk.Snapshot,
        canvas_w: float,
        canvas_h: float,
        cont: PipelineContainer,
    ) -> None:
        """Render a high-contrast floating overlay explaining why a pipeline has a yellow bang and how it was enabled."""
        lines = [ln for ln in cont.warning_text.splitlines() if ln.strip()]
        if not lines:
            return

        layouts: List[Tuple[Pango.Layout, Gdk.RGBA]] = []
        max_lw = 0.0
        total_lh = 0.0
        for idx, ln in enumerate(lines):
            is_hdr = idx == 0
            lyt = self._get_pango_layout(ln, size_pt=9 if is_hdr else 8, bold=is_hdr)
            _, ext = lyt.get_pixel_extents()
            max_lw = max(max_lw, float(ext.width))
            total_lh += float(ext.height) + (6.0 if is_hdr else 4.0)
            col = rgba(1.0, 0.86, 0.28, 1.0) if is_hdr else TEXT_WHITE
            layouts.append((lyt, col))

        box_w = max_lw + 28.0
        box_h = total_lh + 18.0

        if self._pointer_pos is not None:
            px, py = self._pointer_pos
            tx = min(max(12.0, px + 16.0), max(12.0, canvas_w - box_w - 12.0))
            ty = min(max(12.0, py + 18.0), max(12.0, canvas_h - box_h - 12.0))
        else:
            sx, sy = self.world_to_screen(cont.x + 12.0, cont.y + 34.0)
            tx = min(max(12.0, sx), max(12.0, canvas_w - box_w - 12.0))
            ty = min(max(12.0, sy), max(12.0, canvas_h - box_h - 12.0))

        rect = Graphene.Rect.alloc()
        rect.init(tx, ty, box_w, box_h)
        rrect = Gsk.RoundedRect()
        rrect.init_from_rect(rect, 8.0)

        snapshot.push_rounded_clip(rrect)
        snapshot.append_color(rgba(0.24, 0.19, 0.06, 0.97), rect)
        snapshot.pop()

        bcol = rgba(0.98, 0.78, 0.16, 0.96)
        snapshot.append_border(rrect, [1.8, 1.8, 1.8, 1.8], [bcol, bcol, bcol, bcol])

        cy = ty + 10.0
        pt = Graphene.Point.alloc()
        for idx, (lyt, col) in enumerate(layouts):
            _, ext = lyt.get_pixel_extents()
            snapshot.save()
            pt.init(tx + 14.0, cy)
            snapshot.translate(pt)
            snapshot.append_layout(lyt, col)
            snapshot.restore()
            cy += float(ext.height) + (6.0 if idx == 0 else 4.0)

    def _snapshot_drop_preview_world(self, snapshot: Gtk.Snapshot, preview: DropOutcomePreview) -> None:
        """Render world-space drop outcome cues: target container glow, spliced wire highlight, ghost container/node/wires."""
        if not self.vm:
            return

        # 1. Highlight target pipeline container if joining an existing pipeline
        if preview.target_pid is not None and preview.target_pid in self.vm.containers:
            t_cont = self.vm.containers[preview.target_pid]
            glow_rect = Graphene.Rect.alloc()
            glow_rect.init(t_cont.x - 4.0, t_cont.y - 4.0, t_cont.width + 8.0, t_cont.height + 8.0)
            glow_rrect = Gsk.RoundedRect()
            glow_rrect.init_from_rect(glow_rect, 14.0)

            snapshot.push_rounded_clip(glow_rrect)
            snapshot.append_color(rgba(0.16, 0.88, 0.62, 0.16), glow_rect)
            snapshot.pop()

            hl_col = rgba(0.22, 0.96, 0.68, 0.98)
            snapshot.append_border(glow_rrect, [2.8, 2.8, 2.8, 2.8], [hl_col, hl_col, hl_col, hl_col])

            tag_text = f"JOIN PIPELINE {preview.target_pid}"
            tag_lyt = self._get_pango_layout(tag_text, size_pt=8, bold=True)
            _, tag_ext = tag_lyt.get_pixel_extents()
            tw = float(tag_ext.width) + 16.0
            th = float(tag_ext.height) + 8.0
            tx = t_cont.x + t_cont.width - tw - 10.0
            ty = t_cont.y + 7.0
            t_rect = Graphene.Rect.alloc()
            t_rect.init(tx, ty, tw, th)
            t_rr = Gsk.RoundedRect()
            t_rr.init_from_rect(t_rect, 6.0)
            snapshot.push_rounded_clip(t_rr)
            snapshot.append_color(rgba(0.10, 0.56, 0.38, 0.96), t_rect)
            snapshot.pop()
            snapshot.append_border(t_rr, [1.2, 1.2, 1.2, 1.2], [hl_col, hl_col, hl_col, hl_col])
            snapshot.save()
            pt = Graphene.Point.alloc()
            pt.init(tx + 8.0, ty + 4.0)
            snapshot.translate(pt)
            snapshot.append_layout(tag_lyt, TEXT_WHITE)
            snapshot.restore()

        # 2. Highlight existing edge being spliced if inserting between two connected widgets
        if preview.spliced_edge_id:
            s_edge = next((e for e in self.vm.edges if e.id == preview.spliced_edge_id), None)
            if s_edge:
                u = self.vm.nodes.get(s_edge.source_node)
                v = self.vm.nodes.get(s_edge.sink_node)
                if u and v:
                    u_port, v_port = self._resolve_edge_ports(u, v, s_edge)
                    self._snapshot_bezier_wire(
                        snapshot,
                        u.x + u_port.rel_x,
                        u.y + u_port.rel_y,
                        v.x + v_port.rel_x,
                        v.y + v_port.rel_y,
                        rgba(1.0, 0.52, 0.24, 0.96),
                        width=3.4,
                        dashed=True,
                    )

        # 3. Draw Ghost Pipeline Container (when creating a new pipeline or placing an adjacent pipeline)
        if preview.ghost_container is not None:
            gx, gy, gw, gh, glabel, is_cap = preview.ghost_container
            g_rect = Graphene.Rect.alloc()
            g_rect.init(gx, gy, gw, gh)
            g_rrect = Gsk.RoundedRect()
            g_rrect.init_from_rect(g_rect, 12.0)

            fill_col = rgba(0.14, 0.46, 0.34, 0.24) if is_cap else rgba(0.14, 0.36, 0.62, 0.24)
            border_col = rgba(0.28, 0.94, 0.68, 0.92) if is_cap else rgba(0.36, 0.84, 1.00, 0.92)

            snapshot.push_rounded_clip(g_rrect)
            snapshot.append_color(fill_col, g_rect)
            snapshot.pop()

            pb = Gsk.PathBuilder.new()
            pb.add_rounded_rect(g_rrect)
            stroke = Gsk.Stroke.new(2.2)
            stroke.set_dash([8.0, 5.0])
            snapshot.append_stroke(pb.to_path(), stroke, border_col)

            hdr_lyt = self._get_pango_layout(glabel, size_pt=9, bold=True)
            snapshot.save()
            pt = Graphene.Point.alloc()
            pt.init(gx + 14.0, gy + 10.0)
            snapshot.translate(pt)
            snapshot.append_layout(hdr_lyt, border_col)
            snapshot.restore()

        # 4. Draw Ghost Node Card
        if preview.ghost_node is not None:
            nx, ny, nw, nh, ntitle, nsub = preview.ghost_node
            n_rect = Graphene.Rect.alloc()
            n_rect.init(nx, ny, nw, nh)
            n_rrect = Gsk.RoundedRect()
            n_rrect.init_from_rect(n_rect, 9.0)

            snapshot.push_rounded_clip(n_rrect)
            snapshot.append_color(rgba(0.10, 0.22, 0.26, 0.86), n_rect)
            hdr_rect = Graphene.Rect.alloc()
            hdr_rect.init(nx, ny, nw, 26.0)
            snapshot.append_color(rgba(0.16, 0.68, 0.56, 0.90), hdr_rect)
            snapshot.pop()

            pb = Gsk.PathBuilder.new()
            pb.add_rounded_rect(n_rrect)
            stroke = Gsk.Stroke.new(2.2)
            stroke.set_dash([6.0, 4.0])
            snapshot.append_stroke(pb.to_path(), stroke, rgba(0.32, 0.98, 0.76, 0.96))

            t_lyt = self._get_pango_layout(ntitle, size_pt=9, bold=True)
            s_lyt = self._get_pango_layout(nsub, size_pt=8, bold=False)
            snapshot.save()
            pt = Graphene.Point.alloc()
            pt.init(nx + 10.0, ny + 5.0)
            snapshot.translate(pt)
            snapshot.append_layout(t_lyt, TEXT_WHITE)
            snapshot.restore()

            snapshot.save()
            pt = Graphene.Point.alloc()
            pt.init(nx + 10.0, ny + 34.0)
            snapshot.translate(pt)
            snapshot.append_layout(s_lyt, rgba(0.76, 0.94, 0.88, 0.96))
            snapshot.restore()

        # 5. Draw Ghost Route Wires showing new connections that will be formed on drop
        for x1, y1, x2, y2, is_inter in preview.ghost_wires:
            w_col = rgba(1.0, 0.78, 0.28, 0.96) if is_inter else rgba(0.24, 0.98, 0.68, 0.96)
            self._snapshot_bezier_wire(snapshot, x1, y1, x2, y2, w_col, width=3.0, dashed=True)

    def _snapshot_drop_preview_overlay(
        self,
        snapshot: Gtk.Snapshot,
        canvas_w: float,
        canvas_h: float,
        preview: DropOutcomePreview,
    ) -> None:
        """Render floating screen-space Drop Outcome Signal Card near the paused cursor."""
        if preview.action_kind == "join_pipeline":
            badge_str = "DROP OUTCOME  •  JOIN EXISTING PIPELINE"
            accent_col = rgba(0.22, 0.96, 0.68, 1.0)
            bg_col = rgba(0.06, 0.20, 0.16, 0.96)
        elif preview.action_kind == "create_pipeline":
            badge_str = "DROP OUTCOME  •  CREATE NEW PIPELINE"
            accent_col = rgba(0.36, 0.86, 1.00, 1.0)
            bg_col = rgba(0.07, 0.16, 0.24, 0.96)
        else:
            badge_str = "DROP OUTCOME  •  REPOSITION OBJECT"
            accent_col = rgba(0.98, 0.82, 0.34, 1.0)
            bg_col = rgba(0.20, 0.16, 0.07, 0.96)

        l_badge = self._get_pango_layout(badge_str, size_pt=8, bold=True)
        l_head = self._get_pango_layout(preview.headline, size_pt=10, bold=True)
        l_det = self._get_pango_layout(preview.detail, size_pt=9, bold=False)

        _, ext_b = l_badge.get_pixel_extents()
        _, ext_h = l_head.get_pixel_extents()
        _, ext_d = l_det.get_pixel_extents()

        max_w = max(float(ext_b.width), float(ext_h.width), float(ext_d.width))
        box_w = max_w + 28.0
        box_h = float(ext_b.height + ext_h.height + ext_d.height) + 32.0

        tx = min(max(12.0, preview.sx + 20.0), max(12.0, canvas_w - box_w - 12.0))
        ty = min(max(12.0, preview.sy + 20.0), max(12.0, canvas_h - box_h - 12.0))

        rect = Graphene.Rect.alloc()
        rect.init(tx, ty, box_w, box_h)
        rrect = Gsk.RoundedRect()
        rrect.init_from_rect(rect, 9.0)

        snapshot.push_rounded_clip(rrect)
        snapshot.append_color(bg_col, rect)
        bar_rect = Graphene.Rect.alloc()
        bar_rect.init(tx, ty, 5.0, box_h)
        snapshot.append_color(accent_col, bar_rect)
        snapshot.pop()

        snapshot.append_border(rrect, [1.8, 1.8, 1.8, 1.8], [accent_col, accent_col, accent_col, accent_col])

        cy = ty + 9.0
        pt = Graphene.Point.alloc()
        for lyt, col, gap in (
            (l_badge, accent_col, 4.0),
            (l_head, TEXT_WHITE, 4.0),
            (l_det, rgba(0.84, 0.92, 0.96, 1.0), 0.0),
        ):
            _, ext = lyt.get_pixel_extents()
            snapshot.save()
            pt.init(tx + 14.0, cy)
            snapshot.translate(pt)
            snapshot.append_layout(lyt, col)
            snapshot.restore()
            cy += float(ext.height) + gap

    def _snapshot_node(self, snapshot: Gtk.Snapshot, node: GraphNode, is_selected: bool) -> None:
        rect = Graphene.Rect.alloc()
        rect.init(node.x, node.y, node.width, node.height)
        rrect = Gsk.RoundedRect()
        rrect.init_from_rect(rect, 6.0 if node.class_category == "Control" else 9.0)

        header_col, body_col = CATEGORY_COLORS.get(node.category, CATEGORY_COLORS["widget"])

        if self.diag_highlight_node_id == node.id:
            d_rect = Graphene.Rect.alloc()
            d_rect.init(node.x - 6.0, node.y - 6.0, node.width + 12.0, node.height + 12.0)
            d_rr = Gsk.RoundedRect()
            d_rr.init_from_rect(d_rect, 13.0)
            d_fill = (
                rgba(1.0, 0.24, 0.24, 0.26)
                if self.diag_banner_severity == "error"
                else rgba(1.0, 0.80, 0.20, 0.26)
            )
            d_bcol = (
                rgba(1.0, 0.30, 0.30, 0.98)
                if self.diag_banner_severity == "error"
                else rgba(1.0, 0.82, 0.25, 0.98)
            )
            snapshot.push_rounded_clip(d_rr)
            snapshot.append_color(d_fill, d_rect)
            snapshot.pop()
            snapshot.append_border(d_rr, [2.8, 2.8, 2.8, 2.8], [d_bcol, d_bcol, d_bcol, d_bcol])

        # Selection glow shadow
        if is_selected:
            glow_rect = Graphene.Rect.alloc()
            glow_rect.init(node.x - 4.0, node.y - 4.0, node.width + 8.0, node.height + 8.0)
            glow_rr = Gsk.RoundedRect()
            glow_rr.init_from_rect(glow_rect, 9.0 if node.class_category == "Control" else 12.0)
            snapshot.push_rounded_clip(glow_rr)
            snapshot.append_color(NODE_SHADOW_SELECTED, glow_rect)
            snapshot.pop()

        if node.class_category == "Control":
            # Compact vertical kcontrol node rendered beneath its parent widget
            snapshot.push_rounded_clip(rrect)
            snapshot.append_color(body_col, rect)

            hdr_rect = Graphene.Rect.alloc()
            hdr_rect.init(node.x, node.y, node.width, 18.0)
            snapshot.append_color(header_col, hdr_rect)

            # Short control type tag in top cap
            tag_map = {
                "control_mixer": "VOL",
                "control_switch": "SW",
                "control_bytes": "BYT",
                "control_enum": "ENM",
            }
            tag_txt = tag_map.get(node.category, "CTL")
            tag_layout = self._get_pango_layout(tag_txt, size_pt=6, bold=True)
            _, tag_ext = tag_layout.get_pixel_extents()
            snapshot.save()
            pt = Graphene.Point.alloc()
            pt.init(node.x + max(2.0, (node.width - float(tag_ext.width)) / 2.0), node.y + 3.0)
            snapshot.translate(pt)
            snapshot.append_layout(tag_layout, TEXT_WHITE)
            snapshot.restore()

            # 90-degree rotated vertical label inside the control pill
            v_layout = self._get_pango_layout(node.title, size_pt=7, bold=True)
            _, v_ext = v_layout.get_pixel_extents()
            lh = float(v_ext.height) if v_ext.height > 0 else 11.0
            snapshot.save()
            pt.init(node.x + (node.width + lh) / 2.0, node.y + 22.0)
            snapshot.translate(pt)
            snapshot.rotate(90.0)
            snapshot.append_layout(v_layout, TEXT_WHITE)
            snapshot.restore()

            snapshot.pop()

            bcol = NODE_BORDER_SELECTED if is_selected else header_col
            bw = 2.0 if is_selected else 1.1
            snapshot.append_border(rrect, [bw, bw, bw, bw], [bcol, bcol, bcol, bcol])

            for port in node.out_ports:
                px = node.x + port.rel_x
                py = node.y + port.rel_y
                self._snapshot_port_circle(snapshot, px, py, header_col, radius=3.6)
            return

        if node.class_category == "TestbenchIO":
            self._snapshot_testbench_io_node(snapshot, node, rect, rrect, is_selected)
            return

        # Clip rounded node card
        snapshot.push_rounded_clip(rrect)
        snapshot.append_color(body_col, rect)

        # Colored header strip
        hdr_rect = Graphene.Rect.alloc()
        hdr_rect.init(node.x, node.y, node.width, 26.0)
        snapshot.append_color(header_col, hdr_rect)
        snapshot.pop()

        # Node border
        bcol = NODE_BORDER_SELECTED if is_selected else NODE_BORDER_NORMAL
        bw = 2.2 if is_selected else 1.2
        snapshot.append_border(rrect, [bw, bw, bw, bw], [bcol, bcol, bcol, bcol])

        # Node Title in header (full text, node width is sized to fit)
        title_layout = self._get_pango_layout(node.title, size_pt=9, bold=True)
        snapshot.save()
        pt = Graphene.Point.alloc()
        pt.init(node.x + 10.0, node.y + 5.0)
        snapshot.translate(pt)
        snapshot.append_layout(title_layout, TEXT_WHITE)
        snapshot.restore()

        # Subtitle (full text, node width is sized to fit)
        sub_layout = self._get_pango_layout(node.subtitle, size_pt=8, bold=False)
        snapshot.save()
        pt.init(node.x + 10.0, node.y + 31.0)
        snapshot.translate(pt)
        snapshot.append_layout(sub_layout, TEXT_MUTED)
        snapshot.restore()

        # Bottom row: Input audio format on bottom-left, Output audio format on bottom-right
        by = node.y + node.height - 24.0
        bh_px = 16.0
        if node.in_fmt_badge or node.out_fmt_badge:
            if node.in_fmt_badge:
                in_layout = self._get_pango_layout(node.in_fmt_badge, size_pt=7, bold=True)
                _, in_log = in_layout.get_pixel_extents()
                in_bw = float(in_log.width) + 10.0
                in_bx = node.x + 10.0
                brect = Graphene.Rect.alloc()
                brect.init(in_bx, by, in_bw, bh_px)
                brr = Gsk.RoundedRect()
                brr.init_from_rect(brect, 4.0)
                snapshot.push_rounded_clip(brr)
                snapshot.append_color(BADGE_BG, brect)
                snapshot.pop()

                snapshot.save()
                pt.init(in_bx + 5.0, by + 2.0)
                snapshot.translate(pt)
                snapshot.append_layout(in_layout, BADGE_FG)
                snapshot.restore()

            if node.out_fmt_badge:
                out_layout = self._get_pango_layout(node.out_fmt_badge, size_pt=7, bold=True)
                _, out_log = out_layout.get_pixel_extents()
                out_bw = float(out_log.width) + 10.0
                out_bx = node.x + node.width - out_bw - 10.0
                brect = Graphene.Rect.alloc()
                brect.init(out_bx, by, out_bw, bh_px)
                brr = Gsk.RoundedRect()
                brr.init_from_rect(brect, 4.0)
                snapshot.push_rounded_clip(brr)
                snapshot.append_color(BADGE_BG, brect)
                snapshot.pop()

                snapshot.save()
                pt.init(out_bx + 5.0, by + 2.0)
                snapshot.translate(pt)
                snapshot.append_layout(out_layout, BADGE_FG)
                snapshot.restore()
        elif node.badges:
            bx = node.x + 10.0
            for btxt in node.badges[:2]:
                blayout = self._get_pango_layout(btxt, size_pt=7, bold=True)
                ink, logical = blayout.get_pixel_extents()
                bw_px = float(logical.width) + 10.0
                if bx + bw_px > node.x + node.width - 8.0:
                    break
                brect = Graphene.Rect.alloc()
                brect.init(bx, by, bw_px, bh_px)
                brr = Gsk.RoundedRect()
                brr.init_from_rect(brect, 4.0)
                snapshot.push_rounded_clip(brr)
                snapshot.append_color(BADGE_BG, brect)
                snapshot.pop()

                snapshot.save()
                pt.init(bx + 5.0, by + 2.0)
                snapshot.translate(pt)
                snapshot.append_layout(blayout, BADGE_FG)
                snapshot.restore()
                bx += bw_px + 5.0

        # Input & Output connection ports
        for port in node.in_ports:
            px = node.x + port.rel_x
            py = node.y + port.rel_y
            is_hov = self.hovered_port == (node.id, port.id, "in")
            self._snapshot_port_circle(snapshot, px, py, PORT_HOVER_COLOR if is_hov else PORT_IN_COLOR, radius=6.5 if is_hov else 5.0)

        for port in node.out_ports:
            px = node.x + port.rel_x
            py = node.y + port.rel_y
            is_hov = self.hovered_port == (node.id, port.id, "out")
            self._snapshot_port_circle(snapshot, px, py, PORT_HOVER_COLOR if is_hov else PORT_OUT_COLOR, radius=6.5 if is_hov else 5.0)

    def _snapshot_testbench_io_node(
        self,
        snapshot: Gtk.Snapshot,
        node: GraphNode,
        rect: Graphene.Rect,
        rrect: Gsk.RoundedRect,
        is_selected: bool,
    ) -> None:
        """Render interactive Testbench Host Source/Sink card with pipeline radio and File/ALSA selector."""
        is_enabled = str(node.config.get_scalar("enabled", "false")).lower() == "true"
        io_type = str(node.config.get_scalar("io_type", "file")).lower()
        file_path = str(node.config.get_scalar("file_path", "audio.wav"))
        alsa_pcm = str(node.config.get_scalar("alsa_pcm", "default"))
        pipes_str = str(node.config.get_scalar("target_pipelines", ""))

        header_col = rgba(0.14, 0.58, 0.38, 1.0) if is_enabled else rgba(0.24, 0.29, 0.38, 1.0)
        body_col = rgba(0.10, 0.16, 0.15, 0.97) if is_enabled else rgba(0.12, 0.14, 0.18, 0.96)

        snapshot.push_rounded_clip(rrect)
        snapshot.append_color(body_col, rect)

        # Header bar with Radio Button
        hdr_rect = Graphene.Rect.alloc()
        hdr_rect.init(node.x, node.y, node.width, 26.0)
        snapshot.append_color(header_col, hdr_rect)

        # Radio outer circle & inner dot at (node.x + 14, node.y + 13)
        rcx = node.x + 14.0
        rcy = node.y + 13.0
        self._snapshot_port_circle(snapshot, rcx, rcy, TEXT_WHITE, radius=6.5)
        self._snapshot_port_circle(snapshot, rcx, rcy, header_col, radius=4.8)
        if is_enabled:
            self._snapshot_port_circle(snapshot, rcx, rcy, rgba(0.45, 1.0, 0.72, 1.0), radius=3.2)

        # Header title + ACTIVE/OFF status
        status_txt = "ACTIVE" if is_enabled else "OFF"
        title_layout = self._get_pango_layout(f"{node.title}  [{status_txt}]", size_pt=8, bold=True)
        pt = Graphene.Point.alloc()
        snapshot.save()
        pt.init(node.x + 26.0, node.y + 6.0)
        snapshot.translate(pt)
        snapshot.append_layout(title_layout, TEXT_WHITE)
        snapshot.restore()

        # Row 1: Mode radio pills ([ ● File ]  [ ○ ALSA PCM ])
        pill_y = node.y + 31.0
        pill_h = 19.0
        avail_w = node.width - 26.0
        file_pw = round(avail_w * 0.46, 1)
        alsa_pw = round(avail_w - file_pw, 1)
        for mode_key, label_txt, px_off, pw in (
            ("file", "File", 10.0, file_pw),
            ("alsa_pcm", "ALSA PCM", 16.0 + file_pw, alsa_pw),
        ):
            active_mode = io_type == mode_key
            p_rect = Graphene.Rect.alloc()
            p_rect.init(node.x + px_off, pill_y, pw, pill_h)
            p_rr = Gsk.RoundedRect()
            p_rr.init_from_rect(p_rect, 4.5)
            p_bg = rgba(0.20, 0.48, 0.72, 0.95) if active_mode else rgba(0.17, 0.20, 0.26, 0.92)
            snapshot.push_rounded_clip(p_rr)
            snapshot.append_color(p_bg, p_rect)
            snapshot.pop()
            p_bcol = rgba(0.42, 0.76, 1.0, 0.95) if active_mode else rgba(0.28, 0.33, 0.42, 0.85)
            snapshot.append_border(p_rr, [1.0, 1.0, 1.0, 1.0], [p_bcol, p_bcol, p_bcol, p_bcol])

            dot_prefix = "● " if active_mode else "○ "
            m_layout = self._get_pango_layout(dot_prefix + label_txt, size_pt=7, bold=active_mode)
            snapshot.save()
            pt.init(node.x + px_off + 7.0, pill_y + 3.0)
            snapshot.translate(pt)
            snapshot.append_layout(m_layout, TEXT_WHITE if active_mode else TEXT_MUTED)
            snapshot.restore()

        # Row 2: Clickable File / ALSA PCM selector box
        sel_y = node.y + 54.0
        sel_h = 20.0
        sel_rect = Graphene.Rect.alloc()
        sel_rect.init(node.x + 10.0, sel_y, node.width - 20.0, sel_h)
        sel_rr = Gsk.RoundedRect()
        sel_rr.init_from_rect(sel_rect, 4.5)
        snapshot.push_rounded_clip(sel_rr)
        snapshot.append_color(rgba(0.08, 0.10, 0.14, 0.95), sel_rect)
        snapshot.pop()
        sel_bcol = rgba(0.34, 0.44, 0.58, 0.95)
        snapshot.append_border(sel_rr, [1.0, 1.0, 1.0, 1.0], [sel_bcol, sel_bcol, sel_bcol, sel_bcol])

        if io_type == "file":
            disp_val = os.path.basename(file_path) or file_path
            sel_txt = f"File: {disp_val}"
        else:
            sel_txt = f"PCM: {alsa_pcm}"
        if len(sel_txt) > 26:
            sel_txt = sel_txt[:24] + "…"
        sel_layout = self._get_pango_layout(sel_txt, size_pt=7, bold=False)
        snapshot.save()
        pt.init(node.x + 16.0, sel_y + 3.0)
        snapshot.translate(pt)
        snapshot.append_layout(sel_layout, BADGE_FG)
        snapshot.restore()

        caret_layout = self._get_pango_layout("▾", size_pt=7, bold=True)
        snapshot.save()
        pt.init(node.x + node.width - 22.0, sel_y + 3.0)
        snapshot.translate(pt)
        snapshot.append_layout(caret_layout, TEXT_MUTED)
        snapshot.restore()

        # Row 3: Downstream/upstream pipelines badge
        foot_txt = f"Enable Pipes: {pipes_str}" if pipes_str else node.subtitle
        foot_layout = self._get_pango_layout(foot_txt, size_pt=7, bold=True)
        snapshot.save()
        pt.init(node.x + 12.0, node.y + 78.0)
        snapshot.translate(pt)
        snapshot.append_layout(foot_layout, rgba(0.55, 0.94, 0.74, 1.0) if is_enabled else TEXT_MUTED)
        snapshot.restore()

        snapshot.pop()

        bcol = (
            NODE_BORDER_SELECTED
            if is_selected
            else (rgba(0.28, 0.84, 0.56, 0.95) if is_enabled else NODE_BORDER_NORMAL)
        )
        bw = 2.2 if (is_selected or is_enabled) else 1.2
        snapshot.append_border(rrect, [bw, bw, bw, bw], [bcol, bcol, bcol, bcol])

        for port in node.in_ports:
            px = node.x + port.rel_x
            py = node.y + port.rel_y
            self._snapshot_port_circle(snapshot, px, py, PORT_IN_COLOR, radius=5.0)
        for port in node.out_ports:
            px = node.x + port.rel_x
            py = node.y + port.rel_y
            self._snapshot_port_circle(snapshot, px, py, PORT_OUT_COLOR, radius=5.0)

    def _snapshot_port_circle(self, snapshot: Gtk.Snapshot, cx: float, cy: float, color: Gdk.RGBA, radius: float = 5.0) -> None:
        rect = Graphene.Rect.alloc()
        rect.init(cx - radius, cy - radius, radius * 2.0, radius * 2.0)
        rrect = Gsk.RoundedRect()
        rrect.init_from_rect(rect, radius)
        snapshot.push_rounded_clip(rrect)
        snapshot.append_color(color, rect)
        snapshot.pop()

    def _snapshot_edge(self, snapshot: Gtk.Snapshot, edge: GraphEdge) -> None:
        if not self.vm:
            return
        u = self.vm.nodes.get(edge.source_node)
        v = self.vm.nodes.get(edge.sink_node)
        if not u or not v:
            return

        is_sel = edge.id == self.selected_edge_id
        if u.class_category == "Control":
            x1 = u.x + u.width / 2.0
            y1 = u.y
            x2 = max(v.x + 10.0, min(v.x + v.width - 10.0, x1))
            y2 = v.y + v.height
            hdr_col, _ = CATEGORY_COLORS.get(u.category, CATEGORY_COLORS["control"])
            color = EDGE_SELECTED_COLOR if is_sel else hdr_col
            dy = max(6.0, abs(y1 - y2) * 0.45)
            pb = Gsk.PathBuilder.new()
            pb.move_to(x1, y1)
            pb.cubic_to(x1, y1 - dy, x2, y2 + dy, x2, y2)
            stroke = Gsk.Stroke.new(2.4 if is_sel else 1.5)
            stroke.set_dash([3.0, 2.5])
            snapshot.append_stroke(pb.to_path(), stroke, color)

            arrow_pb = Gsk.PathBuilder.new()
            arrow_pb.move_to(x2, y2)
            arrow_pb.line_to(x2 - 3.2, y2 + 5.5)
            arrow_pb.line_to(x2 + 3.2, y2 + 5.5)
            arrow_pb.close()
            snapshot.append_fill(arrow_pb.to_path(), Gsk.FillRule.WINDING, color)
            return

        u_port, v_port = self._resolve_edge_ports(u, v, edge)

        x1 = u.x + u_port.rel_x
        y1 = u.y + u_port.rel_y
        x2 = v.x + v_port.rel_x
        y2 = v.y + v_port.rel_y

        is_diag = edge.id == self.diag_highlight_edge_id
        if is_diag:
            color = (
                rgba(1.0, 0.30, 0.30, 1.0)
                if self.diag_banner_severity == "error"
                else rgba(1.0, 0.82, 0.25, 1.0)
            )
            width = 3.6
        else:
            color = EDGE_SELECTED_COLOR if is_sel else (EDGE_INTER_COLOR if edge.is_inter_pipeline else EDGE_INTRA_COLOR)
            width = 3.2 if is_sel else (2.4 if edge.is_inter_pipeline else 2.0)
        self._snapshot_bezier_wire(snapshot, x1, y1, x2, y2, color, width=width, dashed=edge.is_inter_pipeline)

    @staticmethod
    def _resolve_edge_ports(u: GraphNode, v: GraphNode, edge: GraphEdge) -> Tuple[GraphPort, GraphPort]:
        u_port = next((p for p in u.out_ports if p.id == edge.source_port), None)
        if u_port is None:
            u_port = u.out_ports[0] if u.out_ports else GraphPort("out0", "out", "out", rel_x=u.width, rel_y=u.height / 2.0)
        v_port = next((p for p in v.in_ports if p.id == edge.sink_port), None)
        if v_port is None:
            v_port = v.in_ports[0] if v.in_ports else GraphPort("in0", "in", "in", rel_x=0.0, rel_y=v.height / 2.0)
        return (u_port, v_port)

    def _snapshot_bezier_wire(
        self,
        snapshot: Gtk.Snapshot,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        color: Gdk.RGBA,
        width: float = 2.0,
        dashed: bool = False,
    ) -> None:
        dx = max(45.0, abs(x2 - x1) * 0.48)
        sign = 1.0 if x2 >= x1 else -1.0
        pb = Gsk.PathBuilder.new()
        pb.move_to(x1, y1)
        pb.cubic_to(x1 + sign * dx, y1, x2 - sign * dx, y2, x2, y2)
        path = pb.to_path()

        stroke = Gsk.Stroke.new(width)
        if dashed:
            stroke.set_dash([7.0, 4.0])
        snapshot.append_stroke(path, stroke, color)

        # Draw arrowhead at (x2, y2) pointing along the flow direction
        arrow_pb = Gsk.PathBuilder.new()
        arrow_pb.move_to(x2, y2)
        arrow_pb.line_to(x2 - sign * 9.0, y2 - 4.5)
        arrow_pb.line_to(x2 - sign * 9.0, y2 + 4.5)
        arrow_pb.close()
        snapshot.append_fill(arrow_pb.to_path(), Gsk.FillRule.WINDING, color)

    def _snapshot_hud(self, snapshot: Gtk.Snapshot, w: float, h: float) -> None:
        if not self.vm:
            return
        dirty_tag = " [Modified]" if self.vm.dirty else ""
        hud_text = (
            f"{self.vm.title}{dirty_tag}  •  {len(self.vm.nodes)} nodes, {len(self.vm.edges)} routes"
            f"  •  Zoom: {int(self.zoom * 100)}%  (Drag port to wire, Del to remove, F to fit)"
        )
        layout = self._get_pango_layout(hud_text, size_pt=9, bold=False)
        ink, logical = layout.get_pixel_extents()
        box_w = float(logical.width) + 20.0
        box_h = 26.0

        rect = Graphene.Rect.alloc()
        rect.init(12.0, h - box_h - 10.0, box_w, box_h)
        rrect = Gsk.RoundedRect()
        rrect.init_from_rect(rect, 6.0)
        snapshot.push_rounded_clip(rrect)
        snapshot.append_color(rgba(0.10, 0.12, 0.16, 0.88), rect)
        snapshot.pop()

        snapshot.save()
        pt = Graphene.Point.alloc()
        pt.init(22.0, h - box_h - 4.0)
        snapshot.translate(pt)
        snapshot.append_layout(layout, TEXT_MUTED)
        snapshot.restore()

    # -------------------------------------------------------------------------
    # Hit Testing & Interactive Editing Handlers
    # -------------------------------------------------------------------------

    def _hit_test_port(self, wx: float, wy: float, radius: float = 11.0) -> Optional[Tuple[str, str, str, float, float]]:
        if not self.vm:
            return None
        for nid, node in reversed(list(self.vm.nodes.items())):
            for p in node.out_ports:
                px = node.x + p.rel_x
                py = node.y + p.rel_y
                if math.hypot(wx - px, wy - py) <= radius:
                    return (nid, p.id, "out", px, py)
            for p in node.in_ports:
                px = node.x + p.rel_x
                py = node.y + p.rel_y
                if math.hypot(wx - px, wy - py) <= radius:
                    return (nid, p.id, "in", px, py)
        return None

    def _hit_test_node(self, wx: float, wy: float) -> Optional[str]:
        if not self.vm:
            return None
        for nid, node in reversed(list(self.vm.nodes.items())):
            if node.x <= wx <= node.x + node.width and node.y <= wy <= node.y + node.height:
                return nid
        return None

    def _hit_test_edge(self, wx: float, wy: float, threshold: float = 8.0) -> Optional[str]:
        if not self.vm:
            return None
        for edge in reversed(self.vm.edges):
            u = self.vm.nodes.get(edge.source_node)
            v = self.vm.nodes.get(edge.sink_node)
            if not u or not v:
                continue
            u_port, v_port = self._resolve_edge_ports(u, v, edge)
            x1 = u.x + u_port.rel_x
            y1 = u.y + u_port.rel_y
            x2 = v.x + v_port.rel_x
            y2 = v.y + v_port.rel_y
            dx = max(45.0, abs(x2 - x1) * 0.48)
            sign = 1.0 if x2 >= x1 else -1.0
            # Sample 16 points along cubic Bezier
            for step in range(17):
                t = step / 16.0
                mt = 1.0 - t
                bx = (mt**3) * x1 + 3 * (mt**2) * t * (x1 + sign * dx) + 3 * mt * (t**2) * (x2 - sign * dx) + (t**3) * x2
                by = (mt**3) * y1 + 3 * (mt**2) * t * y1 + 3 * mt * (t**2) * y2 + (t**3) * y2
                if math.hypot(wx - bx, wy - by) <= threshold:
                    return edge.id
        return None

    def _hit_test_container(self, wx: float, wy: float) -> Optional[int]:
        if not self.vm:
            return None
        for pid, cont in self.vm.containers.items():
            if cont.x <= wx <= cont.x + cont.width and cont.y <= wy <= cont.y + cont.height:
                return pid
        return None

    def _on_left_pressed(self, gesture: Gtk.GestureClick, n_press: int, sx: float, sy: float) -> None:
        self.grab_focus()
        if not self.vm:
            return
        wx, wy = self.screen_to_world(sx, sy)
        state = gesture.get_current_event_state()
        additive = bool(state & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK))

        # Check port first (don't change selection if starting a wire drag)
        if self._hit_test_port(wx, wy) is not None:
            return

        nid = self._hit_test_node(wx, wy)
        if nid:
            node = self.vm.nodes[nid]
            self.select_node(nid, additive=additive)
            if node.class_category == "TestbenchIO":
                lx = wx - node.x
                ly = wy - node.y
                # Header / Radio button click -> enable this TestbenchIO pipeline chain
                if 0.0 <= ly <= 26.0:
                    self.select_testbench_io_radio(nid)
                    return
                # Row 1: [ File ] vs [ ALSA PCM ] mode pills
                if 30.0 <= ly <= 51.0:
                    avail_w = node.width - 26.0
                    file_pw = round(avail_w * 0.46, 1)
                    if 10.0 <= lx <= 10.0 + file_pw:
                        if (node.config.get_scalar("io_type") or "").strip().lower() != "file":
                            self.record_undo_snapshot(f"Set {node.title} mode to File")
                            node.config.set_scalar("io_type", "file", origin="instance")
                            self.vm.dirty = True
                            self.select_node(nid)
                            if self.on_graph_modified:
                                self.on_graph_modified()
                            self.queue_draw()
                        return
                    if 16.0 + file_pw <= lx <= node.width - 10.0:
                        if (node.config.get_scalar("io_type") or "").strip().lower() != "alsa_pcm":
                            self.record_undo_snapshot(f"Set {node.title} mode to ALSA PCM")
                            node.config.set_scalar("io_type", "alsa_pcm", origin="instance")
                            self.vm.dirty = True
                            self.select_node(nid)
                            if self.on_graph_modified:
                                self.on_graph_modified()
                            self.queue_draw()
                        return
                # Row 2: Selector box click -> open FileChooser or ALSA PCM popover
                if 53.0 <= ly <= 75.0 and 10.0 <= lx <= node.width - 10.0:
                    io_type = (node.config.get_scalar("io_type") or "file").strip().lower()
                    if io_type == "alsa_pcm":
                        self._open_tb_alsa_popover(node, sx, sy)
                    else:
                        self._open_tb_file_chooser(node)
                    return
            return

        eid = self._hit_test_edge(wx, wy)
        if eid:
            self.selected_node_id = None
            self.selected_node_ids.clear()
            self.selected_edge_id = eid
            self.selected_container_id = None
            self.selected_container_ids.clear()
            edge = next((e for e in self.vm.edges if e.id == eid), None)
            if edge and self.on_selection_changed:
                self.on_selection_changed(
                    f"Route: {edge.source_node} ({edge.source_port}) → {edge.sink_node} ({edge.sink_port})",
                    edge.config,
                    "Object.Base.route",
                )
            self.queue_draw()
            return

        pid = self._hit_test_container(wx, wy)
        if pid is not None:
            self.selected_edge_id = None
            if additive:
                if pid in self.selected_container_ids:
                    self.selected_container_ids.remove(pid)
                    self.selected_container_id = next(iter(self.selected_container_ids), None)
                else:
                    self.selected_container_ids.add(pid)
                    self.selected_container_id = pid
            else:
                self.selected_node_id = None
                self.selected_node_ids.clear()
                self.selected_container_ids = {pid}
                self.selected_container_id = pid
            cont = self.vm.containers[pid]
            if self.on_selection_changed:
                self.on_selection_changed(
                    f"Pipeline {pid}: {cont.class_name}",
                    cont.config,
                    f"Direction: {cont.direction}",
                )
            self.queue_draw()
            return

        # Clicked empty background -> clear object selection and show topology/root attributes
        self.select_node(None)
        if self.on_selection_changed:
            self.on_selection_changed(self.vm.title, self.vm.root_config, self.vm.subtitle)
        self.queue_draw()

    def _on_drag_begin(self, gesture: Gtk.GestureDrag, sx: float, sy: float) -> None:
        if not self.vm:
            return
        self._drag_start_screen = (sx, sy)
        wx, wy = self.screen_to_world(sx, sy)
        self._drag_start_world = (wx, wy)

        port_hit = self._hit_test_port(wx, wy)
        if port_hit and port_hit[2] == "out":
            nid, pid, _, px, py = port_hit
            self._drag_mode = "wire"
            self._wire_source = (nid, pid, px, py)
            self._wire_cursor_world = (wx, wy)
            return

        nid = self._hit_test_node(wx, wy)
        if nid:
            self._drag_mode = "node"
            if nid not in self.selected_node_ids:
                self.select_node(nid)
            else:
                self.selected_node_id = nid
            self._pending_drag_history_entry = self._capture_history_entry(f"Move {nid}")
            node = self.vm.nodes[nid]
            self._drag_node_initial = (node.x, node.y)
            self._drag_children_initial = {
                cid: (cnode.x, cnode.y)
                for cid, cnode in self.vm.nodes.items()
                if cnode.parent_node_id == nid
            }
            self.set_cursor_from_name("move")
            return

        pid = self._hit_test_container(wx, wy)
        if pid is not None and pid in self.vm.containers:
            self._drag_mode = "container"
            self._drag_container_id = pid
            self._pending_drag_history_entry = self._capture_history_entry(f"Move Pipeline {pid}")
            cont = self.vm.containers[pid]
            self._drag_container_initial = (cont.x, cont.y)
            pipe_nids = {n.id for n in self.vm.nodes.values() if n.pipeline_id == pid}
            self._drag_children_initial = {
                cid: (cnode.x, cnode.y)
                for cid, cnode in self.vm.nodes.items()
                if cid in pipe_nids or (cnode.parent_node_id and cnode.parent_node_id in pipe_nids)
            }
            self.set_cursor_from_name("move")
            return

        self._drag_mode = "pan"
        self._drag_start_pan = (self.pan_x, self.pan_y)
        self.set_cursor_from_name("grabbing")

    def _on_drag_update(self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        if not self.vm:
            return
        if self._drag_mode == "pan":
            if abs(offset_x) > 2.0 or abs(offset_y) > 2.0:
                self._is_fitted = False
            self.pan_x = self._drag_start_pan[0] + offset_x
            self.pan_y = self._drag_start_pan[1] + offset_y
            self.queue_draw()
        elif self._drag_mode == "node" and self.selected_node_id in self.vm.nodes:
            dx = offset_x / self.zoom
            dy = offset_y / self.zoom
            node = self.vm.nodes[self.selected_node_id]
            node.x = self._drag_node_initial[0] + dx
            node.y = self._drag_node_initial[1] + dy
            for cid, (cx0, cy0) in self._drag_children_initial.items():
                if cid in self.vm.nodes:
                    self.vm.nodes[cid].x = cx0 + dx
                    self.vm.nodes[cid].y = cy0 + dy
            update_container_bounds_from_nodes(self.vm)
            self._schedule_drag_pause_preview(
                self._drag_start_screen[0] + offset_x,
                self._drag_start_screen[1] + offset_y,
            )
            self.queue_draw()
        elif self._drag_mode == "container" and self._drag_container_id is not None and self._drag_container_id in self.vm.containers:
            dx = offset_x / self.zoom
            dy = offset_y / self.zoom
            cont = self.vm.containers[self._drag_container_id]
            cont.x = self._drag_container_initial[0] + dx
            cont.y = self._drag_container_initial[1] + dy
            for cid, (cx0, cy0) in self._drag_children_initial.items():
                if cid in self.vm.nodes:
                    self.vm.nodes[cid].x = cx0 + dx
                    self.vm.nodes[cid].y = cy0 + dy
            self.queue_draw()
        elif self._drag_mode == "wire":
            wx = self._drag_start_world[0] + offset_x / self.zoom
            wy = self._drag_start_world[1] + offset_y / self.zoom
            self._wire_cursor_world = (wx, wy)
            self.queue_draw()

    def _on_drag_end(self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        self._clear_drop_preview()
        moved = abs(offset_x) > 1.0 or abs(offset_y) > 1.0
        if self.vm and self._drag_mode == "wire" and self._wire_source is not None:
            src_nid, src_pid, _, _ = self._wire_source
            wx = self._drag_start_world[0] + offset_x / self.zoom
            wy = self._drag_start_world[1] + offset_y / self.zoom
            port_hit = self._hit_test_port(wx, wy, radius=16.0)
            target_nid = port_hit[0] if (port_hit and port_hit[2] == "in") else self._hit_test_node(wx, wy)
            target_pid = port_hit[1] if (port_hit and port_hit[2] == "in") else None
            if target_nid and target_nid != src_nid:
                self._create_route_edge(src_nid, target_nid, src_port=src_pid, snk_port=target_pid)
        elif self.vm and self._drag_mode in ("node", "container"):
            if moved and self._pending_drag_history_entry is not None:
                self.history.push_undo_entry(self._pending_drag_history_entry)
                self.vm.dirty = True
            if self._drag_mode == "node" and self.selected_node_id in self.vm.nodes:
                self._suppress_auto_history = True
                try:
                    self._maybe_reparent_dragged_node(self.selected_node_id)
                finally:
                    self._suppress_auto_history = False
            if moved and self.on_graph_modified:
                self.on_graph_modified()
        self._pending_drag_history_entry = None
        self._drag_mode = "none"
        self._wire_source = None
        self.set_cursor(None)
        self.queue_draw()

    def _on_pan_begin(self, gesture: Gtk.GestureDrag, sx: float, sy: float) -> None:
        self._drag_start_pan = (self.pan_x, self.pan_y)
        self.set_cursor_from_name("grabbing")

    def _on_pan_update(self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        if abs(offset_x) > 2.0 or abs(offset_y) > 2.0:
            self._is_fitted = False
        self.pan_x = self._drag_start_pan[0] + offset_x
        self.pan_y = self._drag_start_pan[1] + offset_y
        self.queue_draw()

    def _on_pan_end(self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        self.set_cursor(None)

    # ------------------------------------------------------------------
    # Right Mouse Button Hold & Drag:
    # - If object(s) are selected: change pointer to "move" and drag selected object(s)
    # - If no object is selected: change pointer to "grabbing" and drag the whole view
    # ------------------------------------------------------------------
    def _on_right_pressed(self, gesture: Gtk.GestureClick, n_press: int, sx: float, sy: float) -> None:
        self.grab_focus()
        if self._has_selected_objects():
            self.set_cursor_from_name("move")
        else:
            self.set_cursor_from_name("grabbing")

    def _on_right_released(self, gesture: Gtk.GestureClick, n_press: int, sx: float, sy: float) -> None:
        if self._right_drag_mode == "none":
            self.set_cursor(None)

    def _capture_selected_objects_initial(self) -> None:
        self._right_drag_nodes_initial.clear()
        self._right_drag_containers_initial.clear()
        if not self.vm:
            return

        sel_nids: Set[str] = set(self.selected_node_ids)
        if self.selected_node_id and self.selected_node_id in self.vm.nodes:
            sel_nids.add(self.selected_node_id)

        sel_pids: Set[int] = set(self.selected_container_ids)
        if self.selected_container_id is not None and self.selected_container_id in self.vm.containers:
            sel_pids.add(self.selected_container_id)

        for pid in sel_pids:
            if pid in self.vm.containers:
                cont = self.vm.containers[pid]
                self._right_drag_containers_initial[pid] = (cont.x, cont.y)
                for n in self.vm.nodes.values():
                    if n.pipeline_id == pid:
                        sel_nids.add(n.id)

        # Include attached child nodes (kcontrols, testbench I/O) of any selected node
        all_nids = set(sel_nids)
        for cid, cnode in self.vm.nodes.items():
            if cnode.parent_node_id and cnode.parent_node_id in sel_nids:
                all_nids.add(cid)

        for nid in all_nids:
            if nid in self.vm.nodes:
                n = self.vm.nodes[nid]
                self._right_drag_nodes_initial[nid] = (n.x, n.y)

    def _on_right_drag_begin(self, gesture: Gtk.GestureDrag, sx: float, sy: float) -> None:
        if not self.vm:
            return
        self._drag_start_screen = (sx, sy)
        if self._has_selected_objects():
            self._right_drag_mode = "objects"
            self.set_cursor_from_name("move")
            self._pending_drag_history_entry = self._capture_history_entry("Move selected object(s)")
            self._capture_selected_objects_initial()
        else:
            self._right_drag_mode = "pan"
            self._drag_start_pan = (self.pan_x, self.pan_y)
            self.set_cursor_from_name("grabbing")

    def _on_right_drag_update(self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        if not self.vm:
            return
        if self._right_drag_mode == "objects":
            dx = offset_x / self.zoom
            dy = offset_y / self.zoom
            for pid, (cx0, cy0) in self._right_drag_containers_initial.items():
                if pid in self.vm.containers:
                    self.vm.containers[pid].x = cx0 + dx
                    self.vm.containers[pid].y = cy0 + dy
            for nid, (nx0, ny0) in self._right_drag_nodes_initial.items():
                if nid in self.vm.nodes:
                    self.vm.nodes[nid].x = nx0 + dx
                    self.vm.nodes[nid].y = ny0 + dy
            if not self._right_drag_containers_initial:
                update_container_bounds_from_nodes(self.vm)
            self._schedule_drag_pause_preview(
                self._drag_start_screen[0] + offset_x,
                self._drag_start_screen[1] + offset_y,
            )
            self.queue_draw()
        elif self._right_drag_mode == "pan":
            if abs(offset_x) > 2.0 or abs(offset_y) > 2.0:
                self._is_fitted = False
            self.pan_x = self._drag_start_pan[0] + offset_x
            self.pan_y = self._drag_start_pan[1] + offset_y
            self.queue_draw()

    def _on_right_drag_end(self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        self._clear_drop_preview()
        moved = abs(offset_x) > 1.0 or abs(offset_y) > 1.0
        if self.vm and self._right_drag_mode == "objects":
            if moved and self._pending_drag_history_entry is not None:
                self.history.push_undo_entry(self._pending_drag_history_entry)
                self.vm.dirty = True
            if not self._right_drag_containers_initial and self.selected_node_id in self.vm.nodes:
                self._suppress_auto_history = True
                try:
                    self._maybe_reparent_dragged_node(self.selected_node_id)
                finally:
                    self._suppress_auto_history = False
            if moved and self.on_graph_modified:
                self.on_graph_modified()
        self._pending_drag_history_entry = None
        self._right_drag_mode = "none"
        self._right_drag_nodes_initial.clear()
        self._right_drag_containers_initial.clear()
        self.set_cursor(None)
        self.queue_draw()

    def _on_motion_enter(self, controller: Gtk.EventControllerMotion, sx: float, sy: float) -> None:
        self._pointer_pos = (sx, sy)

    def _on_motion(self, controller: Gtk.EventControllerMotion, sx: float, sy: float) -> None:
        self._pointer_pos = (sx, sy)
        if not self.vm:
            return
        wx, wy = self.screen_to_world(sx, sy)
        hit = self._hit_test_port(wx, wy)
        new_hov = (hit[0], hit[1], hit[2]) if hit else None

        # Check if hovering over the yellow warning bang / header bar of a pipeline container with a warning
        new_warn_pid: Optional[int] = None
        for pid, cont in self.vm.containers.items():
            if cont.warning_text and cont.x <= wx <= cont.x + cont.width and cont.y <= wy <= cont.y + 38.0:
                new_warn_pid = pid
                break

        needs_redraw = False
        if new_hov != self.hovered_port:
            self.hovered_port = new_hov
            needs_redraw = True
        if new_warn_pid != self.hovered_warning_pid:
            self.hovered_warning_pid = new_warn_pid
            needs_redraw = True
        elif new_warn_pid is not None:
            needs_redraw = True
        if needs_redraw:
            self.queue_draw()

    def _on_scroll(self, controller: Gtk.EventControllerScroll, dx: float, dy: float) -> bool:
        if dy == 0.0:
            return False
        self._is_fitted = False
        factor = 1.12 if dy < 0 else (1.0 / 1.12)
        new_zoom = max(0.08, min(5.0, self.zoom * factor))

        # Anchor zoom on the mouse pointer position
        if self._pointer_pos is not None:
            px, py = self._pointer_pos
        else:
            px = float(self.get_width()) / 2.0
            py = float(self.get_height()) / 2.0

        wx, wy = self.screen_to_world(px, py)
        self.zoom = new_zoom
        self.pan_x = px - wx * self.zoom
        self.pan_y = py - wy * self.zoom
        self.queue_draw()
        return True

    def _on_key_pressed(self, controller: Gtk.EventControllerKey, keyval: int, keycode: int, state: Gdk.ModifierType) -> bool:
        ctrl_held = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift_held = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if ctrl_held and keyval in (Gdk.KEY_z, Gdk.KEY_Z):
            if shift_held:
                return self.redo()
            return self.undo()
        if ctrl_held and keyval in (Gdk.KEY_y, Gdk.KEY_Y):
            return self.redo()
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
            self.delete_selected()
            return True
        if keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.fit_to_view()
            return True
        if keyval in (Gdk.KEY_l, Gdk.KEY_L) and self.vm:
            self.relayout_graph()
            return True
        return False

    # -------------------------------------------------------------------------
    # Graph Mutation Operations (Add Node, Wire Route, Delete Selected)
    # -------------------------------------------------------------------------

    def _create_route_edge(
        self,
        src_nid: str,
        snk_nid: str,
        src_port: Optional[str] = None,
        snk_port: Optional[str] = None,
    ) -> None:
        if not self.vm:
            return
        if any(e.source_node == src_nid and e.sink_node == snk_nid for e in self.vm.edges):
            return
        self.record_undo_snapshot(f"Wire route {src_nid} → {snk_nid}")
        u = self.vm.nodes[src_nid]
        v = self.vm.nodes[snk_nid]
        if not src_port:
            used_out = {e.source_port for e in self.vm.edges if e.source_node == src_nid}
            free_out = next((p.id for p in u.out_ports if p.id not in used_out), None)
            src_port = free_out or (u.out_ports[0].id if u.out_ports else "out0")
        if not snk_port:
            used_in = {e.sink_port for e in self.vm.edges if e.sink_node == snk_nid}
            free_in = next((p.id for p in v.in_ports if p.id not in used_in), None)
            snk_port = free_in or (v.in_ports[0].id if v.in_ports else "in0")
        is_inter = (u.pipeline_id is not None and v.pipeline_id is not None and u.pipeline_id != v.pipeline_id)
        edge_cfg = ConfigNode("Object.Base.route", node_type="object")
        edge_cfg.set_scalar("source", src_nid, origin="instance")
        edge_cfg.set_scalar("sink", snk_nid, origin="instance")
        if u.pipeline_id is not None and not is_inter:
            edge_cfg.set_scalar("index", str(u.pipeline_id), origin="instance")
        new_edge = GraphEdge(
            id=f"user_edge_{len(self.vm.edges) + 1}",
            source_node=src_nid,
            sink_node=snk_nid,
            source_port=src_port,
            sink_port=snk_port,
            pipeline_id=u.pipeline_id if not is_inter else None,
            is_inter_pipeline=is_inter,
            config=edge_cfg,
        )
        self.vm.edges.append(new_edge)
        self.vm.dirty = True
        if self.on_graph_modified:
            self.on_graph_modified()
        self.queue_draw()

    def delete_selected(self) -> None:
        if not self.vm:
            return
        if self.selected_edge_id:
            self.record_undo_snapshot("Delete route")
            self.vm.edges = [e for e in self.vm.edges if e.id != self.selected_edge_id]
            self.selected_edge_id = None
            self.vm.dirty = True
            self._notify_current_selection()
            if self.on_graph_modified:
                self.on_graph_modified()
            self.queue_draw()
        elif self.selected_node_ids or (self.selected_node_id and self.selected_node_id in self.vm.nodes) or self.selected_container_ids or (self.selected_container_id is not None and self.selected_container_id in self.vm.containers):
            self.record_undo_snapshot("Delete selected object(s)")
            nids = set(self.selected_node_ids)
            if self.selected_node_id:
                nids.add(self.selected_node_id)
            pids = set(self.selected_container_ids)
            if self.selected_container_id is not None:
                pids.add(self.selected_container_id)

            to_remove = set()
            for nid in nids:
                if nid in self.vm.nodes:
                    to_remove.add(nid)
                    for cid, cn in self.vm.nodes.items():
                        if cn.parent_node_id == nid:
                            to_remove.add(cid)

            for pid in pids:
                if pid in self.vm.containers:
                    pipe_nids = {nid for nid, n in self.vm.nodes.items() if n.pipeline_id == pid}
                    to_remove |= pipe_nids
                    to_remove |= {
                        cid for cid, cn in self.vm.nodes.items()
                        if cn.parent_node_id and cn.parent_node_id in pipe_nids
                    }
                    self.vm.containers.pop(pid, None)

            for rid in to_remove:
                self.vm.nodes.pop(rid, None)
            self.vm.edges = [
                e for e in self.vm.edges
                if e.source_node not in to_remove and e.sink_node not in to_remove and e.pipeline_id not in pids
            ]
            self.selected_node_id = None
            self.selected_node_ids.clear()
            self.selected_container_id = None
            self.selected_container_ids.clear()
            self.vm.dirty = True
            update_container_bounds_from_nodes(self.vm)
            self._notify_current_selection()
            if self.on_graph_modified:
                self.on_graph_modified()
            self.queue_draw()

    def _find_target_pipeline_at(self, wx: Optional[float], wy: Optional[float]) -> Optional[int]:
        """Return the existing pipeline_id if (wx, wy) is placed inside an existing pipeline container or on one of its nodes/edges."""
        if not self.vm or wx is None or wy is None:
            return None
        pid = self._hit_test_container(wx, wy)
        if pid is not None and pid in self.vm.containers:
            return pid
        nid = self._hit_test_node(wx, wy)
        if nid and nid in self.vm.nodes:
            node = self.vm.nodes[nid]
            if node.pipeline_id is not None and node.pipeline_id in self.vm.containers:
                return node.pipeline_id
            if node.parent_node_id and node.parent_node_id in self.vm.nodes:
                parent = self.vm.nodes[node.parent_node_id]
                if parent.pipeline_id is not None and parent.pipeline_id in self.vm.containers:
                    return parent.pipeline_id
        eid = self._hit_test_edge(wx, wy)
        if eid:
            edge = next((e for e in self.vm.edges if e.id == eid), None)
            if edge and edge.pipeline_id is not None and edge.pipeline_id in self.vm.containers:
                return edge.pipeline_id
        return None

    def _shift_node_and_children(self, n: GraphNode, dx: float, dy: float = 0.0) -> None:
        n.x += dx
        n.y += dy
        if not self.vm:
            return
        for cnode in self.vm.nodes.values():
            if cnode.parent_node_id == n.id:
                cnode.x += dx
                cnode.y += dy

    def _join_module_to_pipeline(self, node: GraphNode, pid: int, wx: float, wy: float) -> None:
        """Join a newly placed module node into the existing widget graph of pipeline `pid`."""
        if not self.vm:
            return
        peers = [
            n for n in self.vm.nodes.values()
            if n.pipeline_id == pid
            and n.id != node.id
            and n.parent_node_id is None
            and n.class_category not in ("Control", "TestbenchIO")
        ]
        if not peers:
            return

        is_cap = _is_node_capture(self.vm, node)

        def flow_x(val: float) -> float:
            return -val if is_cap else val

        peer_ids = {p.id for p in peers}
        pipe_edges = [
            e for e in self.vm.edges
            if e.source_node in peer_ids and e.sink_node in peer_ids
        ]

        # 1. Check if dropped directly on an intra-pipeline edge or horizontally between two connected peers u -> v
        hit_eid = self._hit_test_edge(wx, wy)
        splice_edge = next((e for e in pipe_edges if e.id == hit_eid), None) if hit_eid else None
        if splice_edge is None and pipe_edges:
            best_dist = float("inf")
            for e in pipe_edges:
                u = self.vm.nodes[e.source_node]
                v = self.vm.nodes[e.sink_node]
                u_cx = u.x + u.width / 2.0
                v_cx = v.x + v.width / 2.0
                if min(u_cx, v_cx) <= wx <= max(u_cx, v_cx):
                    mid_y = (u.y + u.height / 2.0 + v.y + v.height / 2.0) / 2.0
                    dist_y = abs(wy - mid_y)
                    if dist_y < best_dist:
                        best_dist = dist_y
                        splice_edge = e

        if splice_edge is not None and node.in_ports and node.out_ports:
            u = self.vm.nodes[splice_edge.source_node]
            v = self.vm.nodes[splice_edge.sink_node]
            self.vm.edges = [e for e in self.vm.edges if e.id != splice_edge.id]
            if not is_cap:
                node.x = max(node.x, u.x + u.width + H_GAP)
                if abs(wy - (u.y + u.height / 2.0)) < max(u.height, v.height) * 1.2:
                    node.y = (u.y + v.y) / 2.0
                min_v_x = node.x + node.width + H_GAP
                if v.x < min_v_x:
                    shift_dx = min_v_x - v.x
                    v_x0 = v.x
                    for p in peers:
                        if p.x >= v_x0 - 5.0:
                            self._shift_node_and_children(p, shift_dx)
            else:
                node.x = min(node.x, u.x - node.width - H_GAP)
                if abs(wy - (u.y + u.height / 2.0)) < max(u.height, v.height) * 1.2:
                    node.y = (u.y + v.y) / 2.0
                max_v_x = node.x - v.width - H_GAP
                if v.x > max_v_x:
                    shift_dx = max_v_x - v.x
                    v_x0 = v.x
                    for p in peers:
                        if p.x <= v_x0 + 5.0:
                            self._shift_node_and_children(p, shift_dx)
            self._create_route_edge(u.id, node.id, src_port=splice_edge.source_port)
            self._create_route_edge(node.id, v.id, snk_port=splice_edge.sink_port)
            return

        # 2. Otherwise connect to the nearest upstream and/or downstream peer in `pid`
        sorted_peers = sorted(peers, key=lambda p: flow_x(p.x + p.width / 2.0))
        drop_fx = flow_x(wx)
        up_peers = [p for p in sorted_peers if flow_x(p.x + p.width / 2.0) <= drop_fx]
        down_peers = [p for p in sorted_peers if flow_x(p.x + p.width / 2.0) > drop_fx]

        if up_peers:
            u = up_peers[-1]
            if u.out_ports and node.in_ports:
                if not is_cap and node.x < u.x + u.width + H_GAP * 0.5 and abs(node.y - u.y) < max(node.height, u.height):
                    node.x = u.x + u.width + H_GAP
                    node.y = u.y
                elif is_cap and node.x + node.width > u.x - H_GAP * 0.5 and abs(node.y - u.y) < max(node.height, u.height):
                    node.x = u.x - node.width - H_GAP
                    node.y = u.y
                self._create_route_edge(u.id, node.id)

        if down_peers:
            v = down_peers[0]
            if node.out_ports and v.in_ports:
                if not up_peers:
                    if not is_cap and node.x + node.width > v.x - H_GAP * 0.5 and abs(node.y - v.y) < max(node.height, v.height):
                        node.x = v.x - node.width - H_GAP
                        node.y = v.y
                    elif is_cap and node.x < v.x + v.width + H_GAP * 0.5 and abs(node.y - v.y) < max(node.height, v.height):
                        node.x = v.x + v.width + H_GAP
                        node.y = v.y
                else:
                    if not is_cap and v.x < node.x + node.width + H_GAP:
                        shift_dx = (node.x + node.width + H_GAP) - v.x
                        v_x0 = v.x
                        for p in peers:
                            if p.x >= v_x0 - 5.0:
                                self._shift_node_and_children(p, shift_dx)
                    elif is_cap and v.x > node.x - v.width - H_GAP:
                        shift_dx = (node.x - v.width - H_GAP) - v.x
                        v_x0 = v.x
                        for p in peers:
                            if p.x <= v_x0 + 5.0:
                                self._shift_node_and_children(p, shift_dx)
                self._create_route_edge(node.id, v.id)

    def _join_pipelines(self, target_pid: int, new_pid: int, place_on_right: bool) -> None:
        """Create an inter-pipeline route edge joining `target_pid` and `new_pid`."""
        if not self.vm or target_pid not in self.vm.containers or new_pid not in self.vm.containers:
            return
        t_cont = self.vm.containers[target_pid]
        n_cont = self.vm.containers[new_pid]
        t_nodes = [
            n for n in self.vm.nodes.values()
            if n.pipeline_id == target_pid and n.parent_node_id is None and n.class_category not in ("Control", "TestbenchIO")
        ]
        n_nodes = [
            n for n in self.vm.nodes.values()
            if n.pipeline_id == new_pid and n.parent_node_id is None and n.class_category not in ("Control", "TestbenchIO")
        ]
        if not t_nodes or not n_nodes:
            return

        is_cap = "capture" in t_cont.direction.lower() or "capture" in n_cont.direction.lower()
        left_nodes = t_nodes if place_on_right else n_nodes
        right_nodes = n_nodes if place_on_right else t_nodes

        if not is_cap:
            # Playback: left pipeline's rightmost output -> right pipeline's leftmost input
            src_candidates = [n for n in left_nodes if n.out_ports]
            snk_candidates = [n for n in right_nodes if n.in_ports]
            if not src_candidates or not snk_candidates:
                return
            src_node = max(src_candidates, key=lambda n: (n.class_name == "module-copier", n.x + n.width))
            snk_node = min(snk_candidates, key=lambda n: (0 if n.class_name == "module-copier" else 1, n.x))
        else:
            # Capture: right pipeline's leftmost output -> left pipeline's rightmost input
            src_candidates = [n for n in right_nodes if n.out_ports]
            snk_candidates = [n for n in left_nodes if n.in_ports]
            if not src_candidates or not snk_candidates:
                return
            src_node = min(src_candidates, key=lambda n: (0 if n.class_name == "module-copier" else 1, n.x))
            snk_node = max(snk_candidates, key=lambda n: (n.class_name == "module-copier", n.x + n.width))

        self._create_route_edge(src_node.id, snk_node.id)

    def add_module_instance(self, module_name: str, wx: Optional[float] = None, wy: Optional[float] = None) -> Optional[str]:
        """Instantiate a new Processing Module (Class.Widget.<module_name>) onto the active graph.
        - If placed within an existing pipeline container at (wx, wy), join that existing pipeline and wire into it.
        - Otherwise, create its own new pipeline container.
        """
        if not self.vm or not self.workspace or module_name not in self.workspace.processing_modules:
            return None
        self.record_undo_snapshot(f"Add module {module_name}")
        self._suppress_auto_history = True
        try:
            cdef = self.workspace.processing_modules[module_name]

            target_pid: Optional[int] = None
            if wx is not None and wy is not None:
                target_pid = self._find_target_pipeline_at(wx, wy)
            else:
                if self.selected_container_id is not None and self.selected_container_id in self.vm.containers:
                    target_pid = self.selected_container_id
                elif (
                    self.selected_node_id
                    and self.selected_node_id in self.vm.nodes
                    and self.vm.nodes[self.selected_node_id].pipeline_id in self.vm.containers
                ):
                    target_pid = self.vm.nodes[self.selected_node_id].pipeline_id

            if target_pid is not None and target_pid in self.vm.containers:
                pid = target_pid
            else:
                used_pids = set(self.vm.containers.keys()) | {
                    n.pipeline_id for n in self.vm.nodes.values() if n.pipeline_id is not None
                }
                pid = 1
                while pid in used_pids:
                    pid += 1
                wdir = str(cdef.defaults.get("direction", "")).lower()
                wtype = str(cdef.defaults.get("type", "")).lower()
                pipe_dir = (
                    "capture"
                    if ("capture" in module_name.lower() or wdir == "capture" or wtype == "dai_out")
                    else "playback"
                )
                p_cfg = ConfigNode("Object.Widget.pipeline.1", node_type="object")
                p_cfg.set_scalar("index", str(pid), origin="instance", is_constructor=True)
                self.vm.containers[pid] = PipelineContainer(
                    pipeline_id=pid,
                    class_name="custom",
                    direction=pipe_dir,
                    enabled_by="Added from Object View",
                    config=p_cfg,
                )

            # Find unused instance number
            inst_num = 1
            while f"{module_name}.{pid}.{inst_num}" in self.vm.nodes:
                inst_num += 1

            node = self.workspace._instantiate_widget_node(
                wclass=module_name,
                winst=str(inst_num),
                pipeline_id=pid,
                pipeline_class=self.vm.containers[pid].class_name if pid in self.vm.containers else "custom",
                pipeline_direction=self.vm.containers[pid].direction if pid in self.vm.containers else "playback",
                override_stmts=[],
                defines=self.vm.defines or self.workspace.global_defines,
                source_file=cdef.source_file,
            )
            _compute_node_geometry(node, is_capture=_is_node_capture(self.vm, node))
            if wx is None or wy is None:
                if target_pid is not None and target_pid in self.vm.containers:
                    t_cont = self.vm.containers[target_pid]
                    wx = t_cont.x + t_cont.width - 10.0
                    wy = t_cont.y + t_cont.height / 2.0
                else:
                    wx, wy = self.screen_to_world(
                        float(self.get_width() or 600) / 2.0,
                        float(self.get_height() or 400) / 2.0,
                    )
            node.x = wx - node.width / 2.0
            node.y = wy - node.height / 2.0
            self.vm.nodes[node.id] = node

            if target_pid is not None:
                self._join_module_to_pipeline(node, pid, wx, wy)

            self.workspace.attach_widget_kcontrol_nodes(self.vm, node)
            ctrls = [
                cnode for cnode in self.vm.nodes.values()
                if cnode.parent_node_id == node.id and cnode.class_category != "TestbenchIO"
            ]
            if ctrls:
                for cnode in ctrls:
                    _compute_node_geometry(cnode)
                row_w = sum(cnode.width for cnode in ctrls) + max(0, len(ctrls) - 1) * CTRL_H_GAP
                ctrl_x = node.x + (node.width - row_w) / 2.0
                ctrl_y = node.y + node.height + CTRL_TOP_GAP
                for cnode in ctrls:
                    cnode.x = ctrl_x
                    cnode.y = ctrl_y
                    ctrl_x += cnode.width + CTRL_H_GAP

            self.vm.dirty = True
            update_container_bounds_from_nodes(self.vm)
            self.workspace.attach_host_copier_io_nodes(self.vm)
            for tb_node in self.vm.nodes.values():
                if tb_node.class_category == "TestbenchIO":
                    _compute_node_geometry(tb_node)
            _position_testbench_io_nodes(self.vm)
            _assign_edge_ports(self.vm)
            self.select_node(node.id)
            if self.on_graph_modified:
                self.on_graph_modified()
            self.queue_draw()
            return node.id
        finally:
            self._suppress_auto_history = False

    def add_pipeline_instance(self, pipeline_name: str, wx: Optional[float] = None, wy: Optional[float] = None) -> Optional[int]:
        """Instantiate a full Pipeline (Class.Pipeline.<pipeline_name>) onto the active graph.
        - If placed within an existing pipeline at (wx, wy), place adjacent to it and join them with an inter-pipeline route.
        - Otherwise, create its own standalone pipeline at (wx, wy).
        """
        if not self.vm or not self.workspace or pipeline_name not in self.workspace.pipeline_classes:
            return None
        self.record_undo_snapshot(f"Add pipeline {pipeline_name}")
        self._suppress_auto_history = True
        try:
            target_pid = self._find_target_pipeline_at(wx, wy)

            used_pids = set(self.vm.containers.keys()) | {
                n.pipeline_id for n in self.vm.nodes.values() if n.pipeline_id is not None
            }
            new_pid = 1
            while new_pid in used_pids:
                new_pid += 1

            sub_vm = self.workspace.build_graph_for_pipeline(
                pipeline_name,
                index=new_pid,
                include_testbench_io=True,
            )
            layout_graph(sub_vm)

            sub_cont = sub_vm.containers.get(new_pid)
            if sub_cont is not None:
                sub_min_x = sub_cont.x
                sub_min_y = sub_cont.y
                sub_max_x = sub_cont.x + sub_cont.width
                sub_max_y = sub_cont.y + sub_cont.height
            else:
                sub_xs = [n.x for n in sub_vm.nodes.values()]
                sub_ys = [n.y for n in sub_vm.nodes.values()]
                sub_x2s = [n.x + n.width for n in sub_vm.nodes.values()]
                sub_y2s = [n.y + n.height for n in sub_vm.nodes.values()]
                sub_min_x = min(sub_xs) if sub_xs else 0.0
                sub_min_y = min(sub_ys) if sub_ys else 0.0
                sub_max_x = max(sub_x2s) if sub_x2s else 300.0
                sub_max_y = max(sub_y2s) if sub_y2s else 180.0

            sub_cx = (sub_min_x + sub_max_x) / 2.0
            sub_cy = (sub_min_y + sub_max_y) / 2.0
            sub_w = sub_max_x - sub_min_x

            place_on_right = True
            if target_pid is not None and target_pid in self.vm.containers:
                t_cont = self.vm.containers[target_pid]
                t_nodes = [
                    n for n in self.vm.nodes.values()
                    if n.pipeline_id == target_pid and n.parent_node_id is None
                ]
                n_nodes = [
                    n for n in sub_vm.nodes.values()
                    if n.pipeline_id == new_pid and n.parent_node_id is None
                ]
                t_has_host = any(n.class_name == "host-copier" for n in t_nodes)
                t_has_dai = any(n.class_name in ("dai-copier", "alh-copier") for n in t_nodes)
                n_has_host = any(n.class_name == "host-copier" for n in n_nodes)
                n_has_dai = any(n.class_name in ("dai-copier", "alh-copier") for n in n_nodes)

                if t_has_host and n_has_dai:
                    place_on_right = True
                elif n_has_host and t_has_dai:
                    place_on_right = False
                elif wx is not None:
                    place_on_right = wx >= (t_cont.x + t_cont.width / 2.0)

                if place_on_right:
                    wx = t_cont.x + t_cont.width + CONTAINER_GAP_X + sub_w / 2.0
                else:
                    wx = t_cont.x - CONTAINER_GAP_X - sub_w / 2.0
                wy = t_cont.y + t_cont.height / 2.0
            elif wx is None or wy is None:
                if self.vm.nodes or self.vm.containers:
                    g_min_x, g_min_y, g_w, g_h = self._get_graph_bounds()
                    wx = g_min_x + g_w / 2.0
                    wy = g_min_y + g_h + 64.0 + (sub_max_y - sub_min_y) / 2.0
                else:
                    wx, wy = self.screen_to_world(
                        float(self.get_width() or 600) / 2.0,
                        float(self.get_height() or 400) / 2.0,
                    )

            dx = wx - sub_cx
            dy = wy - sub_cy

            existing_enabled_sides = {
                str(n.config.get_scalar("side", "left")).lower()
                for n in self.vm.nodes.values()
                if n.class_category == "TestbenchIO"
                and str(n.config.get_scalar("enabled", "false")).lower() in ("true", "1", "yes")
            }

            for cid, cont in sub_vm.containers.items():
                cont.x += dx
                cont.y += dy
                cont.enabled_by = "Added from Object View"
                cont.config.set_scalar("index", str(new_pid), origin="instance", is_constructor=True)
                self.vm.containers[cid] = cont

            for nid, node in sub_vm.nodes.items():
                node.x += dx
                node.y += dy
                if node.class_category == "TestbenchIO":
                    side = str(node.config.get_scalar("side", "left")).lower()
                    if side in existing_enabled_sides:
                        node.config.set_scalar("enabled", "false", origin="instance")
                self.vm.nodes[nid] = node

            for edge in sub_vm.edges:
                edge.id = f"pipe_{new_pid}_{edge.id}"
                self.vm.edges.append(edge)

            if target_pid is not None and target_pid in self.vm.containers:
                self._join_pipelines(target_pid, new_pid, place_on_right=place_on_right)
                self.workspace.attach_host_copier_io_nodes(self.vm)
                _position_testbench_io_nodes(self.vm)
                _assign_edge_ports(self.vm)

            self.vm.dirty = True
            self.selected_node_id = None
            self.selected_node_ids.clear()
            self.selected_edge_id = None
            self.selected_container_id = new_pid
            self.selected_container_ids = {new_pid}
            if new_pid in self.vm.containers and self.on_selection_changed:
                cont = self.vm.containers[new_pid]
                self.on_selection_changed(
                    f"Pipeline {new_pid}: {cont.class_name}",
                    cont.config,
                    f"Direction: {cont.direction}",
                )
            if self.on_graph_modified:
                self.on_graph_modified()
            self.queue_draw()
            return new_pid
        finally:
            self._suppress_auto_history = False

    # -------------------------------------------------------------------------
    # Testbench IO Node Radio Selection & File / ALSA PCM Pickers
    # -------------------------------------------------------------------------

    def select_testbench_io_radio(self, active_nid: str) -> None:
        """Select a TestbenchIO node in the radio group to enable its downstream/upstream pipelines."""
        if not self.vm or active_nid not in self.vm.nodes:
            return
        active_node = self.vm.nodes[active_nid]
        self.record_undo_snapshot(f"Enable {active_node.title}")
        active_side = str(active_node.config.get_scalar("side", "left")).lower()
        active_pids = self._parse_pipe_ids(str(active_node.config.get_scalar("target_pipelines", "")))

        # 1. Enable only active_nid on active_side
        for nid, n in self.vm.nodes.items():
            if n.class_category == "TestbenchIO" and str(n.config.get_scalar("side", "left")).lower() == active_side:
                n.config.set_scalar("enabled", "true" if nid == active_nid else "false", origin="instance")

        # 2. Synchronize connected TestbenchIO endpoint(s) on the opposite side
        opp_side = "right" if active_side == "left" else "left"
        opp_nodes = [
            n for n in self.vm.nodes.values()
            if n.class_category == "TestbenchIO"
            and str(n.config.get_scalar("side", "left")).lower() == opp_side
        ]
        for n in opp_nodes:
            parent = self.vm.nodes.get(n.parent_node_id or "")
            parent_pid = parent.pipeline_id if parent else None
            is_connected = parent_pid in active_pids if parent_pid is not None else bool(
                self._parse_pipe_ids(str(n.config.get_scalar("target_pipelines", ""))) & active_pids
            )
            n.config.set_scalar("enabled", "true" if is_connected else "false", origin="instance")

        self.vm.dirty = True
        self.select_node(active_nid)
        if self.on_graph_modified:
            self.on_graph_modified()
        self.queue_draw()

    @staticmethod
    def _discover_alsa_pcms(role: str) -> List[str]:
        """Discover available ALSA PCM device names for source (playback) or sink (capture)."""
        devices: List[str] = ["default", "hw:0,0", "plughw:0,0", "sysdefault", "null"]
        cmd = "arecord" if "sink" in role.lower() else "aplay"
        try:
            proc = subprocess.run(
                [cmd, "-L"],
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                for line in proc.stdout.splitlines():
                    if line and not line[0].isspace():
                        dev = line.strip()
                        if dev and dev not in devices:
                            devices.append(dev)
        except Exception:
            pass
        return devices[:24]

    def _open_tb_file_chooser(self, node: GraphNode) -> None:
        """Open a GTK4 FileDialog to choose a source or sink audio file for a TestbenchIO node."""
        role = (node.config.get_scalar("role") or "Playback Source").strip().lower()
        is_sink = "sink" in role or node.class_name == "testbench-sink"
        dialog = Gtk.FileDialog.new()
        curr_path = (node.config.get_scalar("file_path") or "").strip()
        if curr_path:
            parent_dir = os.path.dirname(os.path.abspath(curr_path))
            if os.path.isdir(parent_dir):
                dialog.set_initial_folder(Gio.File.new_for_path(parent_dir))
            if os.path.basename(curr_path):
                dialog.set_initial_name(os.path.basename(curr_path))

        root = self.get_root()
        win = root if isinstance(root, Gtk.Window) else None

        def _on_done(dlg: Gtk.FileDialog, res: Gio.AsyncResult) -> None:
            try:
                gfile = dlg.save_finish(res) if is_sink else dlg.open_finish(res)
                if gfile and gfile.get_path():
                    self.record_undo_snapshot(f"Set {node.title} file")
                    node.config.set_scalar("file_path", gfile.get_path(), origin="instance")
                    node.config.set_scalar("io_type", "file", origin="instance")
                    if self.vm:
                        self.vm.dirty = True
                    self.select_node(node.id)
                    if self.on_graph_modified:
                        self.on_graph_modified()
                    self.queue_draw()
            except Exception:
                pass

        if is_sink:
            dialog.set_title(f"Select Testbench Sink Output File ({node.title})")
            dialog.save(win, None, _on_done)
        else:
            dialog.set_title(f"Select Testbench Source Input File ({node.title})")
            dialog.open(win, None, _on_done)

    def _open_tb_alsa_popover(self, node: GraphNode, sx: float, sy: float) -> None:
        """Open a GTK4 Popover at (sx, sy) to pick or enter an ALSA PCM device or file path."""
        role = (node.config.get_scalar("role") or "Playback Source").strip().lower()
        is_sink = "sink" in role or node.class_name == "testbench-sink"
        pop = Gtk.Popover()
        pop.set_parent(self)
        rect = Gdk.Rectangle()
        rect.x = int(sx)
        rect.y = int(sy)
        rect.width = 1
        rect.height = 1
        pop.set_pointing_to(rect)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)

        title = Gtk.Label(
            label=f"ALSA PCM {'Sink (Capture Output)' if is_sink else 'Source (Playback Input)'} Device",
            xalign=0.0,
        )
        title.add_css_class("heading")
        box.append(title)

        entry = Gtk.Entry()
        entry.set_text(node.config.get_scalar("alsa_pcm") or "default")
        entry.set_width_chars(24)
        box.append(entry)

        devices = self._discover_alsa_pcms(role)
        dd = Gtk.DropDown.new_from_strings(devices)
        curr_pcm = node.config.get_scalar("alsa_pcm") or "default"
        if curr_pcm in devices:
            dd.set_selected(devices.index(curr_pcm))

        def _on_dd_changed(dropdown: Gtk.DropDown, _pspec: object) -> None:
            idx = dropdown.get_selected()
            if 0 <= idx < len(devices):
                entry.set_text(devices[idx])

        dd.connect("notify::selected", _on_dd_changed)
        box.append(dd)

        apply_btn = Gtk.Button(label="Apply ALSA PCM")
        apply_btn.add_css_class("suggested-action")

        def _apply(_w: Gtk.Widget) -> None:
            val = entry.get_text().strip() or "default"
            self.record_undo_snapshot(f"Set {node.title} ALSA PCM")
            node.config.set_scalar("alsa_pcm", val, origin="instance")
            node.config.set_scalar("io_type", "alsa_pcm", origin="instance")
            if self.vm:
                self.vm.dirty = True
            self.select_node(node.id)
            if self.on_graph_modified:
                self.on_graph_modified()
            self.queue_draw()
            pop.popdown()

        entry.connect("activate", _apply)
        apply_btn.connect("clicked", _apply)
        box.append(apply_btn)

        pop.set_child(box)
        pop.connect("closed", lambda p: p.unparent())
        pop.popup()

    # -------------------------------------------------------------------------
    # Drag-Pause Drop Outcome Preview & Drag-and-Drop Handlers
    # -------------------------------------------------------------------------

    def _cancel_drag_pause_timer(self) -> None:
        if self._drag_pause_timer_id is not None:
            GLib.source_remove(self._drag_pause_timer_id)
            self._drag_pause_timer_id = None

    def _clear_drop_preview(self) -> None:
        self._cancel_drag_pause_timer()
        if self.drop_preview is not None:
            self.drop_preview = None
            self.queue_draw()

    def _schedule_drag_pause_preview(self, sx: float, sy: float, payload: Optional[str] = None) -> None:
        """Schedule a brief pause timer; if mouse stops moving for DRAG_PAUSE_DELAY_MS, compute & render drop preview."""
        if (
            self._dnd_screen_pos is not None
            and math.hypot(sx - self._dnd_screen_pos[0], sy - self._dnd_screen_pos[1]) < 2.0
            and (self._drag_pause_timer_id is not None or self.drop_preview is not None)
        ):
            return
        self._dnd_screen_pos = (sx, sy)
        if payload:
            self._dnd_payload = payload
        if self.drop_preview is not None:
            self.drop_preview = None
            self.queue_draw()
        self._cancel_drag_pause_timer()
        self._drag_pause_timer_id = GLib.timeout_add(DRAG_PAUSE_DELAY_MS, self._on_drag_pause_timeout)

    def _on_drag_pause_timeout(self) -> bool:
        self._drag_pause_timer_id = None
        if self._dnd_screen_pos is None or not self.vm:
            return GLib.SOURCE_REMOVE
        sx, sy = self._dnd_screen_pos
        payload = self._dnd_payload or TopologyGraphCanvas._active_drag_payload
        if payload:
            self.drop_preview = self._compute_drop_preview(payload, sx, sy)
        elif self._drag_mode == "node" and self.selected_node_id in self.vm.nodes:
            self.drop_preview = self._compute_canvas_node_drag_preview(self.selected_node_id, sx, sy)
        elif (
            self._right_drag_mode == "objects"
            and not self._right_drag_containers_initial
            and self.selected_node_id in self.vm.nodes
        ):
            self.drop_preview = self._compute_canvas_node_drag_preview(self.selected_node_id, sx, sy)
        if self.drop_preview is not None:
            self.queue_draw()
        return GLib.SOURCE_REMOVE

    def _resolve_dnd_payload(self, target: Optional[Gtk.DropTarget]) -> Optional[str]:
        if target is not None:
            try:
                val = target.get_value()
                if isinstance(val, str) and val:
                    return val
                if isinstance(val, GObject.Value):
                    s = val.get_string()
                    if s:
                        return s
            except Exception:
                pass
        return self._dnd_payload or TopologyGraphCanvas._active_drag_payload

    def _compute_drop_preview(self, payload: str, sx: float, sy: float) -> Optional[DropOutcomePreview]:
        """Compute the visual and textual drop outcome if `payload` is dropped at screen position `(sx, sy)`."""
        if not self.vm or not self.workspace or not payload:
            return None
        wx, wy = self.screen_to_world(sx, sy)

        if payload.startswith("module:"):
            module_name = payload.split(":", 1)[1]
            cdef = self.workspace.processing_modules.get(module_name)
            if not cdef:
                return None
            nw, nh = 200.0, 86.0
            target_pid = self._find_target_pipeline_at(wx, wy)
            if target_pid is not None and target_pid in self.vm.containers:
                t_cont = self.vm.containers[target_pid]
                inst_num = 1
                while f"{module_name}.{target_pid}.{inst_num}" in self.vm.nodes:
                    inst_num += 1
                preview_nid = f"{module_name}.{target_pid}.{inst_num}"
                is_cap = "capture" in t_cont.direction.lower() or "capture" in module_name.lower()
                nx = wx - nw / 2.0
                ny = wy - nh / 2.0

                peers = [
                    n for n in self.vm.nodes.values()
                    if n.pipeline_id == target_pid
                    and n.parent_node_id is None
                    and n.class_category not in ("Control", "TestbenchIO")
                ]
                peer_ids = {p.id for p in peers}
                pipe_edges = [
                    e for e in self.vm.edges
                    if e.source_node in peer_ids and e.sink_node in peer_ids
                ]
                hit_eid = self._hit_test_edge(wx, wy)
                splice_edge = next((e for e in pipe_edges if e.id == hit_eid), None) if hit_eid else None
                if splice_edge is None and pipe_edges:
                    best_dist = float("inf")
                    for e in pipe_edges:
                        u = self.vm.nodes[e.source_node]
                        v = self.vm.nodes[e.sink_node]
                        u_cx = u.x + u.width / 2.0
                        v_cx = v.x + v.width / 2.0
                        if min(u_cx, v_cx) <= wx <= max(u_cx, v_cx):
                            mid_y = (u.y + u.height / 2.0 + v.y + v.height / 2.0) / 2.0
                            dist_y = abs(wy - mid_y)
                            if dist_y < best_dist:
                                best_dist = dist_y
                                splice_edge = e

                ghost_wires: List[Tuple[float, float, float, float, bool]] = []
                in_p = int(str(cdef.defaults.get("num_input_pins", 1)) or 1)
                out_p = int(str(cdef.defaults.get("num_output_pins", 1)) or 1)

                if splice_edge is not None and in_p > 0 and out_p > 0:
                    u = self.vm.nodes[splice_edge.source_node]
                    v = self.vm.nodes[splice_edge.sink_node]
                    if abs(wy - (u.y + u.height / 2.0)) < max(u.height, v.height) * 1.2:
                        ny = (u.y + v.y) / 2.0
                    u_out_x = u.x if is_cap else (u.x + u.width)
                    u_out_y = u.y + u.height / 2.0
                    n_in_x = (nx + nw) if is_cap else nx
                    n_in_y = ny + nh / 2.0
                    n_out_x = nx if is_cap else (nx + nw)
                    n_out_y = ny + nh / 2.0
                    v_in_x = (v.x + v.width) if is_cap else v.x
                    v_in_y = v.y + v.height / 2.0
                    ghost_wires.append((u_out_x, u_out_y, n_in_x, n_in_y, False))
                    ghost_wires.append((n_out_x, n_out_y, v_in_x, v_in_y, False))
                    detail = f"Splice {preview_nid} into route: {u.id} → {preview_nid} → {v.id}"
                elif peers:
                    def flow_x(val: float) -> float:
                        return -val if is_cap else val

                    sorted_peers = sorted(peers, key=lambda p: flow_x(p.x + p.width / 2.0))
                    drop_fx = flow_x(wx)
                    up_peers = [p for p in sorted_peers if flow_x(p.x + p.width / 2.0) <= drop_fx]
                    down_peers = [p for p in sorted_peers if flow_x(p.x + p.width / 2.0) > drop_fx]
                    wired_parts: List[str] = []
                    if up_peers and in_p > 0:
                        u = up_peers[-1]
                        if u.out_ports:
                            if not is_cap and nx < u.x + u.width + H_GAP * 0.5 and abs(ny - u.y) < max(nh, u.height):
                                nx = u.x + u.width + H_GAP
                                ny = u.y
                            elif is_cap and nx + nw > u.x - H_GAP * 0.5 and abs(ny - u.y) < max(nh, u.height):
                                nx = u.x - nw - H_GAP
                                ny = u.y
                            u_out_x = u.x if is_cap else (u.x + u.width)
                            u_out_y = u.y + u.height / 2.0
                            n_in_x = (nx + nw) if is_cap else nx
                            n_in_y = ny + nh / 2.0
                            ghost_wires.append((u_out_x, u_out_y, n_in_x, n_in_y, False))
                            wired_parts.append(f"{u.id} → {preview_nid}")
                    if down_peers and out_p > 0:
                        v = down_peers[0]
                        if v.in_ports:
                            if not up_peers:
                                if not is_cap and nx + nw > v.x - H_GAP * 0.5 and abs(ny - v.y) < max(nh, v.height):
                                    nx = v.x - nw - H_GAP
                                    ny = v.y
                                elif is_cap and nx < v.x + v.width + H_GAP * 0.5 and abs(ny - v.y) < max(nh, v.height):
                                    nx = v.x + v.width + H_GAP
                                    ny = v.y
                            n_out_x = nx if is_cap else (nx + nw)
                            n_out_y = ny + nh / 2.0
                            v_in_x = (v.x + v.width) if is_cap else v.x
                            v_in_y = v.y + v.height / 2.0
                            ghost_wires.append((n_out_x, n_out_y, v_in_x, v_in_y, False))
                            wired_parts.append(f"{preview_nid} → {v.id}")
                    detail = (
                        f"Add {preview_nid} & wire {', '.join(wired_parts)}"
                        if wired_parts
                        else f"Add {preview_nid} to Pipeline {target_pid}"
                    )
                else:
                    detail = f"Add {preview_nid} as first widget in Pipeline {target_pid}"

                return DropOutcomePreview(
                    action_kind="join_pipeline",
                    headline=f"Join Existing Pipeline {target_pid} ({t_cont.class_name})",
                    detail=detail,
                    sx=sx,
                    sy=sy,
                    wx=wx,
                    wy=wy,
                    target_pid=target_pid,
                    ghost_node=(nx, ny, nw, nh, preview_nid, f"Class.Widget.{module_name}"),
                    ghost_wires=ghost_wires,
                    spliced_edge_id=splice_edge.id if splice_edge else None,
                )

            # Outside existing pipelines -> Create its own new pipeline
            used_pids = set(self.vm.containers.keys()) | {
                n.pipeline_id for n in self.vm.nodes.values() if n.pipeline_id is not None
            }
            new_pid = 1
            while new_pid in used_pids:
                new_pid += 1
            wdir = str(cdef.defaults.get("direction", "")).lower()
            wtype = str(cdef.defaults.get("type", "")).lower()
            pipe_dir = (
                "capture"
                if ("capture" in module_name.lower() or wdir == "capture" or wtype == "dai_out")
                else "playback"
            )
            preview_nid = f"{module_name}.{new_pid}.1"
            nx = wx - nw / 2.0
            ny = wy - nh / 2.0
            gx = nx - 26.0
            gy = ny - 44.0
            gw = nw + 52.0
            gh = nh + 66.0
            return DropOutcomePreview(
                action_kind="create_pipeline",
                headline=f"Create New Pipeline {new_pid} ({pipe_dir})",
                detail=f"Outside existing pipelines: create Pipeline {new_pid} with {preview_nid}",
                sx=sx,
                sy=sy,
                wx=wx,
                wy=wy,
                ghost_container=(
                    gx,
                    gy,
                    gw,
                    gh,
                    f"+ New Pipeline {new_pid}: custom  [{pipe_dir}]",
                    pipe_dir == "capture",
                ),
                ghost_node=(nx, ny, nw, nh, preview_nid, f"Class.Widget.{module_name}"),
            )

        if payload.startswith("pipeline:"):
            pipeline_name = payload.split(":", 1)[1]
            pdef = self.workspace.pipeline_classes.get(pipeline_name)
            if not pdef:
                return None
            used_pids = set(self.vm.containers.keys()) | {
                n.pipeline_id for n in self.vm.nodes.values() if n.pipeline_id is not None
            }
            new_pid = 1
            while new_pid in used_pids:
                new_pid += 1

            w_classes = [w.get("class_name", "") for w in pdef.widgets if w.get("class_name") != "pipeline"]
            wcount = max(1, len(w_classes))
            est_w = wcount * 200.0 + max(0, wcount - 1) * H_GAP + 52.0
            est_h = 175.0
            default_dir = "capture" if "capture" in pipeline_name else "playback"
            pipe_dir = str(pdef.defaults.get("direction", default_dir))
            is_cap = "capture" in pipe_dir.lower()

            target_pid = self._find_target_pipeline_at(wx, wy)
            if target_pid is not None and target_pid in self.vm.containers:
                t_cont = self.vm.containers[target_pid]
                t_nodes = [
                    n for n in self.vm.nodes.values()
                    if n.pipeline_id == target_pid and n.parent_node_id is None and n.class_category not in ("Control", "TestbenchIO")
                ]
                t_has_host = any(n.class_name == "host-copier" for n in t_nodes)
                t_has_dai = any(n.class_name in ("dai-copier", "alh-copier") for n in t_nodes)
                n_has_host = "host-copier" in w_classes
                n_has_dai = ("dai-copier" in w_classes) or ("alh-copier" in w_classes)

                if t_has_host and n_has_dai:
                    place_on_right = True
                elif n_has_host and t_has_dai:
                    place_on_right = False
                else:
                    place_on_right = wx >= (t_cont.x + t_cont.width / 2.0)

                if place_on_right:
                    gx = t_cont.x + t_cont.width + CONTAINER_GAP_X
                    x1 = t_cont.x + t_cont.width - 26.0
                    x2 = gx + 26.0
                else:
                    gx = t_cont.x - CONTAINER_GAP_X - est_w
                    x1 = gx + est_w - 26.0
                    x2 = t_cont.x + 26.0
                gy = t_cont.y + (t_cont.height - est_h) / 2.0
                mid_y = t_cont.y + t_cont.height / 2.0
                ghost_wires = [(x2, mid_y, x1, mid_y, True)] if is_cap else [(x1, mid_y, x2, mid_y, True)]
                side_str = "right" if place_on_right else "left"
                return DropOutcomePreview(
                    action_kind="join_pipeline",
                    headline=f"Join to Existing Pipeline {target_pid} ({t_cont.class_name})",
                    detail=f"Place Pipeline {new_pid} ({pipeline_name}) on {side_str} & wire inter-pipeline route",
                    sx=sx,
                    sy=sy,
                    wx=wx,
                    wy=wy,
                    target_pid=target_pid,
                    ghost_container=(
                        gx,
                        gy,
                        est_w,
                        est_h,
                        f"+ Pipeline {new_pid}: {pipeline_name}  [{pipe_dir}]",
                        is_cap,
                    ),
                    ghost_wires=ghost_wires,
                )

            gx = wx - est_w / 2.0
            gy = wy - est_h / 2.0
            return DropOutcomePreview(
                action_kind="create_pipeline",
                headline=f"Create New Pipeline {new_pid}: {pipeline_name}",
                detail=f"Outside existing pipelines: instantiate standalone {pipe_dir} pipeline ({wcount} widgets)",
                sx=sx,
                sy=sy,
                wx=wx,
                wy=wy,
                ghost_container=(
                    gx,
                    gy,
                    est_w,
                    est_h,
                    f"+ New Pipeline {new_pid}: {pipeline_name}  [{pipe_dir}]",
                    is_cap,
                ),
            )

        return None

    def _find_other_pipeline_for_node(self, node: GraphNode) -> Optional[int]:
        """Check if `node`'s center lies inside a different pipeline container (`pid != node.pipeline_id`)."""
        if not self.vm:
            return None
        cx = node.x + node.width / 2.0
        cy = node.y + node.height / 2.0
        for pid, cont in self.vm.containers.items():
            if pid == node.pipeline_id:
                continue
            if cont.x <= cx <= cont.x + cont.width and cont.y <= cy <= cont.y + cont.height:
                return pid
        return None

    def _compute_canvas_node_drag_preview(self, nid: str, sx: float, sy: float) -> Optional[DropOutcomePreview]:
        """Compute drop outcome preview when dragging an existing node `nid` on the canvas."""
        if not self.vm or nid not in self.vm.nodes:
            return None
        node = self.vm.nodes[nid]
        if node.class_category in ("Control", "TestbenchIO"):
            return None
        wx = node.x + node.width / 2.0
        wy = node.y + node.height / 2.0
        other_pid = self._find_other_pipeline_for_node(node)
        if other_pid is not None and other_pid in self.vm.containers:
            t_cont = self.vm.containers[other_pid]
            return DropOutcomePreview(
                action_kind="join_pipeline",
                headline=f"Join Existing Pipeline {other_pid} ({t_cont.class_name})",
                detail=f"Move {node.id} into Pipeline {other_pid} and connect into its widget chain",
                sx=sx,
                sy=sy,
                wx=wx,
                wy=wy,
                target_pid=other_pid,
            )
        if node.pipeline_id is not None and node.pipeline_id in self.vm.containers:
            cont = self.vm.containers[node.pipeline_id]
            return DropOutcomePreview(
                action_kind="reposition",
                headline=f"Reposition {node.id} in Pipeline {node.pipeline_id} ({cont.class_name})",
                detail=f"Keep in Pipeline {node.pipeline_id} at ({node.x:.0f}, {node.y:.0f})",
                sx=sx,
                sy=sy,
                wx=wx,
                wy=wy,
                target_pid=node.pipeline_id,
            )
        return None

    def _maybe_reparent_dragged_node(self, nid: str) -> None:
        """If an existing node was dragged into another pipeline container, move it into that pipeline and wire it."""
        if not self.vm or nid not in self.vm.nodes:
            return
        node = self.vm.nodes[nid]
        if node.class_category in ("Control", "TestbenchIO"):
            return
        other_pid = self._find_other_pipeline_for_node(node)
        if other_pid is None or other_pid == node.pipeline_id:
            return
        old_pid = node.pipeline_id
        node.pipeline_id = other_pid
        if old_pid is not None and old_pid in self.vm.containers:
            remaining = [n for n in self.vm.nodes.values() if n.pipeline_id == old_pid]
            if not remaining:
                self.vm.containers.pop(old_pid, None)
        has_edges = any(
            (e.source_node == nid or e.sink_node == nid)
            and self.vm.nodes.get(e.source_node, node).class_category not in ("Control", "TestbenchIO")
            and self.vm.nodes.get(e.sink_node, node).class_category not in ("Control", "TestbenchIO")
            for e in self.vm.edges
        )
        if not has_edges:
            self._join_module_to_pipeline(node, other_pid, node.x + node.width / 2.0, node.y + node.height / 2.0)
        update_container_bounds_from_nodes(self.vm)
        if self.workspace:
            self.workspace.attach_host_copier_io_nodes(self.vm)
        _position_testbench_io_nodes(self.vm)
        _assign_edge_ports(self.vm)
        self.vm.dirty = True
        if self.on_graph_modified:
            self.on_graph_modified()

    def _on_dnd_enter(self, target: Gtk.DropTarget, x: float, y: float) -> Gdk.DragAction:
        payload = self._resolve_dnd_payload(target)
        self._schedule_drag_pause_preview(x, y, payload)
        return Gdk.DragAction.COPY

    def _on_dnd_motion(self, target: Gtk.DropTarget, x: float, y: float) -> Gdk.DragAction:
        payload = self._resolve_dnd_payload(target)
        self._schedule_drag_pause_preview(x, y, payload)
        return Gdk.DragAction.COPY

    def _on_dnd_leave(self, target: Gtk.DropTarget) -> None:
        self._dnd_payload = None
        self._dnd_screen_pos = None
        self._clear_drop_preview()

    def _on_drop_item(self, target: Gtk.DropTarget, value: str, x: float, y: float) -> bool:
        self._dnd_payload = None
        self._dnd_screen_pos = None
        TopologyGraphCanvas._active_drag_payload = None
        self._clear_drop_preview()
        if not value:
            return False
        wx, wy = self.screen_to_world(x, y)
        if value.startswith("module:"):
            mod_name = value.split(":", 1)[1]
            self.add_module_instance(mod_name, wx, wy)
            return True
        if value.startswith("pipeline:"):
            pipe_name = value.split(":", 1)[1]
            self.add_pipeline_instance(pipe_name, wx, wy)
            return True
        return False


class MiniGraphPreview(TopologyGraphCanvas):
    """Scaled miniature (or hover-zoomed) visual preview of a Module or Pipeline graph."""

    def __init__(self, vm: GraphViewModel, zoom_card: bool = False) -> None:
        super().__init__()
        self.set_focusable(False)
        self.set_can_target(False)
        self.is_row_selected: bool = False
        self.zoom_card: bool = zoom_card
        self.vm = vm
        layout_graph(vm)
        _, _, gw, gh = self._get_graph_bounds()
        if zoom_card:
            if vm.view_kind == "module":
                req_w = int(min(780.0, max(520.0, gw * 1.55 + 48.0)))
                req_h = int(min(520.0, max(280.0, gh * 1.55 + 44.0)))
            else:
                req_w = int(min(1120.0, max(720.0, gw * 1.15 + 56.0)))
                req_h = int(min(580.0, max(340.0, gh * 1.15 + 48.0)))
            self.set_size_request(req_w, req_h)
        else:
            if vm.view_kind == "module":
                req_h = 132 if gh > 110.0 else 86
            else:
                req_h = int(max(106.0, min(164.0, 290.0 * (gh / max(gw, 240.0)) + 24.0)))
            self.set_size_request(220, req_h)
        self.set_hexpand(True)

    def _setup_controllers(self) -> None:
        # Read-only thumbnail preview; let parent Gtk.ListBox handle clicks, selection, and scrolling
        pass

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        iw = self.get_width()
        ih = self.get_height()
        if iw <= 0 or ih <= 0 or not self.vm:
            return
        w = float(iw)
        h = float(ih)

        rect = Graphene.Rect.alloc()
        rect.init(0.0, 0.0, w, h)
        rrect = Gsk.RoundedRect()
        rrect.init_from_rect(rect, 8.0)

        snapshot.push_rounded_clip(rrect)
        snapshot.append_color(BG_COLOR, rect)

        min_x, min_y, graph_w, graph_h = self._get_graph_bounds()
        pad = 18.0 if self.zoom_card else 10.0
        zx = max(20.0, w - 2.0 * pad) / max(graph_w, 1.0)
        zy = max(20.0, h - 2.0 * pad) / max(graph_h, 1.0)
        if self.zoom_card:
            max_sc = 1.65 if self.vm.view_kind == "module" else 1.35
        else:
            max_sc = 0.82
        scale = max(0.10, min(max_sc, min(zx, zy)))
        px = (w - graph_w * scale) / 2.0 - min_x * scale
        py = (h - graph_h * scale) / 2.0 - min_y * scale

        snapshot.save()
        pt = Graphene.Point.alloc()
        pt.init(px, py)
        snapshot.translate(pt)
        snapshot.scale(scale, scale)

        for pid, cont in self.vm.containers.items():
            self._snapshot_container(snapshot, pid, cont)
        for edge in self.vm.edges:
            self._snapshot_edge(snapshot, edge)
        for nid, node in self.vm.nodes.items():
            self._snapshot_node(snapshot, node, is_selected=self.is_row_selected)

        snapshot.restore()
        snapshot.pop()

        bcol = NODE_BORDER_SELECTED if self.is_row_selected else rgba(0.24, 0.28, 0.36, 0.85)
        bw = 1.8 if self.is_row_selected else 1.0
        snapshot.append_border(rrect, [bw, bw, bw, bw], [bcol, bcol, bcol, bcol])
