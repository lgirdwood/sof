# SPDX-License-Identifier: BSD-3-Clause
"""SOF Testbench (sof-testbench4) runner, SOFR network protocol client, telemetry parser,
audio signal generator, and Gtk.Snapshot waveform viewer.

Uses the SOF Remote Server TCP protocol (SOF_NET_MAGIC = 0x534F4652 'SOFR', v1) exposed
by `sof-testbench4 -S <port> -W` to configure, control, and collect real-time DSP telemetry
from testbench runs.
"""

from __future__ import annotations
import math
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import gi
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk, Pango

from .model import debug_log_command, debug_log_line
from .tplg_ipc4 import (
    SND_SOC_TPLG_TYPE_BYTES,
    SND_SOC_TPLG_TYPE_ENUM,
    SND_SOC_TPLG_TYPE_MIXER,
    SOF_IPC4_PIPELINE_STATE_PAUSED,
    SOF_IPC4_PIPELINE_STATE_RESET,
    SOF_IPC4_PIPELINE_STATE_RUNNING,
    TplgIpc4Parser,
    TplgIpc4Plan,
    TplgKcontrolDesc,
    build_ipc4_plan,
    encode_delete_pipeline,
    encode_kcontrol_set_ipc,
    encode_large_config_set_chunks,
    encode_set_pipeline_state,
)


# -----------------------------------------------------------------------------
# SOF Remote Server Network Protocol Constants (matching sof_remote_proto.h)
# -----------------------------------------------------------------------------
SOF_NET_MAGIC = 0x534F4652  # 'SOFR'
SOF_NET_VERSION = 1
SOF_NET_DEFAULT_PORT = 9998

SOF_NET_OP_PING = 0x0000
SOF_NET_OP_GET_INFO = 0x0001
SOF_NET_OP_PM_CTL = 0x0002
SOF_NET_OP_IPC_TX = 0x0010
SOF_NET_OP_IPC_LARGE_CFG = 0x0011
SOF_NET_OP_WIN_INFO = 0x0020
SOF_NET_OP_WIN_READ = 0x0021
SOF_NET_OP_WIN_WRITE = 0x0022
SOF_NET_OP_WINSTREAM_IO = 0x0023
SOF_NET_OP_TELEM_SNAPSHOT = 0x0028
SOF_NET_OP_WORKLOAD_SIM_CFG = 0x0029
SOF_NET_OP_LLEXT_LOAD = 0x0030
SOF_NET_OP_SHELL_EXEC = 0x0040
SOF_NET_OP_PROBE_CTL = 0x0050
SOF_NET_OP_PCM_CTL = 0x0060
SOF_NET_OP_CARD_LIST = 0x0070
SOF_NET_OP_PCM_CAPS = 0x0071
SOF_NET_OP_DAPM_GRAPH = 0x0072
SOF_NET_OP_COMPR_CTL = 0x0073
SOF_NET_OP_KCTL_LIST = 0x0074
SOF_NET_OP_KCTL_GET = 0x0075
SOF_NET_OP_KCTL_SET = 0x0076

# Workload Simulation Operations & Modes (SOF_NET_OP_WORKLOAD_SIM_CFG)
SOF_WORKLOAD_SIM_OP_GET = 0
SOF_WORKLOAD_SIM_OP_SET = 1
SOF_WORKLOAD_SIM_OP_MOD_SET = 2
SOF_WORKLOAD_SIM_OP_RESET_OVERRIDES = 3

SOF_WORKLOAD_SIM_OFF = 0
SOF_WORKLOAD_SIM_VIRTUAL = 1
SOF_WORKLOAD_SIM_REALTIME = 2

SOF_WORKLOAD_MEM_SRAM = 0
SOF_WORKLOAD_MEM_DRAM = 1
SOF_WORKLOAD_MEM_AUTO = 0xFF

# SRAM Window IDs (SOF_NET_OP_WIN_READ)
SOF_CDEV_WIN_FW_REGS = 0
SOF_CDEV_WIN_OUTBOX = 1
SOF_CDEV_WIN_DEBUG = 2
SOF_CDEV_WIN_PERF = 3

# Power Management Operations (SOF_NET_OP_PM_CTL)
SOF_PM_OP_QUERY = 0
SOF_PM_OP_WAKE_HOLD = 1
SOF_PM_OP_RELEASE_HOLD = 2
SOF_PM_OP_BOOT_DSP = 3
SOF_PM_OP_CORE_GET = 4
SOF_PM_OP_CORE_PUT = 5

# Telemetry Query Flags (SOF_NET_OP_TELEM_SNAPSHOT)
SOF_TELEM_FL_FW_REGS = 1 << 0
SOF_TELEM_FL_TELEM_SLOT = 1 << 1
SOF_TELEM_FL_PERF_WIN3 = 1 << 2
SOF_TELEM_FL_MEM_STATE_TLV = 1 << 3
SOF_TELEM_FL_PWR_STATE_TLV = 1 << 4
SOF_TELEM_FL_EXT_PERF_TLV = 1 << 5
SOF_TELEM_FL_MODULES_INFO_TLV = 1 << 6
SOF_TELEM_FL_SYS_HOST_STATS = 1 << 7
SOF_TELEM_FL_WAKE_DSP = 1 << 15
SOF_TELEM_FL_ALL = (
    SOF_TELEM_FL_FW_REGS
    | SOF_TELEM_FL_TELEM_SLOT
    | SOF_TELEM_FL_PERF_WIN3
    | SOF_TELEM_FL_MEM_STATE_TLV
    | SOF_TELEM_FL_PWR_STATE_TLV
    | SOF_TELEM_FL_EXT_PERF_TLV
    | SOF_TELEM_FL_MODULES_INFO_TLV
)

# PCM Control Operations (SOF_NET_OP_PCM_CTL)
SOF_PCM_OP_STATUS = 0
SOF_PCM_OP_START_TONE = 1
SOF_PCM_OP_STOP_TONE = 2
SOF_PCM_OP_PLAY_BUFFER = 3
SOF_PCM_OP_CAPTURE_BUFFER = 4

SOF_AUDIO_API_ALSA = 1

SOF_PCM_FMT_S16_LE = 0
SOF_PCM_FMT_S24_LE = 1
SOF_PCM_FMT_S24_3LE = 2
SOF_PCM_FMT_S32_LE = 3

# Probe Control Operations (SOF_NET_OP_PROBE_CTL)
SOF_PROBE_OP_INIT = 1
SOF_PROBE_OP_DEINIT = 2
SOF_PROBE_OP_INFO_ACTIVE = 3
SOF_PROBE_OP_INFO_AVAILABLE = 4
SOF_PROBE_OP_POINTS_ADD = 5
SOF_PROBE_OP_POINTS_REMOVE = 6
SOF_PROBE_OP_READ_BUFFER = 7

# IPC4 BaseFW Large Config Parameter IDs
TB_IPC4_MODULES_INFO_GET = 9
TB_IPC4_MEMORY_STATE_INFO_GET = 14
TB_IPC4_POWER_STATE_INFO_GET = 15
TB_IPC4_PERF_MEASUREMENTS_STATE = 17
TB_IPC4_GLOBAL_PERF_DATA = 18
TB_IPC4_EXTENDED_GLOBAL_PERF_DATA = 30

SOF_CDEV_LARGE_CFG_GET = 0
SOF_CDEV_LARGE_CFG_SET = 1

WELL_KNOWN_IPC4_MODULES: Dict[int, str] = {
    0x0000: "basefw",
    0x0001: "mixin",
    0x0002: "mixout",
    0x0003: "copier",
    0x0004: "peakvol",
    0x0005: "src",
    0x0006: "asrc",
    0x0007: "probe",
    0x0008: "updown_mix",
    0x000C: "eq_iir",
    0x000D: "eq_fir",
    0x000E: "drc",
    0x000F: "tdfb",
    0x0010: "crossover",
    0x0011: "multiband_drc",
    0x0012: "aria",
    0x0094: "copier",
    0x0095: "mixin",
    0x0096: "mixout",
    0x0097: "gain",
    0x0098: "src",
}


# -----------------------------------------------------------------------------
# Structured Telemetry & Run Result Dataclasses
# -----------------------------------------------------------------------------
@dataclass
class CoreTelemetry:
    core_id: int
    enabled: bool = False
    tick_count: int = 0
    last_time_elapsed_cyc: int = 0
    max_time_elapsed_cyc: int = 0
    last_time_elapsed_us: float = 0.0
    max_time_elapsed_us: float = 0.0
    last_ccount: int = 0
    avg_kcps: int = 0
    peak_kcps: int = 0
    budget_kcps: int = 0
    cpu_load_pct: float = 0.0
    peak_load_pct: float = 0.0
    freq_hz: int = 400_000_000
    ll_deadline_violations: int = 0
    ll_active_tasks: int = 0
    ll_avg_cycles: int = 0
    ll_peak_cycles: int = 0
    ll_total_cycles: int = 0
    ll_avg_exec_us: float = 0.0
    ll_peak_exec_us: float = 0.0
    dp_run_count: int = 0
    dp_active_threads: int = 0
    dp_avg_cycles: int = 0
    dp_peak_cycles: int = 0
    dp_total_cycles: int = 0
    dp_avg_kcps: int = 0
    dp_peak_kcps: int = 0
    dp_avg_exec_us: float = 0.0
    dp_peak_exec_us: float = 0.0

    @property
    def ll_ticks(self) -> int:
        return self.tick_count

    @property
    def dp_runs(self) -> int:
        return self.dp_run_count

    @property
    def util_pct(self) -> float:
        return self.cpu_load_pct

    @property
    def ll_exec_avg_us(self) -> float:
        return self.ll_avg_exec_us if self.ll_avg_exec_us > 0 else self.last_time_elapsed_us

    @property
    def ll_exec_peak_us(self) -> float:
        return self.ll_peak_exec_us if self.ll_peak_exec_us > 0 else self.max_time_elapsed_us

    @property
    def overruns(self) -> int:
        return self.ll_deadline_violations

    @property
    def avg_cycles(self) -> int:
        return self.ll_avg_cycles or self.last_time_elapsed_cyc

    @property
    def peak_cycles(self) -> int:
        return self.ll_peak_cycles or self.max_time_elapsed_cyc


@dataclass
class PipelineSchedTelemetry:
    pipeline_id: int
    core_id: int = 0
    priority: int = 0
    period_us: int = 1000
    mem_domain: str = "SRAM"
    num_ll_modules: int = 0
    num_dp_modules: int = 0
    copy_iterations: int = 0
    avg_cycles: int = 0
    peak_cycles: int = 0
    total_cycles: int = 0
    avg_kcps: int = 0
    peak_kcps: int = 0
    avg_exec_us: float = 0.0
    peak_exec_us: float = 0.0
    xrun_count: int = 0

    @property
    def exec_avg_us(self) -> float:
        return self.avg_exec_us

    @property
    def exec_peak_us(self) -> float:
        return self.peak_exec_us

    @property
    def copies(self) -> int:
        return self.copy_iterations

    @property
    def mem_type(self) -> str:
        return self.mem_domain

    @property
    def ll_module_count(self) -> int:
        return self.num_ll_modules

    @property
    def dp_module_count(self) -> int:
        return self.num_dp_modules

    @property
    def xruns(self) -> int:
        return self.xrun_count


@dataclass
class ModulePinBufferStats:
    direction: str  # "IN" or "OUT"
    pin_index: int = 0
    buf_id: int = 0
    peer_comp_id: int = 0xFFFFFFFF
    peer_widget_name: str = ""
    rate: int = 48000
    channels: int = 2
    frame_fmt: int = 2
    buf_size: int = 0
    ll_entry_avail: int = 0
    ll_entry_free: int = 0
    ll_exit_avail: int = 0
    ll_exit_free: int = 0
    dp_ring_size: int = 0
    dp_entry_avail: int = 0
    dp_entry_free: int = 0
    dp_exit_avail: int = 0
    dp_exit_free: int = 0
    last_bytes_transferred: int = 0
    total_bytes_transferred: int = 0
    xrun_count: int = 0
    underrun_count: int = 0
    overrun_count: int = 0
    tick0_xrun_count: int = 0
    entry_rms_db: float = -120.0
    entry_peak_db: float = -120.0
    exit_rms_db: float = -120.0
    exit_peak_db: float = -120.0
    samples: List[int] = field(default_factory=list)


@dataclass
class ComponentPerfItem:
    resource_id: int
    module_id: int
    instance_id: int
    module_name: str
    widget_name: str = ""
    pipeline_id: int = 0
    core_id: int = 0
    domain: str = "LL"
    mem_domain: str = "SRAM"
    is_removed: bool = False
    avg_kcps: int = 0
    peak_kcps: int = 0
    iterations: int = 0
    last_cycles: int = 0
    avg_cycles: int = 0
    peak_cycles: int = 0
    total_cycles: int = 0
    avg_exec_us: float = 0.0
    peak_exec_us: float = 0.0
    heap_bytes: int = 0
    effective_cpc: int = 0
    cpc_violations: int = 0
    error_count: int = 0
    tick0_error_count: int = 0
    last_error_msg: str = ""
    fw_errors: List[Dict[str, Any]] = field(default_factory=list)
    ibs: int = 0
    obs: int = 0
    ll_ring_sync_iters: int = 0
    ll_ring_sync_cycles: int = 0
    in_rms_db: float = -120.0
    out_rms_db: float = -120.0
    in_pins: List[ModulePinBufferStats] = field(default_factory=list)
    out_pins: List[ModulePinBufferStats] = field(default_factory=list)

    @property
    def comp_id(self) -> int:
        return self.resource_id

    @property
    def exec_avg_us(self) -> float:
        return self.avg_exec_us

    @property
    def exec_peak_us(self) -> float:
        return self.peak_exec_us

    @property
    def mem_type(self) -> str:
        return self.mem_domain


@dataclass
class SramSegmentState:
    name: str
    total_banks: int = 0
    powered_banks: int = 0
    gated_banks: int = 0
    free_phys_pages: int = 0


@dataclass
class KcontrolTelemetryItem:
    numid: int
    name: str
    iface_str: str = "MIXER"
    type_id: int = 2
    type_str: str = "INT"
    access_str: str = "rw"
    count: int = 2
    min_val: int = 0
    max_val: int = 0
    values: List[int] = field(default_factory=list)
    enum_names: List[str] = field(default_factory=list)
    target_widget: str = ""
    pipeline_id: int = -1


@dataclass
class TestbenchTelemetry:
    connected: bool = False
    server_port: int = 0
    platform: str = "sof-testbench4"
    fw_version: str = "0.0.0.0"
    fw_filename: str = ""
    ipc_type: int = 1
    fw_state: int = 7
    dsp_power_state: int = 1
    pm_runtime_active: bool = True
    num_cores: int = 4
    enabled_cores_mask: int = 1
    dsp_clock_hz: int = 400_000_000
    total_ll_ticks: int = 0
    active_pcm_streams: int = 0
    # Workload Simulation state (SOF_NET_OP_WORKLOAD_SIM_CFG)
    workload_sim_mode: int = 0  # 0=OFF, 1=VIRTUAL, 2=REALTIME
    default_mem_domain: int = 0  # 0=SRAM, 1=DRAM
    sram_factor_x100: int = 100
    dram_factor_x100: int = 160
    dram_cycles_per_word: int = 2
    num_workload_overrides: int = 0
    # PCM status over SOF_NET_OP_PCM_CTL
    pcm_tone_running: bool = False
    pcm_device: str = "hw:0,0"
    pcm_rate: int = 48000
    pcm_channels: int = 2
    pcm_freq_hz: int = 1000
    pcm_frames_played: int = 0
    pcm_rms_level: int = 0
    pcm_peak_level: int = 0
    pcm_xruns: int = 0
    # DAPM Graph over SOF_NET_OP_DAPM_GRAPH
    dapm_total_nodes: int = 0
    dapm_powered_nodes: int = 0
    dapm_total_edges: int = 0
    dapm_active_edges: int = 0
    dapm_nodes: List[Dict[str, Any]] = field(default_factory=list)
    dapm_edges: List[Dict[str, Any]] = field(default_factory=list)
    # Memory & Heap
    hpsram: SramSegmentState = field(default_factory=lambda: SramSegmentState("HPSRAM"))
    lpsram: SramSegmentState = field(default_factory=lambda: SramSegmentState("LPSRAM"))
    total_heap_bytes: int = 0
    module_heap: Dict[str, int] = field(default_factory=dict)
    # Cores, Pipelines, Components, Kcontrols & Probes
    cores: List[CoreTelemetry] = field(default_factory=list)
    pipelines: List[PipelineSchedTelemetry] = field(default_factory=list)
    components: List[ComponentPerfItem] = field(default_factory=list)
    kcontrols: List[KcontrolTelemetryItem] = field(default_factory=list)
    probes: List[Dict[str, Any]] = field(default_factory=list)
    loaded_modules: Dict[int, str] = field(default_factory=dict)
    fw_error_logs: List[Dict[str, Any]] = field(default_factory=list)
    # Shell outputs
    shell_info: str = ""
    shell_pipelines: str = ""
    shell_stats: str = ""
    shell_cycles: str = ""
    shell_threads: str = ""
    shell_platform_cfg: str = ""
    shell_sched_telem: str = ""
    shell_mod_buffers: str = ""
    protocol_log: List[str] = field(default_factory=list)

    def format_report(self) -> str:
        pwr = "D0 (ACTIVE)" if self.dsp_power_state == 1 else f"D3 (PAUSED/{self.dsp_power_state})"
        sim_modes = {0: "OFF (HOST-MEASURED)", 1: "VIRTUAL-CPC", 2: "REALTIME-PACED"}
        mem_dom = "DRAM" if self.default_mem_domain == 1 else "SRAM"
        lines = [
            f"=== SOFR Network Protocol Telemetry (127.0.0.1:{self.server_port}) ===",
            f"Platform: {self.platform}  |  FW: v{self.fw_version} ({self.fw_filename})  |  State: {pwr}",
            f"DSP Cores: mask=0x{self.enabled_cores_mask:02x}/{self.num_cores}  |  Total LL Ticks: {self.total_ll_ticks}  |  Active PCMs: {self.active_pcm_streams}",
            f"Workload Sim: {sim_modes.get(self.workload_sim_mode, 'OFF')}  |  Mem Domain: {mem_dom} (DRAM {self.dram_factor_x100 / 100.0:.2f}x +{self.dram_cycles_per_word} cyc/w)  |  Overrides: {self.num_workload_overrides}",
            f"DAPM Graph: {self.dapm_powered_nodes}/{self.dapm_total_nodes} widgets powered, {self.dapm_active_edges}/{self.dapm_total_edges} routes active",
            f"Memory: HPSRAM {self.hpsram.powered_banks}/{self.hpsram.total_banks} banks ON ({self.hpsram.free_phys_pages} free pages)  |  "
            f"LPSRAM {self.lpsram.powered_banks}/{self.lpsram.total_banks} banks ON  |  Module Heap: {self.total_heap_bytes} B",
            f"PCM Stream: {self.pcm_frames_played} frames @ {self.pcm_rate} Hz ({self.pcm_channels} ch)  |  "
            f"RMS={self.pcm_rms_level}  Peak={self.pcm_peak_level}  Tone={'ON' if self.pcm_tone_running else 'OFF'}",
        ]
        if self.cores:
            lines.append("--- Active DSP Core Telemetry ---")
            for c in self.cores:
                if c.enabled or c.tick_count > 0:
                    freq_mhz = c.freq_hz / 1e6
                    lines.append(
                        f"  Core {c.core_id} ({freq_mhz:.0f}MHz): ticks={c.tick_count}  last={c.last_time_elapsed_cyc} cyc ({c.last_time_elapsed_us:.1f} us)  "
                        f"max={c.max_time_elapsed_cyc} cyc  LL={c.avg_kcps}/{c.peak_kcps} kcps  DP={c.dp_avg_kcps}/{c.dp_peak_kcps} kcps  "
                        f"load={c.cpu_load_pct:.2f}%  overruns={c.ll_deadline_violations}"
                    )
        if self.components:
            lines.append("--- Per-Component Performance, Buffer Level & Heap Telemetry ---")
            for comp in self.components:
                if comp.module_id == 0:
                    continue
                label = comp.widget_name or f"{comp.module_name} (0x{comp.module_id:02x}:{comp.instance_id})"
                heap_str = f"  heap={comp.heap_bytes}B" if comp.heap_bytes > 0 else ""
                cpc_str = f"  cpc={comp.effective_cpc}(v={comp.cpc_violations})" if comp.effective_cpc > 0 else ""
                lvl_str = ""
                if comp.in_rms_db > -119.0 or comp.out_rms_db > -119.0:
                    lvl_str = f"  in={comp.in_rms_db:+.1f}dB→out={comp.out_rms_db:+.1f}dB"
                lines.append(
                    f"  [{comp.domain} C{comp.core_id} {comp.mem_domain}] {label:<30} avg={comp.avg_kcps:5d} kcps  "
                    f"peak={comp.peak_kcps:5d} kcps  iters={comp.iterations:4d}  cyc={comp.total_cycles}{cpc_str}{lvl_str}{heap_str}"
                )
        if self.kcontrols:
            lines.append(f"--- Topology Kcontrols ({len(self.kcontrols)}) ---")
            for kc in self.kcontrols[:12]:
                val_str = ",".join(str(v) for v in kc.values)
                lines.append(
                    f"  [numid={kc.numid:2d}] {kc.type_str:<5} {kc.name:<42} = [{val_str}] (widget: {kc.target_widget})"
                )
        if self.shell_platform_cfg:
            lines.append("--- Platform Hardware Configuration ---")
            lines.append(self.shell_platform_cfg)
        if self.shell_sched_telem:
            lines.append("--- Scheduler & Buffer Telemetry (sched-telem) ---")
            lines.append(self.shell_sched_telem)
        return "\n".join(lines)


@dataclass
class TestbenchRunResult:
    success: bool
    command: str
    in_wav_path: str
    out_wav_path: str
    in_samples: List[float] = field(default_factory=list)
    out_samples: List[float] = field(default_factory=list)
    multi_in_samples: List[List[float]] = field(default_factory=list)
    multi_out_samples: List[List[float]] = field(default_factory=list)
    multi_out_wav_paths: List[str] = field(default_factory=list)
    in_peak_db: float = -120.0
    out_peak_db: float = -120.0
    in_rms_db: float = -120.0
    out_rms_db: float = -120.0
    stdout: str = ""
    stderr: str = ""
    summary_text: str = ""
    telemetry: Optional[TestbenchTelemetry] = None


# -----------------------------------------------------------------------------
# SOFR Network Protocol Client for sof-testbench4
# -----------------------------------------------------------------------------
class TestbenchRemoteClient:
    """TCP client implementing the SOFR binary framing protocol (sof_remote_proto.h)."""

    def __init__(self, host: str = "127.0.0.1", port: int = SOF_NET_DEFAULT_PORT, timeout: float = 2.5):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._rpc_lock = threading.RLock()
        self.req_id: int = 1
        self.protocol_log: List[str] = []
        self.cached_modules: Dict[int, str] = dict(WELL_KNOWN_IPC4_MODULES)
        # Metadata populated when pipelines/modules are created purely via IPC4 (no -t topology loaded on server)
        self.ipc_widget_map: Dict[int, Tuple[str, int]] = {}  # comp_id -> (widget_name, pipeline_id)
        self.ipc_kcontrols: List[KcontrolTelemetryItem] = []
        self.ipc_route_edges: List[Dict[str, Any]] = []

    def _log(self, msg: str) -> None:
        self.protocol_log.append(msg)
        debug_log_line("SOFR", msg)

    def connect(self) -> bool:
        with self._rpc_lock:
            self.close()
            try:
                s = socket.create_connection((self.host, self.port), timeout=self.timeout)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.settimeout(self.timeout)
                self.sock = s
                return True
            except Exception:
                self.sock = None
                return False

    def close(self) -> None:
        with self._rpc_lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

    def _recv_exact(self, n: int) -> bytes:
        if self.sock is None:
            raise ConnectionError("Socket not connected")
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Remote testbench socket closed")
            buf.extend(chunk)
        return bytes(buf)

    def rpc(self, opcode: int, payload: bytes = b"") -> Tuple[int, bytes]:
        with self._rpc_lock:
            if self.sock is None and not self.connect():
                return (-107, b"")
            rid = self.req_id
            self.req_id += 1
            hdr = struct.pack(
                "<IHHIIi",
                SOF_NET_MAGIC,
                SOF_NET_VERSION,
                opcode,
                rid,
                len(payload),
                0,
            )
            try:
                assert self.sock is not None
                self.sock.sendall(hdr + payload)
                rhdr = self._recv_exact(20)
                magic, ver, rop, rrid, plen, status = struct.unpack("<IHHIIi", rhdr)
                if magic != SOF_NET_MAGIC or ver != SOF_NET_VERSION:
                    raise ConnectionError(f"Invalid SOFR header magic=0x{magic:08x} ver={ver}")
                rdata = self._recv_exact(plen) if plen > 0 else b""
                return (status, rdata)
            except Exception:
                self.close()
                return (-104, b"")

    def ping(self) -> bool:
        st, data = self.rpc(SOF_NET_OP_PING)
        ok = st == 0 and data.startswith(b"PONG")
        if ok:
            self._log(f"[SOFR] OP_PING (0x{SOF_NET_OP_PING:04x}) -> PONG")
        return ok

    def get_info(self) -> Optional[Dict[str, Any]]:
        st, data = self.rpc(SOF_NET_OP_GET_INFO)
        if st != 0 or len(data) < 140:
            return None
        (
            abi_ver,
            ipc_type,
            fw_state,
            dsp_power_state,
            pm_active,
            num_cores,
            enabled_cores_mask,
            max_payload,
            ver_maj,
            ver_min,
            ver_mic,
            ver_bld,
            hw_ip,
            plat_bytes,
            fw_bytes,
        ) = struct.unpack_from("<IIIIIIIIHHHHI32s64s", data, 0)
        info = {
            "abi_version": abi_ver,
            "ipc_type": ipc_type,
            "fw_state": fw_state,
            "dsp_power_state": dsp_power_state,
            "pm_runtime_active": bool(pm_active),
            "num_cores": max(1, min(8, num_cores)),
            "enabled_cores_mask": enabled_cores_mask,
            "max_payload_size": max_payload,
            "fw_version": f"{ver_maj}.{ver_min}.{ver_mic}.{ver_bld}",
            "hw_ip_version": hw_ip,
            "platform": plat_bytes.split(b"\x00", 1)[0].decode("ascii", errors="replace"),
            "fw_filename": fw_bytes.split(b"\x00", 1)[0].decode("ascii", errors="replace"),
        }
        self._log(
            f"[SOFR] OP_GET_INFO -> platform={info['platform']} fw=v{info['fw_version']} "
            f"cores_mask=0x{enabled_cores_mask:02x} pwr={'D0' if dsp_power_state == 1 else 'D3'}"
        )
        return info

    def pm_control(self, op: int, core_id: int = 0) -> Tuple[int, Dict[str, Any]]:
        req = struct.pack("<IIIIIIII", op, core_id, 0, 0, 0, 0, 0, 0)
        st, data = self.rpc(SOF_NET_OP_PM_CTL, req)
        op_names = {
            SOF_PM_OP_QUERY: "QUERY",
            SOF_PM_OP_WAKE_HOLD: "WAKE_HOLD(D0)",
            SOF_PM_OP_RELEASE_HOLD: "RELEASE_HOLD(D3)",
            SOF_PM_OP_BOOT_DSP: "BOOT_DSP",
            SOF_PM_OP_CORE_GET: f"CORE_GET({core_id})",
            SOF_PM_OP_CORE_PUT: f"CORE_PUT({core_id})",
        }
        if st == 0 and len(data) >= 32:
            _, _, fw_st, pwr_st, pm_act, cmask, hold, _ = struct.unpack_from("<IIIIIIII", data, 0)
            res = {
                "fw_state": fw_st,
                "dsp_power_state": pwr_st,
                "pm_runtime_active": bool(pm_act),
                "enabled_cores_mask": cmask,
                "fd_holding_d0": bool(hold),
            }
            self._log(
                f"[SOFR] OP_PM_CTL ({op_names.get(op, str(op))}) -> status={st} "
                f"pwr={'D0' if pwr_st == 1 else 'D3'} cores=0x{cmask:02x}"
            )
            return (st, res)
        return (st, {})

    def set_perf_measurement_state(self, state_val: int = 2) -> int:
        """Configure BaseFW TB_IPC4_PERF_MEASUREMENTS_STATE (17): 0=disabled, 1=stopped, 2=started, 3=paused."""
        payload = struct.pack(
            "<IIIIII",
            0,
            0,
            TB_IPC4_PERF_MEASUREMENTS_STATE,
            SOF_CDEV_LARGE_CFG_SET,
            4,
            0,
        ) + struct.pack("<I", state_val)
        st, _ = self.rpc(SOF_NET_OP_IPC_LARGE_CFG, payload)
        state_names = {0: "DISABLED", 1: "STOPPED", 2: "STARTED", 3: "PAUSED"}
        self._log(
            f"[SOFR] OP_IPC_LARGE_CFG (PERF_MEASUREMENTS_STATE={state_names.get(state_val, str(state_val))}) -> status={st}"
        )
        return st

    def exec_shell(self, cmd: str, timeout_ms: int = 1500) -> Tuple[int, str]:
        cmd_b = cmd.encode("utf-8") + b"\x00"
        req = struct.pack("<II", timeout_ms, len(cmd_b)) + cmd_b
        st, rdata = self.rpc(SOF_NET_OP_SHELL_EXEC, req)
        out = rdata.decode("utf-8", errors="replace") if rdata else ""
        self._log(f"[SOFR] OP_SHELL_EXEC '{cmd}' -> status={st} ({len(out)} bytes)")
        debug_log_command("SOFR-SHELL", cmd, returncode=st, stdout=out)
        return (st, out)

    def pcm_control(
        self,
        op: int,
        device: str = "hw:0,0",
        rate: int = 48000,
        channels: int = 2,
        freq_hz: int = 1000,
        duration_ms: int = 150,
        api_backend: int = SOF_AUDIO_API_ALSA,
        fmt_id: int = SOF_PCM_FMT_S16_LE,
    ) -> Tuple[int, Dict[str, Any], bytes]:
        dev_b = device.encode("ascii", errors="ignore")[:31].ljust(32, b"\x00")
        req = struct.pack(
            "<I32sIIIIIIIII",
            op,
            dev_b,
            rate,
            channels,
            freq_hz,
            duration_ms,
            0,
            api_backend,
            fmt_id,
            1024,
            4,
        )
        st, rdata = self.rpc(SOF_NET_OP_PCM_CTL, req)
        op_names = {
            SOF_PCM_OP_STATUS: "STATUS",
            SOF_PCM_OP_START_TONE: f"START_TONE({freq_hz}Hz)",
            SOF_PCM_OP_STOP_TONE: "STOP_TONE",
            SOF_PCM_OP_PLAY_BUFFER: "PLAY_BUFFER",
            SOF_PCM_OP_CAPTURE_BUFFER: f"CAPTURE_BUFFER({duration_ms}ms)",
        }
        if len(rdata) >= 80:
            tone_run, adev, r_rate, r_ch, r_freq, frames, rms, peak, dsz, api_u, fmt_u, xruns = (
                struct.unpack_from("<I32sIIIQIIIIII", rdata, 0)
            )
            pcm_bytes = rdata[80 : 80 + dsz] if dsz > 0 else b""
            info = {
                "tone_running": bool(tone_run),
                "device": adev.split(b"\x00", 1)[0].decode("ascii", errors="replace"),
                "rate": r_rate,
                "channels": r_ch,
                "freq_hz": r_freq,
                "frames_played": frames,
                "rms_level": rms,
                "peak_level": peak,
                "data_size": dsz,
                "api_used": api_u,
                "format": fmt_u,
                "xruns": xruns,
            }
            if op != SOF_PCM_OP_STATUS:
                self._log(
                    f"[SOFR] OP_PCM_CTL ({op_names.get(op, str(op))}) -> frames={frames} "
                    f"rms={rms} peak={peak} captured={len(pcm_bytes)}B"
                )
            return (st, info, pcm_bytes)
        return (st, {}, b"")

    def list_kcontrols(self, card_id: int = 0) -> List[KcontrolTelemetryItem]:
        req = struct.pack("<II", card_id, SOF_AUDIO_API_ALSA)
        st, kdata = self.rpc(SOF_NET_OP_KCTL_LIST, req)
        items: List[KcontrolTelemetryItem] = []
        if st != 0 or len(kdata) < 12:
            return items
        _, _, nk = struct.unpack_from("<III", kdata, 0)
        off = 12
        khead_fmt = "<III12sIII64sI12sI12sIqqqIiiII"
        khead_sz = struct.calcsize(khead_fmt)  # 180 bytes
        kanc_fmt = "<Iii64s48siHH"
        kanc_sz = struct.calcsize(kanc_fmt)  # 132 bytes
        kinfo_sz = khead_sz + 192 + 64 + kanc_sz  # 568 bytes
        for _ in range(nk):
            if off + kinfo_sz > len(kdata):
                break
            (
                _cid,
                numid,
                _iface,
                if_b,
                _dev,
                _subdev,
                _idx,
                nm_b,
                ktype,
                tstr_b,
                _amask,
                astr_b,
                cnt,
                min_v,
                max_v,
                _step_v,
                _has_db,
                _db_min,
                _db_step,
                _db_mute,
                nenums,
            ) = struct.unpack_from(khead_fmt, kdata, off)
            enum_names: List[str] = []
            for e_i in range(min(8, nenums)):
                eb = kdata[off + khead_sz + e_i * 24 : off + khead_sz + (e_i + 1) * 24]
                ename = eb.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
                if ename:
                    enum_names.append(ename)
            v_off = off + khead_sz + 192
            vals_tuple = struct.unpack_from("<qqqqqqqq", kdata, v_off)
            a_off = v_off + 64
            _anc_type, _t_nid, _t_eid, tw_b, _tc_b, pipe_id, _lcol, _lrow = struct.unpack_from(
                kanc_fmt, kdata, a_off
            )
            off += kinfo_sz
            items.append(
                KcontrolTelemetryItem(
                    numid=numid,
                    name=nm_b.split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
                    iface_str=if_b.split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
                    type_id=ktype,
                    type_str=tstr_b.split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
                    access_str=astr_b.split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
                    count=max(1, min(cnt, 8)),
                    min_val=min_v,
                    max_val=max_v,
                    values=list(vals_tuple[: max(1, min(cnt, 8))]),
                    enum_names=enum_names,
                    target_widget=tw_b.split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
                    pipeline_id=pipe_id,
                )
            )
        self._log(f"[SOFR] OP_KCTL_LIST -> {len(items)} kcontrol(s)")
        return items

    def set_kcontrol(
        self,
        numid: int = 0,
        name: str = "",
        values: Optional[List[int]] = None,
        bytes_data: bytes = b"",
        card_id: int = 0,
    ) -> Tuple[int, Optional[KcontrolTelemetryItem]]:
        val_list = list(values or [])
        cnt = len(val_list)
        padded_vals = (val_list + [0] * 16)[:16]
        name_b = name.encode("utf-8", errors="ignore")[:63].ljust(64, b"\x00")
        b_len = min(len(bytes_data), 512)
        b_padded = bytes_data[:b_len].ljust(512, b"\x00")
        req = struct.pack(
            "<IIIIII64sI16qI512s",
            card_id,
            SOF_AUDIO_API_ALSA,
            numid,
            2,  # SND_CTL_ELEM_IFACE_MIXER
            0,
            0,
            name_b,
            cnt,
            *padded_vals,
            b_len,
            b_padded,
        )
        st, rdata = self.rpc(SOF_NET_OP_KCTL_SET, req)
        self._log(
            f"[SOFR] OP_KCTL_SET (numid={numid} name='{name}' vals={val_list}) -> status={st}"
        )
        if st == 0 and len(rdata) >= 8:
            return (st, None)
        return (st, None)

    def query_dapm_graph(self, card_id: int = 0) -> Dict[str, Any]:
        req = struct.pack("<II", card_id, 0)
        st, gdata = self.rpc(SOF_NET_OP_DAPM_GRAPH, req)
        res: Dict[str, Any] = {
            "hdr": {},
            "nodes": [],
            "edges": [],
        }
        if st != 0 or len(gdata) < 188:
            return res
        cid, cname_b, tplg_b, ncomp, nnodes, npow, nedges, nact_e, ndpcm = struct.unpack_from(
            "<I64s96sIIIIII", gdata, 0
        )
        res["hdr"] = {
            "card_id": cid,
            "card_name": cname_b.split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
            "tplg_file": tplg_b.split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
            "num_components": ncomp,
            "num_nodes": nnodes,
            "num_powered_nodes": npow,
            "num_edges": nedges,
            "num_active_edges": nact_e,
            "num_dpcm_links": ndpcm,
        }
        off = 188 + ncomp * struct.calcsize("<48s16sIII")
        dnode_fmt = "<I64s48s24sIIII48siIiHHHHIIII48s48s"
        dnode_sz = struct.calcsize(dnode_fmt)
        for _ in range(nnodes):
            if off + dnode_sz > len(gdata):
                break
            unpacked = struct.unpack_from(dnode_fmt, gdata, off)
            off += dnode_sz
            res["nodes"].append(
                {
                    "node_id": unpacked[0],
                    "name": unpacked[1].split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
                    "widget_type": unpacked[3].split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
                    "power": bool(unpacked[4]),
                    "pipeline_id": unpacked[11],
                }
            )
        dedge_fmt = "<Iii48s48s32s32s48sII"
        dedge_sz = struct.calcsize(dedge_fmt)
        for _ in range(nedges):
            if off + dedge_sz > len(gdata):
                break
            eid, sid, did, snm_b, dnm_b, _sc_b, _dc_b, ctl_b, knid, act = struct.unpack_from(
                dedge_fmt, gdata, off
            )
            off += dedge_sz
            res["edges"].append(
                {
                    "edge_id": eid,
                    "src_node_id": sid,
                    "dst_node_id": did,
                    "src_name": snm_b.split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
                    "dst_name": dnm_b.split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
                    "control_name": ctl_b.split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
                    "kcontrol_numid": knid,
                    "active": bool(act),
                }
            )
        self._log(
            f"[SOFR] OP_DAPM_GRAPH -> {npow}/{nnodes} powered widgets, {nact_e}/{nedges} active edges"
        )
        return res

    def query_probes(self, active_only: bool = False) -> List[Dict[str, Any]]:
        op = SOF_PROBE_OP_INFO_ACTIVE if active_only else SOF_PROBE_OP_INFO_AVAILABLE
        req = struct.pack("<IIII", op, 0, 0, 64)
        st, rdata = self.rpc(SOF_NET_OP_PROBE_CTL, req)
        pts: List[Dict[str, Any]] = []
        if st == 0 and len(rdata) >= 16:
            _, _, _, count = struct.unpack_from("<IIII", rdata, 0)
            off = 16
            for _ in range(min(count, 64)):
                if off + 48 > len(rdata):
                    break
                bid, purp, stag, _, wname = struct.unpack_from("<IIII32s", rdata, off)
                off += 48
                pts.append(
                    {
                        "buffer_id": bid,
                        "purpose": purp,
                        "pipeline_id": stag,
                        "widget_name": wname.split(b"\x00", 1)[0].decode("ascii", errors="replace"),
                    }
                )
        self._log(f"[SOFR] OP_PROBE_CTL -> {len(pts)} probe point(s)")
        return pts

    def probe_read_buffer(
        self,
        comp_id: int = 0,
        max_bytes: int = 4096,
    ) -> Tuple[int, int, List[float]]:
        """Read live internal 32-bit PCM exit samples from module `comp_id` via SOF_PROBE_OP_READ_BUFFER.
        Returns (status, resolved_comp_id, normalized_samples).
        """
        cap_bytes = max(64, min(8192, max_bytes))
        req = struct.pack("<IIII", SOF_PROBE_OP_READ_BUFFER, comp_id, cap_bytes, 0)
        st, rdata = self.rpc(SOF_NET_OP_PROBE_CTL, req)
        if st != 0 or len(rdata) < 16:
            return (st, comp_id, [])
        _rop, resolved_id, copied_bytes, num_pts = struct.unpack_from("<IIII", rdata, 0)
        # If target comp_id is a sink endpoint with 0 output pins, fall back to comp_id=0 (first active upstream module)
        if copied_bytes == 0 and comp_id != 0:
            req0 = struct.pack("<IIII", SOF_PROBE_OP_READ_BUFFER, 0, cap_bytes, 0)
            st0, rdata0 = self.rpc(SOF_NET_OP_PROBE_CTL, req0)
            if st0 == 0 and len(rdata0) >= 16:
                st, rdata = st0, rdata0
                _rop, resolved_id, copied_bytes, num_pts = struct.unpack_from("<IIII", rdata, 0)
        payload = rdata[16 : 16 + copied_bytes]
        n_smp = min(num_pts, len(payload) // 4)
        samples: List[float] = []
        if n_smp > 0:
            ints = struct.unpack_from(f"<{n_smp}i", payload, 0)
            samples = [max(-1.0, min(1.0, v / 2147483648.0)) for v in ints]
        self._log(
            f"[SOFR] OP_PROBE_CTL (READ_BUFFER comp=0x{resolved_id:06x}) -> {len(samples)} samples"
        )
        return (st, resolved_id, samples)

    def pm_core_enable(self, core_id: int, enable: bool) -> Tuple[int, Dict[str, Any]]:
        """Enable (`SOF_PM_OP_CORE_GET`) or disable (`SOF_PM_OP_CORE_PUT`) a DSP core."""
        op = SOF_PM_OP_CORE_GET if enable else SOF_PM_OP_CORE_PUT
        return self.pm_control(op, core_id=core_id)

    def workload_sim_control(
        self,
        op: int = SOF_WORKLOAD_SIM_OP_GET,
        mode: int = SOF_WORKLOAD_SIM_OFF,
        default_mem_domain: int = SOF_WORKLOAD_MEM_SRAM,
        dram_factor_x100: int = 160,
        dram_cycles_per_word: int = 2,
        core_freqs_hz: Optional[List[int]] = None,
        target_comp_id: int = 0,
        cpc_override: int = 0,
        mem_domain_override: int = SOF_WORKLOAD_MEM_AUTO,
    ) -> Optional[Dict[str, Any]]:
        """Query or configure CPC/Frequency/SRAM-DRAM workload simulation (`SOF_NET_OP_WORKLOAD_SIM_CFG` 0x0029)."""
        freqs = list(core_freqs_hz or [])
        padded_freqs = (freqs + [0] * 8)[:8]
        req = struct.pack(
            "<5I8I3I",
            op,
            mode,
            default_mem_domain,
            dram_factor_x100,
            dram_cycles_per_word,
            *padded_freqs,
            target_comp_id,
            cpc_override,
            mem_domain_override,
        )
        st, rdata = self.rpc(SOF_NET_OP_WORKLOAD_SIM_CFG, req)
        if st != 0 or len(rdata) < 32:
            return None
        # Since SET/MOD_SET/RESET_OVERRIDES resets host scheduler telemetry on the server,
        # advance 2 LL scheduler ticks so fresh simulated KCPS and exit samples are immediately populated.
        if op != SOF_WORKLOAD_SIM_OP_GET:
            self.exec_shell("step 2")
            req_get = struct.pack("<5I8I3I", SOF_WORKLOAD_SIM_OP_GET, *([0] * 15))
            st_g, rdata_g = self.rpc(SOF_NET_OP_WORKLOAD_SIM_CFG, req_get)
            if st_g == 0 and len(rdata_g) >= 32:
                rdata = rdata_g
        (
            rsp_status,
            r_mode,
            r_mem_dom,
            r_sram_f,
            r_dram_f,
            r_dram_cpw,
            r_ncores,
            r_novr,
        ) = struct.unpack_from("<i7I", rdata, 0)
        core_stats: List[Dict[str, Any]] = []
        off = 32
        cstat_fmt = "<6I2Q2I2Q"
        cstat_sz = struct.calcsize(cstat_fmt)
        for cid in range(min(8, max(1, r_ncores))):
            if off + cstat_sz > len(rdata):
                break
            (
                c_act,
                c_freq,
                c_util_x100,
                c_viol,
                ll_avg_k,
                ll_pk_k,
                ll_avg_ns,
                ll_pk_ns,
                dp_avg_k,
                dp_pk_k,
                dp_avg_ns,
                dp_pk_ns,
            ) = struct.unpack_from(cstat_fmt, rdata, off)
            off += cstat_sz
            core_stats.append(
                {
                    "core_id": cid,
                    "active": bool(c_act),
                    "freq_hz": c_freq,
                    "util_pct": c_util_x100 / 100.0,
                    "ll_deadline_violations": c_viol,
                    "ll_avg_kcps": ll_avg_k,
                    "ll_peak_kcps": ll_pk_k,
                    "ll_avg_exec_us": ll_avg_ns / 1000.0,
                    "ll_peak_exec_us": ll_pk_ns / 1000.0,
                    "dp_avg_kcps": dp_avg_k,
                    "dp_peak_kcps": dp_pk_k,
                    "dp_avg_exec_us": dp_avg_ns / 1000.0,
                    "dp_peak_exec_us": dp_pk_ns / 1000.0,
                }
            )
        op_names = {
            SOF_WORKLOAD_SIM_OP_GET: "GET",
            SOF_WORKLOAD_SIM_OP_SET: "SET",
            SOF_WORKLOAD_SIM_OP_MOD_SET: f"MOD_SET(0x{target_comp_id:06x})",
            SOF_WORKLOAD_SIM_OP_RESET_OVERRIDES: "RESET_OVERRIDES",
        }
        self._log(
            f"[SOFR] OP_WORKLOAD_SIM_CFG ({op_names.get(op, str(op))}) -> mode={r_mode} "
            f"mem={'DRAM' if r_mem_dom == 1 else 'SRAM'} overrides={r_novr}"
        )
        return {
            "status": rsp_status,
            "mode": r_mode,
            "default_mem_domain": r_mem_dom,
            "sram_factor_x100": r_sram_f,
            "dram_factor_x100": r_dram_f,
            "dram_cycles_per_word": r_dram_cpw,
            "num_cores": r_ncores,
            "num_overrides": r_novr,
            "cores": core_stats,
        }

    def workload_sim_get(self) -> Optional[Dict[str, Any]]:
        return self.workload_sim_control(op=SOF_WORKLOAD_SIM_OP_GET)

    def workload_sim_set(
        self,
        mode: int,
        default_mem_domain: int = SOF_WORKLOAD_MEM_SRAM,
        dram_factor_x100: int = 160,
        dram_cycles_per_word: int = 2,
        core_freqs_hz: Optional[List[int]] = None,
    ) -> Optional[Dict[str, Any]]:
        return self.workload_sim_control(
            op=SOF_WORKLOAD_SIM_OP_SET,
            mode=mode,
            default_mem_domain=default_mem_domain,
            dram_factor_x100=dram_factor_x100,
            dram_cycles_per_word=dram_cycles_per_word,
            core_freqs_hz=core_freqs_hz,
        )

    def workload_sim_set_module_override(
        self,
        target_comp_id: int,
        cpc_override: int,
        mem_domain_override: int = SOF_WORKLOAD_MEM_AUTO,
    ) -> Optional[Dict[str, Any]]:
        return self.workload_sim_control(
            op=SOF_WORKLOAD_SIM_OP_MOD_SET,
            target_comp_id=target_comp_id,
            cpc_override=cpc_override,
            mem_domain_override=mem_domain_override,
        )

    def workload_sim_reset(self) -> Optional[Dict[str, Any]]:
        return self.workload_sim_control(op=SOF_WORKLOAD_SIM_OP_RESET_OVERRIDES)

    def load_llext_module(
        self,
        so_path: str,
        lib_id: int = 1,
        mod_name: str = "",
    ) -> Tuple[int, str]:
        """Upload and dlopen() a shared library / LLEXT `.so` module image over `SOF_NET_OP_LLEXT_LOAD` (0x0030)."""
        if not os.path.isfile(so_path):
            return (-2, f"File not found: {so_path}")
        with open(so_path, "rb") as fh:
            so_bytes = fh.read()
        mname = (mod_name or os.path.splitext(os.path.basename(so_path))[0]).encode("ascii", errors="ignore")[:63]
        req = struct.pack(
            "<4I64s",
            0,  # mode
            max(1, lib_id),
            len(so_bytes),
            1,  # issue_shell_cmd
            mname.ljust(64, b"\x00"),
        )
        st, rdata = self.rpc(SOF_NET_OP_LLEXT_LOAD, req + so_bytes)
        msg = f"LLEXT load status={st}"
        if len(rdata) >= 520:
            r_lid, _fw_st, sh_b = struct.unpack_from("<Ii512s", rdata, 0)
            msg = sh_b.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip() or f"Loaded lib_id={r_lid}"
        self._log(f"[SOFR] OP_LLEXT_LOAD '{os.path.basename(so_path)}' (lib_id={lib_id}) -> {msg}")
        return (st, msg)

    def read_sram_window(
        self,
        win_id: int = SOF_CDEV_WIN_FW_REGS,
        offset: int = 0,
        size: int = 256,
        slot_type: int = 0,
    ) -> Tuple[int, bytes]:
        """Read bytes from an emulated DSP SRAM window (`SOF_NET_OP_WIN_READ` 0x0021)."""
        req_sz = max(16, min(4096, size))
        req = struct.pack("<6I", win_id, slot_type, 0, offset, req_sz, 0)
        st, rdata = self.rpc(SOF_NET_OP_WIN_READ, req)
        self._log(f"[SOFR] OP_WIN_READ (win={win_id} off={offset} sz={req_sz}) -> status={st} ({len(rdata)}B)")
        return (st, rdata)

    def send_ipc4_tx(
        self,
        primary: int,
        extension: int,
        payload: bytes = b"",
        rx_size: int = 0,
        description: str = "",
    ) -> Tuple[int, Dict[str, Any], bytes]:
        """Send raw IPC4 message via `SOF_NET_OP_IPC_TX` (0x0010)."""
        req = struct.pack("<5I", primary, extension, len(payload), rx_size, 0) + payload
        st, rdata = self.rpc(SOF_NET_OP_IPC_TX, req)
        desc_tag = f" {description}" if description else ""
        if st == 0 and len(rdata) >= 16:
            r_prim, r_ext, ipc_st, r_rx = struct.unpack_from("<IIiI", rdata, 0)
            rx_bytes = rdata[16 : 16 + r_rx] if r_rx > 0 else b""
            log_msg = (
                f"[SOFR] OP_IPC_TX{desc_tag} (0x{primary:08x}, 0x{extension:08x}, len={len(payload)}B) "
                f"-> ipc_status={ipc_st}"
            )
            self._log(log_msg)
            debug_log_command(
                "IPC4-TX",
                f"{description or 'IPC_TX'} pri=0x{primary:08x} ext=0x{extension:08x} len={len(payload)}",
                returncode=ipc_st,
                stdout=f"reply_primary=0x{r_prim:08x} reply_extension=0x{r_ext:08x}",
            )
            return (
                st,
                {
                    "reply_primary": r_prim,
                    "reply_extension": r_ext,
                    "ipc_status": ipc_st,
                },
                rx_bytes,
            )
        debug_log_command(
            "IPC4-TX",
            f"{description or 'IPC_TX'} pri=0x{primary:08x} ext=0x{extension:08x} len={len(payload)}",
            returncode=st,
            stderr="RPC error or empty response",
        )
        return (st, {}, b"")

    def get_telemetry_snapshot(self, query_mask: int = SOF_TELEM_FL_ALL) -> Optional[TestbenchTelemetry]:
        req = struct.pack("<II", query_mask, 0)
        st, raw_snap = self.rpc(SOF_NET_OP_TELEM_SNAPSHOT, req)
        cdev_info_sz = 716
        if st != 0 or len(raw_snap) < cdev_info_sz + 8:
            return None

        telem = TestbenchTelemetry(connected=True, server_port=self.port)
        (
            _abi_ver,
            ipc_type,
            fw_state,
            dsp_power_state,
            pm_active,
            num_cores,
            enabled_cores_mask,
            _max_payload,
            ver_maj,
            ver_min,
            ver_mic,
            ver_bld,
            _hw_ip,
            plat_bytes,
            fw_bytes,
        ) = struct.unpack_from("<IIIIIIIIHHHHI32s64s", raw_snap, 0)

        telem.ipc_type = ipc_type
        telem.fw_state = fw_state
        telem.dsp_power_state = dsp_power_state
        telem.pm_runtime_active = bool(pm_active)
        telem.num_cores = max(1, min(8, num_cores))
        telem.enabled_cores_mask = enabled_cores_mask
        telem.fw_version = f"{ver_maj}.{ver_min}.{ver_mic}.{ver_bld}"
        telem.platform = plat_bytes.split(b"\x00", 1)[0].decode("ascii", errors="replace")
        telem.fw_filename = fw_bytes.split(b"\x00", 1)[0].decode("ascii", errors="replace")

        flags_present, active_pcm = struct.unpack_from("<II", raw_snap, cdev_info_sz)
        telem.active_pcm_streams = active_pcm

        off = cdev_info_sz + 8
        fw_regs = raw_snap[off : off + 512]
        off += 512
        telem_slot = raw_snap[off : off + 512]
        off += 512
        perf_win3 = raw_snap[off : off + 4096]
        off += 4096

        (mem_len,) = struct.unpack_from("<I", raw_snap, off)
        off += 4
        mem_tlv = raw_snap[off : off + min(2048, mem_len)]
        off += 2048

        (pwr_len,) = struct.unpack_from("<I", raw_snap, off)
        off += 4
        pwr_tlv = raw_snap[off : off + min(256, pwr_len)]
        off += 256

        (ext_perf_len,) = struct.unpack_from("<I", raw_snap, off)
        off += 4
        ext_perf_tlv = raw_snap[off : off + min(4096, ext_perf_len)]
        off += 4096

        (mod_info_len,) = struct.unpack_from("<I", raw_snap, off)
        off += 4
        mod_info_tlv = raw_snap[off : off + min(4096, mod_info_len)]

        # 1. Modules Info TLV
        if (flags_present & SOF_TELEM_FL_MODULES_INFO_TLV) and len(mod_info_tlv) >= 4:
            (mod_count,) = struct.unpack_from("<I", mod_info_tlv, 0)
            m_off = 4
            for idx in range(min(mod_count, 64)):
                if m_off + 116 > len(mod_info_tlv):
                    break
                mod_id_rt, _ = struct.unpack_from("<HH", mod_info_tlv, m_off)
                name_raw = mod_info_tlv[m_off + 4 : m_off + 12]
                mname = name_raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
                if mname:
                    self.cached_modules[mod_id_rt] = mname
                m_off += 116
        telem.loaded_modules = dict(self.cached_modules)

        # 2. Memory State TLV (HPSRAM / LPSRAM)
        if (flags_present & SOF_TELEM_FL_MEM_STATE_TLV) and mem_len > 0:
            m_off = 0
            while m_off + 8 <= len(mem_tlv):
                ttype, tlen = struct.unpack_from("<II", mem_tlv, m_off)
                m_off += 8
                if tlen == 0 or m_off + tlen > len(mem_tlv):
                    break
                val = mem_tlv[m_off : m_off + tlen]
                m_off += tlen
                if len(val) < 16:
                    continue
                free_pages, ebb_dwords = struct.unpack_from("<II", val, 0)
                if ebb_dwords > 8 or 8 + ebb_dwords * 4 + 4 > len(val):
                    continue
                pgctl_masks = struct.unpack_from(f"<{ebb_dwords}I", val, 8) if ebb_dwords > 0 else ()
                (bank_count,) = struct.unpack_from("<I", val, 8 + ebb_dwords * 4)
                bank_count = min(bank_count, ebb_dwords * 32, 64) if ebb_dwords > 0 else 0
                seg = SramSegmentState("HPSRAM" if ttype == 1 else "LPSRAM")
                seg.total_banks = bank_count
                seg.free_phys_pages = free_pages
                for b in range(bank_count):
                    is_gated = bool((pgctl_masks[b // 32] >> (b % 32)) & 1)
                    if is_gated:
                        seg.gated_banks += 1
                    else:
                        seg.powered_banks += 1
                if ttype == 1:
                    telem.hpsram = seg
                elif ttype == 0:
                    telem.lpsram = seg

        # 3. Power State TLV
        core_kcps_map: Dict[int, int] = {}
        if (flags_present & SOF_TELEM_FL_PWR_STATE_TLV) and pwr_len > 0:
            p_off = 0
            while p_off + 8 <= len(pwr_tlv):
                ttype, tlen = struct.unpack_from("<II", pwr_tlv, p_off)
                p_off += 8
                if p_off + tlen > len(pwr_tlv):
                    break
                val = pwr_tlv[p_off : p_off + tlen]
                p_off += tlen
                if ttype == 0 and tlen >= 4:
                    (mask,) = struct.unpack_from("<I", val, 0)
                    telem.enabled_cores_mask = mask
                elif ttype == 1 and tlen >= 4:
                    n = tlen // 4
                    for cid, kv in enumerate(struct.unpack_from(f"<{n}I", val, 0)):
                        core_kcps_map[cid] = kv

        # 4. Window 0 FW Regs
        if (flags_present & SOF_TELEM_FL_FW_REGS) and len(fw_regs) >= 20:
            _fw_st, _lec, _fps, _lnec, ltr = struct.unpack_from("<IIIII", fw_regs, 0)
            telem.total_ll_ticks = ltr

        # 5. Window 2 Telemetry Slot (per-core utilization)
        clock_khz = max(1, telem.dsp_clock_hz // 1000)
        for cid in range(telem.num_cores):
            en = bool((telem.enabled_cores_mask >> cid) & 1)
            ct = CoreTelemetry(core_id=cid, enabled=en, budget_kcps=core_kcps_map.get(cid, 0))
            if (flags_present & SOF_TELEM_FL_TELEM_SLOT) and len(telem_slot) >= 4 + (cid + 1) * 32:
                (sep,) = struct.unpack_from("<I", telem_slot, 0)
                if sep != 0xFFFFFFFF:
                    t_cnt, last_el, max_el, last_cc, avg_u, peak_u, _p4k, _p8k = struct.unpack_from(
                        "<IIIIIIII", telem_slot, 4 + cid * 32
                    )
                    if t_cnt != 0xFFFFFFFF:
                        ct.tick_count = t_cnt
                        ct.last_time_elapsed_cyc = last_el
                        ct.max_time_elapsed_cyc = max_el
                        ct.last_time_elapsed_us = last_el / (telem.dsp_clock_hz / 1e6)
                        ct.max_time_elapsed_us = max_el / (telem.dsp_clock_hz / 1e6)
                        ct.last_ccount = last_cc
                        ct.avg_kcps = avg_u
                        ct.peak_kcps = peak_u
                        eff_kcps = avg_u if avg_u > 0 else ct.budget_kcps
                        ct.cpu_load_pct = min(100.0, (eff_kcps / clock_khz) * 100.0)
                        ct.peak_load_pct = min(100.0, (max(peak_u, eff_kcps) / clock_khz) * 100.0)
            telem.cores.append(ct)

        # 6. Window 3 & Extended Perf TLV (per-component kcps & cycles)
        comp_items: List[ComponentPerfItem] = []
        if (flags_present & SOF_TELEM_FL_EXT_PERF_TLV) and len(ext_perf_tlv) >= 4:
            (count,) = struct.unpack_from("<I", ext_perf_tlv, 0)
            e_off = 4
            for _ in range(min(count, 128)):
                if e_off + 32 > len(ext_perf_tlv):
                    break
                res_id, cflags, peak_kcps, avg_kcps, iters, cycles = struct.unpack_from(
                    "<IIIIQQ", ext_perf_tlv, e_off
                )
                e_off += 32
                mod_id = res_id & 0xFFFF
                inst_id = (res_id >> 16) & 0xFFFF
                if mod_id == 0:
                    continue
                mname = self.cached_modules.get(mod_id, f"mod_0x{mod_id:02x}")
                comp_items.append(
                    ComponentPerfItem(
                        resource_id=res_id,
                        module_id=mod_id,
                        instance_id=inst_id,
                        module_name=mname,
                        domain="DP" if (cflags & 1) else "LL",
                        is_removed=bool((cflags >> 1) & 1),
                        avg_kcps=avg_kcps,
                        peak_kcps=peak_kcps,
                        iterations=iters,
                        total_cycles=cycles,
                    )
                )
        elif flags_present & SOF_TELEM_FL_PERF_WIN3:
            off3 = 0
            while off3 + 32 <= len(perf_win3):
                res_id, cflags, peak_kcps, avg_kcps, iters, cycles = struct.unpack_from(
                    "<IIIIQQ", perf_win3, off3
                )
                off3 += 32
                if res_id in (0, 0xFFFFFFFF):
                    break
                mod_id = res_id & 0xFFFF
                inst_id = (res_id >> 16) & 0xFFFF
                mname = self.cached_modules.get(mod_id, f"mod_0x{mod_id:02x}")
                comp_items.append(
                    ComponentPerfItem(
                        resource_id=res_id,
                        module_id=mod_id,
                        instance_id=inst_id,
                        module_name=mname,
                        domain="DP" if (cflags & 1) else "LL",
                        is_removed=bool((cflags >> 1) & 1),
                        avg_kcps=avg_kcps,
                        peak_kcps=peak_kcps,
                        iterations=iters,
                        total_cycles=cycles,
                    )
                )
        telem.components = comp_items
        self._log(
            f"[SOFR] OP_TELEM_SNAPSHOT -> ticks={telem.total_ll_ticks} "
            f"cores={len(telem.cores)} components={len(telem.components)}"
        )
        return telem

    def collect_full_telemetry(self) -> Optional[TestbenchTelemetry]:
        """Collect composite telemetry snapshot, PCM status, DAPM graph, Kcontrols, Probes, and Shell stats."""
        telem = self.get_telemetry_snapshot(SOF_TELEM_FL_ALL)
        if telem is None:
            return None

        # PCM Status
        st_pcm, pcm_info, _ = self.pcm_control(SOF_PCM_OP_STATUS)
        if st_pcm == 0 and pcm_info:
            telem.pcm_tone_running = pcm_info.get("tone_running", False)
            telem.pcm_device = pcm_info.get("device", "hw:0,0")
            telem.pcm_rate = pcm_info.get("rate", 48000)
            telem.pcm_channels = pcm_info.get("channels", 2)
            telem.pcm_freq_hz = pcm_info.get("freq_hz", 1000)
            telem.pcm_frames_played = pcm_info.get("frames_played", 0)
            telem.pcm_rms_level = pcm_info.get("rms_level", 0)
            telem.pcm_peak_level = pcm_info.get("peak_level", 0)
            telem.pcm_xruns = pcm_info.get("xruns", 0)

        # DAPM Graph
        dapm = self.query_dapm_graph(0)
        hdr = dapm.get("hdr", {})
        telem.dapm_total_nodes = hdr.get("num_nodes", 0)
        telem.dapm_powered_nodes = hdr.get("num_powered_nodes", 0)
        telem.dapm_total_edges = hdr.get("num_edges", 0)
        telem.dapm_active_edges = hdr.get("num_active_edges", 0)
        telem.dapm_nodes = dapm.get("nodes", [])
        telem.dapm_edges = dapm.get("edges", [])

        # Kcontrols & Probes
        telem.kcontrols = self.list_kcontrols(0)
        if not telem.kcontrols and self.ipc_kcontrols:
            telem.kcontrols = list(self.ipc_kcontrols)
        telem.probes = self.query_probes(active_only=False)

        # Enrich component widget names from probes, IPC widget map, & shell pipelines
        probe_by_id = {p["buffer_id"]: p["widget_name"] for p in telem.probes if p.get("widget_name")}
        for cid, (wname, _pid) in self.ipc_widget_map.items():
            if wname:
                probe_by_id[cid] = wname

        if telem.dapm_total_nodes == 0 and self.ipc_widget_map:
            telem.dapm_total_nodes = len(self.ipc_widget_map)
            telem.dapm_powered_nodes = len(self.ipc_widget_map)
            telem.dapm_nodes = [
                {
                    "node_id": idx + 1,
                    "name": wname,
                    "widget_type": "IPC4_MOD",
                    "power": True,
                    "pipeline_id": pid,
                }
                for idx, (_cid, (wname, pid)) in enumerate(self.ipc_widget_map.items())
            ]
        if telem.dapm_total_edges == 0 and self.ipc_route_edges:
            telem.dapm_total_edges = len(self.ipc_route_edges)
            telem.dapm_active_edges = len(self.ipc_route_edges)
            telem.dapm_edges = list(self.ipc_route_edges)

        _, sh_info = self.exec_shell("info")
        _, sh_pipes = self.exec_shell("pipelines")
        _, sh_stats = self.exec_shell("stats")
        _, sh_cycles = self.exec_shell("kernel cycles")
        _, sh_threads = self.exec_shell("kernel threads")
        _, sh_plat = self.exec_shell("platform-cfg status")
        _, sh_sched = self.exec_shell("sched-telem")
        _, sh_modbuf = self.exec_shell("mod-buffers")

        telem.shell_info = sh_info.strip()
        telem.shell_pipelines = sh_pipes.strip()
        telem.shell_stats = sh_stats.strip()
        telem.shell_cycles = sh_cycles.strip()
        telem.shell_threads = sh_threads.strip()
        telem.shell_platform_cfg = sh_plat.strip()
        telem.shell_sched_telem = sh_sched.strip()
        telem.shell_mod_buffers = sh_modbuf.strip()

        # Query Workload Simulation state & per-core LL/DP stats over SOF_NET_OP_WORKLOAD_SIM_CFG (0x0029)
        wsim = self.workload_sim_get()
        if wsim:
            telem.workload_sim_mode = int(wsim.get("mode", 0))
            telem.default_mem_domain = int(wsim.get("default_mem_domain", 0))
            telem.sram_factor_x100 = int(wsim.get("sram_factor_x100", 100))
            telem.dram_factor_x100 = int(wsim.get("dram_factor_x100", 160))
            telem.dram_cycles_per_word = int(wsim.get("dram_cycles_per_word", 2))
            telem.num_workload_overrides = int(wsim.get("num_overrides", 0))
            w_cores = wsim.get("cores", [])
            for wc in w_cores:
                cid = int(wc.get("core_id", 0))
                if 0 <= cid < len(telem.cores):
                    ct = telem.cores[cid]
                    if wc.get("freq_hz", 0) > 0:
                        ct.freq_hz = int(wc["freq_hz"])
                    ct.ll_deadline_violations = int(wc.get("ll_deadline_violations", 0))
                    ct.ll_avg_exec_us = float(wc.get("ll_avg_exec_us", 0.0))
                    ct.ll_peak_exec_us = float(wc.get("ll_peak_exec_us", 0.0))
                    ct.dp_avg_kcps = int(wc.get("dp_avg_kcps", 0))
                    ct.dp_peak_kcps = int(wc.get("dp_peak_kcps", 0))
                    ct.dp_avg_exec_us = float(wc.get("dp_avg_exec_us", 0.0))
                    ct.dp_peak_exec_us = float(wc.get("dp_peak_exec_us", 0.0))
                    if wc.get("util_pct", 0.0) > 0.0:
                        ct.cpu_load_pct = float(wc["util_pct"])

        # Parse core assignment & widget names from `pipelines` shell output
        comp_core_map: Dict[int, int] = {}
        for line in telem.shell_pipelines.splitlines():
            m = re.search(r"id=0x([0-9a-fA-F]+)\s+mod=0x[0-9a-fA-F]+\s+inst=\s*\d+\s+core=(\d+)\s+(LL|DP)\s+state=\d+\s+name=(\S+)", line)
            if m:
                cid = int(m.group(1), 16)
                core_id = int(m.group(2))
                wname = m.group(4)
                comp_core_map[cid] = core_id
                if wname and wname != "dynamic_mod":
                    probe_by_id[cid] = wname

        # Parse per-core, per-pipeline, and per-module scheduling & entry/exit buffer stats from `sched-telem`
        sched_mod_map: Dict[int, Dict[str, Any]] = {}
        sched_pipes: List[PipelineSchedTelemetry] = []
        for line in telem.shell_sched_telem.splitlines():
            m_core = re.search(
                r"\[Core\s+(\d+)\s+([\d.]+)MHz\]\s+util=([\d.]+)%\s+overruns=(\d+)\s+\|\s+"
                r"LL:\s+ticks=(\d+)\s+tasks=(\d+)\s+avg=(\d+)\s+peak=(\d+)\s+tot=(\d+)\s+cyc\s+\((\d+)/(\d+)\s+kcps\)\s+exec=([\d.]+)/([\d.]+)us\s+\|\s+"
                r"DP:\s+runs=(\d+)\s+thr=(\d+)\s+avg=(\d+)\s+peak=(\d+)\s+tot=(\d+)\s+cyc\s+\((\d+)/(\d+)\s+kcps\)\s+exec=([\d.]+)/([\d.]+)us",
                line,
            )
            if m_core:
                c_idx = int(m_core.group(1))
                while len(telem.cores) <= c_idx:
                    telem.cores.append(CoreTelemetry(core_id=len(telem.cores)))
                ct = telem.cores[c_idx]
                ct.enabled = True
                ct.freq_hz = int(round(float(m_core.group(2)) * 1_000_000))
                ct.cpu_load_pct = float(m_core.group(3))
                ct.ll_deadline_violations = int(m_core.group(4))
                ct.tick_count = int(m_core.group(5))
                ct.ll_active_tasks = int(m_core.group(6))
                ct.ll_avg_cycles = int(m_core.group(7))
                ct.ll_peak_cycles = int(m_core.group(8))
                ct.ll_total_cycles = int(m_core.group(9))
                ct.avg_kcps = int(m_core.group(10))
                ct.peak_kcps = int(m_core.group(11))
                ct.ll_avg_exec_us = float(m_core.group(12))
                ct.ll_peak_exec_us = float(m_core.group(13))
                ct.dp_run_count = int(m_core.group(14))
                ct.dp_active_threads = int(m_core.group(15))
                ct.dp_avg_cycles = int(m_core.group(16))
                ct.dp_peak_cycles = int(m_core.group(17))
                ct.dp_total_cycles = int(m_core.group(18))
                ct.dp_avg_kcps = int(m_core.group(19))
                ct.dp_peak_kcps = int(m_core.group(20))
                ct.dp_avg_exec_us = float(m_core.group(21))
                ct.dp_peak_exec_us = float(m_core.group(22))
                continue

            m_pipe = re.search(
                r"\[Pipe\s+(\d+)\]\s+core=(\d+)\s+prio=(\d+)\s+per=(\d+)us\s+mem=(SRAM|DRAM)\s+"
                r"mods=(\d+)LL/(\d+)DP\s+copies=(\d+)\s+avg=(\d+)\s+peak=(\d+)\s+tot=(\d+)\s+cyc\s+"
                r"\((\d+)/(\d+)\s+kcps\)\s+exec=([\d.]+)/([\d.]+)us\s+xruns=(\d+)",
                line,
            )
            if m_pipe:
                sched_pipes.append(
                    PipelineSchedTelemetry(
                        pipeline_id=int(m_pipe.group(1)),
                        core_id=int(m_pipe.group(2)),
                        priority=int(m_pipe.group(3)),
                        period_us=int(m_pipe.group(4)),
                        mem_domain=m_pipe.group(5),
                        num_ll_modules=int(m_pipe.group(6)),
                        num_dp_modules=int(m_pipe.group(7)),
                        copy_iterations=int(m_pipe.group(8)),
                        avg_cycles=int(m_pipe.group(9)),
                        peak_cycles=int(m_pipe.group(10)),
                        total_cycles=int(m_pipe.group(11)),
                        avg_kcps=int(m_pipe.group(12)),
                        peak_kcps=int(m_pipe.group(13)),
                        avg_exec_us=float(m_pipe.group(14)),
                        peak_exec_us=float(m_pipe.group(15)),
                        xrun_count=int(m_pipe.group(16)),
                    )
                )
                continue

            m_mod = re.search(
                r"\[0x([0-9a-fA-F]+)\s+(\S+)\s*\]\s+pipe=(\d+)\s+core=(\d+)\s+(LL|DP)\s+mem=(SRAM|DRAM)\s+"
                r"iters=(\d+)\s+avg=(\d+)\s+peak=(\d+)\s+tot=(\d+)\s+cyc\s+\((\d+)/(\d+)\s+kcps\)\s+"
                r"exec=([\d.]+)/([\d.]+)us\s+ibs=(\d+)\s+obs=(\d+)\s+cpc=(\d+)\(viol=(\d+)\)",
                line,
            )
            if m_mod:
                cid = int(m_mod.group(1), 16)
                wname = m_mod.group(2)
                if wname and wname != "comp":
                    probe_by_id[cid] = wname
                in_rms = -120.0
                out_rms = -120.0
                m_in = re.search(r"IN0:.*?rms=([+-]?\d+\.\d+)dBFS", line)
                if m_in:
                    in_rms = float(m_in.group(1))
                m_out = re.search(r"OUT0:.*?rms=([+-]?\d+\.\d+)dBFS", line)
                if m_out:
                    out_rms = float(m_out.group(1))
                ll_sync_it = 0
                ll_sync_cyc = 0
                m_sync = re.search(r"ll_sync=(\d+)\((\d+)\s+cyc\)", line)
                if m_sync:
                    ll_sync_it = int(m_sync.group(1))
                    ll_sync_cyc = int(m_sync.group(2))
                mod_err_cnt = 0
                mod_t0_err_cnt = 0
                m_err = re.search(r"err=(\d+)(?:\(t0=(\d+)\))?", line)
                if m_err:
                    mod_err_cnt = int(m_err.group(1))
                    if m_err.group(2) is not None:
                        mod_t0_err_cnt = int(m_err.group(2))
                sched_mod_map[cid] = {
                    "pipeline_id": int(m_mod.group(3)),
                    "core_id": int(m_mod.group(4)),
                    "domain": m_mod.group(5),
                    "mem_domain": m_mod.group(6),
                    "iterations": int(m_mod.group(7)),
                    "avg_cycles": int(m_mod.group(8)),
                    "peak_cycles": int(m_mod.group(9)),
                    "total_cycles": int(m_mod.group(10)),
                    "avg_kcps": int(m_mod.group(11)),
                    "peak_kcps": int(m_mod.group(12)),
                    "avg_exec_us": float(m_mod.group(13)),
                    "peak_exec_us": float(m_mod.group(14)),
                    "ibs": int(m_mod.group(15)),
                    "obs": int(m_mod.group(16)),
                    "effective_cpc": int(m_mod.group(17)),
                    "cpc_violations": int(m_mod.group(18)),
                    "error_count": mod_err_cnt,
                    "tick0_error_count": mod_t0_err_cnt,
                    "ll_ring_sync_iters": ll_sync_it,
                    "ll_ring_sync_cycles": ll_sync_cyc,
                    "in_rms_db": in_rms,
                    "out_rms_db": out_rms,
                }
                continue

            m_fw_err = re.search(
                r"\[FW_ERR\s+tick=(\d+)\s+level=(\S+)\s+pipe=(-?\d+)\s+comp=0x([0-9a-fA-F]+)\s+"
                r"name=(\S+)\s+pin=(IN|OUT|NONE):(-?\d+)\s+src=([^:]+):(\d+)\]\s*(.+)$",
                line.strip(),
            )
            if m_fw_err:
                telem.fw_error_logs.append(
                    {
                        "tick": int(m_fw_err.group(1)),
                        "level": m_fw_err.group(2),
                        "pipe_id": int(m_fw_err.group(3)),
                        "comp_id": int(m_fw_err.group(4), 16),
                        "widget_name": m_fw_err.group(5),
                        "pin_dir": m_fw_err.group(6),
                        "pin_index": int(m_fw_err.group(7)),
                        "file": m_fw_err.group(8),
                        "line": int(m_fw_err.group(9)),
                        "msg": m_fw_err.group(10).strip(),
                    }
                )
        telem.pipelines = sorted(sched_pipes, key=lambda p: (p.core_id, p.pipeline_id))

        # Parse detailed per-module & per-pin buffer stats from `mod-buffers` output
        mod_buf_map: Dict[int, Dict[str, Any]] = {}
        cur_mod_cid: Optional[int] = None
        cur_pin_obj: Optional[ModulePinBufferStats] = None
        for line in telem.shell_mod_buffers.splitlines():
            m_hdr = re.match(
                r"^\[0x([0-9a-fA-F]+)\s+(\S+)\]\s+pipe=(\d+)\s+core=(\d+)\s+domain=(LL|DP)\s+"
                r"iters=(\d+)\s+cyc\(last=(\d+)\s+avg=(\d+)\s+peak=(\d+)\)(?:\s+err=(\d+)(?:\(t0=(\d+)\))?)?",
                line.strip(),
            )
            if m_hdr:
                cur_mod_cid = int(m_hdr.group(1), 16)
                wname = m_hdr.group(2)
                if wname and wname != "comp":
                    probe_by_id[cur_mod_cid] = wname
                mod_buf_map[cur_mod_cid] = {
                    "pipeline_id": int(m_hdr.group(3)),
                    "core_id": int(m_hdr.group(4)),
                    "domain": m_hdr.group(5),
                    "iterations": int(m_hdr.group(6)),
                    "last_cycles": int(m_hdr.group(7)),
                    "avg_cycles": int(m_hdr.group(8)),
                    "peak_cycles": int(m_hdr.group(9)),
                    "error_count": int(m_hdr.group(10)) if m_hdr.group(10) is not None else 0,
                    "tick0_error_count": int(m_hdr.group(11)) if m_hdr.group(11) is not None else 0,
                    "last_error_msg": "",
                    "in_pins": [],
                    "out_pins": [],
                }
                cur_pin_obj = None
                continue

            if cur_mod_cid is not None:
                m_fw_msg = re.match(r"^\s*FW_ERR:\s*(.+)$", line)
                if m_fw_msg:
                    mod_buf_map[cur_mod_cid]["last_error_msg"] = m_fw_msg.group(1).strip()
                    continue

                m_pin = re.match(
                    r"^\s*(IN|OUT)\s*\[(\d+)\]\s+buf=(\d+)\s+peer=0x([0-9a-fA-F]+)\s+"
                    r"(\d+)Hz/(\d+)ch/fmt=(\d+)\s+size=(\d+)B\s+"
                    r"LL\(entry=(\d+)/(\d+)B\s*->\s*exit=(\d+)/(\d+)B\)",
                    line,
                )
                if m_pin:
                    direction = m_pin.group(1)
                    peer_cid = int(m_pin.group(4), 16)
                    pin_stat = ModulePinBufferStats(
                        direction=direction,
                        pin_index=int(m_pin.group(2)),
                        buf_id=int(m_pin.group(3)),
                        peer_comp_id=peer_cid,
                        rate=int(m_pin.group(5)),
                        channels=int(m_pin.group(6)),
                        frame_fmt=int(m_pin.group(7)),
                        buf_size=int(m_pin.group(8)),
                        ll_entry_avail=int(m_pin.group(9)),
                        ll_entry_free=int(m_pin.group(10)),
                        ll_exit_avail=int(m_pin.group(11)),
                        ll_exit_free=int(m_pin.group(12)),
                    )
                    m_dp = re.search(
                        r"DP_RING\(size=(\d+)B\s+entry=(\d+)/(\d+)B\s*->\s*exit=(\d+)/(\d+)B\)",
                        line,
                    )
                    if m_dp:
                        pin_stat.dp_ring_size = int(m_dp.group(1))
                        pin_stat.dp_entry_avail = int(m_dp.group(2))
                        pin_stat.dp_entry_free = int(m_dp.group(3))
                        pin_stat.dp_exit_avail = int(m_dp.group(4))
                        pin_stat.dp_exit_free = int(m_dp.group(5))
                    m_xfer = re.search(r"(?:consumed|produced)=(\d+)B\(tot=(\d+)B\)", line)
                    if m_xfer:
                        pin_stat.last_bytes_transferred = int(m_xfer.group(1))
                        pin_stat.total_bytes_transferred = int(m_xfer.group(2))
                    m_xrun = re.search(
                        r"xruns=(\d+)\(underrun=(\d+)\s+overrun=(\d+)(?:\s+t0=(\d+))?\)",
                        line,
                    )
                    if m_xrun:
                        pin_stat.xrun_count = int(m_xrun.group(1))
                        pin_stat.underrun_count = int(m_xrun.group(2))
                        pin_stat.overrun_count = int(m_xrun.group(3))
                        if m_xrun.group(4) is not None:
                            pin_stat.tick0_xrun_count = int(m_xrun.group(4))
                    m_erms = re.search(r"entry_rms=([+-]?\d+\.\d+)dB(?:\(pk=([+-]?\d+\.\d+)dB\))?", line)
                    if m_erms:
                        pin_stat.entry_rms_db = float(m_erms.group(1))
                        if m_erms.group(2) is not None:
                            pin_stat.entry_peak_db = float(m_erms.group(2))
                    m_xrms = re.search(r"exit_rms=([+-]?\d+\.\d+)dB(?:\(pk=([+-]?\d+\.\d+)dB\))?", line)
                    if m_xrms:
                        pin_stat.exit_rms_db = float(m_xrms.group(1))
                        if m_xrms.group(2) is not None:
                            pin_stat.exit_peak_db = float(m_xrms.group(2))
                    if direction == "IN":
                        mod_buf_map[cur_mod_cid]["in_pins"].append(pin_stat)
                    else:
                        mod_buf_map[cur_mod_cid]["out_pins"].append(pin_stat)
                    cur_pin_obj = pin_stat
                    continue

                if cur_pin_obj is not None:
                    m_smp = re.match(r"^\s*(?:entry_samples|exit_samples)\[\d+\]:\s*(.+)$", line)
                    if m_smp:
                        vals: List[int] = []
                        for tok in m_smp.group(1).split():
                            try:
                                vals.append(int(tok))
                            except ValueError:
                                pass
                        cur_pin_obj.samples = vals

        # Ensure any active modules reported by sched-telem/mod-buffers are present in telem.components
        known_comp_ids = {c.resource_id for c in telem.components}
        for cid in list(sched_mod_map.keys()) + list(mod_buf_map.keys()):
            if cid not in known_comp_ids:
                known_comp_ids.add(cid)
                mod_id = cid & 0xFFFF
                inst_id = (cid >> 16) & 0xFFFF
                mname = self.cached_modules.get(mod_id, f"mod_0x{mod_id:02x}")
                telem.components.append(
                    ComponentPerfItem(
                        resource_id=cid,
                        module_id=mod_id,
                        instance_id=inst_id,
                        module_name=mname,
                    )
                )

        # Parse per-module heap usage from `stats` shell output
        in_heap_sec = False
        total_heap = 0
        for line in telem.shell_stats.splitlines():
            if "=== Module Heap Usage ===" in line:
                in_heap_sec = True
                continue
            if in_heap_sec:
                m = re.match(r"^\s*(\S+)\s+(\d+)\s+bytes", line)
                if m:
                    m_key = m.group(1)
                    b_val = int(m.group(2))
                    telem.module_heap[m_key] = telem.module_heap.get(m_key, 0) + b_val
                    total_heap += b_val
        telem.total_heap_bytes = total_heap

        for comp in telem.components:
            raw_wname = probe_by_id.get(comp.resource_id, "")
            # Strip any trailing byte/dB summary appended by probe point info if present
            if " " in raw_wname:
                raw_wname = raw_wname.split(" ", 1)[0]
            comp.widget_name = raw_wname
            if comp.resource_id in self.ipc_widget_map:
                ipc_wname, ipc_pid = self.ipc_widget_map[comp.resource_id]
                if ipc_wname:
                    comp.widget_name = ipc_wname
                if comp.pipeline_id == 0:
                    comp.pipeline_id = ipc_pid
            if comp.resource_id in comp_core_map:
                comp.core_id = comp_core_map[comp.resource_id]
            sinfo = sched_mod_map.get(comp.resource_id)
            if sinfo:
                comp.pipeline_id = sinfo["pipeline_id"]
                comp.core_id = sinfo["core_id"]
                comp.domain = sinfo["domain"]
                comp.mem_domain = sinfo["mem_domain"]
                comp.iterations = sinfo["iterations"]
                comp.avg_cycles = sinfo["avg_cycles"]
                comp.peak_cycles = sinfo["peak_cycles"]
                comp.total_cycles = sinfo["total_cycles"]
                comp.avg_kcps = sinfo["avg_kcps"]
                comp.peak_kcps = sinfo["peak_kcps"]
                comp.avg_exec_us = sinfo["avg_exec_us"]
                comp.peak_exec_us = sinfo["peak_exec_us"]
                comp.ibs = sinfo["ibs"]
                comp.obs = sinfo["obs"]
                comp.effective_cpc = sinfo["effective_cpc"]
                comp.cpc_violations = sinfo["cpc_violations"]
                comp.error_count = sinfo.get("error_count", 0)
                comp.tick0_error_count = sinfo.get("tick0_error_count", 0)
                comp.ll_ring_sync_iters = sinfo["ll_ring_sync_iters"]
                comp.ll_ring_sync_cycles = sinfo["ll_ring_sync_cycles"]
                comp.in_rms_db = sinfo["in_rms_db"]
                comp.out_rms_db = sinfo["out_rms_db"]
            binfo = mod_buf_map.get(comp.resource_id)
            if binfo:
                comp.last_cycles = binfo["last_cycles"]
                if comp.avg_cycles == 0:
                    comp.avg_cycles = binfo["avg_cycles"]
                if comp.peak_cycles == 0:
                    comp.peak_cycles = binfo["peak_cycles"]
                if binfo.get("error_count", 0) > comp.error_count:
                    comp.error_count = binfo["error_count"]
                if binfo.get("tick0_error_count", 0) > comp.tick0_error_count:
                    comp.tick0_error_count = binfo["tick0_error_count"]
                if binfo.get("last_error_msg"):
                    comp.last_error_msg = binfo["last_error_msg"]
                comp.in_pins = list(binfo["in_pins"])
                comp.out_pins = list(binfo["out_pins"])
                for p in comp.in_pins + comp.out_pins:
                    if p.peer_comp_id in probe_by_id:
                        p.peer_widget_name = probe_by_id[p.peer_comp_id]
                    elif p.peer_comp_id in self.ipc_widget_map:
                        p.peer_widget_name = self.ipc_widget_map[p.peer_comp_id][0]
            comp.fw_errors = [
                fe for fe in telem.fw_error_logs if fe.get("comp_id") == comp.resource_id
            ]
            if comp.fw_errors and not comp.last_error_msg:
                comp.last_error_msg = str(comp.fw_errors[-1].get("msg", ""))
            if comp.widget_name and comp.widget_name in telem.module_heap:
                comp.heap_bytes = telem.module_heap[comp.widget_name]
            elif comp.module_name in telem.module_heap:
                comp.heap_bytes = telem.module_heap[comp.module_name]

        telem.protocol_log = list(self.protocol_log)
        return telem


# -----------------------------------------------------------------------------
# SOF Testbench Runner (Launches sof-testbench4 -S <port> -W and drives SOFR)
# -----------------------------------------------------------------------------
class TestbenchRunner:
    """Executes sof-testbench4 with the SOFR TCP server enabled (-S <port> -W) and configures,
    controls, and queries telemetry over the network protocol.
    """

    def __init__(self, topology2_root: str, work_dir: Optional[str] = None):
        self.topology2_root = os.path.abspath(topology2_root)
        self.work_dir = work_dir or os.path.join(tempfile.gettempdir(), "sof_tplg2_gui_build")
        os.makedirs(self.work_dir, exist_ok=True)
        self.testbench_bin = self._find_testbench_bin()
        # Optional persistent live server process and client
        self.live_proc: Optional[subprocess.Popen] = None
        self.live_client: Optional[TestbenchRemoteClient] = None
        self.live_port: int = 0
        # Target hardware preset & CLI simulation parameters
        self.platform_preset: str = "mtl"
        self.num_cores_override: int = 0
        self.mem_config_override: str = ""
        self.workload_sim_mode: int = SOF_WORKLOAD_SIM_OFF  # 0=off, 1=virtual, 2=realtime
        self.core_freq_override: str = ""
        self.mem_domain_override: str = ""  # "" or "sram" or "dram:160:2"
        self.trace_level: int = 0
        self.copy_iterations: int = 0
        self.remote_tone_freq_hz: int = 1000
        # Active internal module probe tap (0 = disabled)
        self.active_probe_comp_id: int = 0
        self.active_probe_widget_name: str = ""
        # Active IPC-created pipeline state on the persistent interactive server (started without -t)
        self.active_ipc_plan: Optional[TplgIpc4Plan] = None
        self.active_tplg_path: str = ""
        self.active_pipeline_ids: List[int] = []
        self._active_preset_applied: str = ""
        self._ipc_lock = threading.Lock()
        # Cached parameters & buffers for real-time waveform streaming
        self.last_in_samples: List[float] = []
        self.last_out_raw_path: str = ""
        self.last_rate: int = 48000
        self.last_rate_out: int = 48000
        self.last_channels: int = 2
        self.last_channels_out: int = 2
        self.last_bits: int = 32
        self.last_signal_kind: str = "sine_997"
        self.rt_phase_idx: int = 0

    @staticmethod
    def map_platform_to_tb_preset(platform_id: str) -> str:
        """Map topology2 platform architecture ID to sof-testbench4 `-A <preset>`."""
        plat = (platform_id or "mtl").strip().lower()
        preset_map = {
            "tgl": "tgl",
            "tgl-h": "tgl-h",
            "adl": "tgl",
            "rpl": "tgl",
            "mtl": "mtl",
            "arl": "arl",
            "lnl": "lnl",
            "ptl": "ptl",
            "wcl": "wcl",
            "nvl": "nvl",
            "nvl-s": "nvl-s",
        }
        return preset_map.get(plat, "mtl")

    def _find_testbench_bin(self) -> str:
        sof_root = os.path.abspath(os.path.join(self.topology2_root, "..", "..", ".."))
        workspace_root = os.path.abspath(os.path.join(sof_root, ".."))
        candidates = [
            os.path.join(sof_root, "tools", "testbench", "build_testbench", "sof-testbench4"),
            os.path.join(sof_root, "tools", "testbench", "build_testbench", "install", "bin", "sof-testbench4"),
            os.path.join(workspace_root, "tools", "bin", "sof-testbench4"),
            "/home/lrg/work/work-extra/sof-host/build_testbench/sof-testbench4",
            shutil.which("sof-testbench4") or "",
        ]
        for c in candidates:
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        return "sof-testbench4"

    @staticmethod
    def _allocate_free_port(preferred: int = 9998) -> int:
        for port in (preferred, 0):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(("127.0.0.1", port))
                    return s.getsockname()[1]
            except Exception:
                continue
        return 9998

    def generate_input_signal(
        self,
        signal_kind: str,
        rate: int,
        channels: int,
        bits: int,
        duration_sec: float = 0.15,
        wav_source: str = "",
        stem: str = "tb_input",
    ) -> Tuple[str, str, List[float]]:
        """Create input .raw and .wav files and return (raw_path, wav_path, normalized_mono_samples)."""
        in_raw = os.path.join(self.work_dir, f"{stem}.raw")
        in_wav = os.path.join(self.work_dir, f"{stem}.wav")
        n_frames = max(256, int(rate * duration_sec))

        frames_ch0: List[float] = []
        if signal_kind == "wav_file" and wav_source and os.path.isfile(wav_source):
            if shutil.which("sox"):
                subprocess.run(
                    [
                        "sox",
                        "--encoding",
                        "signed-integer",
                        wav_source,
                        "-L",
                        "-r",
                        str(rate),
                        "-c",
                        str(channels),
                        "-b",
                        "32" if bits == 24 else str(bits),
                        in_raw,
                        "trim",
                        "0",
                        f"{duration_sec:.2f}",
                    ],
                    check=False,
                    capture_output=True,
                )
                frames_ch0 = self._read_raw_normalized(in_raw, channels, bits)
                self._write_wav_from_normalized(in_wav, frames_ch0, rate)
                return (in_raw, in_wav, frames_ch0)

        # Synthesize clean mathematical test waveform
        rng = random.Random(42)
        pink_b0 = pink_b1 = pink_b2 = 0.0
        impulse_period = max(1, rate // 20)
        for i in range(n_frames):
            t = i / float(rate)
            if signal_kind == "sine_1000":
                val = 0.50 * math.sin(2.0 * math.pi * 1000.0 * t)
            elif signal_kind == "sine_440":
                val = 0.50 * math.sin(2.0 * math.pi * 440.0 * t)
            elif signal_kind == "chirp":
                f0, f1 = 100.0, min(12000.0, rate * 0.4)
                k = (f1 - f0) / max(duration_sec, 0.01)
                phase = 2.0 * math.pi * (f0 * t + 0.5 * k * t * t)
                val = 0.50 * math.sin(phase)
            elif signal_kind == "multitone":
                val = (
                    0.25 * math.sin(2.0 * math.pi * 250.0 * t)
                    + 0.25 * math.sin(2.0 * math.pi * 997.0 * t)
                    + 0.20 * math.sin(2.0 * math.pi * 4000.0 * t)
                )
            elif signal_kind == "square":
                val = 0.45 if math.sin(2.0 * math.pi * 500.0 * t) >= 0.0 else -0.45
            elif signal_kind == "sawtooth":
                cyc = 500.0 * t
                val = 0.45 * (2.0 * (cyc - math.floor(0.5 + cyc)))
            elif signal_kind in ("noise", "white_noise"):
                val = 0.35 * rng.uniform(-1.0, 1.0)
            elif signal_kind == "pink_noise":
                wn = rng.uniform(-1.0, 1.0)
                pink_b0 = 0.99765 * pink_b0 + wn * 0.0990460
                pink_b1 = 0.96300 * pink_b1 + wn * 0.2965164
                pink_b2 = 0.57000 * pink_b2 + wn * 1.0526913
                val = max(-0.85, min(0.85, 0.14 * (pink_b0 + pink_b1 + pink_b2 + wn * 0.1848)))
            elif signal_kind == "impulse":
                val = 0.85 if (i % impulse_period) == 0 else 0.0
            elif signal_kind == "remote_tone":
                val = 0.50 * math.sin(2.0 * math.pi * 1000.0 * t)
            else:
                # Default: 997 Hz Sine at -6 dBFS
                val = 0.50 * math.sin(2.0 * math.pi * 997.0 * t)
            frames_ch0.append(val)

        self._write_raw_from_normalized(in_raw, frames_ch0, channels, bits)
        self._write_wav_from_normalized(in_wav, frames_ch0, rate)
        return (in_raw, in_wav, frames_ch0)

    def stop_live_session(self) -> None:
        with self._ipc_lock:
            if self.live_client is not None and self.active_ipc_plan is not None:
                try:
                    self._destroy_active_pipelines_unlocked()
                except Exception:
                    pass
            if self.live_client is not None:
                try:
                    self.live_client.exec_shell("quit")
                except Exception:
                    pass
                self.live_client.close()
                self.live_client = None
            if self.live_proc is not None:
                try:
                    self.live_proc.wait(timeout=1.5)
                except Exception:
                    try:
                        self.live_proc.kill()
                    except Exception:
                        pass
                self.live_proc = None
            self.live_port = 0
            self.active_ipc_plan = None
            self.active_tplg_path = ""
            self.active_pipeline_ids = []
            self._active_preset_applied = ""

    def start_interactive_server(
        self,
        platform_preset: Optional[str] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Ensure sof-testbench4 is running in interactive server mode (`-I -S <port> -W -A <preset>`)
        WITHOUT loading any topology (`-t`), and apply the target platform preset.
        """
        eff_preset = self.map_platform_to_tb_preset(platform_preset or self.platform_preset)
        self.platform_preset = eff_preset

        with self._ipc_lock:
            if (
                self.live_client is not None
                and self.live_proc is not None
                and self.live_proc.poll() is None
                and self.live_client.ping()
            ):
                if self._active_preset_applied != eff_preset:
                    _st, out = self.live_client.exec_shell(f"platform-cfg preset {eff_preset}")
                    self._active_preset_applied = eff_preset
                    if log_callback and out.strip():
                        log_callback(f"[SOFR :{self.live_port}] {out.strip()}")
                return True

            # Clean up any stale process before starting a fresh interactive server
            if self.live_client is not None:
                self.live_client.close()
                self.live_client = None
            if self.live_proc is not None:
                try:
                    self.live_proc.kill()
                except Exception:
                    pass
                self.live_proc = None
            self.active_ipc_plan = None
            self.active_tplg_path = ""
            self.active_pipeline_ids = []

            port = self._allocate_free_port(SOF_NET_DEFAULT_PORT)
            cmd = [
                self.testbench_bin,
                "-I",
                "-S",
                str(port),
                "-W",
                "-A",
                eff_preset,
                "-w",
                str(self.workload_sim_mode if isinstance(self.workload_sim_mode, int) else 1),
            ]
            if self.num_cores_override > 0:
                cmd.extend(["-N", str(self.num_cores_override)])
            if self.mem_config_override.strip():
                cmd.extend(["-m", self.mem_config_override.strip()])
            if self.trace_level > 0:
                cmd.extend(["-d", str(min(4, self.trace_level))])

            cmd_str = " ".join(f'"{a}"' if " " in a else a for a in cmd)
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except Exception as exc:
                debug_log_command("SOF-TESTBENCH4-START", cmd_str, returncode=-1, stderr=str(exc))
                if log_callback:
                    log_callback(f"[ERROR] Failed to start interactive sof-testbench4: {exc}")
                return False

            # Continuously drain proc.stdout in a daemon thread so the 64 KB OS pipe buffer
            # never fills up and blocks sof-testbench4 on trace write() calls during multi-PCM IPC syncs.
            def _drain_proc_stdout(p: subprocess.Popen) -> None:
                stream = p.stdout
                if stream is None:
                    return
                try:
                    for _line in stream:
                        pass
                except Exception:
                    pass

            threading.Thread(target=_drain_proc_stdout, args=(proc,), daemon=True).start()

            client = TestbenchRemoteClient("127.0.0.1", port, timeout=3.0)
            connected = False
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                if client.connect() and client.ping():
                    connected = True
                    break
                time.sleep(0.01)

            if not connected:
                try:
                    proc.kill()
                except Exception:
                    pass
                debug_log_command("SOF-TESTBENCH4-START", cmd_str, returncode=-1, stderr="Failed to connect to SOFR server")
                if log_callback:
                    log_callback(f"[ERROR] Interactive sof-testbench4 failed to bind port {port}")
                return False

            self.live_proc = proc
            self.live_client = client
            self.live_port = port
            client.exec_shell("pause")
            _st_p, plat_out = client.exec_shell(f"platform-cfg preset {eff_preset}")
            self._active_preset_applied = eff_preset
            client.get_info()
            client.set_perf_measurement_state(2)

            start_msg = (
                f"[SOFR :{port}] Started interactive sof-testbench4 (no topology loaded): $ {cmd_str}\n"
                f"[SOFR :{port}] {plat_out.strip()}"
            )
            debug_log_command("SOF-TESTBENCH4-START", cmd_str, returncode=0, stdout=plat_out.strip())
            if log_callback:
                log_callback(start_msg)
            return True

    def _destroy_active_pipelines_unlocked(
        self,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> List[str]:
        """Destroy all currently active pipelines on the interactive server via IPC4 messages
        (SET_PIPELINE_STATE(PAUSED) -> MOD_UNBIND -> SET_PIPELINE_STATE(RESET) -> DELETE_PIPELINE).
        Caller must hold `self._ipc_lock`.
        """
        logs: List[str] = []
        client = self.live_client
        plan = self.active_ipc_plan
        if client is None or plan is None:
            self.active_ipc_plan = None
            self.active_pipeline_ids = []
            return logs

        st_p, _ = client.exec_shell("pause")
        all_pipes = list(plan.playback_pipelines) + list(plan.capture_pipelines)
        if not all_pipes or st_p < 0:
            self.active_ipc_plan = None
            self.active_pipeline_ids = []
            return logs

        header = f"[IPC4] Destroying active pipelines {plan.selected_pipeline_ids} via IPC4…"
        logs.append(header)
        if log_callback:
            log_callback(header)

        transport_err = False
        # 1. Transition active pipelines to PAUSED
        for pop in all_pipes:
            pri, ext = encode_set_pipeline_state(pop.instance_id, SOF_IPC4_PIPELINE_STATE_PAUSED)
            desc = f"SET_PIPELINE_STATE(ppl={pop.instance_id} '{pop.name}', PAUSED)"
            st, rdict, _ = client.send_ipc4_tx(pri, ext, description=desc)
            ipc_st = rdict.get("ipc_status", st)
            line = f"  [IPC4-TX] {desc} -> status={ipc_st}"
            logs.append(line)
            if log_callback:
                log_callback(line)
            if st < 0:
                transport_err = True
                break

        # 2. Unbind cross-pipeline modules (SOF_IPC4_MOD_UNBIND)
        if not transport_err:
            for rop in plan.routes:
                if rop.is_inter_pipeline:
                    desc = (
                        f"MOD_UNBIND({rop.src_name} [0x{rop.src_module_id:x}:{rop.src_instance_id} q{rop.src_queue}] "
                        f"-> {rop.sink_name} [0x{rop.dst_module_id:x}:{rop.dst_instance_id} q{rop.dst_queue}])"
                    )
                    st, rdict, _ = client.send_ipc4_tx(
                        rop.unbind_primary, rop.unbind_extension, description=desc
                    )
                    ipc_st = rdict.get("ipc_status", st)
                    line = f"  [IPC4-TX] {desc} -> status={ipc_st}"
                    logs.append(line)
                    if log_callback:
                        log_callback(line)
                    if st < 0:
                        transport_err = True
                        break

        # 3. Transition active pipelines to RESET
        if not transport_err:
            for pop in all_pipes:
                pri, ext = encode_set_pipeline_state(pop.instance_id, SOF_IPC4_PIPELINE_STATE_RESET)
                desc = f"SET_PIPELINE_STATE(ppl={pop.instance_id} '{pop.name}', RESET)"
                st, rdict, _ = client.send_ipc4_tx(pri, ext, description=desc)
                ipc_st = rdict.get("ipc_status", st)
                line = f"  [IPC4-TX] {desc} -> status={ipc_st}"
                logs.append(line)
                if log_callback:
                    log_callback(line)
                if st < 0:
                    transport_err = True
                    break

        # 4. Delete pipelines (SOF_IPC4_GLB_DELETE_PIPELINE)
        if not transport_err:
            for pop in all_pipes:
                pri, ext = encode_delete_pipeline(pop.instance_id)
                desc = f"DELETE_PIPELINE(ppl={pop.instance_id} '{pop.name}')"
                st, rdict, _ = client.send_ipc4_tx(pri, ext, description=desc)
                ipc_st = rdict.get("ipc_status", st)
                line = f"  [IPC4-TX] {desc} -> status={ipc_st}"
                logs.append(line)
                if log_callback:
                    log_callback(line)
                if st < 0:
                    break

        self.active_ipc_plan = None
        self.active_pipeline_ids = []
        client.ipc_widget_map.clear()
        client.ipc_kcontrols.clear()
        client.ipc_route_edges.clear()
        return logs

    def destroy_active_pipelines_via_ipc(
        self,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> List[str]:
        """Thread-safe wrapper to destroy currently active pipelines via IPC4."""
        with self._ipc_lock:
            return self._destroy_active_pipelines_unlocked(log_callback=log_callback)

    def sync_selected_pipelines_via_ipc(
        self,
        tplg_path: str,
        pipeline_ids: List[int],
        rate: int = 48000,
        rate_out: Optional[int] = None,
        channels: int = 2,
        channels_out: Optional[int] = None,
        bits: int = 32,
        in_raw_paths: Optional[List[str]] = None,
        out_raw_paths: Optional[List[str]] = None,
        platform_preset: Optional[str] = None,
        force_recreate: bool = False,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[bool, List[str]]:
        """Destroy any previously active pipelines via IPC4 and create the newly selected
        pipelines (`pipeline_ids`) from `tplg_path` via IPC4 (`CREATE_PIPELINE`, `MOD_INIT_INSTANCE`,
        `LARGE_CONFIG_SET`, `MOD_BIND`).
        """
        if not tplg_path or not os.path.isfile(tplg_path):
            return (False, [f"[IPC4] Topology file not found: {tplg_path}"])

        eff_rate_out = rate_out if (rate_out and rate_out > 0) else rate
        eff_ch_out = channels_out if (channels_out and channels_out > 0) else channels
        norm_pipes = [int(p) for p in (pipeline_ids or [1, 2])]

        if not self.start_interactive_server(platform_preset=platform_preset, log_callback=log_callback):
            return (False, ["[IPC4] Failed to start interactive sof-testbench4 server"])

        with self._ipc_lock:
            client = self.live_client
            if client is None:
                return (False, ["[IPC4] Interactive testbench client not connected"])

            if (
                not force_recreate
                and self.active_ipc_plan is not None
                and self.active_tplg_path == tplg_path
                and self.active_pipeline_ids == norm_pipes
            ):
                return (True, [])

            logs: List[str] = []
            # 1. Destroy previously created pipelines via IPC4 if any are active
            if self.active_ipc_plan is not None:
                logs.extend(self._destroy_active_pipelines_unlocked(log_callback=log_callback))

            pipe_arg = ",".join(str(p) for p in norm_pipes)
            fmt_arg = f"S{bits}_LE"

            # 2. Parse compiled .tplg and build the IPC4 creation & binding plan for norm_pipes
            try:
                parsed_tplg = TplgIpc4Parser.parse_file(tplg_path)
                plan = build_ipc4_plan(
                    parsed_tplg,
                    norm_pipes,
                    fs_in=rate,
                    fs_out=eff_rate_out,
                    channels_in=channels,
                    channels_out=eff_ch_out,
                    frame_fmt_name=fmt_arg,
                )
            except Exception as exc:
                err_line = f"[IPC4] Failed to build IPC4 plan from {os.path.basename(tplg_path)}: {exc}"
                logs.append(err_line)
                if log_callback:
                    log_callback(err_line)
                return (False, logs)

            # 3. Ensure enough input/output raw file slots exist for all simultaneous PCMs in plan (up to 16)
            req_inputs = max(1, min(16, len(plan.input_widgets)))
            req_outputs = max(1, min(16, len(plan.output_widgets)))

            eff_in_paths = list(in_raw_paths) if in_raw_paths else []
            if not eff_in_paths:
                default_in = os.path.join(self.work_dir, "tb_input.raw")
                if not os.path.isfile(default_in):
                    self.generate_input_signal("sine_997", rate, channels, bits, duration_sec=0.15)
                eff_in_paths.append(default_in)
            while len(eff_in_paths) < req_inputs:
                idx = len(eff_in_paths) + 1
                ex_in = os.path.join(self.work_dir, f"tb_input_{idx}.raw")
                if not os.path.isfile(ex_in) and os.path.isfile(eff_in_paths[0]):
                    shutil.copyfile(eff_in_paths[0], ex_in)
                eff_in_paths.append(ex_in)

            eff_out_paths = list(out_raw_paths) if out_raw_paths else []
            if not eff_out_paths:
                eff_out_paths.append(os.path.join(self.work_dir, "tb_output.raw"))
            while len(eff_out_paths) < req_outputs:
                idx = len(eff_out_paths) + 1
                eff_out_paths.append(os.path.join(self.work_dir, f"tb_output_{idx}.raw"))

            in_arg = ",".join(eff_in_paths[:16])
            out_arg = ",".join(eff_out_paths[:16])

            client.exec_shell("pause")
            io_cmd = (
                f"io-cfg -i {in_arg} -o {out_arg} -p {pipe_arg} "
                f"-r {rate} -R {eff_rate_out} -c {channels} -n {eff_ch_out} -b {fmt_arg}"
            )
            client.exec_shell(io_cmd)

            create_hdr = (
                f"[IPC4] Creating selected pipelines {norm_pipes} from {os.path.basename(tplg_path)} "
                f"({len(plan.pipelines_by_id)} pipelines, {len(plan.widgets)} modules, {len(plan.routes)} bindings, "
                f"inputs={len(plan.input_widgets)}, outputs={len(plan.output_widgets)})…"
            )
            logs.append(create_hdr)
            if log_callback:
                log_callback(create_hdr)

            # 4. Execute each IPC4 step (CREATE_PIPELINE, MOD_INIT, LARGE_CFG, MOD_BIND)
            all_ok = True
            for step in plan.steps:
                st, rdict, _ = client.send_ipc4_tx(
                    step.primary,
                    step.extension,
                    step.payload,
                    description=step.description,
                )
                ipc_st = rdict.get("ipc_status", st)
                line = f"  [IPC4-TX] {step.description} -> status={ipc_st}"
                logs.append(line)
                if log_callback:
                    log_callback(line)
                if st != 0 or ipc_st != 0:
                    all_ok = False
                    break

            if not all_ok:
                return (False, logs)

            self.active_ipc_plan = plan
            self.active_tplg_path = tplg_path
            self.active_pipeline_ids = list(norm_pipes)

            # Populate client IPC metadata maps for telemetry & live kcontrols
            client.ipc_widget_map.clear()
            for wop in plan.widgets:
                cid = ((wop.instance_id & 0xFFFF) << 16) | (wop.module_id & 0xFFFF)
                client.ipc_widget_map[cid] = (wop.name, wop.pipeline_id)

            client.ipc_route_edges = [
                {
                    "edge_id": idx + 1,
                    "src_node_id": ((r.src_instance_id & 0xFFFF) << 16) | (r.src_module_id & 0xFFFF),
                    "dst_node_id": ((r.dst_instance_id & 0xFFFF) << 16) | (r.dst_module_id & 0xFFFF),
                    "src_name": r.src_name,
                    "dst_name": r.sink_name,
                    "control_name": "",
                    "kcontrol_numid": 0,
                    "active": True,
                }
                for idx, r in enumerate(plan.routes)
            ]

            client.ipc_kcontrols = []
            for idx, ctl in enumerate(plan.kcontrols, start=1):
                if ctl.ctl_type == SND_SOC_TPLG_TYPE_MIXER:
                    t_str = "BOOL" if ctl.max_val == 1 else "INT"
                elif ctl.ctl_type == SND_SOC_TPLG_TYPE_ENUM:
                    t_str = "ENUM"
                else:
                    t_str = "BYTES"
                if ctl.ctl_type == SND_SOC_TPLG_TYPE_BYTES:
                    default_vals: List[int] = []
                else:
                    default_vals = [ctl.default_step_value()] * max(1, ctl.num_channels)
                client.ipc_kcontrols.append(
                    KcontrolTelemetryItem(
                        numid=idx,
                        name=ctl.name,
                        iface_str="MIXER",
                        type_id=ctl.ctl_type,
                        type_str=t_str,
                        access_str="rw",
                        count=max(1, ctl.num_channels),
                        min_val=0,
                        max_val=ctl.max_val,
                        values=default_vals,
                        enum_names=list(ctl.enum_texts),
                        target_widget=ctl.widget_name,
                        pipeline_id=ctl.pipeline_id,
                    )
                )

            return (True, logs)

    def apply_kcontrol_via_ipc(
        self,
        kcontrol_name: str,
        values: List[int],
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[int, str]:
        """Send live kcontrol update (volume/switch/enum) to an IPC-created module via SOF_IPC4_MOD_LARGE_CONFIG_SET."""
        with self._ipc_lock:
            if not self.live_client or not self.active_ipc_plan:
                return (-1, "No active IPC pipelines in testbench")
            matched: Optional[TplgKcontrolDesc] = None
            for ctl in self.active_ipc_plan.kcontrols:
                if ctl.name == kcontrol_name or kcontrol_name in ctl.name or ctl.name in kcontrol_name:
                    matched = ctl
                    break
            if matched is None:
                return (-2, f"Kcontrol '{kcontrol_name}' not found in active pipelines {self.active_pipeline_ids}")
            chunks = encode_kcontrol_set_ipc(matched, values)
            if not chunks:
                return (-3, f"Unsupported kcontrol type for '{matched.name}'")
            last_st = 0
            for c_idx, (pri, ext, payload) in enumerate(chunks):
                desc = (
                    f"LARGE_CONFIG_SET(kctl='{matched.name}' on '{matched.widget_name}' "
                    f"[0x{matched.module_id:x}:{matched.instance_id}] = {values})"
                )
                st, rdict, _ = self.live_client.send_ipc4_tx(pri, ext, payload, description=desc)
                ipc_st = rdict.get("ipc_status", st)
                last_st = ipc_st if st == 0 else st
                msg = f"[IPC4-TX] {desc} -> status={last_st}"
                if log_callback:
                    log_callback(msg)
            return (last_st, f"Updated '{matched.name}' = {values} via IPC4 (status={last_st})")

    def apply_bytes_kcontrol_via_ipc(
        self,
        kcontrol_name: str,
        blob_or_abi: bytes,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[int, str]:
        """Send live BYTES kcontrol update to an IPC-created module via SOF_IPC4_MOD_LARGE_CONFIG_SET."""
        with self._ipc_lock:
            if not self.live_client or not self.active_ipc_plan:
                return (-1, "No active IPC pipelines in testbench")
            matched: Optional[TplgKcontrolDesc] = None
            for ctl in self.active_ipc_plan.kcontrols:
                if ctl.ctl_type == SND_SOC_TPLG_TYPE_BYTES and (
                    ctl.name == kcontrol_name or kcontrol_name in ctl.name or ctl.name in kcontrol_name
                ):
                    matched = ctl
                    break
            if matched is None:
                return (-2, f"Bytes kcontrol '{kcontrol_name}' not found in active pipelines")
            param_id = matched.abi_type
            payload_data = blob_or_abi
            # If blob starts with struct sof_abi_hdr magic ("SOF\0" = 0x00464F53), strip 32-byte ABI header
            if len(blob_or_abi) >= 32:
                magic, ptype, psize = struct.unpack_from("<3I", blob_or_abi, 0)
                if magic == 0x00464F53 and 32 + psize <= len(blob_or_abi):
                    param_id = ptype
                    payload_data = blob_or_abi[32 : 32 + psize]
            chunks = encode_large_config_set_chunks(
                matched.module_id, matched.instance_id, param_id, payload_data
            )
            last_st = 0
            for pri, ext, chunk in chunks:
                desc = (
                    f"LARGE_CONFIG_SET(bytes='{matched.name}' on '{matched.widget_name}' "
                    f"[0x{matched.module_id:x}:{matched.instance_id}], param_id={param_id}, {len(chunk)}B)"
                )
                st, rdict, _ = self.live_client.send_ipc4_tx(pri, ext, chunk, description=desc)
                ipc_st = rdict.get("ipc_status", st)
                last_st = ipc_st if st == 0 else st
                if log_callback:
                    log_callback(f"[IPC4-TX] {desc} -> status={last_st}")
            return (last_st, f"Updated bytes '{matched.name}' ({len(payload_data)}B) via IPC4 (status={last_st})")

    def set_pipeline_state_via_ipc(
        self,
        pipeline_id: int,
        state_str: str,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[int, str]:
        """Send SOF_IPC4_GLB_SET_PIPELINE_STATE for `pipeline_id` via IPC4."""
        st_map = {
            "reset": SOF_IPC4_PIPELINE_STATE_RESET,
            "pause": SOF_IPC4_PIPELINE_STATE_PAUSED,
            "paused": SOF_IPC4_PIPELINE_STATE_PAUSED,
            "run": SOF_IPC4_PIPELINE_STATE_RUNNING,
            "running": SOF_IPC4_PIPELINE_STATE_RUNNING,
        }
        state_val = st_map.get(state_str.strip().lower(), SOF_IPC4_PIPELINE_STATE_PAUSED)
        with self._ipc_lock:
            if not self.live_client:
                return (-1, "Interactive testbench not connected")
            pri, ext = encode_set_pipeline_state(pipeline_id, state_val)
            desc = f"SET_PIPELINE_STATE(ppl={pipeline_id}, {state_str.upper()})"
            st, rdict, _ = self.live_client.send_ipc4_tx(pri, ext, description=desc)
            ipc_st = rdict.get("ipc_status", st)
            msg = f"[IPC4-TX] {desc} -> status={ipc_st}"
            if log_callback:
                log_callback(msg)
            return (ipc_st if st == 0 else st, msg)

    def run_testbench(
        self,
        tplg_path: str,
        pipeline_ids: List[int],
        signal_kind: str = "sine_997",
        rate: int = 48000,
        channels: int = 2,
        bits: int = 32,
        rate_out: Optional[int] = None,
        channels_out: Optional[int] = None,
        duration_sec: float = 0.15,
        wav_source: str = "",
        extra_in_wavs: Optional[List[str]] = None,
        num_outputs: int = 1,
        platform_preset: Optional[str] = None,
        control_script: str = "",
        kcontrol_overrides: Optional[Dict[str, List[int]]] = None,
        bytes_overrides: Optional[Dict[str, str]] = None,
        keep_alive: bool = False,
        hot_reload_if_alive: bool = False,
        on_live_waveform: Optional[Callable[[List[float], List[float], int], None]] = None,
    ) -> TestbenchRunResult:
        """Drive the persistent interactive sof-testbench4 server (started WITHOUT -t):
        1. Generate input signal .raw/.wav files.
        2. Destroy any previously active pipelines via IPC4 and create the selected pipelines
           (`pipeline_ids`) via IPC4 (`CREATE_PIPELINE`, `MOD_INIT_INSTANCE`, `LARGE_CONFIG_SET`, `MOD_BIND`).
        3. Apply any kcontrol/bytes overrides via IPC4 `LARGE_CONFIG_SET`.
        4. Transition selected pipelines to `PAUSED` then `RUNNING` via `SET_PIPELINE_STATE` IPC4.
        5. Execute `run-ipc` to step the DSP scheduler until input EOF and collect telemetry & output audio.
        6. Transition selected pipelines back to `PAUSED` via `SET_PIPELINE_STATE` IPC4 (keeping them resident).
        """
        eff_rate_out = rate_out if (rate_out and rate_out > 0) else rate
        eff_ch_out = channels_out if (channels_out and channels_out > 0) else channels
        eff_preset = self.map_platform_to_tb_preset(platform_preset or self.platform_preset)
        norm_pipes = [int(p) for p in (pipeline_ids or [1, 2])]

        in_raw, in_wav, in_norm = self.generate_input_signal(
            signal_kind=signal_kind,
            rate=rate,
            channels=channels,
            bits=bits,
            duration_sec=duration_sec,
            wav_source=wav_source,
        )

        # Support multiple comma-separated input raw files (-i in1,in2,...) up to 16
        target_in_frames = max(256, int(rate * duration_sec))
        in_raw_list = [in_raw]
        multi_in_samples: List[List[float]] = [list(in_norm)]
        if extra_in_wavs:
            for idx, ex_wav in enumerate(extra_in_wavs[:15], start=2):
                ex_raw = os.path.join(self.work_dir, f"tb_input_{idx}.raw")
                ex_norm: List[float] = []
                if ex_wav and ex_wav.startswith("sig_gen:"):
                    ex_sig = ex_wav.split(":", 1)[1].strip() or signal_kind
                    ex_raw, _ex_w, ex_norm = self.generate_input_signal(
                        signal_kind=ex_sig,
                        rate=rate,
                        channels=channels,
                        bits=bits,
                        duration_sec=duration_sec,
                        stem=f"tb_input_{idx}",
                    )
                elif ex_wav and os.path.isfile(ex_wav) and shutil.which("sox"):
                    subprocess.run(
                        [
                            "sox",
                            "--encoding",
                            "signed-integer",
                            ex_wav,
                            "-L",
                            "-r",
                            str(rate),
                            "-c",
                            str(channels),
                            "-b",
                            "32" if bits == 24 else str(bits),
                            ex_raw,
                            "trim",
                            "0",
                            f"{duration_sec:.2f}",
                        ],
                        check=False,
                        capture_output=True,
                    )
                    if os.path.isfile(ex_raw) and os.path.getsize(ex_raw) > 0:
                        ex_norm = self._read_raw_head_normalized(ex_raw, channels, bits, target_in_frames)
                else:
                    shutil.copyfile(in_raw, ex_raw)
                    ex_norm = list(in_norm)
                in_raw_list.append(ex_raw)
                multi_in_samples.append(ex_norm if ex_norm else list(in_norm))

        # Support multiple comma-separated output raw files (-o out1,out2,...) up to 16
        out_raw = os.path.join(self.work_dir, "tb_output.raw")
        out_wav = os.path.join(self.work_dir, "tb_output.wav")
        out_raw_list = [out_raw]
        for idx in range(2, min(16, max(1, num_outputs)) + 1):
            out_raw_list.append(os.path.join(self.work_dir, f"tb_output_{idx}.raw"))
        for idx in range(1, 17):
            cand_out = os.path.join(self.work_dir, "tb_output.raw" if idx == 1 else f"tb_output_{idx}.raw")
            if os.path.exists(cand_out):
                os.remove(cand_out)

        self.last_in_samples = list(in_norm)
        self.last_multi_in_samples = list(multi_in_samples)
        self.last_out_raw_path = out_raw
        self.last_rate = rate
        self.last_rate_out = eff_rate_out
        self.last_channels = channels
        self.last_channels_out = eff_ch_out
        self.last_bits = bits
        self.last_signal_kind = signal_kind
        self.rt_phase_idx = 0

        pipe_arg = ",".join(str(p) for p in norm_pipes)
        fmt_arg = f"S{bits}_LE"
        target_frames = max(256, int(eff_rate_out * duration_sec))

        ipc_logs: List[str] = []
        sync_ok, sync_lines = self.sync_selected_pipelines_via_ipc(
            tplg_path=tplg_path,
            pipeline_ids=norm_pipes,
            rate=rate,
            rate_out=eff_rate_out,
            channels=channels,
            channels_out=eff_ch_out,
            bits=bits,
            in_raw_paths=in_raw_list,
            out_raw_paths=out_raw_list,
            platform_preset=eff_preset,
            force_recreate=True,
        )
        ipc_logs.extend(sync_lines)
        port = self.live_port
        cmd_str = (
            f"[SOFR Interactive :{port} IPC4] platform={eff_preset} "
            f"pipelines={pipe_arg} rate={rate}->{eff_rate_out} ch={channels}->{eff_ch_out} fmt={fmt_arg}"
        )

        if not sync_ok or self.live_client is None or self.active_ipc_plan is None:
            err_txt = "\n".join(ipc_logs) or "Failed to create pipelines via IPC4"
            debug_log_command("SOF-TESTBENCH4", cmd_str, returncode=1, stderr=err_txt)
            return TestbenchRunResult(
                success=False,
                command=cmd_str,
                in_wav_path=in_wav,
                out_wav_path="",
                in_samples=in_norm,
                out_samples=[],
                stdout="\n".join(ipc_logs),
                stderr=err_txt,
                summary_text=f"Status: FAIL (IPC4 Pipeline Creation Failed)  |  Pipelines: -p {pipe_arg}",
                telemetry=None,
            )

        try:
            client = self.live_client
            plan = self.active_ipc_plan

            # Apply any kcontrol overrides via IPC4 LARGE_CONFIG_SET
            if kcontrol_overrides:
                for kname, kvals in kcontrol_overrides.items():
                    _st_k, k_msg = self.apply_kcontrol_via_ipc(kname, kvals)
                    ipc_logs.append(f"  {k_msg}")

            # Apply any BYTES kcontrol file overrides via IPC4 LARGE_CONFIG_SET
            if bytes_overrides:
                for bname, bpath in bytes_overrides.items():
                    if not bpath or not os.path.isfile(bpath):
                        continue
                    try:
                        with open(bpath, "rb") as bfh:
                            raw_blob = bfh.read(4096)
                        _st_b, b_msg = self.apply_bytes_kcontrol_via_ipc(bname, raw_blob)
                        ipc_logs.append(f"  {b_msg}")
                    except Exception:
                        pass

            # Transition selected pipelines to PAUSED then RUNNING via IPC4 (matching tb_set_running_state)
            with self._ipc_lock:
                for pop in plan.playback_pipelines:
                    pri, ext = encode_set_pipeline_state(pop.instance_id, SOF_IPC4_PIPELINE_STATE_PAUSED)
                    desc = f"SET_PIPELINE_STATE(ppl={pop.instance_id} '{pop.name}', PAUSED)"
                    st, rdict, _ = client.send_ipc4_tx(pri, ext, description=desc)
                    ipc_logs.append(f"  [IPC4-TX] {desc} -> status={rdict.get('ipc_status', st)}")
                    if st < 0:
                        break
                for pop in plan.playback_pipelines:
                    pri, ext = encode_set_pipeline_state(pop.instance_id, SOF_IPC4_PIPELINE_STATE_RUNNING)
                    desc = f"SET_PIPELINE_STATE(ppl={pop.instance_id} '{pop.name}', RUNNING)"
                    st, rdict, _ = client.send_ipc4_tx(pri, ext, description=desc)
                    ipc_logs.append(f"  [IPC4-TX] {desc} -> status={rdict.get('ipc_status', st)}")
                    if st < 0:
                        break

                # Capture pipelines transition in reverse order (sink to source) matching utils_ipc4.c
                for pop in reversed(plan.capture_pipelines):
                    pri, ext = encode_set_pipeline_state(pop.instance_id, SOF_IPC4_PIPELINE_STATE_PAUSED)
                    desc = f"SET_PIPELINE_STATE(ppl={pop.instance_id} '{pop.name}', PAUSED)"
                    st, rdict, _ = client.send_ipc4_tx(pri, ext, description=desc)
                    ipc_logs.append(f"  [IPC4-TX] {desc} -> status={rdict.get('ipc_status', st)}")
                    if st < 0:
                        break
                for pop in reversed(plan.capture_pipelines):
                    pri, ext = encode_set_pipeline_state(pop.instance_id, SOF_IPC4_PIPELINE_STATE_RUNNING)
                    desc = f"SET_PIPELINE_STATE(ppl={pop.instance_id} '{pop.name}', RUNNING)"
                    st, rdict, _ = client.send_ipc4_tx(pri, ext, description=desc)
                    ipc_logs.append(f"  [IPC4-TX] {desc} -> status={rdict.get('ipc_status', st)}")
                    if st < 0:
                        break

                # Step DSP scheduler on IPC-created pipelines until input EOF
                _st_run, run_out = client.exec_shell("run-ipc", timeout_ms=8000)

                # Collect full telemetry while pipelines are active
                telemetry = client.collect_full_telemetry()

                # Transition pipelines back to PAUSED so they remain resident until selection changes
                for pop in list(plan.playback_pipelines) + list(reversed(plan.capture_pipelines)):
                    pri, ext = encode_set_pipeline_state(pop.instance_id, SOF_IPC4_PIPELINE_STATE_PAUSED)
                    desc = f"SET_PIPELINE_STATE(ppl={pop.instance_id} '{pop.name}', PAUSED)"
                    st, rdict, _ = client.send_ipc4_tx(pri, ext, description=desc)
                    ipc_logs.append(f"  [IPC4-TX] {desc} -> status={rdict.get('ipc_status', st)}")
                    if st < 0:
                        break

            num_in_slots = max(1, min(16, len(plan.input_widgets)))
            while len(multi_in_samples) < num_in_slots:
                idx = len(multi_in_samples) + 1
                cand_in = os.path.join(self.work_dir, f"tb_input_{idx}.raw")
                if os.path.isfile(cand_in) and os.path.getsize(cand_in) > 0:
                    multi_in_samples.append(self._read_raw_head_normalized(cand_in, channels, bits, target_in_frames))
                else:
                    multi_in_samples.append(list(in_norm))
            self.last_multi_in_samples = list(multi_in_samples)

            num_out_slots = max(1, min(16, max(num_outputs, len(plan.output_widgets))))
            multi_out_samples: List[List[float]] = []
            multi_out_wav_paths: List[str] = []
            for idx in range(1, num_out_slots + 1):
                cand_out = os.path.join(self.work_dir, "tb_output.raw" if idx == 1 else f"tb_output_{idx}.raw")
                cand_wav = os.path.join(self.work_dir, "tb_output.wav" if idx == 1 else f"tb_output_{idx}.wav")
                slot_norm: List[float] = []
                if os.path.isfile(cand_out) and os.path.getsize(cand_out) > 0:
                    slot_norm = self._read_raw_head_normalized(cand_out, eff_ch_out, bits, target_frames)
                    if slot_norm:
                        self._write_wav_from_normalized(cand_wav, slot_norm, eff_rate_out)
                        multi_out_wav_paths.append(cand_wav)
                multi_out_samples.append(slot_norm)

            out_norm = multi_out_samples[0] if multi_out_samples else []
            if not out_norm:
                for cand_s in multi_out_samples[1:]:
                    if cand_s:
                        out_norm = cand_s
                        break

            if on_live_waveform is not None and out_norm:
                on_live_waveform(in_norm[: len(out_norm)], out_norm, len(out_norm))

            ok = bool(any(len(s) > 0 for s in multi_out_samples) or (telemetry and telemetry.total_ll_ticks > 0))

            in_peak, in_rms = self._compute_metrics(in_norm)
            out_peak, out_rms = self._compute_metrics(out_norm)

            telem_badge = ""
            if telemetry:
                active_kcps = sum(c.avg_kcps for c in telemetry.cores if c.enabled)
                telem_badge = (
                    f"  |  SOFR :{port} [{eff_preset.upper()}] Ticks={telemetry.total_ll_ticks} "
                    f"KCPS={active_kcps} Heap={telemetry.total_heap_bytes}B "
                    f"DAPM={telemetry.dapm_powered_nodes}/{telemetry.dapm_total_nodes}"
                )

            if len(multi_out_samples) > 1:
                out_parts = []
                for idx, s_out in enumerate(multi_out_samples, start=1):
                    pk_i, rms_i = self._compute_metrics(s_out)
                    out_parts.append(f"Out{idx}: {len(s_out)}f ({pk_i:.1f}/{rms_i:.1f} dBFS)")
                out_summary_str = " | ".join(out_parts)
            else:
                out_summary_str = f"Out: {len(out_norm)}f (Peak {out_peak:.1f}, RMS {out_rms:.1f} dBFS)"

            summary = (
                f"Status: {'PASS' if ok else 'FAIL'} (IPC4)  |  Pipelines: -p {pipe_arg}  |  "
                f"In: {len(in_norm)}f (Peak {in_peak:.1f}, RMS {in_rms:.1f} dBFS) → "
                f"{out_summary_str}"
                f"{telem_badge}"
            )
            stdout_str = "\n".join(ipc_logs) + ("\n" + run_out.strip() if run_out.strip() else "")
            debug_log_command(
                "SOF-TESTBENCH4",
                cmd_str,
                returncode=0 if ok else 1,
                stdout=stdout_str,
                stderr="",
                extra=(
                    f"{summary} | streams={len(multi_in_samples)}->{len(multi_out_samples)} "
                    f"in[:6]={[round(x, 4) for x in in_norm[:6]]} "
                    f"out[:6]={[round(x, 4) for x in out_norm[:6]]}"
                ),
            )
            return TestbenchRunResult(
                success=ok,
                command=cmd_str,
                in_wav_path=in_wav,
                out_wav_path=out_wav if (ok and out_norm) else "",
                in_samples=in_norm,
                out_samples=out_norm,
                multi_in_samples=multi_in_samples,
                multi_out_samples=multi_out_samples,
                multi_out_wav_paths=multi_out_wav_paths,
                in_peak_db=in_peak,
                out_peak_db=out_peak,
                in_rms_db=in_rms,
                out_rms_db=out_rms,
                stdout=stdout_str,
                stderr="",
                summary_text=summary,
                telemetry=telemetry,
            )
        except Exception as exc:
            debug_log_command(
                "SOF-TESTBENCH4",
                cmd_str,
                returncode=-1,
                stdout="\n".join(ipc_logs),
                stderr=str(exc),
                extra="EXCEPTION",
            )
            return TestbenchRunResult(
                success=False,
                command=cmd_str,
                in_wav_path=in_wav,
                out_wav_path="",
                in_samples=in_norm,
                out_samples=[],
                stdout="\n".join(ipc_logs),
                stderr=str(exc),
                summary_text=f"Error executing testbench via IPC4 protocol: {exc}",
                telemetry=None,
            )

    def poll_live_waveforms(
        self,
        window_frames: int = 3600,
    ) -> Optional[Tuple[List[float], List[float], int, bool, bool]]:
        """Poll real-time Input and Output (or active Probe Tap) waveform slices from the active live testbench session.
        Returns (in_samples, out_samples, frames_played, dsp_active, tone_running) or None if disconnected.
        """
        if not self.live_client or not self.live_proc or self.live_proc.poll() is not None:
            return None

        st, pcm_info, pcm_bytes = self.live_client.pcm_control(
            SOF_PCM_OP_CAPTURE_BUFFER,
            duration_ms=max(40, int((window_frames / max(1, self.last_rate_out)) * 1000)),
        )
        if st != 0:
            return None

        frames_played = int(pcm_info.get("frames_played", 0))
        tone_running = bool(pcm_info.get("tone_running", False))
        tone_freq = int(pcm_info.get("freq_hz", 1000)) or 1000

        # Check DSP power state (D0 active vs D3 paused) via lightweight GET_INFO
        info = self.live_client.get_info()
        dsp_active = bool(info.get("dsp_power_state", 1) == 1) if info else True

        # Advance rolling phase when DSP is active
        if dsp_active:
            self.rt_phase_idx = (self.rt_phase_idx + 96) % max(1, len(self.last_in_samples) or window_frames)

        # Build live Input window
        in_slice: List[float] = []
        if tone_running:
            for i in range(window_frames):
                t = (self.rt_phase_idx + i) / float(max(1, self.last_rate))
                in_slice.append(0.50 * math.sin(2.0 * math.pi * float(tone_freq) * t))
        elif self.last_in_samples:
            n_in = len(self.last_in_samples)
            start = self.rt_phase_idx % n_in
            if start + window_frames <= n_in:
                in_slice = self.last_in_samples[start : start + window_frames]
            else:
                rem = (start + window_frames) - n_in
                in_slice = self.last_in_samples[start:] + self.last_in_samples[: min(n_in, rem)]

        # If an internal module Probe Tap is active, read its live exit samples via SOF_PROBE_OP_READ_BUFFER
        out_slice: List[float] = []
        if self.active_probe_comp_id > 0:
            pst, _rid, probe_s = self.live_client.probe_read_buffer(
                comp_id=self.active_probe_comp_id,
                max_bytes=8192,
            )
            if pst == 0 and probe_s:
                if len(probe_s) >= window_frames:
                    out_slice = probe_s[:window_frames]
                else:
                    reps = (window_frames // max(1, len(probe_s))) + 1
                    out_slice = (probe_s * reps)[:window_frames]

        # Build live Output window from latest tail of tb_output.raw or SOFR PCM ring buffer
        if not out_slice and pcm_bytes and tone_running:
            out_slice = self._decode_s16_bytes_normalized(pcm_bytes, self.last_channels_out)
        if not out_slice and self.last_out_raw_path and os.path.isfile(self.last_out_raw_path):
            raw_tail = self._read_raw_tail_normalized(
                self.last_out_raw_path,
                self.last_channels_out,
                self.last_bits,
                max(window_frames * 2, 7200),
            )
            if raw_tail:
                n_out = len(raw_tail)
                if n_out <= window_frames:
                    out_slice = raw_tail
                else:
                    start = self.rt_phase_idx % n_out
                    if start + window_frames <= n_out:
                        out_slice = raw_tail[start : start + window_frames]
                    else:
                        rem = (start + window_frames) - n_out
                        out_slice = raw_tail[start:] + raw_tail[: min(n_out, rem)]
        if not out_slice and pcm_bytes:
            out_slice = self._decode_s16_bytes_normalized(pcm_bytes, self.last_channels_out)

        return (in_slice, out_slice, frames_played, dsp_active, tone_running)

    @staticmethod
    def _decode_s16_bytes_normalized(pcm_bytes: bytes, channels: int) -> List[float]:
        ch = max(1, channels)
        frame_sz = 2 * ch
        n = len(pcm_bytes) // frame_sz
        out: List[float] = []
        for i in range(n):
            vals = struct.unpack_from("<" + "h" * ch, pcm_bytes, i * frame_sz)
            out.append(vals[0] / 32768.0)
        return out

    @staticmethod
    def _write_raw_from_normalized(raw_path: str, samples: List[float], channels: int, bits: int) -> None:
        with open(raw_path, "wb") as f:
            if bits == 16:
                scale = 32767.0
                for s in samples:
                    v = max(-32768, min(32767, int(s * scale)))
                    f.write(struct.pack("<" + "h" * channels, *([v] * channels)))
            elif bits == 24:
                scale = 8388607.0
                for s in samples:
                    v = max(-8388608, min(8388607, int(s * scale)))
                    f.write(struct.pack("<" + "i" * channels, *([v] * channels)))
            else:
                scale = 2147483647.0
                for s in samples:
                    v = max(-2147483648, min(2147483647, int(s * scale)))
                    f.write(struct.pack("<" + "i" * channels, *([v] * channels)))

    @staticmethod
    def _read_raw_head_normalized(
        raw_path: str,
        channels: int,
        bits: int,
        max_frames: int = 7200,
    ) -> List[float]:
        """Read up to the first `max_frames` frames from byte offset 0 of a raw PCM file normalized to [-1.0, 1.0]."""
        if not os.path.isfile(raw_path):
            return []
        ch = max(1, channels)
        frame_sz = (2 if bits == 16 else 4) * ch
        try:
            file_sz = os.path.getsize(raw_path)
            total_frames = file_sz // frame_sz
            if total_frames <= 0:
                return []
            frames_to_read = min(total_frames, max_frames)
            with open(raw_path, "rb") as f:
                data = f.read(frames_to_read * frame_sz)
        except Exception:
            return []

        n = len(data) // frame_sz
        out: List[float] = []
        if bits == 16:
            for i in range(n):
                vals = struct.unpack_from("<" + "h" * ch, data, i * frame_sz)
                out.append(vals[0] / 32768.0)
        elif bits == 24:
            for i in range(n):
                vals = struct.unpack_from("<" + "i" * ch, data, i * frame_sz)
                out.append(max(-1.0, min(1.0, vals[0] / 8388608.0)))
        else:
            for i in range(n):
                vals = struct.unpack_from("<" + "i" * ch, data, i * frame_sz)
                out.append(vals[0] / 2147483648.0)
        return out

    @staticmethod
    def _read_raw_tail_normalized(
        raw_path: str,
        channels: int,
        bits: int,
        max_frames: int = 7200,
    ) -> List[float]:
        """Read up to the last `max_frames` frames from a raw PCM file normalized to [-1.0, 1.0]."""
        if not os.path.isfile(raw_path):
            return []
        ch = max(1, channels)
        frame_sz = (2 if bits == 16 else 4) * ch
        try:
            file_sz = os.path.getsize(raw_path)
            total_frames = file_sz // frame_sz
            if total_frames <= 0:
                return []
            frames_to_read = min(total_frames, max_frames)
            byte_offset = (total_frames - frames_to_read) * frame_sz
            with open(raw_path, "rb") as f:
                f.seek(byte_offset)
                data = f.read(frames_to_read * frame_sz)
        except Exception:
            return []

        n = len(data) // frame_sz
        out: List[float] = []
        if bits == 16:
            for i in range(n):
                vals = struct.unpack_from("<" + "h" * ch, data, i * frame_sz)
                out.append(vals[0] / 32768.0)
        elif bits == 24:
            for i in range(n):
                vals = struct.unpack_from("<" + "i" * ch, data, i * frame_sz)
                out.append(max(-1.0, min(1.0, vals[0] / 8388608.0)))
        else:
            for i in range(n):
                vals = struct.unpack_from("<" + "i" * ch, data, i * frame_sz)
                out.append(vals[0] / 2147483648.0)
        return out

    @staticmethod
    def _read_raw_normalized(raw_path: str, channels: int, bits: int) -> List[float]:
        return TestbenchRunner._read_raw_tail_normalized(raw_path, channels, bits, max_frames=200000)

    @staticmethod
    def _write_wav_from_normalized(wav_path: str, samples: List[float], rate: int) -> None:
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            buf = bytearray()
            for s in samples:
                v = max(-32768, min(32767, int(s * 32767.0)))
                buf.extend(struct.pack("<h", v))
            wf.writeframes(bytes(buf))

    @staticmethod
    def _compute_metrics(samples: List[float]) -> Tuple[float, float]:
        if not samples:
            return (-120.0, -120.0)
        peak = max(abs(x) for x in samples)
        rms = math.sqrt(sum(x * x for x in samples) / len(samples))
        peak_db = 20.0 * math.log10(peak) if peak > 1e-9 else -120.0
        rms_db = 20.0 * math.log10(rms) if rms > 1e-9 else -120.0
        return (peak_db, rms_db)


class WaveformSnapshotWidget(Gtk.Widget):
    """Hardware-accelerated Gtk.Snapshot dual-channel oscilloscope & waveform viewer.
    Supports:
    - Separate, clearly labelled INPUT PCM (top) and OUTPUT PCM (bottom) waveform lanes
    - Real-time streaming mode with optional rising zero-crossing trigger lock or free-scroll
    - Horizontal time-axis Zoom In / Zoom Out (mouse wheel or toolbar buttons) and drag-to-pan
    - Vertical amplitude magnification (Ctrl + mouse wheel or Amp +/- buttons)
    - Y-axis amplitude scale markers, X-axis time (ms) & sample (#) ruler, viewport scrollbar,
      and interactive hover crosshair cursor with exact sample/dBFS readout.
    """

    __gtype_name__ = "WaveformSnapshotWidget"

    BASE_VISIBLE_SAMPLES = 600

    def __init__(self, on_zoom_changed: Optional[Callable[[], None]] = None):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_size_request(-1, 175)
        self.set_focusable(True)

        self.on_zoom_changed = on_zoom_changed

        # Waveform sample buffers & stream metadata
        self.in_samples: List[float] = []
        self.out_samples: List[float] = []
        self.multi_in_samples: List[List[float]] = []
        self.multi_out_samples: List[List[float]] = []
        self.sample_rate: int = 48000
        self.channels: int = 2
        self.bits: int = 32
        self.in_label: str = "INPUT PCM: Source (Host/DAI)"
        self.out_label: str = "OUTPUT PCM: Sink (DAI/Host)"
        self.signal_label: str = "Sine 997 Hz"
        self.multi_in_labels: List[str] = []
        self.multi_out_labels: List[str] = []
        self.multi_signal_labels: List[str] = []
        self.in_peak_db: float = -120.0
        self.in_rms_db: float = -120.0
        self.out_peak_db: float = -120.0
        self.out_rms_db: float = -120.0
        self.summary_label: str = "Run Testbench or enable Real-Time to inspect Input vs. Output audio waveforms"

        # Real-time oscilloscope state
        self.is_realtime: bool = False
        self.dsp_active: bool = False
        self.trigger_lock: bool = True
        self.live_frames_played: int = 0

        # Zoom & Pan state
        self.time_zoom: float = 1.0  # 1.0x = 600 samples (~12.5 ms @ 48 kHz)
        self.amp_zoom: float = 1.0   # 1.0x = [-1.0, +1.0] full scale
        self.pan_offset: int = 0     # Starting sample index
        self._pan_offset_f: float = 0.0  # Sub-sample floating-point pan offset for drift-free cursor zoom

        # Vertical scroll state for multi-PCM stacked waveforms (bound to external Gtk.Scrollbar slider)
        self.v_scroll_offset: float = 0.0
        self._max_v_scroll: float = 0.0
        self._updating_v_adj: bool = False
        self._v_adj_idle_scheduled: bool = False
        self._pending_v_adj_cfg: Optional[Tuple[float, float, float]] = None
        self.v_adj: Gtk.Adjustment = Gtk.Adjustment.new(0.0, 0.0, 185.0, 36.0, 120.0, 185.0)
        self.v_adj.connect("value-changed", self._on_v_adj_value_changed)

        # Mouse interaction state
        self._hover_x: Optional[float] = None
        self._hover_y: Optional[float] = None
        self._drag_start_pan: int = 0
        self._drag_start_v_scroll: float = 0.0

        self._setup_controllers()

    def _setup_controllers(self) -> None:
        # Scroll wheel: Time zoom (default), Amp zoom (Ctrl), or Horizontal pan (Shift)
        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL | Gtk.EventControllerScrollFlags.HORIZONTAL
        )
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        # Drag (left or right mouse button) to pan horizontally along time axis
        drag = Gtk.GestureDrag.new()
        drag.set_button(0)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        self.add_controller(drag)

        # Double-click to reset zoom/pan
        click = Gtk.GestureClick.new()
        click.set_button(1)
        click.connect("pressed", self._on_pressed)
        self.add_controller(click)

        # Pointer motion for interactive sample crosshair readout
        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

    # -------------------------------------------------------------------------
    # Public API for Labels, Waveforms, Real-Time & Zoom/Pan
    # -------------------------------------------------------------------------

    def set_stream_labels(
        self,
        in_label: str,
        out_label: str,
        signal_label: str = "",
        rate: int = 48000,
        channels: int = 2,
        bits: int = 32,
        multi_in_labels: Optional[List[str]] = None,
        multi_out_labels: Optional[List[str]] = None,
        multi_signal_labels: Optional[List[str]] = None,
    ) -> None:
        self.in_label = in_label
        self.out_label = out_label
        if signal_label:
            self.signal_label = signal_label
        self.multi_in_labels = list(multi_in_labels) if multi_in_labels else [in_label]
        self.multi_out_labels = list(multi_out_labels) if multi_out_labels else [out_label]
        self.multi_signal_labels = (
            list(multi_signal_labels) if multi_signal_labels else [self.signal_label]
        )
        expected_in = max(1, len(self.multi_in_labels))
        expected_out = max(1, len(self.multi_out_labels))
        if len(self.multi_in_samples) > expected_in:
            self.multi_in_samples = self.multi_in_samples[:expected_in]
        if len(self.multi_out_samples) > expected_out:
            self.multi_out_samples = self.multi_out_samples[:expected_out]
        self.sample_rate = max(1000, rate)
        self.channels = max(1, channels)
        self.bits = bits
        self.queue_draw()

    def set_waveforms(
        self,
        in_samples: List[float],
        out_samples: List[float],
        summary: str = "",
        is_realtime: bool = False,
        frames_played: int = 0,
        dsp_active: bool = True,
        multi_in_samples: Optional[List[List[float]]] = None,
        multi_out_samples: Optional[List[List[float]]] = None,
    ) -> None:
        self.in_samples = in_samples
        self.out_samples = out_samples
        if multi_in_samples is not None:
            self.multi_in_samples = [list(s) for s in multi_in_samples]
        elif not self.multi_in_samples or len(self.multi_in_samples) <= 1:
            self.multi_in_samples = [list(in_samples)] if in_samples else []
        else:
            self.multi_in_samples[0] = list(in_samples)

        if multi_out_samples is not None:
            self.multi_out_samples = [list(s) for s in multi_out_samples]
        elif not self.multi_out_samples or len(self.multi_out_samples) <= 1:
            self.multi_out_samples = [list(out_samples)] if out_samples else []
        else:
            self.multi_out_samples[0] = list(out_samples)

        self.in_peak_db, self.in_rms_db = TestbenchRunner._compute_metrics(in_samples)
        self.out_peak_db, self.out_rms_db = TestbenchRunner._compute_metrics(out_samples)
        if summary:
            self.summary_label = summary
        self.is_realtime = is_realtime
        self.dsp_active = dsp_active
        if frames_played > 0:
            self.live_frames_played = frames_played
        self._clamp_pan()
        if self.on_zoom_changed:
            self.on_zoom_changed()
        self.queue_draw()

    def total_samples(self) -> int:
        best = max(len(self.in_samples), len(self.out_samples), 0)
        for s in self.multi_in_samples:
            if len(s) > best:
                best = len(s)
        for s in self.multi_out_samples:
            if len(s) > best:
                best = len(s)
        return best

    def visible_sample_count(self) -> int:
        total = max(self.total_samples(), self.BASE_VISIBLE_SAMPLES)
        raw_vis = int(round(self.BASE_VISIBLE_SAMPLES / max(0.05, self.time_zoom)))
        return max(16, min(total, raw_vis))

    def get_zoom_summary_text(self) -> str:
        vis = self.visible_sample_count()
        dur_ms = (vis / float(max(1, self.sample_rate))) * 1000.0
        return f"Time: {self.time_zoom:.2g}× ({dur_ms:.2f} ms / {vis} smp)  |  Amp: {self.amp_zoom:.2g}×"

    def _get_pointer_anchor_ratio(self) -> float:
        """Return normalized [0.0, 1.0] horizontal mouse pointer position within the waveform plot."""
        w = max(1.0, float(self.get_width()) or 920.0)
        left_gutter = 62.0
        right_pad = 8.0
        plot_w = max(20.0, w - left_gutter - right_pad)
        if self._hover_x is not None and self._hover_x >= 0.0:
            return max(0.0, min(1.0, (self._hover_x - left_gutter) / plot_w))
        return 0.5

    def zoom_in_time(self, factor: float = 1.5, anchor_ratio: Optional[float] = None) -> None:
        self._set_time_zoom(self.time_zoom * factor, anchor_ratio)

    def zoom_out_time(self, factor: float = 1.5, anchor_ratio: Optional[float] = None) -> None:
        self._set_time_zoom(self.time_zoom / factor, anchor_ratio)

    def zoom_in_amp(self, factor: float = 1.4142) -> None:
        self.amp_zoom = min(16.0, self.amp_zoom * factor)
        if self.on_zoom_changed:
            self.on_zoom_changed()
        self.queue_draw()

    def zoom_out_amp(self, factor: float = 1.4142) -> None:
        self.amp_zoom = max(0.25, self.amp_zoom / factor)
        if self.on_zoom_changed:
            self.on_zoom_changed()
        self.queue_draw()

    def reset_zoom(self) -> None:
        self.time_zoom = 1.0
        self.amp_zoom = 1.0
        self.pan_offset = 0
        self._pan_offset_f = 0.0
        if self.on_zoom_changed:
            self.on_zoom_changed()
        self.queue_draw()

    def _set_time_zoom(self, new_zoom: float, anchor_ratio: Optional[float] = None) -> None:
        if anchor_ratio is None:
            anchor_ratio = self._get_pointer_anchor_ratio()
        r = max(0.0, min(1.0, float(anchor_ratio)))
        total = max(self.total_samples(), self.BASE_VISIBLE_SAMPLES)
        min_zoom = max(0.05, float(self.BASE_VISIBLE_SAMPLES) / float(max(1, total)))
        max_zoom = 37.5  # down to 16 samples
        old_vis = self.visible_sample_count()
        anchor_sample_f = self._pan_offset_f + r * float(max(1, old_vis - 1))

        self.time_zoom = max(min_zoom, min(max_zoom, new_zoom))
        new_vis = self.visible_sample_count()
        self._pan_offset_f = anchor_sample_f - r * float(max(1, new_vis - 1))
        self._clamp_pan()
        if self.on_zoom_changed:
            self.on_zoom_changed()
        self.queue_draw()

    def _clamp_pan(self) -> None:
        total = max(self.total_samples(), self.BASE_VISIBLE_SAMPLES)
        vis = self.visible_sample_count()
        max_pan = max(0, total - vis)
        self._pan_offset_f = max(0.0, min(float(max_pan), self._pan_offset_f))
        self.pan_offset = int(round(self._pan_offset_f))

    def _on_v_adj_value_changed(self, adj: Gtk.Adjustment) -> None:
        if self._updating_v_adj:
            return
        self.v_scroll_offset = max(0.0, min(self._max_v_scroll, float(adj.get_value())))
        self.queue_draw()

    def set_v_scroll(self, new_offset: float) -> None:
        clamped = max(0.0, min(self._max_v_scroll, float(new_offset)))
        if abs(clamped - self.v_scroll_offset) < 0.1:
            return
        self.v_scroll_offset = clamped
        self._updating_v_adj = True
        try:
            self.v_adj.set_value(self.v_scroll_offset)
        finally:
            self._updating_v_adj = False
        self.queue_draw()

    def _sync_v_adj(self, viewport_h: float, virtual_h: float) -> None:
        eff_upper = max(viewport_h, virtual_h)
        self._max_v_scroll = max(0.0, eff_upper - viewport_h)
        self.v_scroll_offset = max(0.0, min(self._max_v_scroll, self.v_scroll_offset))
        if (
            abs(self.v_adj.get_upper() - eff_upper) > 0.5
            or abs(self.v_adj.get_page_size() - viewport_h) > 0.5
            or abs(self.v_adj.get_value() - self.v_scroll_offset) > 0.5
        ):
            # Defer Gtk.Adjustment.configure() to GLib.idle_add so we never invalidate
            # the sibling Gtk.Scrollbar slider GtkGizmo allocation mid-snapshot.
            self._pending_v_adj_cfg = (self.v_scroll_offset, eff_upper, viewport_h)
            if not self._v_adj_idle_scheduled:
                self._v_adj_idle_scheduled = True
                GLib.idle_add(self._apply_pending_v_adj_cfg)

    def _apply_pending_v_adj_cfg(self) -> bool:
        self._v_adj_idle_scheduled = False
        cfg = self._pending_v_adj_cfg
        self._pending_v_adj_cfg = None
        if cfg is None:
            return False
        val, eff_upper, viewport_h = cfg
        self._updating_v_adj = True
        try:
            self.v_adj.configure(
                val,
                0.0,
                eff_upper,
                40.0,
                max(80.0, viewport_h * 0.75),
                viewport_h,
            )
        finally:
            self._updating_v_adj = False
        return False

    @staticmethod
    def _find_rising_zero_crossing(samples: List[float], max_search: int = 240) -> int:
        """Find first rising zero-crossing index for stable real-time oscilloscope triggering."""
        limit = min(len(samples) - 1, max_search)
        for i in range(1, limit):
            if samples[i - 1] <= 0.0 < samples[i]:
                return i
        return 0

    # -------------------------------------------------------------------------
    # Event Handlers
    # -------------------------------------------------------------------------

    def _on_scroll(
        self,
        ctrl: Gtk.EventControllerScroll,
        dx: float,
        dy: float,
    ) -> bool:
        state = ctrl.get_current_event_state()
        ctrl_held = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift_held = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt_held = bool(state & Gdk.ModifierType.ALT_MASK)

        if ctrl_held:
            if dy < 0:
                self.zoom_in_amp(1.25)
            elif dy > 0:
                self.zoom_out_amp(1.25)
            return True

        if shift_held or (abs(dx) > abs(dy) and abs(dx) > 0.01):
            delta = dx if abs(dx) > abs(dy) else dy
            step = max(1.0, float(self.visible_sample_count()) * 0.12)
            self._pan_offset_f += math.copysign(step, delta)
            self._clamp_pan()
            self.queue_draw()
            return True

        # Scroll vertically through stacked lanes only when Alt is held or pointer is over the left gutter
        left_gutter = 62.0
        in_left_gutter = self._hover_x is not None and self._hover_x < left_gutter
        if self._max_v_scroll > 0.5 and (alt_held or in_left_gutter) and abs(dy) > 0.01:
            self.set_v_scroll(self.v_scroll_offset + dy * 48.0)
            return True

        anchor = self._get_pointer_anchor_ratio()
        if dy < 0:
            self.zoom_in_time(1.25, anchor_ratio=anchor)
        elif dy > 0:
            self.zoom_out_time(1.25, anchor_ratio=anchor)
        return True

    def _on_drag_begin(self, _gesture: Gtk.GestureDrag, start_x: float, start_y: float) -> None:
        self._hover_x = start_x
        self._hover_y = start_y
        self._drag_start_pan = self.pan_offset
        self._drag_start_v_scroll = self.v_scroll_offset

    def _on_drag_update(self, _gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        w = max(1.0, float(self.get_width()) or 920.0)
        plot_w = max(20.0, w - 70.0)
        vis = self.visible_sample_count()
        delta_samples = (-offset_x / plot_w) * float(max(1, vis - 1))
        self._pan_offset_f = float(self._drag_start_pan) + delta_samples
        self._clamp_pan()
        if self._max_v_scroll > 0.5 and abs(offset_y) > 2.0:
            self.set_v_scroll(self._drag_start_v_scroll - offset_y)
        self.queue_draw()

    def _on_pressed(self, _gesture: Gtk.GestureClick, n_press: int, _x: float, _y: float) -> None:
        if n_press == 2:
            self.reset_zoom()

    def _on_motion(self, _ctrl: Gtk.EventControllerMotion, x: float, y: float) -> None:
        self._hover_x = x
        self._hover_y = y
        self.queue_draw()

    def _on_leave(self, _ctrl: Gtk.EventControllerMotion) -> None:
        self._hover_x = None
        self._hover_y = None
        self.queue_draw()

    # -------------------------------------------------------------------------
    # Rendering (Gtk.Snapshot)
    # -------------------------------------------------------------------------

    @staticmethod
    def _rgba(r: float, g: float, b: float, a: float = 1.0) -> Gdk.RGBA:
        c = Gdk.RGBA()
        c.red, c.green, c.blue, c.alpha = (r, g, b, a)
        return c

    def _draw_text(
        self,
        snapshot: Gtk.Snapshot,
        text: str,
        x: float,
        y: float,
        font_str: str,
        color: Gdk.RGBA,
    ) -> Tuple[int, int]:
        layout = self.create_pango_layout(text)
        layout.set_font_description(Pango.FontDescription.from_string(font_str))
        pw, ph = layout.get_pixel_size()
        snapshot.save()
        pt = Graphene.Point.alloc()
        pt.init(x, y)
        snapshot.translate(pt)
        snapshot.append_layout(layout, color)
        snapshot.restore()
        return (pw, ph)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        w = float(self.get_width())
        h = float(self.get_height())
        if w <= 20 or h <= 20:
            return

        # Overall dark background
        bounds = Graphene.Rect.alloc()
        bounds.init(0.0, 0.0, w, h)
        snapshot.append_color(self._rgba(0.06, 0.07, 0.10, 1.0), bounds)

        num_in_lanes = min(16, max(1, len(self.multi_in_labels), len(self.multi_in_samples)))
        num_out_lanes = min(16, max(1, len(self.multi_out_labels), len(self.multi_out_samples)))
        total_lanes = num_in_lanes + num_out_lanes

        left_gutter = 62.0
        right_pad = 8.0
        top_pad = 4.0
        bottom_ruler_h = 22.0
        intra_gap = 4.0
        group_gap = 7.0 if total_lanes > 2 else 6.0

        plot_x = left_gutter
        plot_w = max(20.0, w - left_gutter - right_pad)
        total_gaps = (
            max(0, num_in_lanes - 1) * intra_gap
            + group_gap
            + max(0, num_out_lanes - 1) * intra_gap
        )
        viewport_lanes_h = max(32.0, h - top_pad - bottom_ruler_h)
        natural_lane_h = max(16.0, (viewport_lanes_h - total_gaps) / float(total_lanes))
        min_lane_h = 62.0 if total_lanes > 2 else 24.0
        lane_h = max(min_lane_h, natural_lane_h)
        virtual_lanes_h = total_lanes * lane_h + total_gaps
        self._sync_v_adj(viewport_lanes_h, virtual_lanes_h)
        all_lanes_bot_y = top_pad + min(viewport_lanes_h, virtual_lanes_h)
        ruler_y = top_pad + viewport_lanes_h + 2.0

        # Determine sample slice to display
        total = self.total_samples()
        vis = self.visible_sample_count()
        trig_shift = 0
        if self.is_realtime and self.trigger_lock:
            ref = self.out_samples if self.out_samples else self.in_samples
            if ref:
                trig_shift = self._find_rising_zero_crossing(ref, max_search=min(240, max(1, len(ref) - vis)))
        start_idx = min(max(0, total - vis), self.pan_offset + trig_shift) if total > vis else 0

        # Color palettes per vertically stacked Input lane and Output lane
        in_palettes = [
            (self._rgba(0.28, 0.80, 0.98, 0.96), self._rgba(0.28, 0.80, 0.98, 0.13), self._rgba(0.10, 0.16, 0.24, 0.95), self._rgba(0.35, 0.85, 1.0, 1.0)),
            (self._rgba(0.98, 0.76, 0.24, 0.96), self._rgba(0.98, 0.76, 0.24, 0.13), self._rgba(0.22, 0.17, 0.08, 0.95), self._rgba(1.0, 0.82, 0.35, 1.0)),
            (self._rgba(0.98, 0.52, 0.34, 0.96), self._rgba(0.98, 0.52, 0.34, 0.13), self._rgba(0.24, 0.12, 0.09, 0.95), self._rgba(1.0, 0.62, 0.44, 1.0)),
            (self._rgba(0.68, 0.95, 0.30, 0.96), self._rgba(0.68, 0.95, 0.30, 0.13), self._rgba(0.14, 0.22, 0.08, 0.95), self._rgba(0.75, 1.0, 0.40, 1.0)),
            (self._rgba(0.45, 0.65, 1.0, 0.96), self._rgba(0.45, 0.65, 1.0, 0.13), self._rgba(0.11, 0.14, 0.26, 0.95), self._rgba(0.58, 0.76, 1.0, 1.0)),
        ]
        out_palettes = [
            (self._rgba(0.24, 0.92, 0.58, 0.96), self._rgba(0.24, 0.92, 0.58, 0.13), self._rgba(0.09, 0.20, 0.16, 0.95), self._rgba(0.30, 0.96, 0.64, 1.0)),
            (self._rgba(0.76, 0.48, 1.0, 0.96), self._rgba(0.76, 0.48, 1.0, 0.13), self._rgba(0.18, 0.11, 0.25, 0.95), self._rgba(0.84, 0.62, 1.0, 1.0)),
            (self._rgba(0.20, 0.90, 0.84, 0.96), self._rgba(0.20, 0.90, 0.84, 0.13), self._rgba(0.08, 0.20, 0.20, 0.95), self._rgba(0.32, 0.98, 0.92, 1.0)),
            (self._rgba(0.98, 0.45, 0.72, 0.96), self._rgba(0.98, 0.45, 0.72, 0.13), self._rgba(0.24, 0.10, 0.18, 0.95), self._rgba(1.0, 0.58, 0.80, 1.0)),
            (self._rgba(0.95, 0.88, 0.32, 0.96), self._rgba(0.95, 0.88, 0.32, 0.13), self._rgba(0.22, 0.20, 0.08, 0.95), self._rgba(1.0, 0.94, 0.45, 1.0)),
        ]

        # Build flat list of active Input lanes (num_in_lanes) followed by active Output lanes (num_out_lanes)
        resolved_lanes: List[Tuple[str, str, List[float], float, float]] = []
        self.last_rendered_lanes: List[Tuple[str, str, int]] = []

        lanes_clip = Graphene.Rect.alloc()
        lanes_clip.init(0.0, top_pad, w, viewport_lanes_h)
        snapshot.push_clip(lanes_clip)

        cur_y = top_pad - self.v_scroll_offset
        for in_idx in range(num_in_lanes):
            lane_y = cur_y
            cur_y += lane_h + (group_gap if in_idx == num_in_lanes - 1 else intra_gap)

            if in_idx < len(self.multi_in_samples):
                s_in = self.multi_in_samples[in_idx]
            elif in_idx == 0:
                s_in = self.in_samples
            else:
                s_in = []

            s_in_lbl = (
                self.multi_in_labels[in_idx]
                if in_idx < len(self.multi_in_labels)
                else (f"IN {in_idx + 1}" if num_in_lanes > 1 else self.in_label)
            )
            s_sig_lbl = (
                self.multi_signal_labels[in_idx]
                if in_idx < len(self.multi_signal_labels)
                else self.signal_label
            )
            in_fmt_str = f"{s_sig_lbl} @ {self.sample_rate / 1000.0:.1f}kHz S{self.bits}_LE {self.channels}ch"
            title_badge = f"● {s_in_lbl}  [{in_fmt_str}]"
            short_tag = f"IN {in_idx + 1}" if num_in_lanes > 1 else "IN"
            resolved_lanes.append((short_tag, s_in_lbl, s_in, lane_y, lane_y + lane_h))
            self.last_rendered_lanes.append(("in", s_in_lbl, len(s_in)))

            if lane_y + lane_h < top_pad or lane_y > top_pad + viewport_lanes_h:
                continue

            in_pk, in_rms = TestbenchRunner._compute_metrics(s_in)
            in_dur_ms = (len(s_in) / float(max(1, self.sample_rate))) * 1000.0
            in_metrics = (
                f"Peak: {in_pk:+.1f} dBFS  |  RMS: {in_rms:+.1f} dBFS  |  {len(s_in)} smp ({in_dur_ms:.1f} ms)"
                if s_in
                else "No input signal loaded"
            )
            in_tr, in_fl, in_hb, in_ac = in_palettes[in_idx % len(in_palettes)]
            self._draw_waveform_lane(
                snapshot=snapshot,
                lane_x=plot_x,
                lane_y=lane_y,
                lane_w=plot_w,
                lane_h=lane_h,
                left_gutter=left_gutter,
                title_badge=title_badge,
                metrics_badge=in_metrics,
                samples=s_in,
                start_idx=start_idx,
                vis_count=vis,
                trace_color=in_tr,
                fill_color=in_fl,
                header_bg=in_hb,
                accent_color=in_ac,
            )

        for out_idx in range(num_out_lanes):
            lane_y = cur_y
            cur_y += lane_h + intra_gap

            if out_idx < len(self.multi_out_samples):
                s_out = self.multi_out_samples[out_idx]
            elif out_idx == 0:
                s_out = self.out_samples
            else:
                s_out = []

            s_out_lbl = (
                self.multi_out_labels[out_idx]
                if out_idx < len(self.multi_out_labels)
                else (f"OUT {out_idx + 1}" if num_out_lanes > 1 else self.out_label)
            )
            out_fmt_str = f"{self.sample_rate / 1000.0:.1f}kHz S{self.bits}_LE {self.channels}ch"
            title_badge = f"● {s_out_lbl}  [{out_fmt_str}]"
            short_tag = f"OUT {out_idx + 1}" if num_out_lanes > 1 else "OUT"
            resolved_lanes.append((short_tag, s_out_lbl, s_out, lane_y, lane_y + lane_h))
            self.last_rendered_lanes.append(("out", s_out_lbl, len(s_out)))

            if lane_y + lane_h < top_pad or lane_y > top_pad + viewport_lanes_h:
                continue

            out_pk, out_rms = TestbenchRunner._compute_metrics(s_out)
            out_dur_ms = (len(s_out) / float(max(1, self.sample_rate))) * 1000.0
            out_metrics = (
                f"Peak: {out_pk:+.1f} dBFS  |  RMS: {out_rms:+.1f} dBFS  |  {len(s_out)} smp ({out_dur_ms:.1f} ms)"
                if s_out
                else "No output captured yet"
            )
            out_tr, out_fl, out_hb, out_ac = out_palettes[out_idx % len(out_palettes)]
            self._draw_waveform_lane(
                snapshot=snapshot,
                lane_x=plot_x,
                lane_y=lane_y,
                lane_w=plot_w,
                lane_h=lane_h,
                left_gutter=left_gutter,
                title_badge=title_badge,
                metrics_badge=out_metrics,
                samples=s_out,
                start_idx=start_idx,
                vis_count=vis,
                trace_color=out_tr,
                fill_color=out_fl,
                header_bg=out_hb,
                accent_color=out_ac,
            )

        snapshot.pop()

        self._draw_time_ruler(
            snapshot=snapshot,
            plot_x=plot_x,
            ruler_y=ruler_y,
            plot_w=plot_w,
            ruler_h=bottom_ruler_h - 2.0,
            start_idx=start_idx,
            vis_count=vis,
            total_count=total,
            left_gutter=left_gutter,
        )

        if (
            self._hover_x is not None
            and self._hover_y is not None
            and plot_x <= self._hover_x <= plot_x + plot_w
            and top_pad <= self._hover_y <= all_lanes_bot_y
            and resolved_lanes
        ):
            hov_idx = 0
            for idx, (_tag, _lbl, _smps, ly0, ly1) in enumerate(resolved_lanes):
                if ly0 <= self._hover_y <= ly1:
                    hov_idx = idx
                    break
            h_tag, _h_lbl, h_samples, _hy0, _hy1 = resolved_lanes[hov_idx]
            primary_in = resolved_lanes[0][2] if num_in_lanes >= 1 else self.in_samples
            primary_out = resolved_lanes[num_in_lanes][2] if len(resolved_lanes) > num_in_lanes else self.out_samples
            self._draw_hover_crosshair(
                snapshot=snapshot,
                hx=self._hover_x,
                hy=self._hover_y,
                plot_x=plot_x,
                plot_w=plot_w,
                top_y=top_pad,
                bot_y=all_lanes_bot_y,
                start_idx=start_idx,
                vis_count=vis,
                in_samples_col=primary_in,
                out_samples_col=primary_out,
                hovered_tag=h_tag if total_lanes > 2 else None,
                hovered_samples=h_samples if total_lanes > 2 else None,
            )

    def _draw_waveform_lane(
        self,
        snapshot: Gtk.Snapshot,
        lane_x: float,
        lane_y: float,
        lane_w: float,
        lane_h: float,
        left_gutter: float,
        title_badge: str,
        metrics_badge: str,
        samples: List[float],
        start_idx: int,
        vis_count: int,
        trace_color: Gdk.RGBA,
        fill_color: Gdk.RGBA,
        header_bg: Gdk.RGBA,
        accent_color: Gdk.RGBA,
    ) -> None:
        hdr_h = min(19.0, max(13.0, lane_h * 0.25))
        body_y = lane_y + hdr_h
        body_h = max(12.0, lane_h - hdr_h)
        mid_y = body_y + body_h * 0.5
        amp_px = (body_h * 0.44)

        # Lane background & header strip
        lane_rect = Graphene.Rect.alloc()
        lane_rect.init(lane_x, lane_y, lane_w, lane_h)
        snapshot.append_color(self._rgba(0.08, 0.10, 0.14, 1.0), lane_rect)

        hdr_rect = Graphene.Rect.alloc()
        hdr_rect.init(lane_x, lane_y, lane_w, hdr_h)
        snapshot.append_color(header_bg, hdr_rect)

        # Left gutter background for this lane
        gut_x = max(2.0, lane_x - left_gutter + 2.0)
        gut_rect = Graphene.Rect.alloc()
        gut_rect.init(gut_x, lane_y, left_gutter - 4.0, lane_h)
        snapshot.append_color(self._rgba(0.07, 0.08, 0.12, 1.0), gut_rect)

        # Y-axis scale labels in left gutter
        full_scale_val = 1.0 / max(0.1, self.amp_zoom)
        axis_col = self._rgba(0.62, 0.68, 0.78, 0.95)
        self._draw_text(
            snapshot,
            f"+{full_scale_val:.2g}",
            gut_x + 4.0,
            body_y + 1.0,
            "Monospace 7.5",
            axis_col,
        )
        if body_h >= 34.0:
            self._draw_text(
                snapshot,
                " 0.0",
                gut_x + 4.0,
                mid_y - 6.0,
                "Monospace 7.5",
                axis_col,
            )
        if body_h >= 22.0:
            self._draw_text(
                snapshot,
                f"-{full_scale_val:.2g}",
                gut_x + 4.0,
                body_y + body_h - 13.0,
                "Monospace 7.5",
                axis_col,
            )

        # Horizontal & vertical grid lines inside body
        grid_pb = Gsk.PathBuilder.new()
        for frac in (0.25, 0.75):
            gy = body_y + body_h * frac
            grid_pb.move_to(lane_x, gy)
            grid_pb.line_to(lane_x + lane_w, gy)
        n_vgrid = 6
        for gi_idx in range(1, n_vgrid):
            gx = lane_x + (gi_idx / float(n_vgrid)) * lane_w
            grid_pb.move_to(gx, body_y)
            grid_pb.line_to(gx, body_y + body_h)
        snapshot.append_stroke(
            grid_pb.to_path(),
            Gsk.Stroke.new(1.0),
            self._rgba(0.14, 0.17, 0.23, 0.85),
        )

        # Zero line
        zpb = Gsk.PathBuilder.new()
        zpb.move_to(lane_x, mid_y)
        zpb.line_to(lane_x + lane_w, mid_y)
        snapshot.append_stroke(
            zpb.to_path(),
            Gsk.Stroke.new(1.1),
            self._rgba(0.24, 0.30, 0.40, 1.0),
        )

        # Header labels (Left: Stream Title & Format; Right: Peak/RMS/Frames)
        m_layout = self.create_pango_layout(metrics_badge)
        m_layout.set_font_description(Pango.FontDescription.from_string("Monospace 7.5"))
        mw, _ = m_layout.get_pixel_size()
        show_metrics = (lane_w - mw - 10.0 > 130.0)
        avail_title_px = (lane_w - mw - 20.0) if show_metrics else (lane_w - 16.0)
        disp_title = title_badge
        if avail_title_px < len(disp_title) * 6.5:
            max_chars = max(16, int(avail_title_px / 6.2))
            if len(disp_title) > max_chars:
                disp_title = disp_title[: max_chars - 1] + "…"

        self._draw_text(
            snapshot,
            disp_title,
            lane_x + 8.0,
            lane_y + 2.0,
            "Sans Bold 8",
            accent_color,
        )
        if show_metrics:
            snapshot.save()
            pt = Graphene.Point.alloc()
            pt.init(lane_x + lane_w - mw - 8.0, lane_y + 2.5)
            snapshot.translate(pt)
            snapshot.append_layout(m_layout, self._rgba(0.84, 0.90, 0.96, 0.95))
            snapshot.restore()

        # Clip & Draw Waveform Trace inside body rect
        clip_rect = Graphene.Rect.alloc()
        clip_rect.init(lane_x, body_y, lane_w, body_h)
        snapshot.push_clip(clip_rect)

        if samples and start_idx < len(samples):
            slice_end = min(len(samples), start_idx + vis_count)
            view_slice = samples[start_idx:slice_end]
            n_pts = len(view_slice)
            if n_pts >= 2:
                # Decimate if zoomed way out so rendering stays 60fps
                max_draw_pts = max(400, int(lane_w * 1.5))
                step = max(1, n_pts // max_draw_pts)
                pb = Gsk.PathBuilder.new()
                fill_pb = Gsk.PathBuilder.new()
                denom = float(max(1, vis_count - 1))

                first_x = lane_x
                last_x = lane_x
                for idx in range(0, n_pts, step):
                    x = lane_x + (idx / denom) * lane_w
                    scaled = max(-1.15, min(1.15, view_slice[idx] * self.amp_zoom))
                    y = mid_y - scaled * amp_px
                    if idx == 0:
                        first_x = x
                        pb.move_to(x, y)
                        fill_pb.move_to(x, mid_y)
                        fill_pb.line_to(x, y)
                    else:
                        pb.line_to(x, y)
                        fill_pb.line_to(x, y)
                    last_x = x

                fill_pb.line_to(last_x, mid_y)
                fill_pb.line_to(first_x, mid_y)
                fill_pb.close()
                snapshot.append_fill(fill_pb.to_path(), Gsk.FillRule.WINDING, fill_color)
                snapshot.append_stroke(pb.to_path(), Gsk.Stroke.new(1.65), trace_color)

                # When zoomed in closely (<= 120 samples), draw discrete sample markers
                if vis_count <= 120:
                    dot_pb = Gsk.PathBuilder.new()
                    for idx in range(n_pts):
                        x = lane_x + (idx / denom) * lane_w
                        scaled = max(-1.15, min(1.15, view_slice[idx] * self.amp_zoom))
                        y = mid_y - scaled * amp_px
                        c_pt = Graphene.Point.alloc()
                        c_pt.init(x, y)
                        dot_pb.add_circle(c_pt, 2.4)
                    snapshot.append_fill(dot_pb.to_path(), Gsk.FillRule.WINDING, accent_color)
        else:
            self._draw_text(
                snapshot,
                self.summary_label,
                lane_x + 14.0,
                mid_y - 7.0,
                "Sans Italic 8.5",
                self._rgba(0.55, 0.62, 0.72, 0.85),
            )

        snapshot.pop()

        # Lane border
        b_pb = Gsk.PathBuilder.new()
        b_pb.add_rect(lane_rect)
        snapshot.append_stroke(
            b_pb.to_path(),
            Gsk.Stroke.new(1.0),
            self._rgba(0.20, 0.25, 0.34, 1.0),
        )

    def _draw_time_ruler(
        self,
        snapshot: Gtk.Snapshot,
        plot_x: float,
        ruler_y: float,
        plot_w: float,
        ruler_h: float,
        start_idx: int,
        vis_count: int,
        total_count: int,
        left_gutter: float = 62.0,
    ) -> None:
        r_rect = Graphene.Rect.alloc()
        r_rect.init(plot_x, ruler_y, plot_w, ruler_h)
        snapshot.append_color(self._rgba(0.07, 0.08, 0.12, 1.0), r_rect)

        gut_x = max(4.0, plot_x - left_gutter + 4.0)
        self._draw_text(
            snapshot,
            "TIME / SMP",
            gut_x,
            ruler_y + 3.0,
            "Sans Bold 7",
            self._rgba(0.55, 0.62, 0.72, 0.9),
        )

        n_ticks = 3 if plot_w < 380.0 else (4 if plot_w < 560.0 else 6)
        rate = float(max(1, self.sample_rate))
        tick_col = self._rgba(0.72, 0.78, 0.88, 0.95)
        for ti in range(n_ticks + 1):
            frac = ti / float(n_ticks)
            smp_idx = start_idx + int(round(frac * (vis_count - 1)))
            t_ms = (smp_idx / rate) * 1000.0
            lbl = f"{t_ms:.2f}ms (#{smp_idx})"
            tx = plot_x + frac * plot_w
            if ti == n_ticks:
                tx -= 78.0
            elif ti > 0:
                tx -= 34.0
            self._draw_text(snapshot, lbl, max(plot_x + 2.0, tx), ruler_y + 2.0, "Monospace 7.5", tick_col)

        # Mini horizontal viewport scrollbar along bottom 3px of ruler
        if total_count > 0:
            sb_y = ruler_y + ruler_h - 3.5
            track_rect = Graphene.Rect.alloc()
            track_rect.init(plot_x, sb_y, plot_w, 3.0)
            snapshot.append_color(self._rgba(0.14, 0.17, 0.24, 1.0), track_rect)

            thumb_x = plot_x + (start_idx / float(total_count)) * plot_w
            thumb_w = max(12.0, min(plot_w, (vis_count / float(total_count)) * plot_w))
            if thumb_x + thumb_w > plot_x + plot_w:
                thumb_x = plot_x + plot_w - thumb_w
            thumb_rect = Graphene.Rect.alloc()
            thumb_rect.init(thumb_x, sb_y, thumb_w, 3.0)
            snapshot.append_color(self._rgba(0.35, 0.68, 0.95, 0.85), thumb_rect)

    def _draw_hover_crosshair(
        self,
        snapshot: Gtk.Snapshot,
        hx: float,
        plot_x: float,
        plot_w: float,
        top_y: float,
        bot_y: float,
        start_idx: int,
        vis_count: int,
        in_samples_col: Optional[List[float]] = None,
        out_samples_col: Optional[List[float]] = None,
        hy: float = 0.0,
        stream_idx: Optional[int] = None,
        hovered_tag: Optional[str] = None,
        hovered_samples: Optional[List[float]] = None,
    ) -> None:
        rel = max(0.0, min(1.0, (hx - plot_x) / max(1.0, plot_w)))
        smp_idx = start_idx + int(round(rel * max(1, vis_count - 1)))
        t_ms = (smp_idx / float(max(1, self.sample_rate))) * 1000.0

        # Vertical cursor line
        cpb = Gsk.PathBuilder.new()
        cpb.move_to(hx, top_y)
        cpb.line_to(hx, bot_y)
        snapshot.append_stroke(
            cpb.to_path(),
            Gsk.Stroke.new(1.0),
            self._rgba(1.0, 0.86, 0.36, 0.85),
        )

        if hovered_tag is not None and hovered_samples is not None:
            h_val = hovered_samples[smp_idx] if smp_idx < len(hovered_samples) else 0.0
            h_db = 20.0 * math.log10(abs(h_val)) if abs(h_val) > 1e-9 else -120.0
            info_txt = (
                f"{hovered_tag}  |  Sample #{smp_idx} ({t_ms:.3f} ms)  |  "
                f"{h_val:+.4f} ({h_db:+.1f} dBFS)"
            )
        else:
            in_arr = in_samples_col if in_samples_col is not None else self.in_samples
            out_arr = out_samples_col if out_samples_col is not None else self.out_samples
            in_val = in_arr[smp_idx] if smp_idx < len(in_arr) else 0.0
            out_val = out_arr[smp_idx] if smp_idx < len(out_arr) else 0.0
            in_db = 20.0 * math.log10(abs(in_val)) if abs(in_val) > 1e-9 else -120.0
            out_db = 20.0 * math.log10(abs(out_val)) if abs(out_val) > 1e-9 else -120.0
            stream_prefix = f"S{stream_idx}  |  " if stream_idx is not None else ""
            info_txt = (
                f"{stream_prefix}Sample #{smp_idx} ({t_ms:.3f} ms)  |  "
                f"IN: {in_val:+.4f} ({in_db:+.1f} dBFS)  |  "
                f"OUT: {out_val:+.4f} ({out_db:+.1f} dBFS)"
            )
        layout = self.create_pango_layout(info_txt)
        layout.set_font_description(Pango.FontDescription.from_string("Monospace Bold 8"))
        tw, th = layout.get_pixel_size()

        bx = min(plot_x + plot_w - tw - 14.0, max(plot_x + 6.0, hx + 10.0))
        by = min(bot_y - th - 6.0, max(top_y + 20.0, hy - th - 8.0)) if hy > 0.0 else top_y + 20.0
        bg_rect = Graphene.Rect.alloc()
        bg_rect.init(bx - 5.0, by - 2.0, float(tw + 10), float(th + 4))
        snapshot.append_color(self._rgba(0.05, 0.06, 0.09, 0.92), bg_rect)

        b_pb = Gsk.PathBuilder.new()
        b_pb.add_rect(bg_rect)
        snapshot.append_stroke(b_pb.to_path(), Gsk.Stroke.new(1.0), self._rgba(1.0, 0.86, 0.36, 0.8))

        snapshot.save()
        pt = Graphene.Point.alloc()
        pt.init(bx, by)
        snapshot.translate(pt)
        snapshot.append_layout(layout, self._rgba(0.98, 0.95, 0.82, 1.0))
        snapshot.restore()


class SchedulingTelemetryWidget(Gtk.Widget):
    """Custom GTK4 Gsk.Path/Snapshot widget rendering SOF Testbench Scheduling & Buffer Telemetry:
    - Top chart: Core ID (Y-axis, Cores 0..3) vs. Time (X-axis, ms/us across LL ticks & DP periods),
      showing which pipeline and which module are running when on each DSP core.
    - Bottom chart: Per-module buffer statistics (IBS/OBS/CPC, IN/OUT pin buffer capacity,
      LL entry->exit available/free bytes, DP ring fill, bytes consumed/produced, RMS/Peak dBFS,
      and pin sample tap sparklines).
    """

    PIPE_PALETTE: List[Tuple[float, float, float]] = [
        (0.24, 0.68, 0.96),  # Pipe 1: Cyan-Blue
        (0.32, 0.86, 0.56),  # Pipe 2: Emerald Green
        (0.96, 0.64, 0.24),  # Pipe 3: Amber Orange
        (0.76, 0.46, 0.96),  # Pipe 4: Purple
        (0.96, 0.38, 0.58),  # Pipe 5: Rose Pink
        (0.88, 0.84, 0.28),  # Pipe 6: Yellow-Gold
    ]

    MOD_PALETTE: List[Tuple[float, float, float]] = [
        (0.20, 0.58, 0.88),
        (0.26, 0.76, 0.52),
        (0.88, 0.56, 0.22),
        (0.68, 0.42, 0.88),
        (0.22, 0.74, 0.78),
        (0.86, 0.38, 0.52),
        (0.56, 0.76, 0.28),
        (0.82, 0.68, 0.26),
    ]

    def __init__(
        self,
        on_zoom_changed: Optional[Callable[[], None]] = None,
        on_violation_clicked: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)

        self.telemetry: Optional[TestbenchTelemetry] = None
        self.on_zoom_changed = on_zoom_changed
        self.on_violation_clicked = on_violation_clicked

        # Expandable violation tree state in Module Buffer Stats & Pin Telemetry
        self._expanded_violation_mods: Set[int] = set()
        self._collapsed_violation_groups: Set[str] = set()
        self._selected_violation_key: Optional[str] = None
        self._focused_violation_slice: Optional[Dict[str, Any]] = None

        # Time axis zoom & pan state
        self.time_zoom: float = 1.0          # 1.0x = show ~12 LL ticks across viewport, up to 64.0x
        self.pan_offset_ms: float = 0.0      # Horizontal scroll offset in milliseconds
        self.expanded_tick_mode: bool = True # Magnify sub-ms execution blocks within each 1ms tick for readability
        self.show_all_cores: bool = True     # Show Cores 0..3 on Y axis
        self.ignore_tick0_underrun: bool = True  # Ignore tick 0 / no-input underrun & stall violations

        # Pin buffer sample sparkline zoom & pan state (for 64-sample buffer taps)
        self.buf_spark_zoom: float = 1.0
        self.buf_spark_pan_f: float = 0.0
        self._spark_hit_boxes: List[Dict[str, Any]] = []

        # Vertical scroll adjustment for the Module Buffer Stats section
        self.v_scroll_offset: float = 0.0
        self._updating_v_adj: bool = False
        self._v_adj_idle_scheduled: bool = False
        self._pending_v_adj_cfg: Optional[Tuple[float, float, float]] = None
        self.v_adj = Gtk.Adjustment.new(0.0, 0.0, 100.0, 24.0, 96.0, 100.0)
        self.v_adj.connect("value-changed", self._on_v_adj_value_changed)

        # Interactive hover & drag state
        self._hover_x: float = -1.0
        self._hover_y: float = -1.0
        self._hovered_comp_id: Optional[int] = None
        self._hovered_pipe_id: Optional[int] = None
        self._hovered_hit_info: Optional[Dict[str, Any]] = None
        self._hit_boxes: List[Dict[str, Any]] = []
        self._drag_start_pan_ms: float = 0.0
        self._drag_start_v_scroll: float = 0.0
        self._last_plot_w: float = 700.0
        self._last_buf_area_y: float = 200.0

        self._setup_controllers()

    def _setup_controllers(self) -> None:
        scroll_ctrl = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL | Gtk.EventControllerScrollFlags.HORIZONTAL
        )
        scroll_ctrl.connect("scroll", self._on_scroll)
        self.add_controller(scroll_ctrl)

        drag_ctrl = Gtk.GestureDrag.new()
        drag_ctrl.set_button(1)
        drag_ctrl.connect("drag-begin", self._on_drag_begin)
        drag_ctrl.connect("drag-update", self._on_drag_update)
        self.add_controller(drag_ctrl)

        click_ctrl = Gtk.GestureClick.new()
        click_ctrl.set_button(1)
        click_ctrl.connect("pressed", self._on_click_pressed)
        self.add_controller(click_ctrl)

        motion_ctrl = Gtk.EventControllerMotion.new()
        motion_ctrl.connect("motion", self._on_motion)
        motion_ctrl.connect("leave", self._on_leave)
        self.add_controller(motion_ctrl)

    def _on_v_adj_value_changed(self, adj: Gtk.Adjustment) -> None:
        if self._updating_v_adj:
            return
        self.v_scroll_offset = max(0.0, float(adj.get_value()))
        self.queue_draw()

    def _sync_v_adjustment(self, content_h: float, viewport_h: float) -> None:
        eff_upper = max(content_h, viewport_h)
        max_scroll = max(0.0, eff_upper - viewport_h)
        if self.v_scroll_offset > max_scroll:
            self.v_scroll_offset = max_scroll
        if (
            abs(self.v_adj.get_upper() - eff_upper) > 0.5
            or abs(self.v_adj.get_page_size() - viewport_h) > 0.5
            or abs(self.v_adj.get_value() - self.v_scroll_offset) > 0.5
        ):
            self._pending_v_adj_cfg = (self.v_scroll_offset, eff_upper, viewport_h)
            if not self._v_adj_idle_scheduled:
                self._v_adj_idle_scheduled = True
                GLib.idle_add(self._apply_pending_v_adj_cfg)

    def _apply_pending_v_adj_cfg(self) -> bool:
        self._v_adj_idle_scheduled = False
        cfg = self._pending_v_adj_cfg
        self._pending_v_adj_cfg = None
        if cfg is None:
            return False
        val, eff_upper, viewport_h = cfg
        self._updating_v_adj = True
        try:
            self.v_adj.configure(
                val,
                0.0,
                eff_upper,
                28.0,
                max(48.0, viewport_h * 0.75),
                viewport_h,
            )
        finally:
            self._updating_v_adj = False
        return False

    def set_telemetry(self, telemetry: Optional[TestbenchTelemetry]) -> None:
        self.telemetry = telemetry
        self._clamp_pan()
        self._notify_zoom()
        self.queue_draw()

    def set_expanded_tick_mode(self, expanded: bool) -> None:
        self.expanded_tick_mode = bool(expanded)
        self._notify_zoom()
        self.queue_draw()

    def set_ignore_tick0_underrun(self, ignore: bool) -> None:
        self.ignore_tick0_underrun = bool(ignore)
        self.queue_draw()

    def _effective_pin_xruns(self, m_obj: ComponentPerfItem, pin_obj: ModulePinBufferStats) -> int:
        """Return effective xrun count from actual testbench/FW errors, honoring ignore_tick0_underrun."""
        p_dir = str(getattr(pin_obj, "direction", "IN") or "IN").upper()
        p_idx = int(getattr(pin_obj, "pin_index", 0) or 0)
        tot_x = max(
            int(getattr(pin_obj, "xrun_count", 0) or 0),
            int(getattr(pin_obj, "underrun_count", 0) or 0)
            + int(getattr(pin_obj, "overrun_count", 0) or 0),
        )
        t0_x = int(getattr(pin_obj, "tick0_xrun_count", 0) or 0)
        fw_errs = getattr(m_obj, "fw_errors", []) or []
        pin_fw_errs = [
            fe
            for fe in fw_errs
            if str(fe.get("pin_dir", "")).upper() == p_dir
            and int(fe.get("pin_index", -1)) == p_idx
        ]
        if pin_fw_errs and tot_x == 0:
            tot_x = len(pin_fw_errs)
            t0_x = sum(1 for fe in pin_fw_errs if int(fe.get("tick", 0) or 0) == 0)
        if tot_x <= 0:
            return 0
        if self.ignore_tick0_underrun:
            if t0_x > 0:
                return max(0, tot_x - t0_x)
            if int(getattr(m_obj, "iterations", 0) or 0) <= 1:
                return 0
        return tot_x

    def _is_pin_underrun_or_stall_ignored(self, m_obj: ComponentPerfItem, pin_obj: ModulePinBufferStats) -> bool:
        """Return True if pin has no active testbench/FW xrun violations after applying ignore_tick0_underrun."""
        return self._effective_pin_xruns(m_obj, pin_obj) <= 0

    def _get_pointer_anchor_ratio(self) -> float:
        """Return normalized [0.0, 1.0] horizontal mouse pointer position within the schedule timeline."""
        w = max(1.0, float(self.get_width()) or 920.0)
        gutter_w = 156.0
        right_pad = 10.0
        plot_w = max(80.0, w - gutter_w - right_pad)
        if self._hover_x >= 0.0:
            return max(0.0, min(1.0, (self._hover_x - gutter_w) / plot_w))
        return 0.5

    def _get_hovered_spark_anchor_ratio(self) -> Optional[float]:
        """If the mouse pointer is inside a buffer sample sparkline, return its [0.0, 1.0] ratio."""
        if self._hover_x < 0.0 or self._hover_y < 0.0:
            return None
        for sb in self._spark_hit_boxes:
            if sb["x"] <= self._hover_x <= sb["x"] + sb["w"] and sb["y"] <= self._hover_y <= sb["y"] + sb["h"]:
                return max(0.0, min(1.0, (self._hover_x - sb["x"]) / max(1.0, sb["w"])))
        return None

    def _set_spark_zoom(self, new_zoom: float, anchor_ratio: float = 0.5, total_pts: int = 64) -> None:
        r = max(0.0, min(1.0, float(anchor_ratio)))
        old_vis = max(8.0, float(total_pts) / max(1.0, self.buf_spark_zoom))
        anchor_smp = self.buf_spark_pan_f + r * max(1.0, old_vis - 1.0)
        self.buf_spark_zoom = max(1.0, min(8.0, float(new_zoom)))
        new_vis = max(8.0, float(total_pts) / max(1.0, self.buf_spark_zoom))
        max_pan = max(0.0, float(total_pts) - new_vis)
        self.buf_spark_pan_f = max(0.0, min(max_pan, anchor_smp - r * max(1.0, new_vis - 1.0)))

    def _set_time_zoom(self, new_zoom: float, anchor_ratio: Optional[float] = None) -> None:
        if anchor_ratio is None:
            anchor_ratio = self._get_pointer_anchor_ratio()
        r = max(0.0, min(1.0, float(anchor_ratio)))
        total_ms = self._get_total_duration_ms()
        base_win = min(total_ms, 12.0)
        min_zoom = max(0.05, base_win / max(0.1, total_ms))
        max_zoom = max(64.0, base_win / 0.05)

        old_vis_ms = self._get_visible_window_ms()
        anchor_time_ms = self.pan_offset_ms + r * old_vis_ms

        self.time_zoom = max(min_zoom, min(max_zoom, float(new_zoom)))
        new_vis_ms = self._get_visible_window_ms()
        self.pan_offset_ms = anchor_time_ms - r * new_vis_ms
        self._clamp_pan()
        self._notify_zoom()
        self.queue_draw()

    def zoom_in_time(self, factor: float = 1.4, anchor_ratio: Optional[float] = None) -> None:
        self._set_time_zoom(self.time_zoom * factor, anchor_ratio)

    def zoom_out_time(self, factor: float = 1.4, anchor_ratio: Optional[float] = None) -> None:
        self._set_time_zoom(self.time_zoom / factor, anchor_ratio)

    def reset_zoom(self) -> None:
        self.time_zoom = 1.0
        self.pan_offset_ms = 0.0
        self.v_scroll_offset = 0.0
        self.buf_spark_zoom = 1.0
        self.buf_spark_pan_f = 0.0
        self._focused_violation_slice = None
        self._notify_zoom()
        self.queue_draw()

    def focus_and_zoom_to_violation(self, violation_info: Dict[str, Any]) -> None:
        """Zoom and pan the Core Schedule Timeline directly onto the violating module and tick slice."""
        telem = self.telemetry
        if not telem:
            return
        comp_id = violation_info.get("comp_id")
        pipe_id = violation_info.get("pipe_id")
        core_id = int(violation_info.get("core_id", 0) or 0)
        tick_idx = int(violation_info.get("tick_idx", 0) or 0)

        period_us = 1000
        if telem.pipelines:
            period_us = max(250, max((p.period_us for p in telem.pipelines if p.period_us > 0), default=1000))
        period_ms = period_us / 1000.0

        active_mods = [
            c for c in telem.components
            if c.iterations > 0 or c.avg_cycles > 0 or c.in_pins or c.out_pins
        ]
        active_mods.sort(key=lambda c: (c.core_id, c.pipeline_id, c.comp_id))

        target_mod = next((m for m in active_mods if m.comp_id == comp_id), None)
        if target_mod is not None:
            core_id = target_mod.core_id
            pipe_id = target_mod.pipeline_id
            self._expanded_violation_mods.add(target_mod.comp_id)

        if violation_info.get("v_key"):
            self._selected_violation_key = str(violation_info["v_key"])

        ll_mods = [
            m for m in active_mods
            if m.core_id == core_id and m.domain.upper() != "DP" and tick_idx < max(1, m.iterations)
        ]
        tot_m_us = (
            sum(
                max(0.05, m.exec_avg_us if m.exec_avg_us > 0 else (m.avg_cycles / 393.216))
                for m in ll_mods
            )
            or 1.0
        )
        c_info = next((c for c in telem.cores if c.core_id == core_id), None)
        core_ll_us = (
            c_info.ll_exec_avg_us
            if (c_info and c_info.ll_exec_avg_us > 0)
            else tot_m_us
        )
        core_ll_us = max(core_ll_us, tot_m_us, 0.2)

        if self.expanded_tick_mode:
            span_frac = min(0.84, max(0.58, (core_ll_us / float(period_us)) * 18.0))
        else:
            span_frac = min(0.98, max(0.004, core_ll_us / float(period_us)))

        mod_start_frac = 0.0
        mod_dur_frac = span_frac
        if target_mod is not None and ll_mods:
            cum_frac = 0.0
            for m in ll_mods:
                m_us = max(
                    0.05,
                    m.exec_avg_us if m.exec_avg_us > 0 else (m.avg_cycles / 393.216),
                )
                mf = m_us / tot_m_us
                if m.comp_id == target_mod.comp_id:
                    mod_start_frac = cum_frac * span_frac
                    mod_dur_frac = max(0.04, mf * span_frac)
                    break
                cum_frac += mf

        tick_start_ms = float(tick_idx) * period_ms
        slice_start_ms = tick_start_ms + mod_start_frac * period_ms
        slice_dur_ms = max(0.04, mod_dur_frac * period_ms)
        slice_center_ms = slice_start_ms + slice_dur_ms * 0.5

        total_ms = self._get_total_duration_ms()
        base_win = min(total_ms, 12.0)
        target_vis_ms = max(0.45, min(period_ms * 1.35, slice_dur_ms * 4.5))
        desired_zoom = base_win / max(0.05, target_vis_ms)
        min_zoom = max(0.05, base_win / max(0.1, total_ms))
        max_zoom = max(64.0, base_win / 0.05)

        self.time_zoom = max(min_zoom, min(max_zoom, desired_zoom))
        vis_ms = self._get_visible_window_ms()
        self.pan_offset_ms = slice_center_ms - 0.5 * vis_ms
        self._clamp_pan()

        self._focused_violation_slice = {
            **violation_info,
            "comp_id": comp_id,
            "pipe_id": pipe_id,
            "core_id": core_id,
            "tick_idx": tick_idx,
        }
        self._hovered_comp_id = comp_id
        self._hovered_pipe_id = pipe_id
        self._notify_zoom()
        self.queue_draw()

    def _get_total_duration_ms(self) -> float:
        telem = self.telemetry
        if not telem:
            return 10.0
        ticks = max(1, telem.total_ll_ticks)
        if ticks <= 1 and telem.components:
            ticks = max([1] + [c.iterations for c in telem.components])
        period_ms = 1.0
        if telem.pipelines:
            p_us = max((p.period_us for p in telem.pipelines if p.period_us > 0), default=1000)
            period_ms = max(0.25, p_us / 1000.0)
        return max(4.0, float(ticks) * period_ms)

    def _get_visible_window_ms(self) -> float:
        total_ms = self._get_total_duration_ms()
        base_win = min(total_ms, 12.0)
        return max(0.05, min(total_ms, base_win / max(0.05, self.time_zoom)))

    def _clamp_pan(self) -> None:
        total_ms = self._get_total_duration_ms()
        vis_ms = self._get_visible_window_ms()
        max_pan = max(0.0, total_ms - vis_ms)
        self.pan_offset_ms = max(0.0, min(max_pan, self.pan_offset_ms))

    def get_zoom_summary_text(self) -> str:
        vis_ms = self._get_visible_window_ms()
        total_ms = self._get_total_duration_ms()
        mode_tag = "Expanded Slices" if self.expanded_tick_mode else "True 1:1 µs"
        return f"Win: {vis_ms:.2f}/{total_ms:.1f} ms ({self.time_zoom:.1f}×, {mode_tag})"

    def _notify_zoom(self) -> None:
        if self.on_zoom_changed:
            self.on_zoom_changed()

    def _on_scroll(
        self,
        ctrl: Gtk.EventControllerScroll,
        dx: float,
        dy: float,
    ) -> bool:
        state = ctrl.get_current_event_state()
        shift_held = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt_held = bool(state & Gdk.ModifierType.ALT_MASK)

        # Scroll buffer rows vertically only when Alt is held or pointer is in the left gutter
        in_left_gutter = 0.0 <= self._hover_x < 156.0
        max_v = max(0.0, self.v_adj.get_upper() - self.v_adj.get_page_size())
        if max_v > 0.5 and (alt_held or (in_left_gutter and self._hover_y >= self._last_buf_area_y)) and abs(dy) > 0.01:
            step = 34.0
            self.v_adj.set_value(max(0.0, min(max_v, self.v_adj.get_value() + dy * step)))
            return True

        if shift_held or (abs(dx) > abs(dy) and abs(dx) > 0.01):
            delta = dx if abs(dx) > abs(dy) else dy
            vis_ms = self._get_visible_window_ms()
            self.pan_offset_ms += delta * max(0.05, vis_ms * 0.12)
            self._clamp_pan()
            self._notify_zoom()
            self.queue_draw()
            return True

        spark_anchor = self._get_hovered_spark_anchor_ratio()
        if spark_anchor is not None:
            if dy < 0:
                self._set_spark_zoom(self.buf_spark_zoom * 1.25, anchor_ratio=spark_anchor)
            elif dy > 0:
                self._set_spark_zoom(self.buf_spark_zoom / 1.25, anchor_ratio=spark_anchor)

        anchor = self._get_pointer_anchor_ratio()
        if dy < 0:
            self.zoom_in_time(1.25, anchor_ratio=anchor)
        elif dy > 0:
            self.zoom_out_time(1.25, anchor_ratio=anchor)
        return True

    def _on_drag_begin(self, _gesture: Gtk.GestureDrag, start_x: float, start_y: float) -> None:
        self._drag_start_pan_ms = self.pan_offset_ms
        self._drag_start_v_scroll = self.v_scroll_offset
        self._hover_x = start_x
        self._hover_y = start_y

    def _on_drag_update(self, _gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        vis_ms = self._get_visible_window_ms()
        ms_per_px = vis_ms / max(60.0, self._last_plot_w)
        self.pan_offset_ms = self._drag_start_pan_ms - offset_x * ms_per_px
        self._clamp_pan()
        if self._hover_y >= self._last_buf_area_y:
            max_s = max(0.0, self.v_adj.get_upper() - self.v_adj.get_page_size())
            self.v_adj.set_value(max(0.0, min(max_s, self._drag_start_v_scroll - offset_y)))
        self._notify_zoom()
        self.queue_draw()

    def _collect_module_violations(self, m_obj: ComponentPerfItem) -> List[Dict[str, Any]]:
        """Collect structured violation tree nodes (and their expandable child events) for a module."""
        violations: List[Dict[str, Any]] = []
        telem = self.telemetry
        wname = m_obj.widget_name or m_obj.module_name or f"0x{m_obj.comp_id:x}"
        m_us = m_obj.exec_avg_us if m_obj.exec_avg_us > 0 else (m_obj.avg_cycles / 393.216)
        m_pk_us = m_obj.exec_peak_us if m_obj.exec_peak_us > 0 else (m_obj.peak_cycles / 393.216)

        # 1. CPC Budget Violations
        has_cpc_v = m_obj.cpc_violations > 0 or (
            m_obj.effective_cpc > 0 and m_obj.peak_cycles > m_obj.effective_cpc
        )
        if has_cpc_v:
            v_cnt = max(1, m_obj.cpc_violations)
            cpc_bud = max(1, m_obj.effective_cpc or m_obj.avg_cycles)
            over_peak = max(0, m_obj.peak_cycles - cpc_bud)
            over_avg = max(0, m_obj.avg_cycles - cpc_bud)
            pct_peak = (m_obj.peak_cycles / float(cpc_bud)) * 100.0
            gkey = f"mod:{m_obj.comp_id}:cpc"
            children: List[Dict[str, Any]] = []
            max_events = min(v_cnt, 5)
            in_b = m_obj.in_pins[0].ll_entry_avail if m_obj.in_pins else m_obj.ibs
            out_b = m_obj.out_pins[0].ll_exit_avail if m_obj.out_pins else m_obj.obs
            for ev_i in range(max_events):
                ev_cyc = m_obj.peak_cycles if ev_i == 0 else max(cpc_bud + 1, m_obj.avg_cycles)
                ev_over = max(1, ev_cyc - cpc_bud)
                ev_us = m_pk_us if ev_i == 0 else m_us
                ev_key = f"{gkey}:ev:{ev_i}"
                children.append(
                    {
                        "v_key": ev_key,
                        "type": "CPC_VIOLATION_EVENT",
                        "comp_id": m_obj.comp_id,
                        "pipe_id": m_obj.pipeline_id,
                        "core_id": m_obj.core_id,
                        "widget_name": wname,
                        "tick_idx": ev_i,
                        "time_ms": float(ev_i),
                        "cycles": ev_cyc,
                        "effective_cpc": cpc_bud,
                        "over_cycles": ev_over,
                        "exec_us": ev_us,
                        "count": v_cnt,
                        "summary": (
                            f"Tick #{ev_i} ({float(ev_i):.2f} ms): cyc={ev_cyc} > CPC={cpc_bud} "
                            f"(+{ev_over} cyc, {ev_us:.2f} µs) | mem={m_obj.mem_type} IN={in_b}B OUT={out_b}B"
                        ),
                        "mod": m_obj,
                    }
                )
            if v_cnt > max_events:
                children.append(
                    {
                        "v_key": f"{gkey}:ev:more",
                        "type": "CPC_VIOLATION_SUMMARY",
                        "comp_id": m_obj.comp_id,
                        "pipe_id": m_obj.pipeline_id,
                        "core_id": m_obj.core_id,
                        "widget_name": wname,
                        "cycles": m_obj.avg_cycles,
                        "effective_cpc": cpc_bud,
                        "over_cycles": over_avg,
                        "exec_us": m_us,
                        "count": v_cnt - max_events,
                        "summary": (
                            f"+{v_cnt - max_events} additional CPC violations across {m_obj.iterations} LL ticks "
                            f"(avg={m_obj.avg_cycles} cyc, peak={m_obj.peak_cycles} cyc, budget={cpc_bud} cyc)"
                        ),
                        "mod": m_obj,
                    }
                )
            violations.append(
                {
                    "v_key": gkey,
                    "type": "CPC_VIOLATION",
                    "comp_id": m_obj.comp_id,
                    "pipe_id": m_obj.pipeline_id,
                    "core_id": m_obj.core_id,
                    "widget_name": wname,
                    "count": v_cnt,
                    "cycles": m_obj.peak_cycles,
                    "effective_cpc": cpc_bud,
                    "over_cycles": over_peak,
                    "exec_us": m_pk_us,
                    "summary": (
                        f"CPC Budget Exceeded ({v_cnt}×): peak={m_obj.peak_cycles} cyc > CPC={cpc_bud} cyc "
                        f"(+{over_peak} cyc, {pct_peak:.1f}% of budget, peak={m_pk_us:.2f} µs)"
                    ),
                    "children": children,
                    "mod": m_obj,
                }
            )

        # 2. Pipeline XRUN / Period Overrun Violations (only attach to the module that violated CPC
        #    or the highest-cycle module in the pipeline if no module had an explicit CPC violation,
        #    so healthy neighbor modules do not get cascaded violation messages)
        if telem and telem.pipelines:
            pipe = next((p for p in telem.pipelines if p.pipeline_id == m_obj.pipeline_id), None)
            if pipe and (pipe.xruns > 0 or (pipe.period_us > 0 and pipe.exec_peak_us > pipe.period_us)):
                pipe_mods = [c for c in telem.components if c.pipeline_id == m_obj.pipeline_id]
                any_pipe_cpc_v = any(
                    c.cpc_violations > 0 or (c.effective_cpc > 0 and c.peak_cycles > c.effective_cpc)
                    for c in pipe_mods
                )
                is_pipe_culprit = has_cpc_v if any_pipe_cpc_v else (
                    m_obj.peak_cycles > 0
                    and m_obj.peak_cycles == max((c.peak_cycles for c in pipe_mods), default=0)
                )
                if is_pipe_culprit:
                    gkey = f"mod:{m_obj.comp_id}:pipe_xrun"
                    x_cnt = max(1, pipe.xruns)
                    violations.append(
                        {
                            "v_key": gkey,
                            "type": "PIPELINE_XRUN",
                            "comp_id": m_obj.comp_id,
                            "pipe_id": m_obj.pipeline_id,
                            "core_id": m_obj.core_id,
                            "widget_name": wname,
                            "count": x_cnt,
                            "cycles": pipe.peak_cycles,
                            "effective_cpc": 0,
                            "over_cycles": 0,
                            "exec_us": pipe.exec_peak_us,
                            "summary": (
                                f"Pipeline {pipe.pipeline_id} XRUN / Period Overrun ({x_cnt}×): "
                                f"pipe_peak={pipe.exec_peak_us:.2f} µs vs period={pipe.period_us} µs"
                            ),
                            "children": [
                                {
                                    "v_key": f"{gkey}:ev:0",
                                    "type": "PIPELINE_XRUN_EVENT",
                                    "comp_id": m_obj.comp_id,
                                    "pipe_id": m_obj.pipeline_id,
                                    "core_id": m_obj.core_id,
                                    "widget_name": wname,
                                    "count": x_cnt,
                                    "exec_us": pipe.exec_peak_us,
                                    "summary": (
                                        f"Pipe {pipe.pipeline_id} on Core {pipe.core_id}: xruns={pipe.xruns}, "
                                        f"avg={pipe.exec_avg_us:.2f} µs / peak={pipe.exec_peak_us:.2f} µs ({pipe.avg_kcps} kcps)"
                                    ),
                                    "mod": m_obj,
                                }
                            ],
                            "mod": m_obj,
                        }
                    )

        # 3. Core LL Deadline Violations (only attach to the violating module, never cascade to healthy modules)
        if telem and telem.cores:
            core = next((c for c in telem.cores if c.core_id == m_obj.core_id), None)
            if core and core.overruns > 0:
                core_mods = [c for c in telem.components if c.core_id == m_obj.core_id]
                any_core_cpc_v = any(
                    c.cpc_violations > 0 or (c.effective_cpc > 0 and c.peak_cycles > c.effective_cpc)
                    for c in core_mods
                )
                is_core_culprit = has_cpc_v if any_core_cpc_v else (
                    m_obj.peak_cycles > 0
                    and m_obj.peak_cycles == max((c.peak_cycles for c in core_mods), default=0)
                )
                if is_core_culprit:
                    gkey = f"mod:{m_obj.comp_id}:core_overrun"
                    violations.append(
                        {
                            "v_key": gkey,
                            "type": "CORE_OVERRUN",
                            "comp_id": m_obj.comp_id,
                            "pipe_id": m_obj.pipeline_id,
                            "core_id": m_obj.core_id,
                            "widget_name": wname,
                            "count": core.overruns,
                            "cycles": core.peak_cycles,
                            "effective_cpc": 0,
                            "over_cycles": 0,
                            "exec_us": core.last_time_elapsed_us,
                            "summary": (
                                f"Core {core.core_id} LL Deadline Overrun ({core.overruns}×): "
                                f"max={core.max_time_elapsed_cyc} cyc, load={core.cpu_load_pct:.1f}%"
                            ),
                            "children": [
                                {
                                    "v_key": f"{gkey}:ev:0",
                                    "type": "CORE_OVERRUN_EVENT",
                                    "comp_id": m_obj.comp_id,
                                    "pipe_id": m_obj.pipeline_id,
                                    "core_id": m_obj.core_id,
                                    "widget_name": wname,
                                    "count": core.overruns,
                                    "summary": (
                                        f"Core {core.core_id} @ {core.freq_hz // 1000000} MHz: "
                                        f"overruns={core.overruns}, ticks={core.tick_count}, peak={core.peak_kcps} kcps"
                                    ),
                                    "mod": m_obj,
                                }
                            ],
                            "mod": m_obj,
                        }
                    )

        # 4. Pin Buffer Xrun / Underrun / Overrun Violations from actual testbench/FW errors
        fw_errs = list(getattr(m_obj, "fw_errors", []) or [])
        if self.ignore_tick0_underrun:
            fw_errs = [fe for fe in fw_errs if int(fe.get("tick", 0) or 0) > 0]
        handled_fw_err_ids: set = set()

        for p_in in m_obj.in_pins:
            eff_x = self._effective_pin_xruns(m_obj, p_in)
            if eff_x > 0:
                gkey = f"mod:{m_obj.comp_id}:pin_in:{p_in.pin_index}"
                pin_logs = [
                    fe
                    for fe in fw_errs
                    if str(fe.get("pin_dir", "")).upper() == "IN"
                    and int(fe.get("pin_index", -1)) in (p_in.pin_index, -1)
                ]
                for fe in pin_logs:
                    handled_fw_err_ids.add(id(fe))
                last_msg = (
                    pin_logs[-1].get("msg", "")
                    if pin_logs
                    else (m_obj.last_error_msg or f"underrun={p_in.underrun_count} overrun={p_in.overrun_count}")
                )
                in_children: List[Dict[str, Any]] = []
                for ev_i, fe in enumerate(pin_logs[:5]):
                    t_idx = int(fe.get("tick", 0) or 0)
                    in_children.append(
                        {
                            "v_key": f"{gkey}:ev:{ev_i}",
                            "type": "FW_LOG_ERROR",
                            "comp_id": m_obj.comp_id,
                            "pipe_id": m_obj.pipeline_id,
                            "core_id": m_obj.core_id,
                            "widget_name": wname,
                            "tick_idx": t_idx,
                            "time_ms": float(t_idx),
                            "count": 1,
                            "summary": (
                                f"[FW {fe.get('level', 'ERR')} @ Tick #{t_idx} ({fe.get('file', 'fw')}:{fe.get('line', 0)})] "
                                f"{fe.get('msg', '')}"
                            ),
                            "mod": m_obj,
                        }
                    )
                if not in_children:
                    in_children.append(
                        {
                            "v_key": f"{gkey}:ev:0",
                            "type": "PIN_BUFFER_EVENT",
                            "comp_id": m_obj.comp_id,
                            "pipe_id": m_obj.pipeline_id,
                            "core_id": m_obj.core_id,
                            "widget_name": wname,
                            "count": eff_x,
                            "summary": (
                                f"IN[{p_in.pin_index}] peer=0x{p_in.peer_comp_id:x} ({p_in.peer_widget_name or 'host'}): "
                                f"xruns={eff_x} (underrun={p_in.underrun_count}, overrun={p_in.overrun_count}) | "
                                f"entry_avail={p_in.ll_entry_avail}B, exit_avail={p_in.ll_exit_avail}B | {last_msg}"
                            ),
                            "mod": m_obj,
                        }
                    )
                violations.append(
                    {
                        "v_key": gkey,
                        "type": "PIN_BUFFER_UNDERRUN",
                        "comp_id": m_obj.comp_id,
                        "pipe_id": m_obj.pipeline_id,
                        "core_id": m_obj.core_id,
                        "widget_name": wname,
                        "count": eff_x,
                        "summary": (
                            f"IN[{p_in.pin_index}] FW Buffer Underrun/Xrun ({eff_x}×, buf={p_in.buf_id}): {last_msg}"
                        ),
                        "children": in_children,
                        "mod": m_obj,
                    }
                )

        for p_out in m_obj.out_pins:
            eff_x = self._effective_pin_xruns(m_obj, p_out)
            if eff_x > 0:
                gkey = f"mod:{m_obj.comp_id}:pin_out:{p_out.pin_index}"
                pin_logs = [
                    fe
                    for fe in fw_errs
                    if str(fe.get("pin_dir", "")).upper() == "OUT"
                    and int(fe.get("pin_index", -1)) in (p_out.pin_index, -1)
                ]
                for fe in pin_logs:
                    handled_fw_err_ids.add(id(fe))
                last_msg = (
                    pin_logs[-1].get("msg", "")
                    if pin_logs
                    else (m_obj.last_error_msg or f"overrun={p_out.overrun_count} underrun={p_out.underrun_count}")
                )
                out_children: List[Dict[str, Any]] = []
                for ev_i, fe in enumerate(pin_logs[:5]):
                    t_idx = int(fe.get("tick", 0) or 0)
                    out_children.append(
                        {
                            "v_key": f"{gkey}:ev:{ev_i}",
                            "type": "FW_LOG_ERROR",
                            "comp_id": m_obj.comp_id,
                            "pipe_id": m_obj.pipeline_id,
                            "core_id": m_obj.core_id,
                            "widget_name": wname,
                            "tick_idx": t_idx,
                            "time_ms": float(t_idx),
                            "count": 1,
                            "summary": (
                                f"[FW {fe.get('level', 'ERR')} @ Tick #{t_idx} ({fe.get('file', 'fw')}:{fe.get('line', 0)})] "
                                f"{fe.get('msg', '')}"
                            ),
                            "mod": m_obj,
                        }
                    )
                if not out_children:
                    out_children.append(
                        {
                            "v_key": f"{gkey}:ev:0",
                            "type": "PIN_BUFFER_EVENT",
                            "comp_id": m_obj.comp_id,
                            "pipe_id": m_obj.pipeline_id,
                            "core_id": m_obj.core_id,
                            "widget_name": wname,
                            "count": eff_x,
                            "summary": (
                                f"OUT[{p_out.pin_index}] peer=0x{p_out.peer_comp_id:x} ({p_out.peer_widget_name or 'dai'}): "
                                f"xruns={eff_x} (overrun={p_out.overrun_count}, underrun={p_out.underrun_count}) | "
                                f"entry_avail={p_out.ll_entry_avail}B, free={p_out.ll_exit_free}B | {last_msg}"
                            ),
                            "mod": m_obj,
                        }
                    )
                violations.append(
                    {
                        "v_key": gkey,
                        "type": "PIN_BUFFER_STALL",
                        "comp_id": m_obj.comp_id,
                        "pipe_id": m_obj.pipeline_id,
                        "core_id": m_obj.core_id,
                        "widget_name": wname,
                        "count": eff_x,
                        "summary": (
                            f"OUT[{p_out.pin_index}] FW Buffer Overrun/Stall ({eff_x}×, buf={p_out.buf_id}): {last_msg}"
                        ),
                        "children": out_children,
                        "mod": m_obj,
                    }
                )

        # 5. Any remaining module-level FW errors logged by testbench over the network interface
        rem_fw_errs = [fe for fe in fw_errs if id(fe) not in handled_fw_err_ids]
        raw_err_cnt = int(getattr(m_obj, "error_count", 0) or 0)
        t0_err_cnt = int(getattr(m_obj, "tick0_error_count", 0) or 0)
        eff_mod_err = (
            max(0, raw_err_cnt - t0_err_cnt)
            if (self.ignore_tick0_underrun and t0_err_cnt > 0)
            else (0 if (self.ignore_tick0_underrun and m_obj.iterations <= 1) else raw_err_cnt)
        )
        if rem_fw_errs or (eff_mod_err > 0 and not any(v["type"].startswith("PIN_BUFFER_") for v in violations)):
            gkey = f"mod:{m_obj.comp_id}:fw_err"
            err_cnt = max(eff_mod_err, len(rem_fw_errs), 1)
            last_msg = (
                rem_fw_errs[-1].get("msg", "")
                if rem_fw_errs
                else (m_obj.last_error_msg or f"module error count={err_cnt}")
            )
            fw_children: List[Dict[str, Any]] = []
            for ev_i, fe in enumerate(rem_fw_errs[:5]):
                t_idx = int(fe.get("tick", 0) or 0)
                fw_children.append(
                    {
                        "v_key": f"{gkey}:ev:{ev_i}",
                        "type": "FW_LOG_ERROR",
                        "comp_id": m_obj.comp_id,
                        "pipe_id": m_obj.pipeline_id,
                        "core_id": m_obj.core_id,
                        "widget_name": wname,
                        "tick_idx": t_idx,
                        "time_ms": float(t_idx),
                        "count": 1,
                        "summary": (
                            f"[FW {fe.get('level', 'ERR')} @ Tick #{t_idx} ({fe.get('file', 'fw')}:{fe.get('line', 0)})] "
                            f"{fe.get('msg', '')}"
                        ),
                        "mod": m_obj,
                    }
                )
            if not fw_children:
                fw_children.append(
                    {
                        "v_key": f"{gkey}:ev:0",
                        "type": "FW_LOG_ERROR",
                        "comp_id": m_obj.comp_id,
                        "pipe_id": m_obj.pipeline_id,
                        "core_id": m_obj.core_id,
                        "widget_name": wname,
                        "count": err_cnt,
                        "summary": f"[FW ERR] {last_msg}",
                        "mod": m_obj,
                    }
                )
            violations.append(
                {
                    "v_key": gkey,
                    "type": "FW_MODULE_ERROR",
                    "comp_id": m_obj.comp_id,
                    "pipe_id": m_obj.pipeline_id,
                    "core_id": m_obj.core_id,
                    "widget_name": wname,
                    "count": err_cnt,
                    "summary": f"FW Error ({err_cnt}×): {last_msg}",
                    "children": fw_children,
                    "mod": m_obj,
                }
            )
        return violations

    def _scroll_to_expanded_module(self, comp_id: int) -> None:
        """Scroll the Module Buffer Stats section so that `comp_id` and its expanded violation tree are visible."""
        telem = self.telemetry
        if not telem:
            return
        active_mods = [
            c for c in telem.components
            if c.iterations > 0 or c.avg_cycles > 0 or c.in_pins or c.out_pins
        ]
        active_mods.sort(key=lambda c: (c.core_id, c.pipeline_id, c.comp_id))
        row_h = 30.0
        v_grp_row_h = 21.0
        v_ev_row_h = 19.0
        y_off = 0.0
        for m_obj in active_mods:
            if m_obj.comp_id == comp_id:
                self.v_scroll_offset = max(0.0, y_off)
                break
            y_off += row_h
            if m_obj.comp_id in self._expanded_violation_mods:
                for v_grp in self._collect_module_violations(m_obj):
                    y_off += v_grp_row_h
                    if v_grp["v_key"] not in self._collapsed_violation_groups:
                        y_off += len(v_grp.get("children", [])) * v_ev_row_h

    def _on_click_pressed(
        self,
        _gesture: Gtk.GestureClick,
        n_press: int,
        x: float,
        y: float,
    ) -> None:
        if n_press == 2:
            self.reset_zoom()
            return
        if n_press != 1:
            return

        # Hit-test interactive violation tree nodes in reverse draw order
        for hb in reversed(self._hit_boxes):
            if not (hb["x"] <= x <= hb["x"] + hb["w"] and hb["y"] <= y <= hb["y"] + hb["h"]):
                continue
            kind = hb.get("kind", "")
            if kind == "violation_header_badge":
                viol_mods = hb.get("viol_comp_ids", [])
                exp_x_end = hb.get("expander_x_end", hb["x"] + 22.0)
                all_expanded = bool(viol_mods) and all(cid in self._expanded_violation_mods for cid in viol_mods)
                if x <= exp_x_end:
                    if all_expanded:
                        for cid in viol_mods:
                            self._expanded_violation_mods.discard(cid)
                    else:
                        for cid in viol_mods:
                            self._expanded_violation_mods.add(cid)
                        if viol_mods:
                            self._scroll_to_expanded_module(viol_mods[0])
                    self.queue_draw()
                else:
                    for cid in viol_mods:
                        self._expanded_violation_mods.add(cid)
                    if viol_mods:
                        self._scroll_to_expanded_module(viol_mods[0])
                    self._selected_violation_key = "summary:all_violations"
                    self.queue_draw()
                    if self.on_violation_clicked and hb.get("violation"):
                        self.on_violation_clicked(hb["violation"])
                return

            if kind == "violation_node":
                cid = hb["comp_id"]
                exp_x_end = hb.get("expander_x_end", hb["x"] + 20.0)
                if x <= exp_x_end:
                    if cid in self._expanded_violation_mods:
                        self._expanded_violation_mods.remove(cid)
                    else:
                        self._expanded_violation_mods.add(cid)
                        self._scroll_to_expanded_module(cid)
                    self.queue_draw()
                else:
                    self._expanded_violation_mods.add(cid)
                    self._scroll_to_expanded_module(cid)
                    self._selected_violation_key = hb.get("v_key")
                    self.queue_draw()
                    if self.on_violation_clicked and hb.get("violation"):
                        self.on_violation_clicked(hb["violation"])
                return

            if kind == "violation_tree_group":
                gkey = hb["v_key"]
                exp_x_end = hb.get("expander_x_end", hb["x"] + 34.0)
                if x <= exp_x_end:
                    if gkey in self._collapsed_violation_groups:
                        self._collapsed_violation_groups.remove(gkey)
                    else:
                        self._collapsed_violation_groups.add(gkey)
                    self.queue_draw()
                else:
                    self._selected_violation_key = gkey
                    self.queue_draw()
                    if self.on_violation_clicked and hb.get("violation"):
                        self.on_violation_clicked(hb["violation"])
                return

            if kind == "violation_tree_item":
                self._selected_violation_key = hb.get("v_key")
                self.queue_draw()
                if self.on_violation_clicked and hb.get("violation"):
                    self.on_violation_clicked(hb["violation"])
                return

    def _on_motion(self, _ctrl: Gtk.EventControllerMotion, x: float, y: float) -> None:
        self._hover_x = x
        self._hover_y = y
        self._update_hover_hit(x, y)
        self.queue_draw()

    def _on_leave(self, _ctrl: Gtk.EventControllerMotion) -> None:
        self._hover_x = -1.0
        self._hover_y = -1.0
        self._hovered_comp_id = None
        self._hovered_pipe_id = None
        self._hovered_hit_info = None
        self.queue_draw()

    def _update_hover_hit(self, x: float, y: float) -> None:
        self._hovered_comp_id = None
        self._hovered_pipe_id = None
        self._hovered_hit_info = None
        for hb in reversed(self._hit_boxes):
            if hb["x"] <= x <= hb["x"] + hb["w"] and hb["y"] <= y <= hb["y"] + hb["h"]:
                self._hovered_comp_id = hb.get("comp_id")
                self._hovered_pipe_id = hb.get("pipe_id")
                self._hovered_hit_info = hb
                break

    @staticmethod
    def _rgba(r: float, g: float, b: float, a: float = 1.0) -> Gdk.RGBA:
        c = Gdk.RGBA()
        c.red, c.green, c.blue, c.alpha = r, g, b, a
        return c

    def _draw_text(
        self,
        snapshot: Gtk.Snapshot,
        text: str,
        x: float,
        y: float,
        font_str: str,
        color: Gdk.RGBA,
        max_w: Optional[float] = None,
    ) -> Tuple[int, int]:
        layout = self.create_pango_layout(text)
        layout.set_font_description(Pango.FontDescription.from_string(font_str))
        if max_w is not None and max_w > 8.0:
            layout.set_width(int(max_w * Pango.SCALE))
            layout.set_ellipsize(Pango.EllipsizeMode.END)
        tw, th = layout.get_pixel_size()
        snapshot.save()
        pt = Graphene.Point.alloc()
        pt.init(float(x), float(y))
        snapshot.translate(pt)
        snapshot.append_layout(layout, color)
        snapshot.restore()
        return tw, th

    def _draw_rect_fill_stroke(
        self,
        snapshot: Gtk.Snapshot,
        x: float,
        y: float,
        w: float,
        h: float,
        fill_rgba: Optional[Gdk.RGBA],
        stroke_rgba: Optional[Gdk.RGBA] = None,
        stroke_width: float = 1.0,
    ) -> None:
        if w <= 0.2 or h <= 0.2:
            return
        rect = Graphene.Rect.alloc()
        rect.init(float(x), float(y), float(w), float(h))
        if fill_rgba is not None:
            snapshot.append_color(fill_rgba, rect)
        if stroke_rgba is not None:
            pb = Gsk.PathBuilder.new()
            pb.add_rect(rect)
            snapshot.append_stroke(pb.to_path(), Gsk.Stroke.new(stroke_width), stroke_rgba)

    def _pipe_color(self, pipe_id: int) -> Tuple[float, float, float]:
        idx = max(0, pipe_id - 1) % len(self.PIPE_PALETTE)
        return self.PIPE_PALETTE[idx]

    def _mod_color(self, comp_id: int, pipe_id: int) -> Tuple[float, float, float]:
        idx = (comp_id + max(0, pipe_id - 1) * 3) % len(self.MOD_PALETTE)
        return self.MOD_PALETTE[idx]

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        w = float(self.get_width()) or 920.0
        h = float(self.get_height()) or 240.0
        if w <= 10 or h <= 10:
            w, h = 920.0, 240.0

        self._hit_boxes = []
        self._spark_hit_boxes = []

        # Overall dark background
        bg_rect = Graphene.Rect.alloc()
        bg_rect.init(0.0, 0.0, w, h)
        snapshot.append_color(self._rgba(0.07, 0.08, 0.11, 1.0), bg_rect)

        telem = self.telemetry
        has_sched_data = bool(
            telem
            and (
                telem.total_ll_ticks > 0
                or any(c.iterations > 0 or c.in_pins or c.out_pins for c in telem.components)
                or telem.pipelines
            )
        )
        if not has_sched_data:
            self._draw_text(
                snapshot,
                "No scheduling or buffer telemetry captured yet — click [▶ Run in Testbench] to profile Core Schedule & Module Buffers",
                16.0,
                h * 0.45,
                "Sans Italic 9.5",
                self._rgba(0.62, 0.68, 0.78, 0.85),
                max_w=w - 32.0,
            )
            return

        assert telem is not None

        # Layout split between:
        # 1) Top: Core ID (Y-axis) vs. Time (X-axis) Schedule Timeline
        # 2) Bottom: Module Buffer Stats & Pin Telemetry Inspector
        top_header_h = 20.0
        time_axis_h = 18.0
        sched_fraction = 0.54 if h >= 220.0 else 0.50
        sched_total_h = max(108.0, min(h - 72.0, round(h * sched_fraction)))
        buf_section_y = sched_total_h + 2.0
        buf_section_h = max(60.0, h - buf_section_y)
        self._last_buf_area_y = buf_section_y

        gutter_w = 156.0
        right_pad = 10.0
        plot_x = gutter_w
        plot_w = max(80.0, w - gutter_w - right_pad)
        self._last_plot_w = plot_w

        # ------------------------------------------------------------------
        # 1. Top Section Header & Legend
        # ------------------------------------------------------------------
        self._draw_rect_fill_stroke(
            snapshot,
            0.0,
            0.0,
            w,
            top_header_h,
            self._rgba(0.10, 0.12, 0.17, 1.0),
            self._rgba(0.22, 0.26, 0.34, 0.9),
            1.0,
        )
        active_pipes = sorted(telem.pipelines, key=lambda p: p.pipeline_id)
        active_mods = [
            c for c in telem.components if c.iterations > 0 or c.avg_cycles > 0 or c.in_pins or c.out_pins
        ]
        if not active_mods:
            active_mods = list(telem.components)

        total_ticks = max(
            1,
            telem.total_ll_ticks,
            max((c.iterations for c in active_mods), default=1),
        )
        period_us = max((p.period_us for p in active_pipes if p.period_us > 0), default=1000)
        period_ms = max(0.25, float(period_us) / 1000.0)
        total_ms = self._get_total_duration_ms()
        vis_ms = self._get_visible_window_ms()
        self._clamp_pan()
        t_start_ms = self.pan_offset_ms
        t_end_ms = t_start_ms + vis_ms

        hdr_text = (
            f"DSP CORE SCHEDULE TIMELINE (Y: Core ID  ×  X: Time)  —  "
            f"{total_ticks} LL Ticks ({period_us} µs/tick)  |  "
            f"{len(active_pipes)} Pipelines  |  {len(active_mods)} Active Modules"
        )
        self._draw_text(
            snapshot,
            hdr_text,
            8.0,
            3.0,
            "Monospace Bold 8",
            self._rgba(0.84, 0.91, 0.98, 1.0),
            max_w=w * 0.62,
        )

        # Draw Pipeline color legend pills in top right of header
        leg_x = w - right_pad
        for p in reversed(active_pipes):
            pr, pg, pb_col = self._pipe_color(p.pipeline_id)
            p_lbl = f"Pipe {p.pipeline_id} (C{p.core_id}•{p.exec_avg_us:.1f}µs)"
            layout = self.create_pango_layout(p_lbl)
            layout.set_font_description(Pango.FontDescription.from_string("Monospace Bold 7.5"))
            tw, th = layout.get_pixel_size()
            pill_w = float(tw + 14)
            leg_x -= pill_w + 5.0
            if leg_x < w * 0.55:
                break
            self._draw_rect_fill_stroke(
                snapshot,
                leg_x,
                2.5,
                pill_w,
                top_header_h - 5.0,
                self._rgba(pr, pg, pb_col, 0.25),
                self._rgba(pr, pg, pb_col, 0.85),
                1.0,
            )
            self._draw_text(
                snapshot,
                p_lbl,
                leg_x + 6.0,
                3.5,
                "Monospace Bold 7.5",
                self._rgba(0.95, 0.97, 1.0, 1.0),
            )

        # ------------------------------------------------------------------
        # 2. Core Swimlanes (Y-Axis = Core 0..3) & Time Grid (X-Axis = ms)
        # ------------------------------------------------------------------
        lanes_top_y = top_header_h
        lanes_bot_y = sched_total_h - time_axis_h
        lanes_h = max(60.0, lanes_bot_y - lanes_top_y)

        cores_by_id: Dict[int, CoreTelemetry] = {c.core_id: c for c in telem.cores}
        num_cores = 4
        lane_h = lanes_h / float(num_cores)

        # Group pipelines and modules by core_id
        pipes_by_core: Dict[int, List[PipelineSchedTelemetry]] = {cid: [] for cid in range(num_cores)}
        for p in active_pipes:
            cid = p.core_id if 0 <= p.core_id < num_cores else 0
            pipes_by_core[cid].append(p)

        mods_by_core: Dict[int, List[ComponentPerfItem]] = {cid: [] for cid in range(num_cores)}
        for m in active_mods:
            cid = m.core_id if 0 <= m.core_id < num_cores else 0
            mods_by_core[cid].append(m)
        for cid in range(num_cores):
            mods_by_core[cid].sort(key=lambda c: (c.pipeline_id, c.comp_id))

        # Helper to map time (ms) -> X pixel
        def time_to_x(t_ms: float) -> float:
            return plot_x + ((t_ms - t_start_ms) / max(1e-6, vis_ms)) * plot_w

        # Determine visible LL tick range
        first_tick = max(0, int(math.floor(t_start_ms / period_ms)))
        last_tick = min(total_ticks - 1, int(math.ceil(t_end_ms / period_ms)))

        # Draw each Core swimlane background & Y-axis header
        for cid in range(num_cores):
            ly = lanes_top_y + cid * lane_h
            c_info = cores_by_id.get(cid)
            has_work = bool(
                (c_info and (c_info.ll_ticks > 0 or c_info.dp_runs > 0 or c_info.avg_cycles > 0))
                or pipes_by_core[cid]
                or mods_by_core[cid]
            )
            is_enabled = bool(c_info.enabled) if c_info else (cid == 0 or has_work)

            bg_col = (
                self._rgba(0.09, 0.11, 0.16, 1.0)
                if (cid % 2 == 0)
                else self._rgba(0.075, 0.09, 0.135, 1.0)
            )
            self._draw_rect_fill_stroke(
                snapshot,
                0.0,
                ly,
                w,
                lane_h,
                bg_col,
                self._rgba(0.18, 0.22, 0.30, 0.85),
                1.0,
            )

            # Left Y-Axis Core ID Gutter
            gutter_bg = (
                self._rgba(0.12, 0.16, 0.23, 1.0)
                if has_work
                else (
                    self._rgba(0.10, 0.12, 0.17, 1.0)
                    if is_enabled
                    else self._rgba(0.08, 0.09, 0.12, 1.0)
                )
            )
            self._draw_rect_fill_stroke(
                snapshot,
                0.0,
                ly,
                gutter_w,
                lane_h,
                gutter_bg,
                self._rgba(0.24, 0.29, 0.38, 0.9),
                1.0,
            )

            freq_mhz = (c_info.freq_hz / 1e6) if (c_info and c_info.freq_hz > 0) else 393.2
            util_pct = c_info.util_pct if c_info else 0.0
            core_title = f"Core {cid}"
            status_tag = f"{freq_mhz:.0f}MHz • {util_pct:.2f}%" if is_enabled else "OFFLINE"
            title_col = (
                self._rgba(0.45, 0.88, 1.0, 1.0)
                if has_work
                else (
                    self._rgba(0.75, 0.82, 0.90, 0.9)
                    if is_enabled
                    else self._rgba(0.48, 0.52, 0.60, 0.75)
                )
            )
            self._draw_text(
                snapshot,
                f"{core_title}  [{status_tag}]",
                6.0,
                ly + 2.0,
                "Monospace Bold 8",
                title_col,
                max_w=gutter_w - 10.0,
            )

            if c_info and is_enabled and lane_h >= 24.0:
                sub_line = (
                    f"LL:{c_info.ll_exec_avg_us:.1f}/{c_info.ll_exec_peak_us:.1f}µs "
                    f"({c_info.avg_kcps}kcps)"
                )
                if c_info.dp_runs > 0:
                    sub_line += f" DP:{c_info.dp_runs}"
                if c_info.overruns > 0:
                    sub_line += f" XRUN:{c_info.overruns}"
                self._draw_text(
                    snapshot,
                    sub_line,
                    6.0,
                    ly + min(lane_h - 11.0, 13.5),
                    "Monospace 7",
                    self._rgba(0.72, 0.79, 0.88, 0.9),
                    max_w=gutter_w - 10.0,
                )

                # Mini core utilization bar at bottom of gutter cell
                if lane_h >= 30.0:
                    bar_y = ly + lane_h - 5.5
                    bar_w = gutter_w - 12.0
                    self._draw_rect_fill_stroke(
                        snapshot,
                        6.0,
                        bar_y,
                        bar_w,
                        3.5,
                        self._rgba(0.05, 0.06, 0.09, 0.9),
                        None,
                    )
                    fill_ratio = min(1.0, max(0.03 if has_work else 0.0, util_pct / 25.0))
                    u_col = (
                        self._rgba(0.95, 0.35, 0.35, 0.95)
                        if util_pct > 80.0
                        else (
                            self._rgba(0.95, 0.75, 0.25, 0.95)
                            if util_pct > 40.0
                            else self._rgba(0.28, 0.82, 0.58, 0.95)
                        )
                    )
                    self._draw_rect_fill_stroke(
                        snapshot,
                        6.0,
                        bar_y,
                        bar_w * fill_ratio,
                        3.5,
                        u_col,
                        None,
                    )

        # Clip timeline drawing to the plot area
        clip_rect = Graphene.Rect.alloc()
        clip_rect.init(plot_x, lanes_top_y, plot_w, lanes_h + time_axis_h)
        snapshot.push_clip(clip_rect)

        # Draw vertical LL tick grid lines & sub-tick grid lines across all Core lanes
        tick_px_w = (period_ms / max(1e-6, vis_ms)) * plot_w
        tick_step = max(1, int(math.ceil(42.0 / max(1.0, tick_px_w))))

        for tick_idx in range(first_tick, last_tick + 2):
            t_ms = tick_idx * period_ms
            tx = time_to_x(t_ms)
            if plot_x - 2.0 <= tx <= plot_x + plot_w + 2.0:
                is_major = (tick_idx % tick_step) == 0
                pb = Gsk.PathBuilder.new()
                pb.move_to(tx, lanes_top_y)
                pb.line_to(tx, lanes_bot_y)
                snapshot.append_stroke(
                    pb.to_path(),
                    Gsk.Stroke.new(1.0 if is_major else 0.6),
                    self._rgba(0.26, 0.32, 0.44, 0.55 if is_major else 0.25),
                )

                # Sub-tick 0.25ms / 0.5ms grid lines when zoomed in
                if tick_px_w >= 110.0:
                    for sub_i in (1, 2, 3):
                        sub_x = tx + (sub_i * 0.25) * tick_px_w
                        if plot_x <= sub_x <= plot_x + plot_w:
                            spb = Gsk.PathBuilder.new()
                            spb.move_to(sub_x, lanes_top_y)
                            spb.line_to(sub_x, lanes_bot_y)
                            snapshot.append_stroke(
                                spb.to_path(),
                                Gsk.Stroke.new(0.5),
                                self._rgba(0.20, 0.25, 0.35, 0.22),
                            )

        # Render Pipeline & Module execution blocks inside each Core lane along the Time X-axis
        for cid in range(num_cores):
            core_mods = mods_by_core[cid]
            core_pipes = pipes_by_core[cid]
            c_info = cores_by_id.get(cid)
            if not core_mods and not core_pipes:
                continue

            ly = lanes_top_y + cid * lane_h
            ll_mods = [m for m in core_mods if m.domain.upper() != "DP"]
            dp_mods = [m for m in core_mods if m.domain.upper() == "DP"]

            has_dp = bool(dp_mods or (c_info and c_info.dp_runs > 0))
            pipe_band_h = min(13.0, max(9.0, lane_h * 0.28))
            mod_band_y = ly + pipe_band_h + 2.0
            mod_band_h = max(
                10.0,
                (lane_h - pipe_band_h - (13.0 if has_dp else 3.0)),
            )
            dp_band_y = mod_band_y + mod_band_h + 1.5
            dp_band_h = max(8.0, ly + lane_h - dp_band_y - 1.5) if has_dp else 0.0

            # Compute total LL execution time on this core per tick (in us)
            total_mod_exec_us = sum(
                max(0.05, m.exec_avg_us if m.exec_avg_us > 0 else (m.avg_cycles / 393.216))
                for m in ll_mods
            )
            core_ll_us = (
                c_info.ll_exec_avg_us
                if (c_info and c_info.ll_exec_avg_us > 0)
                else max(total_mod_exec_us, sum(p.exec_avg_us for p in core_pipes))
            )
            core_ll_us = max(core_ll_us, total_mod_exec_us, 0.2)

            # In Expanded Tick View, scale the active LL execution window to occupy 76% of the tick slot
            # (or proportional to utilization) so module sequence & relative duration are clearly visible.
            # In True 1:1 µs View, use exact (exec_us / period_us) width.
            for tick_idx in range(first_tick, last_tick + 1):
                tick_start_ms = tick_idx * period_ms
                tick_x = time_to_x(tick_start_ms)
                if tick_x + tick_px_w < plot_x or tick_x > plot_x + plot_w:
                    continue

                if self.expanded_tick_mode:
                    # Keep proportional differences between cores while ensuring readability
                    busy_frac = min(0.84, max(0.58, (core_ll_us / float(period_us)) * 18.0))
                    active_span_px = tick_px_w * busy_frac
                else:
                    true_frac = min(0.98, max(0.004, core_ll_us / float(period_us)))
                    active_span_px = tick_px_w * true_frac

                # 2a. Draw Pipeline Execution Spans in the top band of Core `cid`
                pipes_for_tick = [p for p in core_pipes if tick_idx < max(1, p.copies)]
                if not pipes_for_tick and ll_mods:
                    # Synthesize pipeline grouping from modules if pipeline list is empty
                    seen_pids: List[int] = []
                    for m in ll_mods:
                        if m.pipeline_id not in seen_pids:
                            seen_pids.append(m.pipeline_id)
                    pipes_for_tick = [
                        PipelineSchedTelemetry(
                            pipeline_id=pid,
                            core_id=cid,
                            period_us=period_us,
                            copy_iterations=total_ticks,
                            avg_exec_us=sum(
                                max(0.05, m.exec_avg_us) for m in ll_mods if m.pipeline_id == pid
                            ),
                        )
                        for pid in seen_pids
                    ]

                total_pipe_us = sum(max(0.05, p.exec_avg_us) for p in pipes_for_tick) or 1.0
                p_cum_frac = 0.0
                for p_obj in pipes_for_tick:
                    p_frac = max(0.05, p_obj.exec_avg_us) / total_pipe_us
                    cur_px = tick_x + 1.0 + p_cum_frac * active_span_px
                    next_px = tick_x + 1.0 + (p_cum_frac + p_frac) * active_span_px
                    pw = max(2.0, (next_px - cur_px) - 1.0)
                    p_cum_frac += p_frac
                    pr, pg, pb_col = self._pipe_color(p_obj.pipeline_id)
                    is_p_hov = (
                        self._hovered_pipe_id == p_obj.pipeline_id and self._hovered_comp_id is None
                    )
                    self._draw_rect_fill_stroke(
                        snapshot,
                        cur_px,
                        ly + 1.5,
                        pw,
                        pipe_band_h,
                        self._rgba(pr, pg, pb_col, 0.48 if is_p_hov else 0.32),
                        self._rgba(pr, pg, pb_col, 1.0 if is_p_hov else 0.80),
                        1.2 if is_p_hov else 0.9,
                    )
                    if pw >= 26.0:
                        p_txt = (
                            f"Pipe {p_obj.pipeline_id} ({p_obj.exec_avg_us:.2f}µs)"
                            if pw >= 68.0
                            else f"P{p_obj.pipeline_id}"
                        )
                        self._draw_text(
                            snapshot,
                            p_txt,
                            cur_px + 3.0,
                            ly + 1.5,
                            "Monospace Bold 7",
                            self._rgba(0.96, 0.98, 1.0, 0.98),
                            max_w=pw - 4.0,
                        )
                    self._hit_boxes.append(
                        {
                            "kind": "pipeline",
                            "pipe_id": p_obj.pipeline_id,
                            "core_id": cid,
                            "tick_idx": tick_idx,
                            "time_ms": tick_start_ms,
                            "pipe": p_obj,
                            "x": cur_px,
                            "y": ly + 1.5,
                            "w": pw,
                            "h": pipe_band_h,
                        }
                    )

                # 2b. Draw sequential Module Execution Blocks inside Core `cid`
                mods_in_tick = [m for m in ll_mods if tick_idx < max(1, m.iterations)]
                tot_m_us = (
                    sum(
                        max(0.05, m.exec_avg_us if m.exec_avg_us > 0 else (m.avg_cycles / 393.216))
                        for m in mods_in_tick
                    )
                    or 1.0
                )
                cum_us = 0.0
                m_cum_frac = 0.0
                for m_obj in mods_in_tick:
                    m_us = max(
                        0.05,
                        m_obj.exec_avg_us if m_obj.exec_avg_us > 0 else (m_obj.avg_cycles / 393.216),
                    )
                    m_frac = m_us / tot_m_us
                    mod_x = tick_x + 1.0 + m_cum_frac * active_span_px
                    next_mod_x = tick_x + 1.0 + (m_cum_frac + m_frac) * active_span_px
                    mw = max(2.0, (next_mod_x - mod_x) - 1.0)
                    m_cum_frac += m_frac
                    mr, mg, mb_col = self._mod_color(m_obj.comp_id, m_obj.pipeline_id)
                    is_hov = self._hovered_comp_id == m_obj.comp_id
                    has_mod_v = m_obj.cpc_violations > 0 or (
                        m_obj.effective_cpc > 0 and m_obj.peak_cycles > m_obj.effective_cpc
                    )
                    is_focused_v_slice = bool(
                        self._focused_violation_slice
                        and self._focused_violation_slice.get("comp_id") == m_obj.comp_id
                        and int(self._focused_violation_slice.get("tick_idx", 0) or 0) == tick_idx
                    )

                    if is_focused_v_slice:
                        # Vertical crimson cursor marker across all core lanes at the focused violation
                        vcx = mod_x + mw * 0.5
                        vpb = Gsk.PathBuilder.new()
                        vpb.move_to(vcx, lanes_top_y)
                        vpb.line_to(vcx, lanes_bot_y)
                        vstroke = Gsk.Stroke.new(1.6)
                        vstroke.set_dash([4.0, 2.5])
                        snapshot.append_stroke(
                            vpb.to_path(),
                            vstroke,
                            self._rgba(1.0, 0.25, 0.28, 0.96),
                        )
                        # Outer glowing focus frame around the violating timeline slice
                        self._draw_rect_fill_stroke(
                            snapshot,
                            mod_x - 2.0,
                            mod_band_y - 2.0,
                            mw + 4.0,
                            mod_band_h + 4.0,
                            self._rgba(0.96, 0.14, 0.20, 0.36),
                            self._rgba(1.0, 0.85, 0.85, 1.0),
                            2.0,
                        )

                    self._draw_rect_fill_stroke(
                        snapshot,
                        mod_x,
                        mod_band_y,
                        mw,
                        mod_band_h,
                        self._rgba(mr, mg, mb_col, 0.88 if (is_hov or is_focused_v_slice) else 0.56),
                        self._rgba(1.0, 0.28, 0.30, 1.0)
                        if (has_mod_v or is_focused_v_slice)
                        else self._rgba(
                            1.0 if is_hov else mr * 1.15,
                            0.92 if is_hov else mg * 1.15,
                            0.42 if is_hov else mb_col * 1.15,
                            1.0 if is_hov else 0.9,
                        ),
                        1.8 if (has_mod_v or is_hov or is_focused_v_slice) else 1.0,
                    )

                    # Mini buffer fill indicator bar along the bottom edge of the module block
                    if mod_band_h >= 14.0 and mw >= 8.0 and (m_obj.in_pins or m_obj.out_pins):
                        in_fill = 0.0
                        if m_obj.in_pins and m_obj.in_pins[0].buf_size > 0:
                            in_fill = min(
                                1.0,
                                m_obj.in_pins[0].ll_entry_avail / float(m_obj.in_pins[0].buf_size),
                            )
                        out_fill = 0.0
                        if m_obj.out_pins and m_obj.out_pins[0].buf_size > 0:
                            out_fill = min(
                                1.0,
                                m_obj.out_pins[0].ll_exit_avail / float(m_obj.out_pins[0].buf_size),
                            )
                        bf_h = 3.0
                        bf_y = mod_band_y + mod_band_h - bf_h - 0.5
                        half_w = max(2.0, (mw - 2.0) * 0.5)
                        self._draw_rect_fill_stroke(
                            snapshot,
                            mod_x + 1.0,
                            bf_y,
                            half_w * in_fill,
                            bf_h,
                            self._rgba(0.32, 0.92, 0.98, 0.95),
                            None,
                        )
                        self._draw_rect_fill_stroke(
                            snapshot,
                            mod_x + 1.0 + half_w,
                            bf_y,
                            half_w * out_fill,
                            bf_h,
                            self._rgba(0.42, 0.98, 0.56, 0.95),
                            None,
                        )

                    if mw >= 22.0:
                        short_name = f"! {m_obj.widget_name}" if has_mod_v else m_obj.widget_name
                        lbl = (
                            f"{short_name} ({m_us:.2f}µs)"
                            if mw >= 78.0
                            else short_name
                        )
                        self._draw_text(
                            snapshot,
                            lbl,
                            mod_x + 2.5,
                            mod_band_y + 1.0,
                            "Monospace Bold 7",
                            self._rgba(1.0, 0.85, 0.85, 1.0)
                            if has_mod_v
                            else self._rgba(0.98, 0.99, 1.0, 1.0),
                            max_w=mw - 4.0,
                        )

                    if is_focused_v_slice:
                        banner_txt = (
                            f"! VIOLATION @ T{tick_idx}: {m_obj.widget_name} "
                            f"({m_obj.peak_cycles}>{m_obj.effective_cpc} cyc)"
                        )
                        b_lyt = self.create_pango_layout(banner_txt)
                        b_lyt.set_font_description(Pango.FontDescription.from_string("Monospace Bold 7"))
                        btw, bth = b_lyt.get_pixel_size()
                        bw_box = float(btw + 10)
                        bh_box = float(bth + 4)
                        bx_box = max(plot_x + 4.0, min(plot_x + plot_w - bw_box - 4.0, mod_x + mw * 0.5 - bw_box * 0.5))
                        by_box = max(lanes_top_y + 1.0, mod_band_y - bh_box - 2.0)
                        self._draw_rect_fill_stroke(
                            snapshot,
                            bx_box,
                            by_box,
                            bw_box,
                            bh_box,
                            self._rgba(0.72, 0.10, 0.15, 0.96),
                            self._rgba(1.0, 0.65, 0.65, 1.0),
                            1.1,
                        )
                        self._draw_text(
                            snapshot,
                            banner_txt,
                            bx_box + 5.0,
                            by_box + 1.5,
                            "Monospace Bold 7",
                            self._rgba(1.0, 0.96, 0.96, 1.0),
                        )

                    self._hit_boxes.append(
                        {
                            "kind": "module_slice",
                            "comp_id": m_obj.comp_id,
                            "pipe_id": m_obj.pipeline_id,
                            "core_id": cid,
                            "tick_idx": tick_idx,
                            "start_us": tick_idx * period_us + cum_us,
                            "dur_us": m_us,
                            "mod": m_obj,
                            "x": mod_x,
                            "y": mod_band_y,
                            "w": mw,
                            "h": mod_band_h,
                        }
                    )
                    cum_us += m_us

            # 2c. Draw DP Thread Execution Spans & LL-Sync markers if Core has DP modules
            if has_dp and dp_band_h >= 6.0:
                for dm in dp_mods:
                    dp_iters = max(1, dm.iterations)
                    ticks_per_dp = max(1, total_ticks // dp_iters)
                    mr, mg, mb_col = self._mod_color(dm.comp_id, dm.pipeline_id)
                    is_hov = self._hovered_comp_id == dm.comp_id
                    for dp_idx in range(dp_iters):
                        dp_t0_ms = dp_idx * ticks_per_dp * period_ms
                        dp_span_ms = ticks_per_dp * period_ms * 0.85
                        dx0 = time_to_x(dp_t0_ms)
                        dx1 = time_to_x(dp_t0_ms + dp_span_ms)
                        dw = max(6.0, dx1 - dx0)
                        if dx0 + dw < plot_x or dx0 > plot_x + plot_w:
                            continue
                        self._draw_rect_fill_stroke(
                            snapshot,
                            dx0,
                            dp_band_y,
                            dw,
                            dp_band_h,
                            self._rgba(0.92, 0.58, 0.18, 0.78 if is_hov else 0.52),
                            self._rgba(1.0, 0.78, 0.32, 0.95),
                            1.0,
                        )
                        if dw >= 32.0:
                            self._draw_text(
                                snapshot,
                                f"[DP] {dm.widget_name} ({dm.exec_avg_us:.1f}µs)",
                                dx0 + 3.0,
                                dp_band_y,
                                "Monospace Bold 6.5",
                                self._rgba(1.0, 0.96, 0.88, 1.0),
                                max_w=dw - 5.0,
                            )
                        self._hit_boxes.append(
                            {
                                "kind": "module_slice",
                                "comp_id": dm.comp_id,
                                "pipe_id": dm.pipeline_id,
                                "core_id": cid,
                                "tick_idx": dp_idx * ticks_per_dp,
                                "start_us": dp_t0_ms * 1000.0,
                                "dur_us": dm.exec_avg_us,
                                "mod": dm,
                                "x": dx0,
                                "y": dp_band_y,
                                "w": dw,
                                "h": dp_band_h,
                            }
                        )

        # ------------------------------------------------------------------
        # 3. X-Axis Time Ruler (bottom of Core Schedule Timeline)
        # ------------------------------------------------------------------
        self._draw_rect_fill_stroke(
            snapshot,
            0.0,
            lanes_bot_y,
            w,
            time_axis_h,
            self._rgba(0.10, 0.12, 0.17, 1.0),
            self._rgba(0.24, 0.28, 0.38, 0.9),
            1.0,
        )
        self._draw_text(
            snapshot,
            "Time (ms / LL Tick) ▸",
            6.0,
            lanes_bot_y + 2.5,
            "Monospace Bold 7.5",
            self._rgba(0.75, 0.82, 0.92, 0.95),
            max_w=gutter_w - 8.0,
        )

        for tick_idx in range(first_tick, last_tick + 2, tick_step):
            t_ms = tick_idx * period_ms
            tx = time_to_x(t_ms)
            if plot_x <= tx <= plot_x + plot_w - 28.0:
                lbl = f"{t_ms:.1f}ms (T{tick_idx})" if tick_px_w >= 55.0 else f"{t_ms:.1f}ms"
                self._draw_text(
                    snapshot,
                    lbl,
                    tx + 2.0,
                    lanes_bot_y + 2.5,
                    "Monospace 7.5",
                    self._rgba(0.82, 0.88, 0.96, 0.95),
                )

        snapshot.pop()

        # ------------------------------------------------------------------
        # 4. Bottom Section: Module Buffer Stats & Pin Fill Inspector
        # ------------------------------------------------------------------
        buf_hdr_h = 19.0
        self._draw_rect_fill_stroke(
            snapshot,
            0.0,
            buf_section_y,
            w,
            buf_hdr_h,
            self._rgba(0.11, 0.14, 0.20, 1.0),
            self._rgba(0.25, 0.30, 0.40, 0.95),
            1.0,
        )

        total_xfer_bytes = sum(
            sum(p.total_bytes_transferred for p in m.in_pins)
            + sum(p.total_bytes_transferred for p in m.out_pins)
            for m in active_mods
        )
        mod_violations_map: Dict[int, List[Dict[str, Any]]] = {
            m.comp_id: self._collect_module_violations(m) for m in active_mods
        }
        viol_comp_ids = [m.comp_id for m in active_mods if mod_violations_map.get(m.comp_id)]
        cpc_violations = sum(m.cpc_violations for m in active_mods)
        total_violation_nodes = sum(len(vlist) for vlist in mod_violations_map.values())

        hdr_badge_w = 235.0 if (cpc_violations > 0 or total_violation_nodes > 0) else 0.0
        self._draw_text(
            snapshot,
            f"MODULE BUFFER STATS & PIN TELEMETRY  —  {len(active_mods)} Modules  |  "
            f"Total Pin Xfer: {total_xfer_bytes:,} B  |  CPC Violations: {cpc_violations}",
            8.0,
            buf_section_y + 2.5,
            "Monospace Bold 8",
            self._rgba(0.84, 0.92, 0.98, 1.0),
            max_w=max(120.0, w - hdr_badge_w - 20.0),
        )

        if viol_comp_ids:
            all_exp = all(cid in self._expanded_violation_mods for cid in viol_comp_ids)
            hdr_arrow = "▾" if all_exp else "▸"
            bx_hdr = max(240.0, w - hdr_badge_w - 6.0)
            by_hdr = buf_section_y + 1.5
            bh_hdr = buf_hdr_h - 3.0
            self._draw_rect_fill_stroke(
                snapshot,
                bx_hdr,
                by_hdr,
                hdr_badge_w,
                bh_hdr,
                self._rgba(0.42, 0.10, 0.14, 0.95),
                self._rgba(0.98, 0.42, 0.42, 0.95),
                1.0,
            )
            first_v = mod_violations_map[viol_comp_ids[0]][0]
            self._draw_text(
                snapshot,
                f"{hdr_arrow} ⚠ Violations ({cpc_violations or total_violation_nodes}) • Click → Log",
                bx_hdr + 5.0,
                by_hdr + 1.0,
                "Monospace Bold 7.5",
                self._rgba(1.0, 0.72, 0.72, 1.0),
                max_w=hdr_badge_w - 8.0,
            )
            self._hit_boxes.append(
                {
                    "kind": "violation_header_badge",
                    "viol_comp_ids": viol_comp_ids,
                    "violation": {
                        **first_v,
                        "type": "ALL_VIOLATIONS",
                        "v_key": "summary:all_violations",
                        "summary": f"All Scheduling & Pin Buffer Violations ({cpc_violations} CPC violations across {len(viol_comp_ids)} modules)",
                    },
                    "x": bx_hdr,
                    "y": by_hdr,
                    "w": hdr_badge_w,
                    "h": bh_hdr,
                    "expander_x_end": bx_hdr + 22.0,
                }
            )

        rows_top_y = buf_section_y + buf_hdr_h
        rows_viewport_h = max(30.0, h - rows_top_y)
        row_h = 30.0
        v_grp_row_h = 21.0
        v_ev_row_h = 19.0

        # Calculate total scrollable content height including expanded violation tree rows
        total_rows_h = 4.0
        for m_obj in active_mods:
            total_rows_h += row_h
            m_vlist = mod_violations_map.get(m_obj.comp_id, [])
            if m_vlist and m_obj.comp_id in self._expanded_violation_mods:
                for v_grp in m_vlist:
                    total_rows_h += v_grp_row_h
                    if v_grp["v_key"] not in self._collapsed_violation_groups:
                        total_rows_h += len(v_grp.get("children", [])) * v_ev_row_h
        content_h = max(rows_viewport_h, total_rows_h)
        self._sync_v_adjustment(content_h, rows_viewport_h)

        rows_clip = Graphene.Rect.alloc()
        rows_clip.init(0.0, rows_top_y, w, rows_viewport_h)
        snapshot.push_clip(rows_clip)

        col_mod_w = min(230.0, max(165.0, w * 0.24))
        col_exec_w = min(185.0, max(135.0, w * 0.19))
        rem_w = max(160.0, w - col_mod_w - col_exec_w - 12.0)
        col_in_w = rem_w * 0.50
        col_out_w = rem_w * 0.50

        cur_row_y = rows_top_y - self.v_scroll_offset
        for idx, m_obj in enumerate(active_mods):
            ry = cur_row_y
            cur_row_y += row_h
            m_vlist = mod_violations_map.get(m_obj.comp_id, [])
            is_mod_expanded = bool(m_vlist) and (m_obj.comp_id in self._expanded_violation_mods)

            if ry + row_h >= rows_top_y and ry <= rows_top_y + rows_viewport_h:
                is_hov = self._hovered_comp_id == m_obj.comp_id
                row_bg = (
                    self._rgba(0.16, 0.22, 0.32, 0.95)
                    if is_hov
                    else (
                        self._rgba(0.085, 0.10, 0.145, 0.95)
                        if (idx % 2 == 0)
                        else self._rgba(0.075, 0.09, 0.13, 0.95)
                    )
                )
                self._draw_rect_fill_stroke(
                    snapshot,
                    0.0,
                    ry,
                    w,
                    row_h,
                    row_bg,
                    self._rgba(0.20, 0.25, 0.34, 0.75),
                    0.8,
                )

                # Color stripe on left matching module's timeline color (or red accent if violation)
                mr, mg, mb_col = self._mod_color(m_obj.comp_id, m_obj.pipeline_id)
                self._draw_rect_fill_stroke(
                    snapshot,
                    0.0,
                    ry,
                    4.5,
                    row_h,
                    self._rgba(mr, mg, mb_col, 0.95),
                    None,
                )

                # Column 1: Module Name, Pipe, Core, Domain, Memory
                tree_prefix = ("▾ " if is_mod_expanded else "▸ ") if m_vlist else ""
                m_title = f"{tree_prefix}[P{m_obj.pipeline_id}•C{m_obj.core_id}•{m_obj.domain}] {m_obj.widget_name}"
                self._draw_text(
                    snapshot,
                    m_title,
                    8.0,
                    ry + 2.0,
                    "Monospace Bold 8",
                    self._rgba(1.0, 0.80, 0.80, 1.0) if m_vlist else self._rgba(0.94, 0.97, 1.0, 1.0),
                    max_w=col_mod_w - 12.0,
                )
                m_sub = (
                    f"id=0x{m_obj.comp_id:x} {m_obj.mem_type} iters={m_obj.iterations} "
                    f"ibs={m_obj.ibs}B obs={m_obj.obs}B"
                )
                self._draw_text(
                    snapshot,
                    m_sub,
                    8.0,
                    ry + 15.0,
                    "Monospace 7",
                    self._rgba(0.68, 0.76, 0.86, 0.92),
                    max_w=col_mod_w - 12.0,
                )

                # Column 2: Execution Time, Cycles, KCPS, CPC & Expandable Red Violation Tree Badge
                ex_x = col_mod_w + 4.0
                m_us = m_obj.exec_avg_us if m_obj.exec_avg_us > 0 else (m_obj.avg_cycles / 393.216)
                m_pk_us = m_obj.exec_peak_us if m_obj.exec_peak_us > 0 else (m_obj.peak_cycles / 393.216)
                self._draw_text(
                    snapshot,
                    f"exec: {m_us:.2f}/{m_pk_us:.2f} µs ({m_obj.avg_kcps} kcps)",
                    ex_x,
                    ry + 2.0,
                    "Monospace Bold 7.5",
                    self._rgba(0.86, 0.92, 0.98, 0.95),
                    max_w=col_exec_w - 6.0,
                )

                if m_vlist:
                    exp_arrow = "▾" if is_mod_expanded else "▸"
                    pill_x = ex_x - 2.0
                    pill_y = ry + 14.0
                    pill_w = col_exec_w - 4.0
                    pill_h = 14.5
                    is_v_sel = self._selected_violation_key == m_vlist[0]["v_key"]
                    self._draw_rect_fill_stroke(
                        snapshot,
                        pill_x,
                        pill_y,
                        pill_w,
                        pill_h,
                        self._rgba(0.48, 0.12, 0.16, 0.96)
                        if is_v_sel
                        else self._rgba(0.36, 0.09, 0.13, 0.92),
                        self._rgba(1.0, 0.45, 0.45, 0.98),
                        1.0,
                    )
                    self._draw_text(
                        snapshot,
                        f"{exp_arrow} ⚠ cyc:{m_obj.avg_cycles}/{m_obj.peak_cycles}|CPC:{m_obj.effective_cpc}(v={m_obj.cpc_violations})",
                        ex_x + 2.0,
                        ry + 14.5,
                        "Monospace Bold 7",
                        self._rgba(1.0, 0.68, 0.68, 1.0),
                        max_w=pill_w - 6.0,
                    )
                else:
                    self._draw_text(
                        snapshot,
                        f"cyc: {m_obj.avg_cycles}/{m_obj.peak_cycles} | CPC:{m_obj.effective_cpc}(v={m_obj.cpc_violations})",
                        ex_x,
                        ry + 15.0,
                        "Monospace 7",
                        self._rgba(0.66, 0.78, 0.88, 0.9),
                        max_w=col_exec_w - 6.0,
                    )

                # Column 3: Input Pin Buffer Stats (IN[0..])
                in_x = col_mod_w + col_exec_w + 6.0
                self._draw_pin_buffer_cell(
                    snapshot,
                    m_obj.in_pins,
                    "IN",
                    in_x,
                    ry,
                    col_in_w - 6.0,
                    row_h,
                    is_input=True,
                )

                # Column 4: Output Pin Buffer Stats (OUT[0..])
                out_x = in_x + col_in_w
                self._draw_pin_buffer_cell(
                    snapshot,
                    m_obj.out_pins,
                    "OUT",
                    out_x,
                    ry,
                    col_out_w - 6.0,
                    row_h,
                    is_input=False,
                )

                self._hit_boxes.append(
                    {
                        "kind": "buffer_row",
                        "comp_id": m_obj.comp_id,
                        "pipe_id": m_obj.pipeline_id,
                        "core_id": m_obj.core_id,
                        "mod": m_obj,
                        "x": 0.0,
                        "y": ry,
                        "w": w,
                        "h": row_h,
                    }
                )
                if m_vlist:
                    # Register violation tree hit-box on both the Column 1 tree arrow and the Column 2 red violation pill
                    self._hit_boxes.append(
                        {
                            "kind": "violation_node",
                            "comp_id": m_obj.comp_id,
                            "pipe_id": m_obj.pipeline_id,
                            "core_id": m_obj.core_id,
                            "v_key": m_vlist[0]["v_key"],
                            "violation": m_vlist[0],
                            "mod": m_obj,
                            "x": 6.0,
                            "y": ry + 1.0,
                            "w": 20.0,
                            "h": 16.0,
                            "expander_x_end": 26.0,
                        }
                    )
                    self._hit_boxes.append(
                        {
                            "kind": "violation_node",
                            "comp_id": m_obj.comp_id,
                            "pipe_id": m_obj.pipeline_id,
                            "core_id": m_obj.core_id,
                            "v_key": m_vlist[0]["v_key"],
                            "violation": m_vlist[0],
                            "mod": m_obj,
                            "x": ex_x - 2.0,
                            "y": ry + 14.0,
                            "w": col_exec_w - 4.0,
                            "h": 14.5,
                            "expander_x_end": ex_x + 16.0,
                        }
                    )

            # Render expanded violation tree rows beneath the module row
            if is_mod_expanded:
                for g_idx, v_grp in enumerate(m_vlist):
                    gy = cur_row_y
                    cur_row_y += v_grp_row_h
                    gkey = v_grp["v_key"]
                    grp_expanded = gkey not in self._collapsed_violation_groups
                    children = v_grp.get("children", [])
                    is_last_grp = g_idx == len(m_vlist) - 1

                    if gy + v_grp_row_h >= rows_top_y and gy <= rows_top_y + rows_viewport_h:
                        is_grp_sel = self._selected_violation_key == gkey
                        is_grp_hov = (
                            self._hovered_hit_info is not None
                            and self._hovered_hit_info.get("v_key") == gkey
                        )
                        grp_bg = (
                            self._rgba(0.36, 0.11, 0.15, 0.96)
                            if (is_grp_sel or is_grp_hov)
                            else self._rgba(0.22, 0.08, 0.11, 0.94)
                        )
                        self._draw_rect_fill_stroke(
                            snapshot,
                            0.0,
                            gy,
                            w,
                            v_grp_row_h,
                            grp_bg,
                            self._rgba(0.65, 0.22, 0.26, 0.85),
                            0.8,
                        )
                        # Red vertical tree bar on left
                        self._draw_rect_fill_stroke(
                            snapshot,
                            0.0,
                            gy,
                            4.5,
                            v_grp_row_h,
                            self._rgba(0.98, 0.35, 0.35, 1.0),
                            None,
                        )
                        branch_chr = "└─" if (is_last_grp and not grp_expanded) else "├─"
                        grp_arrow = "▾" if grp_expanded else "▸"
                        grp_txt = f"  {branch_chr} {grp_arrow} ⚠ [{v_grp['type']}] {v_grp['summary']}"
                        action_w = 135.0
                        self._draw_text(
                            snapshot,
                            grp_txt,
                            10.0,
                            gy + 3.0,
                            "Monospace Bold 7.5",
                            self._rgba(1.0, 0.72, 0.72, 1.0),
                            max_w=max(120.0, w - action_w - 20.0),
                        )
                        # Right-side action badge indicating click opens Telemetry Log notebook page
                        act_x = max(140.0, w - action_w - 8.0)
                        self._draw_rect_fill_stroke(
                            snapshot,
                            act_x,
                            gy + 2.5,
                            action_w,
                            v_grp_row_h - 5.0,
                            self._rgba(0.46, 0.14, 0.18, 0.95),
                            self._rgba(0.98, 0.48, 0.48, 0.90),
                            0.8,
                        )
                        self._draw_text(
                            snapshot,
                            "Telemetry Log →",
                            act_x + 8.0,
                            gy + 3.0,
                            "Monospace Bold 7",
                            self._rgba(1.0, 0.88, 0.88, 1.0),
                            max_w=action_w - 10.0,
                        )
                        self._hit_boxes.append(
                            {
                                "kind": "violation_tree_group",
                                "comp_id": m_obj.comp_id,
                                "pipe_id": m_obj.pipeline_id,
                                "core_id": m_obj.core_id,
                                "v_key": gkey,
                                "violation": v_grp,
                                "mod": m_obj,
                                "x": 0.0,
                                "y": gy,
                                "w": w,
                                "h": v_grp_row_h,
                                "expander_x_end": 48.0,
                            }
                        )

                    if grp_expanded:
                        for c_idx, ev_item in enumerate(children):
                            ey = cur_row_y
                            cur_row_y += v_ev_row_h
                            ekey = ev_item["v_key"]
                            is_last_ev = c_idx == len(children) - 1
                            if ey + v_ev_row_h >= rows_top_y and ey <= rows_top_y + rows_viewport_h:
                                is_ev_sel = self._selected_violation_key == ekey
                                is_ev_hov = (
                                    self._hovered_hit_info is not None
                                    and self._hovered_hit_info.get("v_key") == ekey
                                )
                                ev_bg = (
                                    self._rgba(0.32, 0.10, 0.14, 0.95)
                                    if (is_ev_sel or is_ev_hov)
                                    else self._rgba(0.16, 0.06, 0.09, 0.94)
                                )
                                self._draw_rect_fill_stroke(
                                    snapshot,
                                    0.0,
                                    ey,
                                    w,
                                    v_ev_row_h,
                                    ev_bg,
                                    self._rgba(0.45, 0.16, 0.20, 0.75),
                                    0.6,
                                )
                                self._draw_rect_fill_stroke(
                                    snapshot,
                                    0.0,
                                    ey,
                                    4.5,
                                    v_ev_row_h,
                                    self._rgba(0.92, 0.30, 0.30, 0.90),
                                    None,
                                )
                                tree_stem = "   " if is_last_grp else "│  "
                                ev_branch = "└─" if is_last_ev else "├─"
                                ev_txt = f"  {tree_stem}{ev_branch} • {ev_item['summary']}"
                                self._draw_text(
                                    snapshot,
                                    ev_txt,
                                    14.0,
                                    ey + 2.5,
                                    "Monospace 7",
                                    self._rgba(0.98, 0.62, 0.62, 0.98),
                                    max_w=max(120.0, w - 125.0),
                                )
                                self._draw_text(
                                    snapshot,
                                    "[Inspect Log →]",
                                    max(140.0, w - 108.0),
                                    ey + 2.5,
                                    "Monospace Bold 6.5",
                                    self._rgba(1.0, 0.78, 0.78, 0.92),
                                    max_w=100.0,
                                )
                                self._hit_boxes.append(
                                    {
                                        "kind": "violation_tree_item",
                                        "comp_id": m_obj.comp_id,
                                        "pipe_id": m_obj.pipeline_id,
                                        "core_id": m_obj.core_id,
                                        "v_key": ekey,
                                        "violation": ev_item,
                                        "mod": m_obj,
                                        "x": 0.0,
                                        "y": ey,
                                        "w": w,
                                        "h": v_ev_row_h,
                                    }
                                )

        snapshot.pop()

        # ------------------------------------------------------------------
        # 5. Interactive Hover Crosshair & Floating Telemetry Tooltip
        # ------------------------------------------------------------------
        if self._hovered_hit_info is not None and self._hover_x >= 0.0 and self._hover_y >= 0.0:
            self._draw_hover_tooltip(snapshot, w, h, self._hovered_hit_info)

    def _draw_pin_buffer_cell(
        self,
        snapshot: Gtk.Snapshot,
        pins: List[ModulePinBufferStats],
        dir_label: str,
        x: float,
        y: float,
        w: float,
        h: float,
        is_input: bool,
    ) -> None:
        if w <= 24.0:
            return
        if not pins:
            self._draw_text(
                snapshot,
                f"{dir_label}: (endpoint / host-dai)",
                x,
                y + 8.0,
                "Monospace Italic 7",
                self._rgba(0.48, 0.54, 0.64, 0.75),
                max_w=w,
            )
            return

        p0 = pins[0]
        bsize = max(1, p0.buf_size)
        entry_b = p0.ll_entry_avail
        exit_b = p0.ll_exit_avail
        xfer_b = p0.last_bytes_transferred
        tot_b = p0.total_bytes_transferred
        rms_db = p0.entry_rms_db if is_input else p0.exit_rms_db

        # Reserve right portion of cell for sample sparkline if samples are available
        spark_w = min(56.0, w * 0.24) if p0.samples and w >= 150.0 else 0.0
        text_w = max(40.0, w - spark_w - 4.0)

        pin_cnt_tag = f"{dir_label}[0..{len(pins)-1}]" if len(pins) > 1 else f"{dir_label}[{p0.pin_index}]"
        dp_tag = f" DP:{p0.dp_entry_avail}->{p0.dp_exit_avail}/{p0.dp_ring_size}B" if p0.dp_ring_size > 0 else ""
        top_txt = (
            f"{pin_cnt_tag} {entry_b}->{exit_b}/{p0.buf_size}B{dp_tag} "
            f"xfer={xfer_b}B(tot={tot_b // 1024}KB) {rms_db:+.1f}dB"
        )
        self._draw_text(
            snapshot,
            top_txt,
            x,
            y + 2.0,
            "Monospace 7",
            self._rgba(0.84, 0.93, 0.98, 0.96) if is_input else self._rgba(0.84, 0.98, 0.88, 0.96),
            max_w=text_w,
        )

        # Draw visual buffer capacity & entry->exit fill gauge bar
        bar_y = y + 16.5
        bar_h = 8.5
        bar_w = max(30.0, text_w - 6.0)
        self._draw_rect_fill_stroke(
            snapshot,
            x,
            bar_y,
            bar_w,
            bar_h,
            self._rgba(0.05, 0.06, 0.09, 0.95),
            self._rgba(0.24, 0.30, 0.40, 0.85),
            0.8,
        )

        # Fill bar: entry fill (cyan/blue) and exit fill (emerald green) or transferred fraction
        active_bytes = entry_b if is_input else exit_b
        if active_bytes == 0 and xfer_b > 0:
            active_bytes = min(p0.buf_size, xfer_b)
        fill_frac = min(1.0, max(0.0, float(active_bytes) / float(bsize)))
        fill_col = (
            self._rgba(0.24, 0.78, 0.96, 0.80)
            if is_input
            else self._rgba(0.28, 0.86, 0.54, 0.80)
        )
        self._draw_rect_fill_stroke(
            snapshot,
            x + 0.5,
            bar_y + 0.5,
            (bar_w - 1.0) * fill_frac,
            bar_h - 1.0,
            fill_col,
            None,
        )

        # Overlay rate/ch & fill percentage inside the bar
        pct = (float(active_bytes) / float(bsize)) * 100.0
        fmt_lbl = (
            f"{p0.rate // 1000}kHz/{p0.channels}ch • {active_bytes}/{p0.buf_size}B ({pct:.0f}%)"
            if p0.rate > 0
            else f"{active_bytes}/{p0.buf_size}B ({pct:.0f}%)"
        )
        self._draw_text(
            snapshot,
            fmt_lbl,
            x + 4.0,
            bar_y - 0.5,
            "Monospace Bold 6.5",
            self._rgba(0.96, 0.98, 1.0, 0.95),
            max_w=bar_w - 6.0,
        )

        # Mini sample tap sparkline on the right of the cell (supports pointer-anchored zoom)
        if spark_w >= 28.0 and p0.samples:
            sx = x + text_w + 2.0
            sy = y + 3.0
            sh = h - 6.0
            self._spark_hit_boxes.append({"x": sx, "y": sy, "w": spark_w, "h": sh})
            self._draw_rect_fill_stroke(
                snapshot,
                sx,
                sy,
                spark_w,
                sh,
                self._rgba(0.04, 0.05, 0.08, 0.95),
                self._rgba(0.22, 0.28, 0.38, 0.8),
                0.7,
            )
            smps = p0.samples
            n_total = len(smps)
            vis_cnt = max(8, int(round(float(n_total) / max(1.0, self.buf_spark_zoom))))
            start_i = max(0, min(max(0, n_total - vis_cnt), int(round(self.buf_spark_pan_f))))
            view_smps = smps[start_i : start_i + vis_cnt]
            peak_s = max((abs(v) for v in smps), default=1) or 1
            pb = Gsk.PathBuilder.new()
            n_pts = len(view_smps)
            mid_y = sy + sh * 0.5
            for s_idx, val in enumerate(view_smps):
                px = sx + 1.0 + (s_idx / float(max(1, n_pts - 1))) * (spark_w - 2.0)
                py = mid_y - (float(val) / float(peak_s)) * (sh * 0.42)
                if s_idx == 0:
                    pb.move_to(px, py)
                else:
                    pb.line_to(px, py)
            snapshot.append_stroke(
                pb.to_path(),
                Gsk.Stroke.new(1.0),
                self._rgba(0.38, 0.88, 1.0, 0.95) if is_input else self._rgba(0.42, 0.96, 0.62, 0.95),
            )

    def _draw_hover_tooltip(
        self,
        snapshot: Gtk.Snapshot,
        w: float,
        h: float,
        hit: Dict[str, Any],
    ) -> None:
        lines: List[str] = []
        kind = hit.get("kind", "")
        if kind == "violation_header_badge":
            v = hit.get("violation", {})
            lines.append(f"⚠ {v.get('summary', 'Scheduling & Buffer Violations')}")
            lines.append("Click ▸/▾ arrow to expand/collapse all violation trees, or click badge to open Telemetry Log tab")
        elif kind in ("violation_node", "violation_tree_group", "violation_tree_item"):
            v = hit.get("violation", {})
            m: Optional[ComponentPerfItem] = hit.get("mod") or v.get("mod")
            lines.append(f"⚠ [{v.get('type', 'VIOLATION')}] {v.get('summary', '')}")
            if m is not None:
                lines.append(
                    f"Module: {m.widget_name} (0x{m.comp_id:06x})  |  Pipe {m.pipeline_id}  |  Core {m.core_id} [{m.domain} • {m.mem_type}]"
                )
                lines.append(
                    f"Cycles: avg={m.avg_cycles} / peak={m.peak_cycles}  |  Effective CPC={m.effective_cpc} (Violations={m.cpc_violations})"
                )
            lines.append("Click ▸/▾ to toggle violation tree  |  Click violation to open Telemetry Log in Attributes Notebook")
        elif kind == "pipeline":
            p: PipelineSchedTelemetry = hit["pipe"]
            t_idx = hit.get("tick_idx", 0)
            lines.append(
                f"Pipeline {p.pipeline_id}  |  Core {p.core_id}  |  Tick #{t_idx} ({hit.get('time_ms', 0.0):.2f} ms)"
            )
            lines.append(
                f"Priority: {p.priority}  |  Period: {p.period_us} µs  |  Mem: {p.mem_type}  |  "
                f"Modules: {p.ll_module_count} LL / {p.dp_module_count} DP"
            )
            lines.append(
                f"Exec Time: avg={p.exec_avg_us:.3f} µs / peak={p.exec_peak_us:.3f} µs  |  "
                f"Cycles: avg={p.avg_cycles} / peak={p.peak_cycles} ({p.avg_kcps} kcps)  |  Xruns: {p.xruns}"
            )
        elif "mod" in hit and hit["mod"] is not None:
            m = hit["mod"]
            m_us = m.exec_avg_us if m.exec_avg_us > 0 else (m.avg_cycles / 393.216)
            m_pk_us = m.exec_peak_us if m.exec_peak_us > 0 else (m.peak_cycles / 393.216)
            tick_str = (
                f"  |  Tick #{hit['tick_idx']} (@ {hit.get('start_us', 0.0):.1f} µs)"
                if "tick_idx" in hit
                else ""
            )
            lines.append(
                f"Module: {m.widget_name} (0x{m.comp_id:06x})  |  Pipe {m.pipeline_id}  |  "
                f"Core {m.core_id} [{m.domain} • {m.mem_type}]{tick_str}"
            )
            lines.append(
                f"Exec: avg={m_us:.3f} µs / peak={m_pk_us:.3f} µs  |  "
                f"Cycles: avg={m.avg_cycles} / peak={m.peak_cycles} ({m.avg_kcps}/{m.peak_kcps} kcps)  |  "
                f"Iters: {m.iterations}"
            )
            lines.append(
                f"Audio Frame: IBS={m.ibs} B, OBS={m.obs} B  |  "
                f"Effective CPC={m.effective_cpc} (Violations={m.cpc_violations})"
            )
            for p_in in m.in_pins[:2]:
                lines.append(
                    f"  IN[{p_in.pin_index}] buf={p_in.buf_id}: size={p_in.buf_size}B "
                    f"LL({p_in.ll_entry_avail}->{p_in.ll_exit_avail}B) "
                    f"consumed={p_in.last_bytes_transferred}B(tot={p_in.total_bytes_transferred}B) "
                    f"rms={p_in.entry_rms_db:+.2f}->{p_in.exit_rms_db:+.2f}dBFS"
                )
            for p_out in m.out_pins[:2]:
                lines.append(
                    f"  OUT[{p_out.pin_index}] buf={p_out.buf_id}: size={p_out.buf_size}B "
                    f"LL({p_out.ll_entry_avail}->{p_out.ll_exit_avail}B) "
                    f"produced={p_out.last_bytes_transferred}B(tot={p_out.total_bytes_transferred}B) "
                    f"rms={p_out.exit_rms_db:+.2f}dBFS(pk={p_out.exit_peak_db:+.2f}dB)"
                )
        else:
            return

        tooltip_txt = "\n".join(lines)
        layout = self.create_pango_layout(tooltip_txt)
        layout.set_font_description(Pango.FontDescription.from_string("Monospace 7.5"))
        tw, th = layout.get_pixel_size()

        box_w = float(tw + 14)
        box_h = float(th + 10)
        bx = min(w - box_w - 8.0, max(8.0, self._hover_x + 12.0))
        by = self._hover_y - box_h - 10.0
        if by < 6.0:
            by = min(h - box_h - 6.0, self._hover_y + 14.0)

        self._draw_rect_fill_stroke(
            snapshot,
            bx,
            by,
            box_w,
            box_h,
            self._rgba(0.04, 0.05, 0.08, 0.95),
            self._rgba(0.98, 0.84, 0.34, 0.90),
            1.1,
        )
        snapshot.save()
        pt = Graphene.Point.alloc()
        pt.init(bx + 7.0, by + 5.0)
        snapshot.translate(pt)
        snapshot.append_layout(layout, self._rgba(0.96, 0.97, 1.0, 1.0))
        snapshot.restore()


