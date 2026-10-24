# SPDX-License-Identifier: BSD-3-Clause
"""Hardware-accelerated GTK4 Graph Canvas using Gtk.Snapshot and Gsk.PathBuilder."""

from __future__ import annotations
import math
from typing import Callable, Dict, List, Optional, Tuple

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GObject, Graphene, Gsk, Gtk, Pango

from .layout import (
    CTRL_H_GAP,
    CTRL_TOP_GAP,
    _compute_node_geometry,
    layout_graph,
    update_container_bounds_from_nodes,
)
from .model import ConfigNode, GraphEdge, GraphNode, GraphPort, GraphViewModel, PipelineContainer
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


class TopologyGraphCanvas(Gtk.Widget):
    """Custom GTK4 Widget rendering interactive Topology2 graphs via Gtk.Snapshot."""

    __gtype_name__ = "TopologyGraphCanvas"

    def __init__(
        self,
        on_selection_changed: Optional[Callable[[Optional[str], Optional[ConfigNode], str], None]] = None,
        on_graph_modified: Optional[Callable[[], None]] = None,
    ):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_focusable(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)

        self.on_selection_changed = on_selection_changed
        self.on_graph_modified = on_graph_modified

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
        self.selected_edge_id: Optional[str] = None
        self.selected_container_id: Optional[int] = None
        self.hovered_port: Optional[Tuple[str, str, str]] = None  # (node_id, port_id, direction)

        # Interaction state
        self._drag_mode: str = "none"  # "none", "pan", "node", "wire"
        self._drag_start_world: Tuple[float, float] = (0.0, 0.0)
        self._drag_start_pan: Tuple[float, float] = (0.0, 0.0)
        self._drag_node_initial: Tuple[float, float] = (0.0, 0.0)
        self._drag_children_initial: Dict[str, Tuple[float, float]] = {}
        self._wire_source: Optional[Tuple[str, str, float, float]] = None  # (node_id, port_id, wx, wy)
        self._wire_cursor_world: Tuple[float, float] = (0.0, 0.0)

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

        # Middle or Right button pan gesture
        for btn in (2, 3):
            pan_drag = Gtk.GestureDrag.new()
            pan_drag.set_button(btn)
            pan_drag.connect("drag-begin", self._on_pan_begin)
            pan_drag.connect("drag-update", self._on_pan_update)
            self.add_controller(pan_drag)

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
        drop_target.connect("drop", self._on_drop_item)
        self.add_controller(drop_target)

    def set_graph(self, vm: GraphViewModel, workspace: Optional[Topology2Workspace] = None) -> None:
        """Load a new GraphViewModel, auto-layout its nodes, and trigger snapshot redraw."""
        self.workspace = workspace
        self.vm = vm
        self.selected_node_id = None
        self.selected_edge_id = None
        self.selected_container_id = None
        self._layout_cache.clear()
        layout_graph(vm)
        self.fit_to_view()
        # Select first node or root config so the Right-Hand Attribute Tree is populated immediately
        if vm.nodes:
            first_id = next(iter(vm.nodes.keys()))
            self.select_node(first_id)
        elif self.on_selection_changed:
            self.on_selection_changed(vm.title, vm.root_config, vm.subtitle)
        self.queue_draw()

    def select_node(self, node_id: Optional[str]) -> None:
        self.selected_node_id = node_id
        self.selected_edge_id = None
        self.selected_container_id = None
        if self.vm and node_id and node_id in self.vm.nodes:
            node = self.vm.nodes[node_id]
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
            self._snapshot_node(snapshot, node, is_selected=(nid == self.selected_node_id))

        snapshot.restore()

        # 8. Draw HUD overlay (title, zoom badge, interaction hint)
        self._snapshot_hud(snapshot, w, h)

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

    def _snapshot_container(self, snapshot: Gtk.Snapshot, pid: int, cont: PipelineContainer) -> None:
        rect = Graphene.Rect.alloc()
        rect.init(cont.x, cont.y, cont.width, cont.height)
        rrect = Gsk.RoundedRect()
        rrect.init_from_rect(rect, 12.0)

        is_cap = "capture" in cont.direction.lower()
        fill_col = CONTAINER_FILL_CAP if is_cap else CONTAINER_FILL_PB
        border_col = (
            NODE_BORDER_SELECTED
            if self.selected_container_id == pid
            else (CONTAINER_BORDER_CAP if is_cap else CONTAINER_BORDER_PB)
        )

        snapshot.push_rounded_clip(rrect)
        snapshot.append_color(fill_col, rect)
        snapshot.pop()

        bw = 2.2 if self.selected_container_id == pid else 1.4
        snapshot.append_border(rrect, [bw, bw, bw, bw], [border_col, border_col, border_col, border_col])

        # Container header text
        header_txt = f"Pipeline {pid}: {cont.class_name} ({cont.direction})"
        layout = self._get_pango_layout(header_txt, size_pt=9, bold=True)
        snapshot.save()
        pt = Graphene.Point.alloc()
        pt.init(cont.x + 14.0, cont.y + 10.0)
        snapshot.translate(pt)
        snapshot.append_layout(layout, TEXT_WHITE)
        snapshot.restore()

    def _snapshot_node(self, snapshot: Gtk.Snapshot, node: GraphNode, is_selected: bool) -> None:
        rect = Graphene.Rect.alloc()
        rect.init(node.x, node.y, node.width, node.height)
        rrect = Gsk.RoundedRect()
        rrect.init_from_rect(rect, 6.0 if node.class_category == "Control" else 9.0)

        header_col, body_col = CATEGORY_COLORS.get(node.category, CATEGORY_COLORS["widget"])

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

        u_port = u.out_ports[0] if u.out_ports else GraphPort("out0", "out", "out", rel_x=u.width, rel_y=u.height / 2.0)
        v_port = v.in_ports[0] if v.in_ports else GraphPort("in0", "in", "in", rel_x=0.0, rel_y=v.height / 2.0)

        x1 = u.x + u_port.rel_x
        y1 = u.y + u_port.rel_y
        x2 = v.x + v_port.rel_x
        y2 = v.y + v_port.rel_y

        color = EDGE_SELECTED_COLOR if is_sel else (EDGE_INTER_COLOR if edge.is_inter_pipeline else EDGE_INTRA_COLOR)
        width = 3.2 if is_sel else (2.4 if edge.is_inter_pipeline else 2.0)
        self._snapshot_bezier_wire(snapshot, x1, y1, x2, y2, color, width=width, dashed=edge.is_inter_pipeline)

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
        pb = Gsk.PathBuilder.new()
        pb.move_to(x1, y1)
        pb.cubic_to(x1 + dx, y1, x2 - dx, y2, x2, y2)
        path = pb.to_path()

        stroke = Gsk.Stroke.new(width)
        if dashed:
            stroke.set_dash([7.0, 4.0])
        snapshot.append_stroke(path, stroke, color)

        # Draw arrowhead at (x2, y2)
        arrow_pb = Gsk.PathBuilder.new()
        arrow_pb.move_to(x2, y2)
        arrow_pb.line_to(x2 - 9.0, y2 - 4.5)
        arrow_pb.line_to(x2 - 9.0, y2 + 4.5)
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
            x1 = u.x + (u.out_ports[0].rel_x if u.out_ports else u.width)
            y1 = u.y + (u.out_ports[0].rel_y if u.out_ports else u.height / 2.0)
            x2 = v.x + (v.in_ports[0].rel_x if v.in_ports else 0.0)
            y2 = v.y + (v.in_ports[0].rel_y if v.in_ports else v.height / 2.0)
            dx = max(45.0, abs(x2 - x1) * 0.48)
            # Sample 16 points along cubic Bezier
            for step in range(17):
                t = step / 16.0
                mt = 1.0 - t
                bx = (mt**3) * x1 + 3 * (mt**2) * t * (x1 + dx) + 3 * mt * (t**2) * (x2 - dx) + (t**3) * x2
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

        # Check port first (don't change selection if starting a wire drag)
        if self._hit_test_port(wx, wy) is not None:
            return

        nid = self._hit_test_node(wx, wy)
        if nid:
            self.select_node(nid)
            return

        eid = self._hit_test_edge(wx, wy)
        if eid:
            self.selected_node_id = None
            self.selected_edge_id = eid
            self.selected_container_id = None
            edge = next((e for e in self.vm.edges if e.id == eid), None)
            if edge and self.on_selection_changed:
                self.on_selection_changed(
                    f"Route: {edge.source_node} → {edge.sink_node}",
                    edge.config,
                    "Object.Base.route",
                )
            self.queue_draw()
            return

        pid = self._hit_test_container(wx, wy)
        if pid is not None:
            self.selected_node_id = None
            self.selected_edge_id = None
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

        # Clicked empty background -> show topology/root attributes
        self.selected_node_id = None
        self.selected_edge_id = None
        self.selected_container_id = None
        if self.on_selection_changed:
            self.on_selection_changed(self.vm.title, self.vm.root_config, self.vm.subtitle)
        self.queue_draw()

    def _on_drag_begin(self, gesture: Gtk.GestureDrag, sx: float, sy: float) -> None:
        if not self.vm:
            return
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
            self.select_node(nid)
            node = self.vm.nodes[nid]
            self._drag_node_initial = (node.x, node.y)
            self._drag_children_initial = {
                cid: (cnode.x, cnode.y)
                for cid, cnode in self.vm.nodes.items()
                if cnode.parent_node_id == nid
            }
            return

        self._drag_mode = "pan"
        self._drag_start_pan = (self.pan_x, self.pan_y)

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
            self.queue_draw()
        elif self._drag_mode == "wire":
            wx = self._drag_start_world[0] + offset_x / self.zoom
            wy = self._drag_start_world[1] + offset_y / self.zoom
            self._wire_cursor_world = (wx, wy)
            self.queue_draw()

    def _on_drag_end(self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        if self.vm and self._drag_mode == "wire" and self._wire_source is not None:
            src_nid, src_pid, _, _ = self._wire_source
            wx = self._drag_start_world[0] + offset_x / self.zoom
            wy = self._drag_start_world[1] + offset_y / self.zoom
            port_hit = self._hit_test_port(wx, wy, radius=16.0)
            target_nid = port_hit[0] if (port_hit and port_hit[2] == "in") else self._hit_test_node(wx, wy)
            if target_nid and target_nid != src_nid:
                self._create_route_edge(src_nid, target_nid)
        self._drag_mode = "none"
        self._wire_source = None
        self.queue_draw()

    def _on_pan_begin(self, gesture: Gtk.GestureDrag, sx: float, sy: float) -> None:
        self._drag_start_pan = (self.pan_x, self.pan_y)

    def _on_pan_update(self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        if abs(offset_x) > 2.0 or abs(offset_y) > 2.0:
            self._is_fitted = False
        self.pan_x = self._drag_start_pan[0] + offset_x
        self.pan_y = self._drag_start_pan[1] + offset_y
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
        if new_hov != self.hovered_port:
            self.hovered_port = new_hov
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
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
            self.delete_selected()
            return True
        if keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.fit_to_view()
            return True
        if keyval in (Gdk.KEY_l, Gdk.KEY_L) and self.vm:
            layout_graph(self.vm)
            self.fit_to_view()
            return True
        return False

    # -------------------------------------------------------------------------
    # Graph Mutation Operations (Add Node, Wire Route, Delete Selected)
    # -------------------------------------------------------------------------

    def _create_route_edge(self, src_nid: str, snk_nid: str) -> None:
        if not self.vm:
            return
        if any(e.source_node == src_nid and e.sink_node == snk_nid for e in self.vm.edges):
            return
        u = self.vm.nodes[src_nid]
        v = self.vm.nodes[snk_nid]
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
            self.vm.edges = [e for e in self.vm.edges if e.id != self.selected_edge_id]
            self.selected_edge_id = None
            self.vm.dirty = True
            if self.on_graph_modified:
                self.on_graph_modified()
            self.queue_draw()
        elif self.selected_node_id and self.selected_node_id in self.vm.nodes:
            nid = self.selected_node_id
            to_remove = {nid} | {
                cid for cid, cn in self.vm.nodes.items() if cn.parent_node_id == nid
            }
            for rid in to_remove:
                self.vm.nodes.pop(rid, None)
            self.vm.edges = [
                e for e in self.vm.edges if e.source_node not in to_remove and e.sink_node not in to_remove
            ]
            self.selected_node_id = None
            self.vm.dirty = True
            update_container_bounds_from_nodes(self.vm)
            if self.on_graph_modified:
                self.on_graph_modified()
            self.queue_draw()

    def add_module_instance(self, module_name: str, wx: Optional[float] = None, wy: Optional[float] = None) -> Optional[str]:
        """Instantiate a new Processing Module (Class.Widget.<module_name>) onto the active graph."""
        if not self.vm or not self.workspace or module_name not in self.workspace.processing_modules:
            return None
        cdef = self.workspace.processing_modules[module_name]
        # Pick default pipeline_id (first container if available, else 1)
        pid = min(self.vm.containers.keys()) if self.vm.containers else 1
        # Find unused instance number
        inst_num = 1
        while f"{module_name}.{pid}.{inst_num}" in self.vm.nodes:
            inst_num += 1

        node = self.workspace._instantiate_widget_node(
            wclass=module_name,
            winst=str(inst_num),
            pipeline_id=pid,
            pipeline_class=self.vm.containers[pid].class_name if pid in self.vm.containers else "custom",
            pipeline_direction="playback",
            override_stmts=[],
            defines=self.vm.defines or self.workspace.global_defines,
            source_file=cdef.source_file,
        )
        _compute_node_geometry(node)
        if wx is None or wy is None:
            wx, wy = self.screen_to_world(float(self.get_width() or 600) / 2.0, float(self.get_height() or 400) / 2.0)
        node.x = wx - node.width / 2.0
        node.y = wy - node.height / 2.0
        self.vm.nodes[node.id] = node
        self.workspace.attach_widget_kcontrol_nodes(self.vm, node)
        ctrls = [cnode for cnode in self.vm.nodes.values() if cnode.parent_node_id == node.id]
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
        self.select_node(node.id)
        if self.on_graph_modified:
            self.on_graph_modified()
        self.queue_draw()
        return node.id

    def _on_drop_item(self, target: Gtk.DropTarget, value: str, x: float, y: float) -> bool:
        if not value:
            return False
        wx, wy = self.screen_to_world(x, y)
        if value.startswith("module:"):
            mod_name = value.split(":", 1)[1]
            self.add_module_instance(mod_name, wx, wy)
            return True
        return False
