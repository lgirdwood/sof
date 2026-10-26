# SPDX-License-Identifier: BSD-3-Clause
#
# Copyright(c) 2026 Intel Corporation. All rights reserved.

"""Pure-Python ALSA .tplg IPC4 parser and IPC4 message builder for tplg2_gui.

Allows the GUI to act as the host IPC4 driver talking to a persistent interactive
`sof-testbench4` server (started WITHOUT `-t <topology.tplg>`), dynamically
creating, binding, configuring, running, unbinding, and destroying only the
selected pipelines in the Main Window via IPC4 messages (`SOF_NET_OP_IPC_TX`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import struct
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# ALSA Topology UAPI & SOF Token / IPC4 Constants
# ---------------------------------------------------------------------------

SND_SOC_TPLG_TYPE_MIXER = 1
SND_SOC_TPLG_TYPE_BYTES = 2
SND_SOC_TPLG_TYPE_ENUM = 3
SND_SOC_TPLG_TYPE_DAPM_GRAPH = 4
SND_SOC_TPLG_TYPE_DAPM_WIDGET = 5
SND_SOC_TPLG_TYPE_DAI_LINK = 6
SND_SOC_TPLG_TYPE_PCM = 7
SND_SOC_TPLG_TYPE_MANIFEST = 8
SND_SOC_TPLG_TYPE_CODEC_LINK = 9
SND_SOC_TPLG_TYPE_BACKEND_LINK = 10
SND_SOC_TPLG_TYPE_PDATA = 11
SND_SOC_TPLG_TYPE_DAI = 12

SND_SOC_TPLG_DAPM_INPUT = 0
SND_SOC_TPLG_DAPM_OUTPUT = 1
SND_SOC_TPLG_DAPM_MUX = 2
SND_SOC_TPLG_DAPM_MIXER = 3
SND_SOC_TPLG_DAPM_PGA = 4
SND_SOC_TPLG_DAPM_OUT_DRV = 5
SND_SOC_TPLG_DAPM_ADC = 6
SND_SOC_TPLG_DAPM_DAC = 7
SND_SOC_TPLG_DAPM_SWITCH = 8
SND_SOC_TPLG_DAPM_PRE = 9
SND_SOC_TPLG_DAPM_POST = 10
SND_SOC_TPLG_DAPM_AIF_IN = 11
SND_SOC_TPLG_DAPM_AIF_OUT = 12
SND_SOC_TPLG_DAPM_DAI_IN = 13
SND_SOC_TPLG_DAPM_DAI_OUT = 14
SND_SOC_TPLG_DAPM_DAI_LINK = 15
SND_SOC_TPLG_DAPM_BUFFER = 16
SND_SOC_TPLG_DAPM_SCHEDULER = 17
SND_SOC_TPLG_DAPM_EFFECT = 18
SND_SOC_TPLG_DAPM_SIGGEN = 19
SND_SOC_TPLG_DAPM_SRC = 20
SND_SOC_TPLG_DAPM_ASRC = 21

SND_SOC_TPLG_CTL_VOLSW = 1
SND_SOC_TPLG_CTL_VOLSW_SX = 2
SND_SOC_TPLG_CTL_VOLSW_XR_SX = 3
SND_SOC_TPLG_CTL_ENUM = 4
SND_SOC_TPLG_CTL_BYTES = 5
SND_SOC_TPLG_CTL_ENUM_VALUE = 6
SND_SOC_TPLG_CTL_RANGE = 7
SND_SOC_TPLG_CTL_STROBE = 8
SND_SOC_TPLG_DAPM_CTL_VOLSW = 64
SND_SOC_TPLG_DAPM_CTL_ENUM_DOUBLE = 65
SND_SOC_TPLG_DAPM_CTL_ENUM_VIRT = 66
SND_SOC_TPLG_DAPM_CTL_ENUM_VALUE = 67
SND_SOC_TPLG_DAPM_CTL_PIN = 68

SND_SOC_TPLG_TUPLE_TYPE_UUID = 0
SND_SOC_TPLG_TUPLE_TYPE_STRING = 1
SND_SOC_TPLG_TUPLE_TYPE_BOOL = 2
SND_SOC_TPLG_TUPLE_TYPE_BYTE = 3
SND_SOC_TPLG_TUPLE_TYPE_WORD = 4
SND_SOC_TPLG_TUPLE_TYPE_SHORT = 5

# SOF Topology Tokens (from src/include/kernel/tokens.h)
SOF_TKN_SCHED_PRIORITY = 201
SOF_TKN_SCHED_CORE = 203
SOF_TKN_GAIN_RAMP_TYPE = 260
SOF_TKN_GAIN_RAMP_DURATION = 261
SOF_TKN_GAIN_VAL = 262
SOF_TKN_SRC_RATE_OUT = 301
SOF_TKN_ASRC_RATE_OUT = 321
SOF_TKN_ASRC_OPERATION_MODE = 323
SOF_TKN_COMP_CORE_ID = 404
SOF_TKN_COMP_UUID = 405
SOF_TKN_COMP_CPC = 406
SOF_TKN_COMP_IS_PAGES = 409
SOF_TKN_COMP_NUM_INPUT_PINS = 411
SOF_TKN_COMP_NUM_OUTPUT_PINS = 412
SOF_TKN_COMP_INPUT_PIN_BINDING_WNAME = 413
SOF_TKN_COMP_OUTPUT_PIN_BINDING_WNAME = 414
SOF_TKN_COMP_NUM_INPUT_AUDIO_FORMATS = 415
SOF_TKN_COMP_NUM_OUTPUT_AUDIO_FORMATS = 416
SOF_TKN_COMP_SCHED_DOMAIN = 418
SOF_TKN_COMP_DOMAIN_ID = 419
SOF_TKN_COMP_STACK_BYTES = 420
SOF_TKN_COMP_HEAP_BYTES = 421

SOF_TKN_CAVS_AUDIO_FORMAT_IN_RATE = 1900
SOF_TKN_CAVS_AUDIO_FORMAT_IN_BIT_DEPTH = 1901
SOF_TKN_CAVS_AUDIO_FORMAT_IN_CH_MAP = 1904
SOF_TKN_CAVS_AUDIO_FORMAT_IN_CH_CFG = 1905
SOF_TKN_CAVS_AUDIO_FORMAT_IN_INTERLEAVING_STYLE = 1906
SOF_TKN_CAVS_AUDIO_FORMAT_IN_FMT_CFG = 1907
SOF_TKN_CAVS_AUDIO_FORMAT_INPUT_PIN_INDEX = 1909

SOF_TKN_CAVS_AUDIO_FORMAT_OUT_RATE = 1930
SOF_TKN_CAVS_AUDIO_FORMAT_OUT_BIT_DEPTH = 1931
SOF_TKN_CAVS_AUDIO_FORMAT_OUT_CH_MAP = 1934
SOF_TKN_CAVS_AUDIO_FORMAT_OUT_CH_CFG = 1935
SOF_TKN_CAVS_AUDIO_FORMAT_OUT_INTERLEAVING_STYLE = 1936
SOF_TKN_CAVS_AUDIO_FORMAT_OUT_FMT_CFG = 1937
SOF_TKN_CAVS_AUDIO_FORMAT_OUTPUT_PIN_INDEX = 1939

SOF_TKN_CAVS_AUDIO_FORMAT_IBS = 1970
SOF_TKN_CAVS_AUDIO_FORMAT_OBS = 1971

# Testbench Module IDs (from tools/testbench/include/testbench/topology_ipc4.h)
TB_COPIER_MODULE_ID = 0x1
TB_MIXIN_MODULE_ID = 0x2
TB_MIXOUT_MODULE_ID = 0x3
TB_PGA_MODULE_ID = 0x6
TB_SRC_MODULE_ID = 0x7
TB_ASRC_MODULE_ID = 0x8
TB_PROCESS_MODULE_ID = 0x95
TB_FILE_OUT_AIF_MODULE_ID = 0x9A
TB_FILE_IN_AIF_MODULE_ID = 0x9B
TB_FILE_OUT_DAI_MODULE_ID = 0x9C
TB_FILE_IN_DAI_MODULE_ID = 0x9D

# Testbench file component UUID: bfc7488c-75aa-4ce8-9dbe-d8da08a698c2
TB_FILE_UUID_BYTES = struct.pack(
    "<IHH8B",
    0xBFC7488C,
    0x75AA,
    0x4CE8,
    0x9D,
    0xBE,
    0xD8,
    0xDA,
    0x08,
    0xA6,
    0x98,
    0xC2,
)

# Stream direction & file modes
SOF_IPC_STREAM_PLAYBACK = 0
SOF_IPC_STREAM_CAPTURE = 1
FILE_READ = 0
FILE_WRITE = 1

# SOF frame formats (enum sof_ipc_frame)
SOF_IPC_FRAME_S16_LE = 0
SOF_IPC_FRAME_S24_4LE = 1
SOF_IPC_FRAME_S32_LE = 2
SOF_IPC_FRAME_FLOAT = 3

# IPC4 Message Targets & Types
SOF_IPC4_MESSAGE_TARGET_FW_GEN_MSG = 0
SOF_IPC4_MESSAGE_TARGET_MODULE_MSG = 1
SOF_IPC4_MESSAGE_DIR_MSG_REQUEST = 0

SOF_IPC4_GLB_CREATE_PIPELINE = 17
SOF_IPC4_GLB_DELETE_PIPELINE = 18
SOF_IPC4_GLB_SET_PIPELINE_STATE = 19

SOF_IPC4_PIPELINE_STATE_RESET = 2
SOF_IPC4_PIPELINE_STATE_PAUSED = 3
SOF_IPC4_PIPELINE_STATE_RUNNING = 4

SOF_IPC4_MOD_INIT_INSTANCE = 0
SOF_IPC4_MOD_CONFIG_GET = 1
SOF_IPC4_MOD_CONFIG_SET = 2
SOF_IPC4_MOD_LARGE_CONFIG_GET = 3
SOF_IPC4_MOD_LARGE_CONFIG_SET = 4
SOF_IPC4_MOD_BIND = 5
SOF_IPC4_MOD_UNBIND = 6
SOF_IPC4_MOD_DELETE_INSTANCE = 11

IPC4_COPIER_MODULE_CFG_PARAM_SET_SINK_FORMAT = 2
IPC4_VOLUME = 0
SOF_IPC4_SWITCH_CONTROL_PARAM_ID = 200
SOF_IPC4_ENUM_CONTROL_PARAM_ID = 201
IPC4_ALL_CHANNELS_MASK = 0xFFFFFFFF
IPC4_INVALID_NODE_ID = 0xFFFFFFFF
SOF_IPC4_VOL_ZERO_DB = 0x7FFFFFFF

TB_IPC4_MAX_MSG_SIZE = 4096 + 8


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class Ipc4AudioFormat:
    sampling_frequency: int = 48000
    bit_depth: int = 32
    ch_map: int = 0xFFFFFF10
    ch_cfg: int = 1
    interleaving_style: int = 0
    fmt_cfg: int = 0

    @property
    def channels_count(self) -> int:
        return self.fmt_cfg & 0xFF

    @property
    def valid_bit_depth(self) -> int:
        return (self.fmt_cfg >> 8) & 0xFF

    @property
    def s_type(self) -> int:
        return (self.fmt_cfg >> 16) & 0xFF

    def pack(self) -> bytes:
        return struct.pack(
            "<6I",
            self.sampling_frequency,
            self.bit_depth,
            self.ch_map,
            self.ch_cfg,
            self.interleaving_style,
            self.fmt_cfg,
        )


@dataclass
class Ipc4PinFormat:
    pin_index: int = 0
    buffer_size: int = 0
    audio_fmt: Ipc4AudioFormat = field(default_factory=Ipc4AudioFormat)

    def pack(self) -> bytes:
        return struct.pack("<2I", self.pin_index, self.buffer_size) + self.audio_fmt.pack()


@dataclass
class TplgKcontrolDesc:
    name: str
    ctl_type: int          # SND_SOC_TPLG_TYPE_MIXER / BYTES / ENUM
    ops_info: int
    index: int             # Per-widget per-type index
    module_id: int = 0
    instance_id: int = 0
    widget_name: str = ""
    pipeline_id: int = 0
    # Mixer fields
    max_val: int = 0
    num_channels: int = 2
    scale_min: int = 0
    scale_step: int = 0
    scale_mute: int = 0
    volume_table: List[int] = field(default_factory=list)
    curve_type: int = 0
    curve_duration: int = 0
    # Enum fields
    enum_texts: List[str] = field(default_factory=list)
    # Bytes fields (ABI header + payload)
    abi_magic: int = 0
    abi_type: int = 0      # param_id for LARGE_CONFIG_SET
    abi_size: int = 0
    abi_data: bytes = b""

    def default_step_value(self) -> int:
        """Return the 0 dB step index for volume controls, 1 for switches, or 0 for enums."""
        if self.ctl_type == SND_SOC_TPLG_TYPE_MIXER:
            if self.max_val == 1:
                return 1
            if self.scale_min < 0 and self.scale_step > 0:
                return min(self.max_val, max(0, (-self.scale_min) // self.scale_step))
            return self.max_val
        return 0


@dataclass
class TplgPipelineDesc:
    pipeline_id: int
    name: str
    core_id: int = 0
    priority: int = 0
    instance_id: int = 0
    mem_usage: int = 0


@dataclass
class TplgWidgetDesc:
    comp_id: int
    pipeline_id: int
    dapm_type: int
    name: str
    stream_name: str
    module_id: int = 0
    instance_id: int = 0
    core_id: int = 0
    proc_domain: int = 0
    domain_id: int = 0
    stack_bytes: int = 0
    heap_bytes: int = 0
    is_pages: int = 0
    cpc: int = 0
    uuid_bytes: bytes = b"\x00" * 16
    num_input_pins: int = 1
    num_output_pins: int = 1
    input_pin_binding: List[str] = field(default_factory=list)
    output_pin_binding: List[str] = field(default_factory=list)
    input_pin_fmts: List[Ipc4PinFormat] = field(default_factory=list)
    output_pin_fmts: List[Ipc4PinFormat] = field(default_factory=list)
    # Module-specific parsed tokens
    pga_target_volume: int = 0x7FFFFFFF
    pga_curve_type: int = 0
    pga_curve_duration: int = 0
    src_sink_rate: int = 0
    asrc_out_freq: int = 0
    asrc_mode: int = 0
    kcontrols: List[TplgKcontrolDesc] = field(default_factory=list)
    # Computed during prepare pass
    in_audio_fmt: Ipc4AudioFormat = field(default_factory=Ipc4AudioFormat)
    out_audio_fmt: Ipc4AudioFormat = field(default_factory=Ipc4AudioFormat)
    ibs: int = 0
    obs: int = 0
    file_mode: int = FILE_READ
    file_direction: int = SOF_IPC_STREAM_PLAYBACK
    file_slot_1based: int = 1


@dataclass
class TplgRouteDesc:
    source_name: str
    sink_name: str


@dataclass
class TplgPcmDesc:
    pcm_id: int
    name: str
    playback_stream_name: str
    capture_stream_name: str
    playback_host_name: str = ""
    capture_host_name: str = ""


@dataclass
class TplgIpc4Topology:
    pipelines: Dict[int, TplgPipelineDesc] = field(default_factory=dict)
    widgets: List[TplgWidgetDesc] = field(default_factory=list)
    widgets_by_name: Dict[str, TplgWidgetDesc] = field(default_factory=dict)
    routes: List[TplgRouteDesc] = field(default_factory=list)
    pcms: List[TplgPcmDesc] = field(default_factory=list)
    kcontrols: List[TplgKcontrolDesc] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Planned IPC4 Operations for Selected Pipelines
# ---------------------------------------------------------------------------

@dataclass
class IpcPipelineCreateOp:
    pipeline_id: int
    instance_id: int
    name: str
    core_id: int
    priority: int
    mem_pages: int
    direction: int  # SOF_IPC_STREAM_PLAYBACK / CAPTURE
    primary: int
    extension: int


@dataclass
class IpcLargeConfigSetOp:
    description: str
    module_id: int
    instance_id: int
    param_id: int
    chunks: List[Tuple[int, int, bytes]]  # [(primary, extension, payload_chunk), ...]


@dataclass
class IpcWidgetInitOp:
    name: str
    pipeline_id: int
    ppl_instance_id: int
    module_id: int
    instance_id: int
    core_id: int
    proc_domain: int
    primary: int
    extension: int
    payload: bytes
    bytes_ops: List[IpcLargeConfigSetOp] = field(default_factory=list)


@dataclass
class IpcRouteBindOp:
    src_name: str
    sink_name: str
    src_pipeline_id: int
    dst_pipeline_id: int
    src_module_id: int
    src_instance_id: int
    src_queue: int
    dst_module_id: int
    dst_instance_id: int
    dst_queue: int
    bind_primary: int
    bind_extension: int
    unbind_primary: int
    unbind_extension: int
    copier_sink_fmt_op: Optional[IpcLargeConfigSetOp] = None

    @property
    def is_inter_pipeline(self) -> bool:
        return self.src_pipeline_id != self.dst_pipeline_id


@dataclass
class IpcPipelineStepOp:
    """Ordered step in pipeline setup (CREATE_PIPELINE, MOD_INIT, LARGE_CFG, MOD_BIND)."""
    kind: str  # "CREATE_PIPELINE" | "MOD_INIT" | "LARGE_CFG" | "MOD_BIND"
    description: str
    primary: int
    extension: int
    payload: bytes = b""
    pipeline_id: int = 0
    module_id: int = 0
    instance_id: int = 0


@dataclass
class TplgIpc4Plan:
    selected_pipeline_ids: List[int]
    playback_pipelines: List[IpcPipelineCreateOp] = field(default_factory=list)
    capture_pipelines: List[IpcPipelineCreateOp] = field(default_factory=list)
    pipelines_by_id: Dict[int, IpcPipelineCreateOp] = field(default_factory=dict)
    widgets: List[IpcWidgetInitOp] = field(default_factory=list)
    routes: List[IpcRouteBindOp] = field(default_factory=list)
    steps: List[IpcPipelineStepOp] = field(default_factory=list)
    kcontrols: List[TplgKcontrolDesc] = field(default_factory=list)
    input_widgets: List[str] = field(default_factory=list)
    output_widgets: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# IPC4 Bitfield Encoders
# ---------------------------------------------------------------------------

def encode_create_pipeline(
    instance_id: int,
    priority: int,
    mem_pages: int,
    core_id: int,
) -> Tuple[int, int]:
    """Encode struct ipc4_pipeline_create primary and extension dwords."""
    mem_sz = max(1, min(int(mem_pages), 0x7FF))
    prio = int(priority) & 0x1F
    inst = int(instance_id) & 0xFF
    msg_type = SOF_IPC4_GLB_CREATE_PIPELINE & 0x1F
    primary = (
        mem_sz
        | (prio << 11)
        | (inst << 16)
        | (msg_type << 24)
        | (SOF_IPC4_MESSAGE_DIR_MSG_REQUEST << 29)
        | (SOF_IPC4_MESSAGE_TARGET_FW_GEN_MSG << 30)
    )
    extension = (int(core_id) & 0xF) << 20
    return primary, extension


def encode_delete_pipeline(instance_id: int) -> Tuple[int, int]:
    """Encode struct ipc4_pipeline_delete primary and extension dwords."""
    inst = int(instance_id) & 0xFF
    msg_type = SOF_IPC4_GLB_DELETE_PIPELINE & 0x1F
    primary = (
        (inst << 16)
        | (msg_type << 24)
        | (SOF_IPC4_MESSAGE_DIR_MSG_REQUEST << 29)
        | (SOF_IPC4_MESSAGE_TARGET_FW_GEN_MSG << 30)
    )
    return primary, 0


def encode_set_pipeline_state(instance_id: int, state: int) -> Tuple[int, int]:
    """Encode struct ipc4_pipeline_set_state primary and extension dwords."""
    st = int(state) & 0xFFFF
    inst = int(instance_id) & 0xFF
    msg_type = SOF_IPC4_GLB_SET_PIPELINE_STATE & 0x1F
    primary = (
        st
        | (inst << 16)
        | (msg_type << 24)
        | (SOF_IPC4_MESSAGE_DIR_MSG_REQUEST << 29)
        | (SOF_IPC4_MESSAGE_TARGET_FW_GEN_MSG << 30)
    )
    return primary, 0


def encode_mod_init_instance(
    module_id: int,
    instance_id: int,
    ppl_instance_id: int,
    core_id: int,
    proc_domain: int,
    payload: bytes,
) -> Tuple[int, int]:
    """Encode struct ipc4_module_init_instance primary and extension dwords."""
    mod = int(module_id) & 0xFFFF
    inst = int(instance_id) & 0xFF
    msg_type = SOF_IPC4_MOD_INIT_INSTANCE & 0x1F
    primary = (
        mod
        | (inst << 16)
        | (msg_type << 24)
        | (SOF_IPC4_MESSAGE_DIR_MSG_REQUEST << 29)
        | (SOF_IPC4_MESSAGE_TARGET_MODULE_MSG << 30)
    )
    param_dwords = (len(payload) >> 2) & 0xFFFF
    ppl = int(ppl_instance_id) & 0xFF
    core = int(core_id) & 0xF
    dom = 1 if int(proc_domain) else 0
    extension = param_dwords | (ppl << 16) | (core << 24) | (dom << 28)
    return primary, extension


def encode_mod_bind_unbind(
    src_mod: int,
    src_inst: int,
    src_queue: int,
    dst_mod: int,
    dst_inst: int,
    dst_queue: int,
    is_unbind: bool = False,
) -> Tuple[int, int]:
    """Encode struct ipc4_module_bind_unbind primary and extension dwords."""
    msg_type = (SOF_IPC4_MOD_UNBIND if is_unbind else SOF_IPC4_MOD_BIND) & 0x1F
    primary = (
        (int(src_mod) & 0xFFFF)
        | ((int(src_inst) & 0xFF) << 16)
        | (msg_type << 24)
        | (SOF_IPC4_MESSAGE_DIR_MSG_REQUEST << 29)
        | (SOF_IPC4_MESSAGE_TARGET_MODULE_MSG << 30)
    )
    extension = (
        (int(dst_mod) & 0xFFFF)
        | ((int(dst_inst) & 0xFF) << 16)
        | ((int(dst_queue) & 0x7) << 24)
        | ((int(src_queue) & 0x7) << 27)
    )
    return primary, extension


def encode_large_config_set_chunks(
    module_id: int,
    instance_id: int,
    param_id: int,
    data: bytes,
) -> List[Tuple[int, int, bytes]]:
    """Encode one or more SOF_IPC4_MOD_LARGE_CONFIG_SET messages (matching tb_send_bytes_data)."""
    primary = (
        (int(module_id) & 0xFFFF)
        | ((int(instance_id) & 0xFF) << 16)
        | ((SOF_IPC4_MOD_LARGE_CONFIG_SET & 0x1F) << 24)
        | (SOF_IPC4_MESSAGE_DIR_MSG_REQUEST << 29)
        | (SOF_IPC4_MESSAGE_TARGET_MODULE_MSG << 30)
    )
    payload_limit = TB_IPC4_MAX_MSG_SIZE - 8
    total_size = len(data)
    if total_size == 0:
        ext = (int(param_id) & 0xFF) << 20 | (1 << 28) | (1 << 29)
        return [(primary, ext, b"")]

    chunks: List[Tuple[int, int, bytes]] = []
    offset = 0
    remaining = total_size
    while remaining > 0:
        if remaining > payload_limit:
            chunk_size = payload_limit
            final_block = 0
        else:
            chunk_size = remaining
            final_block = 1

        if offset == 0:
            init_block = 1
            data_off_size = total_size & 0xFFFFF
        else:
            init_block = 0
            data_off_size = offset & 0xFFFFF

        extension = (
            data_off_size
            | ((int(param_id) & 0xFF) << 20)
            | (final_block << 28)
            | (init_block << 29)
        )
        chunks.append((primary, extension, data[offset:offset + chunk_size]))
        offset += chunk_size
        remaining -= chunk_size
    return chunks


# ---------------------------------------------------------------------------
# Volume & Control Helpers (matching topology_ipc4.c)
# ---------------------------------------------------------------------------

def _vol_shift_64(i: int, x: int) -> int:
    if x == 0:
        return i & 0xFFFFFFFF
    if x > 32:
        x = 32
    return (((i >> (x - 1)) + 1) >> 1) & 0xFFFFFFFF


def _vol_pow32(a: int, exp: int, fwl: int = 16) -> int:
    power = 1 << fwl
    if exp == 0:
        return power
    iters = -exp if exp < 0 else exp
    for _ in range(iters):
        power = _vol_shift_64(power * a, fwl)
    if exp > 0:
        return power
    numerator = 1 << (fwl << 1)
    return (numerator // power) & 0xFFFFFFFF if power else 0


def _vol_compute_gain(value: int, scale_min: int, scale_step: int, scale_mute: int) -> int:
    if value == 0 and scale_mute:
        return 0
    db_gain = int(scale_min // 100) + int((value * scale_step) // 100)
    linear_gain = _vol_pow32(73533, db_gain, 16)
    f_step = scale_step - (scale_step // 100)
    if f_step == 50 and (value & 1):
        linear_gain = _vol_shift_64(linear_gain * 69419, 16)
    return linear_gain


def build_volume_table(max_val: int, scale_min: int, scale_step: int, scale_mute: int) -> List[int]:
    table: List[int] = []
    for i in range(max(0, max_val) + 1):
        val = _vol_compute_gain(i, scale_min, scale_step, scale_mute)
        q31 = val << 15
        table.append(SOF_IPC4_VOL_ZERO_DB if q31 > SOF_IPC4_VOL_ZERO_DB else q31)
    return table


def encode_kcontrol_set_ipc(
    ctl: TplgKcontrolDesc,
    values: List[int],
) -> List[Tuple[int, int, bytes]]:
    """Build IPC4 LARGE_CONFIG_SET message(s) for a mixer, switch, enum, or bytes kcontrol."""
    if ctl.ctl_type == SND_SOC_TPLG_TYPE_MIXER:
        if ctl.max_val == 1:
            # Switch control (SOF_IPC4_SWITCH_CONTROL_PARAM_ID = 200)
            return _encode_alsa_control_ipc(ctl, values, SOF_IPC4_SWITCH_CONTROL_PARAM_ID)
        # PeakVolume control (IPC4_VOLUME = 0)
        if not values:
            values = [0]
        all_equal = all(v == values[0] for v in values)
        msgs: List[Tuple[int, int, bytes]] = []
        for idx, v in enumerate(values):
            clamped = max(0, min(int(v), ctl.max_val))
            target_vol = (
                ctl.volume_table[clamped]
                if clamped < len(ctl.volume_table)
                else SOF_IPC4_VOL_ZERO_DB
            )
            chan_id = IPC4_ALL_CHANNELS_MASK if all_equal else idx
            vol_payload = struct.pack(
                "<IIIIQ",
                chan_id,
                target_vol,
                ctl.curve_type,
                0,
                ctl.curve_duration,
            )
            msgs.extend(
                encode_large_config_set_chunks(
                    ctl.module_id, ctl.instance_id, IPC4_VOLUME, vol_payload
                )
            )
            if all_equal:
                break
        return msgs
    elif ctl.ctl_type == SND_SOC_TPLG_TYPE_ENUM:
        return _encode_alsa_control_ipc(ctl, values, SOF_IPC4_ENUM_CONTROL_PARAM_ID)
    elif ctl.ctl_type == SND_SOC_TPLG_TYPE_BYTES and ctl.abi_data:
        return encode_large_config_set_chunks(
            ctl.module_id, ctl.instance_id, ctl.abi_type, ctl.abi_data
        )
    return []


def _encode_alsa_control_ipc(
    ctl: TplgKcontrolDesc,
    values: List[int],
    param_id: int,
) -> List[Tuple[int, int, bytes]]:
    if not values:
        values = [0]
    header = struct.pack("<HH4I", ctl.index & 0xFFFF, len(values) & 0xFFFF, 0, 0, 0, 0)
    chan_elems = b"".join(struct.pack("<Ii", idx, int(v)) for idx, v in enumerate(values))
    return encode_large_config_set_chunks(
        ctl.module_id, ctl.instance_id, param_id, header + chan_elems
    )


# ---------------------------------------------------------------------------
# Binary .tplg Parser (matching topology_ipc4.c & tools/tplg_parser/)
# ---------------------------------------------------------------------------

def _cstr(raw: bytes) -> str:
    nul = raw.find(b"\x00")
    if nul >= 0:
        raw = raw[:nul]
    return raw.decode("utf-8", errors="replace").strip()


def _default_cpc_for_widget_name(name: str, proc_domain: int) -> int:
    if not name:
        return 48000 if proc_domain == 1 else 12000
    prefixes = [
        (("host-copier", "dai-copier", "module-copier", "alh-dai-copier"), 1647),
        (("gain", "volume"), 10183),
        (("mixin",), 910),
        (("mixout",), 5000),
        (("eqiir", "eq-iir"), 18500),
        (("eqfir", "eq-fir"), 26000),
        (("src",), 24000),
        (("asrc",), 32000),
        (("multiband_drc",), 48000),
        (("drc",), 22000),
        (("tdfb",), 35000),
        (("crossover",), 16000),
        (("dcblock",), 6400),
        (("micsel", "mux", "demux"), 29000),
        (("aria",), 5000),
        (("stft",), 64000),
        (("mfcc",), 42000),
        (("kpb",), 3500),
        (("smart_amp",), 38000),
    ]
    for keys, cpc in prefixes:
        if any(name.startswith(k) for k in keys):
            return cpc
    return 48000 if proc_domain == 1 else 12000


def _parse_vendor_arrays(priv_bytes: bytes) -> List[Tuple[int, List[Tuple[int, object]]]]:
    """Parse consecutive struct snd_soc_tplg_vendor_array blocks from priv_bytes."""
    arrays: List[Tuple[int, List[Tuple[int, object]]]] = []
    off = 0
    n = len(priv_bytes)
    while off + 12 <= n:
        asize, atype, num_elems = struct.unpack_from("<3I", priv_bytes, off)
        if asize < 12 or off + asize > n:
            break
        elems: List[Tuple[int, object]] = []
        eoff = off + 12
        if atype == SND_SOC_TPLG_TUPLE_TYPE_UUID:
            for _ in range(num_elems):
                if eoff + 20 > off + asize:
                    break
                tkn = struct.unpack_from("<I", priv_bytes, eoff)[0]
                uuid_b = priv_bytes[eoff + 4:eoff + 20]
                elems.append((tkn, uuid_b))
                eoff += 20
        elif atype == SND_SOC_TPLG_TUPLE_TYPE_STRING:
            for _ in range(num_elems):
                if eoff + 48 > off + asize:
                    break
                tkn = struct.unpack_from("<I", priv_bytes, eoff)[0]
                sval = _cstr(priv_bytes[eoff + 4:eoff + 48])
                elems.append((tkn, sval))
                eoff += 48
        elif atype in (
            SND_SOC_TPLG_TUPLE_TYPE_BOOL,
            SND_SOC_TPLG_TUPLE_TYPE_BYTE,
            SND_SOC_TPLG_TUPLE_TYPE_WORD,
            SND_SOC_TPLG_TUPLE_TYPE_SHORT,
        ):
            for _ in range(num_elems):
                if eoff + 8 > off + asize:
                    break
                tkn, wval = struct.unpack_from("<2I", priv_bytes, eoff)
                elems.append((tkn, wval))
                eoff += 8
        arrays.append((atype, elems))
        off += asize
    return arrays


def _parse_token_sets_word(
    arrays: List[Tuple[int, List[Tuple[int, object]]]],
    token_set: set,
    num_sets: int,
) -> List[Dict[int, int]]:
    """Replicate sof_parse_token_sets() for word token sets."""
    if num_sets <= 0:
        return []
    results: List[Dict[int, int]] = [{} for _ in range(num_sets)]
    count = len(token_set)
    total = 0
    set_idx = 0
    for atype, elems in arrays:
        if total >= count * num_sets or set_idx >= num_sets:
            break
        if atype not in (
            SND_SOC_TPLG_TUPLE_TYPE_BOOL,
            SND_SOC_TPLG_TUPLE_TYPE_BYTE,
            SND_SOC_TPLG_TUPLE_TYPE_WORD,
            SND_SOC_TPLG_TUPLE_TYPE_SHORT,
        ):
            continue
        found = 0
        for tkn, val in elems:
            if tkn in token_set:
                results[set_idx][tkn] = int(val)
                found += 1
        if found > 0:
            total += count
            set_idx += 1
    return results


def _parse_token_sets_string(
    arrays: List[Tuple[int, List[Tuple[int, object]]]],
    target_token: int,
    num_sets: int,
) -> List[str]:
    if num_sets <= 0:
        return []
    results: List[str] = []
    for atype, elems in arrays:
        if len(results) >= num_sets:
            break
        if atype != SND_SOC_TPLG_TUPLE_TYPE_STRING:
            continue
        for tkn, val in elems:
            if tkn == target_token:
                results.append(str(val))
                break
    return results


class TplgIpc4Parser:
    """Parses a compiled ALSA `.tplg` file into IPC4 widget, pipeline, route, and PCM descriptors."""

    @staticmethod
    def parse_file(tplg_path: str) -> TplgIpc4Topology:
        with open(tplg_path, "rb") as f:
            data = f.read()
        return TplgIpc4Parser.parse_bytes(data)

    @staticmethod
    def parse_bytes(data: bytes) -> TplgIpc4Topology:
        tplg = TplgIpc4Topology()
        instance_ids: Dict[int, int] = {}
        comp_id = 1
        off = 0
        size = len(data)

        while off + 36 <= size:
            (
                _magic,
                _abi,
                _version,
                hdr_type,
                hdr_size,
                _vendor_type,
                payload_size,
                hdr_index,
                hdr_count,
            ) = struct.unpack_from("<9I", data, off)
            if hdr_size != 36 or off + 36 + payload_size > size:
                break
            block_end = off + 36 + payload_size
            off += 36

            if hdr_type == SND_SOC_TPLG_TYPE_DAPM_WIDGET:
                pipeline_id = hdr_index
                for _ in range(hdr_count):
                    if off + 132 > block_end:
                        break
                    w_size, w_id = struct.unpack_from("<2I", data, off)
                    w_name = _cstr(data[off + 8:off + 52])
                    w_sname = _cstr(data[off + 52:off + 96])
                    num_kctls, priv_size = struct.unpack_from("<2I", data, off + 124)
                    priv_start = off + 132
                    priv_end = priv_start + priv_size
                    if priv_end > block_end:
                        break
                    priv_bytes = data[priv_start:priv_end]
                    off = priv_end

                    arrays = _parse_vendor_arrays(priv_bytes)
                    word_map: Dict[int, int] = {}
                    str_map: Dict[int, str] = {}
                    uuid_val = b"\x00" * 16
                    for atype, elems in arrays:
                        if atype == SND_SOC_TPLG_TUPLE_TYPE_UUID:
                            for tkn, uval in elems:
                                if tkn == SOF_TKN_COMP_UUID:
                                    uuid_val = bytes(uval)
                        elif atype == SND_SOC_TPLG_TUPLE_TYPE_STRING:
                            for tkn, sval in elems:
                                str_map[tkn] = str(sval)
                        else:
                            for tkn, vval in elems:
                                word_map[tkn] = int(vval)

                    if w_id == SND_SOC_TPLG_DAPM_SCHEDULER:
                        p_prio = word_map.get(SOF_TKN_SCHED_PRIORITY, 0)
                        p_core = word_map.get(SOF_TKN_SCHED_CORE, 0)
                        tplg.pipelines[pipeline_id] = TplgPipelineDesc(
                            pipeline_id=pipeline_id,
                            name=w_name,
                            core_id=p_core,
                            priority=p_prio,
                            instance_id=pipeline_id,
                        )
                        # Consume any attached kcontrols if present
                        off, _ = TplgIpc4Parser._parse_widget_controls(
                            data, off, block_end, num_kctls, None, tplg
                        )
                        comp_id += 1
                        continue

                    proc_dom = (
                        1
                        if str_map.get(SOF_TKN_COMP_SCHED_DOMAIN, "LL").upper() == "DP"
                        else 0
                    )
                    cpc = word_map.get(SOF_TKN_COMP_CPC, 0)
                    if cpc == 0:
                        cpc = _default_cpc_for_widget_name(w_name, proc_dom)

                    wdesc = TplgWidgetDesc(
                        comp_id=comp_id,
                        pipeline_id=pipeline_id,
                        dapm_type=w_id,
                        name=w_name,
                        stream_name=w_sname,
                        core_id=word_map.get(SOF_TKN_COMP_CORE_ID, 0),
                        proc_domain=proc_dom,
                        domain_id=word_map.get(SOF_TKN_COMP_DOMAIN_ID, 0),
                        stack_bytes=word_map.get(SOF_TKN_COMP_STACK_BYTES, 0),
                        heap_bytes=word_map.get(SOF_TKN_COMP_HEAP_BYTES, 0),
                        is_pages=word_map.get(SOF_TKN_COMP_IS_PAGES, 0),
                        cpc=cpc,
                        uuid_bytes=uuid_val,
                        num_input_pins=word_map.get(SOF_TKN_COMP_NUM_INPUT_PINS, 1) or 1,
                        num_output_pins=word_map.get(SOF_TKN_COMP_NUM_OUTPUT_PINS, 1) or 1,
                        pga_target_volume=word_map.get(SOF_TKN_GAIN_VAL, 0x7FFFFFFF),
                        pga_curve_type=word_map.get(SOF_TKN_GAIN_RAMP_TYPE, 0),
                        pga_curve_duration=word_map.get(SOF_TKN_GAIN_RAMP_DURATION, 0),
                        src_sink_rate=word_map.get(SOF_TKN_SRC_RATE_OUT, 0),
                        asrc_out_freq=word_map.get(SOF_TKN_ASRC_RATE_OUT, 0),
                        asrc_mode=word_map.get(SOF_TKN_ASRC_OPERATION_MODE, 0),
                    )

                    # Parse pin audio formats & bindings
                    num_in_fmts = word_map.get(SOF_TKN_COMP_NUM_INPUT_AUDIO_FORMATS, 0)
                    num_out_fmts = word_map.get(SOF_TKN_COMP_NUM_OUTPUT_AUDIO_FORMATS, 0)
                    in_fmt_sets = _parse_token_sets_word(
                        arrays,
                        {
                            SOF_TKN_CAVS_AUDIO_FORMAT_IN_RATE,
                            SOF_TKN_CAVS_AUDIO_FORMAT_IN_BIT_DEPTH,
                            SOF_TKN_CAVS_AUDIO_FORMAT_IN_CH_MAP,
                            SOF_TKN_CAVS_AUDIO_FORMAT_IN_CH_CFG,
                            SOF_TKN_CAVS_AUDIO_FORMAT_IN_INTERLEAVING_STYLE,
                            SOF_TKN_CAVS_AUDIO_FORMAT_IN_FMT_CFG,
                            SOF_TKN_CAVS_AUDIO_FORMAT_INPUT_PIN_INDEX,
                            SOF_TKN_CAVS_AUDIO_FORMAT_IBS,
                        },
                        num_in_fmts,
                    )
                    for fset in in_fmt_sets:
                        wdesc.input_pin_fmts.append(
                            Ipc4PinFormat(
                                pin_index=fset.get(SOF_TKN_CAVS_AUDIO_FORMAT_INPUT_PIN_INDEX, 0),
                                buffer_size=fset.get(SOF_TKN_CAVS_AUDIO_FORMAT_IBS, 0),
                                audio_fmt=Ipc4AudioFormat(
                                    sampling_frequency=fset.get(SOF_TKN_CAVS_AUDIO_FORMAT_IN_RATE, 48000),
                                    bit_depth=fset.get(SOF_TKN_CAVS_AUDIO_FORMAT_IN_BIT_DEPTH, 32),
                                    ch_map=fset.get(SOF_TKN_CAVS_AUDIO_FORMAT_IN_CH_MAP, 0xFFFFFF10),
                                    ch_cfg=fset.get(SOF_TKN_CAVS_AUDIO_FORMAT_IN_CH_CFG, 1),
                                    interleaving_style=fset.get(SOF_TKN_CAVS_AUDIO_FORMAT_IN_INTERLEAVING_STYLE, 0),
                                    fmt_cfg=fset.get(SOF_TKN_CAVS_AUDIO_FORMAT_IN_FMT_CFG, 0),
                                ),
                            )
                        )

                    out_fmt_sets = _parse_token_sets_word(
                        arrays,
                        {
                            SOF_TKN_CAVS_AUDIO_FORMAT_OUT_RATE,
                            SOF_TKN_CAVS_AUDIO_FORMAT_OUT_BIT_DEPTH,
                            SOF_TKN_CAVS_AUDIO_FORMAT_OUT_CH_MAP,
                            SOF_TKN_CAVS_AUDIO_FORMAT_OUT_CH_CFG,
                            SOF_TKN_CAVS_AUDIO_FORMAT_OUT_INTERLEAVING_STYLE,
                            SOF_TKN_CAVS_AUDIO_FORMAT_OUT_FMT_CFG,
                            SOF_TKN_CAVS_AUDIO_FORMAT_OUTPUT_PIN_INDEX,
                            SOF_TKN_CAVS_AUDIO_FORMAT_OBS,
                        },
                        num_out_fmts,
                    )
                    for fset in out_fmt_sets:
                        wdesc.output_pin_fmts.append(
                            Ipc4PinFormat(
                                pin_index=fset.get(SOF_TKN_CAVS_AUDIO_FORMAT_OUTPUT_PIN_INDEX, 0),
                                buffer_size=fset.get(SOF_TKN_CAVS_AUDIO_FORMAT_OBS, 0),
                                audio_fmt=Ipc4AudioFormat(
                                    sampling_frequency=fset.get(SOF_TKN_CAVS_AUDIO_FORMAT_OUT_RATE, 48000),
                                    bit_depth=fset.get(SOF_TKN_CAVS_AUDIO_FORMAT_OUT_BIT_DEPTH, 32),
                                    ch_map=fset.get(SOF_TKN_CAVS_AUDIO_FORMAT_OUT_CH_MAP, 0xFFFFFF10),
                                    ch_cfg=fset.get(SOF_TKN_CAVS_AUDIO_FORMAT_OUT_CH_CFG, 1),
                                    interleaving_style=fset.get(SOF_TKN_CAVS_AUDIO_FORMAT_OUT_INTERLEAVING_STYLE, 0),
                                    fmt_cfg=fset.get(SOF_TKN_CAVS_AUDIO_FORMAT_OUT_FMT_CFG, 0),
                                ),
                            )
                        )

                    if wdesc.num_input_pins > 1:
                        wdesc.input_pin_binding = _parse_token_sets_string(
                            arrays, SOF_TKN_COMP_INPUT_PIN_BINDING_WNAME, wdesc.num_input_pins
                        )
                    if wdesc.num_output_pins > 1:
                        wdesc.output_pin_binding = _parse_token_sets_string(
                            arrays, SOF_TKN_COMP_OUTPUT_PIN_BINDING_WNAME, wdesc.num_output_pins
                        )

                    # Assign module_id and instance_id exactly like topology_ipc4.c
                    if w_id == SND_SOC_TPLG_DAPM_PGA:
                        wdesc.module_id = TB_PGA_MODULE_ID
                        wdesc.instance_id = instance_ids.get(SND_SOC_TPLG_DAPM_PGA, 0)
                        instance_ids[SND_SOC_TPLG_DAPM_PGA] = wdesc.instance_id + 1
                    elif w_id == SND_SOC_TPLG_DAPM_AIF_IN:
                        wdesc.module_id = TB_FILE_OUT_AIF_MODULE_ID
                        wdesc.instance_id = instance_ids.get(SND_SOC_TPLG_DAPM_AIF_IN, 0)
                        instance_ids[SND_SOC_TPLG_DAPM_AIF_IN] = wdesc.instance_id + 1
                        wdesc.file_mode = FILE_READ
                        wdesc.file_direction = SOF_IPC_STREAM_PLAYBACK
                        wdesc.uuid_bytes = TB_FILE_UUID_BYTES
                    elif w_id == SND_SOC_TPLG_DAPM_AIF_OUT:
                        wdesc.module_id = TB_FILE_IN_AIF_MODULE_ID
                        wdesc.instance_id = instance_ids.get(SND_SOC_TPLG_DAPM_AIF_OUT, 0)
                        instance_ids[SND_SOC_TPLG_DAPM_AIF_OUT] = wdesc.instance_id + 1
                        wdesc.file_mode = FILE_WRITE
                        wdesc.file_direction = SOF_IPC_STREAM_CAPTURE
                        wdesc.uuid_bytes = TB_FILE_UUID_BYTES
                    elif w_id == SND_SOC_TPLG_DAPM_DAI_IN:
                        wdesc.module_id = TB_FILE_OUT_DAI_MODULE_ID
                        wdesc.instance_id = instance_ids.get(SND_SOC_TPLG_DAPM_DAI_IN, 0)
                        instance_ids[SND_SOC_TPLG_DAPM_DAI_IN] = wdesc.instance_id + 1
                        wdesc.file_mode = FILE_WRITE
                        wdesc.file_direction = SOF_IPC_STREAM_PLAYBACK
                        wdesc.uuid_bytes = TB_FILE_UUID_BYTES
                    elif w_id == SND_SOC_TPLG_DAPM_DAI_OUT:
                        wdesc.module_id = TB_FILE_IN_DAI_MODULE_ID
                        wdesc.instance_id = instance_ids.get(SND_SOC_TPLG_DAPM_DAI_OUT, 0)
                        instance_ids[SND_SOC_TPLG_DAPM_DAI_OUT] = wdesc.instance_id + 1
                        wdesc.file_mode = FILE_READ
                        wdesc.file_direction = SOF_IPC_STREAM_CAPTURE
                        wdesc.uuid_bytes = TB_FILE_UUID_BYTES
                    elif w_id == SND_SOC_TPLG_DAPM_BUFFER:
                        wdesc.module_id = TB_COPIER_MODULE_ID
                        wdesc.instance_id = instance_ids.get(SND_SOC_TPLG_DAPM_BUFFER, 0)
                        instance_ids[SND_SOC_TPLG_DAPM_BUFFER] = wdesc.instance_id + 1
                    elif w_id == SND_SOC_TPLG_DAPM_SRC:
                        wdesc.module_id = TB_SRC_MODULE_ID
                        wdesc.instance_id = instance_ids.get(SND_SOC_TPLG_DAPM_EFFECT, 0)
                        instance_ids[SND_SOC_TPLG_DAPM_EFFECT] = wdesc.instance_id + 1
                    elif w_id == SND_SOC_TPLG_DAPM_ASRC:
                        wdesc.module_id = TB_ASRC_MODULE_ID
                        wdesc.instance_id = instance_ids.get(SND_SOC_TPLG_DAPM_EFFECT, 0)
                        instance_ids[SND_SOC_TPLG_DAPM_EFFECT] = wdesc.instance_id + 1
                    elif w_id == SND_SOC_TPLG_DAPM_MIXER:
                        wdesc.module_id = (
                            TB_MIXIN_MODULE_ID if "mixin" in w_name else TB_MIXOUT_MODULE_ID
                        )
                        wdesc.instance_id = instance_ids.get(SND_SOC_TPLG_DAPM_MIXER, 0)
                        instance_ids[SND_SOC_TPLG_DAPM_MIXER] = wdesc.instance_id + 1
                    elif w_id == SND_SOC_TPLG_DAPM_EFFECT:
                        _unsupported_tb_prefixes = (
                            "smart_amp",
                            "maxim_dsm",
                            "dts",
                            "kpb",
                            "ov_noise_suppression",
                            "wov",
                        )
                        _unsupported_tb_uuids = {
                            bytes.fromhex("1e967a16e48aea1189f1000c29ce1635"),
                            bytes.fromhex("6838be396ccb334694472379089c592f"),
                            bytes.fromhex("b9809b625c141a4692f0c20369422784"),
                            bytes.fromhex("a2423bc65a974e4b89fd4c19c39c8622"),
                            bytes.fromhex("4fc35fd90f37c74abc86bfdc83f169a1"),
                            bytes.fromhex("0f8421d82d5f4c4ab46842c24d262e12"),
                            bytes.fromhex("438421d8f35f4c4ab3886cfe07b956aa"),
                            bytes.fromhex("329ab5f7a5559f4c9004984c47678791"),
                            bytes.fromhex("a771e67a1746094abf6d9d2975b68791"),
                            bytes.fromhex("f9401882d5f24a4cb46842c24d262e12"),
                            bytes.fromhex("ec7218a3e5c0464a9084f1f7c95340d2"),
                        }
                        _is_unsupported_effect = (
                            wdesc.uuid_bytes in _unsupported_tb_uuids
                            or any(w_name.startswith(pfx) for pfx in _unsupported_tb_prefixes)
                        )
                        if _is_unsupported_effect:
                            wdesc.dapm_type = SND_SOC_TPLG_DAPM_BUFFER
                            wdesc.module_id = TB_COPIER_MODULE_ID
                            wdesc.uuid_bytes = bytes.fromhex("830ca09b12ca834a943c1fa2e82f9dda")
                            wdesc.instance_id = instance_ids.get(SND_SOC_TPLG_DAPM_BUFFER, 0)
                            instance_ids[SND_SOC_TPLG_DAPM_BUFFER] = wdesc.instance_id + 1
                        else:
                            wdesc.module_id = TB_PROCESS_MODULE_ID
                            wdesc.instance_id = instance_ids.get(SND_SOC_TPLG_DAPM_EFFECT, 0)
                            instance_ids[SND_SOC_TPLG_DAPM_EFFECT] = wdesc.instance_id + 1

                    # Parse widget kcontrols
                    off, w_ctls = TplgIpc4Parser._parse_widget_controls(
                        data, off, block_end, num_kctls, wdesc, tplg
                    )
                    if w_id == SND_SOC_TPLG_DAPM_EFFECT and wdesc.dapm_type == SND_SOC_TPLG_DAPM_BUFFER:
                        w_ctls = [c for c in w_ctls if c.ctl_type != SND_SOC_TPLG_TYPE_BYTES]
                    wdesc.kcontrols = w_ctls
                    tplg.widgets.append(wdesc)
                    tplg.widgets_by_name[wdesc.name] = wdesc
                    comp_id += 1

            elif hdr_type == SND_SOC_TPLG_TYPE_DAPM_GRAPH:
                for _ in range(hdr_count):
                    if off + 132 > block_end:
                        break
                    sink_name = _cstr(data[off:off + 44])
                    source_name = _cstr(data[off + 88:off + 132])
                    off += 132
                    if source_name and sink_name:
                        tplg.routes.append(
                            TplgRouteDesc(source_name=source_name, sink_name=sink_name)
                        )

            elif hdr_type == SND_SOC_TPLG_TYPE_PCM:
                for _ in range(hdr_count):
                    if off + 912 > block_end:
                        break
                    pcm_name = _cstr(data[off + 4:off + 48])
                    pcm_id = struct.unpack_from("<I", data, off + 92)[0]
                    pb_sname = _cstr(data[off + 692 + 4:off + 692 + 48])
                    cap_sname = _cstr(data[off + 692 + 104 + 4:off + 692 + 104 + 48])
                    pcm_priv_size = struct.unpack_from("<I", data, off + 908)[0]
                    off += 912 + pcm_priv_size
                    pdesc = TplgPcmDesc(
                        pcm_id=pcm_id,
                        name=pcm_name,
                        playback_stream_name=pb_sname,
                        capture_stream_name=cap_sname,
                    )
                    for w in tplg.widgets:
                        if w.dapm_type in (SND_SOC_TPLG_DAPM_AIF_IN, SND_SOC_TPLG_DAPM_AIF_OUT):
                            if pb_sname and w.stream_name == pb_sname:
                                pdesc.playback_host_name = w.name
                            if cap_sname and w.stream_name == cap_sname:
                                pdesc.capture_host_name = w.name
                    tplg.pcms.append(pdesc)

            off = block_end

        # Assign pipeline core_id to LL widgets matching topology_ipc4.c lines 1858-1879
        for w in tplg.widgets:
            p = tplg.pipelines.get(w.pipeline_id)
            if p is not None and w.proc_domain == 0:
                w.core_id = p.core_id

        return tplg

    @staticmethod
    def _parse_widget_controls(
        data: bytes,
        off: int,
        block_end: int,
        num_kctls: int,
        wdesc: Optional[TplgWidgetDesc],
        tplg: TplgIpc4Topology,
    ) -> Tuple[int, List[TplgKcontrolDesc]]:
        ctls: List[TplgKcontrolDesc] = []
        num_mixers = 0
        num_enums = 0
        num_bytes = 0

        for _ in range(num_kctls):
            if off + 204 > block_end:
                break
            _csize, ctype = struct.unpack_from("<2I", data, off)
            cname = _cstr(data[off + 8:off + 52])
            ops_info = struct.unpack_from("<I", data, off + 64)[0]

            if ops_info in (
                SND_SOC_TPLG_CTL_VOLSW,
                SND_SOC_TPLG_CTL_STROBE,
                SND_SOC_TPLG_CTL_VOLSW_SX,
                SND_SOC_TPLG_CTL_VOLSW_XR_SX,
                SND_SOC_TPLG_CTL_RANGE,
                SND_SOC_TPLG_DAPM_CTL_VOLSW,
            ):
                if off + 360 > block_end:
                    break
                idx = num_mixers
                num_mixers += 1
                max_val = struct.unpack_from("<i", data, off + 212)[0]
                num_ch = struct.unpack_from("<i", data, off + 224)[0]
                cpriv_sz = struct.unpack_from("<I", data, off + 356)[0]
                scale_min, scale_step, scale_mute = struct.unpack_from("<iII", data, off + 76)
                off += 360 + cpriv_sz
                if wdesc is not None and ops_info in (
                    SND_SOC_TPLG_CTL_VOLSW,
                    SND_SOC_TPLG_CTL_VOLSW_SX,
                    SND_SOC_TPLG_CTL_VOLSW_XR_SX,
                ):
                    vol_tbl = build_volume_table(max_val, scale_min, scale_step, scale_mute)
                    kctl = TplgKcontrolDesc(
                        name=cname,
                        ctl_type=ctype,
                        ops_info=ops_info,
                        index=idx,
                        module_id=wdesc.module_id,
                        instance_id=wdesc.instance_id,
                        widget_name=wdesc.name,
                        pipeline_id=wdesc.pipeline_id,
                        max_val=max_val,
                        num_channels=max(1, num_ch),
                        scale_min=scale_min,
                        scale_step=scale_step,
                        scale_mute=scale_mute,
                        volume_table=vol_tbl,
                        curve_type=wdesc.pga_curve_type,
                        curve_duration=wdesc.pga_curve_duration,
                    )
                    ctls.append(kctl)
                    tplg.kcontrols.append(kctl)

            elif ops_info in (
                SND_SOC_TPLG_CTL_ENUM,
                SND_SOC_TPLG_CTL_ENUM_VALUE,
                SND_SOC_TPLG_DAPM_CTL_ENUM_DOUBLE,
                SND_SOC_TPLG_DAPM_CTL_ENUM_VIRT,
                SND_SOC_TPLG_DAPM_CTL_ENUM_VALUE,
            ):
                if off + 1764 > block_end:
                    break
                idx = num_enums
                num_enums += 1
                num_items = struct.unpack_from("<i", data, off + 208)[0]
                texts: List[str] = []
                for ti in range(max(0, min(num_items, 16))):
                    t_str = _cstr(data[off + 352 + ti * 64:off + 352 + (ti + 1) * 64])
                    if t_str:
                        texts.append(t_str)
                cpriv_sz = struct.unpack_from("<I", data, off + 1760)[0]
                off += 1764 + cpriv_sz
                if wdesc is not None:
                    kctl = TplgKcontrolDesc(
                        name=cname,
                        ctl_type=ctype,
                        ops_info=ops_info,
                        index=idx,
                        module_id=wdesc.module_id,
                        instance_id=wdesc.instance_id,
                        widget_name=wdesc.name,
                        pipeline_id=wdesc.pipeline_id,
                        enum_texts=texts,
                    )
                    ctls.append(kctl)
                    tplg.kcontrols.append(kctl)

            elif ops_info == SND_SOC_TPLG_CTL_BYTES:
                if off + 240 > block_end:
                    break
                idx = num_bytes
                num_bytes += 1
                cpriv_sz = struct.unpack_from("<I", data, off + 236)[0]
                priv_data = data[off + 240:off + 240 + cpriv_sz]
                off += 240 + cpriv_sz
                if wdesc is not None and len(priv_data) >= 32:
                    abi_magic, abi_type, abi_size, _abi_ver = struct.unpack_from(
                        "<4I", priv_data, 0
                    )
                    if 32 + abi_size <= len(priv_data):
                        kctl = TplgKcontrolDesc(
                            name=cname,
                            ctl_type=ctype,
                            ops_info=ops_info,
                            index=idx,
                            module_id=wdesc.module_id,
                            instance_id=wdesc.instance_id,
                            widget_name=wdesc.name,
                            pipeline_id=wdesc.pipeline_id,
                            abi_magic=abi_magic,
                            abi_type=abi_type,
                            abi_size=abi_size,
                            abi_data=priv_data[32:32 + abi_size],
                        )
                        ctls.append(kctl)
                        tplg.kcontrols.append(kctl)
            else:
                break

        return off, ctls


# ---------------------------------------------------------------------------
# IPC4 Plan Builder for Selected Pipelines (matching topology_ipc4.c / utils_ipc4.c)
# ---------------------------------------------------------------------------

def _is_single_format(fmts: List[Ipc4PinFormat]) -> bool:
    if not fmts:
        return False
    f0 = fmts[0].audio_fmt
    for f in fmts[1:]:
        af = f.audio_fmt
        if (
            af.sampling_frequency != f0.sampling_frequency
            or af.channels_count != f0.channels_count
            or af.valid_bit_depth != f0.valid_bit_depth
        ):
            return False
    return True


def _find_pin_format(
    fmts: List[Ipc4PinFormat],
    pin_index: int,
    target_rate: int,
    target_channels: int,
    target_valid_bits: int,
) -> Optional[Ipc4PinFormat]:
    if not fmts:
        return None
    if pin_index == 0 and _is_single_format(fmts):
        return fmts[0]
    for f in fmts:
        if f.pin_index != pin_index:
            continue
        af = f.audio_fmt
        if (
            af.sampling_frequency == target_rate
            and af.channels_count == target_channels
            and af.valid_bit_depth == target_valid_bits
        ):
            return f
    for f in fmts:
        if f.pin_index != pin_index:
            continue
        af = f.audio_fmt
        if af.sampling_frequency == target_rate and af.channels_count == target_channels:
            return f
    for f in fmts:
        if f.pin_index != pin_index:
            continue
        if f.audio_fmt.channels_count == target_channels:
            return f
    for f in fmts:
        if f.pin_index == pin_index:
            return f
    return fmts[0]


def _calc_pipeline_widget_mem_pages(ibs: int, is_pages: int) -> int:
    """Replicate tb_pipeline_update_resource_usage() from topology_ipc4.c."""
    def fw_roundup(x: int) -> int:
        return (x + 63) & ~63

    def fw_page(x: int) -> int:
        return ((x + 4095) & ~4095) >> 12

    task_mem = 448 + 12 + is_pages
    task_mem += fw_roundup(72) + 8 * 12 + 12
    ibs_r = fw_roundup(ibs)
    queue_mem = 8 * (128 + ibs_r)
    return fw_page(task_mem + queue_mem)


def _format_name_to_sof_frame(fmt_name: str) -> Tuple[int, int]:
    norm = (fmt_name or "S32_LE").strip().upper()
    if norm in ("S16_LE", "S16LE"):
        return SOF_IPC_FRAME_S16_LE, 16
    if norm in ("S24_LE", "S24_4LE", "S24LE"):
        return SOF_IPC_FRAME_S24_4LE, 24
    return SOF_IPC_FRAME_S32_LE, 32


def build_ipc4_plan(
    tplg: TplgIpc4Topology,
    selected_pipeline_ids: List[int],
    fs_in: int = 48000,
    fs_out: int = 48000,
    channels_in: int = 2,
    channels_out: int = 2,
    frame_fmt_name: str = "S32_LE",
) -> TplgIpc4Plan:
    """Build the ordered IPC4 creation, initialization, config, and bind plan for selected_pipeline_ids."""
    enabled_set = set(int(p) for p in selected_pipeline_ids)
    plan = TplgIpc4Plan(selected_pipeline_ids=list(selected_pipeline_ids))
    frame_fmt, valid_bits = _format_name_to_sof_frame(frame_fmt_name)
    period_frames = max(1, fs_in // 1000)

    # Assign 1-based file slots for enabled AIF/DAI file widgets in selected_pipeline_ids order
    in_slot = 1
    out_slot = 1
    ordered_pids: List[int] = []
    for p in selected_pipeline_ids:
        ip = int(p)
        if ip in enabled_set and ip not in ordered_pids:
            ordered_pids.append(ip)
    for pid in ordered_pids:
        for w in tplg.widgets:
            if w.pipeline_id != pid:
                continue
            if w.dapm_type in (SND_SOC_TPLG_DAPM_AIF_IN, SND_SOC_TPLG_DAPM_DAI_OUT):
                w.file_slot_1based = min(16, in_slot)
                plan.input_widgets.append(w.name)
                in_slot += 1
            elif w.dapm_type in (SND_SOC_TPLG_DAPM_AIF_OUT, SND_SOC_TPLG_DAPM_DAI_IN):
                w.file_slot_1based = min(16, out_slot)
                plan.output_widgets.append(w.name)
                out_slot += 1

    # Track per-pipeline mem_usage and direction
    pipe_mem: Dict[int, int] = {pid: 0 for pid in enabled_set}
    pipe_dir: Dict[int, int] = {}
    playback_pipe_order: List[int] = []
    capture_pipe_order: List[int] = []
    prepared_widgets: set = set()

    def prepare_widget(w: TplgWidgetDesc, direction: int) -> None:
        if w.module_id == 0:
            return
        if w.name not in prepared_widgets:
            fmt = _find_pin_format(w.input_pin_fmts, 0, fs_in, channels_in, valid_bits)
            out_fmt = (
                _find_pin_format(w.output_pin_fmts, 0, fs_in, channels_in, valid_bits)
                if w.output_pin_fmts
                else None
            )
            if w.dapm_type == SND_SOC_TPLG_DAPM_DAI_OUT and fmt and out_fmt:
                if fmt.audio_fmt.valid_bit_depth != valid_bits and out_fmt.audio_fmt.valid_bit_depth == valid_bits:
                    fmt = out_fmt

            if fmt is not None:
                w.in_audio_fmt = fmt.audio_fmt
                if fmt.buffer_size > 0:
                    w.ibs = fmt.buffer_size
                else:
                    sbytes = max(2, fmt.audio_fmt.bit_depth >> 3)
                    w.ibs = 2 * period_frames * max(1, fmt.audio_fmt.channels_count) * sbytes
            else:
                sbytes = 2 if valid_bits == 16 else 4
                w.in_audio_fmt = Ipc4AudioFormat(
                    sampling_frequency=fs_in,
                    bit_depth=sbytes * 8,
                    ch_map=0xFFFFFF10,
                    ch_cfg=1,
                    interleaving_style=0,
                    fmt_cfg=(channels_in & 0xFF) | ((valid_bits & 0xFF) << 8),
                )
                w.ibs = 2 * period_frames * channels_in * sbytes

            if out_fmt is not None:
                w.out_audio_fmt = out_fmt.audio_fmt
                if out_fmt.buffer_size > 0:
                    w.obs = out_fmt.buffer_size
                else:
                    sbytes = max(2, out_fmt.audio_fmt.bit_depth >> 3)
                    w.obs = 2 * period_frames * max(1, out_fmt.audio_fmt.channels_count) * sbytes
            else:
                w.out_audio_fmt = w.in_audio_fmt
                w.obs = w.ibs

            pipe_mem[w.pipeline_id] = pipe_mem.get(w.pipeline_id, 0) + _calc_pipeline_widget_mem_pages(
                w.ibs, w.is_pages
            )
            prepared_widgets.add(w.name)

        pipe_dir.setdefault(w.pipeline_id, direction)
        target_list = capture_pipe_order if direction == SOF_IPC_STREAM_CAPTURE else playback_pipe_order
        if w.pipeline_id not in target_list:
            target_list.append(w.pipeline_id)

    def prepare_playback_dfs(start_w: TplgWidgetDesc, cur_w: TplgWidgetDesc) -> None:
        for r in tplg.routes:
            if r.source_name != cur_w.name:
                continue
            sink_w = tplg.widgets_by_name.get(r.sink_name)
            if sink_w is None:
                continue
            if cur_w.pipeline_id not in enabled_set or sink_w.pipeline_id not in enabled_set:
                continue
            if start_w.name == cur_w.name:
                prepare_widget(cur_w, SOF_IPC_STREAM_PLAYBACK)
            already = sink_w.name in prepared_widgets
            prepare_widget(sink_w, SOF_IPC_STREAM_PLAYBACK)
            if not already and sink_w.dapm_type != SND_SOC_TPLG_DAPM_DAI_IN:
                prepare_playback_dfs(start_w, sink_w)

    def prepare_capture_dfs(start_w: TplgWidgetDesc, cur_w: TplgWidgetDesc) -> None:
        for r in tplg.routes:
            if r.sink_name != cur_w.name:
                continue
            src_w = tplg.widgets_by_name.get(r.source_name)
            if src_w is None:
                continue
            if src_w.pipeline_id not in enabled_set or cur_w.pipeline_id not in enabled_set:
                continue
            if start_w.name == cur_w.name:
                prepare_widget(cur_w, SOF_IPC_STREAM_CAPTURE)
            already = src_w.name in prepared_widgets
            prepare_widget(src_w, SOF_IPC_STREAM_CAPTURE)
            if not already and src_w.dapm_type != SND_SOC_TPLG_DAPM_DAI_OUT:
                prepare_capture_dfs(start_w, src_w)

    # Pass 1: Prepare widgets via PCM host traversal (matching utils_ipc4.c)
    for pcm in tplg.pcms:
        if pcm.playback_host_name:
            hw = tplg.widgets_by_name.get(pcm.playback_host_name)
            if hw and hw.pipeline_id in enabled_set:
                prepare_playback_dfs(hw, hw)
    for pcm in tplg.pcms:
        if pcm.capture_host_name:
            hw = tplg.widgets_by_name.get(pcm.capture_host_name)
            if hw and hw.pipeline_id in enabled_set:
                prepare_capture_dfs(hw, hw)

    # Collect widgets that participate in at least one active route within enabled_set
    # (so unconnected template widgets such as unused module-copier.18.2 / 20.2 are not initialized)
    routed_widget_names: set = set()
    for r in tplg.routes:
        src_w = tplg.widgets_by_name.get(r.source_name)
        sink_w = tplg.widgets_by_name.get(r.sink_name)
        if src_w and sink_w and src_w.pipeline_id in enabled_set and sink_w.pipeline_id in enabled_set:
            routed_widget_names.add(src_w.name)
            routed_widget_names.add(sink_w.name)
    if not routed_widget_names:
        routed_widget_names = {w.name for w in tplg.widgets if w.pipeline_id in enabled_set}

    # Also prepare any remaining routed widgets in selected_pipeline_ids not reached from a PCM host
    for w in tplg.widgets:
        if w.pipeline_id in enabled_set and w.name not in prepared_widgets and w.name in routed_widget_names:
            if w.pipeline_id in pipe_dir:
                inferred_dir = pipe_dir[w.pipeline_id]
            elif w.dapm_type in (SND_SOC_TPLG_DAPM_AIF_OUT, SND_SOC_TPLG_DAPM_DAI_OUT) or ".capture" in w.name:
                inferred_dir = SOF_IPC_STREAM_CAPTURE
            else:
                inferred_dir = SOF_IPC_STREAM_PLAYBACK
            prepare_widget(w, inferred_dir)

    # Build IpcPipelineCreateOp descriptors
    for pid in selected_pipeline_ids:
        if pid not in enabled_set:
            continue
        pdesc = tplg.pipelines.get(pid) or TplgPipelineDesc(
            pipeline_id=pid, name=f"pipeline.{pid}", core_id=0, priority=0, instance_id=pid
        )
        mem_pages = max(1, pipe_mem.get(pid, 4))
        direction = pipe_dir.get(pid, SOF_IPC_STREAM_PLAYBACK)
        pri, ext = encode_create_pipeline(
            instance_id=pid,
            priority=pdesc.priority,
            mem_pages=mem_pages,
            core_id=pdesc.core_id,
        )
        pop = IpcPipelineCreateOp(
            pipeline_id=pid,
            instance_id=pid,
            name=pdesc.name,
            core_id=pdesc.core_id,
            priority=pdesc.priority,
            mem_pages=mem_pages,
            direction=direction,
            primary=pri,
            extension=ext,
        )
        plan.pipelines_by_id[pid] = pop

    for pid in playback_pipe_order:
        if pid in plan.pipelines_by_id:
            plan.playback_pipelines.append(plan.pipelines_by_id[pid])
    for pid in capture_pipe_order:
        if pid in plan.pipelines_by_id and pid not in playback_pipe_order:
            plan.capture_pipelines.append(plan.pipelines_by_id[pid])
    for pid in selected_pipeline_ids:
        if (
            pid in plan.pipelines_by_id
            and pid not in playback_pipe_order
            and pid not in capture_pipe_order
        ):
            plan.playback_pipelines.append(plan.pipelines_by_id[pid])

    # Pass 2: Build ordered CREATE_PIPELINE, MOD_INIT_INSTANCE, LARGE_CONFIG_SET, and MOD_BIND ops
    created_pipes: set = set()
    setup_widgets: set = set()
    setup_routes: set = set()
    in_pin_masks: Dict[str, int] = {}
    out_pin_masks: Dict[str, int] = {}

    def build_widget_payload(w: TplgWidgetDesc) -> bytes:
        base_cfg = struct.pack("<4I", w.cpc, w.ibs, w.obs, w.is_pages) + w.in_audio_fmt.pack()
        if w.dapm_type == SND_SOC_TPLG_DAPM_BUFFER:
            # struct ipc4_copier_module_cfg (84B) + uuid (16B) = 100B
            copier_tail = w.out_audio_fmt.pack() + struct.pack(
                "<5I", 0, IPC4_INVALID_NODE_ID, 0, 0, 0
            )
            return base_cfg + copier_tail + w.uuid_bytes
        elif w.dapm_type in (
            SND_SOC_TPLG_DAPM_AIF_IN,
            SND_SOC_TPLG_DAPM_AIF_OUT,
            SND_SOC_TPLG_DAPM_DAI_IN,
            SND_SOC_TPLG_DAPM_DAI_OUT,
        ):
            # struct ipc4_file_module_cfg (72B) + tb_file_uuid (16B) = 88B
            rate = fs_in if w.file_mode == FILE_READ else fs_out
            chans = channels_in if w.file_mode == FILE_READ else channels_out
            file_cfg = struct.pack(
                "<IIQIIII",
                rate,
                chans,
                w.file_slot_1based,
                w.file_mode,
                frame_fmt,
                w.file_direction,
                0,
            )
            return base_cfg + file_cfg + TB_FILE_UUID_BYTES
        elif w.dapm_type == SND_SOC_TPLG_DAPM_PGA:
            # base_cfg (40B) + struct ipc4_peak_volume_config (24B) + uuid (16B) = 80B
            vol_cfg = struct.pack(
                "<IIIIQ",
                IPC4_ALL_CHANNELS_MASK,
                w.pga_target_volume,
                w.pga_curve_type,
                0,
                w.pga_curve_duration,
            )
            return base_cfg + vol_cfg + w.uuid_bytes
        elif w.dapm_type == SND_SOC_TPLG_DAPM_SRC:
            sink_rate = w.src_sink_rate or w.out_audio_fmt.sampling_frequency
            return base_cfg + struct.pack("<I", sink_rate) + w.uuid_bytes
        elif w.dapm_type == SND_SOC_TPLG_DAPM_ASRC:
            out_freq = w.asrc_out_freq or w.out_audio_fmt.sampling_frequency
            return base_cfg + struct.pack("<2I", out_freq, w.asrc_mode) + w.uuid_bytes
        elif w.dapm_type == SND_SOC_TPLG_DAPM_MIXER:
            return base_cfg + w.uuid_bytes
        else:
            # TB_PROCESS_MODULE_ID: base_cfg (40B) + base_cfg_ext + pin_formats + uuid (16B)
            nb_in = w.num_input_pins
            nb_out = w.num_output_pins
            ext_hdr = struct.pack("<HH12x", nb_in, nb_out)
            pin_blobs: List[bytes] = []
            for p_idx in range(nb_in):
                if p_idx == 0:
                    pin_blobs.append(Ipc4PinFormat(0, w.ibs, w.in_audio_fmt).pack())
                else:
                    pf = _find_pin_format(
                        w.input_pin_fmts,
                        p_idx,
                        w.in_audio_fmt.sampling_frequency,
                        w.in_audio_fmt.channels_count,
                        w.in_audio_fmt.valid_bit_depth,
                    )
                    if pf is None:
                        pf = Ipc4PinFormat(p_idx, w.ibs, w.in_audio_fmt)
                    elif pf.buffer_size == 0:
                        ch = max(1, pf.audio_fmt.channels_count)
                        sb = max(2, pf.audio_fmt.bit_depth >> 3)
                        pf = Ipc4PinFormat(pf.pin_index, 2 * period_frames * ch * sb, pf.audio_fmt)
                    pin_blobs.append(pf.pack())
            for p_idx in range(nb_out):
                if p_idx == 0:
                    pin_blobs.append(Ipc4PinFormat(0, w.obs, w.out_audio_fmt).pack())
                else:
                    pf = _find_pin_format(
                        w.output_pin_fmts,
                        p_idx,
                        w.out_audio_fmt.sampling_frequency,
                        w.out_audio_fmt.channels_count,
                        w.out_audio_fmt.valid_bit_depth,
                    )
                    if pf is None:
                        pf = Ipc4PinFormat(p_idx, w.obs, w.out_audio_fmt)
                    elif pf.buffer_size == 0:
                        ch = max(1, pf.audio_fmt.channels_count)
                        sb = max(2, pf.audio_fmt.bit_depth >> 3)
                        pf = Ipc4PinFormat(pf.pin_index, 2 * period_frames * ch * sb, pf.audio_fmt)
                    pin_blobs.append(pf.pack())
            return base_cfg + ext_hdr + b"".join(pin_blobs) + w.uuid_bytes

    def allocate_queue_id(comp: TplgWidgetDesc, buddy: TplgWidgetDesc, is_output: bool) -> int:
        num_pins = comp.num_output_pins if is_output else comp.num_input_pins
        bindings = comp.output_pin_binding if is_output else comp.input_pin_binding
        mask_dict = out_pin_masks if is_output else in_pin_masks
        mask = mask_dict.get(comp.name, 0)
        if num_pins <= 1:
            return 0
        if bindings and not bindings[0].startswith("NotConnected"):
            for idx, bname in enumerate(bindings[:num_pins]):
                if bname == buddy.name:
                    mask_dict[comp.name] = mask | (1 << idx)
                    return idx
        for idx in range(8):
            if not (mask & (1 << idx)):
                mask_dict[comp.name] = mask | (1 << idx)
                return idx
        return 0

    def set_up_widget(w: TplgWidgetDesc) -> None:
        if w.module_id == 0 or w.name in setup_widgets:
            return
        if w.pipeline_id not in created_pipes and w.pipeline_id in plan.pipelines_by_id:
            pop = plan.pipelines_by_id[w.pipeline_id]
            plan.steps.append(
                IpcPipelineStepOp(
                    kind="CREATE_PIPELINE",
                    description=(
                        f"CREATE_PIPELINE(ppl={pop.instance_id} '{pop.name}', "
                        f"core={pop.core_id}, prio={pop.priority}, pages={pop.mem_pages})"
                    ),
                    primary=pop.primary,
                    extension=pop.extension,
                    pipeline_id=pop.pipeline_id,
                    instance_id=pop.instance_id,
                )
            )
            created_pipes.add(w.pipeline_id)

        payload = build_widget_payload(w)
        pri, ext = encode_mod_init_instance(
            module_id=w.module_id,
            instance_id=w.instance_id,
            ppl_instance_id=w.pipeline_id,
            core_id=w.core_id,
            proc_domain=w.proc_domain,
            payload=payload,
        )
        bytes_ops: List[IpcLargeConfigSetOp] = []
        for ctl in w.kcontrols:
            plan.kcontrols.append(ctl)
            if ctl.ctl_type == SND_SOC_TPLG_TYPE_BYTES and ctl.abi_data:
                chunks = encode_large_config_set_chunks(
                    w.module_id, w.instance_id, ctl.abi_type, ctl.abi_data
                )
                bytes_ops.append(
                    IpcLargeConfigSetOp(
                        description=(
                            f"LARGE_CONFIG_SET(mod=0x{w.module_id:x}:{w.instance_id} '{w.name}', "
                            f"ctl='{ctl.name}', param_id={ctl.abi_type}, size={len(ctl.abi_data)}B)"
                        ),
                        module_id=w.module_id,
                        instance_id=w.instance_id,
                        param_id=ctl.abi_type,
                        chunks=chunks,
                    )
                )

        wop = IpcWidgetInitOp(
            name=w.name,
            pipeline_id=w.pipeline_id,
            ppl_instance_id=w.pipeline_id,
            module_id=w.module_id,
            instance_id=w.instance_id,
            core_id=w.core_id,
            proc_domain=w.proc_domain,
            primary=pri,
            extension=ext,
            payload=payload,
            bytes_ops=bytes_ops,
        )
        plan.widgets.append(wop)
        plan.steps.append(
            IpcPipelineStepOp(
                kind="MOD_INIT",
                description=(
                    f"MOD_INIT_INSTANCE(mod=0x{w.module_id:x}, inst={w.instance_id}, "
                    f"ppl={w.pipeline_id}, core={w.core_id}, dom={'DP' if w.proc_domain else 'LL'}, "
                    f"name='{w.name}', bytes={len(payload)})"
                ),
                primary=pri,
                extension=ext,
                payload=payload,
                pipeline_id=w.pipeline_id,
                module_id=w.module_id,
                instance_id=w.instance_id,
            )
        )
        for bop in bytes_ops:
            for c_idx, (c_pri, c_ext, c_data) in enumerate(bop.chunks):
                plan.steps.append(
                    IpcPipelineStepOp(
                        kind="LARGE_CFG",
                        description=f"{bop.description} [chunk {c_idx + 1}/{len(bop.chunks)}]",
                        primary=c_pri,
                        extension=c_ext,
                        payload=c_data,
                        pipeline_id=w.pipeline_id,
                        module_id=w.module_id,
                        instance_id=w.instance_id,
                    )
                )
        setup_widgets.add(w.name)

    def set_up_route(src_w: TplgWidgetDesc, sink_w: TplgWidgetDesc) -> None:
        if src_w.module_id == 0 or sink_w.module_id == 0:
            return
        rkey = (src_w.name, sink_w.name)
        if rkey in setup_routes:
            return
        src_q = allocate_queue_id(src_w, sink_w, True)
        dst_q = allocate_queue_id(sink_w, src_w, False)
        if sink_w.module_id == TB_COPIER_MODULE_ID and dst_q > 0:
            setup_routes.add(rkey)
            return
        copier_op: Optional[IpcLargeConfigSetOp] = None
        if src_w.module_id == TB_COPIER_MODULE_ID and src_q > 0:
            sink_fmt_payload = (
                struct.pack("<I", src_q) + src_w.in_audio_fmt.pack() + src_w.out_audio_fmt.pack()
            )
            chunks = encode_large_config_set_chunks(
                src_w.module_id,
                src_w.instance_id,
                IPC4_COPIER_MODULE_CFG_PARAM_SET_SINK_FORMAT,
                sink_fmt_payload,
            )
            copier_op = IpcLargeConfigSetOp(
                description=(
                    f"LARGE_CONFIG_SET(copier '{src_w.name}' sink_id={src_q} SET_SINK_FORMAT)"
                ),
                module_id=src_w.module_id,
                instance_id=src_w.instance_id,
                param_id=IPC4_COPIER_MODULE_CFG_PARAM_SET_SINK_FORMAT,
                chunks=chunks,
            )
            for c_pri, c_ext, c_data in chunks:
                plan.steps.append(
                    IpcPipelineStepOp(
                        kind="LARGE_CFG",
                        description=copier_op.description,
                        primary=c_pri,
                        extension=c_ext,
                        payload=c_data,
                        pipeline_id=src_w.pipeline_id,
                        module_id=src_w.module_id,
                        instance_id=src_w.instance_id,
                    )
                )

        b_pri, b_ext = encode_mod_bind_unbind(
            src_w.module_id,
            src_w.instance_id,
            src_q,
            sink_w.module_id,
            sink_w.instance_id,
            dst_q,
            is_unbind=False,
        )
        u_pri, u_ext = encode_mod_bind_unbind(
            src_w.module_id,
            src_w.instance_id,
            src_q,
            sink_w.module_id,
            sink_w.instance_id,
            dst_q,
            is_unbind=True,
        )
        rop = IpcRouteBindOp(
            src_name=src_w.name,
            sink_name=sink_w.name,
            src_pipeline_id=src_w.pipeline_id,
            dst_pipeline_id=sink_w.pipeline_id,
            src_module_id=src_w.module_id,
            src_instance_id=src_w.instance_id,
            src_queue=src_q,
            dst_module_id=sink_w.module_id,
            dst_instance_id=sink_w.instance_id,
            dst_queue=dst_q,
            bind_primary=b_pri,
            bind_extension=b_ext,
            unbind_primary=u_pri,
            unbind_extension=u_ext,
            copier_sink_fmt_op=copier_op,
        )
        plan.routes.append(rop)
        plan.steps.append(
            IpcPipelineStepOp(
                kind="MOD_BIND",
                description=(
                    f"MOD_BIND({src_w.name} [0x{src_w.module_id:x}:{src_w.instance_id} q{src_q}] "
                    f"-> {sink_w.name} [0x{sink_w.module_id:x}:{sink_w.instance_id} q{dst_q}])"
                ),
                primary=b_pri,
                extension=b_ext,
                pipeline_id=src_w.pipeline_id,
                module_id=src_w.module_id,
                instance_id=src_w.instance_id,
            )
        )
        setup_routes.add(rkey)

    def setup_playback_dfs(start_w: TplgWidgetDesc, cur_w: TplgWidgetDesc) -> None:
        for r in tplg.routes:
            if r.source_name != cur_w.name or (r.source_name, r.sink_name) in setup_routes:
                continue
            sink_w = tplg.widgets_by_name.get(r.sink_name)
            if sink_w is None:
                continue
            if cur_w.pipeline_id not in enabled_set or sink_w.pipeline_id not in enabled_set:
                continue
            if start_w.name == cur_w.name:
                set_up_widget(cur_w)
            sink_already = sink_w.name in setup_widgets
            set_up_widget(sink_w)
            set_up_route(cur_w, sink_w)
            if not sink_already and sink_w.dapm_type != SND_SOC_TPLG_DAPM_DAI_IN:
                setup_playback_dfs(start_w, sink_w)

    def setup_capture_dfs(start_w: TplgWidgetDesc, cur_w: TplgWidgetDesc) -> None:
        for r in tplg.routes:
            if r.sink_name != cur_w.name or (r.source_name, r.sink_name) in setup_routes:
                continue
            src_w = tplg.widgets_by_name.get(r.source_name)
            if src_w is None:
                continue
            if src_w.pipeline_id not in enabled_set or cur_w.pipeline_id not in enabled_set:
                continue
            if start_w.name == cur_w.name:
                set_up_widget(cur_w)
            src_already = src_w.name in setup_widgets
            set_up_widget(src_w)
            set_up_route(src_w, cur_w)
            if not src_already and src_w.dapm_type != SND_SOC_TPLG_DAPM_DAI_OUT:
                setup_capture_dfs(start_w, src_w)

    for pcm in tplg.pcms:
        if pcm.playback_host_name:
            hw = tplg.widgets_by_name.get(pcm.playback_host_name)
            if hw and hw.pipeline_id in enabled_set:
                setup_playback_dfs(hw, hw)
    for pcm in tplg.pcms:
        if pcm.capture_host_name:
            hw = tplg.widgets_by_name.get(pcm.capture_host_name)
            if hw and hw.pipeline_id in enabled_set:
                setup_capture_dfs(hw, hw)

    # Also set up any remaining routed widgets & routes in selected_pipeline_ids
    for w in tplg.widgets:
        if w.pipeline_id in enabled_set and w.name not in setup_widgets and w.name in routed_widget_names:
            set_up_widget(w)
    for r in tplg.routes:
        src_w = tplg.widgets_by_name.get(r.source_name)
        sink_w = tplg.widgets_by_name.get(r.sink_name)
        if (
            src_w
            and sink_w
            and src_w.pipeline_id in enabled_set
            and sink_w.pipeline_id in enabled_set
            and (src_w.name, sink_w.name) not in setup_routes
        ):
            set_up_route(src_w, sink_w)

    return plan
