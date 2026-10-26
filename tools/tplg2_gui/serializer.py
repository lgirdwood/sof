# SPDX-License-Identifier: BSD-3-Clause
"""Topology2 (.conf) serializer and standalone testbench harness generator."""

from __future__ import annotations
import os
import re
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


def _extract_active_pipeline_ids(vm: GraphViewModel) -> List[int]:
    """Return the ordered active testbench pipeline IDs from enabled TestbenchIO nodes,
    ordered top-to-bottom by the vertical (y) positions of the PCMs in the Main Graph.
    When both testbench-source and testbench-sink nodes are enabled, returns the pipelines
    lying on paths between enabled sources and enabled sinks.
    """
    enabled_sources = [
        node
        for node in vm.nodes.values()
        if node.class_category == "TestbenchIO"
        and node.class_name == "testbench-source"
        and str(node.config.get_scalar("enabled", "false")).lower() == "true"
    ]
    enabled_sinks = [
        node
        for node in vm.nodes.values()
        if node.class_category == "TestbenchIO"
        and node.class_name == "testbench-sink"
        and str(node.config.get_scalar("enabled", "false")).lower() == "true"
    ]
    enabled_sources.sort(key=lambda n: (n.y, n.x))
    enabled_sinks.sort(key=lambda n: (n.y, n.x))

    src_pids: List[int] = []
    for node in enabled_sources:
        tp_str = str(node.config.get_scalar("target_pipelines", "")).strip()
        for m in re.findall(r"\d+", tp_str):
            pid = int(m)
            if pid not in src_pids:
                src_pids.append(pid)

    snk_pids: List[int] = []
    for node in enabled_sinks:
        tp_str = str(node.config.get_scalar("target_pipelines", "")).strip()
        for m in re.findall(r"\d+", tp_str):
            pid = int(m)
            if pid not in snk_pids:
                snk_pids.append(pid)

    if src_pids and snk_pids:
        snk_set = set(snk_pids)
        inter = [p for p in src_pids if p in snk_set]
        if inter:
            return inter
        combined = list(src_pids)
        for p in snk_pids:
            if p not in combined:
                combined.append(p)
        return combined
    if src_pids:
        return src_pids
    if snk_pids:
        return snk_pids

    pipe_ids = sorted(vm.containers.keys())
    if 1 in pipe_ids and 2 in pipe_ids:
        return [1, 2]
    if 0 in pipe_ids and 1 in pipe_ids:
        return [0, 1]
    if len(pipe_ids) >= 2:
        return pipe_ids[:2]
    if len(pipe_ids) == 1:
        return pipe_ids
    return [1, 2]


def _has_structural_or_param_edits(vm: GraphViewModel) -> bool:
    """Return True if modules, routes, or control_bytes parameter files were modified on a topology graph."""
    if getattr(vm, "graph_structure_modified", False):
        return True
    if any(e.id.startswith("user_edge_") for e in vm.edges):
        return True
    if any(
        c.class_name == "custom" or c.enabled_by == "Added from Object View"
        for c in vm.containers.values()
    ):
        return True
    return False


def _extract_ordered_proc_nodes(vm: GraphViewModel, active_pids: List[int]) -> List[GraphNode]:
    """Extract processing Widget nodes along the active pipeline chain in signal-flow order."""
    from collections import deque

    copier_classes = {
        "host-copier",
        "dai-copier",
        "alh-dai-copier",
        "alh-copier",
        "mixin",
        "mixout",
        "module-copier",
        "pipeline",
        "virtual",
    }

    def is_proc_widget(n: GraphNode) -> bool:
        return n.class_category == "Widget" and n.class_name not in copier_classes

    # Build adjacency map from audio/TestbenchIO edges
    adj: Dict[str, List[str]] = {}
    for e in vm.edges:
        u = vm.nodes.get(e.source_node)
        v = vm.nodes.get(e.sink_node)
        if not u or not v:
            continue
        if u.class_category == "Control" or v.class_category == "Control":
            continue
        adj.setdefault(u.id, []).append(v.id)

    ordered: List[GraphNode] = []
    visited_nids: Set[str] = set()

    # 1. Start BFS/walk from enabled testbench-source node(s)
    enabled_sources = [
        n for n in vm.nodes.values()
        if n.class_category == "TestbenchIO"
        and n.class_name == "testbench-source"
        and str(n.config.get_scalar("enabled", "false")).lower() == "true"
    ]
    queue: deque[str] = deque()
    for snode in enabled_sources:
        queue.append(snode.id)
        if snode.parent_node_id and snode.parent_node_id in vm.nodes:
            queue.append(snode.parent_node_id)

    while queue:
        curr_id = queue.popleft()
        if curr_id in visited_nids:
            continue
        visited_nids.add(curr_id)
        curr_node = vm.nodes.get(curr_id)
        if curr_node and is_proc_widget(curr_node):
            ordered.append(curr_node)
        for nxt_id in adj.get(curr_id, []):
            if nxt_id not in visited_nids:
                queue.append(nxt_id)

    # 2. Include any additional processing widgets in active_pids (or custom pipelines, or all if non-topology)
    target_pid_set = set(active_pids)
    for pid, cont in vm.containers.items():
        if cont.class_name == "custom" or cont.enabled_by == "Added from Object View":
            target_pid_set.add(pid)

    remaining: List[GraphNode] = []
    for node in vm.nodes.values():
        if not is_proc_widget(node) or node in ordered:
            continue
        if vm.view_kind != "topology" or not target_pid_set or node.pipeline_id in target_pid_set:
            remaining.append(node)

    if remaining:
        def _sort_key(n: GraphNode) -> Tuple[int, float]:
            pid = n.pipeline_id if n.pipeline_id is not None else 0
            cont = vm.containers.get(pid)
            is_cap = bool(cont and "capture" in cont.direction.lower())
            return (pid, -n.x if is_cap else n.x)

        remaining.sort(key=_sort_key)
        ordered.extend(remaining)

    return ordered


def _resolve_bytes_param_include(
    pfile: str,
    wclass: str,
    topology2_root: str,
) -> str:
    """Resolve a Control.bytes included_param_file path to a topology2_root-relative include string."""
    raw = pfile.strip().strip('"').strip("'")
    if not raw or raw.startswith("Choose ") or raw.startswith("("):
        return ""
    if os.path.isabs(raw):
        try:
            rel = os.path.relpath(raw, topology2_root)
            if not rel.startswith("..") and os.path.isfile(os.path.join(topology2_root, rel)):
                return rel
        except ValueError:
            pass
    for prefix in ("", "include/", "include/components/", f"include/components/{wclass}/"):
        cand = prefix + (os.path.basename(raw) if prefix.endswith(f"{wclass}/") else raw)
        if os.path.isfile(os.path.join(topology2_root, cand)):
            return cand
    return ""


def generate_compilable_conf(
    vm: GraphViewModel,
    topology2_root: str,
    target_platform: str = "mtl",
    target_harness: str = "hda_analog",
    extra_platform_conf: str = "",
) -> Tuple[str, str, List[int]]:
    """Generate a compilable Topology2 .conf string for the current GraphViewModel.

    Returns:
        (conf_text, defines_str, pipeline_ids_for_testbench)
    """
    if (
        vm.view_kind == "topology"
        and vm.source_file
        and os.path.isfile(vm.source_file)
        and not _has_structural_or_param_edits(vm)
    ):
        return _generate_modified_topology_conf(
            vm,
            topology2_root,
            target_platform=target_platform,
            extra_platform_conf=extra_platform_conf,
        )
    return _generate_standalone_harness_conf(
        vm,
        topology2_root,
        target_platform=target_platform,
        target_harness=target_harness,
        extra_platform_conf=extra_platform_conf,
    )


def _generate_modified_topology_conf(
    vm: GraphViewModel,
    topology2_root: str,
    target_platform: str = "mtl",
    extra_platform_conf: str = "",
) -> Tuple[str, str, List[int]]:
    """Serialize the machine topology with any user edits applied."""
    with open(vm.source_file, "r", encoding="utf-8", errors="replace") as f:
        base_text = f.read()

    defines_parts = []
    if vm.target_variant and vm.target_variant.defines_str:
        defines_parts.append(vm.target_variant.defines_str)
    if target_platform and not any("PLATFORM=" in p for p in defines_parts):
        if "IncludeByKey.PLATFORM" in base_text or "PLATFORM" in base_text:
            defines_parts.append(f"PLATFORM={target_platform}")

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

    tb_pipes = _extract_active_pipeline_ids(vm)

    full_text = base_text
    if extra_blocks:
        full_text = base_text + "\n# --- GUI Edits ---\n" + "\n".join(extra_blocks)

    return (full_text, ",".join(defines_parts), tb_pipes)


def _generate_standalone_harness_conf(
    vm: GraphViewModel,
    topology2_root: str,
    target_platform: str = "mtl",
    target_harness: str = "hda_analog",
    extra_platform_conf: str = "",
) -> Tuple[str, str, List[int]]:
    """Generate a self-contained, compilable Topology2 testbench harness wrapping the current graph.

    Uses the topology2/include/bench/ pattern so any Processing Module or Pipeline graph can
    be compiled by alsatplg -p and executed in sof-testbench4 -p 1,2 immediately.
    """
    active_pids = _extract_active_pipeline_ids(vm)
    proc_nodes = _extract_ordered_proc_nodes(vm, active_pids)
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
        for sub_dir in ("include/components", "include/components/cavs"):
            inc_rel = f"{sub_dir}/{n.class_name}.conf"
            if os.path.isfile(os.path.join(topology2_root, inc_rel)):
                comp_includes.add(inc_rel)
                break
    inc_lines = "\n".join(f"<{inc}>" for inc in sorted(comp_includes))

    w_blocks: List[str] = []
    chain_names: List[str] = ["host-copier.0.playback"]
    used_counts: Dict[str, int] = {}
    used_kctl_names: Set[str] = set()

    for idx, n in enumerate(proc_nodes):
        wclass = n.class_name
        inst_num = used_counts.get(wclass, 0) + 1
        used_counts[wclass] = inst_num

        # Inspect child Control nodes attached to `n` on the graph
        child_ctrls = [
            cnode for cnode in vm.nodes.values()
            if cnode.parent_node_id == n.id and cnode.class_category == "Control"
        ]
        bytes_cnode = next((c for c in child_ctrls if c.class_name == "bytes"), None)
        mixer_cnode = next((c for c in child_ctrls if c.class_name == "mixer" and c.instance_id == "1"), None)
        if mixer_cnode is None:
            mixer_cnode = next((c for c in child_ctrls if c.class_name == "mixer"), None)
        enum_cnode = next((c for c in child_ctrls if c.class_name == "enum"), None)

        raw_pfile = ""
        if bytes_cnode is not None:
            raw_pfile = str(bytes_cnode.config.get_scalar("included_param_file", "")).strip()
        if not raw_pfile:
            ctrl_grp = n.config.get_child("Object.Control")
            if ctrl_grp:
                b_cfg = ctrl_grp.get_child("bytes.1")
                if b_cfg:
                    raw_pfile = str(b_cfg.get_scalar("included_param_file", "")).strip()

        resolved_bytes_inc = _resolve_bytes_param_include(raw_pfile, wclass, topology2_root)

        custom_ctrl_blocks: List[str] = []
        if resolved_bytes_inc:
            b_name = ""
            if bytes_cnode is not None:
                b_name = str(bytes_cnode.config.get_scalar("name", "")).strip().strip('"').strip("'")
            if not b_name or b_name in used_kctl_names:
                suffix = f" {inst_num}" if inst_num > 1 else ""
                b_name = f"Analog Playback {wclass.upper()}{suffix} bytes"
            used_kctl_names.add(b_name)
            if bytes_cnode is not None:
                bytes_cnode.config.set_scalar("name", b_name, origin="instance")
            custom_ctrl_blocks.append(
                f'\tObject.Control.bytes."1" {{\n\t\tname\t\'{b_name}\'\n\t\t<{resolved_bytes_inc}>\n\t}}'
            )

        if mixer_cnode is not None and (wclass == "gain" or resolved_bytes_inc):
            m_name = str(mixer_cnode.config.get_scalar("name", "")).strip().strip('"').strip("'")
            if not m_name or m_name in used_kctl_names:
                is_sw = str(mixer_cnode.config.get_scalar("max", "")).strip() == "1"
                mkind = "switch" if is_sw else "volume"
                suffix = f" {inst_num}" if inst_num > 1 else ""
                m_name = f"Analog Playback {wclass.upper()}{suffix} {mkind}"
            used_kctl_names.add(m_name)
            mixer_cnode.config.set_scalar("name", m_name, origin="instance")
            custom_ctrl_blocks.append(
                f'\tObject.Control.mixer."1" {{\n\t\tname\t\'{m_name}\'\n\t}}'
            )

        if enum_cnode is not None and resolved_bytes_inc:
            e_name = str(enum_cnode.config.get_scalar("name", "")).strip().strip('"').strip("'")
            if not e_name or e_name in used_kctl_names:
                suffix = f" {inst_num}" if inst_num > 1 else ""
                e_name = f"Analog Playback {wclass.upper()}{suffix} enum"
            used_kctl_names.add(e_name)
            enum_cnode.config.set_scalar("name", e_name, origin="instance")
            custom_ctrl_blocks.append(
                f'\tObject.Control.enum."1" {{\n\t\tname\t\'{e_name}\'\n\t}}'
            )

        if custom_ctrl_blocks:
            ctrl_inc = "\n" + "\n".join(custom_ctrl_blocks)
        else:
            # Fallback to include/bench/ control bytes or controls config for this component
            ctrl_inc = ""
            for cand in (
                f"include/bench/{wclass}_control_bytes_playback.conf",
                f"include/bench/{wclass}_controls_playback.conf",
            ):
                if os.path.isfile(os.path.join(topology2_root, cand)):
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

    plat_key = (target_platform or vm.defines.get("PLATFORM", "mtl") or "mtl").strip().lower()
    if plat_key not in ("tgl", "adl", "mtl", "lnl", "ptl", "nvl"):
        plat_key = "mtl"

    extra_plat_includes: List[str] = []
    harness_defaults = {
        "hda_generic": ["dmic-default.conf", "hdmi-default.conf", "deep-buffer-default.conf", "compr-default.conf"],
        "ssp_nocodec": ["ssp-default.conf", "dmic-default.conf", "deep-buffer-default.conf"],
        "dmic_capture": ["dmic-default.conf"],
        "hdmi_playback": ["hdmi-default.conf", "hdmi-in-default.conf"],
        "bt_ssp": ["bt-default.conf", "ssp-default.conf"],
        "deep_buffer": ["deep-buffer-default.conf", "efx-default.conf"],
    }
    for def_inc in harness_defaults.get(target_harness, []):
        if os.path.isfile(os.path.join(topology2_root, "platform", "intel", def_inc)):
            extra_plat_includes.append(f"<{def_inc}>")
    if extra_platform_conf and extra_platform_conf.endswith("-default.conf"):
        base_inc = os.path.basename(extra_platform_conf)
        tag = f"<{base_inc}>"
        if tag not in extra_plat_includes and os.path.isfile(os.path.join(topology2_root, extra_platform_conf)):
            extra_plat_includes.append(tag)
    extra_plat_inc_block = ("\n" + "\n".join(extra_plat_includes)) if extra_plat_includes else ""

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
<common_definitions.conf>{extra_plat_inc_block}
{inc_lines}

Define {{
\tPLATFORM\t\t\t"{plat_key}"
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
\tBENCH_EQIIR_PARAMS\t\t"bandpass"
\tBENCH_EQFIR_PARAMS\t\t"loudness"
\tBENCH_DRC_PARAMS\t\t"enabled"
\tBENCH_MULTIBAND_DRC_PARAMS\t"default"
\tBENCH_DCBLOCK_PARAMS\t\t"default"
\tBENCH_TDFB_PARAMS\t\t"default"
\tBENCH_MICSEL_PARAMS\t\t"passthrough"
\tBENCH_ARIA_PARAMS\t\t"param_2"
}}

IncludeByKey.PLATFORM {{
\t"tgl"\t"platform/intel/tgl.conf"
\t"adl"\t"platform/intel/tgl.conf"
\t"mtl"\t"platform/intel/mtl.conf"
\t"lnl"\t"platform/intel/lnl.conf"
\t"ptl"\t"platform/intel/ptl.conf"
\t"nvl"\t"platform/intel/ptl.conf"
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


def get_editable_conf_text_for_vm(vm: GraphViewModel, topology2_root: str = "") -> str:
    """Return the Topology2 (.conf) source text for viewing/editing in the per-tab Text Editor.

    If the graph is unmodified from an existing .conf file on disk, returns the disk file's
    exact contents (preserving comments and includes). Otherwise serializes the current
    GraphViewModel state into .conf text.
    """
    if vm.source_file and os.path.isfile(vm.source_file) and not vm.dirty:
        try:
            with open(vm.source_file, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except Exception:
            pass
    return serialize_graph_for_save(vm, topology2_root)


def serialize_graph_for_save(vm: GraphViewModel, topology2_root: str) -> str:
    """Serialize a GraphViewModel into Topology2 (.conf) text according to its view_kind:
    - 'module' -> Class.Widget.<name> definition
    - 'pipeline' -> Class.Pipeline.<name> feature topology definition
    - 'function_topology' -> Reusable multi-pipeline function topology block
    - 'topology' -> Full monolithic machine topology .conf
    """
    if vm.view_kind == "module":
        mod_node = next(
            (n for n in vm.nodes.values() if n.class_category == "Widget"),
            None,
        )
        mname = vm.title.replace("Class.Widget.", "").strip() or (mod_node.class_name if mod_node else "custom-module")
        if mod_node:
            uuid_val = mod_node.config.get_scalar("uuid", "97:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00")
            wtype = mod_node.config.get_scalar("type", "effect")
            cpc = mod_node.config.get_scalar("cpc", "1600")
            bss = mod_node.config.get_scalar("bss_size", "512")
            in_pins = mod_node.config.get_scalar("num_input_pins", str(len(mod_node.in_ports) or 1))
            out_pins = mod_node.config.get_scalar("num_output_pins", str(len(mod_node.out_ports) or 1))
        else:
            uuid_val = "97:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00"
            wtype = "effect"
            cpc = "1600"
            bss = "512"
            in_pins = "1"
            out_pins = "1"

        return f"""# SPDX-License-Identifier: BSD-3-Clause
# Generated by SOF Topology2 Visual Graph Editor (Module)

Class.Widget."{mname}" {{
\tDefineAttribute."instance" {{}}
\tDefineAttribute."index" {{}}
\tDefineAttribute."type" {{}}
\tDefineAttribute."uuid" {{}}
\tDefineAttribute."cpc" {{}}
\tDefineAttribute."bss_size" {{}}
\tDefineAttribute."num_input_pins" {{}}
\tDefineAttribute."num_output_pins" {{}}

\tattributes {{
\t\t!constructor [
\t\t\t"index"
\t\t\t"instance"
\t\t]
\t\t!mandatory [
\t\t\t"num_input_audio_formats"
\t\t\t"num_output_audio_formats"
\t\t]
\t\tunique\t"instance"
\t}}

\ttype\t\t\t"{wtype}"
\tuuid\t\t\t"{uuid_val}"
\tcpc\t\t\t{cpc}
\tbss_size\t\t{bss}
\tnum_input_pins\t\t{in_pins}
\tnum_output_pins\t\t{out_pins}
}}
"""

    if vm.view_kind == "pipeline":
        pname = vm.title.replace("Class.Pipeline.", "").strip() or "custom-feature-pipeline"
        cont = next(iter(vm.containers.values()), None)
        pdir = cont.direction if cont else "playback"
        wnodes = [
            n for n in vm.nodes.values()
            if n.class_category not in ("Control", "TestbenchIO")
        ]
        w_blocks: List[str] = []
        for wn in wnodes:
            w_blocks.append(f'\t\t{wn.class_name}."{wn.instance_id}" {{\n\t\t}}')
        routes_blocks: List[str] = []
        for e in vm.edges:
            u = vm.nodes.get(e.source_node)
            v = vm.nodes.get(e.sink_node)
            if not u or not v or u.class_category in ("Control", "TestbenchIO") or v.class_category in ("Control", "TestbenchIO"):
                continue
            routes_blocks.append(f'\t\t{{\n\t\t\tsource\t"{u.id}"\n\t\t\tsink\t"{v.id}"\n\t\t}}')

        w_str = "\n".join(w_blocks)
        r_str = "\n".join(routes_blocks)
        return f"""# SPDX-License-Identifier: BSD-3-Clause
# Generated by SOF Topology2 Visual Graph Editor (Feature Topology Pipeline)

Class.Pipeline."{pname}" {{
\tDefineAttribute."index" {{}}
\tDefineAttribute."direction" {{
\t\tvalue\t"{pdir}"
\t}}
\tattributes {{
\t\t!constructor [
\t\t\t"index"
\t\t]
\t\tunique\t"index"
\t}}
\tObject.Widget {{
\t\tpipeline."1" {{
\t\t\tindex $index
\t\t}}
{w_str}
\t}}
\tObject.Base.route [
{r_str}
\t]
}}
"""

    if vm.view_kind == "function_topology":
        pipe_blocks: List[str] = []
        for pid, cont in sorted(vm.containers.items()):
            w_in_p = [
                n for n in vm.nodes.values()
                if n.pipeline_id == pid and n.class_category not in ("Control", "TestbenchIO")
            ]
            w_lines: List[str] = []
            for wn in w_in_p:
                w_lines.append(f'\t\t\tObject.Widget.{wn.class_name}."{wn.instance_id}" {{\n\t\t\t\tindex {pid}\n\t\t\t}}')
            w_joined = "\n".join(w_lines)
            pipe_blocks.append(
                f"""\t{cont.class_name} [
\t\t{{
\t\t\tindex {pid}
\t\t\tdirection {cont.direction}
{w_joined}
\t\t}}
\t]"""
            )
        r_blocks: List[str] = []
        for e in vm.edges:
            u = vm.nodes.get(e.source_node)
            v = vm.nodes.get(e.sink_node)
            if not u or not v or u.class_category in ("Control", "TestbenchIO") or v.class_category in ("Control", "TestbenchIO"):
                continue
            r_blocks.append(f'\t{{\n\t\tsource\t"{u.id}"\n\t\tsink\t"{v.id}"\n\t}}')
        p_str = "\n".join(pipe_blocks)
        r_str = "\n".join(r_blocks)
        return f"""# SPDX-License-Identifier: BSD-3-Clause
# Generated by SOF Topology2 Visual Graph Editor (Function Topology)

Object.Pipeline {{
{p_str}
}}

Object.Base.route [
{r_str}
]
"""

    conf_text, _, _ = generate_compilable_conf(vm, topology2_root)
    return conf_text
