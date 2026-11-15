# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 3 & 4: triclinic minimum-image covalent bond graph and connected
component homology (polymer rank R in {0,1,2,3}).

Pure numpy / scipy / networkx. The neighbor search prefers ``vesin`` (optional),
falls back to ``ase.neighborlist``, and finally to a self-contained 27-image
cKDTree scan so the engine works with only numpy/scipy installed.

Bond shift convention (agent.md 3.2 Stage 3): for an edge u -> v the integer
lattice shift is ``ell = -round(s_v - s_u)`` and the realized bond vector is
``(s_v + ell - s_u) @ cell``.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from math import gcd
from functools import reduce

import numpy as np
import networkx as nx

from .structure import Structure, cov_radius

D_MIN = 0.4  # Angstrom, reject absurdly short contacts


def mic_shift(s_u, s_v):
    """Minimum-image integer shift ell = -round(s_v - s_u)."""
    return -np.rint(np.asarray(s_v) - np.asarray(s_u)).astype(int)


def bond_vector(s_u, s_v, ell, cell):
    return (np.asarray(s_v) + np.asarray(ell) - np.asarray(s_u)) @ cell


def _canonical(i, j, t):
    t = tuple(int(x) for x in t)
    if i > j or (i == j and _lex_negative(t)):
        return j, i, tuple(-x for x in t)
    return i, j, t


def _lex_negative(t):
    for x in t:
        if x != 0:
            return x < 0
    return False


def _neighbors_vesin(cart, cell, cutoff):
    from vesin import NeighborList
    nl = NeighborList(cutoff=cutoff, full_list=True)
    pairs, shifts, _ = nl.compute(points=cart, box=cell, periodic=True)
    return [(int(i), int(j), tuple(int(x) for x in s))
            for (i, j), s in zip(pairs, shifts)]


def _neighbors_ase(z, cart, cell, cutoff):
    from ase import Atoms
    from ase.neighborlist import neighbor_list
    atoms = Atoms(numbers=z, positions=cart, cell=cell, pbc=True)
    i, j, S = neighbor_list("ijS", atoms, cutoff)
    return [(int(a), int(b), tuple(int(x) for x in s))
            for a, b, s in zip(i, j, S)]


def _neighbors_scipy(cart, cell, cutoff):
    from scipy.spatial import cKDTree
    tree = cKDTree(cart)
    out = []
    for t in itertools.product((-1, 0, 1), repeat=3):
        t = np.array(t, dtype=float)
        query = cart - t @ cell
        for i, js in enumerate(tree.query_ball_point(query, cutoff)):
            for j in js:
                out.append((i, int(j), tuple(int(x) for x in t)))
    return out


def periodic_neighbors(z, cart, cell, cutoff):
    """Return canonical (i, j, shift) neighbor pairs within cutoff.

    Tries vesin -> ASE -> 27-image scipy. Self-image pairs (i == j, shift != 0)
    are kept once with a lexicographically positive shift.
    """
    raw = None
    try:
        raw = _neighbors_vesin(cart, cell, cutoff)
    except Exception:
        raw = None
    if raw is None:
        try:
            raw = _neighbors_ase(z, cart, cell, cutoff)
        except Exception:
            raw = None
    if raw is None:
        raw = _neighbors_scipy(cart, cell, cutoff)

    seen = set()
    for i, j, t in raw:
        if i == j and not any(t):
            continue
        key = _canonical(i, j, t)
        if key[0] == key[1] and _lex_negative(key[2]):
            continue
        seen.add(key)
    return sorted(seen)


@dataclass
class BondGraph:
    edge_u: np.ndarray        # (E,) int
    edge_v: np.ndarray        # (E,) int
    edge_shift: np.ndarray    # (E,3) int
    edge_dist: np.ndarray     # (E,) float

    def __len__(self):
        return len(self.edge_u)

    def edges(self):
        for k in range(len(self)):
            yield int(self.edge_u[k]), int(self.edge_v[k]), tuple(int(x) for x in self.edge_shift[k])


def build_bond_graph(structure: Structure, k_tol=1.20, d_min=D_MIN) -> BondGraph:
    """Stage 3: disorder-aware periodic covalent bond graph."""
    frac = structure.frac_coords
    cell = structure.cell
    z = structure.atomic_numbers
    grp = structure.disorder_groups
    n = len(z)
    rcov = np.array([cov_radius(int(zz)) for zz in z])

    def disorder_ok(u, v):
        gu, gv = int(grp[u]), int(grp[v])
        return gu == 0 or gv == 0 or gu == gv

    eu, ev, es, ed = [], [], [], []
    if structure.explicit_bonds:
        for (u, v) in structure.explicit_bonds:
            u, v = int(u), int(v)
            if not (0 <= u < n and 0 <= v < n) or u == v:
                continue
            if not disorder_ok(u, v):
                continue
            ell = mic_shift(frac[u], frac[v])
            d = float(np.linalg.norm(bond_vector(frac[u], frac[v], ell, cell)))
            if d < d_min:
                continue
            eu.append(u); ev.append(v); es.append(ell); ed.append(d)
    else:
        maxr = float(rcov.max()) if n else 1.5
        cutoff = k_tol * 2.0 * maxr + 1e-6
        cart = frac @ cell
        pairs = periodic_neighbors(z, cart, cell, cutoff)
        h_bonds = {}
        for (i, j, t) in pairs:
            if not disorder_ok(i, j):
                continue
            if z[i] == 1 and z[j] == 1:
                continue  # never bond H-H
            ell = np.array(t, dtype=int)
            d = float(np.linalg.norm(bond_vector(frac[i], frac[j], ell, cell)))
            if d < d_min or d > k_tol * (rcov[i] + rcov[j]):
                continue
            eu.append(i); ev.append(j); es.append(ell); ed.append(d)
        # Prevent over-coordinated hydrogen: keep only its shortest bond.
        eu, ev, es, ed = _trim_hydrogens(np.array(eu, int), np.array(ev, int),
                                         np.array(es, int).reshape(-1, 3),
                                         np.array(ed, float), z)

    eu = np.array(eu, dtype=int).reshape(-1)
    ev = np.array(ev, dtype=int).reshape(-1)
    es = np.array(es, dtype=int).reshape(-1, 3)
    ed = np.array(ed, dtype=float).reshape(-1)
    return BondGraph(eu, ev, es, ed)


def _trim_hydrogens(eu, ev, es, ed, z):
    if len(eu) == 0:
        return eu, ev, es, ed
    keep = np.ones(len(eu), dtype=bool)
    for atom in np.where(z == 1)[0]:
        idx = np.where((eu == atom) | (ev == atom))[0]
        if len(idx) > 1:
            best = idx[np.argmin(ed[idx])]
            for k in idx:
                if k != best:
                    keep[k] = False
    return eu[keep], ev[keep], es[keep], ed[keep]


@dataclass
class Component:
    nodes: list
    rank: int
    component_type: str
    axis: object                 # primitive integer chain axis p (rank 1) or None
    cycle_vectors: np.ndarray    # (K,3) non-zero cycle net lattice vectors
    sigma: dict                  # node -> cumulative fractional shift (Z^3)
    cycle_edges: list = field(default_factory=list)  # global edge ids closing cycles
    tree_edges: list = field(default_factory=list)


_RANK_TYPE = {0: "discrete_molecule", 1: "1d_polymer_chain",
              2: "2d_polymer_sheet", 3: "3d_network"}


def _primitive_axis(delta):
    delta = np.asarray(delta, dtype=int)
    g = reduce(gcd, [abs(int(x)) for x in delta]) or 1
    p = delta // g
    for x in p:
        if x != 0:
            if x < 0:
                p = -p
            break
    return p


def analyze_components(n_atoms, graph: BondGraph) -> list:
    """Stage 4: BFS spanning-tree homology rank classification per component."""
    adj = {i: [] for i in range(n_atoms)}
    mg = nx.MultiGraph()
    mg.add_nodes_from(range(n_atoms))
    for k, (u, v, t) in enumerate(graph.edges()):
        adj[u].append((v, np.array(t, int), k))
        adj[v].append((u, -np.array(t, int), k))
        mg.add_edge(u, v, key=k)

    components = []
    for nodes in nx.connected_components(mg):
        nodes = sorted(nodes)
        root = nodes[0]
        sigma = {root: np.zeros(3, dtype=int)}
        tree_edges, cycle_edges = [], []
        queue = [root]
        visited = {root}
        while queue:
            u = queue.pop(0)
            for (v, ell, k) in adj[u]:
                if v not in visited:
                    visited.add(v)
                    sigma[v] = sigma[u] + ell
                    tree_edges.append(k)
                    queue.append(v)
        deltas = []
        nodeset = set(nodes)
        for k, (u, v, t) in enumerate(graph.edges()):
            if u not in nodeset:
                continue
            delta = sigma[u] + np.array(t, int) - sigma[v]
            if np.any(delta != 0):
                deltas.append(delta)
                cycle_edges.append(k)
        if deltas:
            L = np.array(deltas, dtype=float)
            rank = int(np.linalg.matrix_rank(L))
            rank = min(rank, 3)
        else:
            L = np.zeros((0, 3))
            rank = 0
        axis = _primitive_axis(deltas[0]) if rank == 1 else None
        components.append(Component(
            nodes=nodes, rank=rank, component_type=_RANK_TYPE[rank],
            axis=axis, cycle_vectors=L, sigma=sigma,
            cycle_edges=cycle_edges, tree_edges=tree_edges))
    return components