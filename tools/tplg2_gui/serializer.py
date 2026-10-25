# SPDX-License-Identifier: BSD-3-Clause
"""Topology2 (.conf) serializer and standalone testbench harness generator."""

from __future__ import annotations
import os
from typing import Dict, List, Set, Tuple

from .model import ConfigNode, GraphNode, GraphViewModel


# Non-instance metadata attributes that belong to Class definitions rather than Object instances
CLASS_ONLY_SCALARS = {
    "uuid",
    "type",
    "index",
    "instance",
    "direction",
    "is_pages",
    "bss_size",
    "cpc",
    "no_pm",
    "num_input_pins",
    "num_output_pins",
    "num_input_audio_formats",
    "num_output_audio_formats",
    "included_param_file",
    "included_conf",
}


def serialize_config_node(node: ConfigNode, indent: int = 0) -> str:
    """Serialize a ConfigNode tree into alsaconf / topology2 text."""
    pad = "\t" * indent
    if node.node_type == "scalar":
        val_str = str(node.value)
        if val_str == "":
            return ""
        if any(c in val_str for c in (" ", "\t", "$", ",", ":")) and not (
            val_str.startswith('"') or val_str.startswith("'")
        ):
            val_str = f"'{val_str}'"
        return f"{pad}{node.key}\t{val_str}\n"

    if node.node_type == "array":
        lines = [f"{pad}{node.key} [\n"]
        for ch in node.children:
            lines.append(f"{pad}\t{{\n")
            for sub in ch.children:
                s = serialize_config_node(sub, indent + 2)
                if s:
                    lines.append(s)
            lines.append(f"{pad}\t}}\n")
        lines.append(f"{pad}]\n")
        return "".join(lines)

    lines = [f"{pad}{node.key} {{\n"]
    for ch in node.children:
        s = serialize_config_node(ch, indent + 1)
        if s:
            lines.append(s)
    lines.append(f"{pad}}}\n")
    return "".join(lines)


def generate_compilable_conf(vm: GraphViewModel, topology2_root: str) -> Tuple[str, str, List[int]]:
    """Generate a compilable Topology2 .conf string for the current GraphViewModel.

    Returns:
        (conf_text, defines_str, pipeline_ids_for_testbench)
    """
    if vm.view_kind == "topology" and vm.source_file and os.path.isfile(vm.source_file):
        return _generate_modified_topology_conf(vm, topology2_root)
    return _generate_standalone_harness_conf(vm, topology2_root)


def _generate_modified_topology_conf(
    vm: GraphViewModel,
    topology2_root: str,
) -> Tuple[str, str, List[int]]:
    """Serialize the machine topology with any user edits applied."""
    with open(vm.source_file, "r", encoding="utf-8", errors="replace") as f:
        base_text = f.read()

    defines_parts = []
    if vm.target_variant and vm.target_variant.defines_str:
        defines_parts.append(vm.target_variant.defines_str)

    extra_blocks: List[str] = []
    def_node = vm.root_config.get_child("Define (Active Variables)")
    if def_node and vm.dirty:
        def_lines = ["Define {\n"]
        for ch in def_node.children:
            if ch.node_type == "scalar" and ch.origin == "instance" and ch.value != "":
                val_s = str(ch.value).strip()
                if val_s.lstrip("-").isdigit() or val_s.startswith("0x"):
                    def_lines.append(f"\t{ch.key}\t{val_s}\n")
                elif (val_s.startswith("'") and val_s.endswith("'")) or (
                    val_s.startswith('"') and val_s.endswith('"')
                ):
                    def_lines.append(f"\t{ch.key}\t{val_s}\n")
                else:
                    def_lines.append(f"\t{ch.key}\t'{val_s}'\n")
        if len(def_lines) > 1:
            def_lines.append("}\n")
            extra_blocks.append("".join(def_lines))

    pipe_ids = sorted(vm.containers.keys())
    tb_pipes = [1, 2]
    if 1 in pipe_ids and 2 in pipe_ids:
        tb_pipes = [1, 2]
    elif 0 in pipe_ids and 1 in pipe_ids:
        tb_pipes = [0, 1]
    elif len(pipe_ids) >= 2:
        tb_pipes = pipe_ids[:2]
    elif len(pipe_ids) == 1:
        tb_pipes = pipe_ids

    full_text = base_text
    if extra_blocks:
        full_text = base_text + "\n# --- GUI Edits ---\n" + "\n".join(extra_blocks)

    return (full_text, ",".join(defines_parts), tb_pipes)


def _generate_standalone_harness_conf(
    vm: GraphViewModel,
    topology2_root: str,
) -> Tuple[str, str, List[int]]:
    """Generate a self-contained, compilable Topology2 testbench harness wrapping the current graph.

    Uses the topology2/include/bench/ pattern so any Processing Module or Pipeline graph can
    be compiled by alsatplg -p and executed in sof-testbench4 -p 1,2 immediately.
    """
    proc_nodes: List[GraphNode] = []
    for node in vm.nodes.values():
        if node.class_category == "Widget" and node.class_name not in (
            "host-copier",
            "dai-copier",
            "alh-dai-copier",
            "mixin",
            "mixout",
            "module-copier",
            "pipeline",
            "virtual",
        ):
            proc_nodes.append(node)

    node_order = {nid: idx for idx, nid in enumerate(vm.nodes.keys())}
    proc_nodes.sort(key=lambda n: node_order.get(n.id, 0))
    if not proc_nodes:
        # Default to a single gain widget if the selection has no processing modules
        proc_nodes = [
            GraphNode(
                id="gain.1.1",
                title="gain.1.1",
                subtitle="Widget.gain",
                category="widget",
                class_category="Widget",
                class_name="gain",
                instance_id="1",
                pipeline_id=1,
            )
        ]

    comp_includes: Set[str] = {"include/components/gain.conf"}
    for n in proc_nodes:
        inc_rel = f"include/components/{n.class_name}.conf"
        if os.path.isfile(os.path.join(topology2_root, inc_rel)):
            comp_includes.add(inc_rel)
    inc_lines = "\n".join(f"<{inc}>" for inc in sorted(comp_includes))

    w_blocks: List[str] = []
    chain_names: List[str] = ["host-copier.0.playback"]
    used_counts: Dict[str, int] = {}

    for idx, n in enumerate(proc_nodes):
        wclass = n.class_name
        inst_num = used_counts.get(wclass, 0) + 1
        used_counts[wclass] = inst_num

        # Check if include/bench/ has control bytes or controls config for this component
        ctrl_inc = ""
        for cand in (
            f"include/bench/{wclass}_control_bytes_playback.conf",
            f"include/bench/{wclass}_controls_playback.conf",
        ):
            if os.path.isfile(os.path.join(topology2_root, cand)):
                # Only include on first instance of a given class so control names stay unique
                if inst_num == 1:
                    ctrl_inc = f"\n\t<{cand}>"
                break

        custom_scalars: List[str] = []
        for ch in n.config.children:
            if (
                ch.node_type == "scalar"
                and ch.origin == "instance"
                and ch.key not in CLASS_ONLY_SCALARS
                and not ch.key.startswith("num_")
                and ch.value != ""
            ):
                val_s = str(ch.value)
                if not val_s.lstrip("-").isdigit() and not val_s.startswith("0x"):
                    val_s = f'"{val_s}"'
                custom_scalars.append(f"\t{ch.key}\t{val_s}")
        extra_s = ("\n" + "\n".join(custom_scalars)) if custom_scalars else ""

        w_blocks.append(
            f"""Object.Widget.{wclass}.{inst_num} {{
\tindex $BENCH_PLAYBACK_HOST_PIPELINE
\t<include/bench/one_input_output_format_s32.conf>{ctrl_inc}{extra_s}
}}"""
        )
        chain_names.append(f"{wclass}.1.{inst_num}")

    chain_names.append("dai-copier.HDA.Analog.playback")

    # Add capture passthrough gain widget on pipeline 3 so sof-testbench4's capture setup succeeds
    cap_gain_inst = used_counts.get("gain", 0) + 1
    w_blocks.append(
        f"""Object.Widget.gain.{cap_gain_inst} {{
\tindex $BENCH_CAPTURE_HOST_PIPELINE
\t<include/bench/one_input_output_format_s32.conf>
\t<include/bench/gain_controls_capture.conf>
}}"""
    )

    widgets_text = "\n\n".join(w_blocks)

    route_entries: List[str] = []
    for i in range(len(chain_names) - 1):
        u = chain_names[i]
        v = chain_names[i + 1]
        route_entries.append(f'\t{{\n\t\tsource\t"{u}"\n\t\tsink\t"{v}"\n\t}}')
    route_entries.append(
        f'\t{{\n\t\tsource\t"dai-copier.HDA.Analog.capture"\n\t\tsink\t"gain.3.{cap_gain_inst}"\n\t}}'
    )
    route_entries.append(
        f'\t{{\n\t\tsource\t"gain.3.{cap_gain_inst}"\n\t\tsink\t"host-copier.0.capture"\n\t}}'
    )
    routes_text = "\n".join(route_entries)

    conf_text = f"""<searchdir:include>
<searchdir:include/common>
<searchdir:include/components>
<searchdir:include/controls>
<searchdir:include/dais>
<searchdir:include/pipelines>
<searchdir:include/pipelines/cavs>
<searchdir:platform>
<searchdir:platform/intel>

<vendor-token.conf>
<tokens.conf>
<virtual.conf>
<mixer.conf>
<bytes.conf>
<enum.conf>
<host-gateway-playback.conf>
<host-gateway-capture.conf>
<dai-copier.conf>
<io-gateway.conf>
<io-gateway-capture.conf>
<data.conf>
<pcm.conf>
<pcm_caps.conf>
<fe_dai.conf>
<hda.conf>
<hw_config_simple.conf>
<manifest.conf>
<route.conf>
<common_definitions.conf>
{inc_lines}

Define {{
\tPLATFORM\t\t\t"mtl"
\tANALOG_PLAYBACK_PCM\t\t'Analog Playback'
\tANALOG_CAPTURE_PCM\t\t'Analog Capture'
\tHDA_ANALOG_DAI_NAME\t\t'Analog'
\tHDA_ANALOG_CAPTURE_RATE\t\t48000
\tHDA_ANALOG_PLAYBACK_RATE\t48000
\tBENCH_PLAYBACK_HOST_PIPELINE\t1
\tBENCH_PLAYBACK_DAI_PIPELINE\t2
\tBENCH_CAPTURE_HOST_PIPELINE\t3
\tBENCH_CAPTURE_DAI_PIPELINE\t4
\tBENCH_PLAYBACK_DAI_COPIER\t"dai-copier.HDA.Analog.playback"
\tBENCH_CAPTURE_DAI_COPIER\t"dai-copier.HDA.Analog.capture"
\tBENCH_PCM_CHANNELS_MIN\t\t2
\tBENCH_PCM_CHANNELS_MAX\t\t2
\tBENCH_MODULE_FORMAT\t\t"s32"
\tBENCH_EQIIR_PARAMS\t\t"passthrough"
\tBENCH_EQFIR_PARAMS\t\t"passthrough"
\tBENCH_DRC_PARAMS\t\t"default"
\tBENCH_MULTIBAND_DRC_PARAMS\t"default"
\tBENCH_DCBLOCK_PARAMS\t\t"default"
\tBENCH_TDFB_PARAMS\t\t"default"
\tBENCH_MICSEL_PARAMS\t\t"passthrough"
\tBENCH_ARIA_PARAMS\t\t"param_2"
}}

IncludeByKey.PLATFORM {{
\t"mtl"\t"platform/intel/mtl.conf"
}}

Object.Dai.HDA [
\t{{
\t\tname $HDA_ANALOG_DAI_NAME
\t\tdai_index 0
\t\tid 4
\t\tdefault_hw_conf_id 4
\t\tObject.Base.hw_config.1 {{
\t\t\tname\t"HDA0"
\t\t}}
\t\tdirection duplex
\t}}
]

Object.PCM.pcm [
\t{{
\t\tid 0
\t\tname 'HDA Analog'
\t\tObject.Base.fe_dai.1 {{
\t\t\tname "HDA Analog"
\t\t}}
\t\tObject.PCM.pcm_caps.1 {{
\t\t\tdirection\t"playback"
\t\t\tname $ANALOG_PLAYBACK_PCM
\t\t\tformats 'S32_LE,S24_LE,S16_LE'
\t\t\trates "8000,11025,16000,22050,24000,32000,44100,48000,64000,88200,96000,176400,192000"
\t\t\tchannels_min $BENCH_PCM_CHANNELS_MIN
\t\t\tchannels_max $BENCH_PCM_CHANNELS_MAX
\t\t}}
\t\tObject.PCM.pcm_caps.2 {{
\t\t\tdirection\t"capture"
\t\t\tname $ANALOG_CAPTURE_PCM
\t\t\tformats 'S32_LE,S24_LE,S16_LE'
\t\t\trates "8000,11025,16000,22050,24000,32000,44100,48000,64000,88200,96000,176400,192000"
\t\t\tchannels_min $BENCH_PCM_CHANNELS_MIN
\t\t\tchannels_max $BENCH_PCM_CHANNELS_MAX
\t\t}}
\t\tdirection duplex
\t}}
]

Object.Pipeline {{
\tio-gateway [
\t\t{{
\t\t\tindex $BENCH_PLAYBACK_DAI_PIPELINE
\t\t\tdirection playback
\t\t\tObject.Widget.dai-copier.1 {{
\t\t\t\tnode_type $HDA_LINK_OUTPUT_CLASS
\t\t\t\tstream_name $HDA_ANALOG_DAI_NAME
\t\t\t\tdai_type "HDA"
\t\t\t\tcopier_type "HDA"
\t\t\t\tnum_input_pins 1
\t\t\t\tnum_input_audio_formats 1
\t\t\t\tnum_output_audio_formats 1
\t\t\t\tObject.Base.input_audio_format [
\t\t\t\t\t{{
\t\t\t\t\t\tin_bit_depth\t\t32
\t\t\t\t\t\tin_valid_bit_depth\t32
\t\t\t\t\t}}
\t\t\t\t]
\t\t\t\tObject.Base.output_audio_format [
\t\t\t\t\t{{
\t\t\t\t\t\tout_bit_depth\t\t32
\t\t\t\t\t\tout_valid_bit_depth\t32
\t\t\t\t\t}}
\t\t\t\t]
\t\t\t}}
\t\t}}
\t]
\tio-gateway-capture [
\t\t{{
\t\t\tindex $BENCH_CAPTURE_DAI_PIPELINE
\t\t\tdirection capture
\t\t\tObject.Widget.dai-copier.1 {{
\t\t\t\tdai_type\t"HDA"
\t\t\t\ttype\t\t"dai_out"
\t\t\t\tcopier_type\t"HDA"
\t\t\t\tstream_name\t$HDA_ANALOG_DAI_NAME
\t\t\t\tnode_type\t$HDA_LINK_INPUT_CLASS
\t\t\t\tnum_output_pins 1
\t\t\t\tnum_input_audio_formats 1
\t\t\t\tnum_output_audio_formats 1
\t\t\t\tObject.Base.input_audio_format [
\t\t\t\t\t{{
\t\t\t\t\t\tin_bit_depth\t\t32
\t\t\t\t\t\tin_valid_bit_depth\t32
\t\t\t\t\t}}
\t\t\t\t]
\t\t\t\tObject.Base.output_audio_format [
\t\t\t\t\t{{
\t\t\t\t\t\tout_bit_depth\t\t32
\t\t\t\t\t\tout_valid_bit_depth\t32
\t\t\t\t\t}}
\t\t\t\t]
\t\t\t}}
\t\t}}
\t]
}}

{widgets_text}

<include/bench/host_gateway_pipelines_s32.conf>

Object.Base.route [
{routes_text}
]
"""
    return (conf_text, "", [1, 2])
