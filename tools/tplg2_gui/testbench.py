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
import time
import wave
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import gi
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Graphene, Gsk, Gtk, Pango


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


@dataclass
class ComponentPerfItem:
    resource_id: int
    module_id: int
    instance_id: int
    module_name: str
    widget_name: str = ""
    core_id: int = 0
    domain: str = "LL"
    is_removed: bool = False
    avg_kcps: int = 0
    peak_kcps: int = 0
    iterations: int = 0
    total_cycles: int = 0
    heap_bytes: int = 0


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
    protocol_log: List[str] = field(default_factory=list)

    def format_report(self) -> str:
        pwr = "D0 (ACTIVE)" if self.dsp_power_state == 1 else f"D3 (PAUSED/{self.dsp_power_state})"
        lines = [
            f"=== SOFR Network Protocol Telemetry (127.0.0.1:{self.server_port}) ===",
            f"Platform: {self.platform}  |  FW: v{self.fw_version} ({self.fw_filename})  |  State: {pwr}",
            f"DSP Cores: mask=0x{self.enabled_cores_mask:02x}/{self.num_cores}  |  Total LL Ticks: {self.total_ll_ticks}  |  Active PCMs: {self.active_pcm_streams}",
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
                    lines.append(
                        f"  Core {c.core_id}: ticks={c.tick_count}  last={c.last_time_elapsed_cyc} cyc ({c.last_time_elapsed_us:.1f} us)  "
                        f"max={c.max_time_elapsed_cyc} cyc  avg={c.avg_kcps} kcps  peak={c.peak_kcps} kcps  load={c.cpu_load_pct:.2f}%"
                    )
        if self.components:
            lines.append("--- Per-Component Performance & Heap Telemetry ---")
            for comp in self.components:
                if comp.module_id == 0:
                    continue
                label = comp.widget_name or f"{comp.module_name} (0x{comp.module_id:02x}:{comp.instance_id})"
                heap_str = f"  heap={comp.heap_bytes}B" if comp.heap_bytes > 0 else ""
                lines.append(
                    f"  [{comp.domain} C{comp.core_id}] {label:<32} avg={comp.avg_kcps:5d} kcps  "
                    f"peak={comp.peak_kcps:5d} kcps  iters={comp.iterations:4d}  cycles={comp.total_cycles}{heap_str}"
                )
        if self.kcontrols:
            lines.append(f"--- Topology Kcontrols ({len(self.kcontrols)}) ---")
            for kc in self.kcontrols[:12]:
                val_str = ",".join(str(v) for v in kc.values)
                lines.append(
                    f"  [numid={kc.numid:2d}] {kc.type_str:<5} {kc.name:<42} = [{val_str}] (widget: {kc.target_widget})"
                )
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

    def _log(self, msg: str) -> None:
        self.protocol_log.append(msg)

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
        telem.probes = self.query_probes(active_only=False)

        # Enrich component widget names from probes & shell pipelines
        probe_by_id = {p["buffer_id"]: p["widget_name"] for p in telem.probes if p.get("widget_name")}

        _, sh_info = self.exec_shell("info")
        _, sh_pipes = self.exec_shell("pipelines")
        _, sh_stats = self.exec_shell("stats")
        _, sh_cycles = self.exec_shell("kernel cycles")
        _, sh_threads = self.exec_shell("kernel threads")

        telem.shell_info = sh_info.strip()
        telem.shell_pipelines = sh_pipes.strip()
        telem.shell_stats = sh_stats.strip()
        telem.shell_cycles = sh_cycles.strip()
        telem.shell_threads = sh_threads.strip()

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
            comp.widget_name = probe_by_id.get(comp.resource_id, "")
            if comp.resource_id in comp_core_map:
                comp.core_id = comp_core_map[comp.resource_id]
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
        for i in range(n_frames):
            t = i / float(rate)
            if signal_kind == "chirp":
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
            elif signal_kind == "noise":
                val = 0.35 * (rng.uniform(-1.0, 1.0))
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

    def run_testbench(
        self,
        tplg_path: str,
        pipeline_ids: List[int],
        signal_kind: str = "sine_997",
        rate: int = 48000,
        channels: int = 2,
        bits: int = 32,
        duration_sec: float = 0.15,
        wav_source: str = "",
        control_script: str = "",
        kcontrol_overrides: Optional[Dict[str, List[int]]] = None,
        keep_alive: bool = False,
    ) -> TestbenchRunResult:
        """Launch sof-testbench4 with `-S <port> -W`, configure and control it via the SOFR
        network protocol, collect full DSP telemetry & processed audio, and return TestbenchRunResult.
        """
        self.stop_live_session()

        in_raw, in_wav, in_norm = self.generate_input_signal(
            signal_kind=signal_kind,
            rate=rate,
            channels=channels,
            bits=bits,
            duration_sec=duration_sec,
            wav_source=wav_source,
        )
        out_raw = os.path.join(self.work_dir, "tb_output.raw")
        out_wav = os.path.join(self.work_dir, "tb_output.wav")
        if os.path.exists(out_raw):
            os.remove(out_raw)

        pipe_arg = ",".join(str(p) for p in pipeline_ids) if pipeline_ids else "1,2"
        fmt_arg = f"S{bits}_LE"
        port = self._allocate_free_port(SOF_NET_DEFAULT_PORT)

        cmd = [
            self.testbench_bin,
            "-r",
            str(rate),
            "-R",
            str(rate),
            "-c",
            str(channels),
            "-n",
            str(channels),
            "-b",
            fmt_arg,
            "-p",
            pipe_arg,
            "-t",
            tplg_path,
            "-i",
            in_raw,
            "-o",
            out_raw,
            "-S",
            str(port),
            "-W",
        ]
        if control_script and os.path.isfile(control_script):
            cmd.extend(["-s", control_script])

        cmd_str = " ".join(f'"{a}"' if " " in a else a for a in cmd)
        target_frames = max(256, int(rate * duration_sec))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            client = TestbenchRemoteClient("127.0.0.1", port, timeout=2.0)
            connected = False
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                if client.connect() and client.ping():
                    connected = True
                    break
                time.sleep(0.03)

            telemetry: Optional[TestbenchTelemetry] = None
            captured_pcm_bytes = b""

            if connected:
                # 1. Configure via SOFR Protocol
                client.get_info()
                client.set_perf_measurement_state(2)  # 2 = STARTED

                # Configure remote tone generator if requested
                fmt_id = {16: SOF_PCM_FMT_S16_LE, 24: SOF_PCM_FMT_S24_LE, 32: SOF_PCM_FMT_S32_LE}.get(
                    bits, SOF_PCM_FMT_S32_LE
                )
                if signal_kind == "remote_tone":
                    client.pcm_control(
                        SOF_PCM_OP_START_TONE,
                        device="hw:0,0",
                        rate=rate,
                        channels=channels,
                        freq_hz=1000,
                        fmt_id=fmt_id,
                    )

                # Discover kcontrols and apply any kcontrol overrides over SOF_NET_OP_KCTL_SET
                kctls = client.list_kcontrols(0)
                if kcontrol_overrides:
                    for kname, kvals in kcontrol_overrides.items():
                        matched = next(
                            (k for k in kctls if k.name == kname or kname in k.name),
                            None,
                        )
                        if matched:
                            client.set_kcontrol(
                                numid=matched.numid,
                                name=matched.name,
                                values=kvals,
                            )
                        else:
                            val_csv = ",".join(str(v) for v in kvals)
                            client.exec_shell(f'amixer cset name="{kname}" {val_csv}')

                # 2. Control Execution via SOFR Protocol (ensure D0 active & wait for target_frames)
                client.pm_control(SOF_PM_OP_WAKE_HOLD)
                wait_deadline = time.monotonic() + max(2.0, duration_sec * 12.0)
                while time.monotonic() < wait_deadline:
                    if proc.poll() is not None:
                        break
                    st_p, p_stat, _ = client.pcm_control(SOF_PCM_OP_STATUS)
                    if st_p == 0 and p_stat.get("frames_played", 0) >= target_frames:
                        break
                    time.sleep(0.025)

                # 3. Get Telemetry & Capture Audio Buffer via SOFR Protocol while DSP is in D0
                telemetry = client.collect_full_telemetry()
                _, _, captured_pcm_bytes = client.pcm_control(
                    SOF_PCM_OP_CAPTURE_BUFFER,
                    device="hw:0,0",
                    rate=rate,
                    channels=channels,
                    duration_ms=max(50, int(duration_sec * 1000)),
                    fmt_id=SOF_PCM_FMT_S16_LE,
                )

                # Pause DSP at tick boundary (D3 hold release) after telemetry & audio capture
                if not keep_alive:
                    client.pm_control(SOF_PM_OP_RELEASE_HOLD)

                if signal_kind == "remote_tone" and not keep_alive:
                    client.pcm_control(SOF_PCM_OP_STOP_TONE)

                if keep_alive:
                    self.live_proc = proc
                    self.live_client = client
                    self.live_port = port
                else:
                    # 4. Clean Shutdown via SOFR Protocol (`quit`)
                    client.exec_shell("quit")
                    client.close()

            stdout_str = ""
            stderr_str = ""
            if not keep_alive:
                try:
                    stdout_str, stderr_str = proc.communicate(timeout=4.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout_str, stderr_str = proc.communicate()

            # Read output samples from out_raw (or fallback to SOFR captured PCM ring buffer)
            out_norm: List[float] = []
            if os.path.isfile(out_raw) and os.path.getsize(out_raw) > 0:
                out_norm = self._read_raw_normalized(out_raw, channels, bits)
                if len(out_norm) > target_frames:
                    out_norm = out_norm[:target_frames]
            if not out_norm and captured_pcm_bytes:
                out_norm = self._decode_s16_bytes_normalized(captured_pcm_bytes, channels)
                if out_norm:
                    self._write_raw_from_normalized(out_raw, out_norm, channels, bits)

            ok = bool(connected and (len(out_norm) > 0 or (telemetry and telemetry.total_ll_ticks > 0)))
            if ok and out_norm:
                self._write_wav_from_normalized(out_wav, out_norm, rate)

            in_peak, in_rms = self._compute_metrics(in_norm)
            out_peak, out_rms = self._compute_metrics(out_norm)

            telem_badge = ""
            if telemetry:
                active_kcps = sum(c.avg_kcps for c in telemetry.cores if c.enabled)
                telem_badge = (
                    f"  |  SOFR :{port} Ticks={telemetry.total_ll_ticks} "
                    f"KCPS={active_kcps} Heap={telemetry.total_heap_bytes}B "
                    f"DAPM={telemetry.dapm_powered_nodes}/{telemetry.dapm_total_nodes}"
                )

            summary = (
                f"Status: {'PASS' if ok else 'FAIL'}  |  Pipelines: -p {pipe_arg}  |  "
                f"In: {len(in_norm)}f (Peak {in_peak:.1f}, RMS {in_rms:.1f} dBFS) → "
                f"Out: {len(out_norm)}f (Peak {out_peak:.1f}, RMS {out_rms:.1f} dBFS)"
                f"{telem_badge}"
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
                stderr=stderr_str,
                summary_text=summary,
                telemetry=telemetry,
            )
        except Exception as exc:
            return TestbenchRunResult(
                success=False,
                command=cmd_str,
                in_wav_path=in_wav,
                out_wav_path="",
                in_samples=in_norm,
                out_samples=[],
                stdout="",
                stderr=str(exc),
                summary_text=f"Error executing testbench via SOFR protocol: {exc}",
                telemetry=None,
            )

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
    def _read_raw_normalized(raw_path: str, channels: int, bits: int) -> List[float]:
        if not os.path.isfile(raw_path):
            return []
        data = open(raw_path, "rb").read()
        ch = max(1, channels)
        out: List[float] = []
        if bits == 16:
            frame_sz = 2 * ch
            n = len(data) // frame_sz
            for i in range(n):
                vals = struct.unpack_from("<" + "h" * ch, data, i * frame_sz)
                out.append(vals[0] / 32768.0)
        elif bits == 24:
            frame_sz = 4 * ch
            n = len(data) // frame_sz
            for i in range(n):
                vals = struct.unpack_from("<" + "i" * ch, data, i * frame_sz)
                out.append(max(-1.0, min(1.0, vals[0] / 8388608.0)))
        else:
            frame_sz = 4 * ch
            n = len(data) // frame_sz
            for i in range(n):
                vals = struct.unpack_from("<" + "i" * ch, data, i * frame_sz)
                out.append(vals[0] / 2147483648.0)
        return out

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
    """Hardware-accelerated Gtk.Snapshot widget comparing Input vs Output testbench waveforms."""

    __gtype_name__ = "WaveformSnapshotWidget"

    def __init__(self):
        super().__init__()
        self.set_hexpand(True)
        self.set_size_request(-1, 110)
        self.in_samples: List[float] = []
        self.out_samples: List[float] = []
        self.summary_label: str = "Run Testbench to inspect Input vs. Output audio waveforms & SOFR telemetry"

    def set_waveforms(self, in_samples: List[float], out_samples: List[float], summary: str = "") -> None:
        self.in_samples = in_samples
        self.out_samples = out_samples
        if summary:
            self.summary_label = summary
        self.queue_draw()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        w = float(self.get_width())
        h = float(self.get_height())
        if w <= 0 or h <= 0:
            return

        bg = Gdk.RGBA()
        bg.red, bg.green, bg.blue, bg.alpha = (0.07, 0.08, 0.11, 1.0)
        bounds = Graphene.Rect.alloc()
        bounds.init(0.0, 0.0, w, h)
        snapshot.append_color(bg, bounds)

        mid_in_y = h * 0.32
        mid_out_y = h * 0.74
        amp = h * 0.18

        # Draw center zero-lines
        zero_col = Gdk.RGBA()
        zero_col.red, zero_col.green, zero_col.blue, zero_col.alpha = (0.22, 0.26, 0.34, 1.0)
        zpb = Gsk.PathBuilder.new()
        zpb.move_to(0.0, mid_in_y)
        zpb.line_to(w, mid_in_y)
        zpb.move_to(0.0, mid_out_y)
        zpb.line_to(w, mid_out_y)
        snapshot.append_stroke(zpb.to_path(), Gsk.Stroke.new(1.0), zero_col)

        # Draw Input Waveform (Cyan)
        if self.in_samples:
            in_col = Gdk.RGBA()
            in_col.red, in_col.green, in_col.blue, in_col.alpha = (0.28, 0.78, 0.96, 0.95)
            self._draw_trace(snapshot, self.in_samples, w, mid_in_y, amp, in_col)

        # Draw Output Waveform (Emerald Green)
        if self.out_samples:
            out_col = Gdk.RGBA()
            out_col.red, out_col.green, out_col.blue, out_col.alpha = (0.24, 0.92, 0.58, 0.95)
            self._draw_trace(snapshot, self.out_samples, w, mid_out_y, amp, out_col)

        # Overlay labels
        txt_col = Gdk.RGBA()
        txt_col.red, txt_col.green, txt_col.blue, txt_col.alpha = (0.85, 0.90, 0.96, 1.0)
        layout = self.create_pango_layout(self.summary_label)
        layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 8"))
        snapshot.save()
        pt = Graphene.Point.alloc()
        pt.init(10.0, 4.0)
        snapshot.translate(pt)
        snapshot.append_layout(layout, txt_col)
        snapshot.restore()

    @staticmethod
    def _draw_trace(
        snapshot: Gtk.Snapshot,
        samples: List[float],
        w: float,
        center_y: float,
        amp: float,
        color: Gdk.RGBA,
    ) -> None:
        # Plot first ~600 samples so individual sine/chirp cycles are clearly visible
        view_n = min(len(samples), 600)
        if view_n < 2:
            return
        pb = Gsk.PathBuilder.new()
        for i in range(view_n):
            x = (i / float(view_n - 1)) * w
            y = center_y - max(-1.0, min(1.0, samples[i])) * amp
            if i == 0:
                pb.move_to(x, y)
            else:
                pb.line_to(x, y)
        snapshot.append_stroke(pb.to_path(), Gsk.Stroke.new(1.6), color)
