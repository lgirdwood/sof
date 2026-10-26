# SPDX-License-Identifier: BSD-3-Clause
"""Left-hand navigation panel with two top-level Notebook pages:
1) Primary Page: "Object View" split into two sub-pages ("Modules" and "Pipelines")
   rendering miniature visual graph cards of every module and pipeline.
2) Second Page: "File View" containing the 4 vertical tree-view sub-panes
   (Base Classes, Processing Modules, Pipelines, Machine Topologies).
"""

from __future__ import annotations
import os
from typing import Callable, Dict, Optional, Tuple

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GObject, Gtk, Pango

from .graph_canvas import MiniGraphPreview, TopologyGraphCanvas
from .model import CMakeTargetVariant, GraphViewModel, MachineTopologyEntry, TplgClassDef
from .parser import Topology2Workspace


class LeftNavigationPanel(Gtk.Box):
    """Left panel with primary 'Object View' (Modules / Pipelines visual lists) and 'File View'."""

    def __init__(
        self,
        on_item_selected: Callable[[str, str, Optional[str]], None],
        on_add_module_requested: Optional[Callable[[str], None]] = None,
        on_add_pipeline_requested: Optional[Callable[[str], None]] = None,
        on_view_source_requested: Optional[Callable[[str, str, Optional[str], Optional[str]], None]] = None,
    ):
        """
        Args:
            on_item_selected: callback(pane_kind, primary_id, secondary_id)
                pane_kind in ("base_class", "module", "pipeline", "topology")
            on_add_module_requested: callback(module_name) when user clicks '+ Add Node'
            on_add_pipeline_requested: callback(pipeline_name) when user clicks '+ Add Pipeline'
            on_view_source_requested: callback(pane_kind, primary_id, secondary_id, search_hint)
        """
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.set_size_request(320, -1)
        self.set_margin_start(6)
        self.set_margin_end(4)
        self.set_margin_top(6)
        self.set_margin_bottom(6)

        self.on_item_selected = on_item_selected
        self.on_add_module_requested = on_add_module_requested
        self.on_add_pipeline_requested = on_add_pipeline_requested
        self.on_view_source_requested = on_view_source_requested
        self._active_context_popover: Optional[Gtk.Popover] = None
        self.workspace: Optional[Topology2Workspace] = None
        self._updating_selection: bool = False
        self._filter_text: str = ""
        self._mini_vm_cache: Dict[str, GraphViewModel] = {}
        self._hovered_row: Optional[Gtk.ListBoxRow] = None
        self._is_dragging: bool = False

        # Reusable non-modal Hover Zoom Info Box Popover for Object View items
        self._hover_popover = Gtk.Popover()
        self._hover_popover.set_autohide(False)
        self._hover_popover.set_can_target(False)
        self._hover_popover.set_position(Gtk.PositionType.RIGHT)
        self._hover_popover.add_css_class("zoom-info-popover")

        # Search entry filtering both Object View and File View
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Filter modules, pipelines, classes, topologies…")
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.append(self.search_entry)

        # Top-level Notebook: Page 0 = "Object View" (primary), Page 1 = "File View"
        self.main_notebook = Gtk.Notebook()
        self.main_notebook.set_vexpand(True)
        self.main_notebook.set_hexpand(True)

        # ---------------------------------------------------------------------
        # Page 1 (Primary): Object View -> sub-notebook with "Modules" & "Pipelines"
        # ---------------------------------------------------------------------
        self.object_notebook = Gtk.Notebook()
        self.object_notebook.set_vexpand(True)
        self.object_notebook.set_hexpand(True)

        # Sub-page 1: Modules visual list view (drag-and-drop palette)
        mod_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        mod_page.set_margin_top(4)
        mod_page.set_margin_bottom(4)
        mod_page.set_margin_start(4)
        mod_page.set_margin_end(4)

        mod_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.obj_mod_count_lbl = Gtk.Label(label="Processing Modules", xalign=0.0)
        self.obj_mod_count_lbl.set_hexpand(True)
        self.obj_mod_count_lbl.add_css_class("heading")
        mod_hdr.append(self.obj_mod_count_lbl)

        obj_add_btn = Gtk.Button(label="+ Add Node")
        obj_add_btn.set_tooltip_text("Instantiate selected Processing Module onto the active Graph")
        obj_add_btn.connect("clicked", self._on_add_module_btn_clicked)
        mod_hdr.append(obj_add_btn)
        mod_page.append(mod_hdr)

        self.obj_mod_list = Gtk.ListBox()
        self.obj_mod_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.obj_mod_list.set_activate_on_single_click(False)
        self.obj_mod_list.connect("row-selected", self._on_obj_module_row_selected)
        self.obj_mod_list.connect("row-activated", self._on_obj_row_activated)
        mod_click = Gtk.GestureClick.new()
        mod_click.set_button(1)
        mod_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        mod_click.connect("pressed", self._on_obj_list_pressed, self.obj_mod_list)
        self.obj_mod_list.add_controller(mod_click)
        mod_rclick = Gtk.GestureClick.new()
        mod_rclick.set_button(3)
        mod_rclick.connect("pressed", self._on_obj_list_right_pressed, self.obj_mod_list)
        self.obj_mod_list.add_controller(mod_rclick)
        mod_motion = Gtk.EventControllerMotion.new()
        mod_motion.connect("motion", self._on_obj_list_motion, self.obj_mod_list)
        mod_motion.connect("leave", lambda _c: self._hide_hover_zoom_box())
        self.obj_mod_list.add_controller(mod_motion)
        mod_scroll = Gtk.ScrolledWindow()
        mod_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        mod_scroll.set_vexpand(True)
        mod_scroll.set_child(self.obj_mod_list)
        mod_page.append(mod_scroll)

        self.object_notebook.append_page(mod_page, Gtk.Label(label="Modules"))

        # Sub-page 2: Pipelines visual list view (drag-and-drop palette)
        pipe_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        pipe_page.set_margin_top(4)
        pipe_page.set_margin_bottom(4)
        pipe_page.set_margin_start(4)
        pipe_page.set_margin_end(4)

        pipe_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.obj_pipe_count_lbl = Gtk.Label(label="Pipeline Templates", xalign=0.0)
        self.obj_pipe_count_lbl.set_hexpand(True)
        self.obj_pipe_count_lbl.add_css_class("heading")
        pipe_hdr.append(self.obj_pipe_count_lbl)

        obj_add_pipe_btn = Gtk.Button(label="+ Add Pipeline")
        obj_add_pipe_btn.set_tooltip_text("Instantiate selected Pipeline onto the active Graph")
        obj_add_pipe_btn.connect("clicked", self._on_add_pipeline_btn_clicked)
        pipe_hdr.append(obj_add_pipe_btn)
        pipe_page.append(pipe_hdr)

        self.obj_pipe_list = Gtk.ListBox()
        self.obj_pipe_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.obj_pipe_list.set_activate_on_single_click(False)
        self.obj_pipe_list.connect("row-selected", self._on_obj_pipeline_row_selected)
        self.obj_pipe_list.connect("row-activated", self._on_obj_row_activated)
        pipe_click = Gtk.GestureClick.new()
        pipe_click.set_button(1)
        pipe_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        pipe_click.connect("pressed", self._on_obj_list_pressed, self.obj_pipe_list)
        self.obj_pipe_list.add_controller(pipe_click)
        pipe_rclick = Gtk.GestureClick.new()
        pipe_rclick.set_button(3)
        pipe_rclick.connect("pressed", self._on_obj_list_right_pressed, self.obj_pipe_list)
        self.obj_pipe_list.add_controller(pipe_rclick)
        pipe_motion = Gtk.EventControllerMotion.new()
        pipe_motion.connect("motion", self._on_obj_list_motion, self.obj_pipe_list)
        pipe_motion.connect("leave", lambda _c: self._hide_hover_zoom_box())
        self.obj_pipe_list.add_controller(pipe_motion)
        pipe_scroll = Gtk.ScrolledWindow()
        pipe_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        pipe_scroll.set_vexpand(True)
        pipe_scroll.set_child(self.obj_pipe_list)
        pipe_page.append(pipe_scroll)

        self.object_notebook.append_page(pipe_page, Gtk.Label(label="Pipelines"))
        self.object_notebook.connect("switch-page", lambda *_: self._hide_hover_zoom_box())

        self.main_notebook.append_page(self.object_notebook, Gtk.Label(label="Object View"))
        self.main_notebook.connect("switch-page", lambda *_: self._hide_hover_zoom_box())

        # ---------------------------------------------------------------------
        # Page 2: File View (4 vertical tree-view sub-panes)
        # ---------------------------------------------------------------------
        self.base_store, self.base_tree, pane1 = self._create_tree_subpane(
            "1. Base Classes",
            ("Class / Category", "Details"),
            "base_class",
        )
        self.mod_store, self.mod_tree, pane2 = self._create_tree_subpane(
            "2. Processing Modules",
            ("Widget Module", "Type / Pins"),
            "module",
            enable_drag_add=True,
        )
        self.pipe_store, self.pipe_tree, pane3 = self._create_tree_subpane(
            "3. Pipelines",
            ("Pipeline Class", "Widgets / Dir"),
            "pipeline",
        )
        self.top_store, self.top_tree, pane4 = self._create_tree_subpane(
            "4. Machine Topologies",
            ("Topology / Variant", "Defines / Target"),
            "topology",
        )

        # Pack the 4 vertical sub-panes into nested vertical Gtk.Paned so each is resizable
        self.vpaned_top = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        self.vpaned_top.set_wide_handle(True)
        self.vpaned_top.set_resize_start_child(False)
        self.vpaned_top.set_resize_end_child(True)
        self.vpaned_top.set_start_child(pane1)
        self.vpaned_top.set_end_child(pane2)
        self.vpaned_top.set_position(165)

        self.vpaned_bot = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        self.vpaned_bot.set_wide_handle(True)
        self.vpaned_bot.set_resize_start_child(False)
        self.vpaned_bot.set_resize_end_child(True)
        self.vpaned_bot.set_start_child(pane3)
        self.vpaned_bot.set_end_child(pane4)
        self.vpaned_bot.set_position(185)

        self.vpaned_main = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        self.vpaned_main.set_wide_handle(True)
        self.vpaned_main.set_vexpand(True)
        self.vpaned_main.set_resize_start_child(False)
        self.vpaned_main.set_resize_end_child(True)
        self.vpaned_main.set_start_child(self.vpaned_top)
        self.vpaned_main.set_end_child(self.vpaned_bot)
        self.vpaned_main.set_position(345)

        self.main_notebook.append_page(self.vpaned_main, Gtk.Label(label="File View"))
        self.compile_tab_lbl: Optional[Gtk.Label] = None
        self._compile_page_idx: int = -1
        self.main_notebook.set_current_page(0)
        self.append(self.main_notebook)

    def attach_compile_pane(self, compile_widget: Gtk.Widget) -> int:
        """Attach the Compile output/diagnostics pane as a tab on the Object View / File View notebook."""
        self.compile_tab_lbl = Gtk.Label(label="Compile")
        self._compile_page_idx = self.main_notebook.append_page(compile_widget, self.compile_tab_lbl)
        return self._compile_page_idx

    def show_compile_pane(self) -> None:
        """Switch the Object View / File View notebook to the Compile tab."""
        if self._compile_page_idx >= 0:
            self.main_notebook.set_current_page(self._compile_page_idx)

    def set_compile_tab_title(self, title: str) -> None:
        if self.compile_tab_lbl is not None:
            self.compile_tab_lbl.set_text(title)

    def _create_tree_subpane(
        self,
        title: str,
        col_titles: Tuple[str, str],
        pane_kind: str,
        enable_drag_add: bool = False,
    ) -> Tuple[Gtk.TreeStore, Gtk.TreeView, Gtk.Frame]:
        # Columns: 0=Display Name, 1=Info/Summary, 2=PaneKind, 3=PrimaryKey, 4=SecondaryKey
        store = Gtk.TreeStore.new([str, str, str, str, str])
        tree = Gtk.TreeView.new_with_model(store)
        tree.set_headers_visible(True)
        tree.set_enable_tree_lines(True)

        # Column 0: Name (auto-sized initially, or fixed when resized/restored)
        r0 = Gtk.CellRendererText()
        r0.set_padding(4, 2)
        c0 = Gtk.TreeViewColumn(col_titles[0], r0, text=0)
        c0.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        c0.set_resizable(True)
        tree.append_column(c0)

        # Column 1: Summary (auto-sized initially, or fixed when resized/restored)
        r1 = Gtk.CellRendererText()
        r1.set_padding(4, 2)
        r1.set_property("foreground", "#8fa3bf")
        c1 = Gtk.TreeViewColumn(col_titles[1], r1, text=1)
        c1.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        c1.set_resizable(True)
        tree.append_column(c1)

        tree.connect("row-expanded", lambda tv, _it, _path: self._maybe_autosize_columns(tv))

        sel = tree.get_selection()
        sel.set_mode(Gtk.SelectionMode.SINGLE)
        sel.connect("changed", self._on_tree_selection_changed, pane_kind)

        tree_rclick = Gtk.GestureClick.new()
        tree_rclick.set_button(3)
        tree_rclick.connect("pressed", self._on_tree_right_pressed, tree, store)
        tree.add_controller(tree_rclick)

        if enable_drag_add:
            drag_src = Gtk.DragSource.new()
            drag_src.set_actions(Gdk.DragAction.COPY)
            drag_src.connect("prepare", self._on_module_drag_prepare, tree)
            tree.add_controller(drag_src)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(tree)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        hdr_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hdr_box.set_margin_start(6)
        hdr_box.set_margin_end(6)
        hdr_box.set_margin_top(3)
        hdr_box.set_margin_bottom(2)

        lbl = Gtk.Label(label=title)
        lbl.set_xalign(0.0)
        lbl.set_hexpand(True)
        lbl.add_css_class("heading")
        hdr_box.append(lbl)

        if enable_drag_add:
            add_btn = Gtk.Button.new_With_label("+ Add Node") if hasattr(Gtk.Button, "new_With_label") else Gtk.Button(label="+ Add Node")
            add_btn.set_tooltip_text("Instantiate selected Processing Module onto the active Graph")
            add_btn.connect("clicked", self._on_add_module_btn_clicked)
            hdr_box.append(add_btn)

        vbox.append(hdr_box)
        vbox.append(scroll)

        frame = Gtk.Frame()
        frame.set_child(vbox)
        return (store, tree, frame)

    def populate(self, workspace: Topology2Workspace) -> None:
        self.workspace = workspace
        self._mini_vm_cache.clear()
        self._rebuild_stores()

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._filter_text = entry.get_text().strip().lower()
        self._rebuild_stores()

    def _matches_filter(self, *texts: str) -> bool:
        if not self._filter_text:
            return True
        combined = " ".join(t.lower() for t in texts if t)
        return self._filter_text in combined

    @staticmethod
    def _clear_listbox(listbox: Gtk.ListBox) -> None:
        while True:
            row = listbox.get_row_at_index(0)
            if row is None:
                break
            listbox.remove(row)

    def _get_mini_module_vm(self, mname: str) -> GraphViewModel:
        key = f"mod:{mname}"
        if key not in self._mini_vm_cache and self.workspace:
            self._mini_vm_cache[key] = self.workspace.build_graph_for_module(mname, include_testbench_io=False)
        return self._mini_vm_cache[key]

    def _get_mini_pipeline_vm(self, pname: str) -> GraphViewModel:
        key = f"pipe:{pname}"
        if key not in self._mini_vm_cache and self.workspace:
            self._mini_vm_cache[key] = self.workspace.build_graph_for_pipeline(pname, include_testbench_io=False)
        return self._mini_vm_cache[key]

    def _rebuild_stores(self) -> None:
        if not self.workspace:
            return
        self._updating_selection = True
        try:
            self._populate_object_modules()
            self._populate_object_pipelines()
            self._populate_base_classes()
            self._populate_processing_modules()
            self._populate_pipelines()
            self._populate_machine_topologies()
            for tv in (self.base_tree, self.mod_tree, self.pipe_tree, self.top_tree):
                self._maybe_autosize_columns(tv)
        finally:
            self._updating_selection = False

    @staticmethod
    def _maybe_autosize_columns(tv: Gtk.TreeView) -> None:
        if any(c.get_sizing() == Gtk.TreeViewColumnSizing.FIXED for c in tv.get_columns()):
            return
        tv.columns_autosize()

    def _populate_object_modules(self) -> None:
        self._hide_hover_zoom_box()
        self._clear_listbox(self.obj_mod_list)
        if not self.workspace:
            return
        count = 0
        for mname, cdef in sorted(self.workspace.processing_modules.items()):
            wtype = str(cdef.defaults.get("type", "effect"))
            desc = cdef.description or f"SOF {mname} processing module ({wtype})."
            if not self._matches_filter(mname, wtype, desc, cdef.rel_path):
                continue
            vm = self._get_mini_module_vm(mname)
            preview = MiniGraphPreview(vm)

            in_p = cdef.defaults.get("num_input_pins", 1)
            out_p = cdef.defaults.get("num_output_pins", 1)
            badge_text = f"{wtype} • {in_p}→{out_p} pins"

            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row_box.set_margin_top(4)
            row_box.set_margin_bottom(4)
            row_box.set_margin_start(4)
            row_box.set_margin_end(4)

            hdr_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            title_lbl = Gtk.Label(label=mname, xalign=0.0)
            title_lbl.set_hexpand(True)
            title_lbl.add_css_class("obj-item-title")
            hdr_row.append(title_lbl)

            badge_lbl = Gtk.Label(label=badge_text, xalign=1.0)
            badge_lbl.add_css_class("obj-item-badge")
            hdr_row.append(badge_lbl)
            row_box.append(hdr_row)

            desc_lbl = Gtk.Label(label=desc, xalign=0.0)
            desc_lbl.set_wrap(True)
            desc_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            desc_lbl.add_css_class("obj-item-desc")
            row_box.append(desc_lbl)

            row_box.append(preview)

            row = Gtk.ListBoxRow()
            row.set_child(row_box)
            row._item_kind = "module"  # type: ignore[attr-defined]
            row._item_id = mname  # type: ignore[attr-defined]
            row._title_text = mname  # type: ignore[attr-defined]
            row._badge_text = badge_text  # type: ignore[attr-defined]
            row._desc_text = desc  # type: ignore[attr-defined]
            row._preview = preview  # type: ignore[attr-defined]

            drag_src = Gtk.DragSource.new()
            drag_src.set_actions(Gdk.DragAction.COPY)
            drag_src.connect(
                "prepare",
                lambda _src, _x, _y, mn=mname: self._prepare_drag_item(f"module:{mn}"),
            )
            drag_src.connect("drag-begin", lambda *_a: self._on_drag_started())
            drag_src.connect("drag-end", lambda *_a: self._on_drag_stopped())
            drag_src.connect("drag-cancel", lambda *_a: (self._on_drag_stopped(), False)[1])
            row.add_controller(drag_src)

            self.obj_mod_list.append(row)
            count += 1
        self.obj_mod_count_lbl.set_text(f"Modules ({count})")

    def _populate_object_pipelines(self) -> None:
        self._hide_hover_zoom_box()
        self._clear_listbox(self.obj_pipe_list)
        if not self.workspace:
            return
        count = 0
        for pname, pdef in sorted(self.workspace.pipeline_classes.items()):
            default_dir = "capture" if "capture" in pname else "playback"
            direction = str(pdef.defaults.get("direction", default_dir))
            desc = pdef.description or f"{direction.capitalize()} pipeline: {pname}."
            if not self._matches_filter(pname, direction, desc, pdef.rel_path):
                continue
            vm = self._get_mini_pipeline_vm(pname)
            preview = MiniGraphPreview(vm)

            wcount = len([w for w in pdef.widgets if w["class_name"] != "pipeline"])
            badge_text = f"{direction} • {wcount} widgets"

            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row_box.set_margin_top(4)
            row_box.set_margin_bottom(4)
            row_box.set_margin_start(4)
            row_box.set_margin_end(4)

            hdr_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            title_lbl = Gtk.Label(label=pname, xalign=0.0)
            title_lbl.set_hexpand(True)
            title_lbl.add_css_class("obj-item-title")
            hdr_row.append(title_lbl)

            badge_lbl = Gtk.Label(label=badge_text, xalign=1.0)
            badge_lbl.add_css_class("obj-item-badge")
            hdr_row.append(badge_lbl)
            row_box.append(hdr_row)

            desc_lbl = Gtk.Label(label=desc, xalign=0.0)
            desc_lbl.set_wrap(True)
            desc_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            desc_lbl.add_css_class("obj-item-desc")
            row_box.append(desc_lbl)

            row_box.append(preview)

            row = Gtk.ListBoxRow()
            row.set_child(row_box)
            row._item_kind = "pipeline"  # type: ignore[attr-defined]
            row._item_id = pname  # type: ignore[attr-defined]
            row._title_text = pname  # type: ignore[attr-defined]
            row._badge_text = badge_text  # type: ignore[attr-defined]
            row._desc_text = desc  # type: ignore[attr-defined]
            row._preview = preview  # type: ignore[attr-defined]

            drag_src = Gtk.DragSource.new()
            drag_src.set_actions(Gdk.DragAction.COPY)
            drag_src.connect(
                "prepare",
                lambda _src, _x, _y, pn=pname: self._prepare_drag_item(f"pipeline:{pn}"),
            )
            drag_src.connect("drag-begin", lambda *_a: self._on_drag_started())
            drag_src.connect("drag-end", lambda *_a: self._on_drag_stopped())
            drag_src.connect("drag-cancel", lambda *_a: (self._on_drag_stopped(), False)[1])
            row.add_controller(drag_src)

            self.obj_pipe_list.append(row)
            count += 1
        self.obj_pipe_count_lbl.set_text(f"Pipelines ({count})")

    def _populate_base_classes(self) -> None:
        self.base_store.clear()
        if not self.workspace:
            return
        # Group by category (Base, Dai, PCM, Control, VendorToken)
        by_cat = {}
        for full_key, cdef in sorted(self.workspace.base_classes.items()):
            if not self._matches_filter(full_key, cdef.name, cdef.rel_path):
                continue
            by_cat.setdefault(cdef.category, []).append((full_key, cdef))

        for cat, items in sorted(by_cat.items()):
            cat_iter = self.base_store.append(None, [f"Class.{cat}", f"{len(items)} classes", "", "", ""])
            for full_key, cdef in items:
                info = f"{len(cdef.attributes)} attrs ({cdef.rel_path})"
                self.base_store.append(cat_iter, [cdef.name, info, "base_class", full_key, ""])
        self.base_tree.expand_all()

    def _populate_processing_modules(self) -> None:
        self.mod_store.clear()
        if not self.workspace:
            return
        for mname, cdef in sorted(self.workspace.processing_modules.items()):
            wtype = str(cdef.defaults.get("type", "effect"))
            if not self._matches_filter(mname, wtype, cdef.rel_path):
                continue
            in_p = cdef.defaults.get("num_input_pins", 1)
            out_p = cdef.defaults.get("num_output_pins", 1)
            info = f"{wtype} ({in_p}→{out_p})"
            self.mod_store.append(None, [mname, info, "module", mname, ""])

    def _populate_pipelines(self) -> None:
        self.pipe_store.clear()
        if not self.workspace:
            return
        pb_iter = self.pipe_store.append(None, ["Playback Pipelines", "", "", "", ""])
        cap_iter = self.pipe_store.append(None, ["Capture Pipelines", "", "", "", ""])
        other_iter = None

        pb_count = 0
        cap_count = 0
        for pname, pdef in sorted(self.workspace.pipeline_classes.items()):
            direction = str(pdef.defaults.get("direction", "playback"))
            if not self._matches_filter(pname, direction, pdef.rel_path):
                continue
            wcount = len([w for w in pdef.widgets if w["class_name"] != "pipeline"])
            info = f"{wcount} widgets • {direction}"
            if "capture" in direction or pname.endswith("-capture"):
                self.pipe_store.append(cap_iter, [pname, info, "pipeline", pname, ""])
                cap_count += 1
            elif "playback" in direction or pname.endswith("-playback"):
                self.pipe_store.append(pb_iter, [pname, info, "pipeline", pname, ""])
                pb_count += 1
            else:
                if other_iter is None:
                    other_iter = self.pipe_store.append(None, ["Other Pipelines", "", "", "", ""])
                self.pipe_store.append(other_iter, [pname, info, "pipeline", pname, ""])

        self.pipe_store.set_value(pb_iter, 1, f"{pb_count} classes")
        self.pipe_store.set_value(cap_iter, 1, f"{cap_count} classes")
        self.pipe_tree.expand_all()

    def _populate_machine_topologies(self) -> None:
        self.top_store.clear()
        if not self.workspace:
            return
        for entry in self.workspace.machine_topologies:
            matching_vars = [
                v for v in entry.variants
                if self._matches_filter(entry.name, v.output_name, v.defines_str, v.cmake_file)
            ]
            if not matching_vars and not self._matches_filter(entry.name, entry.rel_path):
                continue

            var_summary = f"{len(entry.variants)} targets" if entry.variants else entry.rel_path
            parent_iter = self.top_store.append(
                None,
                [f"{entry.name}.conf", var_summary, "topology", entry.conf_path, ""],
            )
            for v in (matching_vars if self._filter_text else entry.variants):
                self.top_store.append(
                    parent_iter,
                    [v.output_name, v.defines_str or v.category, "topology", entry.conf_path, v.output_name],
                )
        if self._filter_text:
            self.top_tree.expand_all()

    def _update_listbox_preview_selection(self, listbox: Gtk.ListBox, selected_row: Optional[Gtk.ListBoxRow]) -> None:
        idx = 0
        while True:
            r = listbox.get_row_at_index(idx)
            if r is None:
                break
            prev = getattr(r, "_preview", None)
            if prev is not None:
                is_sel = r == selected_row
                if prev.is_row_selected != is_sel:
                    prev.is_row_selected = is_sel
                    prev.queue_draw()
            idx += 1

    def _on_obj_module_row_selected(self, listbox: Gtk.ListBox, row: Optional[Gtk.ListBoxRow]) -> None:
        """Highlight selected module card in Object View without replacing the active Main View graph."""
        self._update_listbox_preview_selection(listbox, row)
        if self._updating_selection or row is None:
            return
        self._updating_selection = True
        try:
            self.obj_pipe_list.unselect_all()
            self._update_listbox_preview_selection(self.obj_pipe_list, None)
        finally:
            self._updating_selection = False

    def _on_obj_pipeline_row_selected(self, listbox: Gtk.ListBox, row: Optional[Gtk.ListBoxRow]) -> None:
        """Highlight selected pipeline card in Object View without replacing the active Main View graph."""
        self._update_listbox_preview_selection(listbox, row)
        if self._updating_selection or row is None:
            return
        self._updating_selection = True
        try:
            self.obj_mod_list.unselect_all()
            self._update_listbox_preview_selection(self.obj_mod_list, None)
        finally:
            self._updating_selection = False

    def _on_obj_row_activated(self, _listbox: Gtk.ListBox, row: Optional[Gtk.ListBoxRow]) -> None:
        """Open a double-clicked or activated Object View module/pipeline in a Main Graph Notebook tab."""
        if row is None:
            return
        item_kind = getattr(row, "_item_kind", None)
        item_id = getattr(row, "_item_id", None)
        if item_kind and item_id:
            self.on_item_selected(str(item_kind), str(item_id), None)

    def _on_obj_list_pressed(
        self,
        _gesture: Gtk.GestureClick,
        n_press: int,
        _x: float,
        y: float,
        listbox: Gtk.ListBox,
    ) -> None:
        self._hide_hover_zoom_box()
        if n_press == 2:
            row = listbox.get_row_at_y(int(y)) or listbox.get_selected_row()
            if row is not None:
                listbox.select_row(row)
                self._on_obj_row_activated(listbox, row)

    def _on_obj_list_right_pressed(
        self,
        _gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        listbox: Gtk.ListBox,
    ) -> None:
        """Open right-click context menu ('View Source') for an Object View module or pipeline row."""
        self._hide_hover_zoom_box()
        row = listbox.get_row_at_y(int(y)) or listbox.get_selected_row()
        if row is None:
            return
        listbox.select_row(row)
        item_kind = str(getattr(row, "_item_kind", "") or "")
        item_id = str(getattr(row, "_item_id", "") or "")
        title_text = str(getattr(row, "_title_text", item_id) or item_id)
        if item_kind and item_id:
            self._open_item_context_menu(
                parent_widget=listbox,
                x=x,
                y=y,
                pane_kind=item_kind,
                primary_id=item_id,
                secondary_id=None,
                display_title=title_text,
            )

    def _on_tree_right_pressed(
        self,
        _gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        tree: Gtk.TreeView,
        store: Gtk.TreeStore,
    ) -> None:
        """Open right-click context menu ('View Source') for a File View TreeView row."""
        bx, by = tree.convert_widget_to_bin_window_coords(int(x), int(y))
        path_info = tree.get_path_at_pos(bx, by)
        tree_iter: Optional[Gtk.TreeIter] = None
        if path_info is not None and path_info[0] is not None:
            path = path_info[0]
            tree_iter = store.get_iter(path)
            self._updating_selection = True
            try:
                tree.get_selection().select_path(path)
            finally:
                self._updating_selection = False
        else:
            _model, tree_iter = tree.get_selection().get_selected()

        if not tree_iter:
            return
        disp = str(store.get_value(tree_iter, 0) or "")
        kind = str(store.get_value(tree_iter, 2) or "")
        primary = str(store.get_value(tree_iter, 3) or "")
        secondary = str(store.get_value(tree_iter, 4) or "") or None
        if kind and primary:
            self._open_item_context_menu(
                parent_widget=tree,
                x=x,
                y=y,
                pane_kind=kind,
                primary_id=primary,
                secondary_id=secondary,
                display_title=disp or primary,
            )

    def _open_item_context_menu(
        self,
        parent_widget: Gtk.Widget,
        x: float,
        y: float,
        pane_kind: str,
        primary_id: str,
        secondary_id: Optional[str] = None,
        display_title: str = "",
    ) -> Optional[Gtk.Popover]:
        """Open a right-click context menu Popover at (x, y) with 'View Source' and 'Open Graph'."""
        if self._active_context_popover is not None:
            try:
                self._active_context_popover.popdown()
            except Exception:
                pass
            self._active_context_popover = None

        src_fname = f"{primary_id}.conf"
        if self.workspace:
            if pane_kind == "module" and primary_id in self.workspace.processing_modules:
                sf = self.workspace.processing_modules[primary_id].source_file
                if sf:
                    src_fname = os.path.basename(sf)
            elif pane_kind == "pipeline" and primary_id in self.workspace.pipeline_classes:
                sf = self.workspace.pipeline_classes[primary_id].source_file
                if sf:
                    src_fname = os.path.basename(sf)
            elif pane_kind == "base_class" and primary_id in self.workspace.base_classes:
                sf = self.workspace.base_classes[primary_id].source_file
                if sf:
                    src_fname = os.path.basename(sf)
            elif pane_kind == "topology":
                src_fname = os.path.basename(primary_id)

        pop = Gtk.Popover()
        pop.set_parent(parent_widget)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        pop.set_pointing_to(rect)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        hdr = Gtk.Label(label=display_title or primary_id, xalign=0.0)
        hdr.add_css_class("heading")
        box.append(hdr)

        view_src_btn = Gtk.Button(label=f"📄 View Source ({src_fname})")
        view_src_btn.add_css_class("suggested-action")

        def _on_view_src(_b: Gtk.Button) -> None:
            pop.popdown()
            if self.on_view_source_requested:
                self.on_view_source_requested(pane_kind, primary_id, secondary_id, None)

        view_src_btn.connect("clicked", _on_view_src)
        box.append(view_src_btn)

        open_graph_btn = Gtk.Button(label="🕸 Open Graph")

        def _on_open_graph(_b: Gtk.Button) -> None:
            pop.popdown()
            self.on_item_selected(pane_kind, primary_id, secondary_id)

        open_graph_btn.connect("clicked", _on_open_graph)
        box.append(open_graph_btn)

        pop.set_child(box)
        self._active_context_popover = pop

        def _on_closed(p: Gtk.Popover) -> None:
            if self._active_context_popover is p:
                self._active_context_popover = None
            p.unparent()

        pop.connect("closed", _on_closed)
        pop.popup()
        return pop

    def _on_tree_selection_changed(self, selection: Gtk.TreeSelection, active_pane_kind: str) -> None:
        if self._updating_selection:
            return
        model, tree_iter = selection.get_selected()
        if not tree_iter:
            return
        kind = model.get_value(tree_iter, 2)
        primary = model.get_value(tree_iter, 3)
        secondary = model.get_value(tree_iter, 4)
        if not kind or not primary:
            return

        # Clear selection in the other File View panes
        self._updating_selection = True
        try:
            for pk, tv in (
                ("base_class", self.base_tree),
                ("module", self.mod_tree),
                ("pipeline", self.pipe_tree),
                ("topology", self.top_tree),
            ):
                if pk != active_pane_kind:
                    tv.get_selection().unselect_all()
        finally:
            self._updating_selection = False

        self.on_item_selected(kind, primary, secondary or None)

    def _on_add_module_btn_clicked(self, btn: Gtk.Button) -> None:
        if not self.on_add_module_requested:
            return
        if self.main_notebook.get_current_page() == 0:
            row = self.obj_mod_list.get_selected_row()
            if row is not None:
                mod_name = getattr(row, "_item_id", None)
                if mod_name:
                    self.on_add_module_requested(mod_name)
                    return
        sel = self.mod_tree.get_selection()
        model, tree_iter = sel.get_selected()
        if tree_iter:
            mod_name = model.get_value(tree_iter, 3)
            if mod_name:
                self.on_add_module_requested(mod_name)

    def _on_add_pipeline_btn_clicked(self, btn: Gtk.Button) -> None:
        if not self.on_add_pipeline_requested:
            return
        if self.main_notebook.get_current_page() == 0:
            row = self.obj_pipe_list.get_selected_row()
            if row is not None:
                pipe_name = getattr(row, "_item_id", None)
                if pipe_name:
                    self.on_add_pipeline_requested(pipe_name)
                    return
        sel = self.pipe_tree.get_selection()
        model, tree_iter = sel.get_selected()
        if tree_iter:
            pipe_name = model.get_value(tree_iter, 3)
            if pipe_name:
                self.on_add_pipeline_requested(pipe_name)

    def _prepare_drag_item(self, payload: str) -> Gdk.ContentProvider:
        self._is_dragging = True
        TopologyGraphCanvas._active_drag_payload = payload
        self._hide_hover_zoom_box()
        val = GObject.Value(GObject.TYPE_STRING, payload)
        return Gdk.ContentProvider.new_for_value(val)

    def _on_drag_started(self) -> None:
        self._is_dragging = True
        self._hide_hover_zoom_box()

    def _on_drag_stopped(self) -> None:
        self._is_dragging = False
        TopologyGraphCanvas._active_drag_payload = None

    def _on_module_drag_prepare(self, source: Gtk.DragSource, x: float, y: float, tree: Gtk.TreeView) -> Optional[Gdk.ContentProvider]:
        sel = tree.get_selection()
        model, tree_iter = sel.get_selected()
        if tree_iter:
            mod_name = model.get_value(tree_iter, 3)
            if mod_name:
                return self._prepare_drag_item(f"module:{mod_name}")
        return None

    # ------------------------------------------------------------------
    # Hover Zoom Info Box for Object View List Items
    # ------------------------------------------------------------------
    def _on_obj_list_motion(
        self,
        _ctrl: Gtk.EventControllerMotion,
        x: float,
        y: float,
        listbox: Gtk.ListBox,
    ) -> None:
        state = _ctrl.get_current_event_state()
        btn_pressed = bool(
            state
            & (
                Gdk.ModifierType.BUTTON1_MASK
                | Gdk.ModifierType.BUTTON2_MASK
                | Gdk.ModifierType.BUTTON3_MASK
            )
        )
        if self._is_dragging or btn_pressed or TopologyGraphCanvas._active_drag_payload is not None:
            self._hide_hover_zoom_box()
            return
        row = listbox.get_row_at_y(int(y))
        if row is None:
            self._hide_hover_zoom_box()
            return
        if row is self._hovered_row:
            return
        self._hovered_row = row
        self._show_hover_zoom_box(listbox, row, y)

    def _show_hover_zoom_box(
        self,
        listbox: Gtk.ListBox,
        row: Gtk.ListBoxRow,
        pointer_y: float = 0.0,
    ) -> None:
        if not self.workspace:
            return
        item_kind = getattr(row, "_item_kind", None)
        item_id = getattr(row, "_item_id", None)
        if not item_kind or not item_id:
            self._hide_hover_zoom_box()
            return

        if item_kind == "module":
            vm = self._get_mini_module_vm(item_id)
        else:
            vm = self._get_mini_pipeline_vm(item_id)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        # Header row: Full Class Title + Badge
        hdr_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        prefix = "Class.Widget." if item_kind == "module" else "Class.Pipeline."
        title_lbl = Gtk.Label(label=f"{prefix}{item_id}", xalign=0.0)
        title_lbl.set_hexpand(True)
        title_lbl.add_css_class("zoom-info-title")
        hdr_row.append(title_lbl)

        badge_text = getattr(row, "_badge_text", "")
        if badge_text:
            badge_lbl = Gtk.Label(label=badge_text, xalign=1.0)
            badge_lbl.add_css_class("zoom-info-badge")
            hdr_row.append(badge_lbl)
        card.append(hdr_row)

        # Full description text
        desc_text = getattr(row, "_desc_text", "")
        if desc_text:
            desc_lbl = Gtk.Label(label=desc_text, xalign=0.0)
            desc_lbl.set_wrap(True)
            desc_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            desc_lbl.set_max_width_chars(88)
            desc_lbl.add_css_class("zoom-info-desc")
            card.append(desc_lbl)

        # Metadata details
        meta_lines = []
        if item_kind == "module":
            cdef = self.workspace.processing_modules.get(item_id)
            if cdef:
                if cdef.rel_path:
                    meta_lines.append(f"Source: {cdef.rel_path}")
                n_in = cdef.defaults.get("num_input_pins", 0)
                n_out = cdef.defaults.get("num_output_pins", 0)
                in_fmts = cdef.defaults.get("num_input_audio_formats", 0)
                out_fmts = cdef.defaults.get("num_output_audio_formats", 0)
                meta_lines.append(
                    f"Pins: {n_in} in / {n_out} out  |  "
                    f"Audio Formats: {in_fmts} in / {out_fmts} out"
                )
                if int(str(n_in or 0)) > 0:
                    in_det = self.workspace.extract_pin_format_details(cdef.config_tree, "in")
                    meta_lines.append(
                        f"IN Pin(s): Fmt {in_det['formats_line']}  •  Rate {in_det['rates_line']}  •  Ch {in_det['channels_line']}"
                    )
                if int(str(n_out or 0)) > 0:
                    out_det = self.workspace.extract_pin_format_details(cdef.config_tree, "out")
                    meta_lines.append(
                        f"OUT Pin(s): Fmt {out_det['formats_line']}  •  Rate {out_det['rates_line']}  •  Ch {out_det['channels_line']}"
                    )
                ctrl_names = []
                for ctrl in cdef.controls:
                    cname = ctrl.get("name") or f"{ctrl.get('type', 'control')}.{ctrl.get('index', '0')}"
                    ctrl_names.append(str(cname))
                if ctrl_names:
                    meta_lines.append(f"Controls ({len(ctrl_names)}): {', '.join(ctrl_names[:4])}")
        else:
            cdef = self.workspace.pipeline_classes.get(item_id)
            if cdef:
                if cdef.rel_path:
                    meta_lines.append(f"Source: {cdef.rel_path}")
                p_details = []
                if cdef.extends_class:
                    p_details.append(f"Extends: {cdef.extends_class}")
                td = cdef.defaults.get("time_domain")
                if td:
                    p_details.append(f"Time Domain: {td}")
                dyn = cdef.defaults.get("dynamic_pipeline")
                if dyn is not None:
                    p_details.append(f"Dynamic: {dyn}")
                if p_details:
                    meta_lines.append("  |  ".join(p_details))
                w_chain = [f"{w.get('class_name', '')}.{w.get('instance', '')}" for w in cdef.widgets]
                if w_chain:
                    meta_lines.append(f"Chain ({len(w_chain)} widgets): {' → '.join(w_chain)}")
                pin_in_nodes = [n for n in vm.nodes.values() if n.category == "pin_fmt_in"]
                pin_out_nodes = [n for n in vm.nodes.values() if n.category == "pin_fmt_out"]
                if pin_in_nodes:
                    p_in = pin_in_nodes[0]
                    meta_lines.append(
                        f"IN Pin(s) ({len(pin_in_nodes)}): "
                        f"Fmt {p_in.config.get_scalar('formats_line', '')}  •  "
                        f"Rate {p_in.config.get_scalar('rates_line', '')}  •  "
                        f"Ch {p_in.config.get_scalar('channels_line', '')}"
                    )
                if pin_out_nodes:
                    p_out = pin_out_nodes[0]
                    meta_lines.append(
                        f"OUT Pin(s) ({len(pin_out_nodes)}): "
                        f"Fmt {p_out.config.get_scalar('formats_line', '')}  •  "
                        f"Rate {p_out.config.get_scalar('rates_line', '')}  •  "
                        f"Ch {p_out.config.get_scalar('channels_line', '')}"
                    )

        if meta_lines:
            meta_lbl = Gtk.Label(label="\n".join(meta_lines), xalign=0.0)
            meta_lbl.set_wrap(True)
            meta_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            meta_lbl.set_max_width_chars(92)
            meta_lbl.add_css_class("zoom-info-meta")
            card.append(meta_lbl)

        # Zoomed-in graphical preview card
        zoom_preview = MiniGraphPreview(vm, zoom_card=True)
        card.append(zoom_preview)

        hint_lbl = Gtk.Label(
            label="Double-click to open in a new tab  •  Drag and drop onto the Main View to instantiate",
            xalign=0.5,
        )
        hint_lbl.add_css_class("zoom-info-hint")
        card.append(hint_lbl)

        self._hover_popover.set_child(card)
        if self._hover_popover.get_parent() is not listbox:
            if self._hover_popover.get_parent() is not None:
                self._hover_popover.unparent()
            self._hover_popover.set_parent(listbox)

        ok, bounds = row.compute_bounds(listbox)
        rect = Gdk.Rectangle()
        if ok:
            rect.x = int(bounds.origin.x + bounds.size.width - 6)
            rect.y = int(bounds.origin.y + bounds.size.height / 2)
            rect.width = 4
            rect.height = 4
        else:
            rect.x = max(10, listbox.get_Width() - 6 if hasattr(listbox, "get_Width") else 260)
            rect.y = int(pointer_y)
            rect.width = 4
            rect.height = 4
        self._hover_popover.set_pointing_to(rect)
        if listbox.get_mapped():
            self._hover_popover.popup()

    def _hide_hover_zoom_box(self) -> None:
        self._hovered_row = None
        if self._hover_popover.get_parent() is not None:
            self._hover_popover.popdown()
            self._hover_popover.set_visible(False)
