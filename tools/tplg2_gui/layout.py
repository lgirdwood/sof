# SPDX-License-Identifier: BSD-3-Clause
"""Auto-layout engine (layered DAG + pipeline swimlane containers) for Topology2 graphs."""

from __future__ import annotations
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple

from .model import GraphEdge, GraphNode, GraphViewModel, PipelineContainer


NODE_WIDTH = 200.0
NODE_BASE_HEIGHT = 86.0
CONTROL_WIDTH = 118.0
CONTROL_MIN_HEIGHT = 42.0
CTRL_VOL_WIDTH = 56.0
CTRL_VOL_HEIGHT = 92.0
CTRL_COMPACT_WIDTH = 118.0
CTRL_COMPACT_HEIGHT = 42.0
CTRL_TOP_GAP = 22.0
CTRL_H_GAP = 6.0
CTRL_V_GAP = 6.0
CTRL_GROUP_PAD = 6.0
CTRL_GROUP_HDR = 15.0
TB_IO_WIDTH = 224.0
TB_IO_HEIGHT = 98.0
TB_IO_GAP = 48.0
H_GAP = 72.0
V_GAP = 28.0
CONTAINER_PAD_X = 26.0
CONTAINER_PAD_TOP = 44.0
CONTAINER_PAD_BOTTOM = 22.0
CONTAINER_GAP_X = 95.0
CONTAINER_GAP_Y = 48.0


def _is_node_capture(vm: GraphViewModel, node: GraphNode) -> bool:
    if node.pipeline_id is not None and node.pipeline_id in vm.containers:
        if "capture" in vm.containers[node.pipeline_id].direction.lower():
            return True
    if "capture" in node.id.lower() or "capture" in node.subtitle.lower():
        return True
    wdir = str(node.config.get_scalar("direction", "")).lower()
    wtype = str(node.config.get_scalar("type", "")).lower()
    if wdir == "capture" or wtype == "dai_out":
        return True
    return "capture" in vm.title.lower()


def layout_graph(vm: GraphViewModel) -> None:
    """Compute (x, y, width, height) for all nodes, ports, and pipeline containers in vm."""
    if not vm.nodes:
        return

    # Propagate capture direction to any container that holds capture nodes
    for node in vm.nodes.values():
        if node.pipeline_id is not None and node.pipeline_id in vm.containers:
            wdir = str(node.config.get_scalar("direction", "")).lower()
            wtype = str(node.config.get_scalar("type", "")).lower()
            if "capture" in node.id.lower() or wdir == "capture" or wtype == "dai_out":
                vm.containers[node.pipeline_id].direction = "capture"

    # 1. Size each node and position its ports (Playback: in=left/out=right, Capture: in=right/out=left)
    for node in vm.nodes.values():
        _compute_node_geometry(node, is_capture=_is_node_capture(vm, node))

    # 2. If there are pipeline containers and multiple nodes, layout per pipeline container
    if vm.containers and vm.view_kind in ("topology", "pipeline"):
        _layout_with_containers(vm)
    else:
        any_cap = any(
            _is_node_capture(vm, n)
            for n in vm.nodes.values()
            if n.class_category not in ("Control", "TestbenchIO")
        )
        non_tb_ids = [nid for nid, n in vm.nodes.items() if n.class_category != "TestbenchIO"]
        has_left_tb = any(
            n.class_category == "TestbenchIO"
            and str(n.config.get_scalar("side", "left")).lower() == "left"
            for n in vm.nodes.values()
        )
        sx = (60.0 + TB_IO_WIDTH + TB_IO_GAP) if has_left_tb else 60.0
        _layout_flat_dag(vm, non_tb_ids, start_x=sx, start_y=60.0, is_capture=any_cap)

    # 3. Position TestbenchIO nodes to the left of host-copiers and right of dai/alh-copiers
    _position_testbench_io_nodes(vm)

    # 4. Assign distinct source/sink pins to multi-edge nodes ordered top-to-bottom to avoid pin collisions
    _assign_edge_ports(vm)


def _position_testbench_io_nodes(vm: GraphViewModel) -> None:
    """Position TestbenchIO nodes to the left of host-copiers and to the right of dai/alh-copiers."""
    for node in vm.nodes.values():
        if node.class_category != "TestbenchIO" or not node.parent_node_id:
            continue
        parent = vm.nodes.get(node.parent_node_id)
        if not parent:
            continue
        side = str(node.config.get_scalar("side", "left")).lower()
        if side == "right":
            if parent.pipeline_id is not None and parent.pipeline_id in vm.containers:
                right_anchor = max(
                    vm.containers[parent.pipeline_id].x + vm.containers[parent.pipeline_id].width,
                    parent.x + parent.width,
                )
            else:
                right_anchor = parent.x + parent.width
            node.x = right_anchor + TB_IO_GAP
            node.y = parent.y + (parent.height - node.height) / 2.0
        else:
            if parent.pipeline_id is not None and parent.pipeline_id in vm.containers:
                left_anchor = min(vm.containers[parent.pipeline_id].x, parent.x)
            else:
                left_anchor = parent.x
            node.x = left_anchor - node.width - TB_IO_GAP
            node.y = parent.y + (parent.height - node.height) / 2.0


def _assign_edge_ports(vm: GraphViewModel) -> None:
    """Assign distinct output/input ports (out0, out1, ... / in0, in1, ...) to audio edges."""
    out_edges_by_node: Dict[str, List[GraphEdge]] = defaultdict(list)
    in_edges_by_node: Dict[str, List[GraphEdge]] = defaultdict(list)

    for edge in vm.edges:
        u = vm.nodes.get(edge.source_node)
        v = vm.nodes.get(edge.sink_node)
        if not u or not v or u.class_category == "Control":
            continue
        out_edges_by_node[u.id].append(edge)
        in_edges_by_node[v.id].append(edge)

    for uid, edges in out_edges_by_node.items():
        u = vm.nodes[uid]
        if not u.out_ports:
            continue
        sorted_out = sorted(
            edges,
            key=lambda e: (
                vm.nodes[e.sink_node].y + vm.nodes[e.sink_node].height / 2.0,
                vm.nodes[e.sink_node].x,
                e.id,
            ),
        )
        for idx, edge in enumerate(sorted_out):
            port_idx = min(idx, len(u.out_ports) - 1)
            edge.source_port = u.out_ports[port_idx].id

    for vid, edges in in_edges_by_node.items():
        v = vm.nodes[vid]
        if not v.in_ports:
            continue
        sorted_in = sorted(
            edges,
            key=lambda e: (
                vm.nodes[e.source_node].y + vm.nodes[e.source_node].height / 2.0,
                vm.nodes[e.source_node].x,
                e.id,
            ),
        )
        for idx, edge in enumerate(sorted_in):
            port_idx = min(idx, len(v.in_ports) - 1)
            edge.sink_port = v.in_ports[port_idx].id


def _compute_node_geometry(node: GraphNode, is_capture: bool = False) -> None:
    if node.class_category == "TestbenchIO":
        hdr_w = len(node.title + "  [ACTIVE]") * 7.2 + 36.0
        node.width = max(TB_IO_WIDTH, hdr_w)
        node.height = TB_IO_HEIGHT
        side = str(node.config.get_scalar("side", "left")).lower()
        # Left TestbenchIO (host-copier) connects on its right edge; Right TestbenchIO (dai/alh-copier) connects on its left edge
        port_x = 0.0 if side == "right" else node.width
        for port in node.out_ports:
            port.rel_x = port_x
            port.rel_y = node.height / 2.0
        for port in node.in_ports:
            port.rel_x = port_x
            port.rel_y = node.height / 2.0
        return

    if node.class_category == "Control":
        if node.category == "control_mixer":
            node.width = CTRL_VOL_WIDTH
            node.height = CTRL_VOL_HEIGHT
        else:
            node.width = CTRL_COMPACT_WIDTH
            node.height = CTRL_COMPACT_HEIGHT
        # Graphical kcontrol cards connect from their top-center port up to the bottom of the parent widget
        for port in node.out_ports:
            port.rel_x = node.width / 2.0
            port.rel_y = 0.0
        for port in node.in_ports:
            port.rel_x = node.width / 2.0
            port.rel_y = 0.0
        return

    num_ports = max(len(node.in_ports), len(node.out_ports), 1)
    has_bottom_row = bool(node.in_fmt_badge or node.out_fmt_badge or node.badges)
    badge_extra = 20.0 if has_bottom_row else 0.0
    title_w = len(node.title) * 7.6 + 28.0
    sub_w = len(node.subtitle) * 6.6 + 28.0
    badge_w = sum(len(b) * 6.5 + 18.0 for b in node.badges[:2]) + 24.0 if node.badges else 0.0
    in_fmt_w = (len(node.in_fmt_badge) * 6.6 + 16.0) if node.in_fmt_badge else 0.0
    out_fmt_w = (len(node.out_fmt_badge) * 6.6 + 16.0) if node.out_fmt_badge else 0.0
    fmt_row_w = in_fmt_w + out_fmt_w + (36.0 if (in_fmt_w and out_fmt_w) else 24.0)

    node.width = max(NODE_WIDTH, title_w, sub_w, badge_w, fmt_row_w)
    node.height = max(NODE_BASE_HEIGHT, 54.0 + num_ports * 18.0 + badge_extra)

    # Playback flows Left -> Right (in_ports on left, out_ports on right).
    # Capture flows Right -> Left from DAI/ALH on right to Host on left (in_ports on right, out_ports on left).
    in_x = node.width if is_capture else 0.0
    out_x = 0.0 if is_capture else node.width

    in_count = len(node.in_ports)
    for i, port in enumerate(node.in_ports):
        port.rel_x = in_x
        port.rel_y = 34.0 + (i + 0.5) * ((node.height - 38.0) / max(in_count, 1))

    out_count = len(node.out_ports)
    for i, port in enumerate(node.out_ports):
        port.rel_x = out_x
        port.rel_y = 34.0 + (i + 0.5) * ((node.height - 38.0) / max(out_count, 1))


def _compute_dag_layers(node_ids: List[str], edges: List[GraphEdge]) -> List[List[str]]:
    """Assign nodes to left-to-right topological columns."""
    id_set = set(node_ids)
    adj: Dict[str, List[str]] = defaultdict(list)
    in_deg: Dict[str, int] = {nid: 0 for nid in node_ids}

    for e in edges:
        if e.source_node in id_set and e.sink_node in id_set and e.source_node != e.sink_node:
            adj[e.source_node].append(e.sink_node)
            in_deg[e.sink_node] += 1

    # Longest-path layering via Kahn's algorithm
    layer_of: Dict[str, int] = {nid: 0 for nid in node_ids}
    q = deque([nid for nid in node_ids if in_deg[nid] == 0])
    visited: Set[str] = set()

    while q:
        u = q.popleft()
        visited.add(u)
        for v in adj[u]:
            layer_of[v] = max(layer_of[v], layer_of[u] + 1)
            in_deg[v] -= 1
            if in_deg[v] == 0:
                q.append(v)

    # Any cycle remnants get placed after visited
    for nid in node_ids:
        if nid not in visited:
            layer_of[nid] = max(layer_of.values(), default=0) + 1

    max_layer = max(layer_of.values(), default=0)
    layers: List[List[str]] = [[] for _ in range(max_layer + 1)]
    for nid in node_ids:
        layers[layer_of[nid]].append(nid)
    return [lyr for lyr in layers if lyr]


def _layout_flat_dag(
    vm: GraphViewModel,
    node_ids: List[str],
    start_x: float = 60.0,
    start_y: float = 60.0,
    is_capture: bool = False,
) -> Tuple[float, float]:
    """Layout a subset of nodes into left-to-right columns starting at (start_x, start_y),
    with host-copiers on the left, dai/alh-copiers on the right, and kcontrol nodes below their parent.
    Returns (total_width, total_height).
    """
    if not node_ids:
        return (0.0, 0.0)

    id_set = set(node_ids)
    main_ids: List[str] = []
    ctrl_by_parent: Dict[str, List[str]] = defaultdict(list)

    for nid in node_ids:
        node = vm.nodes[nid]
        if node.class_category == "Control" and node.parent_node_id and node.parent_node_id in id_set:
            ctrl_by_parent[node.parent_node_id].append(nid)
        elif node.class_category != "TestbenchIO":
            main_ids.append(nid)

    if not main_ids:
        main_ids = list(node_ids)
        ctrl_by_parent.clear()

    # Keep host-copier on the left (rank 1) and dai-copier/alh-copier on the right (rank 4)
    def node_role_rank(nid: str) -> int:
        n = vm.nodes[nid]
        if n.class_category == "Control":
            return 0
        if "host-copier" in n.class_name:
            return 1
        if n.class_name == "mixout":
            return 1
        if n.class_name == "module-copier":
            return 1 if is_capture else 3
        if n.class_name == "mixin":
            return 3
        if "dai-copier" in n.class_name or "alh-copier" in n.class_name:
            return 4
        return 2

    ordered_ids = sorted(main_ids, key=lambda nid: (node_role_rank(nid), nid))
    if is_capture:
        # In Capture, audio flows from DAI/ALH (right) -> Host (left), so reverse edge direction
        # when assigning left-to-right columns so Host/module-copier is on the left and DAI/ALH is on the right.
        dag_edges = [
            GraphEdge(id=e.id, source_node=e.sink_node, sink_node=e.source_node)
            for e in vm.edges
        ]
    else:
        dag_edges = vm.edges
    layers = _compute_dag_layers(ordered_ids, dag_edges)

    def _measure_and_place_module_controls(
        ctrls: List[str],
        origin_x: float = 0.0,
        origin_y: float = 0.0,
        apply_pos: bool = False,
    ) -> Tuple[float, float]:
        """Arrange a module's kcontrols compactly:
        - Vertical volume faders (control_mixer, 56x92) placed on the left
        - Shrunk Switch / Enum / Bytes controls (118x42) stacked up to 2 per column on the right
        Returns (inner_width, inner_height) of the control cluster.
        """
        if not ctrls:
            return (0.0, 0.0)
        vol_ids = [cid for cid in ctrls if vm.nodes[cid].category == "control_mixer"]
        compact_ids = [cid for cid in ctrls if vm.nodes[cid].category != "control_mixer"]

        cx = origin_x
        max_h = 0.0

        for cid in vol_ids:
            cnode = vm.nodes[cid]
            if apply_pos:
                cnode.x = cx
                cnode.y = origin_y
            cx += cnode.width + CTRL_H_GAP
            max_h = max(max_h, cnode.height)

        # Stack compact controls (switch, enum, bytes) in columns of 2
        for col_start in range(0, len(compact_ids), 2):
            col_slice = compact_ids[col_start : col_start + 2]
            col_w = max(vm.nodes[cid].width for cid in col_slice)
            cy = origin_y
            for idx, cid in enumerate(col_slice):
                cnode = vm.nodes[cid]
                if apply_pos:
                    cnode.x = cx
                    cnode.y = cy
                cy += cnode.height + (CTRL_V_GAP if idx < len(col_slice) - 1 else 0.0)
            max_h = max(max_h, cy - origin_y)
            cx += col_w + CTRL_H_GAP

        total_inner_w = max(0.0, cx - origin_x - CTRL_H_GAP)
        return (total_inner_w, max_h)

    def group_width(nid: str) -> float:
        w = vm.nodes[nid].width
        ctrls = ctrl_by_parent.get(nid, [])
        if ctrls:
            inner_w, _ = _measure_and_place_module_controls(ctrls, apply_pos=False)
            w = max(w, inner_w + 2.0 * CTRL_GROUP_PAD)
        return w

    def group_height(nid: str) -> float:
        h = vm.nodes[nid].height
        ctrls = ctrl_by_parent.get(nid, [])
        if ctrls:
            _, inner_h = _measure_and_place_module_controls(ctrls, apply_pos=False)
            h += CTRL_TOP_GAP + inner_h + CTRL_GROUP_PAD
        return h

    max_col_h = 0.0
    col_heights: List[float] = []
    for lyr in layers:
        h = sum(group_height(nid) for nid in lyr) + max(0, len(lyr) - 1) * V_GAP
        col_heights.append(h)
        max_col_h = max(max_col_h, h)

    cur_x = start_x
    for col_idx, lyr in enumerate(layers):
        col_h = col_heights[col_idx]
        cur_y = start_y + (max_col_h - col_h) / 2.0
        col_w = max(group_width(nid) for nid in lyr)
        for nid in lyr:
            node = vm.nodes[nid]
            gw = group_width(nid)
            node.x = cur_x + (gw - node.width) / 2.0
            node.y = cur_y
            # Place attached kcontrol nodes in a compact module group directly beneath the parent widget node
            ctrls = ctrl_by_parent.get(nid, [])
            if ctrls:
                inner_w, _ = _measure_and_place_module_controls(ctrls, apply_pos=False)
                ctrl_x = node.x + (node.width - inner_w) / 2.0
                ctrl_y = node.y + node.height + CTRL_TOP_GAP
                _measure_and_place_module_controls(ctrls, origin_x=ctrl_x, origin_y=ctrl_y, apply_pos=True)
            cur_y += group_height(nid) + V_GAP
        cur_x += col_w + H_GAP

    total_w = max(0.0, cur_x - start_x - H_GAP)
    return (total_w, max_col_h)


def _layout_with_containers(vm: GraphViewModel) -> None:
    """Layout nodes inside pipeline containers and arrange containers into FE -> BE columns."""
    by_pipe: Dict[int, List[str]] = defaultdict(list)
    orphan_ids: List[str] = []

    for nid, node in vm.nodes.items():
        if node.class_category == "TestbenchIO":
            continue
        if node.pipeline_id is not None and node.pipeline_id in vm.containers:
            by_pipe[node.pipeline_id].append(nid)
        else:
            orphan_ids.append(nid)

    # First layout nodes inside each container at local origin (0, 0) to get container dimensions
    container_sizes: Dict[int, Tuple[float, float]] = {}
    for pid, container in sorted(vm.containers.items()):
        member_ids = by_pipe.get(pid, [])
        header_txt = f"Pipeline {pid}: {container.class_name} ({container.direction})"
        warn_extra = 42.0 if container.warning_text else 0.0
        min_header_w = len(header_txt) * 7.5 + 34.0 + warn_extra
        if not member_ids:
            container.width = max(NODE_WIDTH + 2 * CONTAINER_PAD_X, min_header_w)
            container.height = NODE_BASE_HEIGHT + CONTAINER_PAD_TOP + CONTAINER_PAD_BOTTOM
            container_sizes[pid] = (container.width, container.height)
            continue
        inner_is_cap = "capture" in container.direction.lower()
        inner_w, inner_h = _layout_flat_dag(vm, member_ids, start_x=0.0, start_y=0.0, is_capture=inner_is_cap)
        container.width = max(inner_w + 2 * CONTAINER_PAD_X, min_header_w)
        container.height = inner_h + CONTAINER_PAD_TOP + CONTAINER_PAD_BOTTOM
        container_sizes[pid] = (container.width, container.height)

    # Build inter-pipeline DAG to arrange FE (host-copier on left) and BE (dai/alh-copier on right) columns
    pipe_ids = sorted(vm.containers.keys())
    pipe_adj: Dict[int, Set[int]] = defaultdict(set)
    undirected: Dict[int, Set[int]] = defaultdict(set)

    for e in vm.edges:
        u_node = vm.nodes.get(e.source_node)
        v_node = vm.nodes.get(e.sink_node)
        if u_node and v_node and u_node.pipeline_id is not None and v_node.pipeline_id is not None:
            up = u_node.pipeline_id
            vp = v_node.pipeline_id
            if up != vp and up in vm.containers and vp in vm.containers:
                pipe_adj[up].add(vp)
                undirected[up].add(vp)
                undirected[vp].add(up)

    def pipe_has_dai(pid: int) -> bool:
        return any(
            "dai-copier" in vm.nodes[nid].class_name or "alh-copier" in vm.nodes[nid].class_name
            for nid in by_pipe.get(pid, [])
        )

    def pipe_has_host(pid: int) -> bool:
        return any("host-copier" in vm.nodes[nid].class_name for nid in by_pipe.get(pid, []))

    visited_pipes: Set[int] = set()
    components: List[List[int]] = []
    for pid in pipe_ids:
        if pid in visited_pipes:
            continue
        comp: List[int] = []
        dq = deque([pid])
        visited_pipes.add(pid)
        while dq:
            cur = dq.popleft()
            comp.append(cur)
            for nb in sorted(undirected[cur]):
                if nb not in visited_pipes:
                    visited_pipes.add(nb)
                    dq.append(nb)
        components.append(sorted(comp))

    # Compute left-to-right column index per component so Host pipelines are always on the left
    # and DAI/ALH pipelines are always on the right (for both Playback and Capture).
    pipe_col: Dict[int, int] = {pid: 0 for pid in pipe_ids}
    for comp in components:
        comp_is_cap = any("capture" in vm.containers[p].direction.lower() for p in comp)
        adj_in_comp: Dict[int, Set[int]] = defaultdict(set)
        in_deg: Dict[int, int] = {p: 0 for p in comp}
        for u in comp:
            for v in pipe_adj[u]:
                if v in in_deg:
                    if comp_is_cap:
                        # Reverse capture inter-pipeline edge so Host (sink) is column 0 (left)
                        # and DAI/ALH (source) is the rightmost column.
                        adj_in_comp[v].add(u)
                        in_deg[u] += 1
                    else:
                        adj_in_comp[u].add(v)
                        in_deg[v] += 1

        q = deque([p for p in comp if in_deg[p] == 0])
        while q:
            u = q.popleft()
            for v in sorted(adj_in_comp[u]):
                pipe_col[v] = max(pipe_col[v], pipe_col[u] + 1)
                in_deg[v] -= 1
                if in_deg[v] == 0:
                    q.append(v)

        # Ensure standalone DAI/ALH-only pipelines sit on the right-hand column (column 1)
        if len(comp) == 1 and pipe_has_dai(comp[0]) and not pipe_has_host(comp[0]):
            pipe_col[comp[0]] = 1

    has_tb_io = any(n.class_category == "TestbenchIO" for n in vm.nodes.values())
    base_col_x = (50.0 + TB_IO_WIDTH + TB_IO_GAP) if has_tb_io else 50.0
    default_col0_w = max(
        (vm.containers[p].width for p in pipe_ids if pipe_col[p] == 0),
        default=440.0,
    )
    cur_row_y = 50.0
    for comp in components:
        # Group pipelines in this connected component by their column
        cols: Dict[int, List[int]] = defaultdict(list)
        for pid in comp:
            cols[pipe_col[pid]].append(pid)

        max_comp_h = 0.0
        col_x = base_col_x
        if 0 not in cols and max(cols.keys(), default=0) >= 1:
            col_x += default_col0_w + CONTAINER_GAP_X
        for c_idx in sorted(cols.keys()):
            pids_in_col = cols[c_idx]
            col_w = max(vm.containers[p].width for p in pids_in_col)
            cy = cur_row_y
            for p in pids_in_col:
                cont = vm.containers[p]
                cont.x = col_x
                cont.y = cy
                # Translate member nodes from (0,0) local space into container world space
                for nid in by_pipe.get(p, []):
                    vm.nodes[nid].x += cont.x + CONTAINER_PAD_X
                    vm.nodes[nid].y += cont.y + CONTAINER_PAD_TOP
                cy += cont.height + CONTAINER_GAP_Y
            col_total_h = cy - cur_row_y - CONTAINER_GAP_Y
            max_comp_h = max(max_comp_h, col_total_h)
            col_x += col_w + CONTAINER_GAP_X

        cur_row_y += max_comp_h + CONTAINER_GAP_Y * 1.4

    # Layout any orphan nodes below the containers
    if orphan_ids:
        _layout_flat_dag(vm, orphan_ids, start_x=50.0, start_y=cur_row_y)


def update_container_bounds_from_nodes(vm: GraphViewModel) -> None:
    """Recompute PipelineContainer bounding boxes after a user drags a node."""
    by_pipe: Dict[int, List[GraphNode]] = defaultdict(list)
    for node in vm.nodes.values():
        if node.pipeline_id is not None and node.pipeline_id in vm.containers:
            by_pipe[node.pipeline_id].append(node)

    for pid, cont in vm.containers.items():
        members = by_pipe.get(pid, [])
        if not members:
            continue
        min_x = min(n.x - (CTRL_GROUP_PAD if n.class_category == "Control" else 0.0) for n in members)
        min_y = min(n.y - (CTRL_GROUP_HDR if n.class_category == "Control" else 0.0) for n in members)
        max_x = max(n.x + n.width + (CTRL_GROUP_PAD if n.class_category == "Control" else 0.0) for n in members)
        max_y = max(n.y + n.height + (CTRL_GROUP_PAD if n.class_category == "Control" else 0.0) for n in members)
        cont.x = min_x - CONTAINER_PAD_X
        cont.y = min_y - CONTAINER_PAD_TOP
        cont.width = (max_x - min_x) + 2 * CONTAINER_PAD_X
        cont.height = (max_y - min_y) + CONTAINER_PAD_TOP + CONTAINER_PAD_BOTTOM
