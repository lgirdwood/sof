# SPDX-License-Identifier: BSD-3-Clause
"""Main GTK4 Window with 3 horizontal panels:
- Left: 4 vertical sub-panes (Base Classes, Processing Modules, Pipelines, Machine Topologies)
- Middle: Gtk.Snapshot Graph Canvas + bottom Compile/Testbench waveform & log drawer
- Right: Tree view of attributes of the selected graph node
"""

from __future__ import annotations
from dataclasses import dataclass, field
import os
import shutil
import subprocess
import threading
from typing import Callable, Dict, List, Optional, Tuple

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from .compiler import (
    CompileDiagnostic,
    CompileResult,
    TopologyCompiler,
    classify_log_line,
    parse_compile_diagnostics,
)
from .graph_canvas import TopologyGraphCanvas
from .layout import _compute_node_geometry, layout_graph
from .left_panes import LeftNavigationPanel
from .model import (
    ConfigNode,
    GraphNode,
    GraphViewModel,
    debug_log_action,
    debug_log_command,
    debug_log_line,
)
from .parser import Topology2Workspace
from .right_inspector import RightAttributeInspector
from .serializer import (
    _extract_active_pipeline_ids,
    generate_compilable_conf,
    get_editable_conf_text_for_vm,
    serialize_graph_for_save,
)
from .testbench import (
    KcontrolTelemetryItem,
    SOF_CDEV_WIN_DEBUG,
    SOF_CDEV_WIN_FW_REGS,
    SOF_CDEV_WIN_PERF,
    SOF_PCM_OP_CAPTURE_BUFFER,
    SOF_PCM_OP_START_TONE,
    SOF_PCM_OP_STOP_TONE,
    SOF_PM_OP_RELEASE_HOLD,
    SOF_PM_OP_WAKE_HOLD,
    SOF_WORKLOAD_MEM_DRAM,
    SOF_WORKLOAD_MEM_SRAM,
    SOF_WORKLOAD_SIM_OFF,
    SOF_WORKLOAD_SIM_REALTIME,
    SOF_WORKLOAD_SIM_VIRTUAL,
    TestbenchRunResult,
    TestbenchRunner,
    TestbenchTelemetry,
    WaveformSnapshotWidget,
)
from .text_editor import TopologyTextEditor


@dataclass
class GraphTabPage:
    """State for an open file tab in the Main Graph Notebook (holding both Graph Canvas and Text Editor)."""

    tab_key: str
    pane_kind: str
    primary_id: str
    secondary_id: Optional[str]
    canvas: TopologyGraphCanvas
    text_editor: TopologyTextEditor
    view_stack: Gtk.Stack
    mode_switch_btn: Gtk.ToggleButton
    tab_box: Gtk.Box
    title_lbl: Gtk.Label
    is_untitled: bool = False
    text_needs_refresh_from_graph: bool = True
    stream_waveforms: Dict[str, Tuple[List[float], List[float]]] = field(default_factory=dict)
    last_tb_summary: str = ""


class Topology2MainWindow(Gtk.ApplicationWindow):
    """Main GTK4 Application Window for SOF Topology2 Visual Editor & Testbench Runner."""

    def __init__(self, app: Gtk.Application, initial_dir: Optional[str] = None):
        super().__init__(application=app, title="SOF Topology2 Visual Graph Editor & Testbench")
        self.set_decorated(True)
        self.set_default_size(1560, 940)
        self.maximize()

        win_key = Gtk.EventControllerKey.new()
        win_key.connect("key-pressed", self._on_window_key_pressed)
        self.add_controller(win_key)
        self.connect("close-request", self._on_window_close_request)

        self.workspace: Optional[Topology2Workspace] = None
        self.compiler: Optional[TopologyCompiler] = None
        self.tb_runner: Optional[TestbenchRunner] = None
        self.last_compiled_tplg: Optional[str] = None
        self.last_output_wav: Optional[str] = None
        self.last_telemetry: Optional[TestbenchTelemetry] = None
        self._discovered_kcontrols: List[KcontrolTelemetryItem] = []
        self._pending_kctl_overrides: Dict[str, List[int]] = {}
        self._pending_bytes_overrides: Dict[str, str] = {}
        self._syncing_remote_controls: bool = False
        self._log_line_diagnostics: Dict[int, CompileDiagnostic] = {}
        self._last_compile_diagnostics: List[CompileDiagnostic] = []
        self._last_conf_path: str = ""
        self._last_allocated_width: int = 0
        self._last_allocated_height: int = 0
        self._rt_timer_id: Optional[int] = None
        self._rt_poll_busy: bool = False
        self._pipeline_sync_seq: int = 0
        self.graph_tabs: List[GraphTabPage] = []
        self._new_file_counter: int = 0
        self.target_platform: str = "mtl"
        self.target_harness_kind: str = "hda_analog"
        self.target_platform_def_path: str = "platform/intel/mtl.conf"
        self.target_variant_name: str = "sof-hda-generic"
        self._fallback_canvas = TopologyGraphCanvas(
            on_selection_changed=self._on_canvas_selection_changed,
            on_graph_modified=self._on_graph_modified,
            on_history_changed=self._update_undo_redo_buttons,
            on_kcontrol_changed=self._on_canvas_kcontrol_changed,
            on_probe_tap_toggled=self._on_canvas_probe_tap_toggled,
            on_testbench_io_changed=self._on_canvas_testbench_io_changed,
        )

        self._build_header_bar()
        self._build_main_3panel_layout()

        # Load initial topology2 directory if valid
        default_dir = initial_dir or "/home/lrg/work/sof-arl/sof-arl/tools/topology/topology2"
        if os.path.isdir(default_dir):
            self.load_topology2_directory(default_dir)

    @property
    def canvas(self) -> TopologyGraphCanvas:
        """Return the active tab's TopologyGraphCanvas (or fallback canvas before any tab is opened)."""
        active_tab = self.get_active_tab()
        if active_tab is not None:
            return active_tab.canvas
        return self._fallback_canvas

    def get_active_tab(self) -> Optional[GraphTabPage]:
        """Return the currently selected GraphTabPage in the Main Graph Notebook."""
        if not self.graph_tabs:
            return None
        if hasattr(self, "graph_notebook") and self.graph_notebook is not None:
            page_num = self.graph_notebook.get_current_page()
            if page_num >= 0:
                page_widget = self.graph_notebook.get_nth_page(page_num)
                for tab in self.graph_tabs:
                    if tab.view_stack is page_widget or tab.canvas is page_widget:
                        return tab
        return self.graph_tabs[-1]

    def _on_window_key_pressed(
        self,
        _ctrl: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if keyval == Gdk.KEY_F11:
            if self.is_fullscreen():
                self.unfullscreen()
            else:
                self.fullscreen()
            return True

        focus = self.get_focus()
        ctrl_held = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift_held = bool(state & Gdk.ModifierType.SHIFT_MASK)

        # File / Tab / Mode shortcuts that work even when focus is inside TextView
        if ctrl_held and not isinstance(focus, Gtk.Editable):
            if keyval in (Gdk.KEY_t, Gdk.KEY_T) and not shift_held:
                self.toggle_active_tab_text_mode()
                return True
            if keyval in (Gdk.KEY_s, Gdk.KEY_S):
                if shift_held:
                    self.action_save_as()
                else:
                    self.action_save()
                return True
            if keyval in (Gdk.KEY_o, Gdk.KEY_O):
                if shift_held:
                    self._on_open_dir_clicked(None)
                else:
                    self.action_open_file()
                return True
            if keyval in (Gdk.KEY_w, Gdk.KEY_W) and not shift_held:
                self.close_current_tab()
                return True
            if keyval in (Gdk.KEY_e, Gdk.KEY_E) and not shift_held:
                self.action_export_pdf()
                return True
            if keyval in (Gdk.KEY_n, Gdk.KEY_N) and not shift_held:
                self.action_new_monolithic_topology()
                return True
            if shift_held and keyval in (Gdk.KEY_m, Gdk.KEY_M):
                self.action_new_module()
                return True
            if shift_held and keyval in (Gdk.KEY_p, Gdk.KEY_P):
                self.action_new_feature_topology()
                return True
            if shift_held and keyval in (Gdk.KEY_f, Gdk.KEY_F):
                self.action_new_function_topology()
                return True

        # Canvas-specific editing & view shortcuts (do not intercept when typing in Entry or TextView)
        if not isinstance(focus, (Gtk.Editable, Gtk.TextView)):
            if ctrl_held:
                if keyval in (Gdk.KEY_z, Gdk.KEY_Z):
                    if shift_held:
                        self._on_redo_clicked(None)
                    else:
                        self._on_undo_clicked(None)
                    return True
                if keyval in (Gdk.KEY_y, Gdk.KEY_Y):
                    self._on_redo_clicked(None)
                    return True
                if keyval in (Gdk.KEY_a, Gdk.KEY_A) and not shift_held:
                    self.action_select_all()
                    return True
                if keyval in (Gdk.KEY_c, Gdk.KEY_C) and not shift_held:
                    self.action_copy()
                    return True
                if keyval in (Gdk.KEY_x, Gdk.KEY_X) and not shift_held:
                    self.action_cut()
                    return True
                if keyval in (Gdk.KEY_v, Gdk.KEY_V) and not shift_held:
                    self.action_paste()
                    return True
            else:
                if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
                    self.action_delete_selected()
                    return True
                if keyval in (Gdk.KEY_f, Gdk.KEY_F):
                    self.canvas.fit_to_view()
                    return True
                if keyval in (Gdk.KEY_l, Gdk.KEY_L):
                    self._on_relayout_clicked(None)
                    return True
        return False

    def _register_win_action(self, name: str, callback: Callable[[], None]) -> Gio.SimpleAction:
        act = Gio.SimpleAction.new(name, None)
        act.connect("activate", lambda _a, _p: callback())
        self.add_action(act)
        return act

    def _register_stateful_str_action(
        self,
        name: str,
        initial_val: str,
        callback: Callable[[str], None],
    ) -> Gio.SimpleAction:
        act = Gio.SimpleAction.new_stateful(
            name,
            GLib.VariantType.new("s"),
            GLib.Variant.new_string(initial_val),
        )

        def on_activate(action: Gio.SimpleAction, param: Optional[GLib.Variant]) -> None:
            if param is None:
                return
            val = param.get_string()
            action.set_state(GLib.Variant.new_string(val))
            callback(val)

        act.connect("activate", on_activate)
        self.add_action(act)
        return act

    @staticmethod
    def _append_str_action_item(menu: Gio.Menu, label: str, action_name: str, target_val: str) -> None:
        item = Gio.MenuItem.new(label, None)
        item.set_action_and_target_value(action_name, GLib.Variant.new_string(target_val))
        menu.append_item(item)

    def _build_menubar(self) -> Gtk.PopoverMenuBar:
        """Create the main application Gtk.PopoverMenuBar with File, Edit, View, and Target menus."""
        # Register window Gio.SimpleActions
        self._register_win_action("new_module", lambda: self.action_new_module())
        self._register_win_action("new_feature_topology", lambda: self.action_new_feature_topology())
        self._register_win_action("new_function_topology", lambda: self.action_new_function_topology())
        self._register_win_action("new_monolithic_topology", lambda: self.action_new_monolithic_topology())
        self._register_win_action("open_file", self.action_open_file)
        self._register_win_action("open_dir", lambda: self._on_open_dir_clicked(None))
        self._register_win_action("save", lambda: self.action_save())
        self._register_win_action("save_as", self.action_save_as)
        self._register_win_action("export_conf", lambda: self._on_export_conf_clicked(None))
        self._register_win_action("export_pdf", self.action_export_pdf)
        self._register_win_action("close_tab", lambda: self.close_current_tab())

        self.undo_action = self._register_win_action("undo", lambda: self._on_undo_clicked(None))
        self.undo_action.set_enabled(False)
        self.redo_action = self._register_win_action("redo", lambda: self._on_redo_clicked(None))
        self.redo_action.set_enabled(False)

        self._register_win_action("select_all", lambda: self.action_select_all())
        self._register_win_action("cut", lambda: self.action_cut())
        self._register_win_action("copy", lambda: self.action_copy())
        self._register_win_action("paste", lambda: self.action_paste())
        self._register_win_action("delete_selected", self.action_delete_selected)

        self._register_win_action("fit_graph", lambda: self.canvas.fit_to_view())
        self._register_win_action("auto_layout", lambda: self._on_relayout_clicked(None))
        self._register_win_action("toggle_text_mode", lambda: self.toggle_active_tab_text_mode())
        self._register_win_action("zoom_in", lambda: self.canvas.zoom_in())
        self._register_win_action("zoom_out", lambda: self.canvas.zoom_out())
        self._register_win_action("reset_zoom", lambda: self.canvas.reset_zoom_1to1())
        self._register_win_action(
            "toggle_fullscreen",
            lambda: self.unfullscreen() if self.is_fullscreen() else self.fullscreen(),
        )

        # Target Menu Actions
        self.target_platform_action = self._register_stateful_str_action(
            "select_target_platform",
            self.target_platform,
            self.action_select_target_platform,
        )
        self.target_harness_action = self._register_stateful_str_action(
            "select_target_harness",
            self.target_harness_kind,
            self.action_select_target_harness,
        )
        self.target_platform_def_action = self._register_stateful_str_action(
            "select_platform_def",
            self.target_platform_def_path,
            self.action_select_platform_def,
        )
        self.target_variant_action = self._register_stateful_str_action(
            "select_target_variant",
            self.target_variant_name,
            self.action_select_target_variant,
        )
        self._register_win_action("open_active_platform_def", lambda: self.action_open_active_platform_def())

        app = self.get_application()
        if app is not None:
            accels = {
                "win.new_module": ["<Control><Shift>m"],
                "win.new_feature_topology": ["<Control><Shift>p"],
                "win.new_function_topology": ["<Control><Shift>f"],
                "win.new_monolithic_topology": ["<Control>n"],
                "win.open_file": ["<Control>o"],
                "win.open_dir": ["<Control><Shift>o"],
                "win.save": ["<Control>s"],
                "win.save_as": ["<Control><Shift>s"],
                "win.export_pdf": ["<Control>e"],
                "win.close_tab": ["<Control>w"],
                "win.toggle_text_mode": ["<Control>t"],
                "win.zoom_in": ["<Control>plus", "<Control>equal"],
                "win.zoom_out": ["<Control>minus"],
                "win.reset_zoom": ["<Control>0"],
                "win.toggle_fullscreen": ["F11"],
            }
            for act_name, keys in accels.items():
                app.set_accels_for_action(act_name, keys)

        menubar_model = Gio.Menu.new()

        # 1. File Menu
        file_menu = Gio.Menu.new()

        new_submenu = Gio.Menu.new()
        new_submenu.append("Module (Class.Widget)", "win.new_module")
        new_submenu.append("Feature Topology (Class.Pipeline)", "win.new_feature_topology")
        new_submenu.append("Function Topology (Multi-Pipeline)", "win.new_function_topology")
        new_submenu.append("Monolithic Topology (sof-*.conf)", "win.new_monolithic_topology")

        file_new_sec = Gio.Menu.new()
        file_new_sec.append_submenu("New", new_submenu)
        file_new_sec.append("Open Topology File…", "win.open_file")
        file_new_sec.append("Open Topology2 Directory…", "win.open_dir")
        file_menu.append_section(None, file_new_sec)

        file_save_sec = Gio.Menu.new()
        file_save_sec.append("Save", "win.save")
        file_save_sec.append("Save As…", "win.save_as")
        file_save_sec.append("Export Compilable .conf", "win.export_conf")
        file_save_sec.append("Export to PDF…", "win.export_pdf")
        file_menu.append_section(None, file_save_sec)

        file_close_sec = Gio.Menu.new()
        file_close_sec.append("Close Tab", "win.close_tab")
        file_menu.append_section(None, file_close_sec)

        menubar_model.append_submenu("File", file_menu)

        # 2. Edit Menu (Undo, Redo, Select All, Cut, Copy, Paste, Delete)
        edit_menu = Gio.Menu.new()

        edit_hist_sec = Gio.Menu.new()
        edit_hist_sec.append("Undo", "win.undo")
        edit_hist_sec.append("Redo", "win.redo")
        edit_menu.append_section(None, edit_hist_sec)

        edit_clip_sec = Gio.Menu.new()
        edit_clip_sec.append("Select All", "win.select_all")
        edit_clip_sec.append("Cut", "win.cut")
        edit_clip_sec.append("Copy", "win.copy")
        edit_clip_sec.append("Paste", "win.paste")
        edit_menu.append_section(None, edit_clip_sec)

        edit_del_sec = Gio.Menu.new()
        edit_del_sec.append("Delete", "win.delete_selected")
        edit_menu.append_section(None, edit_del_sec)

        menubar_model.append_submenu("Edit", edit_menu)

        # 3. View Menu (Fit Graph, Auto-Layout, Toggle Text/Graph, Zoom In/Out/Reset, Fullscreen)
        view_menu = Gio.Menu.new()

        view_layout_sec = Gio.Menu.new()
        view_layout_sec.append("Fit Graph", "win.fit_graph")
        view_layout_sec.append("Auto-Layout", "win.auto_layout")
        view_layout_sec.append("Toggle Text / Graph View", "win.toggle_text_mode")
        view_menu.append_section(None, view_layout_sec)

        view_zoom_sec = Gio.Menu.new()
        view_zoom_sec.append("Zoom In", "win.zoom_in")
        view_zoom_sec.append("Zoom Out", "win.zoom_out")
        view_zoom_sec.append("Reset Zoom (1:1)", "win.reset_zoom")
        view_menu.append_section(None, view_zoom_sec)

        view_win_sec = Gio.Menu.new()
        view_win_sec.append("Toggle Fullscreen", "win.toggle_fullscreen")
        view_menu.append_section(None, view_win_sec)

        menubar_model.append_submenu("View", view_menu)

        # 4. Target Menu (Platform Architecture)
        self.target_menu = Gio.Menu.new()
        self._populate_target_menu()
        menubar_model.append_submenu("Target", self.target_menu)

        self.menubar_model = menubar_model
        menubar = Gtk.PopoverMenuBar.new_from_model(menubar_model)
        return menubar

    def _populate_target_menu(self) -> None:
        """Populate the Target menu with Platform Architecture choices."""
        if not hasattr(self, "target_menu") or self.target_menu is None:
            return
        self.target_menu.remove_all()

        plat_sec = Gio.Menu.new()
        plat_choices = [
            ("mtl", "MTL / ARL — ACE 1.x (platform/intel/mtl.conf)"),
            ("lnl", "LNL — ACE 2.0 (platform/intel/lnl.conf)"),
            ("ptl", "PTL — ACE 3.0 (platform/intel/ptl.conf)"),
            ("nvl", "NVL — ACE 4.0 (platform/intel/ptl.conf)"),
            ("tgl", "TGL — CAVS 2.5 (platform/intel/tgl.conf)"),
            ("adl", "ADL / RPL — CAVS 2.5 (platform/intel/tgl.conf)"),
        ]
        for pid, plabel in plat_choices:
            self._append_str_action_item(plat_sec, plabel, "win.select_target_platform", pid)
        self.target_menu.append_section(None, plat_sec)

    def action_select_target_platform(self, platform_id: str) -> None:
        """Select the target SoC platform definition (tgl, adl, mtl, lnl, ptl, nvl) for testbench compilation."""
        plat = platform_id.strip().lower()
        debug_log_action("TARGET-PLATFORM", f"Select target platform '{plat}'")
        self.target_platform = plat
        plat_conf_map = {
            "tgl": "platform/intel/tgl.conf",
            "adl": "platform/intel/tgl.conf",
            "mtl": "platform/intel/mtl.conf",
            "lnl": "platform/intel/lnl.conf",
            "ptl": "platform/intel/ptl.conf",
            "nvl": "platform/intel/ptl.conf",
        }
        if plat in plat_conf_map:
            self.target_platform_def_path = plat_conf_map[plat]
            if hasattr(self, "target_platform_def_action"):
                self.target_platform_def_action.set_state(
                    GLib.Variant.new_string(self.target_platform_def_path)
                )
        if self.compiler:
            self.compiler.target_platform = plat
            self.compiler.extra_platform_conf = self.target_platform_def_path
        if self.tb_runner:
            self.tb_runner.platform_preset = plat
            if self.tb_runner.live_client:
                tb_preset = TestbenchRunner.map_platform_to_tb_preset(plat)
                st, out = self.tb_runner.live_client.exec_shell(f"platform-cfg preset {tb_preset}")
                self.append_log(f"[SOFR] $ platform-cfg preset {tb_preset} (status={st})\n{out.strip()}")
                self._on_refresh_telemetry_clicked(None)

        # Update PLATFORM variable in the active tab's GraphViewModel if present
        active = self.get_active_tab()
        if active is not None and active.canvas.vm is not None:
            vm = active.canvas.vm
            vm.defines["PLATFORM"] = plat
            def_node = vm.root_config.get_child("Define (Active Variables)")
            if def_node is not None:
                def_node.set_scalar("PLATFORM", plat, category_info="Define", origin="instance")
            # Merge SoC platform defines (e.g. DMIC_DRIVER_VERSION, SSP_BLOB_VERSION) into vm.defines
            if self.workspace:
                for pdef in self.workspace.platform_definitions:
                    if pdef.get("rel_path") == self.target_platform_def_path:
                        p_defs = pdef.get("defines", {})
                        if isinstance(p_defs, dict):
                            for k, v in p_defs.items():
                                vm.defines[k] = str(v)
                                if def_node is not None:
                                    def_node.set_scalar(k, str(v), category_info="Platform Define", origin="define")
                        break
            self._sync_ui_to_tab(active)

        msg = (
            f"Target Platform set to PLATFORM={plat.upper()} "
            f"({self.target_platform_def_path})"
        )
        self.append_log(msg)
        self.status_lbl.set_text(msg)

    def action_select_target_harness(self, harness_id: str) -> None:
        """Select the testbench harness configuration used when wrapping standalone modules/pipelines."""
        self.target_harness_kind = harness_id
        if self.compiler:
            self.compiler.target_harness = harness_id
        harness_names = {
            "hda_analog": "HDA Analog Duplex",
            "hda_generic": "HDA Generic + DMIC / HDMI",
            "sdw_alh": "SoundWire ALH Jack Duplex",
            "ssp_nocodec": "NoCodec SSP0 I2S Duplex",
            "dmic_capture": "PDM DMIC Capture",
            "hdmi_playback": "HDMI Display Audio",
            "bt_ssp": "Bluetooth Sideband SSP",
            "deep_buffer": "Deep Buffer Playback",
        }
        h_label = harness_names.get(harness_id, harness_id)
        msg = f"Testbench Harness Config set to: {h_label} [{harness_id}] (PLATFORM={self.target_platform})"
        self.append_log(msg)
        self.status_lbl.set_text(msg)

    def action_select_platform_def(self, rel_path: str) -> None:
        """Select a platform definition file from platform/intel/*.conf and apply its target config."""
        self.target_platform_def_path = rel_path
        if self.compiler:
            self.compiler.extra_platform_conf = rel_path

        stem = os.path.splitext(os.path.basename(rel_path))[0]
        if stem in ("tgl", "mtl", "lnl", "ptl"):
            self.target_platform = stem
            if self.compiler:
                self.compiler.target_platform = stem
            if hasattr(self, "target_platform_action"):
                self.target_platform_action.set_state(GLib.Variant.new_string(stem))

        applied_defs: List[str] = []
        has_objs = False
        abs_p = ""
        if self.workspace:
            abs_p = os.path.join(self.workspace.root_dir, rel_path)
            for pdef in self.workspace.platform_definitions:
                if pdef.get("rel_path") == rel_path:
                    has_objs = bool(pdef.get("has_objects", False))
                    p_defs = pdef.get("defines", {})
                    active = self.get_active_tab()
                    if isinstance(p_defs, dict) and p_defs:
                        for k, v in p_defs.items():
                            applied_defs.append(f"{k}={v}")
                            if active and active.canvas.vm:
                                active.canvas.vm.defines[k] = str(v)
                                def_node = active.canvas.vm.root_config.get_child("Define (Active Variables)")
                                if def_node is not None:
                                    def_node.set_scalar(
                                        k,
                                        str(v),
                                        category_info=f"From {rel_path}",
                                        origin="instance",
                                    )
                    break

        active = self.get_active_tab()
        if active is not None:
            self._sync_ui_to_tab(active)

        defs_preview = f" ({', '.join(applied_defs[:4])}{'…' if len(applied_defs) > 4 else ''})" if applied_defs else ""
        msg = f"Selected Platform Definition: {rel_path}{defs_preview}"
        self.append_log(msg)
        self.status_lbl.set_text(msg)

    def action_open_active_platform_def(self) -> Optional[GraphTabPage]:
        """Open the currently selected platform/intel/*.conf definition file in a Main View tab."""
        if not self.workspace or not self.target_platform_def_path:
            return None
        abs_p = os.path.join(self.workspace.root_dir, self.target_platform_def_path)
        if os.path.isfile(abs_p):
            tab = self.open_conf_file_in_tab(abs_p)
            # If the platform file is a pure Define file (0 nodes), switch it to Text Editor view automatically
            if tab is not None and tab.canvas.vm is not None and len(tab.canvas.vm.nodes) == 0:
                tab.mode_switch_btn.set_active(True)
            return tab
        return None

    def action_select_target_variant(self, output_name: str) -> None:
        """Select a platform topology target configuration (CMakeTargetVariant) and open/configure it for testbench."""
        if not self.workspace:
            return
        self.target_variant_name = output_name
        for entry in self.workspace.machine_topologies:
            for var in entry.variants:
                if var.output_name == output_name:
                    plat = var.defines.get("PLATFORM", "")
                    if plat:
                        self.target_platform = plat
                        if self.compiler:
                            self.compiler.target_platform = plat
                        if hasattr(self, "target_platform_action"):
                            self.target_platform_action.set_state(GLib.Variant.new_string(plat))
                    self._on_left_pane_item_selected("topology", entry.conf_path, var.output_name)
                    msg = (
                        f"Selected Target Config: {var.output_name} "
                        f"({entry.name}.conf  [{var.defines_str or 'default'}])"
                    )
                    self.append_log(msg)
                    self.status_lbl.set_text(msg)
                    return

    def _build_header_bar(self) -> None:
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)
        self.set_titlebar(header)

        # Left side: Unified Menubar (File, Edit, View)
        self.menubar = self._build_menubar()
        header.pack_start(self.menubar)

        # Right header controls: Testbench Audio Config + Compile + Run in Testbench
        self.play_out_btn = Gtk.Button(label="Play Out WAV")
        self.play_out_btn.set_sensitive(False)
        self.play_out_btn.set_tooltip_text("Play processed testbench output WAV via aplay")
        self.play_out_btn.connect("clicked", self._on_play_output_clicked)
        header.pack_end(self.play_out_btn)

        self.keep_alive_btn = Gtk.ToggleButton(label="Keep Server (-W)")
        self.keep_alive_btn.set_tooltip_text(
            "Keep sof-testbench4 SOFR TCP server running after run for interactive control & telemetry"
        )
        header.pack_end(self.keep_alive_btn)

        self.run_tb_btn = Gtk.Button(label="▶ Run in Testbench")
        self.run_tb_btn.add_css_class("suggested-action")
        self.run_tb_btn.set_tooltip_text(
            "Compile topology and run sof-testbench4 configured & monitored via SOFR network protocol"
        )
        self.run_tb_btn.connect("clicked", self._on_run_testbench_clicked)
        header.pack_end(self.run_tb_btn)

        self.compile_btn = Gtk.Button(label="⚙ Compile (.tplg)")
        self.compile_btn.set_tooltip_text("Compile current topology/pipeline with alsatplg -p")
        self.compile_btn.connect("clicked", self._on_compile_clicked)
        header.pack_end(self.compile_btn)

    def _build_main_3panel_layout(self) -> None:
        # Outer horizontal paned: [Left Panel (25%)] | [Center (50%) + Right (25%) Paned]
        self.hpaned_outer = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.hpaned_outer.set_wide_handle(True)
        self.hpaned_outer.set_resize_start_child(False)
        self.hpaned_outer.set_resize_end_child(True)
        self.hpaned_outer.set_position(390)

        # Inner horizontal paned: [Middle Graph + Waveforms Panel (50%)] | [Right Notebook (25%)]
        self.hpaned_inner = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.hpaned_inner.set_wide_handle(True)
        self.hpaned_inner.set_resize_start_child(True)
        self.hpaned_inner.set_resize_end_child(False)
        self.hpaned_inner.set_position(780)

        # 1. Left Hand Panel (Object View, File View & Compile notebook) wrapped in high-level panel border
        self.left_panel = LeftNavigationPanel(
            on_item_selected=self._on_left_pane_item_selected,
            on_add_module_requested=self._on_add_module_requested,
            on_add_pipeline_requested=self._on_add_pipeline_requested,
        )
        left_panel_frame = Gtk.Frame()
        left_panel_frame.add_css_class("high-level-panel")
        left_panel_frame.set_margin_start(4)
        left_panel_frame.set_margin_end(2)
        left_panel_frame.set_margin_top(4)
        left_panel_frame.set_margin_bottom(4)
        left_panel_frame.set_child(self.left_panel)

        # 2. Middle Horizontal Panel:
        #    Vertical Paned: Top = Multi-Tab Graph Notebook, Bottom = Real-Time / Zoomable Waveforms Pane + Status Bar
        center_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        center_box.set_vexpand(True)
        center_box.set_hexpand(True)

        self.center_vpaned = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        self.center_vpaned.set_wide_handle(True)
        self.center_vpaned.set_vexpand(True)
        self.center_vpaned.set_hexpand(True)
        self.center_vpaned.set_resize_start_child(True)
        self.center_vpaned.set_resize_end_child(False)
        self.center_vpaned.set_position(580)

        self.graph_notebook = Gtk.Notebook()
        self.graph_notebook.set_scrollable(True)
        self.graph_notebook.set_vexpand(True)
        self.graph_notebook.set_hexpand(True)
        self.graph_notebook.set_show_border(False)
        self.graph_notebook.connect("switch-page", self._on_graph_notebook_switch_page)

        canvas_frame = Gtk.Frame()
        canvas_frame.add_css_class("sub-panel-frame")
        canvas_frame.set_vexpand(True)
        canvas_frame.set_hexpand(True)
        canvas_frame.set_child(self.graph_notebook)
        self.center_vpaned.set_start_child(canvas_frame)

        # Bottom Waveforms Pane (below the Main Graph Pane)
        wf_pane_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        wf_pane_box.set_margin_start(4)
        wf_pane_box.set_margin_end(4)
        wf_pane_box.set_margin_top(3)
        wf_pane_box.set_margin_bottom(3)

        wf_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.wf_status_lbl = Gtk.Label(label="Waveforms [IDLE]", xalign=0.0)
        self.wf_status_lbl.add_css_class("heading")
        self.wf_status_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.wf_status_lbl.set_hexpand(True)
        wf_toolbar.append(self.wf_status_lbl)

        self.rt_waveform_btn = Gtk.ToggleButton(label="⚡ Real-Time")
        self.rt_waveform_btn.set_tooltip_text(
            "Stream live Input & Output PCM waveforms in real time from sof-testbench4 (-W)"
        )
        self.rt_waveform_btn.connect("toggled", self._on_rt_waveform_toggled)
        wf_toolbar.append(self.rt_waveform_btn)

        self.wf_trigger_btn = Gtk.ToggleButton(label="🎯 Trigger")
        self.wf_trigger_btn.set_active(True)
        self.wf_trigger_btn.set_tooltip_text(
            "Lock real-time waveform to rising zero-crossing (uncheck for free-running scroll)"
        )
        self.wf_trigger_btn.connect("toggled", self._on_wf_trigger_toggled)
        wf_toolbar.append(self.wf_trigger_btn)

        self.wf_zoom_lbl = Gtk.Label(label="Time: 1× (12.50 ms / 600 smp)  |  Amp: 1×")
        self.wf_zoom_lbl.add_css_class("monospace")
        wf_toolbar.append(self.wf_zoom_lbl)

        btn_zout = Gtk.Button(label="🔍−")
        btn_zout.set_tooltip_text("Zoom Out time axis (or Mouse Wheel Down on waveform)")
        btn_zout.connect("clicked", lambda _b: self.waveform_widget.zoom_out_time())
        wf_toolbar.append(btn_zout)

        btn_zin = Gtk.Button(label="🔍+")
        btn_zin.set_tooltip_text("Zoom In time axis (or Mouse Wheel Up on waveform)")
        btn_zin.connect("clicked", lambda _b: self.waveform_widget.zoom_in_time())
        wf_toolbar.append(btn_zin)

        btn_zfit = Gtk.Button(label="1:1 Fit")
        btn_zfit.set_tooltip_text("Reset time & amplitude zoom and pan (or Double-Click waveform)")
        btn_zfit.connect("clicked", lambda _b: self.waveform_widget.reset_zoom())
        wf_toolbar.append(btn_zfit)

        btn_aout = Gtk.Button(label="Amp −")
        btn_aout.set_tooltip_text("Decrease vertical amplitude magnification (Ctrl + Wheel Down)")
        btn_aout.connect("clicked", lambda _b: self.waveform_widget.zoom_out_amp())
        wf_toolbar.append(btn_aout)

        btn_ain = Gtk.Button(label="Amp +")
        btn_ain.set_tooltip_text("Increase vertical amplitude magnification (Ctrl + Wheel Up)")
        btn_ain.connect("clicked", lambda _b: self.waveform_widget.zoom_in_amp())
        wf_toolbar.append(btn_ain)

        wf_pane_box.append(wf_toolbar)

        wf_body_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        wf_body_box.set_hexpand(True)
        wf_body_box.set_vexpand(True)

        self.waveform_widget = WaveformSnapshotWidget(on_zoom_changed=self._on_waveform_zoom_changed)
        self.waveform_widget.set_size_request(-1, 185)
        wf_body_box.append(self.waveform_widget)

        self.wf_vscrollbar = Gtk.Scrollbar.new(Gtk.Orientation.VERTICAL, self.waveform_widget.v_adj)
        self.wf_vscrollbar.set_vexpand(True)
        self.wf_vscrollbar.set_tooltip_text(
            "Scroll vertically through stacked PCM stream waveforms (or use Mouse Wheel)"
        )
        wf_body_box.append(self.wf_vscrollbar)

        wf_pane_box.append(wf_body_box)

        wf_frame = Gtk.Frame()
        wf_frame.add_css_class("sub-panel-frame")
        wf_frame.set_child(wf_pane_box)
        self.center_vpaned.set_end_child(wf_frame)

        center_box.append(self.center_vpaned)

        self.status_lbl = Gtk.Label(label="Ready.")
        self.status_lbl.set_xalign(0.0)
        self.status_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_lbl.set_margin_start(6)
        self.status_lbl.set_margin_end(6)
        self.status_lbl.set_margin_top(2)
        self.status_lbl.set_margin_bottom(4)
        center_box.append(self.status_lbl)

        center_panel_frame = Gtk.Frame()
        center_panel_frame.add_css_class("high-level-panel")
        center_panel_frame.set_margin_start(2)
        center_panel_frame.set_margin_end(2)
        center_panel_frame.set_margin_top(4)
        center_panel_frame.set_margin_bottom(4)
        center_panel_frame.set_child(center_box)

        # Compile Pane (attached as a page on the Left Panel's Object View / File View notebook)
        compile_pane_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        compile_pane_box.set_margin_start(4)
        compile_pane_box.set_margin_end(4)
        compile_pane_box.set_margin_top(4)
        compile_pane_box.set_margin_bottom(4)

        compile_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.compile_pane_status_lbl = Gtk.Label(
            label="alsatplg Compiler Output (Dbl-Click error/warning)",
            xalign=0.0,
        )
        self.compile_pane_status_lbl.set_hexpand(True)
        self.compile_pane_status_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.compile_pane_status_lbl.add_css_class("heading")
        compile_hdr.append(self.compile_pane_status_lbl)

        clear_log_btn = Gtk.Button(label="Clear")
        clear_log_btn.set_tooltip_text("Clear compile & protocol log")
        clear_log_btn.connect("clicked", self._on_clear_compile_log_clicked)
        compile_hdr.append(clear_log_btn)
        compile_pane_box.append(compile_hdr)

        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_vexpand(True)
        log_scroll.set_hexpand(True)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_tooltip_text(
            "Double-click any red error or yellow warning line to focus the failing part in the Main Graph Viewer and Attribute Editor"
        )
        self._init_log_text_tags()

        log_click = Gtk.GestureClick.new()
        log_click.set_button(1)
        log_click.connect("pressed", self._on_log_view_pressed)
        self.log_view.add_controller(log_click)

        log_scroll.set_child(self.log_view)
        compile_pane_box.append(log_scroll)
        self.left_panel.attach_compile_pane(compile_pane_box)

        # 3. Right Hand Panel Notebook:
        #    Page 0 = Attributes (RightAttributeInspector)
        #    Page 1 = SOFR Telemetry (Full-height live DSP telemetry report)
        #    Page 2 = Remote Control (DSP Control, Kcontrols, Remote Shell)
        self.right_notebook = Gtk.Notebook()
        self.right_notebook.set_vexpand(True)
        self.right_notebook.set_hexpand(True)

        # Right Notebook Tab 1: Attributes
        self.right_inspector = RightAttributeInspector(
            on_attribute_edited=self._on_node_attribute_edited,
            on_before_attribute_edit=self._on_before_attribute_edit,
        )
        self.right_notebook.append_page(self.right_inspector, Gtk.Label(label="Attributes"))

        # Right Notebook Tab 2: SOFR Telemetry
        telem_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        telem_box.set_margin_start(4)
        telem_box.set_margin_end(4)
        telem_box.set_margin_top(4)
        telem_box.set_margin_bottom(4)

        self.telem_summary_lbl = Gtk.Label(
            label="SOFR Telemetry: Run Testbench to query DSP cores, KCPS, SRAM banks, heap, DAPM & kcontrols."
        )
        self.telem_summary_lbl.set_xalign(0.0)
        self.telem_summary_lbl.set_wrap(True)
        self.telem_summary_lbl.add_css_class("heading")
        telem_box.append(self.telem_summary_lbl)

        telem_scroll = Gtk.ScrolledWindow()
        telem_scroll.set_vexpand(True)
        telem_scroll.set_hexpand(True)
        self.telem_view = Gtk.TextView()
        self.telem_view.set_editable(False)
        self.telem_view.set_monospace(True)
        self.telem_view.set_wrap_mode(Gtk.WrapMode.NONE)
        telem_scroll.set_child(self.telem_view)
        telem_box.append(telem_scroll)

        self.right_notebook.append_page(telem_box, Gtk.Label(label="SOFR Telemetry"))

        # Right Notebook Tab 3: Remote Control & Kcontrols (SOFR Protocol)
        ctl_scroll = Gtk.ScrolledWindow()
        ctl_scroll.set_vexpand(True)
        ctl_scroll.set_hexpand(True)
        ctl_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        ctl_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        ctl_box.set_margin_start(6)
        ctl_box.set_margin_end(6)
        ctl_box.set_margin_top(6)
        ctl_box.set_margin_bottom(6)

        # Section 1: DSP Power, Execution, Tone, Hot-Reload & Telemetry Poll Controls
        dsp_hdr = Gtk.Label(label="DSP Power, Tone & Execution Control", xalign=0.0)
        dsp_hdr.add_css_class("heading")
        ctl_box.append(dsp_hdr)

        exec_row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        btn_wake = Gtk.Button(label="▶ Resume (D0)")
        btn_wake.set_hexpand(True)
        btn_wake.set_tooltip_text("SOF_NET_OP_PM_CTL: SOF_PM_OP_WAKE_HOLD")
        btn_wake.connect("clicked", lambda _b: self._on_remote_pm_ctl(SOF_PM_OP_WAKE_HOLD))
        exec_row1.append(btn_wake)

        btn_pause = Gtk.Button(label="⏸ Pause (D3)")
        btn_pause.set_hexpand(True)
        btn_pause.set_tooltip_text("SOF_NET_OP_PM_CTL: SOF_PM_OP_RELEASE_HOLD")
        btn_pause.connect("clicked", lambda _b: self._on_remote_pm_ctl(SOF_PM_OP_RELEASE_HOLD))
        exec_row1.append(btn_pause)

        btn_step = Gtk.Button(label="⏭ Step 10")
        btn_step.set_hexpand(True)
        btn_step.set_tooltip_text("SOF_NET_OP_SHELL_EXEC: step 10")
        btn_step.connect("clicked", lambda _b: self._on_remote_shell_preset("step 10"))
        exec_row1.append(btn_step)

        self.hot_reload_btn = Gtk.ToggleButton(label="🔥 Hot-Reload")
        self.hot_reload_btn.set_active(False)
        self.hot_reload_btn.set_tooltip_text(
            "When Keep Server (-W) is active, hot-reload compiled topologies via 'tplg-load' & 'io-cfg' without restarting sof-testbench4"
        )
        exec_row1.append(self.hot_reload_btn)
        ctl_box.append(exec_row1)

        exec_row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.remote_tone_btn = Gtk.ToggleButton(label="🎵 Tone")
        self.remote_tone_btn.set_tooltip_text("SOF_NET_OP_PCM_CTL: START_TONE / STOP_TONE")
        self.remote_tone_btn.connect("toggled", self._on_remote_tone_toggled)
        exec_row2.append(self.remote_tone_btn)

        self.remote_tone_spin = Gtk.SpinButton.new_with_range(20, 20000, 100)
        self.remote_tone_spin.set_value(1000)
        self.remote_tone_spin.set_tooltip_text("Tone generator frequency (Hz) for START_TONE")
        self.remote_tone_spin.connect("value-changed", self._on_remote_tone_freq_changed)
        exec_row2.append(self.remote_tone_spin)

        btn_poll = Gtk.Button(label="🔄 Refresh")
        btn_poll.set_hexpand(True)
        btn_poll.set_tooltip_text("Poll SOF_NET_OP_TELEM_SNAPSHOT, OP_WORKLOAD_SIM_CFG & SOF_PCM_OP_CAPTURE_BUFFER")
        btn_poll.connect("clicked", self._on_refresh_telemetry_clicked)
        exec_row2.append(btn_poll)

        btn_stop_srv = Gtk.Button(label="⏹ Stop")
        btn_stop_srv.set_tooltip_text("Send SOF_NET_OP_SHELL_EXEC 'quit' to stop live testbench server")
        btn_stop_srv.connect("clicked", self._on_stop_live_server_clicked)
        exec_row2.append(btn_stop_srv)
        ctl_box.append(exec_row2)

        # Section 2: Platform & Workload Simulation (SOF_NET_OP_WORKLOAD_SIM_CFG)
        wsim_hdr = Gtk.Label(label="Workload Simulation & Core Gating (OP_WORKLOAD_SIM_CFG)", xalign=0.0)
        wsim_hdr.add_css_class("heading")
        ctl_box.append(wsim_hdr)

        wsim_row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        wsim_row1.append(Gtk.Label(label="Sim:"))
        self.wsim_mode_combo = Gtk.DropDown.new_from_strings(
            ["Off (Host Measured)", "Virtual CPC Sim", "Real-Time Paced"]
        )
        self.wsim_mode_combo.set_selected(0)
        self.wsim_mode_combo.set_hexpand(True)
        self.wsim_mode_combo.set_tooltip_text("Workload simulation mode (-w off|virtual|realtime / OP_WORKLOAD_SIM_CFG)")
        self.wsim_mode_combo.connect("notify::selected", self._on_wsim_config_changed)
        wsim_row1.append(self.wsim_mode_combo)

        wsim_row1.append(Gtk.Label(label="Mem:"))
        self.wsim_mem_combo = Gtk.DropDown.new_from_strings(
            ["SRAM (1.0x)", "DRAM (1.6x+2c)"]
        )
        self.wsim_mem_combo.set_selected(0)
        self.wsim_mem_combo.set_tooltip_text("Default memory domain penalty (-M sram|dram / OP_WORKLOAD_SIM_CFG)")
        self.wsim_mem_combo.connect("notify::selected", self._on_wsim_config_changed)
        wsim_row1.append(self.wsim_mem_combo)
        ctl_box.append(wsim_row1)

        wsim_row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        wsim_row2.append(Gtk.Label(label="MHz:"))
        self.wsim_freq_spin = Gtk.SpinButton.new_with_range(0, 2400, 50)
        self.wsim_freq_spin.set_value(400)
        self.wsim_freq_spin.set_tooltip_text("Simulated DSP core clock frequency in MHz (0 = platform default)")
        self.wsim_freq_spin.connect("value-changed", self._on_wsim_config_changed)
        wsim_row2.append(self.wsim_freq_spin)

        self.core_toggle_btns: List[Gtk.ToggleButton] = []
        for cid in range(4):
            cbtn = Gtk.ToggleButton(label=f"C{cid}")
            cbtn.set_active(cid == 0)
            cbtn.set_tooltip_text(f"Enable/disable DSP Core {cid} via SOF_PM_OP_CORE_GET / CORE_PUT")
            cbtn.connect("toggled", lambda b, idx=cid: self._on_core_toggle_clicked(idx, b))
            self.core_toggle_btns.append(cbtn)
            wsim_row2.append(cbtn)

        btn_wsim_rst = Gtk.Button(label="↺ Reset Sim")
        btn_wsim_rst.set_tooltip_text("Reset workload simulation counters & scheduler telemetry (OP_WORKLOAD_SIM_CFG RESET)")
        btn_wsim_rst.connect("clicked", self._on_reset_wsim_clicked)
        wsim_row2.append(btn_wsim_rst)
        ctl_box.append(wsim_row2)

        # Section 3: Pipeline State, LLEXT Loader, Trace Level & SRAM Window Inspector
        adv_hdr = Gtk.Label(label="Pipeline State, LLEXT & SRAM Windows", xalign=0.0)
        adv_hdr.add_css_class("heading")
        ctl_box.append(adv_hdr)

        ppl_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        ppl_row.append(Gtk.Label(label="Pipe:"))
        self.remote_pipe_spin = Gtk.SpinButton.new_with_range(1, 32, 1)
        self.remote_pipe_spin.set_value(1)
        self.remote_pipe_spin.set_tooltip_text("Target pipeline ID for ppl-state control")
        ppl_row.append(self.remote_pipe_spin)

        for p_lbl, p_st in (("▶ Run", "running"), ("⏸ Pause", "paused"), ("⏹ Reset", "reset")):
            b_ps = Gtk.Button(label=p_lbl)
            b_ps.set_hexpand(True)
            b_ps.set_tooltip_text(f"Set pipeline state to '{p_st}' via 'ppl-state <id> {p_st}'")
            b_ps.connect("clicked", lambda _b, s=p_st: self._on_remote_ppl_state_clicked(s))
            ppl_row.append(b_ps)
        ctl_box.append(ppl_row)

        llext_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        btn_llext = Gtk.Button(label="📦 Load LLEXT (.so)…")
        btn_llext.set_hexpand(True)
        btn_llext.set_tooltip_text("Dynamically load a shared library module via SOF_NET_OP_LLEXT_LOAD (0x0030)")
        btn_llext.connect("clicked", self._on_load_llext_clicked)
        llext_row.append(btn_llext)

        llext_row.append(Gtk.Label(label="Log:"))
        self.trace_lvl_combo = Gtk.DropDown.new_from_strings(
            ["0: Off", "1: Err", "2: Warn", "3: Info", "4: Dbg"]
        )
        self.trace_lvl_combo.set_selected(2)
        self.trace_lvl_combo.set_tooltip_text("Set testbench trace log level (-d / 'loglevel 0..4')")
        self.trace_lvl_combo.connect("notify::selected", self._on_trace_level_changed)
        llext_row.append(self.trace_lvl_combo)
        ctl_box.append(llext_row)

        win_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        win_row.append(Gtk.Label(label="SRAM:"))
        for w_lbl, w_id in (
            ("Win0 FW_REGS", SOF_CDEV_WIN_FW_REGS),
            ("Win2 TELEM", SOF_CDEV_WIN_DEBUG),
            ("Win3 PERF", SOF_CDEV_WIN_PERF),
        ):
            b_win = Gtk.Button(label=w_lbl)
            b_win.set_hexpand(True)
            b_win.set_tooltip_text(f"Read raw 256-byte SRAM Window {w_id} via SOF_NET_OP_WIN_READ (0x0021)")
            b_win.connect("clicked", lambda _b, wid=w_id, wname=w_lbl: self._on_read_sram_window_clicked(wid, wname))
            win_row.append(b_win)
        ctl_box.append(win_row)

        # Section 4: Remote Shell Command Execution (SOF_NET_OP_SHELL_EXEC)
        sh_hdr = Gtk.Label(label="Remote Testbench Shell (OP_SHELL_EXEC)", xalign=0.0)
        sh_hdr.add_css_class("heading")
        ctl_box.append(sh_hdr)

        sh_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.remote_shell_entry = Gtk.Entry()
        self.remote_shell_entry.set_placeholder_text("info, pipelines, controls, stats, platform-cfg, workload-sim…")
        self.remote_shell_entry.set_hexpand(True)
        self.remote_shell_entry.connect("activate", lambda _e: self._on_run_remote_shell_clicked(None))
        sh_row.append(self.remote_shell_entry)

        btn_sh_run = Gtk.Button(label="Send")
        btn_sh_run.connect("clicked", self._on_run_remote_shell_clicked)
        sh_row.append(btn_sh_run)
        ctl_box.append(sh_row)

        sh_presets_row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for sh_cmd in ("info", "pipelines", "stats", "kernel cycles", "kernel threads"):
            b_pre = Gtk.Button(label=sh_cmd)
            b_pre.connect("clicked", lambda _b, c=sh_cmd: self._on_remote_shell_preset(c))
            sh_presets_row1.append(b_pre)
        ctl_box.append(sh_presets_row1)

        sh_presets_row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for sh_cmd in ("platform-cfg status", "workload-sim status", "sched-telem", "mod-buffers"):
            b_pre2 = Gtk.Button(label=sh_cmd)
            b_pre2.connect("clicked", lambda _b, c=sh_cmd: self._on_remote_shell_preset(c))
            sh_presets_row2.append(b_pre2)
        ctl_box.append(sh_presets_row2)

        ctl_scroll.set_child(ctl_box)
        self.right_notebook.append_page(ctl_scroll, Gtk.Label(label="Remote Control"))

        right_panel_frame = Gtk.Frame()
        right_panel_frame.add_css_class("high-level-panel")
        right_panel_frame.set_margin_start(2)
        right_panel_frame.set_margin_end(4)
        right_panel_frame.set_margin_top(4)
        right_panel_frame.set_margin_bottom(4)
        right_panel_frame.set_child(self.right_notebook)

        self.hpaned_inner.set_start_child(center_panel_frame)
        self.hpaned_inner.set_end_child(right_panel_frame)

        self.hpaned_outer.set_start_child(left_panel_frame)
        self.hpaned_outer.set_end_child(self.hpaned_inner)
        self.hpaned_outer.add_tick_callback(self._on_outer_paned_tick)

        self.set_child(self.hpaned_outer)

    def _on_outer_paned_tick(self, widget: Gtk.Widget, _frame_clock: object) -> bool:
        """Keep default horizontal split at 25% Left / 50% Main Graph / 25% Right when UI resizes."""
        total_w = widget.get_width()
        total_h = widget.get_height()
        if total_w > 100 and total_h > 100 and (
            total_w != self._last_allocated_width or total_h != self._last_allocated_height
        ):
            self._last_allocated_width = total_w
            self._last_allocated_height = total_h
            target_left = int(total_w * 0.25)
            target_center = int(total_w * 0.50)
            target_center_vpos = int(total_h * 0.68)
            GLib.idle_add(self._sync_inner_paned_position, target_left, target_center, target_center_vpos)
        return GLib.SOURCE_CONTINUE

    def _sync_inner_paned_position(self, target_left: int, target_center: int, target_center_vpos: int) -> bool:
        self.hpaned_outer.set_position(target_left)
        self.hpaned_inner.set_position(target_center)
        self.center_vpaned.set_position(target_center_vpos)
        self.canvas.queue_draw()
        return False

    # -------------------------------------------------------------------------
    # Workspace Loading & Left Pane Selection Handling
    # -------------------------------------------------------------------------

    def load_topology2_directory(self, path: str) -> None:
        abs_path = os.path.abspath(path)
        debug_log_action("LOAD-WORKSPACE", f"Load topology2 directory '{abs_path}'")
        self.append_log(f"Scanning Topology2 directory: {abs_path} …")
        ws = Topology2Workspace(abs_path)
        ws.scan_all()
        self.workspace = ws
        self.compiler = TopologyCompiler(abs_path)
        self.compiler.target_platform = self.target_platform
        self.compiler.target_harness = self.target_harness_kind
        self.compiler.extra_platform_conf = self.target_platform_def_path
        self.tb_runner = TestbenchRunner(abs_path)
        self.tb_runner.platform_preset = self.target_platform
        # Start sof-testbench4 in interactive server mode on startup WITHOUT loading any topology (-t),
        # and set the target platform preset.
        self.tb_runner.start_interactive_server(
            platform_preset=self.target_platform,
            log_callback=self.append_log,
        )
        self.left_panel.populate(ws)
        self._populate_target_menu()

        summary = (
            f"Loaded {len(ws.base_classes)} Base Classes, "
            f"{len(ws.processing_modules)} Processing Modules, "
            f"{len(ws.pipeline_classes)} Pipelines, "
            f"{len(ws.machine_topologies)} Machine Topologies."
        )
        self.status_lbl.set_text(summary)
        self.append_log(summary)

        # Select default topology (`sof-hda-generic` if present, else first pipeline)
        hda = next((m for m in ws.machine_topologies if m.name == "sof-hda-generic"), None)
        if hda:
            self._on_left_pane_item_selected("topology", hda.conf_path, "sof-hda-generic")
        elif ws.pipeline_classes:
            first_p = sorted(ws.pipeline_classes.keys())[0]
            self._on_left_pane_item_selected("pipeline", first_p, None)

    @staticmethod
    def _format_tab_title(vm: Optional[GraphViewModel], fallback: str = "Untitled.conf") -> str:
        if vm is None:
            return fallback
        icon_map = {
            "module": "🧩",
            "pipeline": "⛓",
            "function_topology": "🔀",
            "topology": "🎛",
            "base_class": "📦",
        }
        icon = icon_map.get(vm.view_kind, "📄")
        base = os.path.basename(vm.source_file) if vm.source_file else vm.title
        # Include variant suffix if viewing a specific topology variant
        if vm.view_kind == "topology" and "[" in vm.title and "]" in vm.title:
            var_part = vm.title[vm.title.find("["):]
            base = f"{base} {var_part}"
        dirty_mark = " •" if vm.dirty else ""
        return f"{icon} {base}{dirty_mark}"

    def _update_active_tab_header(self) -> None:
        tab = self.get_active_tab()
        if tab is None:
            return
        vm = tab.canvas.vm
        tab.title_lbl.set_text(self._format_tab_title(vm, tab.primary_id))
        if vm is not None:
            tip = f"{vm.title}\nFile: {vm.source_file or '(Unsaved)'}"
            tab.tab_box.set_tooltip_text(tip)

    def _open_or_switch_graph_tab(
        self,
        tab_key: str,
        pane_kind: str,
        primary_id: str,
        secondary_id: Optional[str],
        vm: GraphViewModel,
        is_untitled: bool = False,
    ) -> GraphTabPage:
        """Switch to an already-open tab if matching tab_key exists, or create and select a new tab."""
        for existing in self.graph_tabs:
            if existing.tab_key == tab_key:
                pnum = self.graph_notebook.page_num(existing.view_stack)
                if pnum >= 0:
                    self.graph_notebook.set_current_page(pnum)
                self._sync_ui_to_active_tab()
                return existing

        new_canvas = TopologyGraphCanvas(
            on_selection_changed=self._on_canvas_selection_changed,
            on_graph_modified=self._on_graph_modified,
            on_history_changed=self._update_undo_redo_buttons,
            on_kcontrol_changed=self._on_canvas_kcontrol_changed,
            on_probe_tap_toggled=self._on_canvas_probe_tap_toggled,
            on_testbench_io_changed=self._on_canvas_testbench_io_changed,
        )
        new_canvas.set_graph(vm, self.workspace)

        new_text_editor = TopologyTextEditor()

        view_stack = Gtk.Stack()
        view_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        view_stack.set_transition_duration(120)
        view_stack.set_hexpand(True)
        view_stack.set_vexpand(True)
        view_stack.add_named(new_canvas, "graph")
        view_stack.add_named(new_text_editor, "text")
        view_stack.set_visible_child_name("graph")

        tab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title_lbl = Gtk.Label(label=self._format_tab_title(vm, primary_id))
        tab_box.append(title_lbl)

        mode_switch_btn = Gtk.ToggleButton(label="</> Text")
        mode_switch_btn.add_css_class("flat")
        mode_switch_btn.add_css_class("caption")
        mode_switch_btn.set_tooltip_text(
            "Switch between Visual Graph Diagram and Colorized Topology2 (.conf) Text Editor (Ctrl+T)"
        )
        tab_box.append(mode_switch_btn)

        close_btn = Gtk.Button(label="✕")
        close_btn.add_css_class("flat")
        close_btn.set_tooltip_text("Close tab (Shortcut: Ctrl+W)")
        tab_box.append(close_btn)

        tab_page = GraphTabPage(
            tab_key=tab_key,
            pane_kind=pane_kind,
            primary_id=primary_id,
            secondary_id=secondary_id,
            canvas=new_canvas,
            text_editor=new_text_editor,
            view_stack=view_stack,
            tab_box=tab_box,
            title_lbl=title_lbl,
            mode_switch_btn=mode_switch_btn,
            is_untitled=is_untitled,
            text_needs_refresh_from_graph=True,
        )
        tab_box.set_tooltip_text(f"{vm.title}\nFile: {vm.source_file or '(Unsaved)'}")
        new_text_editor.on_text_committed = lambda txt, tp=tab_page: self._on_tab_text_committed(tp, txt)
        mode_switch_btn.connect("toggled", self._on_tab_mode_switch_toggled, tab_page)
        close_btn.connect("clicked", lambda _b, tp=tab_page: self._close_graph_tab(tp))

        pnum = self.graph_notebook.append_page(view_stack, tab_box)
        self.graph_notebook.set_tab_reorderable(view_stack, True)
        self.graph_tabs.append(tab_page)
        self.graph_notebook.set_current_page(pnum)
        debug_log_action(
            "TAB-OPEN",
            f"Opened tab '{vm.title}'",
            f"key={tab_key} kind={pane_kind} nodes={len(vm.nodes)} routes={len(vm.edges)}",
        )
        self._sync_ui_to_active_tab()
        return tab_page

    def _refresh_tab_text_from_graph(self, tab_page: GraphTabPage) -> None:
        """Populate tab_page.text_editor from its current GraphViewModel."""
        vm = tab_page.canvas.vm
        if vm is None:
            return
        root_dir = self.workspace.root_dir if self.workspace else None
        conf_text = get_editable_conf_text_for_vm(vm, root_dir)
        header = f"Topology2 (.conf) Text Editor — {vm.title}"
        if vm.source_file:
            header += f"  [{os.path.basename(vm.source_file)}]"
        tab_page.text_editor.set_text(conf_text, header_title=header)
        tab_page.text_needs_refresh_from_graph = False

    def _on_tab_mode_switch_toggled(
        self,
        btn: Gtk.ToggleButton,
        tab_page: GraphTabPage,
    ) -> None:
        """Switch a Main View notebook tab between Visual Graph ('graph') and Text Editor ('text')."""
        pnum = self.graph_notebook.page_num(tab_page.view_stack)
        if pnum >= 0 and self.graph_notebook.get_current_page() != pnum:
            self.graph_notebook.set_current_page(pnum)

        if btn.get_active():
            debug_log_action("TAB-MODE", f"Switch tab '{tab_page.primary_id}' to Text Editor mode")
            btn.set_label("🕸 Graph")
            btn.set_tooltip_text("Switch back to Visual Graph Diagram (Ctrl+T)")
            if tab_page.text_needs_refresh_from_graph or not tab_page.text_editor.get_text().strip():
                self._refresh_tab_text_from_graph(tab_page)
            tab_page.view_stack.set_visible_child_name("text")
            tab_page.text_editor.text_view.grab_focus()
            vm = tab_page.canvas.vm
            self.status_lbl.set_text(
                f"Text Editor mode: {vm.title if vm else tab_page.primary_id} "
                f"(Edit .conf text directly — changes sync to diagram)"
            )
        else:
            debug_log_action("TAB-MODE", f"Switch tab '{tab_page.primary_id}' to Visual Graph mode")
            btn.set_label("</> Text")
            btn.set_tooltip_text(
                "Switch between Visual Graph Diagram and Colorized Topology2 (.conf) Text Editor (Ctrl+T)"
            )
            if tab_page.text_editor.is_modified_in_text:
                tab_page.text_editor.commit_to_diagram()
            tab_page.view_stack.set_visible_child_name("graph")
            self._sync_ui_to_tab(tab_page)

    def toggle_active_tab_text_mode(self) -> bool:
        """Toggle the active tab between Visual Graph and Topology2 Text Editor."""
        active = self.get_active_tab()
        if active is None:
            return False
        active.mode_switch_btn.set_active(not active.mode_switch_btn.get_active())
        return active.mode_switch_btn.get_active()

    def _on_tab_text_committed(
        self,
        tab_page: GraphTabPage,
        new_text: str,
    ) -> Tuple[bool, str]:
        """Parse edited .conf text from TopologyTextEditor and update tab_page's diagram."""
        ws = self._ensure_workspace()
        old_vm = tab_page.canvas.vm
        source_file = old_vm.source_file if old_vm else ""
        fallback_kind = old_vm.view_kind if old_vm else tab_page.pane_kind
        fallback_title = old_vm.title if old_vm else tab_page.primary_id

        target_variant = None
        if tab_page.pane_kind == "topology" and tab_page.secondary_id and source_file:
            for entry in ws.machine_topologies:
                if os.path.abspath(entry.conf_path) == os.path.abspath(source_file):
                    target_variant = next(
                        (v for v in entry.variants if v.output_name == tab_page.secondary_id),
                        None,
                    )
                    break

        try:
            new_vm = ws.build_graph_from_conf_text(
                new_text,
                source_file=source_file,
                fallback_view_kind=fallback_kind,
                fallback_title=fallback_title,
                target_variant=target_variant,
            )
            layout_graph(new_vm)
            debug_log_action(
                "TEXT-COMMIT",
                f"Committed edited .conf text for '{fallback_title}'",
                f"chars={len(new_text)} nodes={len(new_vm.nodes)} routes={len(new_vm.edges)}",
            )
        except Exception as exc:
            debug_log_action("TEXT-COMMIT-ERROR", f"Parse error in '{fallback_title}'", str(exc))
            return False, str(exc)

        # Record undo snapshot on canvas before replacing view model
        if old_vm is not None:
            tab_page.canvas.record_undo_snapshot("Edit Topology Text")
            # Preserve existing node coordinates when node & edge topology structure is unchanged
            if (
                set(old_vm.nodes.keys()) == set(new_vm.nodes.keys())
                and len(old_vm.edges) == len(new_vm.edges)
            ):
                for nid, new_node in new_vm.nodes.items():
                    old_node = old_vm.nodes.get(nid)
                    if old_node is not None:
                        new_node.x = old_node.x
                        new_node.y = old_node.y
                for cid, new_cont in new_vm.containers.items():
                    old_cont = old_vm.containers.get(cid)
                    if old_cont is not None:
                        new_cont.x = old_cont.x
                        new_cont.y = old_cont.y
                        new_cont.width = old_cont.width
                        new_cont.height = old_cont.height

        new_vm.dirty = True
        tab_page.canvas.vm = new_vm
        if tab_page.canvas.selected_node_id and tab_page.canvas.selected_node_id not in new_vm.nodes:
            tab_page.canvas.selected_node_id = None
        if tab_page.canvas.selected_node_ids:
            tab_page.canvas.selected_node_ids = {
                nid for nid in tab_page.canvas.selected_node_ids if nid in new_vm.nodes
            }
        tab_page.text_needs_refresh_from_graph = False
        tab_page.canvas.queue_draw()
        self._sync_ui_to_tab(tab_page)
        return True, f"{len(new_vm.nodes)} nodes, {len(new_vm.edges)} routes"

    def _close_graph_tab(self, tab_page: GraphTabPage) -> bool:
        """Close the specified GraphTabPage in the Main Graph Notebook."""
        debug_log_action("TAB-CLOSE", f"Close tab '{tab_page.primary_id}'", f"key={tab_page.tab_key}")
        pnum = self.graph_notebook.page_num(tab_page.view_stack)
        if tab_page in self.graph_tabs:
            self.graph_tabs.remove(tab_page)
        if pnum >= 0:
            self.graph_notebook.remove_page(pnum)
        if not self.graph_tabs:
            self.right_inspector.set_node_config(None, None, "")
            self._update_undo_redo_buttons()
            self._update_waveform_stream_labels()
            self.status_lbl.set_text(
                "Closed tab. Use File ▸ New or select an item in the left panel to open a graph."
            )
        else:
            self._sync_ui_to_active_tab()
        return True

    def close_current_tab(self) -> bool:
        """Close the currently active graph tab."""
        active = self.get_active_tab()
        if active is None:
            return False
        return self._close_graph_tab(active)

    def _on_graph_notebook_switch_page(
        self,
        _notebook: Gtk.Notebook,
        page_widget: Gtk.Widget,
        _page_num: int,
    ) -> None:
        for tab in self.graph_tabs:
            if tab.view_stack is page_widget or tab.canvas is page_widget:
                self._sync_ui_to_tab(tab)
                break

    def _sync_ui_to_active_tab(self) -> None:
        active = self.get_active_tab()
        if active is not None:
            self._sync_ui_to_tab(active)

    def _sync_ui_to_tab(self, tab: GraphTabPage) -> None:
        vm = tab.canvas.vm
        self._pending_kctl_overrides.clear()
        self._pending_bytes_overrides.clear()
        self._update_undo_redo_buttons()
        self._update_active_tab_header()
        if vm is None:
            self.right_inspector.set_node_config(None, None, "")
            return
        # Restore right attribute inspector to the tab's selected node/container or root config
        sel_nid = tab.canvas.selected_node_id
        sel_cid = tab.canvas.selected_container_id
        if sel_nid and sel_nid in vm.nodes:
            node = vm.nodes[sel_nid]
            self.right_inspector.set_node_config(
                f"{node.title} ({node.class_name})",
                node.config,
                node.subtitle,
            )
        elif sel_cid is not None:
            cont = vm.containers.get(sel_cid)
            if cont and cont.config:
                self.right_inspector.set_node_config(
                    f"Pipeline {cont.pipeline_id} ({cont.class_name})",
                    cont.config,
                    f"Class.Pipeline.{cont.class_name} [{cont.direction}]",
                )
            else:
                self.right_inspector.set_node_config(vm.title, vm.root_config, vm.source_file)
        else:
            self.right_inspector.set_node_config(vm.title, vm.root_config, vm.source_file)

        self._update_waveform_stream_labels()
        self.status_lbl.set_text(
            f"Viewing {vm.title} ({len(vm.nodes)} nodes, {len(vm.edges)} routes)"
        )
        self._sync_selected_pipelines_to_testbench()

    def _on_left_pane_item_selected(
        self,
        pane_kind: str,
        primary_id: str,
        secondary_id: Optional[str],
    ) -> None:
        if not self.workspace:
            return
        debug_log_action(
            "LEFT-PANE-SELECT",
            f"Select {pane_kind} '{primary_id}'",
            f"secondary={secondary_id or ''}",
        )
        try:
            tab_key = f"{pane_kind}:{primary_id}:{secondary_id or ''}"
            # If already open in a tab, switch to it directly without rebuilding
            for existing in self.graph_tabs:
                if existing.tab_key == tab_key:
                    pnum = self.graph_notebook.page_num(existing.view_stack)
                    if pnum >= 0:
                        self.graph_notebook.set_current_page(pnum)
                    self._sync_ui_to_tab(existing)
                    return

            if pane_kind == "base_class":
                vm = self.workspace.build_graph_for_base_class(primary_id)
            elif pane_kind == "module":
                vm = self.workspace.build_graph_for_module(primary_id)
            elif pane_kind == "pipeline":
                vm = self.workspace.build_graph_for_pipeline(primary_id)
            elif pane_kind == "topology":
                variant = None
                if secondary_id:
                    for entry in self.workspace.machine_topologies:
                        if os.path.abspath(entry.conf_path) == os.path.abspath(primary_id):
                            variant = next((v for v in entry.variants if v.output_name == secondary_id), None)
                            break
                vm = self.workspace.build_graph_for_topology(primary_id, variant)
            else:
                return

            self._open_or_switch_graph_tab(
                tab_key=tab_key,
                pane_kind=pane_kind,
                primary_id=primary_id,
                secondary_id=secondary_id,
                vm=vm,
                is_untitled=False,
            )
        except Exception as exc:
            self.append_log(f"Error building graph for {primary_id}: {exc}")

    # -------------------------------------------------------------------------
    # File Menu Actions (New 4 Types, Open File, Save, Save As) & Edit Actions
    # -------------------------------------------------------------------------

    def _ensure_workspace(self) -> Topology2Workspace:
        if self.workspace is None:
            default_dir = "/home/lrg/work/sof-arl/sof-arl/tools/topology/topology2"
            ws = Topology2Workspace(default_dir if os.path.isdir(default_dir) else os.getcwd())
            if os.path.isdir(default_dir):
                ws.scan_all()
            self.workspace = ws
        return self.workspace

    def action_new_module(self, name: Optional[str] = None) -> GraphTabPage:
        """Create a new Module (Class.Widget) graph tab."""
        ws = self._ensure_workspace()
        self._new_file_counter += 1
        mod_name = name or f"custom_module_{self._new_file_counter}"
        vm = ws.create_new_module_graph(mod_name)
        tab_key = f"new:module:{self._new_file_counter}:{mod_name}"
        tab = self._open_or_switch_graph_tab(
            tab_key=tab_key,
            pane_kind="module",
            primary_id=mod_name,
            secondary_id=None,
            vm=vm,
            is_untitled=True,
        )
        self.status_lbl.set_text(f"Created new Module tab: {vm.title}")
        return tab

    def action_new_feature_topology(self, name: Optional[str] = None) -> GraphTabPage:
        """Create a new Feature Topology (Class.Pipeline) graph tab."""
        ws = self._ensure_workspace()
        self._new_file_counter += 1
        pipe_name = name or f"feature-pipeline-{self._new_file_counter}"
        vm = ws.create_new_feature_topology_graph(pipe_name)
        tab_key = f"new:pipeline:{self._new_file_counter}:{pipe_name}"
        tab = self._open_or_switch_graph_tab(
            tab_key=tab_key,
            pane_kind="pipeline",
            primary_id=pipe_name,
            secondary_id=None,
            vm=vm,
            is_untitled=True,
        )
        self.status_lbl.set_text(f"Created new Feature Topology (Pipeline) tab: {vm.title}")
        return tab

    def action_new_function_topology(self, name: Optional[str] = None) -> GraphTabPage:
        """Create a new Function Topology (Multi-Pipeline FE -> BE sub-graph) tab."""
        ws = self._ensure_workspace()
        self._new_file_counter += 1
        fn_name = name or f"function-topology-{self._new_file_counter}"
        vm = ws.create_new_function_topology_graph(fn_name)
        tab_key = f"new:function_topology:{self._new_file_counter}:{fn_name}"
        tab = self._open_or_switch_graph_tab(
            tab_key=tab_key,
            pane_kind="function_topology",
            primary_id=fn_name,
            secondary_id=None,
            vm=vm,
            is_untitled=True,
        )
        self.status_lbl.set_text(f"Created new Function Topology tab: {vm.title}")
        return tab

    def action_new_monolithic_topology(self, name: Optional[str] = None) -> GraphTabPage:
        """Create a new Monolithic Topology (sof-*.conf) graph tab."""
        ws = self._ensure_workspace()
        self._new_file_counter += 1
        topo_name = name or f"sof-custom-{self._new_file_counter}"
        vm = ws.create_new_monolithic_topology_graph(topo_name)
        tab_key = f"new:topology:{self._new_file_counter}:{topo_name}"
        tab = self._open_or_switch_graph_tab(
            tab_key=tab_key,
            pane_kind="topology",
            primary_id=topo_name,
            secondary_id=None,
            vm=vm,
            is_untitled=True,
        )
        self.status_lbl.set_text(f"Created new Monolithic Topology tab: {vm.title}")
        return tab

    def action_open_file(self) -> None:
        """Open a Gtk.FileDialog to open an existing .conf file in a new graph tab."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Open SOF Topology2 .conf File")
        if self.workspace and os.path.isdir(self.workspace.root_dir):
            dialog.set_initial_folder(Gio.File.new_for_path(self.workspace.root_dir))
        conf_filter = Gtk.FileFilter()
        conf_filter.set_name("ALSA Topology2 Config (*.conf)")
        conf_filter.add_pattern("*.conf")
        all_filter = Gtk.FileFilter()
        all_filter.set_name("All Files (*)")
        all_filter.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(conf_filter)
        filters.append(all_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(conf_filter)
        dialog.open(self, None, self._on_conf_file_selected)

    def _on_conf_file_selected(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            gfile = dialog.open_finish(result)
            if gfile:
                path = gfile.get_path()
                if path:
                    self.open_conf_file_in_tab(path)
        except Exception:
            pass

    def open_conf_file_in_tab(self, path: str) -> Optional[GraphTabPage]:
        """Parse and open an arbitrary .conf file into a new or existing graph tab."""
        ws = self._ensure_workspace()
        abs_path = os.path.abspath(path)
        if not os.path.isfile(abs_path):
            self.status_lbl.set_text(f"File not found: {abs_path}")
            return None
        try:
            vm = ws.build_graph_for_arbitrary_conf_file(abs_path)
            tab_key = f"file:{abs_path}"
            tab = self._open_or_switch_graph_tab(
                tab_key=tab_key,
                pane_kind=vm.view_kind,
                primary_id=abs_path,
                secondary_id=None,
                vm=vm,
                is_untitled=False,
            )
            self.status_lbl.set_text(f"Opened file: {abs_path}")
            return tab
        except Exception as exc:
            self.append_log(f"Error opening {abs_path}: {exc}")
            self.status_lbl.set_text(f"Error opening {os.path.basename(abs_path)}: {exc}")
            return None

    def save_active_tab_to_path(self, target_path: str) -> bool:
        """Serialize and save the active graph tab's view model (or active text buffer) to target_path."""
        active = self.get_active_tab()
        if active is None or active.canvas.vm is None:
            return False
        # If the user is currently in Text Editor mode or has unapplied text edits, commit to diagram first
        in_text_mode = active.view_stack.get_visible_child_name() == "text"
        if in_text_mode or active.text_editor.is_modified_in_text:
            active.text_editor.commit_to_diagram()
        vm = active.canvas.vm
        abs_path = os.path.abspath(target_path)
        try:
            parent_dir = os.path.dirname(abs_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            root_dir = self.workspace.root_dir if self.workspace else None
            if in_text_mode and not active.text_needs_refresh_from_graph and active.text_editor.get_text().strip():
                content = active.text_editor.get_text()
                if not content.endswith("\n"):
                    content += "\n"
            else:
                content = serialize_graph_for_save(vm, root_dir)
            with open(abs_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            vm.source_file = abs_path
            vm.dirty = False
            active.is_untitled = False
            active.primary_id = abs_path
            active.tab_key = f"file:{abs_path}"
            self._update_active_tab_header()
            self.append_log(f"Saved {vm.title} to {abs_path} ({len(content)} bytes)")
            self.status_lbl.set_text(f"Saved: {abs_path}")
            return True
        except Exception as exc:
            self.append_log(f"Error saving to {abs_path}: {exc}")
            self.status_lbl.set_text(f"Save failed: {exc}")
            return False

    def action_save(self) -> bool:
        """Save the active graph tab in-place, or prompt Save As if untitled."""
        active = self.get_active_tab()
        if active is None or active.canvas.vm is None:
            return False
        vm = active.canvas.vm
        if active.is_untitled or not vm.source_file:
            self.action_save_as()
            return False
        return self.save_active_tab_to_path(vm.source_file)

    def action_save_as(self) -> None:
        """Prompt user with Gtk.FileDialog to save the active graph tab to a chosen .conf path."""
        active = self.get_active_tab()
        if active is None or active.canvas.vm is None:
            return
        vm = active.canvas.vm
        dialog = Gtk.FileDialog.new()
        dialog.set_title(f"Save {vm.title} As…")

        default_name = os.path.basename(vm.source_file) if vm.source_file else "custom-topology.conf"
        dialog.set_initial_name(default_name)

        initial_dir = os.path.dirname(vm.source_file) if vm.source_file else ""
        if (not initial_dir or not os.path.isdir(initial_dir)) and self.workspace:
            initial_dir = self.workspace.root_dir
        if initial_dir and os.path.isdir(initial_dir):
            dialog.set_initial_folder(Gio.File.new_for_path(initial_dir))

        dialog.save(self, None, self._on_save_as_finished)

    def _on_save_as_finished(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            gfile = dialog.save_finish(result)
            if gfile:
                path = gfile.get_path()
                if path:
                    if not path.endswith(".conf"):
                        path += ".conf"
                    self.save_active_tab_to_path(path)
        except Exception:
            pass

    def export_active_tab_to_pdf(self, target_path: str) -> bool:
        """Render and save the active graph tab to a vector PDF file."""
        active = self.get_active_tab()
        if active is None or active.canvas.vm is None:
            return False
        if active.text_editor.is_modified_in_text:
            active.text_editor.commit_to_diagram()
        abs_path = os.path.abspath(target_path)
        try:
            ok = active.canvas.export_to_pdf(abs_path)
            if ok:
                size_kb = os.path.getsize(abs_path) / 1024.0
                self.append_log(f"Exported PDF diagram to {abs_path} ({size_kb:.1f} KB)")
                self.status_lbl.set_text(f"Exported PDF: {abs_path} ({size_kb:.1f} KB)")
                return True
            self.status_lbl.set_text(f"PDF export failed for {abs_path}")
            return False
        except Exception as exc:
            self.append_log(f"Error exporting PDF to {abs_path}: {exc}")
            self.status_lbl.set_text(f"PDF export error: {exc}")
            return False

    def action_export_pdf(self) -> None:
        """Prompt user with Gtk.FileDialog to export the active graph tab to a PDF file."""
        active = self.get_active_tab()
        if active is None or active.canvas.vm is None:
            return
        vm = active.canvas.vm
        dialog = Gtk.FileDialog.new()
        dialog.set_title(f"Export {vm.title} to PDF…")

        base_stem = (
            os.path.splitext(os.path.basename(vm.source_file))[0]
            if vm.source_file
            else vm.title.replace("Class.Widget.", "").replace("Class.Pipeline.", "").replace(" ", "_")
        )
        dialog.set_initial_name(f"{base_stem}.pdf")

        pdf_filter = Gtk.FileFilter()
        pdf_filter.set_name("PDF Document (*.pdf)")
        pdf_filter.add_pattern("*.pdf")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(pdf_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(pdf_filter)

        initial_dir = os.path.dirname(vm.source_file) if vm.source_file else ""
        if (not initial_dir or not os.path.isdir(initial_dir)) and self.workspace:
            initial_dir = self.workspace.root_dir
        if initial_dir and os.path.isdir(initial_dir):
            dialog.set_initial_folder(Gio.File.new_for_path(initial_dir))

        dialog.save(self, None, self._on_export_pdf_finished)

    def _on_export_pdf_finished(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            gfile = dialog.save_finish(result)
            if gfile:
                path = gfile.get_path()
                if path:
                    if not path.lower().endswith(".pdf"):
                        path += ".pdf"
                    self.export_active_tab_to_pdf(path)
        except Exception:
            pass

    def action_select_all(self) -> int:
        """Select all nodes and containers on the active graph tab."""
        cnt = self.canvas.select_all()
        self.status_lbl.set_text(f"Selected all ({cnt} items)")
        return cnt

    def action_copy(self) -> int:
        """Copy selected nodes/containers on the active graph tab to clipboard."""
        cnt = self.canvas.copy_selected()
        if cnt > 0:
            self.status_lbl.set_text(f"Copied {cnt} item(s) to clipboard")
        else:
            self.status_lbl.set_text("Nothing selected to copy")
        return cnt

    def action_cut(self) -> int:
        """Cut selected nodes/containers on the active graph tab to clipboard."""
        cnt = self.canvas.cut_selected()
        if cnt > 0:
            self._update_active_tab_header()
            self.status_lbl.set_text(f"Cut {cnt} item(s) to clipboard")
        else:
            self.status_lbl.set_text("Nothing selected to cut")
        return cnt

    def action_paste(self) -> int:
        """Paste clipboard nodes/containers into the active graph tab."""
        cnt = self.canvas.paste_clipboard()
        if cnt > 0:
            self._update_active_tab_header()
            self.status_lbl.set_text(f"Pasted {cnt} node(s) from clipboard")
        else:
            self.status_lbl.set_text("Clipboard is empty")
        return cnt

    def action_delete_selected(self) -> None:
        """Delete selected nodes, containers, or route edge on the active graph tab."""
        self.canvas.delete_selected()
        self._update_active_tab_header()

    def _get_ordered_enabled_tb_io_nodes(
        self,
        vm_override: Optional[GraphViewModel] = None,
    ) -> Tuple[List[GraphNode], List[GraphNode]]:
        """Return (enabled_sources, enabled_sinks) ordered by their connected copier's position
        in _extract_active_pipeline_ids(vm) so Stream 1..N match testbench file slots 1..N.
        """
        vm = vm_override if vm_override is not None else self.canvas.vm
        if not vm:
            return ([], [])
        active_pids = _extract_active_pipeline_ids(vm)
        pid_rank = {pid: idx for idx, pid in enumerate(active_pids)}

        def _node_sort_key(idx_and_node: Tuple[int, GraphNode]) -> Tuple[int, float, float, int]:
            orig_idx, n = idx_and_node
            pnode = vm.nodes.get(n.parent_node_id or "") if n.parent_node_id else None
            if pnode and pnode.pipeline_id is not None and pnode.pipeline_id in pid_rank:
                return (pid_rank[pnode.pipeline_id], n.y, n.x, orig_idx)
            t_str = str(n.config.get_scalar("target_pipelines", ""))
            for tok in t_str.replace("→", ",").replace("->", ",").split(","):
                tok = tok.strip()
                if tok.isdigit() and int(tok) in pid_rank:
                    return (pid_rank[int(tok)], n.y, n.x, orig_idx)
            return (999999, n.y, n.x, orig_idx)

        raw_sources = [
            (i, n)
            for i, n in enumerate(vm.nodes.values())
            if n.class_category == "TestbenchIO"
            and n.class_name == "testbench-source"
            and str(n.config.get_scalar("enabled", "false")).lower() == "true"
        ]
        raw_sinks = [
            (i, n)
            for i, n in enumerate(vm.nodes.values())
            if n.class_category == "TestbenchIO"
            and n.class_name == "testbench-sink"
            and str(n.config.get_scalar("enabled", "false")).lower() == "true"
        ]
        raw_sources.sort(key=_node_sort_key)
        raw_sinks.sort(key=_node_sort_key)
        return ([n for _, n in raw_sources], [n for _, n in raw_sinks])

    def _get_ordered_active_stream_keys(
        self,
        vm_override: Optional[GraphViewModel] = None,
    ) -> List[str]:
        """Return ordered unique keys for each currently enabled PCM stream in `vm_override` (or active vm)."""
        vm = vm_override if vm_override is not None else self.canvas.vm
        if not vm:
            return []
        enabled_sources, enabled_sinks = self._get_ordered_enabled_tb_io_nodes(vm)
        n_streams = max(len(enabled_sources), len(enabled_sinks))
        if n_streams == 0:
            return ["__default__"]
        keys: List[str] = []
        for idx in range(n_streams):
            src_id = enabled_sources[idx].id if idx < len(enabled_sources) else ""
            snk_id = enabled_sinks[idx].id if idx < len(enabled_sinks) else ""
            keys.append(src_id or snk_id or f"stream_{idx}")
        return keys

    @staticmethod
    def _describe_tb_source_signal(snode: GraphNode) -> Tuple[str, str, str]:
        """Return (sig_kind, wav_src, sig_label) for a testbench-source node."""
        sig_kind = "sine_997"
        wav_src = ""
        sig_label = "Sine 997 Hz"
        io_mode = str(snode.config.get_scalar("io_type", "sig_gen")).strip().lower()
        if io_mode == "sig_gen":
            sig_kind = str(snode.config.get_scalar("signal_type", "sine_997")).strip().lower() or "sine_997"
            sig_label = TopologyGraphCanvas.get_signal_type_short_label(sig_kind)
        elif io_mode == "alsa_pcm":
            apcm = str(snode.config.get_scalar("alsa_pcm", "default")).strip()
            sig_label = f"ALSA {apcm}"
        else:
            fpath = str(snode.config.get_scalar("file_path", "")).strip()
            if fpath and os.path.isfile(fpath):
                sig_kind = "wav_file"
                wav_src = fpath
                sig_label = os.path.basename(fpath)
            else:
                sig_label = os.path.basename(fpath) if fpath else "Audio File"
        return (sig_kind, wav_src, sig_label)

    def _extract_graph_stream_params(self) -> Tuple[str, str, str, int, int, int]:
        """Extract (sig_kind, wav_src, sig_label, rate, channels, bits) from the active Main Graph panel."""
        sig_kind = "sine_997"
        wav_src = ""
        sig_label = "Sine 997 Hz"
        rate = 48000
        channels = 2
        bits = 32

        vm = self.canvas.vm
        if not vm:
            return (sig_kind, wav_src, sig_label, rate, channels, bits)

        enabled_sources, _ = self._get_ordered_enabled_tb_io_nodes()
        snode = enabled_sources[0] if enabled_sources else None
        if snode is not None:
            sig_kind, wav_src, sig_label = self._describe_tb_source_signal(snode)

        # Inspect connected host/dai copier or first active pipeline module for audio format (rate, channels, bit depth)
        fmt_candidates: List[GraphNode] = []
        if snode and snode.parent_node_id and snode.parent_node_id in vm.nodes:
            fmt_candidates.append(vm.nodes[snode.parent_node_id])
        for n in vm.nodes.values():
            if n.class_category not in ("Control", "TestbenchIO") and n not in fmt_candidates:
                fmt_candidates.append(n)

        for cand in fmt_candidates:
            for ch in cand.config.children:
                if "input_audio_format" in ch.key or "output_audio_format" in ch.key:
                    r_val = str(ch.get_scalar("in_rate") or ch.get_scalar("out_rate") or "").strip()
                    c_val = str(ch.get_scalar("in_channels") or ch.get_scalar("out_channels") or "").strip()
                    b_val = str(
                        ch.get_scalar("in_valid_bit_depth")
                        or ch.get_scalar("in_bit_depth")
                        or ch.get_scalar("out_valid_bit_depth")
                        or ch.get_scalar("out_bit_depth")
                        or ""
                    ).strip()
                    if r_val.isdigit() and int(r_val) > 0:
                        rate = int(r_val)
                    if c_val.isdigit() and int(c_val) > 0:
                        channels = int(c_val)
                    if b_val.isdigit() and int(b_val) in (16, 24, 32):
                        bits = int(b_val)
                    return (sig_kind, wav_src, sig_label, rate, channels, bits)

        return (sig_kind, wav_src, sig_label, rate, channels, bits)

    def _extract_graph_io_config(self) -> Tuple[Optional[int], Optional[int], List[str], int]:
        """Extract (rate_out, channels_out, extra_in_wavs, num_outputs) for asymmetric & multi-I/O graphs."""
        rate_out: Optional[int] = None
        ch_out: Optional[int] = None
        extra_in_wavs: List[str] = []
        num_outputs: int = 1

        vm = self.canvas.vm
        if not vm:
            return (rate_out, ch_out, extra_in_wavs, num_outputs)

        # Collect any additional enabled testbench-source nodes in pipeline slot order
        enabled_sources, enabled_sinks = self._get_ordered_enabled_tb_io_nodes()
        for sn in enabled_sources[1:]:
            io_mode = str(sn.config.get_scalar("io_type", "sig_gen")).strip().lower()
            if io_mode == "sig_gen":
                stype = str(sn.config.get_scalar("signal_type", "sine_997")).strip().lower() or "sine_997"
                extra_in_wavs.append(f"sig_gen:{stype}")
            else:
                fp = str(sn.config.get_scalar("file_path", "")).strip()
                if fp and os.path.isfile(fp):
                    extra_in_wavs.append(fp)
                else:
                    extra_in_wavs.append("sig_gen:sine_997")

        if len(enabled_sinks) > 1:
            num_outputs = len(enabled_sinks)

        # Inspect sink / DAI endpoint for output rate & channels (e.g. SRC 48k -> 16k or upmix/downmix)
        sink_candidates: List[GraphNode] = []
        if enabled_sinks and enabled_sinks[0].parent_node_id and enabled_sinks[0].parent_node_id in vm.nodes:
            sink_candidates.append(vm.nodes[enabled_sinks[0].parent_node_id])
        for n in vm.nodes.values():
            if n.class_category not in ("Control", "TestbenchIO") and not n.out_ports and n not in sink_candidates:
                sink_candidates.append(n)

        for cand in sink_candidates:
            for ch in cand.config.children:
                if "output_audio_format" in ch.key or "input_audio_format" in ch.key:
                    ro = str(ch.get_scalar("out_rate") or ch.get_scalar("in_rate") or "").strip()
                    co = str(ch.get_scalar("out_channels") or ch.get_scalar("in_channels") or "").strip()
                    if ro.isdigit() and int(ro) > 0:
                        rate_out = int(ro)
                    if co.isdigit() and int(co) > 0:
                        ch_out = int(co)
                    if rate_out or ch_out:
                        return (rate_out, ch_out, extra_in_wavs, num_outputs)

        return (rate_out, ch_out, extra_in_wavs, num_outputs)

    def _update_waveform_stream_labels(self) -> None:
        """Update Input and Output waveform lane labels from active TestbenchIO, Probe Tap, or pipeline endpoints."""
        _sig_kind, _wav_src, sig_label, rate, channels, bits = self._extract_graph_stream_params()

        in_lbl = "INPUT PCM: Source (Host/DAI)"
        out_lbl = "OUTPUT PCM: Sink (DAI/Host)"
        multi_in_labels: List[str] = []
        multi_out_labels: List[str] = []
        multi_sig_labels: List[str] = []

        vm = self.canvas.vm
        if vm:
            enabled_sources, enabled_sinks = self._get_ordered_enabled_tb_io_nodes()
            is_multi = len(enabled_sources) > 1 or len(enabled_sinks) > 1

            for idx, snode in enumerate(enabled_sources):
                target_p = str(snode.config.get_scalar("target_pipelines", "")).strip()
                p_badge = f" (Pipes: {target_p})" if target_p else ""
                _sk, _ws, s_lbl = self._describe_tb_source_signal(snode)
                multi_sig_labels.append(s_lbl)
                if is_multi:
                    multi_in_labels.append(
                        f"S{idx + 1} IN: {snode.title}{f' ({target_p})' if target_p else ''}"
                    )
                else:
                    io_mode = str(snode.config.get_scalar("io_type", "sig_gen")).strip().lower()
                    if io_mode == "sig_gen":
                        multi_in_labels.append(f"INPUT PCM: {snode.title} [SigGen: {s_lbl}]{p_badge}")
                    elif io_mode == "alsa_pcm":
                        apcm = str(snode.config.get_scalar("alsa_pcm", "default")).strip()
                        multi_in_labels.append(f"INPUT PCM: {snode.title} [ALSA: {apcm}]{p_badge}")
                    else:
                        fpath = str(snode.config.get_scalar("file_path", "")).strip()
                        fname = os.path.basename(fpath) if fpath else "audio.wav"
                        multi_in_labels.append(f"INPUT PCM: {snode.title} [File: {fname}]{p_badge}")

            for idx, knode in enumerate(enabled_sinks):
                target_p = str(knode.config.get_scalar("target_pipelines", "")).strip()
                if is_multi:
                    multi_out_labels.append(
                        f"S{idx + 1} OUT: {knode.title}{f' ({target_p})' if target_p else ''}"
                    )
                else:
                    multi_out_labels.append(f"OUTPUT PCM: {knode.title} ({knode.subtitle})")

            if len(enabled_sources) > 1:
                src_titles = " + ".join(n.title for n in enabled_sources)
                pipes_list = [
                    str(n.config.get_scalar("target_pipelines", "")).strip()
                    for n in enabled_sources
                    if str(n.config.get_scalar("target_pipelines", "")).strip()
                ]
                p_badge = f" (Pipes: {', '.join(pipes_list)})" if pipes_list else ""
                in_lbl = f"INPUT PCM ({len(enabled_sources)}x): {src_titles}{p_badge}"
            elif multi_in_labels:
                in_lbl = multi_in_labels[0]
            else:
                host_in = [
                    n for n in vm.nodes.values()
                    if n.class_category not in ("Control", "TestbenchIO") and not n.in_ports
                ]
                if host_in:
                    in_lbl = f"INPUT PCM: {host_in[0].id} (Pipe {host_in[0].pipeline_id or 1})"

            if self.canvas.active_probe_node_id and self.canvas.active_probe_node_id in vm.nodes:
                pnode = vm.nodes[self.canvas.active_probe_node_id]
                cid = self.canvas.live_node_telemetry.get(pnode.id, {}).get("comp_id", 0)
                out_lbl = f"PROBE TAP: {pnode.title} (comp_id={cid})"
                multi_out_labels = [out_lbl]
            elif len(enabled_sinks) > 1:
                snk_titles = " + ".join(n.title for n in enabled_sinks)
                out_lbl = f"OUTPUT PCM ({len(enabled_sinks)}x): {snk_titles}"
            elif multi_out_labels:
                out_lbl = multi_out_labels[0]
            else:
                dai_out = [
                    n for n in vm.nodes.values()
                    if n.class_category not in ("Control", "TestbenchIO") and not n.out_ports
                ]
                if dai_out:
                    out_lbl = f"OUTPUT PCM: {dai_out[0].id} (Pipe {dai_out[0].pipeline_id or 2})"

        self.waveform_widget.set_stream_labels(
            in_label=in_lbl,
            out_label=out_lbl,
            signal_label=sig_label,
            rate=rate,
            channels=channels,
            bits=bits,
            multi_in_labels=multi_in_labels or [in_lbl],
            multi_out_labels=multi_out_labels or [out_lbl],
            multi_signal_labels=multi_sig_labels or [sig_label],
        )

        # Align displayed waveforms with the currently selected PCMs on the active Main Graph tab,
        # removing any deselected PCM waveforms and updating when switching between graph tabs.
        active_tab = self.get_active_tab()
        active_keys = self._get_ordered_active_stream_keys(vm)
        if active_tab is not None:
            active_key_set = set(active_keys)
            for k in list(active_tab.stream_waveforms.keys()):
                if k not in active_key_set:
                    del active_tab.stream_waveforms[k]

        aligned_in_samples: List[List[float]] = []
        aligned_out_samples: List[List[float]] = []
        for skey in active_keys:
            if active_tab is not None and skey in active_tab.stream_waveforms:
                s_in, s_out = active_tab.stream_waveforms[skey]
                aligned_in_samples.append(list(s_in))
                aligned_out_samples.append(list(s_out))
            else:
                aligned_in_samples.append([])
                aligned_out_samples.append([])

        primary_in = aligned_in_samples[0] if aligned_in_samples else []
        primary_out = aligned_out_samples[0] if aligned_out_samples else []
        if not primary_out:
            for cand_s in aligned_out_samples[1:]:
                if cand_s:
                    primary_out = cand_s
                    break

        summary_lbl = (
            active_tab.last_tb_summary
            if (active_tab is not None and active_tab.last_tb_summary and any(aligned_out_samples))
            else "No testbench run yet — click [▶ Run in Testbench]"
        )
        self.waveform_widget.set_waveforms(
            primary_in,
            primary_out,
            summary=summary_lbl,
            is_realtime=self.waveform_widget.is_realtime,
            multi_in_samples=aligned_in_samples,
            multi_out_samples=aligned_out_samples,
        )
        if not self.waveform_widget.is_realtime:
            n_streams = max(1, len(aligned_in_samples), len(aligned_out_samples))
            has_any_out = any(len(s) > 0 for s in aligned_out_samples)
            if has_any_out:
                self.wf_status_lbl.set_text(
                    f"Waveforms [SNAPSHOT — {n_streams} Streams]" if n_streams > 1 else "Waveforms [SNAPSHOT]"
                )
            else:
                self.wf_status_lbl.set_text(
                    f"Waveforms [IDLE — {n_streams} Streams]" if n_streams > 1 else "Waveforms [IDLE]"
                )

    def _on_waveform_zoom_changed(self) -> None:
        self.wf_zoom_lbl.set_text(self.waveform_widget.get_zoom_summary_text())

    def _on_wf_trigger_toggled(self, btn: Gtk.ToggleButton) -> None:
        self.waveform_widget.trigger_lock = btn.get_active()
        self.waveform_widget.queue_draw()

    def _on_rt_waveform_toggled(self, btn: Gtk.ToggleButton) -> None:
        active = btn.get_active()
        self.waveform_widget.is_realtime = active
        if active:
            self.keep_alive_btn.set_active(True)
            if self.tb_runner and self.tb_runner.live_client and self.tb_runner.live_proc:
                # Ensure DSP is in D0 active state for real-time streaming
                self.tb_runner.live_client.pm_control(SOF_PM_OP_WAKE_HOLD)
                self._start_realtime_waveform_timer()
            else:
                self.wf_status_lbl.set_text("Waveforms [Starting Real-Time Testbench…]")
                self._on_run_testbench_clicked(self.run_tb_btn)
        else:
            self._stop_realtime_waveform_timer()
            if self.waveform_widget.out_samples:
                self.wf_status_lbl.set_text("Waveforms [SNAPSHOT]")
            else:
                self.wf_status_lbl.set_text("Waveforms [IDLE]")
            self.waveform_widget.queue_draw()

    def _start_realtime_waveform_timer(self) -> None:
        self._stop_realtime_waveform_timer()
        self.waveform_widget.is_realtime = True
        self._rt_timer_id = GLib.timeout_add(95, self._on_realtime_waveform_tick)

    def _stop_realtime_waveform_timer(self) -> None:
        if self._rt_timer_id is not None:
            GLib.source_remove(self._rt_timer_id)
            self._rt_timer_id = None
        self._rt_poll_busy = False

    def _on_realtime_waveform_tick(self) -> bool:
        if not self.rt_waveform_btn.get_active():
            self._rt_timer_id = None
            return False
        if not self.tb_runner or not self.tb_runner.live_client:
            self._rt_timer_id = None
            return False
        if self._rt_poll_busy:
            return True

        self._rt_poll_busy = True

        def poll_worker() -> None:
            res_tuple = self.tb_runner.poll_live_waveforms(window_frames=3600) if self.tb_runner else None
            GLib.idle_add(self._apply_realtime_waveform_poll, res_tuple)

        threading.Thread(target=poll_worker, daemon=True).start()
        return True

    def _apply_realtime_waveform_poll(
        self,
        res_tuple: Optional[tuple],
    ) -> bool:
        self._rt_poll_busy = False
        if not res_tuple:
            if self.rt_waveform_btn.get_active():
                self.wf_status_lbl.set_text("Waveforms [Disconnected]")
            return False
        in_slice, out_slice, frames_played, dsp_active, tone_running = res_tuple
        state_str = "● LIVE D0" if dsp_active else "⏸ PAUSED D3"
        tone_str = " | 🎵 1kHz Tone" if tone_running else ""
        self.wf_status_lbl.set_text(
            f"Waveforms [{state_str} — {frames_played} frames{tone_str}]"
        )
        self.waveform_widget.set_waveforms(
            in_slice,
            out_slice,
            is_realtime=True,
            frames_played=frames_played,
            dsp_active=dsp_active,
        )
        return False

    def _on_add_module_requested(self, module_name: str) -> None:
        nid = self.canvas.add_module_instance(module_name)
        if nid:
            self.status_lbl.set_text(f"Added module node '{nid}' to graph.")

    def _on_add_pipeline_requested(self, pipeline_name: str) -> None:
        pid = self.canvas.add_pipeline_instance(pipeline_name)
        if pid is not None:
            self.status_lbl.set_text(f"Added pipeline {pid} ('{pipeline_name}') to graph.")

    def _on_canvas_selection_changed(
        self,
        title: Optional[str],
        config: Optional[ConfigNode],
        subtitle: str,
    ) -> None:
        self.right_inspector.set_node_config(title, config, subtitle)

    def _on_before_attribute_edit(self, desc: str) -> None:
        self.canvas.record_undo_snapshot(desc)

    def _mark_active_tab_graph_modified_visually(self) -> None:
        """Mark the active tab so its Text Editor refreshes from the updated GraphViewModel."""
        active = self.get_active_tab()
        if active is None:
            return
        active.text_needs_refresh_from_graph = True
        if active.view_stack.get_visible_child_name() == "text":
            self._refresh_tab_text_from_graph(active)

    def _on_node_attribute_edited(self, config: ConfigNode, key: str, new_val: str) -> None:
        if not self.canvas.vm:
            return
        debug_log_action(
            "ATTR-EDIT",
            f"Edited attribute '{key}' = '{new_val}'",
            f"config={config.key} node={self.canvas.selected_node_id or 'root'}",
        )
        self.canvas.vm.dirty = True
        # Update node input/output format badges and geometry if format or stream_name changed
        if self.canvas.selected_node_id and self.canvas.selected_node_id in self.canvas.vm.nodes:
            node = self.canvas.vm.nodes[self.canvas.selected_node_id]
            if node.class_category == "TestbenchIO":
                if key == "enabled" and new_val.strip().lower() in ("true", "1", "yes"):
                    self.canvas._suppress_auto_history = True
                    try:
                        self.canvas.select_testbench_io_radio(node.id)
                    finally:
                        self.canvas._suppress_auto_history = False
                elif key in ("enabled", "target_pipelines"):
                    self._sync_selected_pipelines_to_testbench(force_recreate=True)
                elif key in ("signal_type", "io_type", "file_path"):
                    active_tab = self.get_active_tab()
                    if active_tab is not None and node.id in active_tab.stream_waveforms:
                        del active_tab.stream_waveforms[node.id]
                    if key == "io_type" and new_val.strip().lower() == "sig_gen" and node.class_name == "testbench-source":
                        if not node.config.get_scalar("signal_type"):
                            node.config.set_scalar(
                                "signal_type",
                                "sine_997",
                                category_info="Signal Generator waveform type (when io_type=sig_gen)",
                                origin="instance",
                                valid_values=[opt[0] for opt in TopologyGraphCanvas.SIGNAL_TYPE_OPTIONS],
                            )
                            self.right_inspector.set_node_config(
                                f"{node.title} ({node.class_name})",
                                node.config,
                                node.subtitle,
                            )
            else:
                if key == "included_param_file" and node.class_category == "Control":
                    self.canvas.vm.graph_structure_modified = True
                    if new_val.strip():
                        node.badges = [os.path.basename(new_val.strip().strip('"'))]
                node.in_fmt_badge = Topology2Workspace.summarize_pin_audio_format(node.config, "in")
                node.out_fmt_badge = Topology2Workspace.summarize_pin_audio_format(node.config, "out")
            _compute_node_geometry(node)
        self._mark_active_tab_graph_modified_visually()
        self._update_waveform_stream_labels()
        self._update_active_tab_header()
        self.canvas.queue_draw()
        self.status_lbl.set_text(f"Updated attribute '{key}' = '{new_val}'")

    def _on_undo_clicked(self, _btn: Optional[Gtk.Button]) -> None:
        desc = self.canvas.history.undo_description
        if self.canvas.undo():
            debug_log_action("UNDO", f"Undid '{desc}'")
            self._mark_active_tab_graph_modified_visually()
            self._update_waveform_stream_labels()
            self._update_active_tab_header()
            self.status_lbl.set_text(f"Undid: {desc}")

    def _on_redo_clicked(self, _btn: Optional[Gtk.Button]) -> None:
        desc = self.canvas.history.redo_description
        if self.canvas.redo():
            debug_log_action("REDO", f"Redid '{desc}'")
            self._mark_active_tab_graph_modified_visually()
            self._update_waveform_stream_labels()
            self._update_active_tab_header()
            self.status_lbl.set_text(f"Redid: {desc}")

    def _update_undo_redo_buttons(self) -> None:
        h = self.canvas.history
        if hasattr(self, "undo_action") and self.undo_action is not None:
            self.undo_action.set_enabled(h.can_undo)
        if hasattr(self, "redo_action") and self.redo_action is not None:
            self.redo_action.set_enabled(h.can_redo)

    def _on_graph_modified(self) -> None:
        if self.canvas.vm:
            self._mark_active_tab_graph_modified_visually()
            self._update_waveform_stream_labels()
            self._update_active_tab_header()
            self.status_lbl.set_text(
                f"Graph modified: {len(self.canvas.vm.nodes)} nodes, {len(self.canvas.vm.edges)} routes"
            )

    def _on_relayout_clicked(self, _btn: Gtk.Button) -> None:
        self.canvas.relayout_graph()

    def _on_export_conf_clicked(self, _btn: Gtk.Button) -> None:
        active = self.get_active_tab()
        if active and active.text_editor.is_modified_in_text:
            active.text_editor.commit_to_diagram()
        if not self.canvas.vm or not self.compiler:
            return
        self.left_panel.show_compile_pane()
        res = self.compiler.compile_graph(self.canvas.vm)
        self.append_log(f"Exported compilable .conf to: {res.conf_path}", diagnostics=res.diagnostics)
        self.status_lbl.set_text(f"Exported: {res.conf_path}")

    def _on_clear_compile_log_clicked(self, _btn: Gtk.Button) -> None:
        buf = self.log_view.get_buffer()
        buf.set_text("")
        self._log_line_diagnostics.clear()
        self.left_panel.set_compile_tab_title("Compile")
        self.compile_pane_status_lbl.set_text("alsatplg Compiler Output (Dbl-Click error/warning)")

    def _on_open_dir_clicked(self, _btn: Gtk.Button) -> None:
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Select tools/topology/topology2 Directory")
        dialog.select_folder(self, None, self._on_folder_selected)

    def _on_folder_selected(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            folder = dialog.select_folder_finish(result)
            if folder:
                path = folder.get_path()
                if path:
                    self.load_topology2_directory(path)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Compile & Testbench Execution
    # -------------------------------------------------------------------------

    def _on_compile_clicked(self, _btn: Gtk.Button) -> None:
        active = self.get_active_tab()
        if active and active.text_editor.is_modified_in_text:
            active.text_editor.commit_to_diagram()
        if not self.canvas.vm or not self.compiler:
            return
        vm = self.canvas.vm
        debug_log_action(
            "COMPILE-CLICKED",
            f"Compile '{vm.title}'",
            f"kind={vm.view_kind} platform={self.target_platform} struct_modified={vm.graph_structure_modified}",
        )
        self.left_panel.set_compile_tab_title("Compile ⏳")
        self.left_panel.show_compile_pane()
        self.compile_pane_status_lbl.set_text(f"Compiling {vm.title} …")
        self.status_lbl.set_text(f"Compiling {vm.title} with alsatplg -p …")
        self.compile_btn.set_sensitive(False)

        def worker() -> None:
            res = self.compiler.compile_graph(vm)
            GLib.idle_add(self._on_compile_finished, res, False)

        threading.Thread(target=worker, daemon=True).start()

    def _on_run_testbench_clicked(self, _btn: Gtk.Button) -> None:
        active = self.get_active_tab()
        if active and active.text_editor.is_modified_in_text:
            active.text_editor.commit_to_diagram()
        if not self.canvas.vm or not self.compiler or not self.tb_runner:
            return
        vm = self.canvas.vm
        debug_log_action(
            "RUN-TESTBENCH-CLICKED",
            f"Compile & Run '{vm.title}' in sof-testbench4",
            f"kind={vm.view_kind} platform={self.target_platform} struct_modified={vm.graph_structure_modified}",
        )
        self.left_panel.set_compile_tab_title("Compile ⏳")
        self.left_panel.show_compile_pane()
        self.compile_pane_status_lbl.set_text(f"Compiling {vm.title} for Testbench …")
        self.status_lbl.set_text(f"Compiling & running {vm.title} in sof-testbench4 …")
        self.compile_btn.set_sensitive(False)
        self.run_tb_btn.set_sensitive(False)

        def worker() -> None:
            res = self.compiler.compile_graph(vm)
            GLib.idle_add(self._on_compile_finished, res, True)

        threading.Thread(target=worker, daemon=True).start()

    def _collect_kcontrol_overrides(self) -> Dict[str, List[int]]:
        """Collect any pending user-modified kcontrol overrides."""
        return dict(self._pending_kctl_overrides)

    def _on_compile_finished(self, res: CompileResult, run_tb_after: bool) -> bool:
        self.compile_btn.set_sensitive(True)
        self._last_conf_path = res.conf_path
        self._last_compile_diagnostics = list(res.diagnostics)
        self.canvas.clear_diagnostic_highlights()
        self.left_panel.show_compile_pane()

        self.append_log(f"$ {res.command}")
        if res.stdout.strip():
            self.append_log(res.stdout.strip(), diagnostics=res.diagnostics)
        if res.stderr.strip():
            self.append_log(res.stderr.strip(), diagnostics=res.diagnostics)

        err_cnt = sum(1 for d in res.diagnostics if d.severity == "error")
        warn_cnt = sum(1 for d in res.diagnostics if d.severity == "warning")

        if not res.success:
            self.run_tb_btn.set_sensitive(True)
            self.left_panel.set_compile_tab_title(f"Compile ❌ ({max(1, err_cnt)})")
            self.compile_pane_status_lbl.set_text(
                f"FAILED ({max(1, err_cnt)} err, {warn_cnt} warn) — Dbl-Click line below"
            )
            self.status_lbl.set_text(
                f"Compilation FAILED for {os.path.basename(res.conf_path)} — double-click error line in Compile pane to inspect"
            )
            self.append_log(
                "[ERROR] alsatplg compilation failed. Double-click any error/warning line above to bring up the failing part in the Graph Viewer & Attribute Editor.",
                diagnostics=res.diagnostics,
            )
            return False

        self.last_compiled_tplg = res.tplg_path
        if warn_cnt > 0:
            self.left_panel.set_compile_tab_title(f"Compile ⚠ ({warn_cnt})")
        else:
            self.left_panel.set_compile_tab_title("Compile ✔")
        self.compile_pane_status_lbl.set_text(
            f"Compiled OK: {os.path.basename(res.tplg_path)} ({res.size_bytes} B)"
        )
        msg = f"Compiled OK: {res.tplg_path} ({res.size_bytes} bytes)"
        self.status_lbl.set_text(msg)
        self.append_log(msg, diagnostics=res.diagnostics)

        if not run_tb_after:
            self.run_tb_btn.set_sensitive(True)
            self._sync_selected_pipelines_to_testbench(force_recreate=True)
            return False

        # Supersede any pending background pipeline sync worker before running testbench
        self._pipeline_sync_seq += 1

        # Resolve testbench signal, format, and pipeline IDs from the active Main Graph panel
        sig_kind, wav_src, _sig_lbl, rate, channels, bits = self._extract_graph_stream_params()
        rate_out, ch_out, extra_in_wavs, num_outputs = self._extract_graph_io_config()
        pipes = list(res.pipeline_ids) if res.pipeline_ids else [1, 2]
        kctl_overrides = self._collect_kcontrol_overrides()
        bytes_overrides = dict(self._pending_bytes_overrides)
        keep_alive = self.keep_alive_btn.get_active() or self.rt_waveform_btn.get_active()
        hot_reload = bool(hasattr(self, "hot_reload_btn") and self.hot_reload_btn.get_active())
        run_tab = self.get_active_tab()
        run_keys = self._get_ordered_active_stream_keys(run_tab.canvas.vm if run_tab else None)
        self._stop_realtime_waveform_timer()
        self._update_waveform_stream_labels()
        self.wf_status_lbl.set_text("Waveforms [● Running Testbench…]")

        def on_live_progress(in_s: List[float], out_s: List[float], frames_cnt: int) -> None:
            GLib.idle_add(
                self.waveform_widget.set_waveforms,
                in_s,
                out_s,
                f"Processing live… ({frames_cnt} frames)",
                True,
                frames_cnt,
                True,
            )

        def tb_worker() -> None:
            assert self.tb_runner is not None
            tb_res = self.tb_runner.run_testbench(
                tplg_path=res.tplg_path,
                pipeline_ids=pipes,
                signal_kind=sig_kind,
                rate=rate,
                channels=channels,
                bits=bits,
                duration_sec=0.15,
                wav_source=wav_src,
                kcontrol_overrides=kctl_overrides,
                bytes_overrides=bytes_overrides,
                keep_alive=keep_alive,
                on_live_waveform=on_live_progress,
                extra_in_wavs=extra_in_wavs,
                num_outputs=num_outputs,
                rate_out=rate_out,
                channels_out=ch_out,
                hot_reload_if_alive=hot_reload,
            )
            GLib.idle_add(self._on_testbench_finished, tb_res, run_tab, run_keys)

        threading.Thread(target=tb_worker, daemon=True).start()
        return False

    def _on_testbench_finished(
        self,
        tb_res: TestbenchRunResult,
        run_tab: Optional[GraphTabPage] = None,
        run_keys: Optional[List[str]] = None,
    ) -> bool:
        self.run_tb_btn.set_sensitive(True)
        target_tab = run_tab if run_tab is not None else self.get_active_tab()
        if target_tab is not None and target_tab.canvas.vm is not None:
            eff_keys = (
                list(run_keys)
                if run_keys
                else self._get_ordered_active_stream_keys(target_tab.canvas.vm)
            )
            cur_active_set = set(self._get_ordered_active_stream_keys(target_tab.canvas.vm))
            target_tab.stream_waveforms.clear()
            for idx, skey in enumerate(eff_keys):
                if skey not in cur_active_set:
                    continue
                s_in = (
                    list(tb_res.multi_in_samples[idx])
                    if idx < len(tb_res.multi_in_samples)
                    else (list(tb_res.in_samples) if idx == 0 else [])
                )
                s_out = (
                    list(tb_res.multi_out_samples[idx])
                    if idx < len(tb_res.multi_out_samples)
                    else (list(tb_res.out_samples) if idx == 0 else [])
                )
                target_tab.stream_waveforms[skey] = (s_in, s_out)
            target_tab.last_tb_summary = tb_res.summary_text

        self.append_log(f"$ {tb_res.command}")
        if tb_res.telemetry and tb_res.telemetry.protocol_log:
            for pline in tb_res.telemetry.protocol_log:
                self.append_log(pline)
        if tb_res.stdout.strip():
            self.append_log(tb_res.stdout.strip())
        if tb_res.stderr.strip():
            lines = tb_res.stderr.strip().splitlines()
            tail = "\n".join(lines[-20:])
            self.append_log(tail)

        self.append_log(tb_res.summary_text)
        self.status_lbl.set_text(tb_res.summary_text)
        live_active = bool(
            self.rt_waveform_btn.get_active()
            and self.tb_runner
            and self.tb_runner.live_client
            and self.tb_runner.live_proc
        )
        self.waveform_widget.is_realtime = live_active
        self._update_waveform_stream_labels()

        active_tab = self.get_active_tab()
        if active_tab is target_tab:
            n_streams = max(
                1,
                len(self.waveform_widget.multi_in_samples),
                len(self.waveform_widget.multi_out_samples),
            )
            has_any_out = bool(
                any(len(s) > 0 for s in self.waveform_widget.multi_out_samples)
            )
            if live_active:
                self.wf_status_lbl.set_text(
                    f"Waveforms [● LIVE D0 — {n_streams} Streams]" if n_streams > 1 else "Waveforms [● LIVE D0]"
                )
                self._start_realtime_waveform_timer()
            else:
                self.wf_status_lbl.set_text(
                    (f"Waveforms [SNAPSHOT — {n_streams} Streams]" if n_streams > 1 else "Waveforms [SNAPSHOT]")
                    if has_any_out
                    else "Waveforms [FAILED]"
                )

        if tb_res.telemetry:
            self._apply_telemetry_to_ui(tb_res.telemetry)
        self.right_notebook.set_current_page(1)

        if tb_res.success and tb_res.out_wav_path and os.path.isfile(tb_res.out_wav_path):
            self.last_output_wav = tb_res.out_wav_path
            self.play_out_btn.set_sensitive(True)
        return False

    def _apply_telemetry_to_ui(self, telem: TestbenchTelemetry) -> None:
        self.last_telemetry = telem
        pwr = "D0 (ACTIVE)" if telem.dsp_power_state == 1 else "D3 (PAUSED)"
        active_kcps = sum(c.avg_kcps for c in telem.cores if c.enabled)
        self.telem_summary_lbl.set_text(
            f"SOFR :{telem.server_port}  |  FW v{telem.fw_version} ({pwr})  |  "
            f"Cores=0x{telem.enabled_cores_mask:02x}  |  Ticks={telem.total_ll_ticks}  |  "
            f"KCPS={active_kcps}  |  HPSRAM={telem.hpsram.powered_banks}/{telem.hpsram.total_banks} banks  |  "
            f"Heap={telem.total_heap_bytes}B  |  DAPM={telem.dapm_powered_nodes}/{telem.dapm_total_nodes} widgets"
        )
        tbuf = self.telem_view.get_buffer()
        tbuf.set_text(telem.format_report())

        self._discovered_kcontrols = list(telem.kcontrols)
        # Sync live telemetry kcontrol values onto matching graph Control nodes
        if self.canvas.vm and self._discovered_kcontrols:
            for kc in self._discovered_kcontrols:
                if not kc.values or kc.type_str == "BYTES":
                    continue
                for gnode in self.canvas.vm.nodes.values():
                    if gnode.class_category == "Control" and gnode.category != "control_bytes":
                        gname = str(gnode.config.get_scalar("name", "")).strip('"')
                        if gname and (gname == kc.name or gname in kc.name or kc.name in gname):
                            if kc.max_val > 1:
                                gnode.config.set_scalar("max", str(kc.max_val), origin="default")
                            if gname not in self._pending_kctl_overrides and kc.name not in self._pending_kctl_overrides:
                                gnode.config.set_scalar("value", str(kc.values[0]), origin="default")
        # Populate live node telemetry & DAPM power overlay badges on the active graph canvas
        self.canvas.set_live_telemetry_overlay(telem)

        # Sync Remote Control tab widgets with live telemetry state
        self._syncing_remote_controls = True
        try:
            if hasattr(self, "core_toggle_btns"):
                for cid, cbtn in enumerate(self.core_toggle_btns):
                    cbtn.set_active(bool(telem.enabled_cores_mask & (1 << cid)))
            if hasattr(self, "wsim_mode_combo") and 0 <= telem.workload_sim_mode <= 2:
                self.wsim_mode_combo.set_selected(telem.workload_sim_mode)
            if hasattr(self, "wsim_mem_combo") and 0 <= telem.default_mem_domain <= 1:
                self.wsim_mem_combo.set_selected(telem.default_mem_domain)
            if hasattr(self, "wsim_freq_spin") and telem.cores and telem.cores[0].freq_hz > 0:
                self.wsim_freq_spin.set_value(float(telem.cores[0].freq_hz) / 1e6)
        finally:
            self._syncing_remote_controls = False

    # -------------------------------------------------------------------------
    # Main Graph Canvas Probe Tap, Pipeline IPC4 Sync & Kcontrol Live Server Synchronization
    # -------------------------------------------------------------------------

    def _on_window_close_request(self, _win: Gtk.Window) -> bool:
        """Cleanly destroy active IPC pipelines and stop the interactive sof-testbench4 server on exit."""
        self._stop_realtime_waveform_timer()
        if self.tb_runner:
            self.tb_runner.stop_live_session()
        return False

    def _on_canvas_testbench_io_changed(self, pipeline_ids: List[int]) -> None:
        """Called when the user changes the selected TestbenchIO pipeline chain on the Main Graph Canvas."""
        debug_log_action(
            "PIPELINE-SELECT-CHANGED",
            f"Selected pipelines {pipeline_ids} in Main Window",
        )
        self._update_waveform_stream_labels()
        self._sync_selected_pipelines_to_testbench(
            force_recreate=False,
            explicit_pipeline_ids=pipeline_ids,
        )

    def _sync_selected_pipelines_to_testbench(
        self,
        force_recreate: bool = False,
        explicit_pipeline_ids: Optional[List[int]] = None,
    ) -> None:
        """Compile the active graph if needed and destroy old pipelines / create the currently
        selected pipelines in the interactive sof-testbench4 server via IPC4 messages.
        """
        if not self.canvas.vm or not self.compiler or not self.tb_runner:
            return
        vm = self.canvas.vm.clone()
        _sig_kind, _wav_src, _sig_lbl, rate, channels, bits = self._extract_graph_stream_params()
        rate_out, ch_out, _extra_in_wavs, _num_outputs = self._extract_graph_io_config()
        target_plat = self.target_platform
        self._pipeline_sync_seq += 1
        seq = self._pipeline_sync_seq

        def sync_worker() -> None:
            if seq != self._pipeline_sync_seq or not self.compiler or not self.tb_runner:
                return
            res = self.compiler.compile_graph(vm)
            if not res.success or not res.tplg_path or not os.path.isfile(res.tplg_path):
                return
            if seq != self._pipeline_sync_seq:
                return
            pipes = (
                list(explicit_pipeline_ids)
                if explicit_pipeline_ids
                else (list(res.pipeline_ids) if res.pipeline_ids else [1, 2])
            )
            ok, ipc_lines = self.tb_runner.sync_selected_pipelines_via_ipc(
                tplg_path=res.tplg_path,
                pipeline_ids=pipes,
                rate=rate,
                rate_out=rate_out,
                channels=channels,
                channels_out=ch_out,
                bits=bits,
                platform_preset=target_plat,
                force_recreate=force_recreate,
            )
            if seq != self._pipeline_sync_seq:
                return
            telem = None
            if ok and self.tb_runner.live_client:
                with self.tb_runner._ipc_lock:
                    if seq == self._pipeline_sync_seq and self.tb_runner.live_client:
                        telem = self.tb_runner.live_client.collect_full_telemetry()

            def apply_on_main() -> bool:
                if seq != self._pipeline_sync_seq:
                    return False
                self.last_compiled_tplg = res.tplg_path
                for line in ipc_lines:
                    self.append_log(line)
                if telem is not None:
                    self._apply_telemetry_to_ui(telem)
                if ipc_lines:
                    pipe_str = ",".join(str(p) for p in pipes)
                    self.status_lbl.set_text(
                        f"IPC4 synced pipelines [{pipe_str}] on interactive sof-testbench4 ({target_plat.upper()})"
                    )
                return False

            GLib.idle_add(apply_on_main)

        threading.Thread(target=sync_worker, daemon=True).start()

    def _on_canvas_probe_tap_toggled(
        self,
        node_id: Optional[str],
        comp_id: int,
        widget_name: str,
    ) -> None:
        """Called when the user clicks the [TAP] probe pill on a module node in the Main Graph Canvas."""
        if self.tb_runner:
            self.tb_runner.active_probe_comp_id = comp_id if node_id else None
            self.tb_runner.active_probe_widget_name = widget_name if node_id else ""
        self._update_waveform_stream_labels()

        if node_id is None:
            self.status_lbl.set_text("Disabled module probe tap — bottom oscilloscope restored to main PCM output.")
            if self.tb_runner and self.tb_runner.live_client:
                self._on_refresh_telemetry_clicked(None)
            return

        self.status_lbl.set_text(
            f"Probe Tap active on '{widget_name}' (comp_id={comp_id}) — bottom oscilloscope showing module exit PCM"
        )
        if self.tb_runner and self.tb_runner.live_client:
            st, _res_id, tap_samples = self.tb_runner.live_client.probe_read_buffer(comp_id=comp_id, max_bytes=4096)
            self.append_log(
                f"[SOFR] Live OP_PROBE_CTL READ_BUFFER comp_id={comp_id} ('{widget_name}') -> status={st}, {len(tap_samples)} samples"
            )
            if tap_samples:
                self.waveform_widget.set_waveforms(self.waveform_widget.in_samples, tap_samples)

    def _on_canvas_kcontrol_changed(self, node: GraphNode) -> None:
        """Called when the user interacts with a Control widget directly on the Main Graph Canvas."""
        if self.canvas.selected_node_id == node.id:
            self.right_inspector.set_node_config(
                f"{node.title} ({node.class_name})",
                node.config,
                node.subtitle,
            )
        self._sync_kcontrol_node_to_live_server(node)

    def _sync_kcontrol_node_to_live_server(self, node: GraphNode) -> None:
        """Send live SOFR OP_KCTL_SET or queue kcontrol override when a Control node changes."""
        kname = str(node.config.get_scalar("name", "")).strip('"') or node.title
        if node.category == "control_bytes":
            bfile = str(node.config.get_scalar("included_param_file", "")).strip('"')
            if bfile:
                self._pending_bytes_overrides[kname] = bfile
            if self.tb_runner and self.tb_runner.live_client and bfile:
                abs_b = bfile
                if not os.path.isabs(abs_b) and self.workspace:
                    cand = os.path.join(self.workspace.root_dir, bfile)
                    if os.path.isfile(cand):
                        abs_b = cand
                matched_kc = next(
                    (kc for kc in self._discovered_kcontrols if kc.name == kname or kname in kc.name or kc.name in kname),
                    None,
                )
                target_name = matched_kc.name if matched_kc else kname
                numid = matched_kc.numid if matched_kc else 0
                if os.path.isfile(abs_b):
                    try:
                        with open(abs_b, "rb") as bf:
                            raw_blob = bf.read(4096)
                        st_ipc, ipc_msg = self.tb_runner.apply_bytes_kcontrol_via_ipc(
                            target_name, raw_blob, log_callback=self.append_log
                        )
                        self.append_log(f"[IPC4] Live Bytes Kcontrol '{target_name}' <- {abs_b}: {ipc_msg}")
                    except OSError:
                        pass
                self.status_lbl.set_text(f"Live Bytes control '{target_name}' updated via IPC4 from {os.path.basename(bfile)}")
            else:
                self.status_lbl.set_text(f"Updated bytes control '{kname}' → {bfile} (queued for next run)")
            return

        if node.category == "control_enum":
            cur_opt, opts = self.canvas._get_kcontrol_enum_state(node)
            ival = opts.index(cur_opt) if cur_opt in opts else 0
        else:
            val_raw = str(node.config.get_scalar("value", "0")).strip('"')
            try:
                ival = int(float(val_raw.split(",")[0].strip()))
            except ValueError:
                ival = 0

        # Volume controls typically have 2 stereo channels; switches/enums have 1-2 channels
        vals = [ival, ival] if node.category in ("control_mixer", "control_switch") else [ival]
        self._pending_kctl_overrides[kname] = vals

        matched_kc: Optional[KcontrolTelemetryItem] = None
        for kc in self._discovered_kcontrols:
            if kc.name == kname or kname in kc.name or kc.name in kname:
                matched_kc = kc
                ch_cnt = max(1, getattr(kc, "count", len(kc.values) or 1))
                kc.values = [ival] * ch_cnt
                break

        if self.tb_runner and self.tb_runner.live_client:
            target_name = matched_kc.name if matched_kc else kname
            ch_cnt = max(1, getattr(matched_kc, "count", len(vals))) if matched_kc else len(vals)
            send_vals = [ival] * ch_cnt
            self._pending_kctl_overrides[target_name] = send_vals
            st, ipc_msg = self.tb_runner.apply_kcontrol_via_ipc(
                target_name, send_vals, log_callback=self.append_log
            )
            self.status_lbl.set_text(f"Live Kcontrol '{target_name}' = {send_vals} via IPC4 (status={st})")
        else:
            self.status_lbl.set_text(
                f"Updated Kcontrol '{kname}' = {vals} (queued for next Testbench run)"
            )

    def _on_remote_pm_ctl(self, op: int) -> None:
        if not self.tb_runner or not self.tb_runner.live_client:
            self.status_lbl.set_text("Enable 'Keep Server (-W)' and click ▶ Run in Testbench for live PM control.")
            return
        st, info = self.tb_runner.live_client.pm_control(op)
        self.append_log(f"[SOFR] Live OP_PM_CTL op={op} -> status={st} {info}")
        self._on_refresh_telemetry_clicked(None)

    def _on_remote_tone_toggled(self, btn: Gtk.ToggleButton) -> None:
        if not self.tb_runner or not self.tb_runner.live_client:
            return
        freq_hz = int(self.remote_tone_spin.get_value()) if hasattr(self, "remote_tone_spin") else 1000
        self.tb_runner.remote_tone_freq_hz = freq_hz
        op = SOF_PCM_OP_START_TONE if btn.get_active() else SOF_PCM_OP_STOP_TONE
        st, info, _ = self.tb_runner.live_client.pcm_control(op, freq_hz=freq_hz)
        self.append_log(
            f"[SOFR] Live OP_PCM_CTL tone={'ON' if btn.get_active() else 'OFF'} ({freq_hz} Hz) -> status={st} {info}"
        )
        self._on_refresh_telemetry_clicked(None)

    def _on_remote_tone_freq_changed(self, spin: Gtk.SpinButton) -> None:
        freq_hz = int(spin.get_value())
        if self.tb_runner:
            self.tb_runner.remote_tone_freq_hz = freq_hz
            if self.tb_runner.live_client and self.remote_tone_btn.get_active():
                st, info, _ = self.tb_runner.live_client.pcm_control(SOF_PCM_OP_START_TONE, freq_hz=freq_hz)
                self.append_log(f"[SOFR] Updated live tone frequency to {freq_hz} Hz -> status={st} {info}")
                self._on_refresh_telemetry_clicked(None)

    def _on_wsim_config_changed(self, *_args: object) -> None:
        if self._syncing_remote_controls or not self.tb_runner:
            return
        mode_idx = int(self.wsim_mode_combo.get_selected())
        mem_idx = int(self.wsim_mem_combo.get_selected())
        freq_mhz = int(self.wsim_freq_spin.get_value())

        mode_names = {0: "off", 1: "virtual", 2: "realtime"}
        mem_names = {0: "sram", 1: "dram"}
        self.tb_runner.workload_sim_mode = mode_names.get(mode_idx, "off")
        self.tb_runner.mem_domain_override = mem_names.get(mem_idx, "sram")
        self.tb_runner.core_freq_override = freq_mhz

        if self.tb_runner.live_client:
            info = self.tb_runner.live_client.workload_sim_set(
                mode=mode_idx,
                default_mem_domain=mem_idx,
                core_freqs_hz=[freq_mhz * 1000000] * 8,
            )
            st = info.get("status", 0) if info else -1
            self.append_log(
                f"[SOFR] Live OP_WORKLOAD_SIM_CFG mode={mode_names.get(mode_idx)} mem={mem_names.get(mem_idx)} freq={freq_mhz}MHz -> status={st}"
            )
            self._on_refresh_telemetry_clicked(None)
        else:
            self.status_lbl.set_text(
                f"Workload Sim configured: mode={mode_names.get(mode_idx)}, mem={mem_names.get(mem_idx)}, {freq_mhz} MHz"
            )

    def _on_core_toggle_clicked(self, core_id: int, btn: Gtk.ToggleButton) -> None:
        if self._syncing_remote_controls:
            return
        if not self.tb_runner or not self.tb_runner.live_client:
            self.status_lbl.set_text("Enable 'Keep Server (-W)' and click ▶ Run in Testbench to toggle DSP cores live.")
            return
        en = btn.get_active()
        st, info = self.tb_runner.live_client.pm_core_enable(core_id, en)
        self.append_log(
            f"[SOFR] Live OP_PM_CTL Core {core_id} {'GET (ENABLE)' if en else 'PUT (DISABLE)'} -> status={st} {info}"
        )
        self._on_refresh_telemetry_clicked(None)

    def _on_reset_wsim_clicked(self, _btn: Gtk.Button) -> None:
        if not self.tb_runner or not self.tb_runner.live_client:
            self.status_lbl.set_text("Enable 'Keep Server (-W)' and click ▶ Run in Testbench to reset simulation stats.")
            return
        info = self.tb_runner.live_client.workload_sim_reset()
        st = info.get("status", 0) if info else -1
        self.tb_runner.live_client.exec_shell("sched-telem-reset")
        self.append_log(f"[SOFR] Reset Workload Simulation & Scheduler Telemetry -> status={st}")
        self._on_refresh_telemetry_clicked(None)

    def _on_remote_ppl_state_clicked(self, state_str: str) -> None:
        pid = int(self.remote_pipe_spin.get_value()) if hasattr(self, "remote_pipe_spin") else 1
        if not self.tb_runner or not self.tb_runner.live_client:
            self.status_lbl.set_text("Interactive testbench server not connected.")
            return
        st, ipc_msg = self.tb_runner.set_pipeline_state_via_ipc(
            pid, state_str, log_callback=self.append_log
        )
        self.status_lbl.set_text(f"Pipeline {pid} state -> {state_str} via IPC4 (status={st})")
        self._on_refresh_telemetry_clicked(None)

    def _on_load_llext_clicked(self, _btn: Gtk.Button) -> None:
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Select LLEXT Shared Library (.so) Module")
        dialog.open(self, None, self._on_llext_file_selected)

    def _on_llext_file_selected(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            gfile = dialog.open_finish(result)
            if not gfile:
                return
            so_path = gfile.get_path()
            if not so_path:
                return
            if self.tb_runner and self.tb_runner.live_client:
                mod_name = os.path.splitext(os.path.basename(so_path))[0]
                st, info = self.tb_runner.live_client.load_llext_module(so_path, lib_id=1, mod_name=mod_name)
                self.append_log(f"[SOFR] Live OP_LLEXT_LOAD '{so_path}' -> status={st} {info}")
                self.status_lbl.set_text(f"Loaded LLEXT module '{mod_name}' (status={st})")
                self._on_refresh_telemetry_clicked(None)
            else:
                self.status_lbl.set_text(f"Selected LLEXT module: {so_path} (start live server to load)")
        except Exception:
            pass

    def _on_trace_level_changed(self, *_args: object) -> None:
        lvl = int(self.trace_lvl_combo.get_selected()) if hasattr(self, "trace_lvl_combo") else 2
        if self.tb_runner:
            self.tb_runner.trace_level = lvl
            if self.tb_runner.live_client:
                st, out = self.tb_runner.live_client.exec_shell(f"loglevel {lvl}")
                self.append_log(f"[SOFR] $ loglevel {lvl} (status={st})\n{out.strip()}")

    def _on_read_sram_window_clicked(self, win_id: int, win_label: str) -> None:
        if not self.tb_runner or not self.tb_runner.live_client:
            self.status_lbl.set_text("Enable 'Keep Server (-W)' and click ▶ Run in Testbench to inspect SRAM windows.")
            return
        st, raw = self.tb_runner.live_client.read_sram_window(win_id=win_id, offset=0, size=256)
        hex_lines: List[str] = [f"[SOFR] OP_WIN_READ {win_label} (win_id={win_id}, status={st}, {len(raw)} bytes):"]
        for row_off in range(0, min(len(raw), 128), 16):
            chunk = raw[row_off:row_off + 16]
            hex_part = " ".join(f"{b:02x}" for b in chunk)
            asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            hex_lines.append(f"  0x{row_off:04x}: {hex_part:<48}  |{asc_part}|")
        self.append_log("\n".join(hex_lines))
        self.left_panel.show_compile_pane()
        self.status_lbl.set_text(f"Read {len(raw)} bytes from SRAM {win_label} (see Console Log)")

    def _on_remote_shell_preset(self, cmd: str) -> None:
        self.remote_shell_entry.set_text(cmd)
        self._on_run_remote_shell_clicked(None)

    def _on_run_remote_shell_clicked(self, _btn: Optional[Gtk.Button]) -> None:
        cmd = self.remote_shell_entry.get_text().strip()
        if not cmd:
            return
        if not self.tb_runner or not self.tb_runner.live_client:
            self.status_lbl.set_text("Enable 'Keep Server (-W)' and click ▶ Run in Testbench to run live shell commands.")
            return
        st, out = self.tb_runner.live_client.exec_shell(cmd)
        self.append_log(f"[SOFR] $ {cmd}\n{out.strip()}")
        self._on_refresh_telemetry_clicked(None)

    def _on_refresh_telemetry_clicked(self, _btn: Optional[Gtk.Button]) -> None:
        if not self.tb_runner or not self.tb_runner.live_client:
            return
        telem = self.tb_runner.live_client.collect_full_telemetry()
        if telem:
            self._apply_telemetry_to_ui(telem)
            if self.tb_runner.active_probe_comp_id is not None:
                _, _, tap_samples = self.tb_runner.live_client.probe_read_buffer(
                    comp_id=self.tb_runner.active_probe_comp_id,
                    max_bytes=4096,
                )
                if tap_samples:
                    self.waveform_widget.set_waveforms(self.waveform_widget.in_samples, tap_samples)
            else:
                _, _, pcm_bytes = self.tb_runner.live_client.pcm_control(
                    SOF_PCM_OP_CAPTURE_BUFFER,
                    duration_ms=150,
                )
                if pcm_bytes:
                    out_norm = self.tb_runner._decode_s16_bytes_normalized(pcm_bytes, telem.pcm_channels)
                    if out_norm:
                        self.waveform_widget.set_waveforms(self.waveform_widget.in_samples, out_norm)

    def _on_stop_live_server_clicked(self, _btn: Gtk.Button) -> None:
        self._stop_realtime_waveform_timer()
        if self.rt_waveform_btn.get_active():
            self.rt_waveform_btn.set_active(False)
        if self.tb_runner:
            self.tb_runner.stop_live_session()
            self.keep_alive_btn.set_active(False)
            self.wf_status_lbl.set_text(
                "Waveforms [SNAPSHOT]" if self.waveform_widget.out_samples else "Waveforms [IDLE]"
            )
            self.status_lbl.set_text("Stopped live SOFR testbench server.")
            self.append_log("[SOFR] Live testbench server stopped.")

    def _on_play_output_clicked(self, _btn: Gtk.Button) -> None:
        if not self.last_output_wav or not os.path.isfile(self.last_output_wav):
            return
        player = shutil.which("aplay") or shutil.which("pw-play")
        if player:
            debug_log_command("AUDIO-PLAY", f"{player} {self.last_output_wav}", returncode=0)
            subprocess.Popen([player, self.last_output_wav])
            self.status_lbl.set_text(f"Playing {self.last_output_wav} …")
        else:
            self.status_lbl.set_text(f"Output WAV ready at: {self.last_output_wav}")

    def _init_log_text_tags(self) -> None:
        buf = self.log_view.get_buffer()
        tag_table = buf.get_tag_table()
        if tag_table.lookup("log_error") is not None:
            return
        buf.create_tag(
            "log_error",
            foreground="#ff6b6b",
            background="#3b1d24",
            weight=Pango.Weight.BOLD,
        )
        buf.create_tag(
            "log_warning",
            foreground="#ffd166",
            background="#382f1b",
            weight=Pango.Weight.BOLD,
        )
        buf.create_tag(
            "log_command",
            foreground="#7ec8ff",
        )
        buf.create_tag(
            "log_success",
            foreground="#5cd68d",
            weight=Pango.Weight.BOLD,
        )
        buf.create_tag(
            "log_clickable",
            underline=Pango.Underline.SINGLE,
        )

    def append_log(
        self,
        text: str,
        diagnostics: Optional[List[CompileDiagnostic]] = None,
    ) -> None:
        if text.strip():
            debug_log_line("GUI-LOG", text.rstrip("\n"))
        buf = self.log_view.get_buffer()
        diag_pool = diagnostics if diagnostics is not None else self._last_compile_diagnostics

        for raw_ln in text.splitlines():
            end_iter = buf.get_end_iter()
            line_idx = end_iter.get_line()
            s = raw_ln.strip()

            matched_diag: Optional[CompileDiagnostic] = None
            if diag_pool and s:
                matched_diag = next(
                    (d for d in diag_pool if d.raw_line == s or (d.message and d.message in s)),
                    None,
                )
                if matched_diag is None and s.startswith("[ERROR]") and diag_pool:
                    matched_diag = diag_pool[0]

            sev = matched_diag.severity if matched_diag else classify_log_line(raw_ln)
            tags: List[str] = []
            if sev == "error":
                tags = ["log_error", "log_clickable"]
            elif sev == "warning":
                tags = ["log_warning", "log_clickable"]
            elif s.startswith("$ ") or s.startswith("[SOFR] $"):
                tags = ["log_command"]
            elif s.startswith("Compiled OK:") or "[PASS]" in s:
                tags = ["log_success"]

            if matched_diag is not None:
                self._log_line_diagnostics[line_idx] = matched_diag
            elif sev in ("error", "warning") and s:
                # Parse on the fly if no pre-parsed diagnostic was supplied
                on_fly = parse_compile_diagnostics(
                    "",
                    raw_ln,
                    self._last_conf_path,
                    vm=self.canvas.vm,
                    topology2_root=self.workspace.root_dir if self.workspace else None,
                )
                if on_fly:
                    self._log_line_diagnostics[line_idx] = on_fly[0]

            start_offset = end_iter.get_offset()
            buf.insert(end_iter, raw_ln + "\n")
            if tags:
                s_it = buf.get_iter_at_offset(start_offset)
                e_it = buf.get_iter_at_offset(start_offset + len(raw_ln))
                for tname in tags:
                    buf.apply_tag_by_name(tname, s_it, e_it)

        mark = buf.create_mark(None, buf.get_end_iter(), False)
        self.log_view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)

    def _on_log_view_pressed(
        self,
        _gesture: Gtk.GestureClick,
        n_press: int,
        x: float,
        y: float,
    ) -> None:
        """Handle double-click on an error or warning line in the Console Log."""
        if n_press != 2:
            return
        bx, by = self.log_view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        ok, it = self.log_view.get_iter_at_location(bx, by)
        if not ok:
            return
        line_idx = it.get_line()
        diag = self._log_line_diagnostics.get(line_idx)
        if diag is None:
            buf = self.log_view.get_buffer()
            ok_s, s_it = buf.get_iter_at_line(line_idx)
            if not ok_s:
                return
            e_it = s_it.copy()
            if not e_it.ends_line():
                e_it.forward_to_line_end()
            line_text = buf.get_text(s_it, e_it, False).strip()
            if not line_text:
                return
            parsed = parse_compile_diagnostics(
                "",
                line_text,
                self._last_conf_path,
                vm=self.canvas.vm,
                topology2_root=self.workspace.root_dir if self.workspace else None,
            )
            if not parsed:
                return
            diag = parsed[0]
            self._log_line_diagnostics[line_idx] = diag

        self._activate_compile_diagnostic(diag)

    def _activate_compile_diagnostic(self, diag: CompileDiagnostic) -> None:
        """Bring up the failing part in the Main Graph Viewer and Right Attribute Editor."""
        if not self.canvas.vm:
            return
        self.right_notebook.set_current_page(0)
        self.canvas.focus_diagnostic(diag)
        attr_key = diag.symbol_or_attr or diag.target_id
        self.right_inspector.highlight_attribute(
            attr_key,
            severity=diag.severity,
            message=diag.message,
            target_kind=diag.target_kind,
        )
        loc_info = f" (line {diag.line_no})" if diag.line_no else ""
        target_desc = f"{diag.target_kind}:{diag.target_id or attr_key or 'topology'}"
        self.status_lbl.set_text(
            f"Focused {diag.severity.upper()}{loc_info} [{target_desc}] → {diag.message}"
        )
