# SPDX-License-Identifier: BSD-3-Clause
"""Auto-layout engine (layered DAG + pipeline swimlane containers) for Topology2 graphs."""

from __future__ import annotations
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple

from .model import GraphEdge, GraphNode, GraphViewModel, PipelineContainer


NODE_WIDTH = 200.0
NODE_BASE_HEIGHT = 86.0
H_GAP = 72.0
V_GAP = 28.0
CONTAINER_PAD_X = 26.0
CONTAINER_PAD_TOP = 44.0
CONTAINER_PAD_BOTTOM = 22.0
CONTAINER_GAP_X = 95.0
CONTAINER_GAP_Y = 48.0


def layout_graph(vm: GraphViewModel) -> None:
    """Compute (x, y, width, height) for all nodes, ports, and pipeline containers in vm."""
    if not vm.nodes:
        return

    # 1. Size each node and position its ports
    for node in vm.nodes.values():
        _compute_node_geometry(node)

    # 2. If there are pipeline containers and multiple nodes, layout per pipeline container
    if vm.containers and vm.view_kind in ("topology", "pipeline"):
        _layout_with_containers(vm)
    else:
        _layout_flat_dag(vm, list(vm.nodes.keys()), start_x=60.0, start_y=60.0)


def _compute_node_geometry(node: GraphNode) -> None:
    num_ports = max(len(node.in_ports), len(node.out_ports), 1)
    badge_extra = 18.0 if node.badges else 0.0
    title_w = len(node.title) * 7.6 + 28.0
    sub_w = len(node.subtitle) * 6.6 + 28.0
    badge_w = sum(len(b) * 6.5 + 18.0 for b in node.badges[:2]) + 24.0 if node.badges else 0.0
    node.width = max(NODE_WIDTH, title_w, sub_w, badge_w)
    node.height = max(NODE_BASE_HEIGHT, 54.0 + num_ports * 18.0 + badge_extra)

    # Position input ports along left edge
    in_count = len(node.in_ports)
    for i, port in enumerate(node.in_ports):
        port.rel_x = 0.0
        port.rel_y = 34.0 + (i + 0.5) * ((node.height - 38.0) / max(in_count, 1))

    # Position output ports along right edge
    out_count = len(node.out_ports)
    for i, port in enumerate(node.out_ports):
        port.rel_x = node.width
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
) -> Tuple[float, float]:
    """Layout a subset of nodes as a left-to-right DAG starting at (start_x, start_y).
    Returns (total_width, total_height).
    """
    if not node_ids:
        return (0.0, 0.0)

    # Heuristic source/sink ordering hint so host-copier/mixout starts on left and mixin/dai-copier ends on right
    def node_role_rank(nid: str) -> int:
        n = vm.nodes[nid]
        if n.category == "control":
            return 0
        if n.class_name in ("host-copier", "mixout") and "capture" not in nid:
            return 1
        if n.class_name == "dai-copier" and "capture" in nid:
            return 1
        if n.class_name in ("mixin", "module-copier"):
            return 3
        if n.class_name == "dai-copier" and "playback" in nid:
            return 4
        if n.class_name == "host-copier" and "capture" in nid:
            return 4
        return 2

    ordered_ids = sorted(node_ids, key=lambda nid: (node_role_rank(nid), nid))
    layers = _compute_dag_layers(ordered_ids, vm.edges)

    max_col_h = 0.0
    col_heights: List[float] = []
    for lyr in layers:
        h = sum(vm.nodes[nid].height for nid in lyr) + max(0, len(lyr) - 1) * V_GAP
        col_heights.append(h)
        max_col_h = max(max_col_h, h)

    cur_x = start_x
    for col_idx, lyr in enumerate(layers):
        col_h = col_heights[col_idx]
        cur_y = start_y + (max_col_h - col_h) / 2.0
        col_w = max(vm.nodes[nid].width for nid in lyr)
        for nid in lyr:
            node = vm.nodes[nid]
            node.x = cur_x
            node.y = cur_y
            cur_y += node.height + V_GAP
        cur_x += col_w + H_GAP

    total_w = max(0.0, cur_x - start_x - H_GAP)
    return (total_w, max_col_h)


def _layout_with_containers(vm: GraphViewModel) -> None:
    """Layout nodes inside pipeline containers and arrange containers into FE -> BE columns."""
    by_pipe: Dict[int, List[str]] = defaultdict(list)
    orphan_ids: List[str] = []

    for nid, node in vm.nodes.items():
        if node.pipeline_id is not None and node.pipeline_id in vm.containers:
            by_pipe[node.pipeline_id].append(nid)
        else:
            orphan_ids.append(nid)

    # First layout nodes inside each container at local origin (0, 0) to get container dimensions
    container_sizes: Dict[int, Tuple[float, float]] = {}
    for pid, container in sorted(vm.containers.items()):
        member_ids = by_pipe.get(pid, [])
        header_txt = f"Pipeline {pid}: {container.class_name} ({container.direction})"
        min_header_w = len(header_txt) * 7.5 + 34.0
        if not member_ids:
            container.width = max(NODE_WIDTH + 2 * CONTAINER_PAD_X, min_header_w)
            container.height = NODE_BASE_HEIGHT + CONTAINER_PAD_TOP + CONTAINER_PAD_BOTTOM
            container_sizes[pid] = (container.width, container.height)
            continue
        inner_w, inner_h = _layout_flat_dag(vm, member_ids, start_x=0.0, start_y=0.0)
        container.width = max(inner_w + 2 * CONTAINER_PAD_X, min_header_w)
        container.height = inner_h + CONTAINER_PAD_TOP + CONTAINER_PAD_BOTTOM
        container_sizes[pid] = (container.width, container.height)

    # Build inter-pipeline DAG to arrange FE and BE containers left-to-right
    pipe_ids = sorted(vm.containers.keys())
    pipe_adj: Dict[int, Set[int]] = defaultdict(set)
    pipe_in_deg: Dict[int, int] = {pid: 0 for pid in pipe_ids}

    for e in vm.edges:
        u_node = vm.nodes.get(e.source_node)
        v_node = vm.nodes.get(e.sink_node)
        if u_node and v_node and u_node.pipeline_id is not None and v_node.pipeline_id is not None:
            up = u_node.pipeline_id
            vp = v_node.pipeline_id
            if up != vp and up in vm.containers and vp in vm.containers:
                if vp not in pipe_adj[up]:
                    pipe_adj[up].add(vp)
                    pipe_in_deg[vp] += 1

    # Assign each pipeline container to a column (0 = upstream/FE, 1 = downstream/BE)
    pipe_col: Dict[int, int] = {pid: 0 for pid in pipe_ids}
    q = deque([pid for pid in pipe_ids if pipe_in_deg[pid] == 0])
    while q:
        u = q.popleft()
        for v in sorted(pipe_adj[u]):
            pipe_col[v] = max(pipe_col[v], pipe_col[u] + 1)
            pipe_in_deg[v] -= 1
            if pipe_in_deg[v] == 0:
                q.append(v)

    # Group connected pipelines into horizontal rows (connected components) so paired FE/BE sit side-by-side
    undirected: Dict[int, Set[int]] = defaultdict(set)
    for u, vs in pipe_adj.items():
        for v in vs:
            undirected[u].add(v)
            undirected[v].add(u)

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

    cur_row_y = 50.0
    for comp in components:
        # Group pipelines in this connected component by their column
        cols: Dict[int, List[int]] = defaultdict(list)
        for pid in comp:
            cols[pipe_col[pid]].append(pid)

        max_comp_h = 0.0
        col_x = 50.0
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
        min_x = min(n.x for n in members)
        min_y = min(n.y for n in members)
        max_x = max(n.x + n.width for n in members)
        max_y = max(n.y + n.height for n in members)
        cont.x = min_x - CONTAINER_PAD_X
        cont.y = min_y - CONTAINER_PAD_TOP
        cont.width = (max_x - min_x) + 2 * CONTAINER_PAD_X
        cont.height = (max_y - min_y) + CONTAINER_PAD_TOP + CONTAINER_PAD_BOTTOM
