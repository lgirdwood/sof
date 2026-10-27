# SPDX-License-Identifier: BSD-3-Clause
"""Main GTK4 Window with 3 horizontal panels:
- Left: 4 vertical sub-panes (Base Classes, Processing Modules, Pipelines, Machine Topologies)
- Middle: Gtk.Snapshot Graph Canvas + bottom Compile/Testbench waveform & log drawer
- Right: Tree view of attributes of the selected graph node
"""

from __future__ import annotations
from dataclasses import dataclass, field
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from . import __version__ as TOOL_VERSION

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
    SchedulingTelemetryWidget,
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
    last_telemetry: Optional[TestbenchTelemetry] = None


class Topology2MainWindow(Gtk.ApplicationWindow):
    """Main GTK4 Application Window for SOF Topology2 Visual Editor & Testbench Runner."""

    def __init__(self, app: Gtk.Application, initial_dir: Optional[str] = None):
        super().__init__(application=app, title="SOF Topology2 Visual Graph Editor & Testbench")
        self.set_decorated(True)

        self._ui_window_width: int = 1560
        self._ui_window_height: int = 940
        self._ui_window_maximized: bool = True
        self._saved_total_w: int = 0
        self._saved_total_h: int = 0
        self._saved_hpaned_outer_pos: Optional[int] = None
        self._saved_hpaned_inner_pos: Optional[int] = None
        self._saved_center_vpaned_pos: Optional[int] = None
        self._hpaned_outer_ratio: float = 0.25
        self._hpaned_inner_ratio: float = 0.50
        self._center_vpaned_ratio: float = 0.68
        self._saved_left_vpaned_main_pos: int = 345
        self._saved_left_vpaned_top_pos: int = 165
        self._saved_left_vpaned_bot_pos: int = 185
        self._saved_treeview_col_widths: Dict[str, List[int]] = {}
        self._saved_open_tabs: List[Dict[str, Any]] = []
        self._saved_active_tab_key: str = ""
        self._restoring_session_tabs: bool = False
        self._applying_programmatic_paned_pos: bool = True
        self._paned_layout_ready: bool = False
        self._paned_sync_seq: int = 0
        self._save_ui_state_timer_id: Optional[int] = None
        self._load_ui_state()

        self.set_default_size(self._ui_window_width, self._ui_window_height)
        if self._ui_window_maximized:
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
            on_view_source_requested=self._on_view_source_requested,
            on_violation_bang_clicked=self._on_canvas_violation_bang_clicked,
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
        self._register_win_action("about", self.action_show_about)

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

        # 5. Help Menu (About)
        help_menu = Gio.Menu.new()
        help_sec = Gio.Menu.new()
        help_sec.append("About…", "win.about")
        help_menu.append_section(None, help_sec)
        menubar_model.append_submenu("Help", help_menu)

        self.menubar_model = menubar_model
        menubar = Gtk.PopoverMenuBar.new_from_model(menubar_model)
        return menubar

    def _collect_version_info(self) -> Dict[str, str]:
        """Collect versions of the Tool, SOF firmware/testbench, ALSA library/driver, and alsa-utils."""
        gtk_ver = f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}"
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        tool_ver = f"SOF Topology2 Studio v{TOOL_VERSION} (GTK {gtk_ver}, Python {py_ver})"

        # Resolve SOF root directory
        tplg2_root = (
            self.workspace.root_dir
            if (self.workspace and self.workspace.root_dir)
            else os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "topology", "topology2"))
        )
        sof_root = os.path.abspath(os.path.join(tplg2_root, "..", "..", ".."))

        # 1. SOF Version
        sof_ver_parts: List[str] = []
        sof_ver_h_candidates = [
            os.path.join(sof_root, "tools", "testbench", "build_testbench", "sof_ep", "build", "generated", "include", "sof_versions.h"),
            os.path.join(sof_root, "build_testbench", "sof_ep", "build", "generated", "include", "sof_versions.h"),
        ]
        for vh_path in sof_ver_h_candidates:
            if os.path.isfile(vh_path):
                try:
                    with open(vh_path, "r", encoding="utf-8", errors="ignore") as fh:
                        vh_txt = fh.read()
                    maj = re.search(r"#define\s+SOF_MAJOR\s+(\d+)", vh_txt)
                    mnr = re.search(r"#define\s+SOF_MINOR\s+(\d+)", vh_txt)
                    mic = re.search(r"#define\s+SOF_MICRO\s+(\d+)", vh_txt)
                    gtag = re.search(r'#define\s+SOF_GIT_TAG\s+"([^"]+)"', vh_txt)
                    if maj and mnr and mic:
                        sof_ver_parts.append(f"v{maj.group(1)}.{mnr.group(1)}.{mic.group(1)}")
                    if gtag:
                        sof_ver_parts.append(f"({gtag.group(1)})")
                    break
                except Exception:
                    pass
        if not sof_ver_parts:
            cmake_p = os.path.join(sof_root, "CMakeLists.txt")
            if os.path.isfile(cmake_p):
                try:
                    with open(cmake_p, "r", encoding="utf-8", errors="ignore") as fh:
                        cm_txt = fh.read()
                    maj = re.search(r"set\(VERSION_MAJOR\s+(\d+)\)", cm_txt)
                    mnr = re.search(r"set\(VERSION_MINOR\s+(\d+)\)", cm_txt)
                    mic = re.search(r"set\(VERSION_MICRO\s+(\d+)\)", cm_txt)
                    if maj and mnr and mic:
                        sof_ver_parts.append(f"v{maj.group(1)}.{mnr.group(1)}.{mic.group(1)}")
                except Exception:
                    pass
            try:
                git_out = subprocess.check_output(
                    ["git", "-C", sof_root, "describe", "--tags", "--always"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=1.5,
                ).strip()
                if git_out:
                    sof_ver_parts.append(f"({git_out})")
            except Exception:
                pass
        if self.last_telemetry and self.last_telemetry.fw_version and self.last_telemetry.fw_version != "0.0.0.0":
            sof_ver_parts.append(f"[Live FW: v{self.last_telemetry.fw_version}]")
        sof_ver = " ".join(sof_ver_parts) if sof_ver_parts else "Unknown"

        # 2. ALSA & alsa-utils versions
        alsatplg_bin = (
            self.compiler.alsatplg_bin
            if (self.compiler and getattr(self.compiler, "alsatplg_bin", None))
            else (shutil.which("alsatplg") or "alsatplg")
        )
        alsatplg_ver = ""
        libasound_ver = ""
        libatopology_ver = ""
        try:
            out = subprocess.check_output(
                [alsatplg_bin, "--version"],
                stderr=subprocess.STDOUT,
                text=True,
                timeout=1.5,
            )
            for ln in out.splitlines():
                m_t = re.search(r"alsatplg\s+version\s+(\S+)", ln, re.IGNORECASE)
                if m_t:
                    alsatplg_ver = m_t.group(1)
                m_a = re.search(r"libasound\s+version\s+(\S+)", ln, re.IGNORECASE)
                if m_a:
                    libasound_ver = m_a.group(1)
                m_top = re.search(r"libatopology\s+version\s+(\S+)", ln, re.IGNORECASE)
                if m_top:
                    libatopology_ver = m_top.group(1)
        except Exception:
            pass

        if not libasound_ver:
            try:
                libasound_ver = subprocess.check_output(
                    ["pkg-config", "--modversion", "alsa"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=1.5,
                ).strip()
            except Exception:
                pass

        aplay_ver = ""
        aplay_bin = shutil.which("aplay")
        if aplay_bin:
            try:
                out = subprocess.check_output(
                    [aplay_bin, "--version"],
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=1.5,
                )
                m_ap = re.search(r"version\s+([0-9][0-9A-Za-z._-]*)", out)
                if m_ap:
                    aplay_ver = m_ap.group(1)
            except Exception:
                pass

        kernel_alsa = ""
        if os.path.isfile("/proc/asound/version"):
            try:
                with open("/proc/asound/version", "r", encoding="utf-8", errors="ignore") as fh:
                    k_line = fh.readline().strip().rstrip(".")
                m_k = re.search(r"Version\s+(\S+)", k_line)
                kernel_alsa = m_k.group(1) if m_k else k_line
            except Exception:
                pass

        alsa_parts: List[str] = []
        if libasound_ver:
            alsa_parts.append(f"libasound {libasound_ver}")
        if libatopology_ver:
            alsa_parts.append(f"libatopology {libatopology_ver}")
        if kernel_alsa:
            alsa_parts.append(f"Kernel ALSA {kernel_alsa}")
        alsa_ver = "  |  ".join(alsa_parts) if alsa_parts else "Unknown"

        utils_parts: List[str] = []
        if alsatplg_ver:
            utils_parts.append(f"alsatplg {alsatplg_ver}")
        if aplay_ver and aplay_ver != alsatplg_ver:
            utils_parts.append(f"aplay {aplay_ver}")
        elif aplay_ver and not alsatplg_ver:
            utils_parts.append(f"alsa-utils {aplay_ver}")
        if alsatplg_bin:
            utils_parts.append(f"({alsatplg_bin})")
        alsautils_ver = " ".join(utils_parts) if utils_parts else "Unknown"

        return {
            "tool": tool_ver,
            "sof": sof_ver,
            "alsa": alsa_ver,
            "alsautils": alsautils_ver,
        }

    def action_show_about(self) -> None:
        """Display the Help -> About dialog showing Tool, SOF, ALSA, and alsa-utils versions."""
        debug_log_action("HELP-ABOUT", "Open About dialog")
        vinfo = self._collect_version_info()

        dlg = Gtk.Window(
            title="About SOF Topology2 Studio",
            transient_for=self,
            modal=True,
            default_width=560,
            default_height=310,
            resizable=False,
        )
        self._about_dialog = dlg

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(18)
        outer.set_margin_bottom(18)
        outer.set_margin_start(22)
        outer.set_margin_end(22)

        title_lbl = Gtk.Label(label="SOF Topology2 Studio")
        title_lbl.add_css_class("heading")
        title_lbl.set_xalign(0.0)
        outer.append(title_lbl)

        sub_lbl = Gtk.Label(
            label="Visual Graph Editor, IPC4 Topology Compiler & Real-Time SOF Testbench"
        )
        sub_lbl.set_xalign(0.0)
        outer.append(sub_lbl)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        outer.append(sep)

        grid = Gtk.Grid(column_spacing=16, row_spacing=10)
        rows = [
            ("Tool Version:", vinfo["tool"]),
            ("SOF Version:", vinfo["sof"]),
            ("ALSA Version:", vinfo["alsa"]),
            ("alsa-utils Version:", vinfo["alsautils"]),
        ]
        for r_idx, (k_txt, val_txt) in enumerate(rows):
            k_lbl = Gtk.Label(label=k_txt)
            k_lbl.set_xalign(0.0)
            k_lbl.add_css_class("heading")
            v_lbl = Gtk.Label(label=val_txt)
            v_lbl.set_xalign(0.0)
            v_lbl.set_selectable(True)
            v_lbl.set_wrap(True)
            v_lbl.set_hexpand(True)
            grid.attach(k_lbl, 0, r_idx, 1, 1)
            grid.attach(v_lbl, 1, r_idx, 1, 1)

        outer.append(grid)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)
        btn_row.set_margin_top(10)
        close_btn = Gtk.Button(label="Close")
        close_btn.add_css_class("suggested-action")
        close_btn.connect("clicked", lambda _b: dlg.close())
        btn_row.append(close_btn)
        outer.append(btn_row)

        dlg.set_child(outer)
        dlg.present()

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

        # Left side: Unified Menubar (File, Edit, View, Target, Help)
        self.menubar = self._build_menubar()
        header.pack_start(self.menubar)

        # Right header controls: Compile + Run in Testbench
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
        self.hpaned_outer.set_position(
            self._saved_hpaned_outer_pos if self._saved_hpaned_outer_pos is not None else 390
        )

        # Inner horizontal paned: [Middle Graph + Waveforms Panel (50%)] | [Right Notebook (25%)]
        self.hpaned_inner = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.hpaned_inner.set_wide_handle(True)
        self.hpaned_inner.set_resize_start_child(False)
        self.hpaned_inner.set_resize_end_child(True)
        self.hpaned_inner.set_position(
            self._saved_hpaned_inner_pos if self._saved_hpaned_inner_pos is not None else 780
        )

        # 1. Left Hand Panel (Object View, File View & Compile notebook) wrapped in high-level panel border
        self.left_panel = LeftNavigationPanel(
            on_item_selected=self._on_left_pane_item_selected,
            on_add_module_requested=self._on_add_module_requested,
            on_add_pipeline_requested=self._on_add_pipeline_requested,
            on_view_source_requested=self._on_view_source_requested,
        )
        self.left_panel.vpaned_main.set_position(self._saved_left_vpaned_main_pos)
        self.left_panel.vpaned_top.set_position(self._saved_left_vpaned_top_pos)
        self.left_panel.vpaned_bot.set_position(self._saved_left_vpaned_bot_pos)
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
        self.center_vpaned.set_resize_start_child(False)
        self.center_vpaned.set_resize_end_child(True)
        self.center_vpaned.set_position(
            self._saved_center_vpaned_pos if self._saved_center_vpaned_pos is not None else 580
        )

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

        # Page 2 of Bottom Waveform Notebook: Scheduling & Module Buffer Telemetry Graph
        sched_pane_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        sched_pane_box.set_margin_start(4)
        sched_pane_box.set_margin_end(4)
        sched_pane_box.set_margin_top(3)
        sched_pane_box.set_margin_bottom(3)

        sched_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.sched_status_lbl = Gtk.Label(
            label="Scheduling (Y: Core ID × X: Time) & Module Buffers [IDLE]",
            xalign=0.0,
        )
        self.sched_status_lbl.add_css_class("heading")
        self.sched_status_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.sched_status_lbl.set_hexpand(True)
        sched_toolbar.append(self.sched_status_lbl)

        self.sched_expanded_btn = Gtk.ToggleButton(label="🔬 Expanded Slices")
        self.sched_expanded_btn.set_active(True)
        self.sched_expanded_btn.set_tooltip_text(
            "Magnify sub-millisecond pipeline & module execution slices within each 1ms LL tick for readability "
            "(uncheck for True 1:1 microsecond scale)"
        )
        self.sched_expanded_btn.connect("toggled", self._on_sched_expanded_toggled)
        sched_toolbar.append(self.sched_expanded_btn)

        self.sched_ignore_tick0_chk = Gtk.CheckButton.new_with_label("Ignore Tick 0 Underrun/Stall")
        self.sched_ignore_tick0_chk.set_active(True)
        self.sched_ignore_tick0_chk.set_tooltip_text(
            "Ignore Tick 0 / startup pin buffer underrun and stall violations when the DSP has no input yet"
        )
        self.sched_ignore_tick0_chk.connect("toggled", self._on_sched_ignore_tick0_toggled)
        sched_toolbar.append(self.sched_ignore_tick0_chk)

        self.sched_zoom_lbl = Gtk.Label(label="Win: 10.00/10.0 ms (1.0×, Expanded Slices)")
        self.sched_zoom_lbl.add_css_class("monospace")
        sched_toolbar.append(self.sched_zoom_lbl)

        btn_sched_zout = Gtk.Button(label="🔍−")
        btn_sched_zout.set_tooltip_text("Zoom Out scheduling time axis (or Mouse Wheel Down on timeline)")
        btn_sched_zout.connect("clicked", lambda _b: self.sched_telemetry_widget.zoom_out_time())
        sched_toolbar.append(btn_sched_zout)

        btn_sched_zin = Gtk.Button(label="🔍+")
        btn_sched_zin.set_tooltip_text("Zoom In scheduling time axis (or Mouse Wheel Up on timeline)")
        btn_sched_zin.connect("clicked", lambda _b: self.sched_telemetry_widget.zoom_in_time())
        sched_toolbar.append(btn_sched_zin)

        btn_sched_zfit = Gtk.Button(label="1:1 Fit")
        btn_sched_zfit.set_tooltip_text("Reset scheduling timeline zoom & pan (or Double-Click timeline)")
        btn_sched_zfit.connect("clicked", lambda _b: self.sched_telemetry_widget.reset_zoom())
        sched_toolbar.append(btn_sched_zfit)

        btn_sched_refresh = Gtk.Button(label="⟳ Refresh")
        btn_sched_refresh.set_tooltip_text("Poll live scheduling & buffer telemetry from sof-testbench4")
        btn_sched_refresh.connect("clicked", self._on_refresh_telemetry_clicked)
        sched_toolbar.append(btn_sched_refresh)

        sched_pane_box.append(sched_toolbar)

        sched_body_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        sched_body_box.set_hexpand(True)
        sched_body_box.set_vexpand(True)

        self.sched_telemetry_widget = SchedulingTelemetryWidget(
            on_zoom_changed=self._on_sched_zoom_changed,
            on_violation_clicked=self._on_sched_violation_clicked,
        )
        self.sched_telemetry_widget.set_size_request(-1, 185)
        sched_body_box.append(self.sched_telemetry_widget)

        self.sched_vscrollbar = Gtk.Scrollbar.new(
            Gtk.Orientation.VERTICAL,
            self.sched_telemetry_widget.v_adj,
        )
        self.sched_vscrollbar.set_vexpand(True)
        self.sched_vscrollbar.set_tooltip_text(
            "Scroll vertically through module buffer statistics rows"
        )
        sched_body_box.append(self.sched_vscrollbar)

        sched_pane_box.append(sched_body_box)

        self.waveform_notebook = Gtk.Notebook()
        self.waveform_notebook.set_hexpand(True)
        self.waveform_notebook.set_vexpand(True)
        self.waveform_notebook.set_show_border(False)
        self.waveform_notebook.append_page(
            wf_pane_box,
            Gtk.Label(label="📈 Waveforms"),
        )
        self.waveform_notebook.append_page(
            sched_pane_box,
            Gtk.Label(label="⏱ Scheduling & Buffers"),
        )

        wf_frame = Gtk.Frame()
        wf_frame.add_css_class("sub-panel-frame")
        wf_frame.set_child(self.waveform_notebook)
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
            "Hot-reload compiled topologies via 'tplg-load' & 'io-cfg' without restarting sof-testbench4"
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

        # Right Notebook Tab 4: Graphical & Searchable Telemetry Log TreeView Inspector
        self.telemetry_log_page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.telemetry_log_page_box.set_margin_start(4)
        self.telemetry_log_page_box.set_margin_end(4)
        self.telemetry_log_page_box.set_margin_top(4)
        self.telemetry_log_page_box.set_margin_bottom(4)

        vlog_hdr_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.telemetry_log_title_lbl = Gtk.Label(
            label="Telemetry Log: Click any violation or run Testbench to inspect structured telemetry."
        )
        self.telemetry_log_title_lbl.set_xalign(0.0)
        self.telemetry_log_title_lbl.set_hexpand(True)
        self.telemetry_log_title_lbl.set_wrap(True)
        self.telemetry_log_title_lbl.add_css_class("heading")
        vlog_hdr_box.append(self.telemetry_log_title_lbl)

        btn_vlog_all = Gtk.Button(label="Show All")
        btn_vlog_all.set_tooltip_text("Reset filters and show full telemetry & violation tree across all sections")
        btn_vlog_all.connect("clicked", self._on_telemetry_log_show_all_clicked)
        vlog_hdr_box.append(btn_vlog_all)
        self.telemetry_log_page_box.append(vlog_hdr_box)

        # 1. Top KPI Summary Cards Strip
        self.telemetry_kpi_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.telemetry_kpi_box.set_homogeneous(True)

        def _make_kpi_badge(default_txt: str) -> Gtk.Label:
            fr = Gtk.Frame()
            lbl = Gtk.Label(label=default_txt)
            lbl.set_margin_start(6)
            lbl.set_margin_end(6)
            lbl.set_margin_top(3)
            lbl.set_margin_bottom(3)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            lbl.add_css_class("caption")
            fr.set_child(lbl)
            self.telemetry_kpi_box.append(fr)
            return lbl

        self.kpi_viol_lbl = _make_kpi_badge("🟢 0 Violations")
        self.kpi_core_lbl = _make_kpi_badge("⚡ Cores: Idle")
        self.kpi_kcps_lbl = _make_kpi_badge("🧩 Modules: 0 (0 KCPS)")
        self.kpi_mem_lbl = _make_kpi_badge("💾 SRAM / Ticks: 0")
        self.telemetry_log_page_box.append(self.telemetry_kpi_box)

        # 2. Search Entry + Expand/Collapse Toolbar
        vlog_search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.telemetry_search_entry = Gtk.SearchEntry()
        self.telemetry_search_entry.set_placeholder_text(
            "Search modules, pins, buffers, violations, cores, kcontrols…"
        )
        self.telemetry_search_entry.set_hexpand(True)
        self.telemetry_search_entry.connect("search-changed", self._on_telemetry_search_changed)
        vlog_search_row.append(self.telemetry_search_entry)

        btn_tree_exp = Gtk.Button(label="⊞ Expand")
        btn_tree_exp.set_tooltip_text("Expand all telemetry tree sections")
        btn_tree_exp.connect("clicked", lambda _b: self.telemetry_log_tree.expand_all())
        vlog_search_row.append(btn_tree_exp)

        btn_tree_col = Gtk.Button(label="⊟ Collapse")
        btn_tree_col.set_tooltip_text("Collapse all telemetry tree sections")
        btn_tree_col.connect("clicked", lambda _b: self.telemetry_log_tree.collapse_all())
        vlog_search_row.append(btn_tree_col)
        self.telemetry_log_page_box.append(vlog_search_row)

        # 3. Quick-Filter Category Pills
        vlog_filter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._telemetry_category_filter: str = "all"
        self._telemetry_filter_btns: Dict[str, Gtk.ToggleButton] = {}
        self._updating_telemetry_filter_btns: bool = False
        for cat_key, cat_lbl in (
            ("all", "All Sections"),
            ("violations", "⚠ Violations"),
            ("modules", "🧩 Modules & Pins"),
            ("cores_pipes", "⚡ Cores & Pipes"),
            ("system", "💾 Power & Kcontrols"),
        ):
            tbtn = Gtk.ToggleButton(label=cat_lbl)
            tbtn.set_active(cat_key == "all")
            tbtn.add_css_class("caption")
            tbtn.connect("toggled", self._on_telemetry_category_pill_toggled, cat_key)
            vlog_filter_row.append(tbtn)
            self._telemetry_filter_btns[cat_key] = tbtn
        self.telemetry_log_page_box.append(vlog_filter_row)

        # 4. Multi-Column Graphical Gtk.TreeView with Gtk.CellRendererProgress
        # Columns:
        #   0: Item / Hierarchy (str)
        #   1: Status / Metric (str)
        #   2: Utilization / Fill % (int 0..100)
        #   3: Progress Bar Text (str)
        #   4: Progress Bar Visible (bool)
        #   5: Details / Connected Peers / Timing (str)
        #   6: Foreground color hex (str)
        #   7: Background color hex (str)
        #   8: Background color set (bool)
        #   9: Font weight (int)
        #  10: Row action key (str)
        self.telemetry_log_store = Gtk.TreeStore.new(
            [str, str, int, str, bool, str, str, str, bool, int, str]
        )
        self.telemetry_log_tree = Gtk.TreeView.new_with_model(self.telemetry_log_store)
        self.telemetry_log_tree.set_headers_visible(True)
        self.telemetry_log_tree.set_enable_tree_lines(True)
        self.telemetry_log_tree.set_activate_on_single_click(False)
        self._telemetry_tree_row_meta: Dict[str, Dict[str, Any]] = {}
        self._syncing_telemetry_tree_selection: bool = False

        # Column 0: Telemetry Section / Module / Pin / Violation
        r_t_item = Gtk.CellRendererText()
        r_t_item.set_padding(4, 3)
        col_t_item = Gtk.TreeViewColumn(
            "Section / Module / Pin",
            r_t_item,
            text=0,
            foreground=6,
            cell_background=7,
            cell_background_set=8,
            weight=9,
        )
        col_t_item.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        col_t_item.set_resizable(True)
        self.telemetry_log_tree.append_column(col_t_item)

        # Column 1: Status / Primary Metric
        r_t_stat = Gtk.CellRendererText()
        r_t_stat.set_padding(4, 3)
        col_t_stat = Gtk.TreeViewColumn(
            "Status / Value",
            r_t_stat,
            text=1,
            foreground=6,
            cell_background=7,
            cell_background_set=8,
            weight=9,
        )
        col_t_stat.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        col_t_stat.set_resizable(True)
        self.telemetry_log_tree.append_column(col_t_stat)

        # Column 2: Graphical Load / Budget / Buffer Fill Progress Bar
        r_t_prog = Gtk.CellRendererProgress()
        r_t_prog.set_fixed_size(110, 18)
        col_t_prog = Gtk.TreeViewColumn(
            "Budget / Fill %",
            r_t_prog,
            value=2,
            text=3,
            visible=4,
            cell_background=7,
            cell_background_set=8,
        )
        col_t_prog.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        col_t_prog.set_fixed_width(120)
        col_t_prog.set_resizable(True)
        self.telemetry_log_tree.append_column(col_t_prog)

        # Column 3: Details / Connected Peers / Timing
        r_t_det = Gtk.CellRendererText()
        r_t_det.set_padding(4, 3)
        col_t_det = Gtk.TreeViewColumn(
            "Details / Connected Peers / Timing",
            r_t_det,
            text=5,
            foreground=6,
            cell_background=7,
            cell_background_set=8,
        )
        col_t_det.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        col_t_det.set_resizable(True)
        col_t_det.set_expand(True)
        self.telemetry_log_tree.append_column(col_t_det)

        t_sel = self.telemetry_log_tree.get_selection()
        t_sel.set_mode(Gtk.SelectionMode.SINGLE)
        t_sel.connect("changed", self._on_telemetry_tree_selection_changed)
        self.telemetry_log_tree.connect("row-activated", self._on_telemetry_tree_row_activated)

        vlog_tree_scroll = Gtk.ScrolledWindow()
        vlog_tree_scroll.set_vexpand(True)
        vlog_tree_scroll.set_hexpand(True)
        vlog_tree_scroll.set_child(self.telemetry_log_tree)
        self.telemetry_log_page_box.append(vlog_tree_scroll)

        # 5. Collapsible Raw Shell & Protocol Log Expander at bottom
        self.telemetry_raw_expander = Gtk.Expander(
            label="📜 Raw Shell & Protocol Log Output (sched-telem / mod-buffers)"
        )
        self.telemetry_raw_expander.set_expanded(False)
        vlog_scroll = Gtk.ScrolledWindow()
        vlog_scroll.set_size_request(-1, 140)
        vlog_scroll.set_hexpand(True)
        self.telemetry_log_view = Gtk.TextView()
        self.telemetry_log_view.set_editable(False)
        self.telemetry_log_view.set_monospace(True)
        self.telemetry_log_view.set_wrap_mode(Gtk.WrapMode.NONE)
        self._init_telemetry_log_tags()
        vlog_scroll.set_child(self.telemetry_log_view)
        self.telemetry_raw_expander.set_child(vlog_scroll)
        self.telemetry_log_page_box.append(self.telemetry_raw_expander)
        self._focused_violation_info: Optional[Dict[str, Any]] = None

        self.right_notebook.append_page(
            self.telemetry_log_page_box,
            Gtk.Label(label="Telemetry Log"),
        )

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
        self.hpaned_outer.connect("notify::position", self._on_main_paned_position_changed)
        self.hpaned_inner.connect("notify::position", self._on_main_paned_position_changed)
        self.center_vpaned.connect("notify::position", self._on_main_paned_position_changed)
        self.left_panel.vpaned_main.connect("notify::position", self._on_left_paned_position_changed)
        self.left_panel.vpaned_top.connect("notify::position", self._on_left_paned_position_changed)
        self.left_panel.vpaned_bot.connect("notify::position", self._on_left_paned_position_changed)
        self._apply_saved_treeview_column_widths()
        for tv_name, tv in self._get_named_treeviews().items():
            for col_idx, col in enumerate(tv.get_columns()):
                col.connect("notify::width", self._on_treeview_column_width_changed, tv_name, col_idx)
                col.connect("notify::fixed-width", self._on_treeview_column_width_changed, tv_name, col_idx)
        self.hpaned_outer.add_tick_callback(self._on_outer_paned_tick)

        self.set_child(self.hpaned_outer)

    def _get_named_treeviews(self) -> Dict[str, Gtk.TreeView]:
        return {
            "right_inspector": self.right_inspector.tree,
            "telemetry_log": self.telemetry_log_tree,
            "left_base": self.left_panel.base_tree,
            "left_mod": self.left_panel.mod_tree,
            "left_pipe": self.left_panel.pipe_tree,
            "left_top": self.left_panel.top_tree,
        }

    def _apply_saved_treeview_column_widths(self) -> None:
        if not self._saved_treeview_col_widths:
            return
        for tv_name, tv in self._get_named_treeviews().items():
            widths = self._saved_treeview_col_widths.get(tv_name)
            if not isinstance(widths, list):
                continue
            for col, w in zip(tv.get_columns(), widths):
                if isinstance(w, (int, float)) and int(w) > 20:
                    col.set_expand(False)
                    col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
                    col.set_fixed_width(int(w))

    def _capture_treeview_column_widths(self, only_tv_name: Optional[str] = None) -> None:
        named_tvs = self._get_named_treeviews()
        targets = (
            [(only_tv_name, named_tvs[only_tv_name])]
            if only_tv_name and only_tv_name in named_tvs
            else list(named_tvs.items())
        )
        for tv_name, tv in targets:
            cols = tv.get_columns()
            prev = list(self._saved_treeview_col_widths.get(tv_name, []))
            while len(prev) < len(cols):
                prev.append(0)
            updated = False
            for idx, col in enumerate(cols):
                if col.get_sizing() == Gtk.TreeViewColumnSizing.FIXED and col.get_fixed_width() > 20:
                    w = int(col.get_fixed_width())
                else:
                    # Do not record auto-stretched width of the last column unless it is FIXED
                    if idx == len(cols) - 1 and len(cols) > 1:
                        w = int(col.get_fixed_width()) if col.get_fixed_width() > 20 else 0
                    else:
                        w = int(col.get_width())
                if w > 20 and prev[idx] != w:
                    prev[idx] = w
                    updated = True
            if any(x > 20 for x in prev) and (updated or tv_name not in self._saved_treeview_col_widths):
                self._saved_treeview_col_widths[tv_name] = prev

    def _on_treeview_column_width_changed(
        self,
        _col: Gtk.TreeViewColumn,
        _pspec: object,
        tv_name: str,
        _col_idx: int,
    ) -> None:
        if self._applying_programmatic_paned_pos or not self._paned_layout_ready:
            return
        self._capture_treeview_column_widths(only_tv_name=tv_name)
        self._schedule_ui_state_save()

    @staticmethod
    def _get_ui_state_path() -> str:
        override = os.environ.get("SOF_TPLG2_GUI_STATE_FILE")
        if override:
            return override
        cfg_dir = os.path.join(GLib.get_user_config_dir(), "sof-tplg2-gui")
        return os.path.join(cfg_dir, "ui_state.json")

    def _load_ui_state(self) -> None:
        """Load persisted window geometry and pane splitter positions from disk."""
        path = self._get_ui_state_path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data: Dict[str, Any] = json.load(fh)
            if not isinstance(data, dict):
                return
            w = int(data.get("window_width", self._ui_window_width))
            h = int(data.get("window_height", self._ui_window_height))
            if w > 300 and h > 200:
                self._ui_window_width = w
                self._ui_window_height = h
            if "window_maximized" in data:
                self._ui_window_maximized = bool(data["window_maximized"])
            self._saved_total_w = int(data.get("saved_total_w", 0))
            self._saved_total_h = int(data.get("saved_total_h", 0))
            if "hpaned_outer_pos" in data and data["hpaned_outer_pos"] is not None:
                self._saved_hpaned_outer_pos = max(80, int(data["hpaned_outer_pos"]))
            if "hpaned_inner_pos" in data and data["hpaned_inner_pos"] is not None:
                self._saved_hpaned_inner_pos = max(80, int(data["hpaned_inner_pos"]))
            if "center_vpaned_pos" in data and data["center_vpaned_pos"] is not None:
                self._saved_center_vpaned_pos = max(80, int(data["center_vpaned_pos"]))
            if "hpaned_outer_ratio" in data:
                self._hpaned_outer_ratio = max(0.05, min(0.90, float(data["hpaned_outer_ratio"])))
            if "hpaned_inner_ratio" in data:
                self._hpaned_inner_ratio = max(0.05, min(0.90, float(data["hpaned_inner_ratio"])))
            if "center_vpaned_ratio" in data:
                self._center_vpaned_ratio = max(0.10, min(0.90, float(data["center_vpaned_ratio"])))
            if "left_vpaned_main_pos" in data:
                self._saved_left_vpaned_main_pos = max(60, int(data["left_vpaned_main_pos"]))
            if "left_vpaned_top_pos" in data:
                self._saved_left_vpaned_top_pos = max(40, int(data["left_vpaned_top_pos"]))
            if "left_vpaned_bot_pos" in data:
                self._saved_left_vpaned_bot_pos = max(40, int(data["left_vpaned_bot_pos"]))
            tv_cols = data.get("treeview_column_widths")
            if isinstance(tv_cols, dict):
                cleaned_cols: Dict[str, List[int]] = {}
                for k, v in tv_cols.items():
                    if isinstance(k, str) and isinstance(v, list):
                        cleaned_cols[k] = [max(0, int(x)) for x in v if isinstance(x, (int, float))]
                self._saved_treeview_col_widths = cleaned_cols
            open_tabs = data.get("open_tabs")
            if isinstance(open_tabs, list):
                self._saved_open_tabs = [t for t in open_tabs if isinstance(t, dict)]
            if "active_tab_key" in data and isinstance(data["active_tab_key"], str):
                self._saved_active_tab_key = data["active_tab_key"]
        except Exception as exc:
            debug_log_line("UI-STATE", f"Failed to load '{path}': {exc}")

    def _capture_open_tabs_state(self) -> None:
        """Record the currently open Main Graph tabs and active tab key for session persistence."""
        if self._restoring_session_tabs:
            return
        if not hasattr(self, "graph_notebook") or self.graph_notebook is None:
            return
        ordered_tabs: List[GraphTabPage] = []
        n_pages = self.graph_notebook.get_n_pages()
        for i in range(n_pages):
            page_w = self.graph_notebook.get_nth_page(i)
            for t in self.graph_tabs:
                if t.view_stack is page_w or t.canvas is page_w:
                    ordered_tabs.append(t)
                    break
        for t in self.graph_tabs:
            if t not in ordered_tabs:
                ordered_tabs.append(t)

        tabs_list: List[Dict[str, Any]] = []
        for t in ordered_tabs:
            if t.is_untitled and (not t.canvas.vm or not t.canvas.vm.source_file):
                continue
            enabled_tb_io_ids: List[str] = []
            if t.canvas.vm is not None:
                enabled_tb_io_ids = [
                    nid
                    for nid, n in t.canvas.vm.nodes.items()
                    if n.class_category == "TestbenchIO"
                    and str(n.config.get_scalar("enabled", "false")).lower() == "true"
                ]
            tabs_list.append(
                {
                    "tab_key": t.tab_key,
                    "pane_kind": t.pane_kind,
                    "primary_id": t.primary_id,
                    "secondary_id": t.secondary_id,
                    "source_file": t.canvas.vm.source_file if t.canvas.vm else "",
                    "text_mode": bool(t.mode_switch_btn.get_active()),
                    "enabled_tb_io_ids": enabled_tb_io_ids,
                }
            )
        self._saved_open_tabs = tabs_list
        active = self.get_active_tab()
        self._saved_active_tab_key = active.tab_key if active else ""

    def _save_ui_state(self) -> None:
        """Persist current window geometry, pane splitter positions, treeview column widths, and open tabs to disk."""
        path = self._get_ui_state_path()
        try:
            win_w = self.get_width()
            win_h = self.get_height()
            if win_w > 300 and win_h > 200 and not self.is_maximized():
                self._ui_window_width = win_w
                self._ui_window_height = win_h
            self._ui_window_maximized = bool(self.is_maximized())
            if not self._restoring_session_tabs and hasattr(self, "graph_notebook"):
                self._capture_open_tabs_state()

            payload: Dict[str, Any] = {
                "window_width": self._ui_window_width,
                "window_height": self._ui_window_height,
                "window_maximized": self._ui_window_maximized,
                "saved_total_w": self._saved_total_w,
                "saved_total_h": self._saved_total_h,
                "hpaned_outer_pos": self._saved_hpaned_outer_pos,
                "hpaned_inner_pos": self._saved_hpaned_inner_pos,
                "center_vpaned_pos": self._saved_center_vpaned_pos,
                "hpaned_outer_ratio": round(self._hpaned_outer_ratio, 6),
                "hpaned_inner_ratio": round(self._hpaned_inner_ratio, 6),
                "center_vpaned_ratio": round(self._center_vpaned_ratio, 6),
                "left_vpaned_main_pos": self._saved_left_vpaned_main_pos,
                "left_vpaned_top_pos": self._saved_left_vpaned_top_pos,
                "left_vpaned_bot_pos": self._saved_left_vpaned_bot_pos,
                "treeview_column_widths": self._saved_treeview_col_widths,
                "open_tabs": self._saved_open_tabs,
                "active_tab_key": self._saved_active_tab_key,
            }
            parent_dir = os.path.dirname(path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
        except Exception as exc:
            debug_log_line("UI-STATE", f"Failed to save '{path}': {exc}")

    def _schedule_ui_state_save(self) -> None:
        if self._save_ui_state_timer_id is not None:
            return
        self._save_ui_state_timer_id = GLib.timeout_add(250, self._flush_ui_state_save)

    def _flush_ui_state_save(self) -> bool:
        self._save_ui_state_timer_id = None
        self._save_ui_state()
        return False

    def _on_main_paned_position_changed(self, _paned: Gtk.Paned, _pspec: object) -> None:
        if self._applying_programmatic_paned_pos or not self._paned_layout_ready:
            return
        total_w = self.hpaned_outer.get_width()
        total_h = self.hpaned_outer.get_height()
        if total_w <= 100 or total_h <= 100:
            return
        if total_w != self._last_allocated_width or total_h != self._last_allocated_height:
            return
        outer_pos = self.hpaned_outer.get_position()
        inner_pos = self.hpaned_inner.get_position()
        vpaned_pos = self.center_vpaned.get_position()
        if outer_pos <= 0 or inner_pos <= 0 or vpaned_pos <= 0:
            return
        self._saved_hpaned_outer_pos = outer_pos
        self._saved_hpaned_inner_pos = inner_pos
        self._saved_center_vpaned_pos = vpaned_pos
        self._saved_total_w = total_w
        self._saved_total_h = total_h
        self._hpaned_outer_ratio = max(0.05, min(0.90, outer_pos / float(total_w)))
        self._hpaned_inner_ratio = max(0.05, min(0.90, inner_pos / float(total_w)))
        self._center_vpaned_ratio = max(0.10, min(0.90, vpaned_pos / float(total_h)))
        self._schedule_ui_state_save()

    def _on_left_paned_position_changed(self, _paned: Gtk.Paned, _pspec: object) -> None:
        if self._applying_programmatic_paned_pos or not self._paned_layout_ready:
            return
        main_pos = self.left_panel.vpaned_main.get_position()
        top_pos = self.left_panel.vpaned_top.get_position()
        bot_pos = self.left_panel.vpaned_bot.get_position()
        if main_pos > 0:
            self._saved_left_vpaned_main_pos = main_pos
        if top_pos > 0:
            self._saved_left_vpaned_top_pos = top_pos
        if bot_pos > 0:
            self._saved_left_vpaned_bot_pos = bot_pos
        self._schedule_ui_state_save()

    def _on_outer_paned_tick(self, widget: Gtk.Widget, _frame_clock: object) -> bool:
        """Restore persisted pane splitter positions (or 25%/50%/68% defaults) when the window allocates/resizes."""
        total_w = widget.get_width()
        total_h = widget.get_height()
        if total_w > 100 and total_h > 100 and (
            total_w != self._last_allocated_width or total_h != self._last_allocated_height
        ):
            self._last_allocated_width = total_w
            self._last_allocated_height = total_h
            self._applying_programmatic_paned_pos = True
            self._paned_sync_seq += 1
            sync_seq = self._paned_sync_seq

            if self._saved_hpaned_outer_pos is not None and self._saved_total_w == total_w:
                target_left = self._saved_hpaned_outer_pos
            else:
                target_left = int(round(total_w * self._hpaned_outer_ratio))

            if self._saved_hpaned_inner_pos is not None and self._saved_total_w == total_w:
                target_center = self._saved_hpaned_inner_pos
            else:
                target_center = int(round(total_w * self._hpaned_inner_ratio))

            if self._saved_center_vpaned_pos is not None and self._saved_total_h == total_h:
                target_center_vpos = self._saved_center_vpaned_pos
            else:
                target_center_vpos = int(round(total_h * self._center_vpaned_ratio))

            target_left = max(80, min(max(80, total_w - 160), target_left))
            target_center = max(80, min(max(80, total_w - target_left - 80), target_center))
            target_center_vpos = max(80, min(max(80, total_h - 80), target_center_vpos))

            GLib.idle_add(
                self._sync_inner_paned_position,
                target_left,
                target_center,
                target_center_vpos,
                0,
                sync_seq,
            )
        return GLib.SOURCE_CONTINUE

    def _sync_inner_paned_position(
        self,
        target_left: int,
        target_center: int,
        target_center_vpos: int,
        pass_idx: int = 0,
        sync_seq: int = 0,
    ) -> bool:
        if sync_seq and sync_seq != self._paned_sync_seq:
            return False
        self._applying_programmatic_paned_pos = True
        self.hpaned_outer.set_position(target_left)
        self.hpaned_inner.set_position(target_center)
        self.center_vpaned.set_position(target_center_vpos)
        self.left_panel.vpaned_main.set_position(self._saved_left_vpaned_main_pos)
        self.left_panel.vpaned_top.set_position(self._saved_left_vpaned_top_pos)
        self.left_panel.vpaned_bot.set_position(self._saved_left_vpaned_bot_pos)
        self._apply_saved_treeview_column_widths()
        if pass_idx == 0:
            GLib.timeout_add(
                25,
                self._sync_inner_paned_position,
                target_left,
                target_center,
                target_center_vpos,
                1,
                sync_seq,
            )
        else:
            self.canvas.queue_draw()
            GLib.timeout_add(25, self._finish_programmatic_paned_sync, sync_seq)
        return False

    def _finish_programmatic_paned_sync(self, sync_seq: int) -> bool:
        if sync_seq == self._paned_sync_seq:
            self._applying_programmatic_paned_pos = False
            self._paned_layout_ready = True
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

        if not self._restore_saved_session_tabs():
            # Select default topology (`sof-hda-generic` if present, else first pipeline)
            hda = next((m for m in ws.machine_topologies if m.name == "sof-hda-generic"), None)
            if hda:
                self._on_left_pane_item_selected("topology", hda.conf_path, "sof-hda-generic")
            elif ws.pipeline_classes:
                first_p = sorted(ws.pipeline_classes.keys())[0]
                self._on_left_pane_item_selected("pipeline", first_p, None)

    def _restore_saved_session_tabs(self) -> bool:
        """Re-open the Main Graph tabs that were open in the previous session and select the active tab."""
        if not self._saved_open_tabs or not self.workspace:
            return False
        saved_tabs = list(self._saved_open_tabs)
        saved_active_key = self._saved_active_tab_key
        self._restoring_session_tabs = True
        opened_any = False
        try:
            for tinfo in saved_tabs:
                tab_key = str(tinfo.get("tab_key", ""))
                pane_kind = str(tinfo.get("pane_kind", ""))
                primary_id = str(tinfo.get("primary_id", ""))
                secondary_id = tinfo.get("secondary_id") or None
                source_file = str(tinfo.get("source_file", ""))
                text_mode = bool(tinfo.get("text_mode", False))
                enabled_tb_io_ids = tinfo.get("enabled_tb_io_ids")

                tab: Optional[GraphTabPage] = None
                if tab_key.startswith("source:"):
                    tab = self.open_source_in_tab(pane_kind or "file", primary_id or source_file, secondary_id)
                elif tab_key.startswith("file:"):
                    fpath = primary_id if (primary_id and os.path.isfile(primary_id)) else source_file
                    if fpath and os.path.isfile(fpath):
                        tab = self.open_conf_file_in_tab(fpath)
                elif pane_kind in ("base_class", "module", "pipeline", "topology"):
                    if pane_kind == "topology" and (not primary_id or not os.path.isfile(primary_id)):
                        bname = os.path.basename(primary_id or source_file)
                        match = next(
                            (
                                m
                                for m in self.workspace.machine_topologies
                                if os.path.basename(m.conf_path) == bname or m.name == secondary_id
                            ),
                            None,
                        )
                        if match:
                            primary_id = match.conf_path
                    prev_count = len(self.graph_tabs)
                    self._on_left_pane_item_selected(pane_kind, primary_id, secondary_id)
                    if len(self.graph_tabs) > prev_count:
                        tab = self.graph_tabs[-1]
                    else:
                        expected_key = f"{pane_kind}:{primary_id}:{secondary_id or ''}"
                        tab = next((t for t in self.graph_tabs if t.tab_key == expected_key), None)
                elif source_file and os.path.isfile(source_file):
                    tab = self.open_conf_file_in_tab(source_file)

                if tab is not None:
                    opened_any = True
                    if isinstance(enabled_tb_io_ids, list) and enabled_tb_io_ids and tab.canvas.vm:
                        tb_id_set = {str(x) for x in enabled_tb_io_ids if x}
                        has_match = any(
                            nid in tb_id_set
                            for nid, n in tab.canvas.vm.nodes.items()
                            if n.class_category == "TestbenchIO"
                        )
                        if has_match:
                            for nid, n in tab.canvas.vm.nodes.items():
                                if n.class_category == "TestbenchIO":
                                    n.config.set_scalar(
                                        "enabled",
                                        "true" if nid in tb_id_set else "false",
                                        origin="instance",
                                    )
                    if text_mode:
                        tab.mode_switch_btn.set_active(True)
        finally:
            self._restoring_session_tabs = False

        if opened_any and self.graph_tabs:
            target_tab = next(
                (t for t in self.graph_tabs if t.tab_key == saved_active_key),
                None,
            )
            if target_tab is None and saved_active_key:
                # Match by primary_id + secondary_id in case workspace path changed
                target_tab = next(
                    (
                        t
                        for t in self.graph_tabs
                        if f"{t.pane_kind}:{os.path.basename(t.primary_id)}:{t.secondary_id or ''}"
                        == f"{saved_active_key.split(':', 1)[0]}:{os.path.basename(saved_active_key.split(':', 2)[1]) if ':' in saved_active_key else ''}:{saved_active_key.split(':', 2)[2] if saved_active_key.count(':') >= 2 else ''}"
                    ),
                    None,
                )
            if target_tab is None:
                target_tab = self.graph_tabs[-1]
            pnum = self.graph_notebook.page_num(target_tab.view_stack)
            if pnum >= 0:
                self.graph_notebook.set_current_page(pnum)
            self._sync_ui_to_tab(target_tab)
            self._capture_open_tabs_state()
            return True
        return False

    @staticmethod
    def _format_tab_title(
        vm: Optional[GraphViewModel],
        fallback: str = "Untitled.conf",
        is_source_mode: bool = False,
    ) -> str:
        if vm is None:
            return fallback
        if is_source_mode:
            icon = "📄"
        else:
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
        is_src = tab.tab_key.startswith("source:") or tab.mode_switch_btn.get_active()
        tab.title_lbl.set_text(self._format_tab_title(vm, tab.primary_id, is_source_mode=is_src))
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
                if not self._restoring_session_tabs:
                    self._sync_ui_to_active_tab()
                    self._capture_open_tabs_state()
                    self._schedule_ui_state_save()
                return existing

        new_canvas = TopologyGraphCanvas(
            on_selection_changed=self._on_canvas_selection_changed,
            on_graph_modified=self._on_graph_modified,
            on_history_changed=self._update_undo_redo_buttons,
            on_kcontrol_changed=self._on_canvas_kcontrol_changed,
            on_probe_tap_toggled=self._on_canvas_probe_tap_toggled,
            on_testbench_io_changed=self._on_canvas_testbench_io_changed,
            on_view_source_requested=self._on_view_source_requested,
            on_violation_bang_clicked=self._on_canvas_violation_bang_clicked,
        )
        if hasattr(self, "sched_ignore_tick0_chk"):
            new_canvas.ignore_tick0_underrun = self.sched_ignore_tick0_chk.get_active()
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
        title_lbl = Gtk.Label(
            label=self._format_tab_title(vm, primary_id, is_source_mode=tab_key.startswith("source:"))
        )
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
        if not self._restoring_session_tabs:
            self._sync_ui_to_active_tab()
            self._capture_open_tabs_state()
            self._schedule_ui_state_save()
        return tab_page

    def _on_view_source_requested(
        self,
        pane_kind: str,
        primary_id: str,
        secondary_id: Optional[str] = None,
        search_hint: Optional[str] = None,
    ) -> None:
        """Handle 'View Source' right-click context menu action from Graph Canvas or Left Panel."""
        self.open_source_in_tab(pane_kind, primary_id, secondary_id, search_hint)

    def open_source_in_tab(
        self,
        pane_kind: str,
        primary_id: str,
        secondary_id: Optional[str] = None,
        search_hint: Optional[str] = None,
    ) -> Optional[GraphTabPage]:
        """Open a source editor tab in the Main Window notebook for the requested item/file,
        or switch to and display its existing notebook tab if the source file is already open.
        """
        ws = self._ensure_workspace()
        source_file = ""
        if pane_kind == "module" and primary_id in ws.processing_modules:
            source_file = ws.processing_modules[primary_id].source_file
        elif pane_kind == "pipeline" and primary_id in ws.pipeline_classes:
            source_file = ws.pipeline_classes[primary_id].source_file
        elif pane_kind == "base_class" and primary_id in ws.base_classes:
            source_file = ws.base_classes[primary_id].source_file
        elif pane_kind in ("topology", "file", "source") and primary_id:
            source_file = primary_id
        elif os.path.isfile(primary_id):
            source_file = primary_id

        abs_path = os.path.abspath(source_file) if source_file else ""
        source_tab_key = (
            f"source:{abs_path}"
            if abs_path
            else f"source:{pane_kind}:{primary_id}:{secondary_id or ''}"
        )

        # Check if a source tab (or a tab already in text/source mode) for this file is already open
        for existing in self.graph_tabs:
            existing_src = (
                os.path.abspath(existing.canvas.vm.source_file)
                if (existing.canvas.vm and existing.canvas.vm.source_file)
                else ""
            )
            if existing.tab_key == source_tab_key or (
                abs_path
                and existing_src == abs_path
                and (
                    existing.tab_key.startswith("source:")
                    or existing.view_stack.get_visible_child_name() == "text"
                    or existing.mode_switch_btn.get_active()
                )
            ):
                pnum = self.graph_notebook.page_num(existing.view_stack)
                if pnum >= 0:
                    self.graph_notebook.set_current_page(pnum)
                if not existing.mode_switch_btn.get_active():
                    existing.mode_switch_btn.set_active(True)
                if search_hint:
                    existing.text_editor.scroll_to_hint(search_hint)
                if not self._restoring_session_tabs:
                    self._sync_ui_to_tab(existing)
                    self._capture_open_tabs_state()
                    self._schedule_ui_state_save()
                self.status_lbl.set_text(
                    f"Switched to open source tab: {os.path.basename(abs_path or primary_id)}"
                )
                return existing

        try:
            if pane_kind == "base_class" and primary_id in ws.base_classes:
                vm = ws.build_graph_for_base_class(primary_id)
            elif pane_kind == "module" and primary_id in ws.processing_modules:
                vm = ws.build_graph_for_module(primary_id)
            elif pane_kind == "pipeline" and primary_id in ws.pipeline_classes:
                vm = ws.build_graph_for_pipeline(primary_id)
            elif pane_kind == "topology" and abs_path and os.path.isfile(abs_path):
                variant = None
                if secondary_id:
                    for entry in ws.machine_topologies:
                        if os.path.abspath(entry.conf_path) == abs_path:
                            variant = next((v for v in entry.variants if v.output_name == secondary_id), None)
                            break
                vm = ws.build_graph_for_topology(abs_path, variant)
            elif abs_path and os.path.isfile(abs_path):
                vm = ws.build_graph_for_arbitrary_conf_file(abs_path)
            else:
                return None

            tab = self._open_or_switch_graph_tab(
                tab_key=source_tab_key,
                pane_kind=pane_kind,
                primary_id=primary_id,
                secondary_id=secondary_id,
                vm=vm,
                is_untitled=False,
            )
            if not tab.mode_switch_btn.get_active():
                tab.mode_switch_btn.set_active(True)
            if search_hint:
                tab.text_editor.scroll_to_hint(search_hint)
            self.status_lbl.set_text(
                f"Opened source tab: {os.path.basename(abs_path or primary_id)}"
            )
            return tab
        except Exception as exc:
            self.append_log(f"Error opening source for {primary_id}: {exc}")
            self.status_lbl.set_text(f"Error opening source: {exc}")
            return None

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
            tab_page.title_lbl.set_text(
                self._format_tab_title(vm, tab_page.primary_id, is_source_mode=True)
            )
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
            tab_page.title_lbl.set_text(
                self._format_tab_title(
                    tab_page.canvas.vm,
                    tab_page.primary_id,
                    is_source_mode=tab_page.tab_key.startswith("source:"),
                )
            )
            if not self._restoring_session_tabs:
                self._sync_ui_to_tab(tab_page)
        if not self._restoring_session_tabs:
            self._capture_open_tabs_state()
            self._schedule_ui_state_save()

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
        self._capture_open_tabs_state()
        self._schedule_ui_state_save()
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
        if self._restoring_session_tabs:
            return
        for tab in self.graph_tabs:
            if tab.view_stack is page_widget or tab.canvas is page_widget:
                self._sync_ui_to_tab(tab)
                self._saved_active_tab_key = tab.tab_key
                self._schedule_ui_state_save()
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
        if hasattr(self, "sched_telemetry_widget"):
            tab_telem = tab.last_telemetry if tab.last_telemetry is not None else self.last_telemetry
            self.sched_telemetry_widget.set_telemetry(tab_telem)
            self._update_sched_status_label(tab_telem)
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
            self._capture_open_tabs_state()
            self._schedule_ui_state_save()
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

    def _get_ordered_active_io_keys(
        self,
        vm_override: Optional[GraphViewModel] = None,
    ) -> Tuple[List[str], List[str]]:
        """Return (active_in_keys, active_out_keys) for the currently enabled Input and Output PCMs
        in `vm_override` (or active vm).
        """
        vm = vm_override if vm_override is not None else self.canvas.vm
        if not vm:
            return (["in:__default__"], ["out:__default__"])
        enabled_sources, enabled_sinks = self._get_ordered_enabled_tb_io_nodes(vm)
        in_keys = (
            [f"in:{n.id}" for n in enabled_sources]
            if enabled_sources
            else ["in:__default__"]
        )
        if self.canvas.active_probe_node_id and self.canvas.active_probe_node_id in vm.nodes:
            out_keys = [f"out:__probe__:{self.canvas.active_probe_node_id}"]
        elif enabled_sinks:
            out_keys = [f"out:{n.id}" for n in enabled_sinks]
        else:
            out_keys = ["out:__default__"]
        return (in_keys, out_keys)

    def _get_ordered_active_stream_keys(
        self,
        vm_override: Optional[GraphViewModel] = None,
    ) -> List[str]:
        """Return ordered unique keys for each currently enabled Input and Output PCM in `vm_override`."""
        in_keys, out_keys = self._get_ordered_active_io_keys(vm_override)
        return in_keys + out_keys

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
            multi_in = len(enabled_sources) > 1
            multi_out = len(enabled_sinks) > 1

            for idx, snode in enumerate(enabled_sources):
                target_p = str(snode.config.get_scalar("target_pipelines", "")).strip()
                p_badge = f" (Pipes: {target_p})" if target_p else ""
                _sk, _ws, s_lbl = self._describe_tb_source_signal(snode)
                multi_sig_labels.append(s_lbl)
                if multi_in:
                    multi_in_labels.append(
                        f"IN {idx + 1}: {snode.title}{f' ({target_p})' if target_p else ''}"
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
                if multi_out:
                    multi_out_labels.append(
                        f"OUT {idx + 1}: {knode.title}{f' ({target_p})' if target_p else ''}"
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

        # Align displayed waveforms with the currently selected Input and Output PCMs on the active
        # Main Graph tab, removing any deselected PCM waveforms and updating when switching tabs.
        active_tab = self.get_active_tab()
        in_keys, out_keys = self._get_ordered_active_io_keys(vm)
        if active_tab is not None:
            active_key_set = set(in_keys) | set(out_keys)
            for k in list(active_tab.stream_waveforms.keys()):
                if k not in active_key_set:
                    del active_tab.stream_waveforms[k]

        aligned_in_samples: List[List[float]] = []
        for ikey in in_keys:
            if active_tab is not None and ikey in active_tab.stream_waveforms:
                s_in, _ = active_tab.stream_waveforms[ikey]
                aligned_in_samples.append(list(s_in))
            else:
                aligned_in_samples.append([])

        aligned_out_samples: List[List[float]] = []
        for okey in out_keys:
            if active_tab is not None and okey in active_tab.stream_waveforms:
                _, s_out = active_tab.stream_waveforms[okey]
                aligned_out_samples.append(list(s_out))
            else:
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
            n_in = max(1, len(aligned_in_samples))
            n_out = max(1, len(aligned_out_samples))
            has_any_out = any(len(s) > 0 for s in aligned_out_samples)
            io_tag = f" — {n_in} In / {n_out} Out" if (n_in > 1 or n_out > 1) else ""
            if has_any_out:
                self.wf_status_lbl.set_text(f"Waveforms [SNAPSHOT{io_tag}]")
            else:
                self.wf_status_lbl.set_text(f"Waveforms [IDLE{io_tag}]")

    def _on_waveform_zoom_changed(self) -> None:
        self.wf_zoom_lbl.set_text(self.waveform_widget.get_zoom_summary_text())

    def _on_sched_zoom_changed(self) -> None:
        if hasattr(self, "sched_zoom_lbl") and hasattr(self, "sched_telemetry_widget"):
            self.sched_zoom_lbl.set_text(self.sched_telemetry_widget.get_zoom_summary_text())

    def _on_sched_expanded_toggled(self, btn: Gtk.ToggleButton) -> None:
        if hasattr(self, "sched_telemetry_widget"):
            self.sched_telemetry_widget.set_expanded_tick_mode(btn.get_active())

    def _on_sched_ignore_tick0_toggled(self, chk: Gtk.CheckButton) -> None:
        ignore = bool(chk.get_active())
        if hasattr(self, "sched_telemetry_widget"):
            self.sched_telemetry_widget.set_ignore_tick0_underrun(ignore)
        if hasattr(self, "_fallback_canvas") and self._fallback_canvas is not None:
            self._fallback_canvas.ignore_tick0_underrun = ignore
        for tab in getattr(self, "graph_tabs", []):
            tab.canvas.ignore_tick0_underrun = ignore
        telem = getattr(self.sched_telemetry_widget, "telemetry", None) if hasattr(self, "sched_telemetry_widget") else None
        if telem is not None:
            if self.canvas is not None:
                self.canvas.set_live_telemetry_overlay(telem)
            self._populate_telemetry_log_view(telem, getattr(self, "_focused_violation_info", None))

    def _init_telemetry_log_tags(self) -> None:
        if not hasattr(self, "telemetry_log_view"):
            return
        buf = self.telemetry_log_view.get_buffer()
        tt = buf.get_tag_table()
        if tt.lookup("vlog_error") is not None:
            return
        buf.create_tag(
            "vlog_error",
            foreground="#ff6b6b",
            background="#3b1d24",
            weight=Pango.Weight.BOLD,
        )
        buf.create_tag(
            "vlog_focus",
            foreground="#ffffff",
            background="#681826",
            weight=Pango.Weight.BOLD,
        )
        buf.create_tag(
            "vlog_warning",
            foreground="#ffd166",
            weight=Pango.Weight.BOLD,
        )
        buf.create_tag(
            "vlog_header",
            foreground="#7ec8ff",
            weight=Pango.Weight.BOLD,
        )
        buf.create_tag(
            "vlog_ok",
            foreground="#5cd68d",
        )
        buf.create_tag(
            "vlog_dim",
            foreground="#94a3b8",
        )

    def _ensure_telemetry_log_page(self) -> int:
        """Ensure the Telemetry Log page exists in self.right_notebook and switch to it."""
        page_num = self.right_notebook.page_num(self.telemetry_log_page_box)
        if page_num < 0:
            page_num = self.right_notebook.append_page(
                self.telemetry_log_page_box,
                Gtk.Label(label="Telemetry Log"),
            )
            self.telemetry_log_page_box.set_visible(True)
        self.right_notebook.set_current_page(page_num)
        return page_num

    def _on_canvas_violation_bang_clicked(self, node_id: str, telem_info: Dict[str, Any]) -> None:
        """Called when the user clicks the red bang (!) badge on a violating module node in the topology graph."""
        mod_obj = telem_info.get("mod_obj")
        if mod_obj is not None and hasattr(self, "sched_telemetry_widget"):
            v_list = self.sched_telemetry_widget._collect_module_violations(mod_obj)
            if v_list:
                self._on_sched_violation_clicked(v_list[0])
                return
        v_fallback = {
            "v_key": f"mod:{telem_info.get('comp_id', 0)}:cpc",
            "type": "CPC_VIOLATION",
            "comp_id": telem_info.get("comp_id", 0),
            "pipe_id": telem_info.get("pipeline_id", 1),
            "core_id": telem_info.get("core_id", 0),
            "widget_name": telem_info.get("widget_name", node_id),
            "tick_idx": 0,
            "summary": (
                f"Violation on {node_id}: peak={telem_info.get('peak_cycles', 0)} cyc vs "
                f"CPC={telem_info.get('effective_cpc', 0)} cyc (v={telem_info.get('violation_count', 1)})"
            ),
            "mod": mod_obj,
        }
        self._on_sched_violation_clicked(v_fallback)

    def _on_sched_violation_clicked(self, violation_info: Dict[str, Any]) -> None:
        """Switch to the Scheduling Graph timeline, zoom to the violation, and focus the Telemetry Log page."""
        self._focused_violation_info = violation_info
        # 1. Switch bottom waveform_notebook to the Scheduling & Buffers timeline page and zoom to the violation
        if hasattr(self, "waveform_notebook"):
            self.waveform_notebook.set_current_page(1)
        if hasattr(self, "sched_telemetry_widget"):
            self.sched_telemetry_widget.focus_and_zoom_to_violation(violation_info)

        # 2. Select/highlight the violating module on the active Topology Graph Canvas if present
        wname = violation_info.get("widget_name", "")
        comp_id = violation_info.get("comp_id")
        if self.canvas and self.canvas.vm:
            for nid, gnode in self.canvas.vm.nodes.items():
                nt = self.canvas.live_node_telemetry.get(nid, {})
                if (comp_id and nt.get("comp_id") == comp_id) or (
                    wname and (nid == wname or gnode.title == wname or wname in nid)
                ):
                    self.canvas.select_node(nid)
                    break

        # 3. Ensure Telemetry Log notebook page is open and scrolled to the violation
        self._ensure_telemetry_log_page()
        telem = (
            self.sched_telemetry_widget.telemetry
            if hasattr(self, "sched_telemetry_widget") and self.sched_telemetry_widget.telemetry
            else self.last_telemetry
        )
        self._populate_telemetry_log_view(telem, violation_info)
        vtype = violation_info.get("type", "VIOLATION")
        summary = violation_info.get("summary", "")
        self.status_lbl.set_text(
            f"Zoomed Timeline & Telemetry Log → [{vtype}] {wname}: {summary}"
        )

    def _on_telemetry_log_show_all_clicked(self, _btn: Gtk.Button) -> None:
        """Reset search/category filters and show the full Telemetry Log TreeView."""
        self._focused_violation_info = None
        if hasattr(self, "telemetry_search_entry") and self.telemetry_search_entry.get_text():
            self.telemetry_search_entry.set_text("")
        if getattr(self, "_telemetry_category_filter", "all") != "all":
            self._set_telemetry_category_filter("all")
        telem = getattr(self, "_last_telemetry_log_data", None) or self.last_telemetry
        self._populate_telemetry_log_view(telem, None)

    def _set_telemetry_category_filter(self, cat_key: str) -> None:
        self._telemetry_category_filter = cat_key
        if not hasattr(self, "_telemetry_filter_btns"):
            return
        self._updating_telemetry_filter_btns = True
        try:
            for k, b in self._telemetry_filter_btns.items():
                b.set_active(k == cat_key)
        finally:
            self._updating_telemetry_filter_btns = False

    def _on_telemetry_category_pill_toggled(self, btn: Gtk.ToggleButton, cat_key: str) -> None:
        if getattr(self, "_updating_telemetry_filter_btns", False):
            return
        if not btn.get_active():
            if getattr(self, "_telemetry_category_filter", "all") == cat_key:
                self._set_telemetry_category_filter("all")
            else:
                return
        else:
            self._set_telemetry_category_filter(cat_key)
        telem = getattr(self, "_last_telemetry_log_data", None) or self.last_telemetry
        self._populate_telemetry_log_view(telem, getattr(self, "_focused_violation_info", None))

    def _on_telemetry_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        telem = getattr(self, "_last_telemetry_log_data", None) or self.last_telemetry
        self._populate_telemetry_log_view(telem, getattr(self, "_focused_violation_info", None))

    def _on_telemetry_tree_selection_changed(self, selection: Gtk.TreeSelection) -> None:
        if getattr(self, "_syncing_telemetry_tree_selection", False):
            return
        model, tree_iter = selection.get_selected()
        if not model or not tree_iter:
            return
        row_key = str(model.get_value(tree_iter, 10) or "")
        if row_key:
            self._activate_telemetry_tree_row(row_key, zoom_timeline=True)

    def _on_telemetry_tree_row_activated(
        self,
        _tree: Gtk.TreeView,
        path: Gtk.TreePath,
        _col: Gtk.TreeViewColumn,
    ) -> None:
        if getattr(self, "_syncing_telemetry_tree_selection", False):
            return
        tree_iter = self.telemetry_log_store.get_iter(path)
        if not tree_iter:
            return
        row_key = str(self.telemetry_log_store.get_value(tree_iter, 10) or "")
        if row_key:
            self._activate_telemetry_tree_row(row_key, zoom_timeline=True)

    def _activate_telemetry_tree_row(self, row_key: str, zoom_timeline: bool = True) -> None:
        meta = getattr(self, "_telemetry_tree_row_meta", {}).get(row_key)
        if not meta:
            return
        comp_id = meta.get("comp_id")
        wname = str(meta.get("widget_name") or "")
        peer_wname = str(meta.get("peer_widget_name") or "")
        viol_dict = meta.get("violation")

        # 1. Highlight/select corresponding module or pin route edge on TopologyGraphCanvas
        if self.canvas and self.canvas.vm:
            matched_edge = None
            if meta.get("kind") == "pin_buffer" and wname and peer_wname:
                for edge in self.canvas.vm.edges:
                    if (
                        (wname in edge.source_node and peer_wname in edge.sink_node)
                        or (peer_wname in edge.source_node and wname in edge.sink_node)
                        or (edge.source_node == wname and edge.sink_node == peer_wname)
                        or (edge.source_node == peer_wname and edge.sink_node == wname)
                    ):
                        matched_edge = edge
                        break
            if matched_edge is not None:
                self.canvas.selected_node_id = None
                self.canvas.selected_node_ids.clear()
                self.canvas.selected_edge_id = matched_edge.id
                self.canvas.queue_draw()
            elif comp_id or wname:
                for nid, gnode in self.canvas.vm.nodes.items():
                    nt = self.canvas.live_node_telemetry.get(nid, {})
                    if (comp_id and nt.get("comp_id") == comp_id) or (
                        wname and (nid == wname or gnode.title == wname or wname in nid)
                    ):
                        self.canvas.select_node(nid)
                        break

        # 2. If row has violation or module timing info, zoom/focus Scheduling & Buffers timeline
        if zoom_timeline and hasattr(self, "sched_telemetry_widget"):
            if viol_dict:
                if hasattr(self, "waveform_notebook"):
                    self.waveform_notebook.set_current_page(1)
                self.sched_telemetry_widget.focus_and_zoom_to_violation(viol_dict)
            elif comp_id is not None and meta.get("mod_obj") is not None:
                m_obj = meta["mod_obj"]
                synth_v = {
                    "v_key": f"mod:{comp_id}:cpc",
                    "type": "MODULE_INSPECT",
                    "comp_id": comp_id,
                    "pipe_id": getattr(m_obj, "pipeline_id", 1),
                    "core_id": getattr(m_obj, "core_id", 0),
                    "widget_name": wname,
                    "tick_idx": 0,
                    "mod": m_obj,
                }
                if hasattr(self, "waveform_notebook"):
                    self.waveform_notebook.set_current_page(1)
                self.sched_telemetry_widget.focus_and_zoom_to_violation(synth_v)

    def _populate_telemetry_log_view(
        self,
        telem: Optional[TestbenchTelemetry],
        focus_violation: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not hasattr(self, "telemetry_log_view"):
            return
        if telem is not None:
            self._last_telemetry_log_data = telem
        else:
            telem = getattr(self, "_last_telemetry_log_data", None)

        self._init_telemetry_log_tags()
        buf = self.telemetry_log_view.get_buffer()
        buf.set_text("")

        def append_tagged(line_str: str, tag_name: Optional[str] = None) -> int:
            end_it = buf.get_end_iter()
            start_off = end_it.get_offset()
            buf.insert(end_it, line_str + "\n")
            if tag_name:
                s_it = buf.get_iter_at_offset(start_off)
                e_it = buf.get_iter_at_offset(start_off + len(line_str))
                buf.apply_tag_by_name(tag_name, s_it, e_it)
            return start_off

        if hasattr(self, "telemetry_log_store"):
            self._syncing_telemetry_tree_selection = True
            self.telemetry_log_store.clear()
            self._telemetry_tree_row_meta.clear()
            self._syncing_telemetry_tree_selection = False

        if telem is None:
            self.telemetry_log_title_lbl.set_text(
                "Telemetry Log: No telemetry data yet — run Testbench to inspect scheduling & pin buffer violations."
            )
            if hasattr(self, "kpi_viol_lbl"):
                self.kpi_viol_lbl.set_text("🟢 0 Violations")
                self.kpi_core_lbl.set_text("⚡ Cores: Idle")
                self.kpi_kcps_lbl.set_text("🧩 Modules: 0 (0 KCPS)")
                self.kpi_mem_lbl.set_text("💾 SRAM / Ticks: 0")
            if hasattr(self, "telemetry_log_store"):
                self.telemetry_log_store.append(
                    None,
                    [
                        "ℹ No telemetry data collected yet",
                        "IDLE",
                        0,
                        "",
                        False,
                        "Click [▶ Run in Testbench] to populate cores, pipelines, modules, pin buffers, and violations.",
                        "#94a3b8",
                        "#181c26",
                        False,
                        int(Pango.Weight.NORMAL),
                        "",
                    ],
                )
            append_tagged("No telemetry data collected yet. Click [▶ Run in Testbench] to populate.", "vlog_dim")
            return

        focus_cid = focus_violation.get("comp_id") if focus_violation else None
        focus_vkey = str(focus_violation.get("v_key", "")) if focus_violation else ""
        focus_vtype = focus_violation.get("type", "") if focus_violation else ""
        focus_wname = focus_violation.get("widget_name", "") if focus_violation else ""
        focus_summary = focus_violation.get("summary", "") if focus_violation else ""
        scroll_target_offset: Optional[int] = None

        active_mods = [
            c for c in telem.components if c.iterations > 0 or c.avg_cycles > 0 or c.in_pins or c.out_pins
        ]
        if not active_mods and telem.components:
            active_mods = list(telem.components)

        # Collect structured violations per module
        mod_violations_map: Dict[int, List[Dict[str, Any]]] = {}
        total_viol_events = 0
        violating_mod_count = 0
        for m in active_mods:
            v_list = (
                self.sched_telemetry_widget._collect_module_violations(m)
                if hasattr(self, "sched_telemetry_widget")
                else []
            )
            if not v_list and (m.cpc_violations > 0 or (m.effective_cpc > 0 and m.peak_cycles > m.effective_cpc)):
                v_list = [
                    {
                        "v_key": f"mod:{m.comp_id}:cpc",
                        "type": "CPC_VIOLATION",
                        "comp_id": m.comp_id,
                        "pipe_id": m.pipeline_id,
                        "core_id": m.core_id,
                        "widget_name": m.widget_name or m.module_name,
                        "count": max(1, m.cpc_violations),
                        "cycles": m.peak_cycles,
                        "effective_cpc": m.effective_cpc,
                        "summary": f"CPC Overrun: peak={m.peak_cycles} cyc > CPC={m.effective_cpc} cyc (v={m.cpc_violations})",
                        "children": [],
                        "mod": m,
                    }
                ]
            if v_list:
                mod_violations_map[m.comp_id] = v_list
                violating_mod_count += 1
                total_viol_events += sum(int(vg.get("count", 1) or 1) for vg in v_list)

        # Update Top Title & KPI Summary Cards
        if focus_violation:
            self.telemetry_log_title_lbl.set_text(
                f"⚠ Telemetry Log — [{focus_vtype}] {focus_wname}: {focus_summary}"
            )
        else:
            self.telemetry_log_title_lbl.set_text(
                f"Telemetry Log — {len(active_mods)} Modules | Violations: {total_viol_events} ({violating_mod_count} Modules) | LL Ticks: {telem.total_ll_ticks}"
            )

        if hasattr(self, "kpi_viol_lbl"):
            if total_viol_events > 0:
                self.kpi_viol_lbl.set_markup(
                    f"<span foreground='#ff6b6b' weight='bold'>🔴 {total_viol_events} Violations ({violating_mod_count} Mod)</span>"
                )
            else:
                self.kpi_viol_lbl.set_markup(
                    "<span foreground='#5cd68d' weight='bold'>🟢 0 Violations (Healthy)</span>"
                )
            active_cores = [c for c in telem.cores if c.enabled or c.tick_count > 0]
            pk_load = max((c.cpu_load_pct for c in active_cores), default=0.0)
            self.kpi_core_lbl.set_text(
                f"⚡ Cores: {len(active_cores)} Active (Peak {pk_load:.1f}%)"
            )
            tot_kcps = sum(m.avg_kcps for m in active_mods)
            pk_kcps = sum(m.peak_kcps for m in active_mods)
            self.kpi_kcps_lbl.set_text(
                f"🧩 {len(active_mods)} Mods ({tot_kcps}/{pk_kcps} KCPS)"
            )
            self.kpi_mem_lbl.set_text(
                f"💾 Heap: {telem.total_heap_bytes:,}B • Ticks: {telem.total_ll_ticks}"
            )

        # ---------------------------------------------------------------------
        # Build Hierarchical Tree Nodes for Gtk.TreeStore
        # ---------------------------------------------------------------------
        W_BOLD = int(Pango.Weight.BOLD)
        W_NORM = int(Pango.Weight.NORMAL)

        def make_node(
            title: str,
            status: str = "",
            prog_val: int = 0,
            prog_txt: str = "",
            prog_vis: bool = False,
            details: str = "",
            fg: str = "#e2e8f0",
            bg: str = "#181c26",
            bg_set: bool = False,
            weight: int = W_NORM,
            row_key: str = "",
            category: str = "all",
            auto_expand: bool = False,
            is_focus_target: bool = False,
            meta: Optional[Dict[str, Any]] = None,
            children: Optional[List[Dict[str, Any]]] = None,
        ) -> Dict[str, Any]:
            if row_key and meta is not None:
                self._telemetry_tree_row_meta[row_key] = meta
            return {
                "title": title,
                "status": status,
                "prog_val": max(0, min(100, int(prog_val))),
                "prog_txt": prog_txt,
                "prog_vis": bool(prog_vis),
                "details": details,
                "fg": fg or "#e2e8f0",
                "bg": bg or "#181c26",
                "bg_set": bool(bg_set),
                "weight": weight,
                "row_key": row_key,
                "category": category,
                "auto_expand": bool(auto_expand),
                "is_focus_target": bool(is_focus_target),
                "children": list(children) if children else [],
            }

        tree_roots: List[Dict[str, Any]] = []

        # SECTION 1: Active Violations & Alerts
        viol_section_children: List[Dict[str, Any]] = []
        if focus_violation:
            f_mod: Optional[ComponentPerfItem] = focus_violation.get("mod")
            if f_mod is None and focus_cid is not None:
                f_mod = next((c for c in telem.components if c.comp_id == focus_cid), None)
            f_cpc_pct = (
                int((f_mod.peak_cycles * 100.0) / max(1, f_mod.effective_cpc))
                if (f_mod and f_mod.effective_cpc > 0)
                else 100
            )
            f_children: List[Dict[str, Any]] = []
            if f_mod is not None:
                m_us = f_mod.exec_avg_us if f_mod.exec_avg_us > 0 else (f_mod.avg_cycles / 393.216)
                m_pk_us = f_mod.exec_peak_us if f_mod.exec_peak_us > 0 else (f_mod.peak_cycles / 393.216)
                f_children.append(
                    make_node(
                        title=f"📍 Location: Pipeline {f_mod.pipeline_id} • Core {f_mod.core_id}",
                        status=f"{f_mod.domain} / {f_mod.mem_type}",
                        details=f"Widget: {f_mod.widget_name or f_mod.module_name} (comp_id=0x{f_mod.comp_id:06x}) | Iterations={f_mod.iterations}",
                        fg="#ffd166",
                        weight=W_BOLD,
                    )
                )
                f_children.append(
                    make_node(
                        title="⏱ Execution Timing & CPC Budget",
                        status=f"{f_mod.peak_cycles:,} / {f_mod.effective_cpc:,} cyc",
                        prog_val=min(100, f_cpc_pct),
                        prog_txt=f"{f_cpc_pct}% CPC !",
                        prog_vis=f_mod.effective_cpc > 0,
                        details=f"avg={m_us:.2f} µs / peak={m_pk_us:.2f} µs | KCPS avg={f_mod.avg_kcps} / peak={f_mod.peak_kcps} | violations={f_mod.cpc_violations}",
                        fg="#ff6b6b",
                        weight=W_BOLD,
                    )
                )
                for p_in in f_mod.in_pins:
                    fill_pct = int((p_in.ll_exit_avail * 100) / max(1, p_in.buf_size)) if p_in.buf_size > 0 else 0
                    f_children.append(
                        make_node(
                            title=f"🔌 Connected Source Pin IN[{p_in.pin_index}] ← {p_in.peer_widget_name or 'host'}",
                            status=f"{p_in.ll_exit_avail}/{p_in.buf_size} B",
                            prog_val=fill_pct,
                            prog_txt=f"{fill_pct}% Fill",
                            prog_vis=p_in.buf_size > 0,
                            details=f"buf={p_in.buf_id} peer=0x{p_in.peer_comp_id:x} ({p_in.peer_widget_name or 'host'}) | xfer={p_in.last_bytes_transferred}B (tot={p_in.total_bytes_transferred:,}B) | {p_in.entry_rms_db:+.1f}dBFS",
                            fg="#ffd166",
                            row_key=f"focus:pin_in:{f_mod.comp_id}:{p_in.pin_index}",
                            meta={
                                "kind": "pin_buffer",
                                "comp_id": f_mod.comp_id,
                                "widget_name": f_mod.widget_name or f_mod.module_name,
                                "peer_widget_name": p_in.peer_widget_name,
                            },
                        )
                    )
                for p_out in f_mod.out_pins:
                    fill_pct = int((p_out.ll_exit_avail * 100) / max(1, p_out.buf_size)) if p_out.buf_size > 0 else 0
                    f_children.append(
                        make_node(
                            title=f"🔌 Connected Sink Pin OUT[{p_out.pin_index}] → {p_out.peer_widget_name or 'dai'}",
                            status=f"{p_out.ll_exit_avail}/{p_out.buf_size} B",
                            prog_val=fill_pct,
                            prog_txt=f"{fill_pct}% Fill",
                            prog_vis=p_out.buf_size > 0,
                            details=f"buf={p_out.buf_id} peer=0x{p_out.peer_comp_id:x} ({p_out.peer_widget_name or 'dai'}) | xfer={p_out.last_bytes_transferred}B (tot={p_out.total_bytes_transferred:,}B) | {p_out.exit_rms_db:+.1f}dBFS",
                            fg="#ffd166",
                            row_key=f"focus:pin_out:{f_mod.comp_id}:{p_out.pin_index}",
                            meta={
                                "kind": "pin_buffer",
                                "comp_id": f_mod.comp_id,
                                "widget_name": f_mod.widget_name or f_mod.module_name,
                                "peer_widget_name": p_out.peer_widget_name,
                            },
                        )
                    )
            viol_section_children.append(
                make_node(
                    title=f"🎯 FOCUSED: [{focus_vtype}] {focus_wname}",
                    status=f"! comp=0x{int(focus_cid or 0):04x}",
                    prog_val=min(100, f_cpc_pct),
                    prog_txt=f"{f_cpc_pct}% CPC !",
                    prog_vis=True,
                    details=focus_summary,
                    fg="#ffffff",
                    bg="#681826",
                    bg_set=True,
                    weight=W_BOLD,
                    row_key="focus:primary_violation",
                    auto_expand=True,
                    is_focus_target=True,
                    meta={
                        "kind": "violation",
                        "comp_id": focus_cid,
                        "widget_name": focus_wname,
                        "violation": focus_violation,
                        "mod_obj": f_mod,
                    },
                    children=f_children,
                )
            )

        for m in active_mods:
            v_grps = mod_violations_map.get(m.comp_id, [])
            if not v_grps:
                continue
            wname = m.widget_name or m.module_name or f"0x{m.comp_id:x}"
            for vg in v_grps:
                vkey = str(vg.get("v_key", f"v:{m.comp_id}"))
                is_f = bool(focus_vkey and focus_vkey == vkey)
                cpc_pct = (
                    int((m.peak_cycles * 100.0) / max(1, m.effective_cpc))
                    if m.effective_cpc > 0
                    else 100
                )
                ev_nodes: List[Dict[str, Any]] = []
                for ev in vg.get("children", []):
                    ev_key = str(ev.get("v_key", ""))
                    ev_is_f = bool(focus_vkey and focus_vkey == ev_key)
                    ev_cyc = int(ev.get("cycles", m.peak_cycles) or m.peak_cycles)
                    ev_bud = int(ev.get("effective_cpc", m.effective_cpc) or m.effective_cpc or 1)
                    ev_pct = int((ev_cyc * 100.0) / max(1, ev_bud))
                    ev_nodes.append(
                        make_node(
                            title=f"• {ev.get('type', 'EVENT')} (Tick #{ev.get('tick_idx', 0)})",
                            status=f"{ev_cyc:,} / {ev_bud:,} cyc",
                            prog_val=min(100, ev_pct),
                            prog_txt=f"{ev_pct}% CPC",
                            prog_vis=ev_bud > 0,
                            details=str(ev.get("summary", "")),
                            fg="#ffffff" if ev_is_f else "#fca5a5",
                            bg="#681826" if ev_is_f else "#2a1218",
                            bg_set=True,
                            weight=W_BOLD if ev_is_f else W_NORM,
                            row_key=f"viol_ev:{ev_key}",
                            is_focus_target=ev_is_f,
                            meta={
                                "kind": "violation",
                                "comp_id": m.comp_id,
                                "widget_name": wname,
                                "violation": ev,
                                "mod_obj": m,
                            },
                        )
                    )
                viol_section_children.append(
                    make_node(
                        title=f"🔴 ! {wname} — [{vg['type']}]",
                        status=f"! v={vg.get('count', 1)}",
                        prog_val=min(100, cpc_pct),
                        prog_txt=f"{cpc_pct}% CPC !",
                        prog_vis=True,
                        details=str(vg.get("summary", "")),
                        fg="#ffffff" if is_f else "#ff6b6b",
                        bg="#681826" if is_f else "#38151e",
                        bg_set=True,
                        weight=W_BOLD,
                        row_key=f"viol_grp:{vkey}",
                        auto_expand=is_f or (focus_cid == m.comp_id) or len(viol_section_children) <= 4,
                        is_focus_target=is_f and not focus_violation,
                        meta={
                            "kind": "violation",
                            "comp_id": m.comp_id,
                            "widget_name": wname,
                            "violation": vg,
                            "mod_obj": m,
                        },
                        children=ev_nodes,
                    )
                )

        if viol_section_children:
            tree_roots.append(
                make_node(
                    title=f"🔴 Active Violations & Alerts ({total_viol_events} events across {violating_mod_count} modules)",
                    status=f"! {total_viol_events} VIOLATIONS",
                    prog_val=100,
                    prog_txt=f"{violating_mod_count}/{max(1, len(active_mods))} Mods !",
                    prog_vis=True,
                    details="Click any violation or event to zoom the Scheduling Timeline & highlight Source/Sink on the Topology Graph",
                    fg="#ff6b6b",
                    bg="#2b1118",
                    bg_set=True,
                    weight=W_BOLD,
                    category="violations",
                    auto_expand=True,
                    children=viol_section_children,
                )
            )
        else:
            tree_roots.append(
                make_node(
                    title="🟢 Active Violations & Alerts (0 Violations)",
                    status="ALL OK",
                    prog_val=0,
                    prog_txt="0% Overruns",
                    prog_vis=True,
                    details="All DSP cores, pipelines, module CPC budgets, and pin buffers operated within real-time constraints",
                    fg="#5cd68d",
                    bg="#11261d",
                    bg_set=True,
                    weight=W_BOLD,
                    category="violations",
                    auto_expand=False,
                )
            )

        # SECTION 2: Modules, CPC Budgets & Pin Buffers
        mod_section_children: List[Dict[str, Any]] = []
        max_mod_kcps = max((m.peak_kcps for m in active_mods), default=1) or 1
        for m in active_mods:
            wname = m.widget_name or m.module_name or f"0x{m.comp_id:x}"
            v_grps = mod_violations_map.get(m.comp_id, [])
            has_v = bool(v_grps)
            is_target_mod = focus_cid is not None and m.comp_id == focus_cid
            m_us = m.exec_avg_us if m.exec_avg_us > 0 else (m.avg_cycles / 393.216)
            m_pk_us = m.exec_peak_us if m.exec_peak_us > 0 else (m.peak_cycles / 393.216)

            if m.effective_cpc > 0:
                cpc_pct = int((m.peak_cycles * 100.0) / max(1, m.effective_cpc))
                bar_txt = f"{cpc_pct}% CPC ({m.peak_cycles}/{m.effective_cpc})"
            else:
                cpc_pct = int((m.peak_kcps * 100.0) / max_mod_kcps)
                bar_txt = f"{m.peak_kcps} KCPS"

            m_children: List[Dict[str, Any]] = [
                make_node(
                    title="⚡ Timing, KCPS & Memory Footprint",
                    status=f"{m.avg_kcps}/{m.peak_kcps} KCPS",
                    prog_val=min(100, cpc_pct),
                    prog_txt=bar_txt,
                    prog_vis=True,
                    details=(
                        f"exec avg={m_us:.2f} µs / peak={m_pk_us:.2f} µs | cyc avg={m.avg_cycles:,} / peak={m.peak_cycles:,} | "
                        f"CPC={m.effective_cpc:,} (v={m.cpc_violations}) | IBS={m.ibs}B OBS={m.obs}B | Heap={m.heap_bytes:,}B | "
                        f"Audio={m.in_rms_db:+.1f}→{m.out_rms_db:+.1f}dBFS"
                    ),
                    fg="#ff6b6b" if has_v else "#cbd5e1",
                )
            ]

            if v_grps:
                v_sub: List[Dict[str, Any]] = []
                for vg in v_grps:
                    ev_sub = [
                        make_node(
                            title=f"• Tick #{ev.get('tick_idx', 0)} ({ev.get('time_ms', 0.0):.2f} ms)",
                            status=f"{ev.get('cycles', 0):,} cyc",
                            details=str(ev.get("summary", "")),
                            fg="#fca5a5",
                            row_key=f"mod_tree_ev:{ev.get('v_key', '')}",
                            meta={
                                "kind": "violation",
                                "comp_id": m.comp_id,
                                "widget_name": wname,
                                "violation": ev,
                                "mod_obj": m,
                            },
                        )
                        for ev in vg.get("children", [])
                    ]
                    v_sub.append(
                        make_node(
                            title=f"⚠ [{vg['type']}]",
                            status=f"v={vg.get('count', 1)}",
                            prog_val=min(100, cpc_pct),
                            prog_txt=f"{cpc_pct}% CPC !",
                            prog_vis=True,
                            details=str(vg.get("summary", "")),
                            fg="#ff6b6b",
                            weight=W_BOLD,
                            row_key=f"mod_tree_vg:{vg.get('v_key', '')}",
                            auto_expand=True,
                            meta={
                                "kind": "violation",
                                "comp_id": m.comp_id,
                                "widget_name": wname,
                                "violation": vg,
                                "mod_obj": m,
                            },
                            children=ev_sub,
                        )
                    )
                m_children.append(
                    make_node(
                        title=f"🔴 Module Violations ({len(v_grps)})",
                        status=f"! v={m.cpc_violations or len(v_grps)}",
                        details="Expand to inspect per-tick violation events",
                        fg="#ff6b6b",
                        weight=W_BOLD,
                        auto_expand=True,
                        children=v_sub,
                    )
                )

            ignore_t0 = bool(getattr(self.sched_telemetry_widget, "ignore_tick0_underrun", True)) if hasattr(self, "sched_telemetry_widget") else True
            if m.in_pins:
                in_sub: List[Dict[str, Any]] = []
                for p_in in m.in_pins:
                    fill_pct = int((p_in.ll_exit_avail * 100) / max(1, p_in.buf_size)) if p_in.buf_size > 0 else 0
                    eff_x = (
                        self.sched_telemetry_widget._effective_pin_xruns(m, p_in)
                        if hasattr(self, "sched_telemetry_widget")
                        else int(getattr(p_in, "xrun_count", 0) or 0)
                    )
                    pin_starved = eff_x > 0
                    xrun_tag = (
                        f" | xruns={eff_x}(underrun={getattr(p_in, 'underrun_count', 0)} overrun={getattr(p_in, 'overrun_count', 0)})"
                        if eff_x > 0
                        else ""
                    )
                    in_sub.append(
                        make_node(
                            title=f"IN[{p_in.pin_index}] ← Source: {p_in.peer_widget_name or 'host'}",
                            status=f"{p_in.ll_entry_avail}→{p_in.ll_exit_avail}/{p_in.buf_size} B",
                            prog_val=fill_pct,
                            prog_txt=f"{fill_pct}% Fill" if not pin_starved else f"XRUN ({eff_x}×) !",
                            prog_vis=p_in.buf_size > 0,
                            details=(
                                f"buf={p_in.buf_id} | peer=0x{p_in.peer_comp_id:x} ({p_in.peer_widget_name or 'host'}) | "
                                f"free={p_in.ll_exit_free}B | xfer={p_in.last_bytes_transferred}B (tot={p_in.total_bytes_transferred:,}B){xrun_tag} | "
                                f"{p_in.rate}Hz {p_in.channels}ch | rms={p_in.entry_rms_db:+.1f}dBFS"
                            ),
                            fg="#ff6b6b" if pin_starved else "#93c5fd",
                            weight=W_BOLD if pin_starved else W_NORM,
                            row_key=f"mod:{m.comp_id}:in:{p_in.pin_index}",
                            meta={
                                "kind": "pin_buffer",
                                "comp_id": m.comp_id,
                                "widget_name": wname,
                                "peer_widget_name": p_in.peer_widget_name,
                                "mod_obj": m,
                            },
                        )
                    )
                m_children.append(
                    make_node(
                        title=f"🔌 Input Pin Buffers ({len(m.in_pins)})",
                        status=f"IBS={m.ibs} B",
                        details=", ".join(f"IN[{p.pin_index}]←{p.peer_widget_name or 'host'}" for p in m.in_pins),
                        fg="#7ec8ff",
                        weight=W_BOLD,
                        auto_expand=has_v or is_target_mod,
                        children=in_sub,
                    )
                )

            if m.out_pins:
                out_sub: List[Dict[str, Any]] = []
                for p_out in m.out_pins:
                    fill_pct = int((p_out.ll_exit_avail * 100) / max(1, p_out.buf_size)) if p_out.buf_size > 0 else 0
                    eff_x = (
                        self.sched_telemetry_widget._effective_pin_xruns(m, p_out)
                        if hasattr(self, "sched_telemetry_widget")
                        else int(getattr(p_out, "xrun_count", 0) or 0)
                    )
                    pin_starved = eff_x > 0
                    xrun_tag = (
                        f" | xruns={eff_x}(overrun={getattr(p_out, 'overrun_count', 0)} underrun={getattr(p_out, 'underrun_count', 0)})"
                        if eff_x > 0
                        else ""
                    )
                    out_sub.append(
                        make_node(
                            title=f"OUT[{p_out.pin_index}] → Sink: {p_out.peer_widget_name or 'dai'}",
                            status=f"{p_out.ll_entry_avail}→{p_out.ll_exit_avail}/{p_out.buf_size} B",
                            prog_val=fill_pct,
                            prog_txt=f"{fill_pct}% Fill" if not pin_starved else f"XRUN ({eff_x}×) !",
                            prog_vis=p_out.buf_size > 0,
                            details=(
                                f"buf={p_out.buf_id} | peer=0x{p_out.peer_comp_id:x} ({p_out.peer_widget_name or 'dai'}) | "
                                f"free={p_out.ll_exit_free}B | xfer={p_out.last_bytes_transferred}B (tot={p_out.total_bytes_transferred:,}B){xrun_tag} | "
                                f"{p_out.rate}Hz {p_out.channels}ch | rms={p_out.exit_rms_db:+.1f}dBFS"
                            ),
                            fg="#ff6b6b" if pin_starved else "#fdba74",
                            weight=W_BOLD if pin_starved else W_NORM,
                            row_key=f"mod:{m.comp_id}:out:{p_out.pin_index}",
                            meta={
                                "kind": "pin_buffer",
                                "comp_id": m.comp_id,
                                "widget_name": wname,
                                "peer_widget_name": p_out.peer_widget_name,
                                "mod_obj": m,
                            },
                        )
                    )
                m_children.append(
                    make_node(
                        title=f"🔌 Output Pin Buffers ({len(m.out_pins)})",
                        status=f"OBS={m.obs} B",
                        details=", ".join(f"OUT[{p.pin_index}]→{p.peer_widget_name or 'dai'}" for p in m.out_pins),
                        fg="#fdba74",
                        weight=W_BOLD,
                        auto_expand=has_v or is_target_mod,
                        children=out_sub,
                    )
                )

            mod_section_children.append(
                make_node(
                    title=f"{'🔴 !' if has_v else '🟢 ●'} {wname} (0x{m.comp_id:04x})",
                    status=f"! VIOLATION (v={m.cpc_violations or len(v_grps)})" if has_v else "OK (v=0)",
                    prog_val=min(100, cpc_pct),
                    prog_txt=bar_txt,
                    prog_vis=True,
                    details=(
                        f"[P{m.pipeline_id}•C{m.core_id}•{m.domain}•{m.mem_type}] "
                        f"cyc={m.avg_cycles}/{m.peak_cycles} CPC={m.effective_cpc} | "
                        f"exec={m_us:.2f}/{m_pk_us:.2f}µs | {m.avg_kcps}/{m.peak_kcps} KCPS | iters={m.iterations}"
                    ),
                    fg="#ffffff" if is_target_mod else ("#ff6b6b" if has_v else "#5cd68d"),
                    bg="#4c1824" if is_target_mod else ("#2c141b" if has_v else ""),
                    bg_set=is_target_mod or has_v,
                    weight=W_BOLD if (has_v or is_target_mod) else W_NORM,
                    row_key=f"mod_row:{m.comp_id}",
                    auto_expand=is_target_mod or has_v,
                    meta={
                        "kind": "module",
                        "comp_id": m.comp_id,
                        "widget_name": wname,
                        "mod_obj": m,
                        "violation": v_grps[0] if v_grps else None,
                    },
                    children=m_children,
                )
            )

        tree_roots.append(
            make_node(
                title=f"🧩 Modules, CPC Budgets & Pin Buffers ({len(active_mods)} Modules)",
                status=f"{violating_mod_count} Violating / {len(active_mods)} Total",
                prog_val=int((violating_mod_count * 100) / max(1, len(active_mods))) if violating_mod_count else 100,
                prog_txt=f"{len(active_mods)} Active Mods",
                prog_vis=True,
                details="Expand any module to inspect CPC cycle budget, input/output pin ring buffers, and connected Source/Sink peers",
                fg="#7ec8ff",
                bg="#162032",
                bg_set=True,
                weight=W_BOLD,
                category="modules",
                auto_expand=True,
                children=mod_section_children,
            )
        )

        # SECTION 3: DSP Cores & Pipelines
        cp_children: List[Dict[str, Any]] = []
        for c in telem.cores:
            if not (c.enabled or c.tick_count > 0 or c.core_id == 0):
                continue
            has_co = c.overruns > 0
            freq_mhz = (c.freq_hz / 1e6) if c.freq_hz > 0 else (telem.dsp_clock_hz / 1e6)
            ld_pct = int(min(100.0, max(0.0, c.cpu_load_pct)))
            cp_children.append(
                make_node(
                    title=f"{'🔴 !' if has_co else '⚡'} DSP Core {c.core_id} ({freq_mhz:.0f} MHz)",
                    status=f"! OVERRUN (v={c.overruns})" if has_co else f"ACTIVE ({c.ll_ticks} ticks)",
                    prog_val=ld_pct,
                    prog_txt=f"{c.cpu_load_pct:.1f}% CPU",
                    prog_vis=True,
                    details=(
                        f"LL cyc avg={c.avg_cycles:,} / peak={c.peak_cycles:,} ({c.ll_exec_avg_us:.1f}/{c.ll_exec_peak_us:.1f} µs) | "
                        f"LL={c.avg_kcps}/{c.peak_kcps} KCPS | DP={c.dp_avg_kcps}/{c.dp_peak_kcps} KCPS ({c.dp_runs} runs)"
                    ),
                    fg="#ff6b6b" if has_co else "#a5f3fc",
                    weight=W_BOLD,
                )
            )

        for p in telem.pipelines:
            has_px = p.xruns > 0
            p_us = p.exec_peak_us if p.exec_peak_us > 0 else (p.peak_cycles / 393.216)
            p_avg_us = p.exec_avg_us if p.exec_avg_us > 0 else (p.avg_cycles / 393.216)
            bud_pct = int((p_us * 100.0) / max(1, p.period_us))
            p_mods = [m for m in active_mods if m.pipeline_id == p.pipeline_id]
            p_mod_rows = [
                make_node(
                    title=f"↳ {m.widget_name or m.module_name} (0x{m.comp_id:04x})",
                    status=f"{m.avg_cycles}/{m.peak_cycles} cyc",
                    prog_val=min(100, int((m.peak_cycles * 100.0) / max(1, m.effective_cpc))) if m.effective_cpc > 0 else 0,
                    prog_txt=f"{int((m.peak_cycles * 100.0) / max(1, m.effective_cpc))}% CPC" if m.effective_cpc > 0 else "",
                    prog_vis=m.effective_cpc > 0,
                    details=f"{m.domain}•{m.mem_type} | {m.avg_kcps}/{m.peak_kcps} KCPS | IBS={m.ibs}B OBS={m.obs}B",
                    fg="#ff6b6b" if m.comp_id in mod_violations_map else "#cbd5e1",
                    row_key=f"pipe_mod:{p.pipeline_id}:{m.comp_id}",
                    meta={"kind": "module", "comp_id": m.comp_id, "widget_name": m.widget_name or m.module_name, "mod_obj": m},
                )
                for m in p_mods
            ]
            cp_children.append(
                make_node(
                    title=f"{'🔴 !' if has_px else '🔀'} Pipeline {p.pipeline_id} [Core {p.core_id} • {p.mem_type}]",
                    status=f"! XRUN (xruns={p.xruns})" if has_px else f"OK ({p.copies} copies)",
                    prog_val=min(100, bud_pct),
                    prog_txt=f"{bud_pct}% Period ({p_us:.1f}/{p.period_us}µs)",
                    prog_vis=True,
                    details=(
                        f"prio={p.priority} period={p.period_us}µs | cyc={p.avg_cycles:,}/{p.peak_cycles:,} | "
                        f"exec={p_avg_us:.2f}/{p_us:.2f}µs | {p.avg_kcps}/{p.peak_kcps} KCPS | "
                        f"LL={p.ll_module_count} DP={p.dp_module_count}"
                    ),
                    fg="#ff6b6b" if has_px else "#c4b5fd",
                    weight=W_BOLD,
                    auto_expand=has_px,
                    children=p_mod_rows,
                )
            )

        tree_roots.append(
            make_node(
                title=f"⚡ DSP Cores & Pipelines ({len(telem.cores)} Cores, {len(telem.pipelines)} Pipelines)",
                status=f"{telem.total_ll_ticks} LL Ticks",
                prog_val=int(max((c.cpu_load_pct for c in telem.cores), default=0.0)),
                prog_txt="Core & Pipe Load",
                prog_vis=bool(telem.cores),
                details="Hardware DSP core utilization, LL tick deadlines, and per-pipeline period budgets",
                fg="#7ec8ff",
                bg="#162032",
                bg_set=True,
                weight=W_BOLD,
                category="cores_pipes",
                auto_expand=False,
                children=cp_children,
            )
        )

        # SECTION 4: Platform, Power, SRAM, Kcontrols & Raw Logs
        sys_children: List[Dict[str, Any]] = []
        pwr_str = "D0 (ACTIVE)" if telem.dsp_power_state == 1 else f"D3 (PAUSED/{telem.dsp_power_state})"
        sim_modes = {0: "OFF (HOST-MEASURED)", 1: "VIRTUAL-CPC", 2: "REALTIME-PACED"}
        mem_dom = "DRAM" if telem.default_mem_domain == 1 else "SRAM"
        sys_children.append(
            make_node(
                title=f"🖥 Platform: {telem.platform} (FW v{telem.fw_version})",
                status=pwr_str,
                details=(
                    f"Sim={sim_modes.get(telem.workload_sim_mode, 'OFF')} | Default Mem={mem_dom} "
                    f"(DRAM {telem.dram_factor_x100 / 100.0:.2f}x +{telem.dram_cycles_per_word} cyc/w) | "
                    f"PCM: {telem.pcm_frames_played} frames @ {telem.pcm_rate}Hz ({telem.pcm_channels}ch)"
                ),
                fg="#a7f3d0",
            )
        )
        hp_tot = max(1, telem.hpsram.total_banks)
        hp_pct = int((telem.hpsram.powered_banks * 100) / hp_tot) if telem.hpsram.total_banks > 0 else 0
        sys_children.append(
            make_node(
                title="💾 Memory: HPSRAM / LPSRAM & Module Heap",
                status=f"Heap: {telem.total_heap_bytes:,} B",
                prog_val=hp_pct,
                prog_txt=f"HPSRAM {telem.hpsram.powered_banks}/{telem.hpsram.total_banks} Banks",
                prog_vis=telem.hpsram.total_banks > 0,
                details=(
                    f"HPSRAM: {telem.hpsram.powered_banks}/{telem.hpsram.total_banks} ON ({telem.hpsram.free_phys_pages} free pages) | "
                    f"LPSRAM: {telem.lpsram.powered_banks}/{telem.lpsram.total_banks} ON | Total Module Heap: {telem.total_heap_bytes:,} B"
                ),
                fg="#fde68a",
            )
        )
        dapm_pct = (
            int((telem.dapm_powered_nodes * 100) / max(1, telem.dapm_total_nodes))
            if telem.dapm_total_nodes > 0
            else 0
        )
        sys_children.append(
            make_node(
                title="🔌 DAPM Graph Power State",
                status=f"{telem.dapm_powered_nodes}/{telem.dapm_total_nodes} Widgets ON",
                prog_val=dapm_pct,
                prog_txt=f"{dapm_pct}% Powered",
                prog_vis=telem.dapm_total_nodes > 0,
                details=f"Active Routes: {telem.dapm_active_edges}/{telem.dapm_total_edges} | Runtime PM Active: {telem.pm_runtime_active}",
                fg="#93c5fd",
            )
        )
        if telem.kcontrols:
            kc_rows = [
                make_node(
                    title=f"🎛 [numid={kc.numid}] {kc.name}",
                    status=f"[{','.join(str(v) for v in kc.values)}]",
                    details=f"type={kc.type_str} ({kc.iface_str}) | access={kc.access_str} | range=[{kc.min_val}..{kc.max_val}] | widget={kc.target_widget}",
                    fg="#cbd5e1",
                    row_key=f"kc_row:{kc.numid}",
                    meta={"kind": "kcontrol", "widget_name": kc.target_widget},
                )
                for kc in telem.kcontrols
            ]
            sys_children.append(
                make_node(
                    title=f"🎛 Topology Kcontrols ({len(telem.kcontrols)})",
                    status=f"{len(telem.kcontrols)} Controls",
                    details="ALSA mixer, enum, switch, and bytes controls discovered on topology",
                    fg="#7ec8ff",
                    weight=W_BOLD,
                    children=kc_rows,
                )
            )
        if telem.shell_sched_telem or telem.shell_mod_buffers:
            raw_rows: List[Dict[str, Any]] = []
            for idx_l, raw_ln in enumerate((telem.shell_sched_telem or "").splitlines()):
                if not raw_ln.strip():
                    continue
                is_err_ln = ("cpc_v=" in raw_ln and "cpc_v=0" not in raw_ln) or (
                    "xruns=" in raw_ln and "xruns=0" not in raw_ln
                )
                raw_rows.append(
                    make_node(
                        title=f"sched-telem[{idx_l}]",
                        status="ALERT" if is_err_ln else "LOG",
                        details=raw_ln.strip(),
                        fg="#ff6b6b" if is_err_ln else "#94a3b8",
                    )
                )
            for idx_l, raw_ln in enumerate((telem.shell_mod_buffers or "").splitlines()):
                if not raw_ln.strip():
                    continue
                raw_rows.append(
                    make_node(
                        title=f"mod-buffers[{idx_l}]",
                        status="BUF",
                        details=raw_ln.strip(),
                        fg="#94a3b8",
                    )
                )
            sys_children.append(
                make_node(
                    title=f"📜 Raw Shell Telemetry Lines ({len(raw_rows)} lines)",
                    status="sched-telem / mod-buffers",
                    details="Searchable raw testbench CLI output lines",
                    fg="#94a3b8",
                    children=raw_rows,
                )
            )

        tree_roots.append(
            make_node(
                title="💾 Platform, Power, SRAM, Kcontrols & Raw Logs",
                status=pwr_str,
                prog_val=dapm_pct,
                prog_txt=f"{telem.dapm_powered_nodes}/{telem.dapm_total_nodes} DAPM",
                prog_vis=telem.dapm_total_nodes > 0,
                details=f"{telem.platform} • FW v{telem.fw_version} • {len(telem.kcontrols)} Kcontrols",
                fg="#7ec8ff",
                bg="#162032",
                bg_set=True,
                weight=W_BOLD,
                category="system",
                auto_expand=False,
                children=sys_children,
            )
        )

        # Apply Category Pill Filter & Search Query Filter
        active_cat = getattr(self, "_telemetry_category_filter", "all")
        search_q = (
            self.telemetry_search_entry.get_text().strip().lower()
            if hasattr(self, "telemetry_search_entry")
            else ""
        )

        def filter_node_recursive(node_dict: Dict[str, Any], is_root: bool = False) -> Optional[Dict[str, Any]]:
            if is_root and active_cat != "all" and node_dict.get("category") != active_cat:
                return None
            if not search_q:
                return node_dict
            haystack = f"{node_dict['title']} {node_dict['status']} {node_dict['prog_txt']} {node_dict['details']}".lower()
            self_matches = search_q in haystack
            filtered_children: List[Dict[str, Any]] = []
            for ch in node_dict.get("children", []):
                f_ch = filter_node_recursive(ch, is_root=False)
                if f_ch is not None:
                    filtered_children.append(f_ch)
            if self_matches or filtered_children:
                copied = dict(node_dict)
                copied["children"] = filtered_children if filtered_children else list(node_dict.get("children", []))
                if filtered_children or search_q:
                    copied["auto_expand"] = True
                return copied
            return None

        filtered_roots = [
            fr for r in tree_roots for fr in [filter_node_recursive(r, is_root=True)] if fr is not None
        ]

        # Populate Gtk.TreeStore and expand/select target paths
        if hasattr(self, "telemetry_log_store") and hasattr(self, "telemetry_log_tree"):
            self._syncing_telemetry_tree_selection = True
            expand_paths: List[Gtk.TreePath] = []
            focus_path: Optional[Gtk.TreePath] = None

            def insert_tree_node(parent_iter: Optional[Gtk.TreeIter], nd: Dict[str, Any]) -> None:
                nonlocal focus_path
                it = self.telemetry_log_store.append(
                    parent_iter,
                    [
                        nd["title"],
                        nd["status"],
                        nd["prog_val"],
                        nd["prog_txt"],
                        nd["prog_vis"],
                        nd["details"],
                        nd["fg"] or "#e2e8f0",
                        nd["bg"] or "#181c26",
                        nd["bg_set"],
                        nd["weight"],
                        nd["row_key"],
                    ],
                )
                tpath = self.telemetry_log_store.get_path(it)
                if nd.get("auto_expand") and tpath is not None:
                    expand_paths.append(tpath)
                if nd.get("is_focus_target") and tpath is not None and focus_path is None:
                    focus_path = tpath
                for child_nd in nd.get("children", []):
                    insert_tree_node(it, child_nd)

            for r_nd in filtered_roots:
                insert_tree_node(None, r_nd)

            for ep in expand_paths:
                self.telemetry_log_tree.expand_to_path(ep)

            if focus_path is not None:
                self.telemetry_log_tree.expand_to_path(focus_path)
                self.telemetry_log_tree.get_selection().select_path(focus_path)
                self.telemetry_log_tree.scroll_to_cell(focus_path, None, True, 0.25, 0.0)
            self._syncing_telemetry_tree_selection = False

        # 1. Focused Violation Diagnostic Section
        if focus_violation:
            scroll_target_offset = append_tagged(
                "================================================================================",
                "vlog_focus",
            )
            append_tagged(
                f">>> FOCUSED VIOLATION: [{focus_vtype}] {focus_wname} (comp_id=0x{int(focus_cid or 0):06x}) <<<",
                "vlog_focus",
            )
            append_tagged(f"  Summary : {focus_summary}", "vlog_focus")
            mod_obj: Optional[ComponentPerfItem] = focus_violation.get("mod")
            if mod_obj is None and focus_cid is not None:
                mod_obj = next((c for c in telem.components if c.comp_id == focus_cid), None)
            if mod_obj is not None:
                m_us = mod_obj.exec_avg_us if mod_obj.exec_avg_us > 0 else (mod_obj.avg_cycles / 393.216)
                m_pk_us = mod_obj.exec_peak_us if mod_obj.exec_peak_us > 0 else (mod_obj.peak_cycles / 393.216)
                append_tagged(
                    f"  Location: Pipeline {mod_obj.pipeline_id} | Core {mod_obj.core_id} | Domain={mod_obj.domain} | Mem={mod_obj.mem_type}",
                    "vlog_error",
                )
                append_tagged(
                    f"  Timing  : avg={m_us:.3f} µs / peak={m_pk_us:.3f} µs | Cycles avg={mod_obj.avg_cycles} / peak={mod_obj.peak_cycles} | "
                    f"KCPS avg={mod_obj.avg_kcps} / peak={mod_obj.peak_kcps}",
                    "vlog_error",
                )
                append_tagged(
                    f"  Budget  : Effective CPC={mod_obj.effective_cpc} cyc | CPC Violations={mod_obj.cpc_violations} / {mod_obj.iterations} iters | "
                    f"IBS={mod_obj.ibs}B OBS={mod_obj.obs}B",
                    "vlog_error",
                )
                for p_in in mod_obj.in_pins:
                    append_tagged(
                        f"  Pin IN[{p_in.pin_index}] : buf={p_in.buf_id} peer=0x{p_in.peer_comp_id:x} ({p_in.peer_widget_name or 'host'}) "
                        f"LL={p_in.ll_entry_avail}->{p_in.ll_exit_avail}/{p_in.buf_size}B "
                        f"xfer={p_in.last_bytes_transferred}B (tot={p_in.total_bytes_transferred:,}B) rms={p_in.entry_rms_db:+.1f}dB",
                        "vlog_warning",
                    )
                for p_out in mod_obj.out_pins:
                    append_tagged(
                        f"  Pin OUT[{p_out.pin_index}]: buf={p_out.buf_id} peer=0x{p_out.peer_comp_id:x} ({p_out.peer_widget_name or 'dai'}) "
                        f"LL={p_out.ll_entry_avail}->{p_out.ll_exit_avail}/{p_out.buf_size}B "
                        f"xfer={p_out.last_bytes_transferred}B (tot={p_out.total_bytes_transferred:,}B) rms={p_out.exit_rms_db:+.1f}dB",
                        "vlog_warning",
                    )
            append_tagged(
                "================================================================================",
                "vlog_focus",
            )
            append_tagged("")

        # 2. All Module Violations & Pin Buffer Telemetry Log
        append_tagged("=== MODULE CPC VIOLATION & PIN BUFFER TELEMETRY LOG ===", "vlog_header")
        active_mods = [
            c for c in telem.components if c.iterations > 0 or c.avg_cycles > 0 or c.in_pins or c.out_pins
        ]
        for m in active_mods:
            has_v = m.cpc_violations > 0 or (m.effective_cpc > 0 and m.peak_cycles > m.effective_cpc)
            is_target = focus_cid is not None and m.comp_id == focus_cid
            m_tag = "vlog_focus" if is_target else ("vlog_error" if has_v else "vlog_ok")
            status_str = f"VIOLATION (v={m.cpc_violations})" if has_v else "OK (v=0)"
            m_us = m.exec_avg_us if m.exec_avg_us > 0 else (m.avg_cycles / 393.216)
            m_pk_us = m.exec_peak_us if m.exec_peak_us > 0 else (m.peak_cycles / 393.216)
            off = append_tagged(
                f"[{status_str}] [P{m.pipeline_id}•C{m.core_id}•{m.domain}•{m.mem_type}] "
                f"{m.widget_name or m.module_name} (0x{m.comp_id:06x})  "
                f"cyc={m.avg_cycles}/{m.peak_cycles}  CPC={m.effective_cpc}  "
                f"exec={m_us:.2f}/{m_pk_us:.2f}µs  kcps={m.avg_kcps}/{m.peak_kcps}  iters={m.iterations}",
                m_tag,
            )
            if is_target and scroll_target_offset is None:
                scroll_target_offset = off
            if has_v and hasattr(self, "sched_telemetry_widget"):
                for v_grp in self.sched_telemetry_widget._collect_module_violations(m):
                    append_tagged(f"  ├─ ⚠ [{v_grp['type']}] {v_grp['summary']}", "vlog_error")
                    for ev in v_grp.get("children", []):
                        ev_tag = (
                            "vlog_focus"
                            if (focus_violation and focus_violation.get("v_key") == ev.get("v_key"))
                            else "vlog_error"
                        )
                        ev_off = append_tagged(f"  │  └─ • {ev['summary']}", ev_tag)
                        if focus_violation and focus_violation.get("v_key") == ev.get("v_key"):
                            scroll_target_offset = ev_off
            for p_in in m.in_pins:
                append_tagged(
                    f"  IN[{p_in.pin_index}]  buf={p_in.buf_id}: {p_in.ll_entry_avail}->{p_in.ll_exit_avail}/{p_in.buf_size}B "
                    f"xfer={p_in.last_bytes_transferred}B (tot={p_in.total_bytes_transferred:,}B) rms={p_in.entry_rms_db:+.1f}dBFS",
                    "vlog_dim",
                )
            for p_out in m.out_pins:
                append_tagged(
                    f"  OUT[{p_out.pin_index}] buf={p_out.buf_id}: {p_out.ll_entry_avail}->{p_out.ll_exit_avail}/{p_out.buf_size}B "
                    f"xfer={p_out.last_bytes_transferred}B (tot={p_out.total_bytes_transferred:,}B) rms={p_out.exit_rms_db:+.1f}dBFS",
                    "vlog_dim",
                )

        # 3. Raw Testbench Scheduler & Buffer Shell Logs
        if telem.shell_sched_telem:
            append_tagged("")
            append_tagged("=== RAW SCHEDULER TELEMETRY LOG (sched-telem) ===", "vlog_header")
            for raw_ln in telem.shell_sched_telem.splitlines():
                ln_tag: Optional[str] = None
                if focus_cid is not None and f"0x{int(focus_cid):x}" in raw_ln.lower():
                    ln_tag = "vlog_focus"
                elif "cpc_v=" in raw_ln and not raw_ln.endswith("cpc_v=0") and "cpc_v=0 " not in raw_ln:
                    ln_tag = "vlog_error"
                elif "xruns=" in raw_ln and "xruns=0" not in raw_ln:
                    ln_tag = "vlog_error"
                append_tagged(raw_ln, ln_tag)

        if telem.shell_mod_buffers:
            append_tagged("")
            append_tagged("=== RAW MODULE PIN BUFFER LOG (mod-buffers) ===", "vlog_header")
            for raw_ln in telem.shell_mod_buffers.splitlines():
                ln_tag = (
                    "vlog_focus"
                    if (focus_cid is not None and f"0x{int(focus_cid):x}" in raw_ln.lower())
                    else None
                )
                append_tagged(raw_ln, ln_tag)

        if scroll_target_offset is not None:
            target_it = buf.get_iter_at_offset(scroll_target_offset)
            mark = buf.create_mark("vlog_focus_mark", target_it, True)
            self.telemetry_log_view.scroll_to_mark(mark, 0.1, True, 0.0, 0.15)

    def _update_sched_status_label(self, telem: Optional[TestbenchTelemetry]) -> None:
        if not hasattr(self, "sched_status_lbl"):
            return
        if not telem or (
            telem.total_ll_ticks == 0
            and not telem.pipelines
            and not any(c.iterations > 0 or c.in_pins or c.out_pins for c in telem.components)
        ):
            self.sched_status_lbl.set_text(
                "Scheduling (Y: Core ID × X: Time) & Module Buffers [IDLE]"
            )
            return
        active_mods = [
            c for c in telem.components if c.iterations > 0 or c.avg_cycles > 0 or c.in_pins or c.out_pins
        ]
        active_kcps = sum(c.avg_kcps for c in telem.cores if c.enabled)
        self.sched_status_lbl.set_text(
            f"Scheduling & Buffers [Ticks={telem.total_ll_ticks} | "
            f"Pipelines={len(telem.pipelines)} | Modules={len(active_mods)} | "
            f"Active KCPS={active_kcps}]"
        )

    def _on_wf_trigger_toggled(self, btn: Gtk.ToggleButton) -> None:
        self.waveform_widget.trigger_lock = btn.get_active()
        self.waveform_widget.queue_draw()

    def _on_rt_waveform_toggled(self, btn: Gtk.ToggleButton) -> None:
        active = btn.get_active()
        self.waveform_widget.is_realtime = active
        if active:
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
        self._rt_telem_tick_cnt = getattr(self, "_rt_telem_tick_cnt", 0) + 1
        fetch_telem = (self._rt_telem_tick_cnt % 4) == 1

        def poll_worker() -> None:
            res_tuple = self.tb_runner.poll_live_waveforms(window_frames=3600) if self.tb_runner else None
            live_telem = None
            if fetch_telem and self.tb_runner and self.tb_runner.live_client:
                try:
                    with self.tb_runner._ipc_lock:
                        if self.tb_runner.live_client:
                            live_telem = self.tb_runner.live_client.collect_full_telemetry()
                except Exception:
                    live_telem = None
            GLib.idle_add(self._apply_realtime_waveform_poll, res_tuple, live_telem)

        threading.Thread(target=poll_worker, daemon=True).start()
        return True

    def _apply_realtime_waveform_poll(
        self,
        res_tuple: Optional[tuple],
        live_telem: Optional[TestbenchTelemetry] = None,
    ) -> bool:
        self._rt_poll_busy = False
        if live_telem is not None:
            self._apply_telemetry_to_ui(live_telem)
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
                    if active_tab is not None:
                        active_tab.stream_waveforms.pop(f"in:{node.id}", None)
                        active_tab.stream_waveforms.pop(f"out:{node.id}", None)
                        active_tab.stream_waveforms.pop(node.id, None)
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
        keep_alive = True
        hot_reload = bool(hasattr(self, "hot_reload_btn") and self.hot_reload_btn.get_active())
        run_tab = self.get_active_tab()
        run_io_keys = self._get_ordered_active_io_keys(run_tab.canvas.vm if run_tab else None)
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
            GLib.idle_add(self._on_testbench_finished, tb_res, run_tab, run_io_keys)

        threading.Thread(target=tb_worker, daemon=True).start()
        return False

    def _on_testbench_finished(
        self,
        tb_res: TestbenchRunResult,
        run_tab: Optional[GraphTabPage] = None,
        run_io_keys: Optional[Tuple[List[str], List[str]]] = None,
    ) -> bool:
        self.run_tb_btn.set_sensitive(True)
        target_tab = run_tab if run_tab is not None else self.get_active_tab()
        if target_tab is not None and target_tab.canvas.vm is not None:
            eff_in_keys, eff_out_keys = (
                run_io_keys
                if run_io_keys is not None
                else self._get_ordered_active_io_keys(target_tab.canvas.vm)
            )
            cur_in_keys, cur_out_keys = self._get_ordered_active_io_keys(target_tab.canvas.vm)
            cur_in_set = set(cur_in_keys)
            cur_out_set = set(cur_out_keys)
            target_tab.stream_waveforms.clear()
            for idx, ikey in enumerate(eff_in_keys):
                if ikey not in cur_in_set:
                    continue
                s_in = (
                    list(tb_res.multi_in_samples[idx])
                    if idx < len(tb_res.multi_in_samples)
                    else (list(tb_res.in_samples) if idx == 0 else [])
                )
                target_tab.stream_waveforms[ikey] = (s_in, [])
            for idx, okey in enumerate(eff_out_keys):
                if okey not in cur_out_set:
                    continue
                s_out = (
                    list(tb_res.multi_out_samples[idx])
                    if idx < len(tb_res.multi_out_samples)
                    else (list(tb_res.out_samples) if idx == 0 else [])
                )
                target_tab.stream_waveforms[okey] = ([], s_out)
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
            n_in = max(1, len(self.waveform_widget.multi_in_samples))
            n_out = max(1, len(self.waveform_widget.multi_out_samples))
            io_tag = f" — {n_in} In / {n_out} Out" if (n_in > 1 or n_out > 1) else ""
            has_any_out = bool(
                any(len(s) > 0 for s in self.waveform_widget.multi_out_samples)
            )
            if live_active:
                self.wf_status_lbl.set_text(f"Waveforms [● LIVE D0{io_tag}]")
                self._start_realtime_waveform_timer()
            else:
                self.wf_status_lbl.set_text(
                    f"Waveforms [SNAPSHOT{io_tag}]"
                    if has_any_out
                    else "Waveforms [FAILED]"
                )

        if tb_res.telemetry:
            if target_tab is not None:
                target_tab.last_telemetry = tb_res.telemetry
            self._apply_telemetry_to_ui(tb_res.telemetry)
        self.right_notebook.set_current_page(1)

        if tb_res.success and tb_res.out_wav_path and os.path.isfile(tb_res.out_wav_path):
            self.last_output_wav = tb_res.out_wav_path
        return False

    def _apply_telemetry_to_ui(self, telem: TestbenchTelemetry) -> None:
        self.last_telemetry = telem
        active_tab = self.get_active_tab()
        if active_tab is not None:
            active_tab.last_telemetry = telem
        if hasattr(self, "sched_telemetry_widget"):
            self.sched_telemetry_widget.set_telemetry(telem)
            self._update_sched_status_label(telem)
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
        if hasattr(self, "telemetry_log_view"):
            self._populate_telemetry_log_view(telem, getattr(self, "_focused_violation_info", None))

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
        """Persist window pane sizes, treeview column widths, and open tabs, then stop sof-testbench4 on exit."""
        if self._save_ui_state_timer_id is not None:
            GLib.source_remove(self._save_ui_state_timer_id)
            self._save_ui_state_timer_id = None
        if self._paned_layout_ready:
            self._on_main_paned_position_changed(self.hpaned_outer, None)
            self._on_left_paned_position_changed(self.left_panel.vpaned_main, None)
        self._capture_treeview_column_widths()
        self._capture_open_tabs_state()
        if self._save_ui_state_timer_id is not None:
            GLib.source_remove(self._save_ui_state_timer_id)
            self._save_ui_state_timer_id = None
        self._save_ui_state()
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
        self._capture_open_tabs_state()
        self._schedule_ui_state_save()
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
            self.status_lbl.set_text("Click ▶ Run in Testbench for live PM control.")
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
            self.status_lbl.set_text("Click ▶ Run in Testbench to toggle DSP cores live.")
            return
        en = btn.get_active()
        st, info = self.tb_runner.live_client.pm_core_enable(core_id, en)
        self.append_log(
            f"[SOFR] Live OP_PM_CTL Core {core_id} {'GET (ENABLE)' if en else 'PUT (DISABLE)'} -> status={st} {info}"
        )
        self._on_refresh_telemetry_clicked(None)

    def _on_reset_wsim_clicked(self, _btn: Gtk.Button) -> None:
        if not self.tb_runner or not self.tb_runner.live_client:
            self.status_lbl.set_text("Click ▶ Run in Testbench to reset simulation stats.")
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
            self.status_lbl.set_text("Click ▶ Run in Testbench to inspect SRAM windows.")
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
            self.status_lbl.set_text("Click ▶ Run in Testbench to run live shell commands.")
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
            self.wf_status_lbl.set_text(
                "Waveforms [SNAPSHOT]" if self.waveform_widget.out_samples else "Waveforms [IDLE]"
            )
            self.status_lbl.set_text("Stopped live SOFR testbench server.")
            self.append_log("[SOFR] Live testbench server stopped.")

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
