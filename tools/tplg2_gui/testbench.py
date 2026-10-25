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
from gi.repository import Gdk, Graphene, Gsk, Gtk, Pango

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
    ll_avg_exec_us: float = 0.0
    ll_peak_exec_us: float = 0.0
    dp_avg_kcps: int = 0
    dp_peak_kcps: int = 0
    dp_avg_exec_us: float = 0.0
    dp_peak_exec_us: float = 0.0


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
    total_cycles: int = 0
    heap_bytes: int = 0
    effective_cpc: int = 0
    cpc_violations: int = 0
    ibs: int = 0
    obs: int = 0
    in_rms_db: float = -120.0
    out_rms_db: float = -120.0

    @property
    def comp_id(self) -> int:
        return self.resource_id


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
    # Cores, Components, Kcontrols & Probes
    cores: List[CoreTelemetry] = field(default_factory=list)
    components: List[ComponentPerfItem] = field(default_factory=list)
    kcontrols: List[KcontrolTelemetryItem] = field(default_factory=list)
    probes: List[Dict[str, Any]] = field(default_factory=list)
    loaded_modules: Dict[int, str] = field(default_factory=dict)
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

        # Parse per-module scheduling & entry/exit buffer RMS dBFS from `sched-telem` output
        sched_mod_map: Dict[int, Dict[str, Any]] = {}
        for line in telem.shell_sched_telem.splitlines():
            m_mod = re.search(
                r"\[0x([0-9a-fA-F]+)\s+(\S+)\s*\]\s+pipe=(\d+)\s+core=(\d+)\s+(LL|DP)\s+mem=(SRAM|DRAM).*?ibs=(\d+)\s+obs=(\d+)\s+cpc=(\d+)\(viol=(\d+)\)",
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
                sched_mod_map[cid] = {
                    "pipeline_id": int(m_mod.group(3)),
                    "core_id": int(m_mod.group(4)),
                    "domain": m_mod.group(5),
                    "mem_domain": m_mod.group(6),
                    "ibs": int(m_mod.group(7)),
                    "obs": int(m_mod.group(8)),
                    "effective_cpc": int(m_mod.group(9)),
                    "cpc_violations": int(m_mod.group(10)),
                    "in_rms_db": in_rms,
                    "out_rms_db": out_rms,
                }

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
                comp.ibs = sinfo["ibs"]
                comp.obs = sinfo["obs"]
                comp.effective_cpc = sinfo["effective_cpc"]
                comp.cpc_violations = sinfo["cpc_violations"]
                comp.in_rms_db = sinfo["in_rms_db"]
                comp.out_rms_db = sinfo["out_rms_db"]
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
    ) -> Tuple[str, str, List[float]]:
        """Create input .raw and .wav files and return (raw_path, wav_path, normalized_mono_samples)."""
        in_raw = os.path.join(self.work_dir, "tb_input.raw")
        in_wav = os.path.join(self.work_dir, "tb_input.wav")
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

        client.exec_shell("pause")
        all_pipes = list(plan.playback_pipelines) + list(plan.capture_pipelines)
        if not all_pipes:
            self.active_ipc_plan = None
            self.active_pipeline_ids = []
            return logs

        header = f"[IPC4] Destroying active pipelines {plan.selected_pipeline_ids} via IPC4…"
        logs.append(header)
        if log_callback:
            log_callback(header)

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

        # 2. Unbind cross-pipeline modules (SOF_IPC4_MOD_UNBIND)
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

        # 3. Transition active pipelines to RESET
        for pop in all_pipes:
            pri, ext = encode_set_pipeline_state(pop.instance_id, SOF_IPC4_PIPELINE_STATE_RESET)
            desc = f"SET_PIPELINE_STATE(ppl={pop.instance_id} '{pop.name}', RESET)"
            st, rdict, _ = client.send_ipc4_tx(pri, ext, description=desc)
            ipc_st = rdict.get("ipc_status", st)
            line = f"  [IPC4-TX] {desc} -> status={ipc_st}"
            logs.append(line)
            if log_callback:
                log_callback(line)

        # 4. Delete pipelines (SOF_IPC4_GLB_DELETE_PIPELINE)
        for pop in all_pipes:
            pri, ext = encode_delete_pipeline(pop.instance_id)
            desc = f"DELETE_PIPELINE(ppl={pop.instance_id} '{pop.name}')"
            st, rdict, _ = client.send_ipc4_tx(pri, ext, description=desc)
            ipc_st = rdict.get("ipc_status", st)
            line = f"  [IPC4-TX] {desc} -> status={ipc_st}"
            logs.append(line)
            if log_callback:
                log_callback(line)

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

            # 2. Ensure input/output raw file paths exist and configure io-cfg on server
            if not in_raw_paths:
                default_in = os.path.join(self.work_dir, "tb_input.raw")
                if not os.path.isfile(default_in):
                    self.generate_input_signal("sine_997", rate, channels, bits, duration_sec=0.15)
                in_raw_paths = [default_in]
            if not out_raw_paths:
                default_out = os.path.join(self.work_dir, "tb_output.raw")
                out_raw_paths = [default_out]

            pipe_arg = ",".join(str(p) for p in norm_pipes)
            fmt_arg = f"S{bits}_LE"
            in_arg = ",".join(in_raw_paths)
            out_arg = ",".join(out_raw_paths)

            client.exec_shell("pause")
            io_cmd = (
                f"io-cfg -i {in_arg} -o {out_arg} -p {pipe_arg} "
                f"-r {rate} -R {eff_rate_out} -c {channels} -n {eff_ch_out} -b {fmt_arg}"
            )
            client.exec_shell(io_cmd)

            # 3. Parse compiled .tplg and build the IPC4 creation & binding plan for norm_pipes
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

            create_hdr = (
                f"[IPC4] Creating selected pipelines {norm_pipes} from {os.path.basename(tplg_path)} "
                f"({len(plan.pipelines_by_id)} pipelines, {len(plan.widgets)} modules, {len(plan.routes)} bindings)…"
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

        # Support multiple comma-separated input raw files (-i in1,in2,...) up to 4
        in_raw_list = [in_raw]
        if extra_in_wavs:
            for idx, ex_wav in enumerate(extra_in_wavs[:3], start=2):
                ex_raw = os.path.join(self.work_dir, f"tb_input_{idx}.raw")
                if ex_wav and os.path.isfile(ex_wav) and shutil.which("sox"):
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
                else:
                    shutil.copyfile(in_raw, ex_raw)
                in_raw_list.append(ex_raw)

        # Support multiple comma-separated output raw files (-o out1,out2,...) up to 4
        out_raw = os.path.join(self.work_dir, "tb_output.raw")
        out_wav = os.path.join(self.work_dir, "tb_output.wav")
        out_raw_list = [out_raw]
        for idx in range(2, min(4, max(1, num_outputs)) + 1):
            out_raw_list.append(os.path.join(self.work_dir, f"tb_output_{idx}.raw"))
        for opath in out_raw_list:
            if os.path.exists(opath):
                os.remove(opath)

        self.last_in_samples = list(in_norm)
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
                for pop in plan.playback_pipelines:
                    pri, ext = encode_set_pipeline_state(pop.instance_id, SOF_IPC4_PIPELINE_STATE_RUNNING)
                    desc = f"SET_PIPELINE_STATE(ppl={pop.instance_id} '{pop.name}', RUNNING)"
                    st, rdict, _ = client.send_ipc4_tx(pri, ext, description=desc)
                    ipc_logs.append(f"  [IPC4-TX] {desc} -> status={rdict.get('ipc_status', st)}")

                # Capture pipelines transition in reverse order (sink to source) matching utils_ipc4.c
                for pop in reversed(plan.capture_pipelines):
                    pri, ext = encode_set_pipeline_state(pop.instance_id, SOF_IPC4_PIPELINE_STATE_PAUSED)
                    desc = f"SET_PIPELINE_STATE(ppl={pop.instance_id} '{pop.name}', PAUSED)"
                    st, rdict, _ = client.send_ipc4_tx(pri, ext, description=desc)
                    ipc_logs.append(f"  [IPC4-TX] {desc} -> status={rdict.get('ipc_status', st)}")
                for pop in reversed(plan.capture_pipelines):
                    pri, ext = encode_set_pipeline_state(pop.instance_id, SOF_IPC4_PIPELINE_STATE_RUNNING)
                    desc = f"SET_PIPELINE_STATE(ppl={pop.instance_id} '{pop.name}', RUNNING)"
                    st, rdict, _ = client.send_ipc4_tx(pri, ext, description=desc)
                    ipc_logs.append(f"  [IPC4-TX] {desc} -> status={rdict.get('ipc_status', st)}")

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

            out_norm: List[float] = []
            if os.path.isfile(out_raw) and os.path.getsize(out_raw) > 0:
                out_norm = self._read_raw_head_normalized(out_raw, eff_ch_out, bits, target_frames)

            if on_live_waveform is not None and out_norm:
                on_live_waveform(in_norm[: len(out_norm)], out_norm, len(out_norm))

            ok = bool(len(out_norm) > 0 or (telemetry and telemetry.total_ll_ticks > 0))
            if ok and out_norm:
                self._write_wav_from_normalized(out_wav, out_norm, eff_rate_out)

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

            summary = (
                f"Status: {'PASS' if ok else 'FAIL'} (IPC4)  |  Pipelines: -p {pipe_arg}  |  "
                f"In: {len(in_norm)}f (Peak {in_peak:.1f}, RMS {in_rms:.1f} dBFS) → "
                f"Out: {len(out_norm)}f (Peak {out_peak:.1f}, RMS {out_rms:.1f} dBFS)"
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
                    f"{summary} | in[:6]={[round(x, 4) for x in in_norm[:6]]} "
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
        self.sample_rate: int = 48000
        self.channels: int = 2
        self.bits: int = 32
        self.in_label: str = "INPUT PCM: Source (Host/DAI)"
        self.out_label: str = "OUTPUT PCM: Sink (DAI/Host)"
        self.signal_label: str = "Sine 997 Hz"
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

        # Mouse interaction state
        self._hover_x: Optional[float] = None
        self._hover_y: Optional[float] = None
        self._drag_start_pan: int = 0

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
    ) -> None:
        self.in_label = in_label
        self.out_label = out_label
        if signal_label:
            self.signal_label = signal_label
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
    ) -> None:
        self.in_samples = in_samples
        self.out_samples = out_samples
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
        return max(len(self.in_samples), len(self.out_samples), 0)

    def visible_sample_count(self) -> int:
        total = max(self.total_samples(), self.BASE_VISIBLE_SAMPLES)
        raw_vis = int(round(self.BASE_VISIBLE_SAMPLES / max(0.05, self.time_zoom)))
        return max(16, min(total, raw_vis))

    def get_zoom_summary_text(self) -> str:
        vis = self.visible_sample_count()
        dur_ms = (vis / float(max(1, self.sample_rate))) * 1000.0
        return f"Time: {self.time_zoom:.2g}× ({dur_ms:.2f} ms / {vis} smp)  |  Amp: {self.amp_zoom:.2g}×"

    def zoom_in_time(self, factor: float = 1.5, anchor_ratio: float = 0.5) -> None:
        self._set_time_zoom(self.time_zoom * factor, anchor_ratio)

    def zoom_out_time(self, factor: float = 1.5, anchor_ratio: float = 0.5) -> None:
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
        if self.on_zoom_changed:
            self.on_zoom_changed()
        self.queue_draw()

    def _set_time_zoom(self, new_zoom: float, anchor_ratio: float = 0.5) -> None:
        total = max(self.total_samples(), self.BASE_VISIBLE_SAMPLES)
        min_zoom = max(0.05, float(self.BASE_VISIBLE_SAMPLES) / float(max(1, total)))
        max_zoom = 37.5  # down to 16 samples
        old_vis = self.visible_sample_count()
        anchor_sample = self.pan_offset + int(old_vis * max(0.0, min(1.0, anchor_ratio)))

        self.time_zoom = max(min_zoom, min(max_zoom, new_zoom))
        new_vis = self.visible_sample_count()
        self.pan_offset = anchor_sample - int(new_vis * max(0.0, min(1.0, anchor_ratio)))
        self._clamp_pan()
        if self.on_zoom_changed:
            self.on_zoom_changed()
        self.queue_draw()

    def _clamp_pan(self) -> None:
        total = self.total_samples()
        vis = self.visible_sample_count()
        max_pan = max(0, total - vis)
        self.pan_offset = max(0, min(max_pan, self.pan_offset))

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

        if ctrl_held:
            if dy < 0:
                self.zoom_in_amp(1.25)
            elif dy > 0:
                self.zoom_out_amp(1.25)
            return True

        if shift_held or (abs(dx) > abs(dy) and abs(dx) > 0.01):
            delta = dx if abs(dx) > abs(dy) else dy
            step = max(1, int(self.visible_sample_count() * 0.12))
            self.pan_offset += int(math.copysign(step, delta))
            self._clamp_pan()
            self.queue_draw()
            return True

        w = max(1.0, float(self.get_width()))
        left_gutter = 62.0
        plot_w = max(10.0, w - left_gutter - 10.0)
        anchor = 0.5
        if self._hover_x is not None and self._hover_x >= left_gutter:
            anchor = max(0.0, min(1.0, (self._hover_x - left_gutter) / plot_w))

        if dy < 0:
            self.zoom_in_time(1.25, anchor_ratio=anchor)
        elif dy > 0:
            self.zoom_out_time(1.25, anchor_ratio=anchor)
        return True

    def _on_drag_begin(self, _gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
        self._drag_start_pan = self.pan_offset

    def _on_drag_update(self, _gesture: Gtk.GestureDrag, offset_x: float, _offset_y: float) -> None:
        w = max(1.0, float(self.get_width()))
        plot_w = max(10.0, w - 72.0)
        vis = self.visible_sample_count()
        delta_samples = int(round((-offset_x / plot_w) * vis))
        self.pan_offset = self._drag_start_pan + delta_samples
        self._clamp_pan()
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

        left_gutter = 62.0
        right_pad = 8.0
        top_pad = 4.0
        bottom_ruler_h = 22.0
        lane_gap = 6.0

        plot_x = left_gutter
        plot_w = max(20.0, w - left_gutter - right_pad)
        avail_h = max(40.0, h - top_pad - bottom_ruler_h - lane_gap)
        lane_h = avail_h * 0.5

        in_lane_y = top_pad
        out_lane_y = top_pad + lane_h + lane_gap
        ruler_y = out_lane_y + lane_h + 2.0

        # Determine sample slice to display
        total = self.total_samples()
        vis = self.visible_sample_count()
        trig_shift = 0
        if self.is_realtime and self.trigger_lock and self.pan_offset == 0:
            ref = self.out_samples if self.out_samples else self.in_samples
            if ref:
                trig_shift = self._find_rising_zero_crossing(ref, max_search=min(240, max(1, len(ref) - vis)))
        start_idx = min(max(0, total - vis), self.pan_offset + trig_shift) if total > vis else 0

        # Draw Lane 1: INPUT WAVEFORM
        in_fmt_str = f"{self.signal_label} @ {self.sample_rate / 1000.0:.1f}kHz S{self.bits}_LE {self.channels}ch"
        in_dur_ms = (len(self.in_samples) / float(max(1, self.sample_rate))) * 1000.0
        in_metrics = (
            f"Peak: {self.in_peak_db:+.1f} dBFS  |  RMS: {self.in_rms_db:+.1f} dBFS  |  "
            f"{len(self.in_samples)} smp ({in_dur_ms:.1f} ms)"
            if self.in_samples
            else "No input signal loaded"
        )
        self._draw_waveform_lane(
            snapshot=snapshot,
            lane_x=plot_x,
            lane_y=in_lane_y,
            lane_w=plot_w,
            lane_h=lane_h,
            left_gutter=left_gutter,
            title_badge=f"● {self.in_label}   [{in_fmt_str}]",
            metrics_badge=in_metrics,
            samples=self.in_samples,
            start_idx=start_idx,
            vis_count=vis,
            trace_color=self._rgba(0.28, 0.80, 0.98, 0.96),
            fill_color=self._rgba(0.28, 0.80, 0.98, 0.13),
            header_bg=self._rgba(0.10, 0.16, 0.24, 0.95),
            accent_color=self._rgba(0.35, 0.85, 1.0, 1.0),
        )

        # Draw Lane 2: OUTPUT WAVEFORM
        out_fmt_str = f"{self.sample_rate / 1000.0:.1f}kHz S{self.bits}_LE {self.channels}ch"
        out_dur_ms = (len(self.out_samples) / float(max(1, self.sample_rate))) * 1000.0
        out_metrics = (
            f"Peak: {self.out_peak_db:+.1f} dBFS  |  RMS: {self.out_rms_db:+.1f} dBFS  |  "
            f"{len(self.out_samples)} smp ({out_dur_ms:.1f} ms)"
            if self.out_samples
            else "No output captured yet"
        )
        self._draw_waveform_lane(
            snapshot=snapshot,
            lane_x=plot_x,
            lane_y=out_lane_y,
            lane_w=plot_w,
            lane_h=lane_h,
            left_gutter=left_gutter,
            title_badge=f"● {self.out_label}   [{out_fmt_str}]",
            metrics_badge=out_metrics,
            samples=self.out_samples,
            start_idx=start_idx,
            vis_count=vis,
            trace_color=self._rgba(0.24, 0.92, 0.58, 0.96),
            fill_color=self._rgba(0.24, 0.92, 0.58, 0.13),
            header_bg=self._rgba(0.09, 0.20, 0.16, 0.95),
            accent_color=self._rgba(0.30, 0.96, 0.64, 1.0),
        )

        # Draw Bottom Time-Axis Ruler & Mini Viewport Scrollbar
        self._draw_time_ruler(
            snapshot=snapshot,
            plot_x=plot_x,
            ruler_y=ruler_y,
            plot_w=plot_w,
            ruler_h=bottom_ruler_h - 2.0,
            start_idx=start_idx,
            vis_count=vis,
            total_count=total,
        )

        # Draw Interactive Crosshair Cursor & Sample Value Tooltip on Hover
        if (
            self._hover_x is not None
            and self._hover_y is not None
            and plot_x <= self._hover_x <= plot_x + plot_w
            and in_lane_y <= self._hover_y <= out_lane_y + lane_h
        ):
            self._draw_hover_crosshair(
                snapshot=snapshot,
                hx=self._hover_x,
                plot_x=plot_x,
                plot_w=plot_w,
                top_y=in_lane_y,
                bot_y=out_lane_y + lane_h,
                start_idx=start_idx,
                vis_count=vis,
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
        hdr_h = min(19.0, max(15.0, lane_h * 0.24))
        body_y = lane_y + hdr_h
        body_h = max(16.0, lane_h - hdr_h)
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
        gut_rect = Graphene.Rect.alloc()
        gut_rect.init(2.0, lane_y, left_gutter - 4.0, lane_h)
        snapshot.append_color(self._rgba(0.07, 0.08, 0.12, 1.0), gut_rect)

        # Y-axis scale labels in left gutter
        full_scale_val = 1.0 / max(0.1, self.amp_zoom)
        axis_col = self._rgba(0.62, 0.68, 0.78, 0.95)
        self._draw_text(
            snapshot,
            f"+{full_scale_val:.2g}",
            6.0,
            body_y + 1.0,
            "Monospace 7.5",
            axis_col,
        )
        self._draw_text(
            snapshot,
            " 0.0",
            6.0,
            mid_y - 6.0,
            "Monospace 7.5",
            axis_col,
        )
        self._draw_text(
            snapshot,
            f"-{full_scale_val:.2g}",
            6.0,
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
        self._draw_text(
            snapshot,
            title_badge,
            lane_x + 8.0,
            lane_y + 2.0,
            "Sans Bold 8.5",
            accent_color,
        )
        m_layout = self.create_pango_layout(metrics_badge)
        m_layout.set_font_description(Pango.FontDescription.from_string("Monospace 8"))
        mw, _ = m_layout.get_pixel_size()
        if lane_w - mw - 10.0 > 240.0:
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
    ) -> None:
        r_rect = Graphene.Rect.alloc()
        r_rect.init(plot_x, ruler_y, plot_w, ruler_h)
        snapshot.append_color(self._rgba(0.07, 0.08, 0.12, 1.0), r_rect)

        self._draw_text(
            snapshot,
            "TIME / SMP",
            4.0,
            ruler_y + 3.0,
            "Sans Bold 7",
            self._rgba(0.55, 0.62, 0.72, 0.9),
        )

        n_ticks = 6
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
    ) -> None:
        rel = max(0.0, min(1.0, (hx - plot_x) / max(1.0, plot_w)))
        smp_idx = start_idx + int(round(rel * max(1, vis_count - 1)))
        t_ms = (smp_idx / float(max(1, self.sample_rate))) * 1000.0

        in_val = self.in_samples[smp_idx] if smp_idx < len(self.in_samples) else 0.0
        out_val = self.out_samples[smp_idx] if smp_idx < len(self.out_samples) else 0.0
        in_db = 20.0 * math.log10(abs(in_val)) if abs(in_val) > 1e-9 else -120.0
        out_db = 20.0 * math.log10(abs(out_val)) if abs(out_val) > 1e-9 else -120.0

        # Vertical cursor line
        cpb = Gsk.PathBuilder.new()
        cpb.move_to(hx, top_y)
        cpb.line_to(hx, bot_y)
        snapshot.append_stroke(
            cpb.to_path(),
            Gsk.Stroke.new(1.0),
            self._rgba(1.0, 0.86, 0.36, 0.85),
        )

        # Floating readout pill
        info_txt = (
            f"Sample #{smp_idx} ({t_ms:.3f} ms)  |  "
            f"IN: {in_val:+.4f} ({in_db:+.1f} dBFS)  |  "
            f"OUT: {out_val:+.4f} ({out_db:+.1f} dBFS)"
        )
        layout = self.create_pango_layout(info_txt)
        layout.set_font_description(Pango.FontDescription.from_string("Monospace Bold 8"))
        tw, th = layout.get_pixel_size()

        bx = min(plot_x + plot_w - tw - 14.0, max(plot_x + 6.0, hx + 10.0))
        by = top_y + 20.0
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

