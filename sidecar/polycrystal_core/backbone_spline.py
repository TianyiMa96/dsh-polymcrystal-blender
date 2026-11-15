# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 6: polymer backbone path extraction for smooth curve-tube rendering.

For each rank-1 chain (or long rank-0 oligomer with > 12 backbone atoms) the
pendant side groups (H, halogens, methyl trees) are pruned and the backbone is
ordered along the periodic cycle (rank 1) or the longest path (rank 0). Output
is a set of ordered polylines in CSR form (points + per-curve offsets).
"""
from __future__ import annotations

from collections import deque

import numpy as np

from .pbc_topology import BondGraph, Component

PENDANT_Z = {1, 9, 17}  # H, F, Cl treated as monovalent pendants


def _shift_lookup(graph: BondGraph):
    lut = {}
    for (u, v, t) in graph.edges():
        t = np.array(t, int)
        lut[(u, v)] = t
        lut[(v, u)] = -t
    return lut


def _prune_core(comp: Component, graph: BondGraph, z):
    nodes = set(comp.nodes)
    adj = {u: set() for u in nodes}
    for (u, v, t) in graph.edges():
        if u in nodes and v in nodes and z[u] not in PENDANT_Z and z[v] not in PENDANT_Z:
            adj[u].add(v)
            adj[v].add(u)
    deg = {u: len(adj[u]) for u in nodes}
    q = deque(u for u in nodes if deg[u] <= 1)
    removed = set()
    while q:
        u = q.popleft()
        if u in removed:
            continue
        removed.add(u)
        for v in adj[u]:
            if v not in removed:
                deg[v] -= 1
                if deg[v] == 1:
                    q.append(v)
    core = [u for u in sorted(nodes) if u not in removed]
    core_adj = {u: sorted(v for v in adj[u] if v not in removed) for u in core}
    return core, core_adj


def _order_cycle(core, core_adj):
    if not core:
        return []
    start = core[0]
    order = [start]
    prev, cur = None, start
    while True:
        nxts = [v for v in core_adj[cur] if v != prev]
        if not nxts:
            break
        nxt = nxts[0]
        if nxt == start:
            break
        order.append(nxt)
        prev, cur = cur, nxt
        if len(order) > len(core) + 1:
            break
    return order


def _longest_path(core, core_adj):
    if not core:
        return []

    def bfs(src):
        dist = {src: 0}
        parent = {src: None}
        q = deque([src])
        far = src
        while q:
            u = q.popleft()
            for v in core_adj[u]:
                if v not in dist:
                    dist[v] = dist[u] + 1
                    parent[v] = u
                    q.append(v)
                    if dist[v] > dist[far]:
                        far = v
        return far, parent

    a, _ = bfs(core[0])
    b, parent = bfs(a)
    path = []
    cur = b
    while cur is not None:
        path.append(cur)
        cur = parent[cur]
    return path[::-1]


def extract_backbones(structure, graph: BondGraph, components, realized):
    """Return (backbone_points (P,3), offsets (C+1,), is_backbone (N,))."""
    z = structure.atomic_numbers
    frac = structure.frac_coords
    cell = structure.cell
    lut = _shift_lookup(graph)
    n_real = len(realized.atomic_numbers)
    is_backbone = np.zeros(n_real, dtype=bool)

    # Map base atom -> representative realized index (non-ghost, non-pad).
    base_to_real = {}
    for ri in range(n_real):
        b = int(realized.base_index[ri])
        if not realized.is_ghost[ri] and not realized.is_boundary_pad[ri]:
            base_to_real.setdefault(b, ri)

    curves = []
    continuous = realized.mode in ("chain_continuous", "pbc_cluster")
    for comp in components:
        core, core_adj = _prune_core(comp, graph, z)
        if comp.rank == 1:
            order = _order_cycle(core, core_adj)
        elif comp.rank == 0 and len(core) > 12:
            order = _longest_path(core, core_adj)
        else:
            continue
        if len(order) < 2:
            continue

        if continuous:
            if comp.rank == 1 and comp.axis is not None:
                # The chain is unwrapped into a linear strand: order backbone
                # nodes along the chain axis instead of cyclically.
                direction = np.asarray(comp.axis, float) @ cell
                direction = direction / (np.linalg.norm(direction) or 1.0)
                order = sorted(order, key=lambda u: float(realized.positions[u] @ direction))
            pts = realized.positions[order]
        else:
            # Unwrap one repeat along the path, then place inside the box.
            sigma = np.zeros(3, int)
            fpts = [frac[order[0]]]
            for a, b in zip(order[:-1], order[1:]):
                sigma = sigma + lut.get((a, b), np.zeros(3, int))
                fpts.append(frac[b] + sigma)
            fpts = np.array(fpts)
            if comp.rank == 1:
                fpts -= np.floor(fpts[0])
            else:
                fpts -= np.floor(fpts.mean(axis=0))
            pts = fpts @ cell

        curves.append(np.asarray(pts, float))
        for node in order:
            ri = base_to_real.get(node)
            if ri is not None:
                is_backbone[ri] = True

    if curves:
        points = np.vstack(curves)
        offsets = np.zeros(len(curves) + 1, dtype=int)
        for i, c in enumerate(curves):
            offsets[i + 1] = offsets[i] + len(c)
    else:
        points = np.zeros((0, 3))
        offsets = np.zeros(1, dtype=int)
    return points, offsets, is_backbone