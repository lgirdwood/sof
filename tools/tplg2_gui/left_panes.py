# SPDX-License-Identifier: BSD-3-Clause
"""Left-hand panel containing 4 vertical sub-panes with tree views:
1) Base Classes
2) Processing Modules
3) Pipelines
4) Machine Topologies
"""

from __future__ import annotations
from typing import Callable, Optional, Tuple

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GObject, Gtk, Pango

from .model import CMakeTargetVariant, MachineTopologyEntry, TplgClassDef
from .parser import Topology2Workspace


class LeftNavigationPanel(Gtk.Box):
    """Left panel housing 4 vertical tree-view sub-panes."""

    def __init__(
        self,
        on_item_selected: Callable[[str, str, Optional[str]], None],
        on_add_module_requested: Optional[Callable[[str], None]] = None,
    ):
        """
        Args:
            on_item_selected: callback(pane_kind, primary_id, secondary_id)
                pane_kind in ("base_class", "module", "pipeline", "topology")
            on_add_module_requested: callback(module_name) when user clicks '+ Add to Graph'
        """
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.set_size_request(300, -1)
        self.set_margin_start(6)
        self.set_margin_end(4)
        self.set_margin_top(6)
        self.set_margin_bottom(6)

        self.on_item_selected = on_item_selected
        self.on_add_module_requested = on_add_module_requested
        self.workspace: Optional[Topology2Workspace] = None
        self._updating_selection: bool = False
        self._filter_text: str = ""

        # Search entry filtering all 4 tree panes
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Filter classes, modules, pipelines, topologies…")
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.append(self.search_entry)

        # Create the 4 vertical sub-panes
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
        vpaned_top.set_start_child(pane1)
        vpaned_top.set_end_child(pane2)
        vpaned_top.set_position(165)

        vpaned_bot = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        vpaned_bot.set_start_child(pane3)
        vpaned_bot.set_end_child(pane4)
        vpaned_bot.set_position(185)

        vpaned_main = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        vpaned_main.set_vexpand(True)
        vpaned_main.set_start_child(vpaned_top)
        vpaned_main.set_end_child(vpaned_bot)
        vpaned_main.set_position(345)

        self.append(vpaned_main)

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

        # Column 0: Name
        r0 = Gtk.CellRendererText()
        r0.set_property("ellipsize", Pango.EllipsizeMode.END)
        c0 = Gtk.TreeViewColumn(col_titles[0], r0, text=0)
        c0.set_resizable(True)
        c0.set_expand(True)
        c0.set_min_width(140)
        tree.append_column(c0)

        # Column 1: Summary
        r1 = Gtk.CellRendererText()
        r1.set_property("ellipsize", Pango.EllipsizeMode.END)
        r1.set_property("foreground", "#8fa3bf")
        c1 = Gtk.TreeViewColumn(col_titles[1], r1, text=1)
        c1.set_resizable(True)
        c1.set_min_width(80)
        tree.append_column(c1)

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
        self._rebuild_stores()

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._filter_text = entry.get_text().strip().lower()
        self._rebuild_stores()

    def _matches_filter(self, *texts: str) -> bool:
        if not self._filter_text:
            return True
        combined = " ".join(t.lower() for t in texts if t)
        return self._filter_text in combined

    def _rebuild_stores(self) -> None:
        if not self.workspace:
            return
        self._updating_selection = True
        try:
            self._populate_base_classes()
            self._populate_processing_modules()
            self._populate_pipelines()
            self._populate_machine_topologies()
        finally:
            self._updating_selection = False

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

        # Clear selection in the other 3 panes so it's always clear which item is active
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
        sel = self.mod_tree.get_selection()
        model, tree_iter = sel.get_selected()
        if tree_iter:
            mod_name = model.get_value(tree_iter, 3)
            if mod_name and self.on_add_module_requested:
                self.on_add_module_requested(mod_name)

    def _on_module_drag_prepare(self, source: Gtk.DragSource, x: float, y: float, tree: Gtk.TreeView) -> Optional[Gdk.ContentProvider]:
        sel = tree.get_selection()
        model, tree_iter = sel.get_selected()
        if tree_iter:
            mod_name = model.get_value(tree_iter, 3)
            if mod_name:
                val = GObject.Value(GObject.TYPE_STRING, f"module:{mod_name}")
                return Gdk.ContentProvider.new_for_value(val)
        return None
