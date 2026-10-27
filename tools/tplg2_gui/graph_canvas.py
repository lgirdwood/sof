# SPDX-License-Identifier: BSD-3-Clause
"""Hardware-accelerated GTK4 Graph Canvas using Gtk.Snapshot and Gsk.PathBuilder."""

from __future__ import annotations
import math
import os
import subprocess
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango

from .layout import (
    CONTAINER_GAP_X,
    CTRL_GROUP_HDR,
    CTRL_GROUP_PAD,
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
    debug_log_action,
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
    "pin_fmt_in": (rgba(0.11, 0.46, 0.68, 1.0), rgba(0.08, 0.14, 0.21, 0.96)),
    "pin_fmt_out": (rgba(0.13, 0.54, 0.42, 1.0), rgba(0.08, 0.16, 0.14, 0.96)),
}

BG_COLOR = rgba(0.08, 0.09, 0.12, 1.0)
GRID_COLOR = rgba(0.16, 0.18, 0.23, 1.0)
CONTAINER_FILL_PB = rgba(0.14, 0.24, 0.38, 0.22)
CONTAINER_FILL_CAP = rgba(0.14, 0.36, 0.26, 0.22)
CONTAINER_BORDER_PB = rgba(0.28, 0.52, 0.82, 0.48)
CONTAINER_BORDER_CAP = rgba(0.28, 0.72, 0.48, 0.48)
NODE_BORDER_NORMAL = rgba(0.30, 0.35, 0.44, 0.90)
NODE_BORDER_SELECTED = rgba(0.35, 0.78, 1.00, 1.00)
NODE_BORDER_PROBED = rgba(0.06, 0.73, 0.51, 0.98)
NODE_BORDER_POWERED = rgba(0.22, 0.74, 0.97, 0.72)
NODE_SHADOW_SELECTED = rgba(0.20, 0.68, 1.00, 0.45)
TAP_BG_ACTIVE = rgba(0.06, 0.73, 0.51, 0.95)
TAP_BG_IDLE = rgba(0.06, 0.09, 0.16, 0.50)
TAP_FG_IDLE = rgba(0.80, 0.84, 0.88, 0.90)
TELEM_SUB_FG = rgba(0.49, 0.83, 0.99, 0.95)
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
    _clipboard_nodes: List[GraphNode] = []
    _clipboard_containers: List[PipelineContainer] = []
    _clipboard_edges: List[GraphEdge] = []
    _clipboard_paste_count: int = 0

    def __init__(
        self,
        on_selection_changed: Optional[Callable[[Optional[str], Optional[ConfigNode], str], None]] = None,
        on_graph_modified: Optional[Callable[[], None]] = None,
        on_history_changed: Optional[Callable[[], None]] = None,
        on_kcontrol_changed: Optional[Callable[[GraphNode], None]] = None,
        on_probe_tap_toggled: Optional[Callable[[Optional[str], int, str], None]] = None,
        on_testbench_io_changed: Optional[Callable[[List[int]], None]] = None,
        on_view_source_requested: Optional[Callable[[str, str, Optional[str], Optional[str]], None]] = None,
        on_violation_bang_clicked: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_focusable(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)

        self.on_selection_changed = on_selection_changed
        self.on_graph_modified = on_graph_modified
        self.on_kcontrol_changed = on_kcontrol_changed
        self.on_probe_tap_toggled = on_probe_tap_toggled
        self.on_testbench_io_changed = on_testbench_io_changed
        self.on_view_source_requested = on_view_source_requested
        self.on_violation_bang_clicked = on_violation_bang_clicked
        self._active_context_popover: Optional[Gtk.Popover] = None
        self.live_node_telemetry: Dict[str, Dict[str, Any]] = {}
        self.violating_edges: Dict[str, Dict[str, Any]] = {}
        self.violating_ports: Set[Tuple[str, str, str]] = set()
        self.violating_port_info: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self.ignore_tick0_underrun: bool = True
        self.active_probe_node_id: Optional[str] = None
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
        self._right_drag_mode: str = "none"  # "none", "box_select", "pan", "objects"
        self._selection_box_world: Optional[Tuple[float, float, float, float]] = None
        self._box_select_additive: bool = False
        self._box_select_initial_nodes: Set[str] = set()
        self._box_select_initial_containers: Set[int] = set()
        self._left_click_kept_multisel: bool = False
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

    def _has_multi_selection(self) -> bool:
        if not self.vm:
            return False
        valid_nids = [nid for nid in self.selected_node_ids if nid in self.vm.nodes]
        valid_pids = [pid for pid in self.selected_container_ids if pid in self.vm.containers]
        return len(valid_nids) > 1 or len(valid_pids) > 1 or (len(valid_nids) >= 1 and len(valid_pids) >= 1)

    def _get_selection_bounds_world(self) -> Optional[Tuple[float, float, float, float]]:
        """Return (min_x, min_y, max_x, max_y) in world space for all currently selected objects."""
        if not self.vm:
            return None
        sel_nids: Set[str] = {nid for nid in self.selected_node_ids if nid in self.vm.nodes}
        if self.selected_node_id and self.selected_node_id in self.vm.nodes:
            sel_nids.add(self.selected_node_id)
        sel_pids: Set[int] = {pid for pid in self.selected_container_ids if pid in self.vm.containers}
        if self.selected_container_id is not None and self.selected_container_id in self.vm.containers:
            sel_pids.add(self.selected_container_id)

        for pid in sel_pids:
            for n in self.vm.nodes.values():
                if n.pipeline_id == pid:
                    sel_nids.add(n.id)

        all_nids = set(sel_nids)
        for cid, cnode in self.vm.nodes.items():
            if cnode.parent_node_id and cnode.parent_node_id in sel_nids:
                all_nids.add(cid)

        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")
        for pid in sel_pids:
            cont = self.vm.containers[pid]
            min_x = min(min_x, cont.x)
            min_y = min(min_y, cont.y)
            max_x = max(max_x, cont.x + cont.width)
            max_y = max(max_y, cont.y + cont.height)
        for nid in all_nids:
            n = self.vm.nodes[nid]
            min_x = min(min_x, n.x)
            min_y = min(min_y, n.y)
            max_x = max(max_x, n.x + n.width)
            max_y = max(max_y, n.y + n.height)

        if min_x == float("inf"):
            return None
        return (min_x, min_y, max_x, max_y)

    def _is_point_in_current_selection(self, wx: float, wy: float) -> bool:
        """Return True if world point (wx, wy) hits a selected node/container or lies inside a multi-selection group box."""
        if not self.vm or not self._has_selected_objects():
            return False
        nid = self._hit_test_node(wx, wy)
        if nid and nid in self.vm.nodes:
            node = self.vm.nodes[nid]
            if nid in self.selected_node_ids or nid == self.selected_node_id:
                return True
            if node.parent_node_id and (
                node.parent_node_id in self.selected_node_ids or node.parent_node_id == self.selected_node_id
            ):
                return True
            if node.pipeline_id is not None and (
                node.pipeline_id in self.selected_container_ids or node.pipeline_id == self.selected_container_id
            ):
                return True
        pid = self._hit_test_container(wx, wy)
        if pid is not None:
            if pid in self.selected_container_ids or pid == self.selected_container_id:
                return True
            if any(
                snid in self.vm.nodes and self.vm.nodes[snid].pipeline_id == pid
                for snid in self.selected_node_ids
            ):
                return True
        if self._has_multi_selection():
            bounds = self._get_selection_bounds_world()
            if bounds is not None:
                bx0, by0, bx1, by1 = bounds
                pad = 8.0
                if (bx0 - pad) <= wx <= (bx1 + pad) and (by0 - pad) <= wy <= (by1 + pad):
                    return True
        return False

    def clear_diagnostic_highlights(self) -> None:
        self.diag_highlight_node_id = None
        self.diag_highlight_container_id = None
        self.diag_highlight_edge_id = None
        self.diag_banner_text = None
        self.queue_draw()

    def set_ignore_tick0_underrun(self, ignore: bool) -> None:
        self.ignore_tick0_underrun = bool(ignore)
        self.queue_draw()

    def set_live_telemetry_overlay(self, telem: Optional[Any]) -> None:
        """Populate live node telemetry, DAPM power badges, and violation status from TestbenchTelemetry."""
        self.live_node_telemetry.clear()
        self.violating_edges.clear()
        self.violating_ports.clear()
        self.violating_port_info.clear()
        if telem is None or not self.vm:
            self.queue_draw()
            return

        comps = getattr(telem, "components", []) or []
        dapms = getattr(telem, "dapm_nodes", []) or []
        pipes = getattr(telem, "pipelines", []) or []
        cores = getattr(telem, "cores", []) or []
        pipe_by_id = {int(getattr(p, "pipeline_id", -1)): p for p in pipes}
        core_by_id = {int(getattr(c, "core_id", -1)): c for c in cores}

        any_pipe_cpc_over: Dict[int, bool] = {}
        max_pipe_peak_cyc: Dict[int, int] = {}
        for c in comps:
            pid_c = int(getattr(c, "pipeline_id", 0) or 0)
            pk_c = int(getattr(c, "peak_cycles", 0) or 0)
            eff_c = int(getattr(c, "effective_cpc", 0) or 0)
            cpcv_c = int(getattr(c, "cpc_violations", 0) or 0)
            if cpcv_c > 0 or (eff_c > 0 and pk_c > eff_c):
                any_pipe_cpc_over[pid_c] = True
            max_pipe_peak_cyc[pid_c] = max(max_pipe_peak_cyc.get(pid_c, 0), pk_c)

        for nid, node in self.vm.nodes.items():
            if node.class_category in ("Control", "TestbenchIO", "PinFormat"):
                continue
            matched_comp = None
            for c in comps:
                wname = getattr(c, "widget_name", "") or ""
                if not wname:
                    continue
                if wname == nid or wname == node.title or nid in wname or wname in nid:
                    matched_comp = c
                    break
                if node.pipeline_id is not None and wname.startswith(f"{node.class_name}.{node.pipeline_id}."):
                    matched_comp = c
                    break

            matched_dapm = None
            for d in dapms:
                dname = (d.get("name", "") if isinstance(d, dict) else getattr(d, "name", "")) or ""
                if not dname:
                    continue
                if dname == nid or dname == node.title or nid in dname or dname in nid:
                    matched_dapm = d
                    break

            if matched_comp or matched_dapm:
                d_nid = matched_dapm.get("node_id", 0) if isinstance(matched_dapm, dict) else getattr(matched_dapm, "node_id", 0)
                d_name = matched_dapm.get("name", "") if isinstance(matched_dapm, dict) else getattr(matched_dapm, "name", "")
                d_pwr = matched_dapm.get("power", 1) if isinstance(matched_dapm, dict) else getattr(matched_dapm, "power", 1)

                cpc_v = int(getattr(matched_comp, "cpc_violations", 0) or 0) if matched_comp else 0
                eff_cpc = int(getattr(matched_comp, "effective_cpc", 0) or 0) if matched_comp else 0
                avg_cyc = int(getattr(matched_comp, "avg_cycles", 0) or 0) if matched_comp else 0
                pk_cyc = int(getattr(matched_comp, "peak_cycles", 0) or 0) if matched_comp else 0
                iters = int(getattr(matched_comp, "iterations", 0) or 0) if matched_comp else 0
                ibs = int(getattr(matched_comp, "ibs", 0) or 0) if matched_comp else 0
                obs = int(getattr(matched_comp, "obs", 0) or 0) if matched_comp else 0
                pid_val = int(getattr(matched_comp, "pipeline_id", node.pipeline_id or 0)) if matched_comp else int(node.pipeline_id or 0)
                cid_val = int(getattr(matched_comp, "core_id", 0)) if matched_comp else 0

                p_obj = pipe_by_id.get(pid_val)
                raw_pipe_xruns = int(getattr(p_obj, "xruns", getattr(p_obj, "xrun_count", 0)) or 0) if p_obj else 0
                c_obj = core_by_id.get(cid_val)
                raw_core_overruns = int(getattr(c_obj, "overruns", getattr(c_obj, "ll_deadline_violations", 0)) or 0) if c_obj else 0

                in_pins = getattr(matched_comp, "in_pins", []) if matched_comp else []
                out_pins = getattr(matched_comp, "out_pins", []) if matched_comp else []
                fw_errs = getattr(matched_comp, "fw_errors", []) if matched_comp else []

                def _pin_eff_xruns(pin_obj: Any, dir_str: str, idx_p: int) -> int:
                    p_idx = int(getattr(pin_obj, "pin_index", idx_p) or idx_p)
                    tot_x = max(
                        int(getattr(pin_obj, "xrun_count", 0) or 0),
                        int(getattr(pin_obj, "underrun_count", 0) or 0)
                        + int(getattr(pin_obj, "overrun_count", 0) or 0),
                    )
                    t0_x = int(getattr(pin_obj, "tick0_xrun_count", 0) or 0)
                    p_logs = [
                        fe
                        for fe in (fw_errs or [])
                        if str(fe.get("pin_dir", "")).upper() == dir_str
                        and int(fe.get("pin_index", -1)) == p_idx
                    ]
                    if p_logs and tot_x == 0:
                        tot_x = len(p_logs)
                        t0_x = sum(1 for fe in p_logs if int(fe.get("tick", 0) or 0) == 0)
                    if tot_x <= 0:
                        return 0
                    if self.ignore_tick0_underrun:
                        if t0_x > 0:
                            return max(0, tot_x - t0_x)
                        if iters <= 1:
                            return 0
                    return tot_x

                violated_in_indices = [
                    int(getattr(p_in, "pin_index", idx_p) or idx_p)
                    for idx_p, p_in in enumerate(in_pins)
                    if _pin_eff_xruns(p_in, "IN", idx_p) > 0
                ]
                violated_out_indices = [
                    int(getattr(p_out, "pin_index", idx_p) or idx_p)
                    for idx_p, p_out in enumerate(out_pins)
                    if _pin_eff_xruns(p_out, "OUT", idx_p) > 0
                ]
                raw_mod_err = int(getattr(matched_comp, "error_count", 0) or 0) if matched_comp else 0
                t0_mod_err = int(getattr(matched_comp, "tick0_error_count", 0) or 0) if matched_comp else 0
                mod_errs = (
                    max(0, raw_mod_err - t0_mod_err)
                    if (self.ignore_tick0_underrun and t0_mod_err > 0)
                    else (0 if (self.ignore_tick0_underrun and iters <= 1) else raw_mod_err)
                )
                pin_underrun = bool(violated_in_indices or violated_out_indices)
                has_cpc_over = cpc_v > 0 or (eff_cpc > 0 and pk_cyc > eff_cpc)

                # Only attribute pipeline XRUN / core overrun to this module if it violated CPC
                # or if no module in the pipeline violated CPC and this module is the peak-cycle culprit
                is_xrun_culprit = has_cpc_over or (
                    not any_pipe_cpc_over.get(pid_val, False)
                    and pk_cyc > 0
                    and pk_cyc == max_pipe_peak_cyc.get(pid_val, 0)
                )
                pipe_xruns = raw_pipe_xruns if is_xrun_culprit else 0
                core_overruns = raw_core_overruns if is_xrun_culprit else 0

                has_violation = bool(
                    has_cpc_over or pin_underrun or mod_errs > 0 or pipe_xruns > 0 or core_overruns > 0
                )
                v_cnt = (
                    cpc_v
                    if cpc_v > 0
                    else (
                        pipe_xruns
                        + core_overruns
                        + len(violated_in_indices)
                        + len(violated_out_indices)
                        + mod_errs
                        + (1 if has_cpc_over else 0)
                    )
                )

                # Build concise violation reason and pin tags for this module only (no cascading to neighbors)
                violation_reasons: List[str] = []
                pill_parts: List[str] = []
                if has_cpc_over:
                    violation_reasons.append(f"CPC:{pk_cyc}>{eff_cpc}(v={max(1, cpc_v)})")
                    pill_parts.append("CPC")
                for p_idx in violated_in_indices:
                    violation_reasons.append(f"IN[{p_idx}] UNDERRUN")
                    pill_parts.append(f"IN[{p_idx}]")
                for p_idx in violated_out_indices:
                    violation_reasons.append(f"OUT[{p_idx}] STALL")
                    pill_parts.append(f"OUT[{p_idx}]")
                if mod_errs > 0 and not (violated_in_indices or violated_out_indices):
                    violation_reasons.append(f"FW_ERR(v={mod_errs})")
                    pill_parts.append("FW_ERR")
                if not violation_reasons and (pipe_xruns > 0 or core_overruns > 0):
                    violation_reasons.append(f"XRUN(v={pipe_xruns or core_overruns})")
                    pill_parts.append("XRUN")

                reason_str = "+".join(pill_parts[:2]) if pill_parts else "VIOL"
                violation_pill_tag = f"! {reason_str} v={max(1, v_cnt)}"

                telem_entry: Dict[str, Any] = {
                    "comp_id": int(getattr(matched_comp, "resource_id", getattr(matched_comp, "comp_id", 0))) if matched_comp else int(d_nid),
                    "widget_name": (getattr(matched_comp, "widget_name", "") if matched_comp else d_name) or nid,
                    "pipeline_id": pid_val,
                    "avg_kcps": float(getattr(matched_comp, "avg_kcps", 0.0)) if matched_comp else 0.0,
                    "peak_kcps": float(getattr(matched_comp, "peak_kcps", 0.0)) if matched_comp else 0.0,
                    "domain": str(getattr(matched_comp, "domain", "LL")) if matched_comp else "LL",
                    "mem_domain": str(getattr(matched_comp, "mem_domain", "SRAM")) if matched_comp else "SRAM",
                    "core_id": cid_val,
                    "in_rms_db": float(getattr(matched_comp, "in_rms_db", -120.0)) if matched_comp else -120.0,
                    "out_rms_db": float(getattr(matched_comp, "out_rms_db", -120.0)) if matched_comp else -120.0,
                    "powered": bool(d_pwr) if matched_dapm else True,
                    "has_violation": has_violation,
                    "direct_violation": has_violation,
                    "violation_reasons": violation_reasons,
                    "violation_pill_tag": violation_pill_tag,
                    "violated_in_pins": violated_in_indices,
                    "violated_out_pins": violated_out_indices,
                    "violation_count": v_cnt,
                    "cpc_violations": cpc_v,
                    "effective_cpc": eff_cpc,
                    "avg_cycles": avg_cyc,
                    "peak_cycles": pk_cyc,
                    "pipe_xruns": pipe_xruns,
                    "core_overruns": core_overruns,
                    "pin_underrun": pin_underrun,
                    "mod_obj": matched_comp,
                }
                self.live_node_telemetry[nid] = telem_entry

                # Mark ONLY the specific violated pin port(s) on this violating module
                for p_idx in violated_in_indices:
                    port_obj = (
                        node.in_ports[p_idx]
                        if p_idx < len(node.in_ports)
                        else (node.in_ports[0] if node.in_ports else None)
                    )
                    if port_obj is not None:
                        pkey = (nid, port_obj.id, "in")
                        self.violating_ports.add(pkey)
                        self.violating_port_info[pkey] = {
                            "primary_nid": nid,
                            "primary_telem": telem_entry,
                            "pin_label": f"IN[{p_idx}]",
                            "reason": f"IN[{p_idx}] UNDERRUN",
                        }
                for p_idx in violated_out_indices:
                    port_obj = (
                        node.out_ports[p_idx]
                        if p_idx < len(node.out_ports)
                        else (node.out_ports[0] if node.out_ports else None)
                    )
                    if port_obj is not None:
                        pkey = (nid, port_obj.id, "out")
                        self.violating_ports.add(pkey)
                        self.violating_port_info[pkey] = {
                            "primary_nid": nid,
                            "primary_telem": telem_entry,
                            "pin_label": f"OUT[{p_idx}]",
                            "reason": f"OUT[{p_idx}] STALL",
                        }

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
        debug_log_action("CANVAS-MUTATE", description, f"graph={self.vm.title}")
        entry = self._capture_history_entry(description)
        if entry is not None:
            self.history.push_undo_entry(entry)

    def _notify_current_selection(self) -> None:
        if not self.vm or not self.on_selection_changed:
            return
        if self.selected_node_id and self.selected_node_id in self.vm.nodes:
            node = self.vm.nodes[self.selected_node_id]
            extra_cnt = len(self.selected_node_ids) - 1 if self.selected_node_id in self.selected_node_ids else len(self.selected_node_ids)
            title = f"{node.title}  (+{extra_cnt} selected)" if extra_cnt > 0 else node.title
            self.on_selection_changed(title, node.config, node.subtitle)
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
            debug_log_action(
                "CANVAS-SELECT",
                f"Select node {node.id}",
                f"class={node.class_name} category={node.class_category} pipe={node.pipeline_id}",
            )
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

        # 6b. Draw Grouped Module Kcontrol Containers & Color-Coordinated Connectors
        self._snapshot_module_kcontrol_groups(snapshot, wx0, wy0, wx1, wy1)

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

        # 7c. Draw Right-Drag Rectangular Selection Box or Active Multi-Selection Group Box in World Space
        if self._selection_box_world is not None:
            self._snapshot_selection_box(snapshot, self._selection_box_world)
        elif self._has_multi_selection():
            bounds = self._get_selection_bounds_world()
            if bounds is not None:
                self._snapshot_group_selection_bounds(snapshot, bounds)

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
            self._snapshot_kcontrol_node(snapshot, node, rect, rrect, is_selected, header_col, body_col)
            return

        if node.class_category == "TestbenchIO":
            self._snapshot_testbench_io_node(snapshot, node, rect, rrect, is_selected)
            return

        if node.class_category == "PinFormat":
            self._snapshot_pin_format_node(snapshot, node, rect, rrect, is_selected, header_col, body_col)
            return

        # Clip rounded node card
        snapshot.push_rounded_clip(rrect)
        snapshot.append_color(body_col, rect)

        # Colored header strip
        hdr_rect = Graphene.Rect.alloc()
        hdr_rect.init(node.x, node.y, node.width, 26.0)
        snapshot.append_color(header_col, hdr_rect)
        snapshot.pop()

        # Node border (highlight in crimson if violating, emerald when actively probed or DAPM powered)
        telem_info = self.live_node_telemetry.get(node.id)
        has_violation = bool(telem_info and telem_info.get("has_violation"))
        is_probed = self.active_probe_node_id == node.id

        if has_violation:
            # Outer red violation alert halo around the module card
            v_rect = Graphene.Rect.alloc()
            v_rect.init(node.x - 4.0, node.y - 4.0, node.width + 8.0, node.height + 8.0)
            v_rr = Gsk.RoundedRect()
            v_rr.init_from_rect(v_rect, 11.0)
            snapshot.push_rounded_clip(v_rr)
            snapshot.append_color(rgba(0.96, 0.16, 0.20, 0.24), v_rect)
            snapshot.pop()
            v_bcol = rgba(1.0, 0.28, 0.28, 0.96)
            snapshot.append_border(v_rr, [2.2, 2.2, 2.2, 2.2], [v_bcol, v_bcol, v_bcol, v_bcol])

        if is_probed:
            bcol = NODE_BORDER_PROBED
            bw = 2.4
        elif has_violation:
            bcol = rgba(1.0, 0.26, 0.28, 1.0)
            bw = 2.4
        elif is_selected:
            bcol = NODE_BORDER_SELECTED
            bw = 2.2
        elif telem_info and telem_info.get("powered"):
            bcol = NODE_BORDER_POWERED
            bw = 1.6
        else:
            bcol = NODE_BORDER_NORMAL
            bw = 1.2
        snapshot.append_border(rrect, [bw, bw, bw, bw], [bcol, bcol, bcol, bcol])

        # Node Title in header (full text, node width is sized to fit)
        title_x = node.x + (26.0 if has_violation else 10.0)
        title_layout = self._get_pango_layout(node.title, size_pt=9, bold=True)
        snapshot.save()
        pt = Graphene.Point.alloc()
        pt.init(title_x, node.y + 5.0)
        snapshot.translate(pt)
        snapshot.append_layout(title_layout, TEXT_WHITE)
        snapshot.restore()

        # Red Bang (!) badge on violating module nodes
        if has_violation and telem_info is not None:
            # 1) Prominent circular red bang (!) icon in top-left of the module header
            bang_cx = node.x + 14.0
            bang_cy = node.y + 13.0
            self._snapshot_port_circle(snapshot, bang_cx, bang_cy, rgba(1.0, 0.92, 0.92, 0.98), radius=8.5)
            self._snapshot_port_circle(snapshot, bang_cx, bang_cy, rgba(0.92, 0.12, 0.16, 1.0), radius=7.0)
            bang_lbl = self._get_pango_layout("!", size_pt=9, bold=True)
            _, bang_ext = bang_lbl.get_pixel_extents()
            snapshot.save()
            pt.init(bang_cx - float(bang_ext.width) * 0.5, bang_cy - float(bang_ext.height) * 0.5)
            snapshot.translate(pt)
            snapshot.append_layout(bang_lbl, TEXT_WHITE)
            snapshot.restore()

            # 2) Compact red [! <reason/pin> v=N] violation pill next to the TAP pill in top-right of header
            v_cnt = int(telem_info.get("violation_count", 1) or 1)
            vpill_txt = str(telem_info.get("violation_pill_tag") or f"! v={v_cnt}")
            vpill_lbl = self._get_pango_layout(vpill_txt, size_pt=6, bold=True)
            _, vpill_ext = vpill_lbl.get_pixel_extents()
            vpill_w = max(32.0, float(vpill_ext.width) + 10.0)
            vpill_h = 16.0
            vpill_x = node.x + node.width - 38.0 - 6.0 - vpill_w - 4.0
            vpill_y = node.y + 5.0
            vpill_rect = Graphene.Rect.alloc()
            vpill_rect.init(vpill_x, vpill_y, vpill_w, vpill_h)
            vpill_rr = Gsk.RoundedRect()
            vpill_rr.init_from_rect(vpill_rect, 4.0)
            snapshot.push_rounded_clip(vpill_rr)
            snapshot.append_color(rgba(0.86, 0.12, 0.16, 0.98), vpill_rect)
            snapshot.pop()
            vp_bcol = rgba(1.0, 0.72, 0.72, 0.95)
            snapshot.append_border(vpill_rr, [1.0, 1.0, 1.0, 1.0], [vp_bcol, vp_bcol, vp_bcol, vp_bcol])
            snapshot.save()
            pt.init(vpill_x + 5.0, vpill_y + 2.0)
            snapshot.translate(pt)
            snapshot.append_layout(vpill_lbl, TEXT_WHITE)
            snapshot.restore()

        # Clickable Probe Tap pill in top-right of module header
        tap_w = 38.0
        tap_h = 16.0
        tap_x = node.x + node.width - tap_w - 6.0
        tap_y = node.y + 5.0
        tap_rect = Graphene.Rect.alloc()
        tap_rect.init(tap_x, tap_y, tap_w, tap_h)
        tap_rr = Gsk.RoundedRect()
        tap_rr.init_from_rect(tap_rect, 4.0)
        snapshot.push_rounded_clip(tap_rr)
        snapshot.append_color(TAP_BG_ACTIVE if is_probed else TAP_BG_IDLE, tap_rect)
        snapshot.pop()
        tap_lbl = self._get_pango_layout("●TAP" if is_probed else "TAP", size_pt=6, bold=True)
        snapshot.save()
        pt.init(tap_x + (4.0 if is_probed else 8.0), tap_y + 2.0)
        snapshot.translate(pt)
        snapshot.append_layout(tap_lbl, TEXT_WHITE if is_probed else TAP_FG_IDLE)
        snapshot.restore()

        # Subtitle (or live telemetry badge if testbench telemetry is available for this node)
        sub_text = node.subtitle
        sub_col = TEXT_MUTED
        if telem_info:
            kcps_val = float(telem_info.get("avg_kcps", 0.0))
            mem_dom = str(telem_info.get("mem_domain", "SRAM"))
            out_db = float(telem_info.get("out_rms_db", -120.0))
            pwr_str = "ON" if telem_info.get("powered", True) else "OFF"
            if has_violation:
                v_cnt = int(telem_info.get("violation_count", 1) or 1)
                reasons = telem_info.get("violation_reasons") or []
                if reasons:
                    sub_text = f"! {' · '.join(reasons)} · ⚡{kcps_val:.0f} KCPS"
                else:
                    pk_cyc = int(telem_info.get("peak_cycles", 0) or 0)
                    eff_cpc = int(telem_info.get("effective_cpc", 0) or 0)
                    cpc_tag = f" · CPC:{pk_cyc}/{eff_cpc}" if eff_cpc > 0 else ""
                    sub_text = f"! VIOLATION (v={v_cnt}){cpc_tag} · ⚡{kcps_val:.0f} KCPS"
                sub_col = rgba(1.0, 0.52, 0.52, 1.0)
            elif out_db > -115.0:
                sub_text = f"⚡{kcps_val:.0f} KCPS · {mem_dom} · {out_db:.1f}dB [{pwr_str}]"
                sub_col = TELEM_SUB_FG if telem_info.get("powered", True) else TEXT_MUTED
            else:
                sub_text = f"⚡{kcps_val:.0f} KCPS · {mem_dom} · C{telem_info.get('core_id', 0)} [{pwr_str}]"
                sub_col = TELEM_SUB_FG if telem_info.get("powered", True) else TEXT_MUTED
        sub_layout = self._get_pango_layout(sub_text, size_pt=8, bold=bool(telem_info))
        snapshot.save()
        pt.init(node.x + 10.0, node.y + 31.0)
        snapshot.translate(pt)
        snapshot.append_layout(sub_layout, sub_col)
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

        # Input & Output connection ports (render red bang (!) badge on violated pin connections)
        for port in node.in_ports:
            px = node.x + port.rel_x
            py = node.y + port.rel_y
            is_hov = self.hovered_port == (node.id, port.id, "in")
            if (node.id, port.id, "in") in self.violating_ports:
                self._snapshot_port_circle(snapshot, px, py, rgba(1.0, 0.90, 0.90, 0.98), radius=8.2 if is_hov else 7.4)
                self._snapshot_port_circle(snapshot, px, py, rgba(0.92, 0.12, 0.16, 1.0), radius=6.8 if is_hov else 6.0)
                pb_lbl = self._get_pango_layout("!", size_pt=7, bold=True)
                _, pb_ext = pb_lbl.get_pixel_extents()
                snapshot.save()
                pt.init(px - float(pb_ext.width) * 0.5, py - float(pb_ext.height) * 0.5)
                snapshot.translate(pt)
                snapshot.append_layout(pb_lbl, TEXT_WHITE)
                snapshot.restore()
            else:
                self._snapshot_port_circle(snapshot, px, py, PORT_HOVER_COLOR if is_hov else PORT_IN_COLOR, radius=6.5 if is_hov else 5.0)

        for port in node.out_ports:
            px = node.x + port.rel_x
            py = node.y + port.rel_y
            is_hov = self.hovered_port == (node.id, port.id, "out")
            if (node.id, port.id, "out") in self.violating_ports:
                self._snapshot_port_circle(snapshot, px, py, rgba(1.0, 0.90, 0.90, 0.98), radius=8.2 if is_hov else 7.4)
                self._snapshot_port_circle(snapshot, px, py, rgba(0.92, 0.12, 0.16, 1.0), radius=6.8 if is_hov else 6.0)
                pb_lbl = self._get_pango_layout("!", size_pt=7, bold=True)
                _, pb_ext = pb_lbl.get_pixel_extents()
                snapshot.save()
                pt.init(px - float(pb_ext.width) * 0.5, py - float(pb_ext.height) * 0.5)
                snapshot.translate(pt)
                snapshot.append_layout(pb_lbl, TEXT_WHITE)
                snapshot.restore()
            else:
                self._snapshot_port_circle(snapshot, px, py, PORT_HOVER_COLOR if is_hov else PORT_OUT_COLOR, radius=6.5 if is_hov else 5.0)

    @staticmethod
    def _get_kcontrol_short_name(node: GraphNode) -> str:
        raw_name = str(node.config.get_scalar("name", "")).strip().strip('"')
        if raw_name:
            # Strip common verbose prefixes if long so the graphical item title is crisp
            s = raw_name
            for pfx in ("Post Mixer Analog ", "Pre Mixer Analog ", "Analog Capture ", "Analog Playback "):
                if s.startswith(pfx) and len(s) > len(pfx) + 4:
                    s = s[len(pfx):]
                    break
            return s if len(s) <= 22 else (s[:20] + "…")
        return node.title if len(node.title) <= 22 else (node.title[:20] + "…")

    @staticmethod
    def _get_kcontrol_volume_state(node: GraphNode) -> Tuple[int, int]:
        """Return (cur_val, max_val) for a volume control_mixer node."""
        max_raw = str(node.config.get_scalar("max", "32")).strip()
        max_v = int(max_raw) if max_raw.isdigit() and int(max_raw) > 0 else 32
        val_raw = str(node.config.get_scalar("value", "") or node.config.get_scalar("volume", "")).strip()
        cur_v = int(round(max_v * 0.78))
        if val_raw:
            parts = [p.strip() for p in val_raw.split(",") if p.strip().lstrip("-").isdigit()]
            if parts:
                cur_v = max(0, min(max_v, int(parts[0])))
        return (cur_v, max_v)

    @staticmethod
    def _get_kcontrol_switch_state(node: GraphNode) -> bool:
        """Return True (ON) or False (OFF) for a switch control_switch node."""
        val_raw = str(node.config.get_scalar("value", "1")).strip().lower()
        if not val_raw:
            return True
        first = val_raw.split(",")[0].strip()
        return first not in ("0", "false", "off", "mute", "muted")

    @staticmethod
    def _get_kcontrol_enum_state(node: GraphNode) -> Tuple[str, List[str]]:
        """Return (selected_option_str, enum_options_list) for a control_enum node."""
        opts_raw = str(node.config.get_scalar("enum_options", "")).strip()
        opts = [x.strip() for x in opts_raw.split(",") if x.strip()] if opts_raw else ["0", "1"]
        val_raw = str(node.config.get_scalar("value", "")).strip()
        if not val_raw:
            cur = "0" if "0" in opts else opts[0]
        elif val_raw in opts:
            cur = val_raw
        elif val_raw.isdigit() and 0 <= int(val_raw) < len(opts):
            cur = opts[int(val_raw)]
        else:
            cur = val_raw
        return (cur, opts)

    @staticmethod
    def _get_kcontrol_bytes_file(node: GraphNode) -> str:
        """Return the current parameter file path or blob label for a control_bytes node."""
        pfile = str(node.config.get_scalar("included_param_file", "")).strip().strip('"')
        if pfile:
            return pfile
        for ch in node.config.children:
            if ch.key.startswith("Object.Base.data"):
                return "inline_blob.bin"
        return "Choose .conf/.bin…"

    def _snapshot_module_kcontrol_groups(
        self,
        snapshot: Gtk.Snapshot,
        wx0: float,
        wy0: float,
        wx1: float,
        wy1: float,
    ) -> None:
        """Draw a color-coordinated grouped container frame and connector for each module's kcontrols."""
        if not self.vm:
            return
        by_parent: Dict[str, List[GraphNode]] = {}
        for node in self.vm.nodes.values():
            if node.class_category == "Control" and node.parent_node_id and node.parent_node_id in self.vm.nodes:
                by_parent.setdefault(node.parent_node_id, []).append(node)

        for parent_id, ctrls in by_parent.items():
            parent = self.vm.nodes[parent_id]
            gx = min(c.x for c in ctrls) - CTRL_GROUP_PAD
            gy = min(c.y for c in ctrls) - CTRL_GROUP_HDR
            gw = (max(c.x + c.width for c in ctrls) - min(c.x for c in ctrls)) + 2.0 * CTRL_GROUP_PAD
            gh = (max(c.y + c.height for c in ctrls) - min(c.y for c in ctrls)) + CTRL_GROUP_HDR + CTRL_GROUP_PAD

            if gx + gw < wx0 or gx > wx1 or gy + gh < wy0 or gy > wy1:
                continue

            g_rect = Graphene.Rect.alloc()
            g_rect.init(gx, gy, gw, gh)
            g_rr = Gsk.RoundedRect()
            g_rr.init_from_rect(g_rect, 6.0)

            any_sel = (
                parent_id == self.selected_node_id
                or parent_id in self.selected_node_ids
                or any(c.id == self.selected_node_id or c.id in self.selected_node_ids for c in ctrls)
            )
            p_hdr_col, p_body_col = CATEGORY_COLORS.get(parent.category, CATEGORY_COLORS["widget"])
            fill_col = (
                rgba(p_body_col.red * 1.15, p_body_col.green * 1.15, p_body_col.blue * 1.15, 0.78)
                if any_sel
                else rgba(p_body_col.red, p_body_col.green, p_body_col.blue, 0.58)
            )
            b_col = (
                NODE_BORDER_SELECTED
                if any_sel
                else rgba(p_hdr_col.red, p_hdr_col.green, p_hdr_col.blue, 0.85)
            )

            snapshot.push_rounded_clip(g_rr)
            snapshot.append_color(fill_col, g_rect)
            hdr_strip = Graphene.Rect.alloc()
            hdr_strip.init(gx, gy, gw, CTRL_GROUP_HDR - 1.0)
            snapshot.append_color(
                rgba(p_hdr_col.red, p_hdr_col.green, p_hdr_col.blue, 0.42 if any_sel else 0.28),
                hdr_strip,
            )
            snapshot.pop()
            bw = 1.6 if any_sel else 1.2
            snapshot.append_border(g_rr, [bw, bw, bw, bw], [b_col, b_col, b_col, b_col])

            # Group header caption
            hdr_lbl = f"{parent.title} ({len(ctrls)})"
            lyt = self._get_pango_layout(hdr_lbl, size_pt=6, bold=True)
            snapshot.save()
            pt = Graphene.Point.alloc()
            pt.init(gx + 5.0, gy + 1.5)
            snapshot.translate(pt)
            snapshot.append_layout(lyt, TEXT_WHITE if any_sel else rgba(0.90, 0.95, 1.0, 0.92))
            snapshot.restore()

            # Single color-coordinated dashed connector from top-center of module's kcontrol group to parent module bottom
            x1 = gx + gw / 2.0
            y1 = gy
            x2 = max(parent.x + 12.0, min(parent.x + parent.width - 12.0, x1))
            y2 = parent.y + parent.height
            dy = max(5.0, abs(y1 - y2) * 0.45)
            pb = Gsk.PathBuilder.new()
            pb.move_to(x1, y1)
            pb.cubic_to(x1, y1 - dy, x2, y2 + dy, x2, y2)
            stroke = Gsk.Stroke.new(2.0 if any_sel else 1.5)
            stroke.set_dash([3.5, 2.5])
            line_col = (
                NODE_BORDER_SELECTED
                if any_sel
                else rgba(p_hdr_col.red, p_hdr_col.green, p_hdr_col.blue, 0.92)
            )
            snapshot.append_stroke(pb.to_path(), stroke, line_col)

            arrow_pb = Gsk.PathBuilder.new()
            arrow_pb.move_to(x2, y2)
            arrow_pb.line_to(x2 - 3.0, y2 + 5.0)
            arrow_pb.line_to(x2 + 3.0, y2 + 5.0)
            arrow_pb.close()
            snapshot.append_fill(arrow_pb.to_path(), Gsk.FillRule.WINDING, line_col)
            self._snapshot_port_circle(snapshot, x1, y1, line_col, radius=3.2)

    def _snapshot_kcontrol_node(
        self,
        snapshot: Gtk.Snapshot,
        node: GraphNode,
        rect: Graphene.Rect,
        rrect: Gsk.RoundedRect,
        is_selected: bool,
        header_col: Gdk.RGBA,
        body_col: Gdk.RGBA,
    ) -> None:
        """Render each Kcontrol node as a compact interactive graphical widget:
        - control_mixer (Volume): Vertical fader track + fill bar + draggable knob + value readout
        - control_switch (Switch): Compact ON / OFF toggle switch pill
        - control_enum (Enum): Compact dropdown selector box with current option & arrow
        - control_bytes (Bytes): Compact file selector button showing active parameter file
        """
        snapshot.push_rounded_clip(rrect)
        snapshot.append_color(body_col, rect)

        hdr_h = 15.0
        hdr_rect = Graphene.Rect.alloc()
        hdr_rect.init(node.x, node.y, node.width, hdr_h)
        snapshot.append_color(header_col, hdr_rect)

        tag_map = {
            "control_mixer": "VOL",
            "control_switch": "SW",
            "control_bytes": "BYTES",
            "control_enum": "ENUM",
        }
        tag_txt = tag_map.get(node.category, "CTL")
        short_name = self._get_kcontrol_short_name(node)

        pt = Graphene.Point.alloc()
        if node.category == "control_mixer":
            # Vertical Volume Fader Card (56 x 92)
            disp_title = short_name[:8] if len(short_name) > 8 else short_name
            title_layout = self._get_pango_layout(disp_title, size_pt=6, bold=True)
            snapshot.save()
            pt.init(node.x + 4.0, node.y + 2.0)
            snapshot.translate(pt)
            snapshot.append_layout(title_layout, TEXT_WHITE)
            snapshot.restore()

            cur_v, max_v = self._get_kcontrol_volume_state(node)
            frac = max(0.0, min(1.0, cur_v / float(max(1, max_v))))
            track_x = node.x + 11.0
            track_w = 8.0
            track_y = node.y + 21.0
            track_h = max(32.0, node.height - 37.0)

            # Recessed vertical slider trough
            t_rect = Graphene.Rect.alloc()
            t_rect.init(track_x, track_y, track_w, track_h)
            t_rr = Gsk.RoundedRect()
            t_rr.init_from_rect(t_rect, 4.0)
            snapshot.push_rounded_clip(t_rr)
            snapshot.append_color(rgba(0.07, 0.08, 0.12, 0.96), t_rect)
            if frac > 0.0:
                fill_h = track_h * frac
                f_rect = Graphene.Rect.alloc()
                f_rect.init(track_x, track_y + track_h - fill_h, track_w, fill_h)
                snapshot.append_color(rgba(0.68, 0.42, 0.98, 0.96), f_rect)
            snapshot.pop()
            t_bcol = rgba(0.42, 0.36, 0.62, 0.92)
            snapshot.append_border(t_rr, [1.0, 1.0, 1.0, 1.0], [t_bcol, t_bcol, t_bcol, t_bcol])

            # Vertical tick marks beside the fader
            tick_pb = Gsk.PathBuilder.new()
            for tf in (0.0, 0.25, 0.5, 0.75, 1.0):
                ty = track_y + track_h * tf
                tick_pb.move_to(track_x + track_w + 2.0, ty)
                tick_pb.line_to(track_x + track_w + 5.0, ty)
            snapshot.append_stroke(tick_pb.to_path(), Gsk.Stroke.new(1.0), rgba(0.55, 0.62, 0.76, 0.65))

            # Vertical slider thumb knob (0 at bottom, max_v at top)
            knob_x = track_x + track_w * 0.5
            knob_y = track_y + track_h * (1.0 - frac)
            self._snapshot_port_circle(snapshot, knob_x, knob_y, TEXT_WHITE, radius=5.4)
            self._snapshot_port_circle(snapshot, knob_x, knob_y, header_col, radius=3.6)

            # Value & max readout to the right of the vertical fader
            val_layout = self._get_pango_layout(f"{cur_v}", size_pt=7, bold=True)
            snapshot.save()
            pt.init(node.x + 25.0, track_y + track_h * 0.26)
            snapshot.translate(pt)
            snapshot.append_layout(val_layout, TEXT_WHITE)
            snapshot.restore()

            max_layout = self._get_pango_layout(f"/{max_v}", size_pt=6, bold=False)
            snapshot.save()
            pt.init(node.x + 25.0, track_y + track_h * 0.52)
            snapshot.translate(pt)
            snapshot.append_layout(max_layout, TEXT_MUTED)
            snapshot.restore()

            foot_layout = self._get_pango_layout("VOL", size_pt=6, bold=True)
            snapshot.save()
            pt.init(node.x + 18.0, node.y + node.height - 13.0)
            snapshot.translate(pt)
            snapshot.append_layout(foot_layout, rgba(0.86, 0.72, 1.0, 0.95))
            snapshot.restore()
        else:
            # Compact Switch / Enum / Bytes Card (118 x 42)
            disp_title = short_name[:12] + "…" if len(short_name) > 13 else short_name
            title_layout = self._get_pango_layout(disp_title, size_pt=6, bold=True)
            snapshot.save()
            pt.init(node.x + 5.0, node.y + 2.0)
            snapshot.translate(pt)
            snapshot.append_layout(title_layout, TEXT_WHITE)
            snapshot.restore()

            tag_layout = self._get_pango_layout(tag_txt, size_pt=5, bold=True)
            _, tag_ext = tag_layout.get_pixel_extents()
            snapshot.save()
            pt.init(node.x + node.width - float(tag_ext.width) - 4.0, node.y + 2.5)
            snapshot.translate(pt)
            snapshot.append_layout(tag_layout, rgba(0.92, 0.96, 1.0, 0.88))
            snapshot.restore()

            ctrl_y = node.y + 18.0
            ctrl_h = 20.0

            if node.category == "control_switch":
                is_on = self._get_kcontrol_switch_state(node)
                btn_rect = Graphene.Rect.alloc()
                btn_rect.init(node.x + 6.0, ctrl_y, node.width - 12.0, ctrl_h)
                btn_rr = Gsk.RoundedRect()
                btn_rr.init_from_rect(btn_rect, 9.5)
                bg_c = rgba(0.14, 0.56, 0.36, 0.95) if is_on else rgba(0.22, 0.16, 0.20, 0.95)
                snapshot.push_rounded_clip(btn_rr)
                snapshot.append_color(bg_c, btn_rect)
                snapshot.pop()
                b_c = rgba(0.32, 0.92, 0.62, 0.95) if is_on else rgba(0.65, 0.32, 0.38, 0.9)
                snapshot.append_border(btn_rr, [1.1, 1.1, 1.1, 1.1], [b_c, b_c, b_c, b_c])

                cy = ctrl_y + ctrl_h * 0.5
                cx = (node.x + node.width - 16.0) if is_on else (node.x + 16.0)
                self._snapshot_port_circle(
                    snapshot,
                    cx,
                    cy,
                    rgba(0.65, 1.0, 0.80, 1.0) if is_on else rgba(0.75, 0.78, 0.84, 0.9),
                    radius=5.5,
                )
                sw_txt = "ON (Active)" if is_on else "OFF (Muted)"
                sw_layout = self._get_pango_layout(sw_txt, size_pt=6, bold=True)
                snapshot.save()
                pt.init(node.x + (14.0 if is_on else 26.0), ctrl_y + 4.0)
                snapshot.translate(pt)
                snapshot.append_layout(sw_layout, TEXT_WHITE if is_on else TEXT_MUTED)
                snapshot.restore()

            elif node.category == "control_enum":
                cur_opt, opts = self._get_kcontrol_enum_state(node)
                box_rect = Graphene.Rect.alloc()
                box_rect.init(node.x + 6.0, ctrl_y, node.width - 12.0, ctrl_h)
                box_rr = Gsk.RoundedRect()
                box_rr.init_from_rect(box_rect, 4.5)
                snapshot.push_rounded_clip(box_rr)
                snapshot.append_color(rgba(0.08, 0.11, 0.15, 0.96), box_rect)
                snapshot.pop()
                eb_col = rgba(0.86, 0.28, 0.48, 0.95)
                snapshot.append_border(box_rr, [1.0, 1.0, 1.0, 1.0], [eb_col, eb_col, eb_col, eb_col])

                disp_opt = f"{cur_opt} ({len(opts)})" if len(cur_opt) <= 9 else cur_opt[:13]
                opt_layout = self._get_pango_layout(disp_opt, size_pt=6, bold=True)
                snapshot.save()
                pt.init(node.x + 10.0, ctrl_y + 4.0)
                snapshot.translate(pt)
                snapshot.append_layout(opt_layout, rgba(1.0, 0.76, 0.86, 1.0))
                snapshot.restore()

                arr_layout = self._get_pango_layout("▾", size_pt=7, bold=True)
                snapshot.save()
                pt.init(node.x + node.width - 16.0, ctrl_y + 3.0)
                snapshot.translate(pt)
                snapshot.append_layout(arr_layout, TEXT_WHITE)
                snapshot.restore()

            else:
                pfile = self._get_kcontrol_bytes_file(node)
                base_f = os.path.basename(pfile) or pfile
                if len(base_f) > 13:
                    base_f = base_f[:12] + "…"
                f_rect = Graphene.Rect.alloc()
                f_rect.init(node.x + 6.0, ctrl_y, node.width - 12.0, ctrl_h)
                f_rr = Gsk.RoundedRect()
                f_rr.init_from_rect(f_rect, 4.5)
                snapshot.push_rounded_clip(f_rr)
                snapshot.append_color(rgba(0.08, 0.12, 0.16, 0.96), f_rect)
                snapshot.pop()
                fb_col = rgba(0.18, 0.68, 0.88, 0.95)
                snapshot.append_border(f_rr, [1.0, 1.0, 1.0, 1.0], [fb_col, fb_col, fb_col, fb_col])

                flayout = self._get_pango_layout(f"📂 {base_f}", size_pt=6, bold=True)
                snapshot.save()
                pt.init(node.x + 9.0, ctrl_y + 4.0)
                snapshot.translate(pt)
                snapshot.append_layout(flayout, rgba(0.68, 0.92, 1.0, 1.0))
                snapshot.restore()

                dots_layout = self._get_pango_layout("…", size_pt=7, bold=True)
                snapshot.save()
                pt.init(node.x + node.width - 15.0, ctrl_y + 2.5)
                snapshot.translate(pt)
                snapshot.append_layout(dots_layout, TEXT_WHITE)
                snapshot.restore()

        snapshot.pop()

        bcol = NODE_BORDER_SELECTED if is_selected else header_col
        bw = 2.0 if is_selected else 1.1
        snapshot.append_border(rrect, [bw, bw, bw, bw], [bcol, bcol, bcol, bcol])

    def _snapshot_testbench_io_node(
        self,
        snapshot: Gtk.Snapshot,
        node: GraphNode,
        rect: Graphene.Rect,
        rrect: Gsk.RoundedRect,
        is_selected: bool,
    ) -> None:
        """Render interactive Testbench Host Source/Sink card with pipeline radio, I/O Mode Enum, and File/ALSA/SigGen selector."""
        is_enabled = str(node.config.get_scalar("enabled", "false")).lower() == "true"
        is_source = node.class_name == "testbench-source"
        io_type = str(node.config.get_scalar("io_type", "file")).lower()
        if not is_source and io_type == "sig_gen":
            io_type = "file"
        file_path = str(node.config.get_scalar("file_path", "audio.wav"))
        alsa_pcm = str(node.config.get_scalar("alsa_pcm", "default"))
        sig_type = str(node.config.get_scalar("signal_type", "sine_997")).lower()
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

        # Row 1: I/O Mode Enum Control (File / ALSA PCM / Signal Generator)
        pill_y = node.y + 31.0
        pill_h = 20.0
        mode_rect = Graphene.Rect.alloc()
        mode_rect.init(node.x + 10.0, pill_y, node.width - 20.0, pill_h)
        mode_rr = Gsk.RoundedRect()
        mode_rr.init_from_rect(mode_rect, 4.5)
        mode_bg = (
            rgba(0.16, 0.34, 0.54, 0.95)
            if io_type == "sig_gen"
            else rgba(0.14, 0.22, 0.34, 0.95)
        )
        snapshot.push_rounded_clip(mode_rr)
        snapshot.append_color(mode_bg, mode_rect)
        snapshot.pop()
        mode_bcol = (
            rgba(0.38, 0.78, 1.0, 0.95)
            if io_type == "sig_gen"
            else rgba(0.34, 0.56, 0.82, 0.92)
        )
        snapshot.append_border(mode_rr, [1.0, 1.0, 1.0, 1.0], [mode_bcol, mode_bcol, mode_bcol, mode_bcol])

        mode_names = {
            "file": "Audio File",
            "alsa_pcm": "ALSA PCM",
            "sig_gen": "Signal Generator",
        }
        prefix_lbl = "Source" if is_source else "Sink"
        mode_txt = f"{prefix_lbl}: {mode_names.get(io_type, 'Audio File')}"
        m_layout = self._get_pango_layout(mode_txt, size_pt=7, bold=True)
        snapshot.save()
        pt.init(node.x + 16.0, pill_y + 3.0)
        snapshot.translate(pt)
        snapshot.append_layout(m_layout, TEXT_WHITE)
        snapshot.restore()

        m_caret = self._get_pango_layout("▾", size_pt=7, bold=True)
        snapshot.save()
        pt.init(node.x + node.width - 22.0, pill_y + 3.0)
        snapshot.translate(pt)
        snapshot.append_layout(m_caret, BADGE_FG)
        snapshot.restore()

        # Row 2: Clickable File / ALSA PCM / Signal Type selector box
        sel_y = node.y + 54.0
        sel_h = 20.0
        sel_rect = Graphene.Rect.alloc()
        sel_rect.init(node.x + 10.0, sel_y, node.width - 20.0, sel_h)
        sel_rr = Gsk.RoundedRect()
        sel_rr.init_from_rect(sel_rect, 4.5)
        snapshot.push_rounded_clip(sel_rr)
        snapshot.append_color(rgba(0.08, 0.10, 0.14, 0.95), sel_rect)
        snapshot.pop()
        sel_bcol = (
            rgba(0.30, 0.72, 0.88, 0.95)
            if io_type == "sig_gen"
            else rgba(0.34, 0.44, 0.58, 0.95)
        )
        snapshot.append_border(sel_rr, [1.0, 1.0, 1.0, 1.0], [sel_bcol, sel_bcol, sel_bcol, sel_bcol])

        if io_type == "sig_gen":
            sig_short = self.get_signal_type_short_label(sig_type)
            sel_txt = f"Sig: {sig_short}"
        elif io_type == "alsa_pcm":
            sel_txt = f"PCM: {alsa_pcm}"
        else:
            disp_val = os.path.basename(file_path) or file_path
            sel_txt = f"File: {disp_val}"
        if len(sel_txt) > 26:
            sel_txt = sel_txt[:24] + "…"
        sel_layout = self._get_pango_layout(sel_txt, size_pt=7, bold=(io_type == "sig_gen"))
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

    def _snapshot_pin_format_node(
        self,
        snapshot: Gtk.Snapshot,
        node: GraphNode,
        rect: Graphene.Rect,
        rrect: Gsk.RoundedRect,
        is_selected: bool,
        header_col: Gdk.RGBA,
        body_col: Gdk.RGBA,
    ) -> None:
        """Render left-hand Input Pin or right-hand Output Pin capability callout as a translucent
        bracketed schematic annotation (distinct from solid widget/module nodes).
        """
        is_in = node.category == "pin_fmt_in"
        is_left_side = str(node.config.get_scalar("side", "left")).lower() == "left"
        f_line = str(node.config.get_scalar("formats_line", "s16, s24/32, s32"))
        r_line = str(node.config.get_scalar("rates_line", "48 kHz"))
        c_line = str(node.config.get_scalar("channels_line", "2 ch • interleaved"))

        # Subtle translucent callout background (grid/canvas remains visible behind it)
        tint_col = (
            rgba(0.08, 0.16, 0.24, 0.38 if is_selected else 0.22)
            if is_in
            else rgba(0.16, 0.13, 0.08, 0.38 if is_selected else 0.22)
        )
        snapshot.push_rounded_clip(rrect)
        snapshot.append_color(tint_col, rect)
        snapshot.pop()

        # Faint dashed callout perimeter (instead of solid node border)
        dash_col = (
            NODE_BORDER_SELECTED
            if is_selected
            else (rgba(0.32, 0.82, 0.96, 0.34) if is_in else rgba(0.98, 0.68, 0.26, 0.34))
        )
        dash_pb = Gsk.PathBuilder.new()
        dash_pb.add_rounded_rect(rrect)
        dash_stroke = Gsk.Stroke.new(1.5 if is_selected else 1.0)
        dash_stroke.set_dash([2.5, 3.0])
        snapshot.append_stroke(dash_pb.to_path(), dash_stroke, dash_col)

        # Open schematic bracket spine [ ┐...┘ on right edge when callout sits on the left, ┌...└ on left edge when on the right ]
        spine_col = (
            NODE_BORDER_SELECTED
            if is_selected
            else (rgba(0.32, 0.82, 0.96, 0.92) if is_in else rgba(0.98, 0.68, 0.26, 0.92))
        )
        arm = 8.0
        sy0 = node.y + 3.0
        sy1 = node.y + node.height - 3.0
        bracket_pb = Gsk.PathBuilder.new()
        if is_left_side:
            sx = node.x + node.width - 1.5
            bracket_pb.move_to(sx - arm, sy0)
            bracket_pb.line_to(sx, sy0)
            bracket_pb.line_to(sx, sy1)
            bracket_pb.line_to(sx - arm, sy1)
        else:
            sx = node.x + 1.5
            bracket_pb.move_to(sx + arm, sy0)
            bracket_pb.line_to(sx, sy0)
            bracket_pb.line_to(sx, sy1)
            bracket_pb.line_to(sx + arm, sy1)
        snapshot.append_stroke(bracket_pb.to_path(), Gsk.Stroke.new(2.0), spine_col)

        # Pin capability header label (no solid header bar) + subtle dotted underline
        pt = Graphene.Point.alloc()
        hdr_txt_col = rgba(0.48, 0.88, 1.0, 0.98) if is_in else rgba(1.0, 0.78, 0.38, 0.98)
        title_layout = self._get_pango_layout(node.title, size_pt=6, bold=True)
        snapshot.save()
        pt.init(node.x + 7.0, node.y + 3.5)
        snapshot.translate(pt)
        snapshot.append_layout(title_layout, hdr_txt_col)
        snapshot.restore()

        sep_pb = Gsk.PathBuilder.new()
        sep_pb.move_to(node.x + 7.0, node.y + 16.5)
        sep_pb.line_to(node.x + node.width - 7.0, node.y + 16.5)
        sep_stroke = Gsk.Stroke.new(0.8)
        sep_stroke.set_dash([1.5, 2.0])
        snapshot.append_stroke(sep_pb.to_path(), sep_stroke, dash_col)

        # 3 compact datasheet capability lines (fmt / rate / ch)
        rows = [
            ("fmt:", f_line, rgba(0.76, 0.94, 1.0, 0.98) if is_in else rgba(1.0, 0.88, 0.64, 0.98)),
            ("rate:", r_line, rgba(0.90, 0.94, 0.98, 0.95)),
            ("ch:", c_line, rgba(0.74, 0.82, 0.90, 0.92)),
        ]
        ry = node.y + 19.0
        key_col = rgba(0.56, 0.72, 0.86, 0.88) if is_in else rgba(0.86, 0.72, 0.52, 0.88)
        for k_txt, v_txt, v_col in rows:
            k_lyt = self._get_pango_layout(k_txt, size_pt=6, bold=False)
            snapshot.save()
            pt.init(node.x + 7.0, ry)
            snapshot.translate(pt)
            snapshot.append_layout(k_lyt, key_col)
            snapshot.restore()

            v_lyt = self._get_pango_layout(v_txt, size_pt=6, bold=(k_txt == "fmt:"))
            snapshot.save()
            pt.init(node.x + 33.0, ry)
            snapshot.translate(pt)
            snapshot.append_layout(v_lyt, v_col)
            snapshot.restore()
            ry += 14.0

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
            if u.parent_node_id and u.parent_node_id in self.vm.nodes and not is_sel:
                # Grouped connector is already drawn by _snapshot_module_kcontrol_groups
                return
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

        if u.class_category == "PinFormat" or v.class_category == "PinFormat":
            is_in_cap = (u.category == "pin_fmt_in") or (v.category == "pin_fmt_in")
            cap_col = (
                EDGE_SELECTED_COLOR
                if is_sel
                else (rgba(0.32, 0.82, 0.96, 0.72) if is_in_cap else rgba(0.98, 0.68, 0.26, 0.72))
            )
            sign = 1.0 if x2 >= x1 else -1.0
            dx = max(6.0, abs(x2 - x1) * 0.4) * sign
            pb = Gsk.PathBuilder.new()
            pb.move_to(x1, y1)
            pb.cubic_to(x1 + dx, y1, x2 - dx, y2, x2, y2)
            stroke = Gsk.Stroke.new(1.8 if is_sel else 1.15)
            stroke.set_dash([2.0, 2.5])
            snapshot.append_stroke(pb.to_path(), stroke, cap_col)
            return

        is_diag = edge.id == self.diag_highlight_edge_id
        v_edge_meta = self.violating_edges.get(edge.id)
        if v_edge_meta is not None:
            # Outer crimson alert glow along the violated pin connection wire
            self._snapshot_bezier_wire(
                snapshot,
                x1,
                y1,
                x2,
                y2,
                rgba(0.96, 0.14, 0.18, 0.28),
                width=6.8,
                dashed=False,
            )
            color = rgba(1.0, 0.22, 0.25, 0.98)
            width = 3.4
        elif is_diag:
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

        # Render a Red Bang (!) badge at the midpoint of a violated pin connection wire
        if v_edge_meta is not None:
            mx = 0.5 * (x1 + x2)
            my = 0.5 * (y1 + y2)
            pin_txt = f"{u_port.id}→{v_port.id}"
            pin_lyt = self._get_pango_layout(pin_txt, size_pt=6, bold=True)
            _, pin_ext = pin_lyt.get_pixel_extents()
            pill_w = 18.0 + float(pin_ext.width) + 8.0
            pill_h = 16.0
            pill_x = mx - pill_w * 0.5
            pill_y = my - pill_h * 0.5

            pill_rect = Graphene.Rect.alloc()
            pill_rect.init(pill_x, pill_y, pill_w, pill_h)
            pill_rr = Gsk.RoundedRect()
            pill_rr.init_from_rect(pill_rect, 8.0)
            snapshot.push_rounded_clip(pill_rr)
            snapshot.append_color(rgba(0.76, 0.08, 0.12, 0.96), pill_rect)
            snapshot.pop()
            eb_col = rgba(1.0, 0.76, 0.76, 0.98)
            snapshot.append_border(pill_rr, [1.2, 1.2, 1.2, 1.2], [eb_col, eb_col, eb_col, eb_col])

            # Circular Red Bang (!) inside left end of the wire badge
            bcx = pill_x + 8.5
            bcy = my
            self._snapshot_port_circle(snapshot, bcx, bcy, rgba(1.0, 0.94, 0.94, 0.98), radius=6.8)
            self._snapshot_port_circle(snapshot, bcx, bcy, rgba(0.95, 0.14, 0.18, 1.0), radius=5.6)
            b_lbl = self._get_pango_layout("!", size_pt=7, bold=True)
            _, b_ext = b_lbl.get_pixel_extents()
            pt = Graphene.Point.alloc()
            snapshot.save()
            pt.init(bcx - float(b_ext.width) * 0.5, bcy - float(b_ext.height) * 0.5)
            snapshot.translate(pt)
            snapshot.append_layout(b_lbl, TEXT_WHITE)
            snapshot.restore()

            snapshot.save()
            pt.init(pill_x + 17.5, pill_y + 2.0)
            snapshot.translate(pt)
            snapshot.append_layout(pin_lyt, TEXT_WHITE)
            snapshot.restore()

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

    def _snapshot_selection_box(
        self,
        snapshot: Gtk.Snapshot,
        box_world: Tuple[float, float, float, float],
    ) -> None:
        """Render the right-button drag rectangular marquee selection box in world space."""
        x0, y0, x1, y1 = box_world
        bw = max(1.0, x1 - x0)
        bh = max(1.0, y1 - y0)

        rect = Graphene.Rect.alloc()
        rect.init(x0, y0, bw, bh)
        rrect = Gsk.RoundedRect()
        rrect.init_from_rect(rect, 4.0 / max(0.25, self.zoom))

        snapshot.push_rounded_clip(rrect)
        snapshot.append_color(rgba(0.22, 0.72, 0.98, 0.16), rect)
        snapshot.pop()

        pb = Gsk.PathBuilder.new()
        pb.add_rounded_rect(rrect)
        stroke_w = max(1.2, 1.8 / max(0.25, self.zoom))
        dash_len = max(3.0, 6.0 / max(0.25, self.zoom))
        gap_len = max(2.0, 4.0 / max(0.25, self.zoom))
        stroke = Gsk.Stroke.new(stroke_w)
        stroke.set_dash([dash_len, gap_len])
        snapshot.append_stroke(pb.to_path(), stroke, rgba(0.38, 0.84, 1.00, 0.95))

        # Show live selection count badge at top-left of marquee when dragging
        sel_cnt = len(self.selected_node_ids)
        if sel_cnt > 0 and bw * self.zoom > 36.0 and bh * self.zoom > 24.0:
            badge_txt = f"{sel_cnt} node{'s' if sel_cnt != 1 else ''} selected"
            lyt = self._get_pango_layout(badge_txt, size_pt=7, bold=True)
            _, ext = lyt.get_pixel_extents()
            inv_z = 1.0 / max(0.25, self.zoom)
            tag_w = (float(ext.width) + 12.0) * inv_z
            tag_h = (float(ext.height) + 6.0) * inv_z
            tag_rect = Graphene.Rect.alloc()
            tag_rect.init(x0, max(0.0, y0 - tag_h - 4.0 * inv_z), tag_w, tag_h)
            tag_rr = Gsk.RoundedRect()
            tag_rr.init_from_rect(tag_rect, 4.0 * inv_z)
            snapshot.push_rounded_clip(tag_rr)
            snapshot.append_color(rgba(0.10, 0.34, 0.56, 0.92), tag_rect)
            snapshot.pop()

            snapshot.save()
            pt = Graphene.Point.alloc()
            pt.init(x0 + 6.0 * inv_z, max(0.0, y0 - tag_h - 4.0 * inv_z) + 3.0 * inv_z)
            snapshot.translate(pt)
            snapshot.scale(inv_z, inv_z)
            snapshot.append_layout(lyt, TEXT_WHITE)
            snapshot.restore()

    def _snapshot_group_selection_bounds(
        self,
        snapshot: Gtk.Snapshot,
        bounds_world: Tuple[float, float, float, float],
    ) -> None:
        """Render a dashed group bounding frame around an active multi-selection."""
        pad = 10.0
        x0 = bounds_world[0] - pad
        y0 = bounds_world[1] - pad
        bw = max(1.0, (bounds_world[2] - bounds_world[0]) + 2.0 * pad)
        bh = max(1.0, (bounds_world[3] - bounds_world[1]) + 2.0 * pad)

        inv_z = 1.0 / max(0.25, self.zoom)
        rect = Graphene.Rect.alloc()
        rect.init(x0, y0, bw, bh)
        rrect = Gsk.RoundedRect()
        rrect.init_from_rect(rect, 8.0 * inv_z)

        pb = Gsk.PathBuilder.new()
        pb.add_rounded_rect(rrect)
        stroke = Gsk.Stroke.new(max(1.2, 1.8 * inv_z))
        stroke.set_dash([max(4.0, 7.0 * inv_z), max(2.5, 4.5 * inv_z)])
        snapshot.append_stroke(pb.to_path(), stroke, rgba(0.32, 0.82, 1.00, 0.78))

        sel_cnt = len(self.selected_node_ids)
        badge_txt = f"Group: {sel_cnt} node{'s' if sel_cnt != 1 else ''}  (Drag to move • Ctrl+C Copy • Ctrl+V Paste)"
        lyt = self._get_pango_layout(badge_txt, size_pt=7, bold=True)
        _, ext = lyt.get_pixel_extents()
        tag_w = (float(ext.width) + 12.0) * inv_z
        tag_h = (float(ext.height) + 6.0) * inv_z
        tag_y = y0 - tag_h - 4.0 * inv_z
        tag_rect = Graphene.Rect.alloc()
        tag_rect.init(x0, tag_y, tag_w, tag_h)
        tag_rr = Gsk.RoundedRect()
        tag_rr.init_from_rect(tag_rect, 4.0 * inv_z)
        snapshot.push_rounded_clip(tag_rr)
        snapshot.append_color(rgba(0.10, 0.34, 0.56, 0.90), tag_rect)
        snapshot.pop()

        snapshot.save()
        pt = Graphene.Point.alloc()
        pt.init(x0 + 6.0 * inv_z, tag_y + 3.0 * inv_z)
        snapshot.translate(pt)
        snapshot.scale(inv_z, inv_z)
        snapshot.append_layout(lyt, TEXT_WHITE)
        snapshot.restore()

    def _snapshot_hud(self, snapshot: Gtk.Snapshot, w: float, h: float) -> None:
        if not self.vm:
            return
        dirty_tag = " [Modified]" if self.vm.dirty else ""
        sel_tag = f"  •  Selected: {len(self.selected_node_ids)}" if len(self.selected_node_ids) > 1 else ""
        hud_text = (
            f"{self.vm.title}{dirty_tag}  •  {len(self.vm.nodes)} nodes, {len(self.vm.edges)} routes{sel_tag}"
            f"  •  Zoom: {int(self.zoom * 100)}%  (Right-drag to box-select, Drag port to wire, Del to remove, F to fit)"
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
            if edge.id in self.violating_edges:
                mx = 0.5 * (x1 + x2)
                my = 0.5 * (y1 + y2)
                if abs(wx - mx) <= 38.0 and abs(wy - my) <= 12.0:
                    return edge.id
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

        # Check port first (don't change selection if starting a wire drag; if violated port red bang clicked, zoom to violation)
        port_hit = self._hit_test_port(wx, wy)
        if not self._has_multi_selection() and port_hit is not None:
            p_key = (port_hit[0], port_hit[1], port_hit[2])
            if p_key in self.violating_port_info and self.on_violation_bang_clicked:
                p_meta = self.violating_port_info[p_key]
                self.on_violation_bang_clicked(
                    str(p_meta.get("primary_nid") or port_hit[0]),
                    p_meta.get("primary_telem") or self.live_node_telemetry.get(port_hit[0], {}),
                )
            return

        # If a multi-selection group is active and user presses inside the selected group/containers/nodes,
        # preserve the multi-selection so left-dragging moves the entire group together.
        if not additive and self._has_multi_selection() and self._is_point_in_current_selection(wx, wy):
            self._left_click_kept_multisel = True
            nid_hit = self._hit_test_node(wx, wy)
            if nid_hit and nid_hit in self.selected_node_ids:
                self.selected_node_id = nid_hit
            self._notify_current_selection()
            self.queue_draw()
            return

        self._left_click_kept_multisel = False
        nid = self._hit_test_node(wx, wy)
        if nid:
            node = self.vm.nodes[nid]
            self.select_node(nid, additive=additive)
            if node.class_category == "Control":
                lx = wx - node.x
                ly = wy - node.y
                # Interactive control area is below the header row (ly >= 15.0)
                if ly >= 15.0 and 4.0 <= lx <= node.width - 4.0:
                    if node.category == "control_mixer":
                        self.record_undo_snapshot(f"Adjust {self._get_kcontrol_short_name(node)} volume")
                        self._set_kcontrol_volume_from_local_y(node, ly, notify=True)
                    elif node.category == "control_switch":
                        self.record_undo_snapshot(f"Toggle {self._get_kcontrol_short_name(node)} switch")
                        is_on = self._get_kcontrol_switch_state(node)
                        node.config.set_scalar("value", "0" if is_on else "1", origin="instance")
                        self.vm.dirty = True
                        self.select_node(nid)
                        if self.on_kcontrol_changed:
                            self.on_kcontrol_changed(node)
                        if self.on_graph_modified:
                            self.on_graph_modified()
                        self.queue_draw()
                    elif node.category == "control_enum":
                        self._open_kcontrol_enum_popover(node, sx, sy)
                    elif node.category == "control_bytes":
                        self._open_kcontrol_bytes_file_chooser(node)
                return
            if node.class_category == "TestbenchIO":
                lx = wx - node.x
                ly = wy - node.y
                ctrl_held = bool(state & Gdk.ModifierType.CONTROL_MASK)
                # Header / Radio button click, footer click, or Ctrl+click anywhere on card -> enable/toggle this TestbenchIO pipeline chain
                if 0.0 <= ly <= 26.0 or ly > 75.0 or ctrl_held:
                    self.select_testbench_io_radio(nid, additive=ctrl_held)
                    return
                # Row 1: I/O Mode Enum Control (File / ALSA PCM / Signal Generator)
                if 30.0 <= ly <= 51.0 and 10.0 <= lx <= node.width - 10.0:
                    self._open_tb_io_type_enum_popover(node, sx, sy)
                    return
                # Row 2: Selector box click -> open FileChooser, ALSA PCM popover, or Signal Type Enum popover
                if 53.0 <= ly <= 75.0 and 10.0 <= lx <= node.width - 10.0:
                    io_type = (node.config.get_scalar("io_type") or "file").strip().lower()
                    if io_type == "sig_gen" and node.class_name == "testbench-source":
                        self._open_tb_sig_gen_popover(node, sx, sy)
                    elif io_type == "alsa_pcm":
                        self._open_tb_alsa_popover(node, sx, sy)
                    else:
                        self._open_tb_file_chooser(node)
                    return
                return
            # Processing module node: check if user clicked the top-right [TAP] probe pill or Red Bang (!) violation badge
            lx = wx - node.x
            ly = wy - node.y
            telem_info = self.live_node_telemetry.get(nid)
            has_v = bool(telem_info and telem_info.get("has_violation"))
            if has_v and telem_info is not None and 0.0 <= ly <= 26.0:
                if lx <= 26.0 or (node.width - 125.0 <= lx < node.width - 48.0):
                    if self.on_violation_bang_clicked:
                        target_nid = (
                            nid
                            if telem_info.get("direct_violation", True)
                            else str(telem_info.get("primary_violation_nid") or nid)
                        )
                        target_telem = (
                            telem_info
                            if telem_info.get("direct_violation", True)
                            else (telem_info.get("primary_violation_telem") or telem_info)
                        )
                        self.on_violation_bang_clicked(target_nid, target_telem)
                    self.queue_draw()
                    return
            if 0.0 <= ly <= 26.0 and lx >= node.width - 48.0:
                if self.active_probe_node_id == nid:
                    self.active_probe_node_id = None
                    comp_id = 0
                else:
                    self.active_probe_node_id = nid
                    comp_id = int(self.live_node_telemetry.get(nid, {}).get("comp_id", 0))
                debug_log_action(
                    "CANVAS-PROBE-TAP",
                    f"Toggle probe tap on {nid}",
                    f"active={self.active_probe_node_id} comp_id={comp_id}",
                )
                if self.on_probe_tap_toggled:
                    self.on_probe_tap_toggled(self.active_probe_node_id, comp_id, nid)
                self.queue_draw()
                return
            return

        eid = self._hit_test_edge(wx, wy)
        if eid:
            if eid in self.violating_edges and self.on_violation_bang_clicked:
                ve_meta = self.violating_edges[eid]
                self.on_violation_bang_clicked(
                    str(ve_meta.get("primary_nid") or ""),
                    ve_meta.get("primary_telem") or {},
                )
            self.selected_node_id = None
            self.selected_node_ids.clear()
            self.selected_edge_id = eid
            self.selected_container_id = None
            self.selected_container_ids.clear()
            edge = next((e for e in self.vm.edges if e.id == eid), None)
            if edge:
                debug_log_action(
                    "CANVAS-SELECT",
                    f"Select route {edge.id}",
                    f"{edge.source_node} ({edge.source_port}) -> {edge.sink_node} ({edge.sink_port})",
                )
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
            debug_log_action(
                "CANVAS-SELECT",
                f"Select pipeline container {pid}",
                f"class={cont.class_name} dir={cont.direction}",
            )
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

        # If a multi-selection group is active and drag starts anywhere on/inside the selected group,
        # drag all selected nodes and containers together as a unit.
        state = gesture.get_current_event_state()
        additive = bool(state & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK))
        if not additive and self._has_multi_selection() and self._is_point_in_current_selection(wx, wy):
            self._drag_mode = "group"
            self._pending_drag_history_entry = self._capture_history_entry(
                f"Move {len(self.selected_node_ids)} selected node(s)"
            )
            self._capture_selected_objects_initial()
            self._drag_children_initial = dict(self._right_drag_nodes_initial)
            self.set_cursor_from_name("move")
            return

        port_hit = self._hit_test_port(wx, wy)
        if port_hit and port_hit[2] == "out":
            nid, pid, _, px, py = port_hit
            self._drag_mode = "wire"
            self._wire_source = (nid, pid, px, py)
            self._wire_cursor_world = (wx, wy)
            return

        nid = self._hit_test_node(wx, wy)
        if nid:
            node = self.vm.nodes[nid]
            if node.class_category == "Control" and (wy - node.y) >= 15.0:
                if node.category == "control_mixer":
                    self._drag_mode = "kctl_slider"
                    self.selected_node_id = nid
                    self._pending_drag_history_entry = self._capture_history_entry(
                        f"Slide {self._get_kcontrol_short_name(node)} volume"
                    )
                    self.set_cursor_from_name("ns-resize")
                else:
                    # Don't move a Control card when clicking its button/dropdown/file picker
                    self._drag_mode = "none"
                return
            self._drag_mode = "node"
            if nid not in self.selected_node_ids:
                self.select_node(nid)
            else:
                self.selected_node_id = nid
            desc = (
                f"Move {len(self.selected_node_ids)} selected nodes"
                if len(self.selected_node_ids) > 1
                else f"Move {nid}"
            )
            self._pending_drag_history_entry = self._capture_history_entry(desc)
            self._drag_node_initial = (node.x, node.y)
            self._capture_selected_objects_initial()
            self._drag_children_initial = dict(self._right_drag_nodes_initial)
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
        elif self._drag_mode in ("node", "group"):
            dx = offset_x / self.zoom
            dy = offset_y / self.zoom
            for pid, (cx0, cy0) in self._right_drag_containers_initial.items():
                if pid in self.vm.containers:
                    self.vm.containers[pid].x = cx0 + dx
                    self.vm.containers[pid].y = cy0 + dy
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
        elif self._drag_mode == "kctl_slider" and self.selected_node_id in self.vm.nodes:
            wy = self._drag_start_world[1] + offset_y / self.zoom
            node = self.vm.nodes[self.selected_node_id]
            self._set_kcontrol_volume_from_local_y(node, wy - node.y, notify=False)

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
        elif self.vm and self._drag_mode in ("node", "container", "group"):
            if moved and self._pending_drag_history_entry is not None:
                self.history.push_undo_entry(self._pending_drag_history_entry)
                self.vm.dirty = True
                debug_log_action(
                    "CANVAS-MOVE",
                    f"Moved selection ({len(self.selected_node_ids)} nodes, {len(self.selected_container_ids)} containers)",
                    f"dx={offset_x / self.zoom:.1f} dy={offset_y / self.zoom:.1f}",
                )
            if not moved and self._left_click_kept_multisel:
                wx0, wy0 = self._drag_start_world
                nid_hit = self._hit_test_node(wx0, wy0)
                pid_hit = self._hit_test_container(wx0, wy0)
                if nid_hit:
                    self.select_node(nid_hit, additive=False)
                elif pid_hit is not None and pid_hit in self.vm.containers:
                    self.selected_node_id = None
                    self.selected_node_ids.clear()
                    self.selected_container_ids = {pid_hit}
                    self.selected_container_id = pid_hit
                    self._notify_current_selection()
                else:
                    self.select_node(None)
            elif self._drag_mode == "node" and self.selected_node_id in self.vm.nodes and len(self.selected_node_ids) <= 1:
                self._suppress_auto_history = True
                try:
                    self._maybe_reparent_dragged_node(self.selected_node_id)
                finally:
                    self._suppress_auto_history = False
            if moved and self.on_graph_modified:
                self.on_graph_modified()
        elif self.vm and self._drag_mode == "kctl_slider" and self.selected_node_id in self.vm.nodes:
            if moved and self._pending_drag_history_entry is not None:
                self.history.push_undo_entry(self._pending_drag_history_entry)
                self.vm.dirty = True
            node = self.vm.nodes[self.selected_node_id]
            self.select_node(node.id)
            if self.on_kcontrol_changed:
                self.on_kcontrol_changed(node)
            if self.on_graph_modified:
                self.on_graph_modified()
        self._left_click_kept_multisel = False
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
    # - Starting on empty space / outside selection (or with Shift/Ctrl):
    #   drag a rectangular marquee selection area to select multiple nodes
    # - Starting on/inside an active selection:
    #   drag all selected nodes and containers together as a group
    # ------------------------------------------------------------------
    def _on_right_pressed(self, gesture: Gtk.GestureClick, n_press: int, sx: float, sy: float) -> None:
        self.grab_focus()
        wx, wy = self.screen_to_world(sx, sy)
        state = gesture.get_current_event_state()
        additive = bool(state & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK))
        if not additive and self._is_point_in_current_selection(wx, wy):
            self.set_cursor_from_name("move")
        else:
            self.set_cursor_from_name("crosshair")

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

        # Include attached child nodes (kcontrols, testbench I/O, pin capability callouts) of any selected node
        all_nids = set(sel_nids)
        for cid, cnode in self.vm.nodes.items():
            if cnode.parent_node_id and cnode.parent_node_id in sel_nids:
                all_nids.add(cid)

        for nid in all_nids:
            if nid in self.vm.nodes:
                n = self.vm.nodes[nid]
                self._right_drag_nodes_initial[nid] = (n.x, n.y)

    def _apply_box_selection(self, bx0: float, by0: float, bx1: float, by1: float) -> None:
        """Select all nodes intersecting the world-space selection rectangle (bx0, by0, bx1, by1)."""
        if not self.vm:
            return
        widget_hits: List[str] = []
        other_hits: List[str] = []
        for nid, node in self.vm.nodes.items():
            if node.class_category == "PinFormat":
                continue
            if node.x + node.width >= bx0 and node.x <= bx1 and node.y + node.height >= by0 and node.y <= by1:
                if node.class_category not in ("Control", "TestbenchIO"):
                    widget_hits.append(nid)
                else:
                    other_hits.append(nid)

        hit_nids = set(widget_hits) | set(other_hits)
        hit_pids: Set[int] = set()
        for pid, cont in self.vm.containers.items():
            if cont.x >= bx0 and cont.x + cont.width <= bx1 and cont.y >= by0 and cont.y + cont.height <= by1:
                hit_pids.add(pid)
                for nid, node in self.vm.nodes.items():
                    if node.pipeline_id == pid and node.class_category != "PinFormat":
                        hit_nids.add(nid)
            else:
                # Also mark pipeline container selected if all of its widget nodes are selected
                pipe_widgets = [
                    nid
                    for nid, node in self.vm.nodes.items()
                    if node.pipeline_id == pid and node.class_category not in ("PinFormat", "TestbenchIO", "Control")
                ]
                if pipe_widgets and all(nid in hit_nids for nid in pipe_widgets):
                    hit_pids.add(pid)

        self.selected_edge_id = None
        self.selected_node_ids = (self._box_select_initial_nodes | hit_nids) if self._box_select_additive else hit_nids
        if widget_hits:
            self.selected_node_id = widget_hits[0]
        elif other_hits:
            self.selected_node_id = other_hits[0]
        else:
            self.selected_node_id = next(iter(self.selected_node_ids), None)

        self.selected_container_ids = (
            (self._box_select_initial_containers | hit_pids) if self._box_select_additive else hit_pids
        )
        self.selected_container_id = next(iter(self.selected_container_ids), None)

    def _on_right_drag_begin(self, gesture: Gtk.GestureDrag, sx: float, sy: float) -> None:
        if not self.vm:
            return
        self._drag_start_screen = (sx, sy)
        wx, wy = self.screen_to_world(sx, sy)
        self._drag_start_world = (wx, wy)
        state = gesture.get_current_event_state()
        additive = bool(
            state & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK)
        )
        if not additive and self._is_point_in_current_selection(wx, wy):
            self._right_drag_mode = "objects"
            self.set_cursor_from_name("move")
            self._pending_drag_history_entry = self._capture_history_entry(
                f"Move {len(self.selected_node_ids)} selected node(s)"
            )
            self._capture_selected_objects_initial()
            return

        self._box_select_additive = additive
        self._box_select_initial_nodes = set(self.selected_node_ids) if self._box_select_additive else set()
        self._box_select_initial_containers = (
            set(self.selected_container_ids) if self._box_select_additive else set()
        )
        self._right_drag_mode = "box_select"
        self._selection_box_world = None
        self.set_cursor_from_name("crosshair")

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
            update_container_bounds_from_nodes(self.vm)
            self._schedule_drag_pause_preview(
                self._drag_start_screen[0] + offset_x,
                self._drag_start_screen[1] + offset_y,
            )
            self.queue_draw()
            return
        if self._right_drag_mode != "box_select":
            return
        if math.hypot(offset_x, offset_y) <= 3.0:
            return
        wx0, wy0 = self._drag_start_world
        wx1, wy1 = self.screen_to_world(
            self._drag_start_screen[0] + offset_x,
            self._drag_start_screen[1] + offset_y,
        )
        bx0, bx1 = min(wx0, wx1), max(wx0, wx1)
        by0, by1 = min(wy0, wy1), max(wy0, wy1)
        self._selection_box_world = (bx0, by0, bx1, by1)
        self._apply_box_selection(bx0, by0, bx1, by1)
        self.queue_draw()

    def _on_right_drag_end(self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        self._clear_drop_preview()
        moved = math.hypot(offset_x, offset_y) > 3.0
        if self.vm and self._right_drag_mode == "objects":
            if moved and self._pending_drag_history_entry is not None:
                self.history.push_undo_entry(self._pending_drag_history_entry)
                self.vm.dirty = True
                debug_log_action(
                    "CANVAS-MOVE",
                    f"Right-drag moved selection ({len(self.selected_node_ids)} nodes, {len(self.selected_container_ids)} containers)",
                    f"dx={offset_x / self.zoom:.1f} dy={offset_y / self.zoom:.1f}",
                )
            if not self._right_drag_containers_initial and len(self.selected_node_ids) <= 1 and self.selected_node_id in self.vm.nodes:
                self._suppress_auto_history = True
                try:
                    self._maybe_reparent_dragged_node(self.selected_node_id)
                finally:
                    self._suppress_auto_history = False
            if moved and self.on_graph_modified:
                self.on_graph_modified()
        elif self.vm and self._right_drag_mode == "box_select":
            if moved:
                wx0, wy0 = self._drag_start_world
                wx1, wy1 = self.screen_to_world(
                    self._drag_start_screen[0] + offset_x,
                    self._drag_start_screen[1] + offset_y,
                )
                self._apply_box_selection(min(wx0, wx1), min(wy0, wy1), max(wx0, wx1), max(wy0, wy1))
                debug_log_action(
                    "CANVAS-BOX-SELECT",
                    f"Box-selected {len(self.selected_node_ids)} node(s)",
                    f"nodes={sorted(self.selected_node_ids)} containers={sorted(self.selected_container_ids)}",
                )
                self._notify_current_selection()
        self._selection_box_world = None
        self._pending_drag_history_entry = None
        self._right_drag_mode = "none"
        self._right_drag_nodes_initial.clear()
        self._right_drag_containers_initial.clear()
        self.set_cursor(None)
        if not moved and self.vm:
            sx, sy = self._drag_start_screen
            wx, wy = self._drag_start_world
            hit_nid = self._hit_test_node(wx, wy)
            hit_pid = (
                self.vm.nodes[hit_nid].pipeline_id
                if (hit_nid and hit_nid in self.vm.nodes)
                else self._hit_test_container(wx, wy)
            )
            if hit_nid is not None:
                if hit_nid not in self.selected_node_ids:
                    self.select_node(hit_nid)
                self._open_node_context_menu(sx, sy, node_id=hit_nid, container_id=hit_pid)
            elif hit_pid is not None:
                if hit_pid not in self.selected_container_ids:
                    self.selected_node_id = None
                    self.selected_node_ids.clear()
                    self.selected_edge_id = None
                    self.selected_container_ids = {hit_pid}
                    self.selected_container_id = hit_pid
                    self._notify_current_selection()
                self._open_node_context_menu(sx, sy, node_id=None, container_id=hit_pid)
        self.queue_draw()

    def resolve_node_source_targets(
        self,
        node_id: Optional[str] = None,
        container_id: Optional[int] = None,
    ) -> List[Tuple[str, str, str, Optional[str], Optional[str]]]:
        """Resolve available source targets for a right-clicked node or pipeline container.

        Returns a list of (menu_label, pane_kind, primary_id, secondary_id, search_hint).
        """
        targets: List[Tuple[str, str, str, Optional[str], Optional[str]]] = []
        seen_keys: Set[Tuple[str, str]] = set()
        if not self.vm:
            return targets

        def _add_target(
            label: str,
            pane_kind: str,
            primary_id: str,
            secondary_id: Optional[str] = None,
            search_hint: Optional[str] = None,
        ) -> None:
            if not primary_id:
                return
            k = (pane_kind, os.path.abspath(primary_id) if os.path.isfile(primary_id) else primary_id)
            if k in seen_keys:
                return
            seen_keys.add(k)
            targets.append((label, pane_kind, primary_id, secondary_id, search_hint))

        node = self.vm.nodes.get(node_id or "") if node_id else None
        if node is not None:
            if node.class_category == "Widget":
                if node.class_name == "pipeline" and node.pipeline_id in self.vm.containers:
                    cont = self.vm.containers[node.pipeline_id]
                    if self.workspace and cont.class_name in self.workspace.pipeline_classes:
                        pdef = self.workspace.pipeline_classes[cont.class_name]
                        pfname = os.path.basename(pdef.source_file) if pdef.source_file else f"{cont.class_name}.conf"
                        _add_target(f"View Source ({pfname})", "pipeline", cont.class_name, None, None)
                elif self.workspace and node.class_name in self.workspace.processing_modules:
                    cdef = self.workspace.processing_modules[node.class_name]
                    mfname = os.path.basename(cdef.source_file) if cdef.source_file else f"{node.class_name}.conf"
                    _add_target(
                        f"View Source ({mfname})",
                        "module",
                        node.class_name,
                        None,
                        f'Class.Widget."{node.class_name}"',
                    )
                if node.pipeline_id in self.vm.containers:
                    cont = self.vm.containers[node.pipeline_id]
                    if self.workspace and cont.class_name in self.workspace.pipeline_classes:
                        pdef = self.workspace.pipeline_classes[cont.class_name]
                        pfname = os.path.basename(pdef.source_file) if pdef.source_file else f"{cont.class_name}.conf"
                        lbl = f"View Source ({pfname})" if not targets else f"View Pipeline Source ({pfname})"
                        _add_target(
                            lbl,
                            "pipeline",
                            cont.class_name,
                            None,
                            f'{node.class_name}."{node.instance_id}"',
                        )
            elif node.class_category == "PinFormat":
                fmt_key = "output_audio_format" if "out" in node.class_name else "input_audio_format"
                if self.vm.view_kind == "module":
                    mname = self.vm.title.replace("Class.Widget.", "").strip()
                    mfname = os.path.basename(self.vm.source_file) if self.vm.source_file else f"{mname}.conf"
                    _add_target(f"View Source ({mfname})", "module", mname, None, fmt_key)
                elif self.vm.view_kind == "pipeline":
                    pname = self.vm.title.replace("Class.Pipeline.", "").strip()
                    pfname = os.path.basename(self.vm.source_file) if self.vm.source_file else f"{pname}.conf"
                    _add_target(f"View Source ({pfname})", "pipeline", pname, None, node.parent_node_id or fmt_key)
                bkey = f"Base.{node.class_name}"
                if self.workspace and bkey in self.workspace.base_classes:
                    bdef = self.workspace.base_classes[bkey]
                    bfname = os.path.basename(bdef.source_file) if bdef.source_file else f"{node.class_name}.conf"
                    lbl = f"View Source ({bfname})" if not targets else f"View Format Class Source ({bfname})"
                    _add_target(lbl, "base_class", bkey, None, None)
            elif node.class_category == "Control":
                inc_rel = (node.config.get_scalar("included_param_file") or "").strip()
                if inc_rel and self.workspace:
                    inc_abs = (
                        inc_rel
                        if os.path.isabs(inc_rel)
                        else os.path.join(self.workspace.root_dir, inc_rel)
                    )
                    if os.path.isfile(inc_abs):
                        _add_target(
                            f"View Source ({os.path.basename(inc_abs)})",
                            "file",
                            inc_abs,
                            None,
                            None,
                        )
                pnode = self.vm.nodes.get(node.parent_node_id or "") if node.parent_node_id else None
                if pnode is not None:
                    if pnode.pipeline_id in self.vm.containers:
                        cont = self.vm.containers[pnode.pipeline_id]
                        if self.workspace and cont.class_name in self.workspace.pipeline_classes:
                            pdef = self.workspace.pipeline_classes[cont.class_name]
                            pfname = os.path.basename(pdef.source_file) if pdef.source_file else f"{cont.class_name}.conf"
                            lbl = f"View Source ({pfname})" if not targets else f"View Pipeline Source ({pfname})"
                            _add_target(lbl, "pipeline", cont.class_name, None, f"Control.{node.class_name}")
                    if self.workspace and pnode.class_name in self.workspace.processing_modules:
                        cdef = self.workspace.processing_modules[pnode.class_name]
                        mfname = os.path.basename(cdef.source_file) if cdef.source_file else f"{pnode.class_name}.conf"
                        lbl = f"View Source ({mfname})" if not targets else f"View Module Source ({mfname})"
                        _add_target(lbl, "module", pnode.class_name, None, f"Control.{node.class_name}")
                bkey = f"Control.{node.class_name}"
                if self.workspace and bkey in self.workspace.base_classes:
                    bdef = self.workspace.base_classes[bkey]
                    bfname = os.path.basename(bdef.source_file) if bdef.source_file else f"{node.class_name}.conf"
                    lbl = f"View Source ({bfname})" if not targets else f"View Control Class Source ({bfname})"
                    _add_target(lbl, "base_class", bkey, None, None)
            else:
                bkey = f"{node.class_category}.{node.class_name}"
                if self.workspace and bkey in self.workspace.base_classes:
                    bdef = self.workspace.base_classes[bkey]
                    bfname = os.path.basename(bdef.source_file) if bdef.source_file else f"{node.class_name}.conf"
                    _add_target(f"View Source ({bfname})", "base_class", bkey, None, None)
                pnode = self.vm.nodes.get(node.parent_node_id or "") if node.parent_node_id else None
                if pnode is not None and self.workspace and pnode.class_name in self.workspace.processing_modules:
                    cdef = self.workspace.processing_modules[pnode.class_name]
                    mfname = os.path.basename(cdef.source_file) if cdef.source_file else f"{pnode.class_name}.conf"
                    lbl = f"View Source ({mfname})" if not targets else f"View Module Source ({mfname})"
                    _add_target(lbl, "module", pnode.class_name, None, None)

        if container_id is not None and container_id in self.vm.containers:
            cont = self.vm.containers[container_id]
            if self.workspace and cont.class_name in self.workspace.pipeline_classes:
                pdef = self.workspace.pipeline_classes[cont.class_name]
                pfname = os.path.basename(pdef.source_file) if pdef.source_file else f"{cont.class_name}.conf"
                lbl = f"View Source ({pfname})" if not targets else f"View Pipeline Source ({pfname})"
                _add_target(lbl, "pipeline", cont.class_name, None, None)

        if self.vm.source_file and os.path.isfile(self.vm.source_file):
            tfname = os.path.basename(self.vm.source_file)
            kind = "topology" if self.vm.view_kind == "topology" else (
                self.vm.view_kind if self.vm.view_kind in ("module", "pipeline", "base_class") else "file"
            )
            prim_id = self.vm.source_file
            if kind == "module":
                prim_id = self.vm.title.replace("Class.Widget.", "").strip() or self.vm.source_file
            elif kind == "pipeline":
                prim_id = self.vm.title.replace("Class.Pipeline.", "").strip() or self.vm.source_file
            elif kind == "base_class":
                prim_id = self.vm.title.replace("Class.", "").strip() or self.vm.source_file
            lbl = f"View Source ({tfname})" if not targets else f"View Topology Source ({tfname})"
            hint = node.class_name if node else (
                self.vm.containers[container_id].class_name
                if (container_id is not None and container_id in self.vm.containers)
                else None
            )
            _add_target(lbl, kind, prim_id, None, hint)

        return targets

    def _open_node_context_menu(
        self,
        sx: float,
        sy: float,
        node_id: Optional[str] = None,
        container_id: Optional[int] = None,
    ) -> Optional[Gtk.Popover]:
        """Open a right-click context menu Popover at (sx, sy) for a node or pipeline container."""
        if not self.vm:
            return None
        if self._active_context_popover is not None:
            try:
                self._active_context_popover.popdown()
            except Exception:
                pass
            self._active_context_popover = None

        targets = self.resolve_node_source_targets(node_id=node_id, container_id=container_id)
        if not targets:
            return None

        pop = Gtk.Popover()
        pop.set_parent(self)
        rect = Gdk.Rectangle()
        rect.x = int(sx)
        rect.y = int(sy)
        rect.width = 1
        rect.height = 1
        pop.set_pointing_to(rect)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        node = self.vm.nodes.get(node_id or "") if node_id else None
        if node is not None:
            hdr_text = f"{node.title} ({node.class_name})"
        elif container_id is not None and container_id in self.vm.containers:
            cont = self.vm.containers[container_id]
            hdr_text = f"Pipeline {container_id}: {cont.class_name}"
        else:
            hdr_text = self.vm.title

        hdr = Gtk.Label(label=hdr_text, xalign=0.0)
        hdr.add_css_class("heading")
        box.append(hdr)

        for idx, (menu_lbl, p_kind, p_id, s_id, s_hint) in enumerate(targets):
            btn = Gtk.Button(label=f"📄 {menu_lbl}")
            if idx == 0:
                btn.add_css_class("suggested-action")

            def _on_view_source_clicked(
                _b: Gtk.Button,
                pk: str = p_kind,
                pid: str = p_id,
                sid: Optional[str] = s_id,
                sh: Optional[str] = s_hint,
            ) -> None:
                pop.popdown()
                if self.on_view_source_requested:
                    self.on_view_source_requested(pk, pid, sid, sh)

            btn.connect("clicked", _on_view_source_clicked)
            box.append(btn)

        pop.set_child(box)
        self._active_context_popover = pop

        def _on_closed(p: Gtk.Popover) -> None:
            if self._active_context_popover is p:
                self._active_context_popover = None
            p.unparent()

        pop.connect("closed", _on_closed)
        pop.popup()
        return pop

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
        if ctrl_held and keyval in (Gdk.KEY_a, Gdk.KEY_A):
            self.select_all()
            return True
        if ctrl_held and keyval in (Gdk.KEY_c, Gdk.KEY_C):
            self.copy_selected()
            return True
        if ctrl_held and keyval in (Gdk.KEY_x, Gdk.KEY_X):
            self.cut_selected()
            return True
        if ctrl_held and keyval in (Gdk.KEY_v, Gdk.KEY_V):
            self.paste_clipboard()
            return True
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
        self.vm.graph_structure_modified = True
        debug_log_action(
            "CANVAS-WIRE",
            f"Wired route {src_nid} ({src_port}) -> {snk_nid} ({snk_port})",
            f"edge_id={new_edge.id} inter_pipeline={is_inter}",
        )
        if self.on_graph_modified:
            self.on_graph_modified()
        self.queue_draw()

    def delete_selected(self) -> None:
        if not self.vm:
            return
        if self.selected_edge_id:
            self.record_undo_snapshot("Delete route")
            debug_log_action("CANVAS-DELETE", f"Deleted route {self.selected_edge_id}")
            self.vm.edges = [e for e in self.vm.edges if e.id != self.selected_edge_id]
            self.selected_edge_id = None
            self.vm.dirty = True
            self.vm.graph_structure_modified = True
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
            debug_log_action(
                "CANVAS-DELETE",
                "Deleted selected objects",
                f"nodes={sorted(nids)} pipelines={sorted(pids)}",
            )

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
            self.vm.graph_structure_modified = True
            update_container_bounds_from_nodes(self.vm)
            self._notify_current_selection()
            if self.on_graph_modified:
                self.on_graph_modified()
            self.queue_draw()

    @classmethod
    def has_clipboard(cls) -> bool:
        return bool(cls._clipboard_nodes or cls._clipboard_containers)

    def select_all(self) -> int:
        """Select all nodes and pipeline containers in the current graph."""
        if not self.vm:
            return 0
        self.selected_edge_id = None
        self.selected_node_ids = {
            nid for nid, n in self.vm.nodes.items()
            if n.class_category != "TestbenchIO"
        }
        self.selected_node_id = next(iter(self.selected_node_ids), None)
        self.selected_container_ids = set(self.vm.containers.keys())
        self.selected_container_id = next(iter(self.selected_container_ids), None)
        self._notify_current_selection()
        self.queue_draw()
        return len(self.selected_node_ids) + len(self.selected_container_ids)

    def copy_selected(self) -> int:
        """Copy selected nodes (including attached child Kcontrols/PinFormats), containers, and routes to clipboard."""
        if not self.vm:
            return 0
        sel_nids: Set[str] = {
            nid
            for nid in self.selected_node_ids
            if nid in self.vm.nodes and self.vm.nodes[nid].class_category not in ("TestbenchIO", "PinFormat")
        }
        if (
            self.selected_node_id
            and self.selected_node_id in self.vm.nodes
            and self.vm.nodes[self.selected_node_id].class_category not in ("TestbenchIO", "PinFormat")
        ):
            sel_nids.add(self.selected_node_id)

        sel_pids: Set[int] = {pid for pid in self.selected_container_ids if pid in self.vm.containers}
        if self.selected_container_id is not None and self.selected_container_id in self.vm.containers:
            sel_pids.add(self.selected_container_id)

        for pid in sel_pids:
            for nid, n in self.vm.nodes.items():
                if n.pipeline_id == pid and n.class_category not in ("TestbenchIO", "PinFormat"):
                    sel_nids.add(nid)

        # When copying a multi-node group, also include any pipeline container whose nodes are in the selection
        copied_pids: Set[int] = set(sel_pids)
        if len(sel_nids) > 1:
            for nid in sel_nids:
                n = self.vm.nodes.get(nid)
                if n and n.pipeline_id is not None and n.pipeline_id in self.vm.containers:
                    copied_pids.add(n.pipeline_id)

        # Include attached child Kcontrols and PinFormat callouts for any selected module node
        all_nids = set(sel_nids)
        for cid, cn in self.vm.nodes.items():
            if cn.parent_node_id and cn.parent_node_id in sel_nids and cn.class_category != "TestbenchIO":
                all_nids.add(cid)

        if not all_nids and not copied_pids:
            return 0

        TopologyGraphCanvas._clipboard_containers = [
            self.vm.containers[pid].clone()
            for pid in sorted(copied_pids)
            if pid in self.vm.containers
        ]
        TopologyGraphCanvas._clipboard_nodes = [
            self.vm.nodes[nid].clone()
            for nid in sorted(all_nids)
            if nid in self.vm.nodes
        ]
        TopologyGraphCanvas._clipboard_edges = [
            e.clone()
            for e in self.vm.edges
            if e.source_node in all_nids and e.sink_node in all_nids
        ]
        TopologyGraphCanvas._clipboard_paste_count = 0

        # Also serialize the copied topology objects into the system Gdk.Clipboard as .conf text
        try:
            from .serializer import serialize_config_node

            conf_blocks: List[str] = []
            for c_clip in TopologyGraphCanvas._clipboard_containers:
                if c_clip.config:
                    conf_blocks.append(serialize_config_node(c_clip.config))
            for n_clip in TopologyGraphCanvas._clipboard_nodes:
                if n_clip.class_category != "PinFormat" and n_clip.config:
                    conf_blocks.append(serialize_config_node(n_clip.config))
            for e_clip in TopologyGraphCanvas._clipboard_edges:
                if e_clip.config and e_clip.config.key == "Object.Base.route":
                    conf_blocks.append(serialize_config_node(e_clip.config))
            clip_text = "\n".join(b.rstrip() for b in conf_blocks if b.strip())
            if clip_text:
                sys_clip = self.get_clipboard()
                if sys_clip is not None:
                    sys_clip.set(clip_text)
        except Exception:
            pass

        primary_cnt = len(sel_nids)
        debug_log_action(
            "CANVAS-COPY",
            f"Copied {primary_cnt} node(s) and {len(copied_pids)} container(s) to clipboard",
            f"nodes={sorted(sel_nids)} containers={sorted(copied_pids)} routes={len(TopologyGraphCanvas._clipboard_edges)}",
        )
        return primary_cnt if primary_cnt > 0 else len(TopologyGraphCanvas._clipboard_containers)

    def cut_selected(self) -> int:
        """Cut selected objects to clipboard and delete them in a single Undo step."""
        count = self.copy_selected()
        if count > 0:
            self.delete_selected()
        return count

    def paste_clipboard(self) -> int:
        """Paste objects from clipboard into the current graph with unique IDs and offset positions."""
        if not self.vm or not self.has_clipboard():
            return 0

        self.record_undo_snapshot("Paste object(s)")
        TopologyGraphCanvas._clipboard_paste_count += 1
        offset = 36.0 * TopologyGraphCanvas._clipboard_paste_count

        # 1. Map old pipeline_id -> new pipeline_id for any copied containers
        pid_map: Dict[int, int] = {}
        new_pids: Set[int] = set()
        for c_orig in TopologyGraphCanvas._clipboard_containers:
            used_pids = set(self.vm.containers.keys()) | {
                n.pipeline_id for n in self.vm.nodes.values() if n.pipeline_id is not None
            }
            new_pid = c_orig.pipeline_id
            if new_pid in used_pids:
                new_pid = 1
                while new_pid in used_pids:
                    new_pid += 1
            pid_map[c_orig.pipeline_id] = new_pid
            c_new = c_orig.clone()
            c_new.pipeline_id = new_pid
            c_new.x += offset
            c_new.y += offset
            c_new.config.set_scalar("index", str(new_pid), origin="pipeline")
            self.vm.containers[new_pid] = c_new
            new_pids.add(new_pid)

        # Target pipeline if pasting standalone nodes without containers
        default_target_pid: Optional[int] = self.selected_container_id
        if default_target_pid is None and self.vm.containers:
            default_target_pid = sorted(self.vm.containers.keys())[0]

        # 2. First pass: clone primary (non-child) nodes and map old_nid -> new_nid
        nid_map: Dict[str, str] = {}
        new_nids: Set[str] = set()
        primary_nodes = [n for n in TopologyGraphCanvas._clipboard_nodes if not n.parent_node_id]
        child_nodes = [n for n in TopologyGraphCanvas._clipboard_nodes if n.parent_node_id]

        for n_orig in primary_nodes:
            n_new = n_orig.clone()
            if n_orig.pipeline_id in pid_map:
                target_pid = pid_map[n_orig.pipeline_id]
            elif n_orig.pipeline_id is not None and n_orig.pipeline_id in self.vm.containers:
                target_pid = n_orig.pipeline_id
            else:
                target_pid = default_target_pid if default_target_pid is not None else (n_orig.pipeline_id or 1)

            n_new.pipeline_id = target_pid
            inst_num = 1
            while f"{n_new.class_name}.{target_pid}.{inst_num}" in self.vm.nodes:
                inst_num += 1
            new_id = f"{n_new.class_name}.{target_pid}.{inst_num}"
            n_new.id = new_id
            n_new.title = new_id
            n_new.instance_id = str(inst_num)
            n_new.x += offset
            n_new.y += offset
            n_new.config.set_scalar("index", str(target_pid), origin="instance")
            n_new.config.set_scalar("instance", str(inst_num), origin="instance")
            self.vm.nodes[new_id] = n_new
            nid_map[n_orig.id] = new_id
            new_nids.add(new_id)

        # 3. Second pass: clone child Kcontrol and PinFormat nodes attached to copied parents
        for c_orig in child_nodes:
            c_new = c_orig.clone()
            new_parent_id = nid_map.get(c_orig.parent_node_id or "", c_orig.parent_node_id)
            c_new.parent_node_id = new_parent_id
            parent_node = self.vm.nodes.get(new_parent_id or "")
            if parent_node:
                c_new.pipeline_id = parent_node.pipeline_id
            if c_orig.class_category == "PinFormat":
                suffix = c_orig.id.split(".pinfmt.")[-1] if ".pinfmt." in c_orig.id else "fmt"
                base_cid = f"{new_parent_id}.pinfmt.{suffix}" if new_parent_id else f"{c_orig.id}_copy"
            else:
                suffix = c_orig.id.split(".ctl.")[-1] if ".ctl." in c_orig.id else c_orig.instance_id
                base_cid = f"{new_parent_id}.ctl.{suffix}" if new_parent_id else f"{c_orig.id}_copy"
            new_cid = base_cid
            k = 2
            while new_cid in self.vm.nodes:
                new_cid = f"{base_cid}_{k}"
                k += 1
            c_new.id = new_cid
            c_new.x += offset
            c_new.y += offset
            self.vm.nodes[new_cid] = c_new
            nid_map[c_orig.id] = new_cid
            if c_orig.class_category != "PinFormat":
                new_nids.add(new_cid)

        # 4. Reconstruct internal routes between pasted nodes
        for idx, e_orig in enumerate(TopologyGraphCanvas._clipboard_edges):
            u_new = nid_map.get(e_orig.source_node)
            v_new = nid_map.get(e_orig.sink_node)
            if u_new and v_new and u_new in self.vm.nodes and v_new in self.vm.nodes:
                e_new = e_orig.clone()
                e_new.id = f"paste_edge_{len(self.vm.edges) + idx + 1}"
                e_new.source_node = u_new
                e_new.sink_node = v_new
                u_node = self.vm.nodes[u_new]
                v_node = self.vm.nodes[v_new]
                e_new.is_inter_pipeline = (
                    u_node.pipeline_id is not None
                    and v_node.pipeline_id is not None
                    and u_node.pipeline_id != v_node.pipeline_id
                )
                e_new.pipeline_id = u_node.pipeline_id if not e_new.is_inter_pipeline else None
                e_new.config.set_scalar("source", u_new, origin="instance")
                e_new.config.set_scalar("sink", v_new, origin="instance")
                self.vm.edges.append(e_new)

        update_container_bounds_from_nodes(self.vm)
        self.selected_edge_id = None
        self.selected_node_ids = set(new_nids)
        self.selected_node_id = next(iter(new_nids), None)
        self.selected_container_ids = set(new_pids)
        self.selected_container_id = next(iter(new_pids), None)
        self.vm.dirty = True
        self.vm.graph_structure_modified = True
        debug_log_action(
            "CANVAS-PASTE",
            f"Pasted {len(new_nids)} node(s) and {len(new_pids)} container(s) from clipboard",
            f"nodes={sorted(new_nids)} containers={sorted(new_pids)}",
        )
        self._notify_current_selection()
        if self.on_graph_modified:
            self.on_graph_modified()
        self.queue_draw()
        return len(new_nids) if new_nids else len(new_pids)

    def zoom_in(self) -> None:
        """Zoom in on the center of the graph canvas."""
        w = float(self.get_width() or 1200)
        h = float(self.get_height() or 800)
        cx, cy = w * 0.5, h * 0.5
        wx, wy = self.screen_to_world(cx, cy)
        self.zoom = min(2.8, self.zoom * 1.18)
        self.pan_x = cx - wx * self.zoom
        self.pan_y = cy - wy * self.zoom
        self.queue_draw()

    def zoom_out(self) -> None:
        """Zoom out from the center of the graph canvas."""
        w = float(self.get_width() or 1200)
        h = float(self.get_height() or 800)
        cx, cy = w * 0.5, h * 0.5
        wx, wy = self.screen_to_world(cx, cy)
        self.zoom = max(0.18, self.zoom / 1.18)
        self.pan_x = cx - wx * self.zoom
        self.pan_y = cy - wy * self.zoom
        self.queue_draw()

    def reset_zoom_1to1(self) -> None:
        """Reset zoom to 100% (1:1) centered on the graph."""
        self.zoom = 1.0
        self.pan_x = 40.0
        self.pan_y = 44.0
        self.queue_draw()

    def export_to_pdf(self, pdf_path: str) -> bool:
        """Render the complete topology graph onto an ISO A4 page (841.89 x 595.28 pt) vector PDF file."""
        if not self.vm:
            return False
        import cairo

        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")

        for cont in self.vm.containers.values():
            min_x = min(min_x, cont.x)
            min_y = min(min_y, cont.y)
            max_x = max(max_x, cont.x + cont.width)
            max_y = max(max_y, cont.y + cont.height)

        for node in self.vm.nodes.values():
            pad = (CTRL_GROUP_PAD + CTRL_GROUP_HDR + 8.0) if node.class_category == "Control" else 0.0
            min_x = min(min_x, node.x - pad)
            min_y = min(min_y, node.y - pad)
            max_x = max(max_x, node.x + node.width + pad)
            max_y = max(max_y, node.y + node.height + pad)

        if min_x == float("inf"):
            min_x, min_y, max_x, max_y = 0.0, 0.0, 720.0, 460.0

        content_w = max(1.0, max_x - min_x)
        content_h = max(1.0, max_y - min_y)

        # Standard ISO A4 page size in PDF points (297mm x 210mm @ 72 pt/inch = 841.89 x 595.28 pt)
        a4_long = 841.89
        a4_short = 595.28
        if content_h > content_w * 1.25:
            page_w, page_h = a4_short, a4_long
        else:
            page_w, page_h = a4_long, a4_short

        margin_x = 24.0
        margin_y = 20.0
        header_h = 34.0
        avail_w = max(100.0, page_w - 2.0 * margin_x)
        avail_h = max(100.0, page_h - header_h - 2.0 * margin_y)

        scale = min(1.15, avail_w / content_w, avail_h / content_h)
        tx = margin_x + (avail_w - content_w * scale) * 0.5 - min_x * scale
        ty = header_h + margin_y + (avail_h - content_h * scale) * 0.5 - min_y * scale

        snap = Gtk.Snapshot.new()

        # 1. Background fill across full A4 page
        bounds = Graphene.Rect.alloc()
        bounds.init(0.0, 0.0, page_w, page_h)
        snap.append_color(BG_COLOR, bounds)

        # 2. Subtle grid in A4 page space
        old_pan_x, old_pan_y, old_zoom = self.pan_x, self.pan_y, self.zoom
        try:
            self.pan_x = tx
            self.pan_y = ty
            self.zoom = scale
            self._snapshot_grid(snap, page_w, page_h)

            # 3. Push world-to-A4-page transform (centered & scaled to fit A4)
            snap.save()
            pt = Graphene.Point.alloc()
            pt.init(tx, ty)
            snap.translate(pt)
            snap.scale(scale, scale)

            for pid, cont in self.vm.containers.items():
                self._snapshot_container(snap, pid, cont)

            for edge in self.vm.edges:
                self._snapshot_edge(snap, edge)

            self._snapshot_module_kcontrol_groups(
                snap,
                min_x - 500.0,
                min_y - 500.0,
                max_x + 500.0,
                max_y + 500.0,
            )

            for nid, node in self.vm.nodes.items():
                self._snapshot_node(snap, node, is_selected=False)

            snap.restore()

            # 4. Header banner on top of the A4 PDF page
            hdr_rect = Graphene.Rect.alloc()
            hdr_rect.init(0.0, 0.0, page_w, header_h)
            snap.append_color(rgba(0.08, 0.10, 0.14, 0.96), hdr_rect)

            src_info = f"  —  {os.path.basename(self.vm.source_file)}" if self.vm.source_file else ""
            title_txt = (
                f"{self.vm.title}{src_info}   "
                f"({len(self.vm.containers)} pipelines, {len(self.vm.nodes)} nodes, {len(self.vm.edges)} routes)"
            )
            lyt = self._get_pango_layout(title_txt, size_pt=9, bold=True)
            snap.save()
            tpt = Graphene.Point.alloc()
            tpt.init(14.0, 9.0)
            snap.translate(tpt)
            snap.append_layout(lyt, TEXT_WHITE)
            snap.restore()
        finally:
            self.pan_x, self.pan_y, self.zoom = old_pan_x, old_pan_y, old_zoom

        render_node = snap.to_node()
        if render_node is None:
            return False

        parent_dir = os.path.dirname(os.path.abspath(pdf_path))
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        surface = cairo.PDFSurface(pdf_path, page_w, page_h)
        cr = cairo.Context(surface)
        render_node.draw(cr)
        surface.show_page()
        surface.finish()
        return os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 0

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
            self.vm.graph_structure_modified = True
            update_container_bounds_from_nodes(self.vm)
            self.workspace.attach_host_copier_io_nodes(self.vm)
            for tb_node in self.vm.nodes.values():
                if tb_node.class_category == "TestbenchIO":
                    _compute_node_geometry(tb_node)
            _position_testbench_io_nodes(self.vm)
            _assign_edge_ports(self.vm)
            self.select_node(node.id)
            debug_log_action(
                "CANVAS-ADD-MODULE",
                f"Added module {node.id} (Class.Widget.{module_name})",
                f"pipeline_id={pid} pos=({node.x:.0f},{node.y:.0f})",
            )
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
            self.vm.graph_structure_modified = True
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
            debug_log_action(
                "CANVAS-ADD-PIPELINE",
                f"Added pipeline {new_pid} (Class.Pipeline.{pipeline_name})",
                f"joined_to={target_pid}",
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

    def select_testbench_io_radio(self, active_nid: str, additive: bool = False) -> None:
        """Select a TestbenchIO node to enable its downstream/upstream pipelines.

        When `additive=False`, acts as an exclusive radio selection on that side.
        When `additive=True` (Ctrl+click), toggles/adds `active_nid` so multiple PCMs
        can run simultaneously in the testbench.
        """
        if not self.vm or active_nid not in self.vm.nodes:
            return
        active_node = self.vm.nodes[active_nid]
        active_side = str(active_node.config.get_scalar("side", "left")).lower()

        side_nodes = [
            (nid, n)
            for nid, n in self.vm.nodes.items()
            if n.class_category == "TestbenchIO"
            and str(n.config.get_scalar("side", "left")).lower() == active_side
        ]
        # Order top-to-bottom by vertical position in the Main Graph so waveforms align vertically with PCMs
        side_nodes.sort(key=lambda item: (item[1].y, item[1].x))

        if not additive:
            # Exclusive radio selection on active_side
            for nid, n in side_nodes:
                n.config.set_scalar("enabled", "true" if nid == active_nid else "false", origin="instance")
        else:
            # Ctrl+click multi-selection: toggle active_nid while keeping at least one enabled
            cur_enabled = str(active_node.config.get_scalar("enabled", "false")).lower() == "true"
            other_enabled_cnt = sum(
                1
                for nid, n in side_nodes
                if nid != active_nid and str(n.config.get_scalar("enabled", "false")).lower() == "true"
            )
            if cur_enabled and other_enabled_cnt > 0:
                active_node.config.set_scalar("enabled", "false", origin="instance")
            else:
                active_node.config.set_scalar("enabled", "true", origin="instance")

        # Collect ordered, deduplicated pipeline IDs across all enabled TestbenchIO nodes on active_side
        combined_pids: List[int] = []
        enabled_titles: List[str] = []
        for nid, n in side_nodes:
            if str(n.config.get_scalar("enabled", "false")).lower() == "true":
                enabled_titles.append(n.title)
                tp_str = str(n.config.get_scalar("target_pipelines", "")).strip()
                for token in tp_str.replace("→", " ").replace(",", " ").split():
                    if token.lstrip("-").isdigit():
                        pid = int(token)
                        if pid not in combined_pids:
                            combined_pids.append(pid)
        active_pids_set = set(combined_pids)

        # Synchronize connected TestbenchIO endpoint(s) on the opposite side
        opp_side = "right" if active_side == "left" else "left"
        opp_nodes = [
            n for n in self.vm.nodes.values()
            if n.class_category == "TestbenchIO"
            and str(n.config.get_scalar("side", "left")).lower() == opp_side
        ]
        opp_nodes.sort(key=lambda n: (n.y, n.x))
        connected_opp: List[GraphNode] = []
        for n in opp_nodes:
            parent = self.vm.nodes.get(n.parent_node_id or "")
            parent_pid = parent.pipeline_id if parent else None
            is_connected = parent_pid in active_pids_set if parent_pid is not None else bool(
                self._parse_pipe_ids(str(n.config.get_scalar("target_pipelines", ""))) & active_pids_set
            )
            if is_connected:
                connected_opp.append(n)

        # If any already-enabled opposite endpoint is connected to the active selection,
        # keep the user's opposite selection subset; otherwise enable all connected opposite endpoints.
        already_enabled_connected = [
            n for n in connected_opp
            if str(n.config.get_scalar("enabled", "false")).lower() == "true"
        ]
        target_enabled_opp_ids = (
            {n.id for n in already_enabled_connected}
            if (additive and already_enabled_connected)
            else {n.id for n in connected_opp}
        )
        for n in opp_nodes:
            n.config.set_scalar(
                "enabled",
                "true" if n.id in target_enabled_opp_ids else "false",
                origin="instance",
            )

        from .serializer import _extract_active_pipeline_ids
        effective_pids = _extract_active_pipeline_ids(self.vm)

        self.select_node(active_nid, additive=additive)
        if additive:
            for nid, n in side_nodes:
                if str(n.config.get_scalar("enabled", "false")).lower() == "true":
                    self.selected_node_ids.add(nid)
                else:
                    self.selected_node_ids.discard(nid)

        debug_log_action(
            "CANVAS-TB-MULTI" if additive else "CANVAS-TB-RADIO",
            f"Enabled TestbenchIO {enabled_titles}",
            f"clicked={active_nid} side={active_side} additive={additive} target_pipelines={effective_pids}",
        )
        if self.on_testbench_io_changed:
            self.on_testbench_io_changed(list(effective_pids))
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

    SIGNAL_TYPE_OPTIONS: List[Tuple[str, str, str]] = [
        ("sine_997", "Sine 997 Hz", "Sine 997 Hz (-6 dBFS)"),
        ("sine_1000", "Sine 1 kHz", "Sine 1 kHz (-6 dBFS)"),
        ("sine_440", "Sine 440 Hz", "Sine 440 Hz (-6 dBFS)"),
        ("chirp", "Chirp 100-12kHz", "Chirp Sweep 100 Hz – 12 kHz"),
        ("multitone", "Multitone", "Multitone (250 Hz + 997 Hz + 4 kHz)"),
        ("square", "Square 500 Hz", "Square Wave 500 Hz"),
        ("sawtooth", "Sawtooth 500 Hz", "Sawtooth Wave 500 Hz"),
        ("noise", "White Noise", "White Noise (-9 dBFS)"),
        ("pink_noise", "Pink Noise", "Pink Noise (1/f Spectrum)"),
        ("impulse", "Impulse Train", "Dirac Impulse Train (20 Hz)"),
        ("remote_tone", "Remote 1kHz Tone", "SOFR Remote 1 kHz Tone"),
    ]

    @classmethod
    def get_signal_type_short_label(cls, sig_type: str) -> str:
        """Return compact human-readable label for a signal_type key."""
        key = (sig_type or "sine_997").strip().lower()
        for skey, short_lbl, _full_lbl in cls.SIGNAL_TYPE_OPTIONS:
            if skey == key:
                return short_lbl
        return sig_type or "Sine 997 Hz"

    def _open_tb_io_type_enum_popover(self, node: GraphNode, sx: float, sy: float) -> None:
        """Open a GTK4 Enum Popover at (sx, sy) to select I/O mode (Audio File, ALSA PCM, or Signal Generator)."""
        is_source = node.class_name == "testbench-source"
        cur_mode = (node.config.get_scalar("io_type") or "file").strip().lower()
        if not is_source and cur_mode == "sig_gen":
            cur_mode = "file"

        options: List[Tuple[str, str]] = [
            ("file", "Audio File (.wav / .raw)"),
            ("alsa_pcm", "ALSA PCM Device"),
        ]
        if is_source:
            options.append(("sig_gen", "Signal Generator"))

        pop = Gtk.Popover()
        pop.set_parent(self)
        rect = Gdk.Rectangle()
        rect.x = int(sx)
        rect.y = int(sy)
        rect.width = 1
        rect.height = 1
        pop.set_pointing_to(rect)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        hdr = Gtk.Label(
            label=f"{node.title} — {'Source' if is_source else 'Sink'} I/O Mode",
            xalign=0.0,
        )
        hdr.add_css_class("heading")
        box.append(hdr)

        for mode_key, mode_label in options:
            prefix = "● " if mode_key == cur_mode else "○ "
            btn = Gtk.Button(label=f"{prefix}{mode_label}")
            if mode_key == cur_mode:
                btn.add_css_class("suggested-action")

            def _on_pick(_b: Gtk.Button, chosen: str = mode_key) -> None:
                if chosen != cur_mode:
                    self.record_undo_snapshot(f"Set {node.title} I/O mode to {chosen}")
                    node.config.set_scalar(
                        "io_type",
                        chosen,
                        origin="instance",
                        valid_values=["file", "alsa_pcm", "sig_gen"] if is_source else ["file", "alsa_pcm"],
                    )
                    if chosen == "sig_gen" and not node.config.get_scalar("signal_type"):
                        node.config.set_scalar(
                            "signal_type",
                            "sine_997",
                            category_info="Signal Generator waveform type (when io_type=sig_gen)",
                            origin="instance",
                            valid_values=[opt[0] for opt in self.SIGNAL_TYPE_OPTIONS],
                        )
                    if self.vm:
                        self.vm.dirty = True
                    self.select_node(node.id)
                    if self.on_graph_modified:
                        self.on_graph_modified()
                    self.queue_draw()
                pop.popdown()

            btn.connect("clicked", _on_pick)
            box.append(btn)

        pop.set_child(box)
        pop.connect("closed", lambda p: p.unparent())
        pop.popup()

    def _open_tb_sig_gen_popover(self, node: GraphNode, sx: float, sy: float) -> None:
        """Open a GTK4 Enum Popover at (sx, sy) to select Signal Generator waveform type."""
        cur_sig = (node.config.get_scalar("signal_type") or "sine_997").strip().lower()

        pop = Gtk.Popover()
        pop.set_parent(self)
        rect = Gdk.Rectangle()
        rect.x = int(sx)
        rect.y = int(sy)
        rect.width = 1
        rect.height = 1
        pop.set_pointing_to(rect)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        hdr = Gtk.Label(
            label=f"{node.title} — Signal Generator Type",
            xalign=0.0,
        )
        hdr.add_css_class("heading")
        box.append(hdr)

        for sig_key, _short_lbl, full_lbl in self.SIGNAL_TYPE_OPTIONS:
            prefix = "● " if sig_key == cur_sig else "○ "
            btn = Gtk.Button(label=f"{prefix}{full_lbl}")
            if sig_key == cur_sig:
                btn.add_css_class("suggested-action")

            def _on_pick_sig(_b: Gtk.Button, chosen_sig: str = sig_key) -> None:
                self.record_undo_snapshot(f"Set {node.title} signal type to {chosen_sig}")
                node.config.set_scalar(
                    "signal_type",
                    chosen_sig,
                    category_info="Signal Generator waveform type (when io_type=sig_gen)",
                    origin="instance",
                    valid_values=[opt[0] for opt in self.SIGNAL_TYPE_OPTIONS],
                )
                node.config.set_scalar("io_type", "sig_gen", origin="instance")
                if self.vm:
                    self.vm.dirty = True
                self.select_node(node.id)
                if self.on_graph_modified:
                    self.on_graph_modified()
                self.queue_draw()
                pop.popdown()

            btn.connect("clicked", _on_pick_sig)
            box.append(btn)

        pop.set_child(box)
        pop.connect("closed", lambda p: p.unparent())
        pop.popup()

    # -------------------------------------------------------------------------
    # Interactive Kcontrol Graphical Widget Helpers (Slider / Enum / Bytes)
    # -------------------------------------------------------------------------

    def _set_kcontrol_volume_from_local_y(self, node: GraphNode, ly: float, notify: bool = True) -> None:
        """Update a vertical volume slider Kcontrol node value based on local Y position on the track."""
        _, max_v = self._get_kcontrol_volume_state(node)
        track_y = 21.0
        track_h = max(32.0, node.height - 37.0)
        ratio = max(0.0, min(1.0, 1.0 - (ly - track_y) / track_h))
        new_val = int(round(ratio * max_v))
        node.config.set_scalar("value", str(new_val), origin="instance")
        if self.vm:
            self.vm.dirty = True
        if notify:
            if self.on_kcontrol_changed:
                self.on_kcontrol_changed(node)
            if self.on_graph_modified:
                self.on_graph_modified()
        self.queue_draw()

    def _set_kcontrol_volume_from_local_x(self, node: GraphNode, lx: float, notify: bool = True) -> None:
        """Compatibility helper delegating to vertical fader setter."""
        self._set_kcontrol_volume_from_local_y(node, lx, notify=notify)

    def _open_kcontrol_enum_popover(self, node: GraphNode, sx: float, sy: float) -> None:
        """Open a GTK4 Popover at (sx, sy) to select an option for a control_enum node."""
        cur_opt, options = self._get_kcontrol_enum_state(node)
        cur_idx = options.index(cur_opt) if cur_opt in options else 0
        pop = Gtk.Popover()
        pop.set_parent(self)
        rect = Gdk.Rectangle()
        rect.x = int(sx)
        rect.y = int(sy)
        rect.width = 1
        rect.height = 1
        pop.set_pointing_to(rect)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        title = Gtk.Label(
            label=f"Enum: {self._get_kcontrol_short_name(node)}",
            xalign=0.0,
        )
        title.add_css_class("heading")
        box.append(title)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_max_content_height(240)
        scroll.set_propagate_natural_height(True)

        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for idx, opt in enumerate(options):
            btn = Gtk.Button(label=f"{opt}   (#{idx})")
            if idx == cur_idx:
                btn.add_css_class("suggested-action")
            else:
                btn.add_css_class("flat")

            def _pick(_b: Gtk.Button, i: int = idx, label_txt: str = opt) -> None:
                self.record_undo_snapshot(f"Set {self._get_kcontrol_short_name(node)} to {label_txt}")
                node.config.set_scalar("value", label_txt, origin="instance")
                if self.vm:
                    self.vm.dirty = True
                self.select_node(node.id)
                if self.on_kcontrol_changed:
                    self.on_kcontrol_changed(node)
                if self.on_graph_modified:
                    self.on_graph_modified()
                self.queue_draw()
                pop.popdown()

            btn.connect("clicked", _pick)
            list_box.append(btn)

        scroll.set_child(list_box)
        box.append(scroll)
        pop.set_child(box)
        pop.connect("closed", lambda p: p.unparent())
        pop.popup()

    def _open_kcontrol_bytes_file_chooser(self, node: GraphNode) -> None:
        """Open a GTK4 FileDialog to choose a bytes parameter configuration/blob file."""
        dialog = Gtk.FileDialog()
        dialog.set_title(f"Select Bytes Parameter File ({self._get_kcontrol_short_name(node)})")
        curr_file = self._get_kcontrol_bytes_file(node)
        tplg_root = (
            self.workspace.root_dir
            if (self.workspace and os.path.isdir(self.workspace.root_dir))
            else (os.path.dirname(os.path.abspath(self.vm.source_file)) if (self.vm and self.vm.source_file) else "")
        )
        base_dir = tplg_root
        if self.vm and node.parent_node_id and node.parent_node_id in self.vm.nodes and tplg_root:
            parent_wclass = self.vm.nodes[node.parent_node_id].class_name
            comp_dir = os.path.join(tplg_root, "include", "components", parent_wclass)
            if os.path.isdir(comp_dir):
                base_dir = comp_dir
        if curr_file and not curr_file.startswith("Choose ") and not curr_file.startswith("(") and tplg_root:
            candidate = curr_file if os.path.isabs(curr_file) else os.path.join(tplg_root, curr_file)
            if os.path.exists(candidate):
                base_dir = os.path.dirname(candidate)
        if base_dir and os.path.isdir(base_dir):
            dialog.set_initial_folder(Gio.File.new_for_path(base_dir))

        root = self.get_root()
        win = root if isinstance(root, Gtk.Window) else None

        def _on_done(dlg: Gtk.FileDialog, res: Gio.AsyncResult) -> None:
            try:
                gfile = dlg.open_finish(res)
                if gfile and gfile.get_path():
                    chosen = gfile.get_path()
                    # Store relative path when inside topology2 root if possible
                    if tplg_root:
                        try:
                            rel = os.path.relpath(chosen, tplg_root)
                            if not rel.startswith(".."):
                                chosen = rel
                        except ValueError:
                            pass
                    self.record_undo_snapshot(f"Set {self._get_kcontrol_short_name(node)} bytes file")
                    node.config.set_scalar("included_param_file", chosen, origin="instance")
                    node.badges = [os.path.basename(chosen)]
                    if self.vm:
                        self.vm.dirty = True
                        self.vm.graph_structure_modified = True
                    self.select_node(node.id)
                    if self.on_kcontrol_changed:
                        self.on_kcontrol_changed(node)
                    if self.on_graph_modified:
                        self.on_graph_modified()
                    self.queue_draw()
            except Exception:
                pass

        dialog.open(win, None, _on_done)

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
                req_w = int(min(940.0, max(640.0, gw * 1.25 + 48.0)))
                req_h = int(min(540.0, max(280.0, gh * 1.25 + 44.0)))
            else:
                req_w = int(min(1120.0, max(720.0, gw * 1.15 + 56.0)))
                req_h = int(min(580.0, max(340.0, gh * 1.15 + 48.0)))
            self.set_size_request(req_w, req_h)
        else:
            if vm.view_kind == "module":
                req_h = 136 if gh > 110.0 else 104
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
        pad = 16.0 if self.zoom_card else 8.0
        zx = max(20.0, w - 2.0 * pad) / max(graph_w, 1.0)
        zy = max(20.0, h - 2.0 * pad) / max(graph_h, 1.0)
        if self.zoom_card:
            max_sc = 1.45 if self.vm.view_kind == "module" else 1.35
        else:
            max_sc = 0.84
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
        self._snapshot_module_kcontrol_groups(
            snapshot,
            min_x - 80.0,
            min_y - 80.0,
            min_x + graph_w + 80.0,
            min_y + graph_h + 80.0,
        )
        for edge in self.vm.edges:
            self._snapshot_edge(snapshot, edge)
        for nid, node in self.vm.nodes.items():
            self._snapshot_node(snapshot, node, is_selected=self.is_row_selected)

        snapshot.restore()
        snapshot.pop()

        bcol = NODE_BORDER_SELECTED if self.is_row_selected else rgba(0.24, 0.28, 0.36, 0.85)
        bw = 1.8 if self.is_row_selected else 1.0
        snapshot.append_border(rrect, [bw, bw, bw, bw], [bcol, bcol, bcol, bcol])
