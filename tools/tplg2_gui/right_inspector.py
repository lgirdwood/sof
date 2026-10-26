# SPDX-License-Identifier: BSD-3-Clause
"""Right-hand panel displaying a hierarchical editable TreeView of attributes for the selected Graph Node."""

from __future__ import annotations
from typing import Callable, Dict, Optional

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango

from .model import ConfigNode


class RightAttributeInspector(Gtk.Box):
    """Right-hand panel showing a tree view of attributes of the selected graph node."""

    def __init__(
        self,
        on_attribute_edited: Optional[Callable[[ConfigNode, str, str], None]] = None,
        on_before_attribute_edit: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_size_request(330, -1)
        self.set_margin_start(4)
        self.set_margin_end(6)
        self.set_margin_top(6)
        self.set_margin_bottom(6)

        self.on_attribute_edited = on_attribute_edited
        self.on_before_attribute_edit = on_before_attribute_edit
        self.current_config: Optional[ConfigNode] = None
        # Map TreePath string -> ConfigNode reference for instant in-place AST updates
        self._path_to_node: Dict[str, ConfigNode] = {}

        # Header frame showing selected node name & class info
        hdr_frame = Gtk.Frame()
        hdr_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        hdr_box.set_margin_start(8)
        hdr_box.set_margin_end(8)
        hdr_box.set_margin_top(6)
        hdr_box.set_margin_bottom(6)

        self.node_title_lbl = Gtk.Label(label="No Node Selected")
        self.node_title_lbl.set_xalign(0.0)
        self.node_title_lbl.set_wrap(True)
        self.node_title_lbl.set_selectable(True)
        self.node_title_lbl.add_css_class("heading")
        hdr_box.append(self.node_title_lbl)

        self.node_sub_lbl = Gtk.Label(label="Select a node in the graph to inspect/edit attributes")
        self.node_sub_lbl.set_xalign(0.0)
        self.node_sub_lbl.set_wrap(True)
        self.node_sub_lbl.set_selectable(True)
        hdr_box.append(self.node_sub_lbl)

        hdr_frame.set_child(hdr_box)
        self.append(hdr_frame)

        # Filter + Add Attribute toolbar
        tool_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.filter_entry = Gtk.SearchEntry()
        self.filter_entry.set_placeholder_text("Filter node attributes…")
        self.filter_entry.set_hexpand(True)
        self.filter_entry.connect("search-changed", self._on_filter_changed)
        tool_row.append(self.filter_entry)

        self.add_attr_btn = Gtk.Button(label="+ Attr")
        self.add_attr_btn.set_tooltip_text("Add or override an attribute on the selected node")
        self.add_attr_btn.connect("clicked", self._on_add_attr_clicked)
        tool_row.append(self.add_attr_btn)
        self.append(tool_row)

        # Inline quick-add attribute row (collapsible)
        self.add_row_revealer = Gtk.Revealer()
        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.new_key_entry = Gtk.Entry()
        self.new_key_entry.set_placeholder_text("Attr name")
        self.new_key_entry.set_hexpand(True)
        self.new_val_entry = Gtk.Entry()
        self.new_val_entry.set_placeholder_text("Value")
        self.new_val_entry.set_hexpand(True)
        confirm_btn = Gtk.Button(label="Set")
        confirm_btn.connect("clicked", self._on_confirm_new_attr)
        add_box.append(self.new_key_entry)
        add_box.append(self.new_val_entry)
        add_box.append(confirm_btn)
        self.add_row_revealer.set_child(add_box)
        self.append(self.add_row_revealer)

        # TreeStore columns:
        # 0: Key (str)
        # 1: Value (str)
        # 2: Type / Constraints / Origin (str)
        # 3: Editable (bool)
        # 4: Value foreground color (str)
        self.store = Gtk.TreeStore.new([str, str, str, bool, str])
        self.tree = Gtk.TreeView.new_with_model(self.store)
        self.tree.set_headers_visible(True)
        self.tree.set_enable_tree_lines(True)

        # Column 0: Attribute Name (auto-sized to fit full object/attribute text)
        r_key = Gtk.CellRendererText()
        r_key.set_padding(4, 2)
        col_key = Gtk.TreeViewColumn("Attribute", r_key, text=0)
        col_key.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        col_key.set_resizable(True)
        self.tree.append_column(col_key)

        # Column 1: Editable Value (auto-sized initially, or fixed when resized/restored)
        r_val = Gtk.CellRendererText()
        r_val.set_padding(4, 2)
        r_val.connect("edited", self._on_value_cell_edited)
        col_val = Gtk.TreeViewColumn("Value (Dbl-Click Edit)", r_val, text=1, editable=3, foreground=4)
        col_val.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        col_val.set_resizable(True)
        self.tree.append_column(col_val)

        # Column 2: Metadata / Constraints / Origin (auto-sized to fit full constraints text)
        r_meta = Gtk.CellRendererText()
        r_meta.set_padding(4, 2)
        r_meta.set_property("foreground", "#8fa3bf")
        col_meta = Gtk.TreeViewColumn("Constraints / Origin", r_meta, text=2)
        col_meta.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        col_meta.set_resizable(True)
        self.tree.append_column(col_meta)

        self.tree.connect("row-expanded", lambda _tv, _it, _path: self._maybe_autosize_columns())

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(self.tree)

        tree_frame = Gtk.Frame()
        tree_frame.set_vexpand(True)
        tree_frame.set_child(scroll)
        self.append(tree_frame)

    def set_node_config(
        self,
        title: Optional[str],
        config: Optional[ConfigNode],
        subtitle: str = "",
    ) -> None:
        """Populate the attribute tree with the selected node's ConfigNode hierarchy."""
        self.current_config = config
        self.node_title_lbl.set_text(title or "No Selection")
        self.node_sub_lbl.set_text(subtitle or "")
        self._rebuild_tree()

    def _on_filter_changed(self, entry: Gtk.SearchEntry) -> None:
        self._rebuild_tree()

    def _maybe_autosize_columns(self) -> None:
        if any(c.get_sizing() == Gtk.TreeViewColumnSizing.FIXED for c in self.tree.get_columns()):
            return
        self.tree.columns_autosize()

    def _rebuild_tree(self) -> None:
        self.store.clear()
        self._path_to_node.clear()
        if not self.current_config:
            return

        flt = self.filter_entry.get_text().strip().lower()
        for child in self.current_config.children:
            self._append_config_subtree(None, child, flt)
        self.tree.expand_all()
        self._maybe_autosize_columns()

    def _node_matches(self, node: ConfigNode, flt: str) -> bool:
        if not flt:
            return True
        if flt in node.key.lower() or flt in str(node.value or "").lower() or flt in node.category_info.lower():
            return True
        return any(self._node_matches(c, flt) for c in node.children)

    def _append_config_subtree(
        self,
        parent_iter: Optional[Gtk.TreeIter],
        node: ConfigNode,
        flt: str,
    ) -> None:
        if not self._node_matches(node, flt):
            return

        is_scalar = node.node_type == "scalar"
        editable = is_scalar and not node.is_immutable
        val_str = str(node.value if node.value is not None else "")
        if not is_scalar and not val_str:
            val_str = f"({len(node.children)} items)"

        meta_parts = []
        if node.origin:
            meta_parts.append(node.origin)
        if node.category_info and node.category_info != node.origin:
            meta_parts.append(node.category_info)
        meta_str = " • ".join(meta_parts)

        if node.is_immutable:
            fg = "#9aa5b4"
        elif node.origin == "instance":
            fg = "#6ee7b7"
        else:
            fg = "#e2e8f0"

        it = self.store.append(parent_iter, [node.key, val_str, meta_str, editable, fg])
        path_str = self.store.get_path(it).to_string()
        self._path_to_node[path_str] = node

        for ch in node.children:
            self._append_config_subtree(it, ch, flt)

    def _on_value_cell_edited(self, renderer: Gtk.CellRendererText, path_str: str, new_text: str) -> None:
        cnode = self._path_to_node.get(path_str)
        if cnode is None or cnode.is_immutable:
            return
        if str(cnode.value if cnode.value is not None else "") == new_text:
            return
        if self.on_before_attribute_edit:
            self.on_before_attribute_edit(f"Edit attribute '{cnode.key}'")

        cnode.value = new_text
        cnode.origin = "instance"
        it = self.store.get_iter_from_string(path_str)
        if it:
            self.store.set_value(it, 1, new_text)
            self.store.set_value(it, 4, "#6ee7b7")
        self._maybe_autosize_columns()

        if self.on_attribute_edited and self.current_config:
            self.on_attribute_edited(self.current_config, cnode.key, new_text)

    def _on_add_attr_clicked(self, btn: Gtk.Button) -> None:
        revealed = self.add_row_revealer.get_reveal_child()
        self.add_row_revealer.set_reveal_child(not revealed)

    def _on_confirm_new_attr(self, btn: Gtk.Button) -> None:
        if not self.current_config:
            return
        k = self.new_key_entry.get_text().strip()
        v = self.new_val_entry.get_text().strip()
        if not k:
            return
        if self.on_before_attribute_edit:
            self.on_before_attribute_edit(f"Add attribute '{k}'")
        self.current_config.set_scalar(k, v, category_info="user", origin="instance")
        self.new_key_entry.set_text("")
        self.new_val_entry.set_text("")
        self.add_row_revealer.set_reveal_child(False)
        self._rebuild_tree()
        if self.on_attribute_edited:
            self.on_attribute_edited(self.current_config, k, v)

    def highlight_attribute(
        self,
        attr_key: Optional[str],
        severity: str = "error",
        message: Optional[str] = None,
        target_kind: str = "root",
    ) -> bool:
        """Select, scroll to, and highlight `attr_key` in the attribute TreeView."""
        color = "#ff6b6b" if severity == "error" else "#ffd166"
        prefix = "⛔ ERROR" if severity == "error" else "⚠ WARN"
        if message:
            self.node_sub_lbl.set_markup(
                f"<span foreground='{color}' weight='bold'>{prefix}:</span> {GLib_markup_escape(message)}"
            )

        if not self.current_config or not attr_key:
            return False

        # Clear filter if active so the target attribute is guaranteed visible
        if self.filter_entry.get_text().strip():
            self.filter_entry.set_text("")
            self._rebuild_tree()

        best_path_str: Optional[str] = None
        for path_str, cnode in self._path_to_node.items():
            if cnode.key == attr_key:
                best_path_str = path_str
                # Prefer scalar nodes (or Define children when target_kind == 'define')
                if cnode.node_type == "scalar":
                    break

        if best_path_str is None:
            # Case-insensitive or suffix fallback
            ak_low = attr_key.lower()
            for path_str, cnode in self._path_to_node.items():
                if cnode.key.lower() == ak_low or cnode.key.lower().endswith(f".{ak_low}"):
                    best_path_str = path_str
                    break

        if best_path_str is None:
            # Attribute is missing on the selected node (e.g. Mandatory attribute not set):
            # open the quick-add attribute row pre-populated with attr_key
            self.new_key_entry.set_text(attr_key)
            self.add_row_revealer.set_reveal_child(True)
            return False

        it = self.store.get_iter_from_string(best_path_str)
        if not it:
            return False

        old_meta = self.store.get_value(it, 2) or ""
        diag_note = f"{prefix}: {message}" if message else prefix
        new_meta = f"{diag_note} • {old_meta}" if old_meta and diag_note not in old_meta else diag_note
        self.store.set_value(it, 2, new_meta)
        self.store.set_value(it, 4, color)

        tree_path = Gtk.TreePath.new_from_string(best_path_str)
        self.tree.expand_to_path(tree_path)
        sel = self.tree.get_selection()
        sel.select_path(tree_path)
        self.tree.scroll_to_cell(tree_path, None, True, 0.5, 0.0)
        self._maybe_autosize_columns()
        return True


def GLib_markup_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
