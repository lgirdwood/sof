# SPDX-License-Identifier: BSD-3-Clause
"""Left-hand navigation panel with two top-level Notebook pages:
1) Primary Page: "Object View" split into two sub-pages ("Modules" and "Pipelines")
   rendering miniature visual graph cards of every module and pipeline.
2) Second Page: "File View" containing the 4 vertical tree-view sub-panes
   (Base Classes, Processing Modules, Pipelines, Machine Topologies).
"""

from __future__ import annotations
from typing import Callable, Dict, Optional, Tuple

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GObject, Gtk, Pango

from .graph_canvas import MiniGraphPreview
from .model import CMakeTargetVariant, GraphViewModel, MachineTopologyEntry, TplgClassDef
from .parser import Topology2Workspace


class LeftNavigationPanel(Gtk.Box):
    """Left panel with primary 'Object View' (Modules / Pipelines visual lists) and 'File View'."""

    def __init__(
        self,
        on_item_selected: Callable[[str, str, Optional[str]], None],
        on_add_module_requested: Optional[Callable[[str], None]] = None,
        on_add_pipeline_requested: Optional[Callable[[str], None]] = None,
    ):
        """
        Args:
            on_item_selected: callback(pane_kind, primary_id, secondary_id)
                pane_kind in ("base_class", "module", "pipeline", "topology")
            on_add_module_requested: callback(module_name) when user clicks '+ Add Node'
            on_add_pipeline_requested: callback(pipeline_name) when user clicks '+ Add Pipeline'
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
        self.workspace: Optional[Topology2Workspace] = None
        self._updating_selection: bool = False
        self._filter_text: str = ""
        self._mini_vm_cache: Dict[str, GraphViewModel] = {}

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
        self.obj_mod_list.connect("row-selected", self._on_obj_module_row_selected)
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
        self.obj_pipe_list.connect("row-selected", self._on_obj_pipeline_row_selected)
        pipe_scroll = Gtk.ScrolledWindow()
        pipe_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        pipe_scroll.set_vexpand(True)
        pipe_scroll.set_child(self.obj_pipe_list)
        pipe_page.append(pipe_scroll)

        self.object_notebook.append_page(pipe_page, Gtk.Label(label="Pipelines"))

        self.main_notebook.append_page(self.object_notebook, Gtk.Label(label="Object View"))

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
        vpaned_top = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        vpaned_top.set_wide_handle(True)
        vpaned_top.set_start_child(pane1)
        vpaned_top.set_end_child(pane2)
        vpaned_top.set_position(165)

        vpaned_bot = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        vpaned_bot.set_wide_handle(True)
        vpaned_bot.set_start_child(pane3)
        vpaned_bot.set_end_child(pane4)
        vpaned_bot.set_position(185)

        vpaned_main = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        vpaned_main.set_wide_handle(True)
        vpaned_main.set_vexpand(True)
        vpaned_main.set_start_child(vpaned_top)
        vpaned_main.set_end_child(vpaned_bot)
        vpaned_main.set_position(345)

        self.main_notebook.append_page(vpaned_main, Gtk.Label(label="File View"))
        self.main_notebook.set_current_page(0)
        self.append(self.main_notebook)

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

        # Column 0: Name (auto-sized to fit full object/class/file text)
        r0 = Gtk.CellRendererText()
        r0.set_padding(4, 2)
        c0 = Gtk.TreeViewColumn(col_titles[0], r0, text=0)
        c0.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        c0.set_resizable(True)
        c0.set_expand(True)
        tree.append_column(c0)

        # Column 1: Summary (auto-sized to fit full summary/defines text)
        r1 = Gtk.CellRendererText()
        r1.set_padding(4, 2)
        r1.set_property("foreground", "#8fa3bf")
        c1 = Gtk.TreeViewColumn(col_titles[1], r1, text=1)
        c1.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        c1.set_resizable(True)
        tree.append_column(c1)

        tree.connect("row-expanded", lambda tv, _it, _path: tv.columns_autosize())

        sel = tree.get_selection()
        sel.set_mode(Gtk.SelectionMode.SINGLE)
        sel.connect("changed", self._on_tree_selection_changed, pane_kind)

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
                tv.columns_autosize()
        finally:
            self._updating_selection = False

    def _populate_object_modules(self) -> None:
        self._clear_listbox(self.obj_mod_list)
        if not self.workspace:
            return
        count = 0
        for mname, cdef in sorted(self.workspace.processing_modules.items()):
            wtype = str(cdef.defaults.get("type", "effect"))
            if not self._matches_filter(mname, wtype, cdef.rel_path):
                continue
            vm = self._get_mini_module_vm(mname)
            preview = MiniGraphPreview(vm)

            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row_box.set_margin_top(3)
            row_box.set_margin_bottom(3)
            row_box.set_margin_start(3)
            row_box.set_margin_end(3)
            row_box.append(preview)

            row = Gtk.ListBoxRow()
            row.set_child(row_box)
            row.set_tooltip_text(f"Drag and drop '{mname}' onto the main view to add this module")
            row._item_id = mname  # type: ignore[attr-defined]
            row._preview = preview  # type: ignore[attr-defined]

            drag_src = Gtk.DragSource.new()
            drag_src.set_actions(Gdk.DragAction.COPY)
            drag_src.connect(
                "prepare",
                lambda _src, _x, _y, mn=mname: Gdk.ContentProvider.new_for_value(
                    GObject.Value(GObject.TYPE_STRING, f"module:{mn}")
                ),
            )
            row.add_controller(drag_src)

            self.obj_mod_list.append(row)
            count += 1
        self.obj_mod_count_lbl.set_text(f"Modules ({count})")

    def _populate_object_pipelines(self) -> None:
        self._clear_listbox(self.obj_pipe_list)
        if not self.workspace:
            return
        count = 0
        for pname, pdef in sorted(self.workspace.pipeline_classes.items()):
            direction = str(pdef.defaults.get("direction", "playback"))
            if not self._matches_filter(pname, direction, pdef.rel_path):
                continue
            vm = self._get_mini_pipeline_vm(pname)
            preview = MiniGraphPreview(vm)

            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row_box.set_margin_top(3)
            row_box.set_margin_bottom(3)
            row_box.set_margin_start(3)
            row_box.set_margin_end(3)
            row_box.append(preview)

            row = Gtk.ListBoxRow()
            row.set_child(row_box)
            row.set_tooltip_text(f"Drag and drop '{pname}' onto the main view to instantiate this pipeline")
            row._item_id = pname  # type: ignore[attr-defined]
            row._preview = preview  # type: ignore[attr-defined]

            drag_src = Gtk.DragSource.new()
            drag_src.set_actions(Gdk.DragAction.COPY)
            drag_src.connect(
                "prepare",
                lambda _src, _x, _y, pn=pname: Gdk.ContentProvider.new_for_value(
                    GObject.Value(GObject.TYPE_STRING, f"pipeline:{pn}")
                ),
            )
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

    def _on_module_drag_prepare(self, source: Gtk.DragSource, x: float, y: float, tree: Gtk.TreeView) -> Optional[Gdk.ContentProvider]:
        sel = tree.get_selection()
        model, tree_iter = sel.get_selected()
        if tree_iter:
            mod_name = model.get_value(tree_iter, 3)
            if mod_name:
                val = GObject.Value(GObject.TYPE_STRING, f"module:{mod_name}")
                return Gdk.ContentProvider.new_for_value(val)
        return None
