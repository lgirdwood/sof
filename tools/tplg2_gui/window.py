# SPDX-License-Identifier: BSD-3-Clause
"""Main GTK4 Window with 3 horizontal panels:
- Left: 4 vertical sub-panes (Base Classes, Processing Modules, Pipelines, Machine Topologies)
- Middle: Gtk.Snapshot Graph Canvas + bottom Compile/Testbench waveform & log drawer
- Right: Tree view of attributes of the selected graph node
"""

from __future__ import annotations
import os
import shutil
import subprocess
import threading
from typing import Dict, List, Optional

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from .compiler import CompileResult, TopologyCompiler
from .graph_canvas import TopologyGraphCanvas
from .layout import _compute_node_geometry, layout_graph
from .left_panes import LeftNavigationPanel
from .model import ConfigNode, GraphViewModel
from .parser import Topology2Workspace
from .right_inspector import RightAttributeInspector
from .serializer import generate_compilable_conf
from .testbench import (
    KcontrolTelemetryItem,
    SOF_PCM_OP_CAPTURE_BUFFER,
    SOF_PCM_OP_START_TONE,
    SOF_PCM_OP_STOP_TONE,
    SOF_PM_OP_RELEASE_HOLD,
    SOF_PM_OP_WAKE_HOLD,
    TestbenchRunResult,
    TestbenchRunner,
    TestbenchTelemetry,
    WaveformSnapshotWidget,
)


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

        self.workspace: Optional[Topology2Workspace] = None
        self.compiler: Optional[TopologyCompiler] = None
        self.tb_runner: Optional[TestbenchRunner] = None
        self.last_compiled_tplg: Optional[str] = None
        self.last_output_wav: Optional[str] = None
        self.last_telemetry: Optional[TestbenchTelemetry] = None
        self._discovered_kcontrols: List[KcontrolTelemetryItem] = []
        self._pending_kctl_overrides: Dict[str, List[int]] = {}
        self._last_allocated_width: int = 0
        self._last_allocated_height: int = 0

        self._build_header_bar()
        self._build_main_3panel_layout()

        # Load initial topology2 directory if valid
        default_dir = initial_dir or "/home/lrg/work/sof-arl/sof-arl/tools/topology/topology2"
        if os.path.isdir(default_dir):
            self.load_topology2_directory(default_dir)

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
        if not isinstance(focus, Gtk.Editable):
            ctrl_held = bool(state & Gdk.ModifierType.CONTROL_MASK)
            shift_held = bool(state & Gdk.ModifierType.SHIFT_MASK)
            if ctrl_held and keyval in (Gdk.KEY_z, Gdk.KEY_Z):
                if shift_held:
                    self._on_redo_clicked(None)
                else:
                    self._on_undo_clicked(None)
                return True
            if ctrl_held and keyval in (Gdk.KEY_y, Gdk.KEY_Y):
                self._on_redo_clicked(None)
                return True
        return False

    def _build_header_bar(self) -> None:
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)
        self.set_titlebar(header)

        # Left header controls: Open Topology2 Dir + Undo/Redo + Graph Layout actions
        open_btn = Gtk.Button(label="Open Topology2 Dir…")
        open_btn.set_tooltip_text("Open a tools/topology/topology2 directory")
        open_btn.connect("clicked", self._on_open_dir_clicked)
        header.pack_start(open_btn)

        self.undo_btn = Gtk.Button(label="↶ Undo")
        self.undo_btn.set_sensitive(False)
        self.undo_btn.set_tooltip_text("Undo last change (Shortcut: Ctrl+Z)")
        self.undo_btn.connect("clicked", self._on_undo_clicked)
        header.pack_start(self.undo_btn)

        self.redo_btn = Gtk.Button(label="↷ Redo")
        self.redo_btn.set_sensitive(False)
        self.redo_btn.set_tooltip_text("Redo last undone change (Shortcut: Ctrl+Y / Ctrl+Shift+Z)")
        self.redo_btn.connect("clicked", self._on_redo_clicked)
        header.pack_start(self.redo_btn)

        fit_btn = Gtk.Button(label="Fit Graph")
        fit_btn.set_tooltip_text("Fit graph to canvas (Shortcut: F)")
        fit_btn.connect("clicked", lambda _b: self.canvas.fit_to_view())
        header.pack_start(fit_btn)

        relayout_btn = Gtk.Button(label="Auto-Layout")
        relayout_btn.set_tooltip_text("Re-run Sugiyama DAG auto-layout (Shortcut: L)")
        relayout_btn.connect("clicked", self._on_relayout_clicked)
        header.pack_start(relayout_btn)

        del_btn = Gtk.Button(label="Delete Node/Route")
        del_btn.set_tooltip_text("Delete selected node or route edge (Shortcut: Del)")
        del_btn.connect("clicked", lambda _b: self.canvas.delete_selected())
        header.pack_start(del_btn)

        export_btn = Gtk.Button(label="Export .conf")
        export_btn.set_tooltip_text("Save current edited topology/pipeline to build directory")
        export_btn.connect("clicked", self._on_export_conf_clicked)
        header.pack_start(export_btn)

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

        # Pipeline IDs entry for testbench (-p)
        self.pipes_entry = Gtk.Entry()
        self.pipes_entry.set_text("1,2")
        self.pipes_entry.set_width_chars(6)
        self.pipes_entry.set_tooltip_text("Pipeline IDs passed to sof-testbench4 -p (e.g. 1,2)")
        header.pack_end(self.pipes_entry)

        pipe_lbl = Gtk.Label(label="Pipes (-p):")
        header.pack_end(pipe_lbl)

        # Format selector
        self.fmt_combo = Gtk.DropDown.new_from_strings(["S32_LE", "S24_LE", "S16_LE"])
        self.fmt_combo.set_selected(0)
        self.fmt_combo.set_tooltip_text("Testbench PCM Sample Format (-b)")
        header.pack_end(self.fmt_combo)

        # Signal selector
        self.sig_combo = Gtk.DropDown.new_from_strings(
            [
                "Sine 997 Hz",
                "Chirp 100-12kHz",
                "Multitone",
                "White Noise",
                "ALSA Front_Center.wav",
                "SOFR Remote 1kHz Tone",
            ]
        )
        self.sig_combo.set_selected(0)
        self.sig_combo.set_tooltip_text("Test audio signal to run through the pipeline")
        header.pack_end(self.sig_combo)

    def _build_main_3panel_layout(self) -> None:
        # Outer horizontal paned: [Left Panel (25%)] | [Center (50%) + Right (25%) Paned]
        self.hpaned_outer = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.hpaned_outer.set_wide_handle(True)
        self.hpaned_outer.set_resize_start_child(False)
        self.hpaned_outer.set_resize_end_child(True)
        self.hpaned_outer.set_position(390)

        # Inner horizontal paned: [Middle Graph Panel (50%)] | [Right Attribute Inspector (25%)]
        self.hpaned_inner = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.hpaned_inner.set_wide_handle(True)
        self.hpaned_inner.set_resize_start_child(True)
        self.hpaned_inner.set_resize_end_child(False)
        self.hpaned_inner.set_position(780)

        # 1. Left Hand Panel (Object View & File View notebook) wrapped in high-level panel border
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

        # 2. Middle Horizontal Panel (Graph Canvas + Bottom Testbench/Compiler Drawer)
        self.center_vpaned = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        self.center_vpaned.set_wide_handle(True)
        self.center_vpaned.set_vexpand(True)
        self.center_vpaned.set_hexpand(True)
        self.center_vpaned.set_resize_start_child(True)
        self.center_vpaned.set_resize_end_child(False)
        self.center_vpaned.set_position(640)

        self.canvas = TopologyGraphCanvas(
            on_selection_changed=self._on_canvas_selection_changed,
            on_graph_modified=self._on_graph_modified,
            on_history_changed=self._update_undo_redo_buttons,
        )
        canvas_frame = Gtk.Frame()
        canvas_frame.add_css_class("sub-panel-frame")
        canvas_frame.set_child(self.canvas)
        self.center_vpaned.set_start_child(canvas_frame)

        # Bottom drawer: Waveform comparison + SOFR Telemetry / Remote Control / Console Log
        bottom_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bottom_box.set_margin_start(4)
        bottom_box.set_margin_end(4)
        bottom_box.set_margin_top(4)
        bottom_box.set_margin_bottom(4)

        self.status_lbl = Gtk.Label(label="Ready.")
        self.status_lbl.set_xalign(0.0)
        bottom_box.append(self.status_lbl)

        # Split bottom drawer horizontally: [WaveformSnapshotWidget] | [Notebook: SOFR Telemetry / Control / Log]
        bottom_hpaned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        bottom_hpaned.set_wide_handle(True)
        bottom_hpaned.set_vexpand(True)
        bottom_hpaned.set_position(360)

        wf_frame = Gtk.Frame()
        wf_frame.add_css_class("sub-panel-frame")
        self.waveform_widget = WaveformSnapshotWidget()
        wf_frame.set_child(self.waveform_widget)
        bottom_hpaned.set_start_child(wf_frame)

        self.bottom_notebook = Gtk.Notebook()
        self.bottom_notebook.set_vexpand(True)
        self.bottom_notebook.set_hexpand(True)

        # Tab 1: SOFR Telemetry View
        telem_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        telem_box.set_margin_start(4)
        telem_box.set_margin_end(4)
        telem_box.set_margin_top(4)
        telem_box.set_margin_bottom(4)

        self.telem_summary_lbl = Gtk.Label(
            label="SOFR Telemetry: Run Testbench to query DSP cores, KCPS, SRAM banks, heap, DAPM & kcontrols."
        )
        self.telem_summary_lbl.set_xalign(0.0)
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
        self.bottom_notebook.append_page(telem_box, Gtk.Label(label="SOFR Telemetry"))

        # Tab 2: Remote Control & Kcontrols (SOFR Protocol)
        ctl_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        ctl_box.set_margin_start(6)
        ctl_box.set_margin_end(6)
        ctl_box.set_margin_top(6)
        ctl_box.set_margin_bottom(6)

        # Row 1: DSP Power, Execution, Tone & Telemetry Poll Controls
        exec_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        exec_row.append(Gtk.Label(label="DSP Control:"))

        btn_wake = Gtk.Button(label="▶ Resume (D0)")
        btn_wake.set_tooltip_text("SOF_NET_OP_PM_CTL: SOF_PM_OP_WAKE_HOLD")
        btn_wake.connect("clicked", lambda _b: self._on_remote_pm_ctl(SOF_PM_OP_WAKE_HOLD))
        exec_row.append(btn_wake)

        btn_pause = Gtk.Button(label="⏸ Pause (D3)")
        btn_pause.set_tooltip_text("SOF_NET_OP_PM_CTL: SOF_PM_OP_RELEASE_HOLD")
        btn_pause.connect("clicked", lambda _b: self._on_remote_pm_ctl(SOF_PM_OP_RELEASE_HOLD))
        exec_row.append(btn_pause)

        btn_step = Gtk.Button(label="⏭ Step 10 Ticks")
        btn_step.set_tooltip_text("SOF_NET_OP_SHELL_EXEC: step 10")
        btn_step.connect("clicked", lambda _b: self._on_remote_shell_preset("step 10"))
        exec_row.append(btn_step)

        self.remote_tone_btn = Gtk.ToggleButton(label="🎵 1kHz Tone")
        self.remote_tone_btn.set_tooltip_text("SOF_NET_OP_PCM_CTL: START_TONE / STOP_TONE")
        self.remote_tone_btn.connect("toggled", self._on_remote_tone_toggled)
        exec_row.append(self.remote_tone_btn)

        btn_poll = Gtk.Button(label="🔄 Refresh Telemetry")
        btn_poll.set_tooltip_text("Poll SOF_NET_OP_TELEM_SNAPSHOT & SOF_PCM_OP_CAPTURE_BUFFER")
        btn_poll.connect("clicked", self._on_refresh_telemetry_clicked)
        exec_row.append(btn_poll)

        btn_stop_srv = Gtk.Button(label="⏹ Stop Server")
        btn_stop_srv.set_tooltip_text("Send SOF_NET_OP_SHELL_EXEC 'quit' to stop live testbench server")
        btn_stop_srv.connect("clicked", self._on_stop_live_server_clicked)
        exec_row.append(btn_stop_srv)
        ctl_box.append(exec_row)

        # Row 2: Kcontrol Configuration (SOF_NET_OP_KCTL_LIST / SOF_NET_OP_KCTL_SET)
        kctl_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        kctl_row.append(Gtk.Label(label="Kcontrol:"))
        self.kctl_combo = Gtk.DropDown.new_from_strings(["(Run testbench to list kcontrols)"])
        self.kctl_combo.set_hexpand(True)
        self.kctl_combo.connect("notify::selected", self._on_kctl_combo_selected)
        kctl_row.append(self.kctl_combo)

        kctl_row.append(Gtk.Label(label="Values:"))
        self.kctl_val_entry = Gtk.Entry()
        self.kctl_val_entry.set_placeholder_text("e.g. 28,28")
        self.kctl_val_entry.set_width_chars(12)
        kctl_row.append(self.kctl_val_entry)

        btn_kctl_set = Gtk.Button(label="Apply Kcontrol (OP_KCTL_SET)")
        btn_kctl_set.connect("clicked", self._on_apply_kcontrol_clicked)
        kctl_row.append(btn_kctl_set)
        ctl_box.append(kctl_row)

        # Row 3: Remote Shell Command Execution (SOF_NET_OP_SHELL_EXEC)
        sh_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sh_row.append(Gtk.Label(label="Shell:"))
        self.remote_shell_entry = Gtk.Entry()
        self.remote_shell_entry.set_placeholder_text(
            "Enter testbench shell command (info, pipelines, controls, stats, kernel cycles, kernel threads…)"
        )
        self.remote_shell_entry.set_hexpand(True)
        self.remote_shell_entry.connect("activate", lambda _e: self._on_run_remote_shell_clicked(None))
        sh_row.append(self.remote_shell_entry)

        btn_sh_run = Gtk.Button(label="Send (OP_SHELL_EXEC)")
        btn_sh_run.connect("clicked", self._on_run_remote_shell_clicked)
        sh_row.append(btn_sh_run)

        for sh_cmd in ("info", "pipelines", "stats", "kernel cycles", "kernel threads"):
            b_pre = Gtk.Button(label=sh_cmd)
            b_pre.connect("clicked", lambda _b, c=sh_cmd: self._on_remote_shell_preset(c))
            sh_row.append(b_pre)
        ctl_box.append(sh_row)

        self.bottom_notebook.append_page(ctl_box, Gtk.Label(label="Remote Control & Kcontrols"))

        # Tab 3: Console & Protocol Log
        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_vexpand(True)
        log_scroll.set_hexpand(True)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        log_scroll.set_child(self.log_view)
        self.bottom_notebook.append_page(log_scroll, Gtk.Label(label="Console & Protocol Log"))

        nb_frame = Gtk.Frame()
        nb_frame.add_css_class("sub-panel-frame")
        nb_frame.set_child(self.bottom_notebook)
        bottom_hpaned.set_end_child(nb_frame)

        bottom_box.append(bottom_hpaned)
        bottom_drawer_frame = Gtk.Frame()
        bottom_drawer_frame.add_css_class("sub-panel-frame")
        bottom_drawer_frame.set_child(bottom_box)
        self.center_vpaned.set_end_child(bottom_drawer_frame)

        center_panel_frame = Gtk.Frame()
        center_panel_frame.add_css_class("high-level-panel")
        center_panel_frame.set_margin_start(2)
        center_panel_frame.set_margin_end(2)
        center_panel_frame.set_margin_top(4)
        center_panel_frame.set_margin_bottom(4)
        center_panel_frame.set_child(self.center_vpaned)

        # 3. Right Hand Panel (Tree View of Attributes of Selected Graph Node)
        self.right_inspector = RightAttributeInspector(
            on_attribute_edited=self._on_node_attribute_edited,
            on_before_attribute_edit=self._on_before_attribute_edit,
        )
        right_panel_frame = Gtk.Frame()
        right_panel_frame.add_css_class("high-level-panel")
        right_panel_frame.set_margin_start(2)
        right_panel_frame.set_margin_end(4)
        right_panel_frame.set_margin_top(4)
        right_panel_frame.set_margin_bottom(4)
        right_panel_frame.set_child(self.right_inspector)

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
            target_vpos = int(total_h * 0.72)
            self.hpaned_outer.set_position(target_left)
            self.hpaned_inner.set_position(target_center)
            self.center_vpaned.set_position(target_vpos)
            GLib.idle_add(self._sync_inner_paned_position, target_left, target_center, target_vpos)
        return GLib.SOURCE_CONTINUE

    def _sync_inner_paned_position(self, target_left: int, target_center: int, target_vpos: int) -> bool:
        self.hpaned_outer.set_position(target_left)
        self.hpaned_inner.set_position(target_center)
        self.center_vpaned.set_position(target_vpos)
        self.canvas.queue_draw()
        return False

    # -------------------------------------------------------------------------
    # Workspace Loading & Left Pane Selection Handling
    # -------------------------------------------------------------------------

    def load_topology2_directory(self, path: str) -> None:
        abs_path = os.path.abspath(path)
        self.append_log(f"Scanning Topology2 directory: {abs_path} …")
        ws = Topology2Workspace(abs_path)
        ws.scan_all()
        self.workspace = ws
        self.compiler = TopologyCompiler(abs_path)
        self.tb_runner = TestbenchRunner(abs_path)
        self.left_panel.populate(ws)

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

    def _on_left_pane_item_selected(
        self,
        pane_kind: str,
        primary_id: str,
        secondary_id: Optional[str],
    ) -> None:
        if not self.workspace:
            return
        try:
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

            self.canvas.set_graph(vm, self.workspace)
            # Auto-populate pipeline IDs for testbench
            _, _, tb_pipes = generate_compilable_conf(vm, self.workspace.root_dir)
            self.pipes_entry.set_text(",".join(str(p) for p in tb_pipes))
            self.status_lbl.set_text(
                f"Viewing {vm.title} ({len(vm.nodes)} nodes, {len(vm.edges)} routes)"
            )
        except Exception as exc:
            self.append_log(f"Error building graph for {primary_id}: {exc}")

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

    def _on_node_attribute_edited(self, config: ConfigNode, key: str, new_val: str) -> None:
        if not self.canvas.vm:
            return
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
            else:
                node.in_fmt_badge = Topology2Workspace.summarize_pin_audio_format(node.config, "in")
                node.out_fmt_badge = Topology2Workspace.summarize_pin_audio_format(node.config, "out")
            _compute_node_geometry(node)
        self.canvas.queue_draw()
        self.status_lbl.set_text(f"Updated attribute '{key}' = '{new_val}'")

    def _on_undo_clicked(self, _btn: Optional[Gtk.Button]) -> None:
        desc = self.canvas.history.undo_description
        if self.canvas.undo():
            self.status_lbl.set_text(f"Undid: {desc}")

    def _on_redo_clicked(self, _btn: Optional[Gtk.Button]) -> None:
        desc = self.canvas.history.redo_description
        if self.canvas.redo():
            self.status_lbl.set_text(f"Redid: {desc}")

    def _update_undo_redo_buttons(self) -> None:
        h = self.canvas.history
        self.undo_btn.set_sensitive(h.can_undo)
        self.redo_btn.set_sensitive(h.can_redo)
        if h.can_undo:
            self.undo_btn.set_tooltip_text(f"Undo: {h.undo_description} (Ctrl+Z)")
        else:
            self.undo_btn.set_tooltip_text("Undo last change (Shortcut: Ctrl+Z)")
        if h.can_redo:
            self.redo_btn.set_tooltip_text(f"Redo: {h.redo_description} (Ctrl+Y / Ctrl+Shift+Z)")
        else:
            self.redo_btn.set_tooltip_text("Redo last undone change (Shortcut: Ctrl+Y / Ctrl+Shift+Z)")

    def _on_graph_modified(self) -> None:
        if self.canvas.vm:
            self.status_lbl.set_text(
                f"Graph modified: {len(self.canvas.vm.nodes)} nodes, {len(self.canvas.vm.edges)} routes"
            )

    def _on_relayout_clicked(self, _btn: Gtk.Button) -> None:
        self.canvas.relayout_graph()

    def _on_export_conf_clicked(self, _btn: Gtk.Button) -> None:
        if not self.canvas.vm or not self.compiler:
            return
        res = self.compiler.compile_graph(self.canvas.vm)
        self.append_log(f"Exported compilable .conf to: {res.conf_path}")
        self.status_lbl.set_text(f"Exported: {res.conf_path}")

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
        if not self.canvas.vm or not self.compiler:
            return
        vm = self.canvas.vm
        self.status_lbl.set_text(f"Compiling {vm.title} with alsatplg -p …")
        self.compile_btn.set_sensitive(False)

        def worker() -> None:
            res = self.compiler.compile_graph(vm)
            GLib.idle_add(self._on_compile_finished, res, False)

        threading.Thread(target=worker, daemon=True).start()

    def _on_run_testbench_clicked(self, _btn: Gtk.Button) -> None:
        if not self.canvas.vm or not self.compiler or not self.tb_runner:
            return
        vm = self.canvas.vm
        self.status_lbl.set_text(f"Compiling & running {vm.title} in sof-testbench4 …")
        self.compile_btn.set_sensitive(False)
        self.run_tb_btn.set_sensitive(False)

        def worker() -> None:
            res = self.compiler.compile_graph(vm)
            GLib.idle_add(self._on_compile_finished, res, True)

        threading.Thread(target=worker, daemon=True).start()

    def _collect_kcontrol_overrides(self) -> Dict[str, List[int]]:
        """Collect any kcontrol overrides from Control nodes on the graph and pending UI edits."""
        overrides: Dict[str, List[int]] = dict(self._pending_kctl_overrides)
        if not self.canvas.vm:
            return overrides
        for node in self.canvas.vm.nodes.values():
            if node.class_category != "Control":
                continue
            cname = node.config.get_scalar("name", "")
            val_str = node.config.get_scalar("value", "") or node.config.get_scalar("volume", "")
            if cname and val_str:
                vals = [int(x.strip()) for x in str(val_str).split(",") if x.strip().lstrip("-").isdigit()]
                if vals:
                    overrides[str(cname).strip('"')] = vals
        return overrides

    def _on_compile_finished(self, res: CompileResult, run_tb_after: bool) -> bool:
        self.compile_btn.set_sensitive(True)
        self.append_log(f"$ {res.command}")
        if res.stdout.strip():
            self.append_log(res.stdout.strip())
        if res.stderr.strip():
            self.append_log(res.stderr.strip())

        if not res.success:
            self.run_tb_btn.set_sensitive(True)
            self.status_lbl.set_text(f"Compilation FAILED for {os.path.basename(res.conf_path)}")
            self.append_log("[ERROR] alsatplg compilation failed.")
            return False

        self.last_compiled_tplg = res.tplg_path
        msg = f"Compiled OK: {res.tplg_path} ({res.size_bytes} bytes)"
        self.status_lbl.set_text(msg)
        self.append_log(msg)

        if not run_tb_after:
            self.run_tb_btn.set_sensitive(True)
            return False

        # Parse testbench parameters from header bar controls
        sig_idx = self.sig_combo.get_selected()
        sig_map = {
            0: ("sine_997", ""),
            1: ("chirp", ""),
            2: ("multitone", ""),
            3: ("noise", ""),
            4: ("wav_file", "/usr/share/sounds/alsa/Front_Center.wav"),
            5: ("remote_tone", ""),
        }
        sig_kind, wav_src = sig_map.get(sig_idx, ("sine_997", ""))

        fmt_idx = self.fmt_combo.get_selected()
        bits = {0: 32, 1: 24, 2: 16}.get(fmt_idx, 32)

        pipe_txt = self.pipes_entry.get_text().strip()
        pipes = [int(x.strip()) for x in pipe_txt.split(",") if x.strip().isdigit()] or res.pipeline_ids
        kctl_overrides = self._collect_kcontrol_overrides()
        keep_alive = self.keep_alive_btn.get_active()

        def tb_worker() -> None:
            assert self.tb_runner is not None
            tb_res = self.tb_runner.run_testbench(
                tplg_path=res.tplg_path,
                pipeline_ids=pipes,
                signal_kind=sig_kind,
                rate=48000,
                channels=2,
                bits=bits,
                duration_sec=0.15,
                wav_source=wav_src,
                kcontrol_overrides=kctl_overrides,
                keep_alive=keep_alive,
            )
            GLib.idle_add(self._on_testbench_finished, tb_res)

        threading.Thread(target=tb_worker, daemon=True).start()
        return False

    def _on_testbench_finished(self, tb_res: TestbenchRunResult) -> bool:
        self.run_tb_btn.set_sensitive(True)
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
        self.waveform_widget.set_waveforms(tb_res.in_samples, tb_res.out_samples, tb_res.summary_text)

        if tb_res.telemetry:
            self._apply_telemetry_to_ui(tb_res.telemetry)

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
        if self._discovered_kcontrols:
            labels = [
                f"[{k.numid}] {k.name} = {','.join(str(v) for v in k.values)}"
                for k in self._discovered_kcontrols
            ]
            self.kctl_combo.set_model(Gtk.StringList.new(labels))
            self.kctl_combo.set_selected(0)
            self._on_kctl_combo_selected(self.kctl_combo, None)
        else:
            self.kctl_combo.set_model(Gtk.StringList.new(["(No kcontrols in active topology)"]))

    # -------------------------------------------------------------------------
    # Interactive SOFR Network Protocol Controls
    # -------------------------------------------------------------------------

    def _on_kctl_combo_selected(self, combo: Gtk.DropDown, _pspec: object) -> None:
        idx = combo.get_selected()
        if 0 <= idx < len(self._discovered_kcontrols):
            kc = self._discovered_kcontrols[idx]
            self.kctl_val_entry.set_text(",".join(str(v) for v in kc.values))

    def _on_apply_kcontrol_clicked(self, _btn: Gtk.Button) -> None:
        idx = self.kctl_combo.get_selected()
        if not (0 <= idx < len(self._discovered_kcontrols)):
            return
        kc = self._discovered_kcontrols[idx]
        raw = self.kctl_val_entry.get_text().strip()
        vals = [int(x.strip()) for x in raw.split(",") if x.strip().lstrip("-").isdigit()]
        if not vals:
            return
        self._pending_kctl_overrides[kc.name] = vals
        if self.tb_runner and self.tb_runner.live_client:
            st, _ = self.tb_runner.live_client.set_kcontrol(
                numid=kc.numid,
                name=kc.name,
                values=vals,
            )
            self.append_log(
                f"[SOFR] Live OP_KCTL_SET numid={kc.numid} '{kc.name}' = {vals} -> status={st}"
            )
            self._on_refresh_telemetry_clicked(None)
        else:
            self.status_lbl.set_text(
                f"Queued kcontrol override '{kc.name}' = {vals} for next Testbench run (or enable Keep Server -W)."
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
        op = SOF_PCM_OP_START_TONE if btn.get_active() else SOF_PCM_OP_STOP_TONE
        st, info, _ = self.tb_runner.live_client.pcm_control(op, freq_hz=1000)
        self.append_log(f"[SOFR] Live OP_PCM_CTL tone={'ON' if btn.get_active() else 'OFF'} -> status={st} {info}")
        self._on_refresh_telemetry_clicked(None)

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
            _, _, pcm_bytes = self.tb_runner.live_client.pcm_control(
                SOF_PCM_OP_CAPTURE_BUFFER,
                duration_ms=150,
            )
            if pcm_bytes:
                out_norm = self.tb_runner._decode_s16_bytes_normalized(pcm_bytes, telem.pcm_channels)
                if out_norm:
                    self.waveform_widget.set_waveforms(self.waveform_widget.in_samples, out_norm)

    def _on_stop_live_server_clicked(self, _btn: Gtk.Button) -> None:
        if self.tb_runner:
            self.tb_runner.stop_live_session()
            self.keep_alive_btn.set_active(False)
            self.status_lbl.set_text("Stopped live SOFR testbench server.")
            self.append_log("[SOFR] Live testbench server stopped.")

    def _on_play_output_clicked(self, _btn: Gtk.Button) -> None:
        if not self.last_output_wav or not os.path.isfile(self.last_output_wav):
            return
        player = shutil.which("aplay") or shutil.which("pw-play")
        if player:
            subprocess.Popen([player, self.last_output_wav])
            self.status_lbl.set_text(f"Playing {self.last_output_wav} …")
        else:
            self.status_lbl.set_text(f"Output WAV ready at: {self.last_output_wav}")

    def append_log(self, text: str) -> None:
        buf = self.log_view.get_buffer()
        end_iter = buf.get_end_iter()
        buf.insert(end_iter, text + "\n")
        mark = buf.create_mark(None, buf.get_end_iter(), False)
        self.log_view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
