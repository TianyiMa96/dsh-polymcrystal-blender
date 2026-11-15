# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 5: realize periodic bond graphs into renderable geometry.

Four selectable modes (agent.md 3.2):
  * pbc_cluster       -- PBC clustering: every connected component is unwrapped
                         into one coherent molecule/chain (nearest periodic
                         image per bond).  No padding, ghosts or half bonds.
  * periodic_clipped  -- boundary padding + half-bond stubs + ghost atoms.
  * chain_continuous  -- cut periodic cycle edges, unwrap continuous chains.
  * hkl_lamella       -- cleave a Miller slab then clip.

Hard invariant: every rendered bond (internal or half-stub) is <= 3.5 Angstrom.
"""
from __future__ import annotations

import itertools
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from .structure import Structure
from .pbc_topology import BondGraph, Component, build_bond_graph, analyze_components

MAX_RENDER_BOND = 3.5  # Angstrom, hard invariant


class UnwrapError(RuntimeError):
    """The periodic graph could not be unwrapped into a coherent cluster."""


@dataclass
class RealizedGeometry:
    cell: np.ndarray
    positions: np.ndarray
    frac_positions: np.ndarray
    atomic_numbers: np.ndarray
    occupancies: np.ndarray
    disorder_groups: np.ndarray
    chain_ids: np.ndarray
    component_ranks: np.ndarray
    is_ghost: np.ndarray
    is_boundary_pad: np.ndarray
    base_index: np.ndarray
    bonds_idx: np.ndarray
    half_bond_starts: np.ndarray
    half_bond_ends: np.ndarray
    half_bond_z: np.ndarray
    is_backbone: np.ndarray = None
    cut_edges: list = field(default_factory=list)
    mode: str = "periodic_clipped"
    clusters: list = field(default_factory=list)
    pbc_unwrapped: bool = False

    def __post_init__(self):
        if self.is_backbone is None:
            self.is_backbone = np.zeros(len(self.atomic_numbers), dtype=bool)


def _assert_render_invariant(positions, bonds_idx, half_starts, half_ends,
                             limit=MAX_RENDER_BOND):
    if len(bonds_idx):
        d = np.linalg.norm(positions[bonds_idx[:, 0]] - positions[bonds_idx[:, 1]], axis=1)
        if float(d.max()) > limit + 1e-6:
            raise RuntimeError(
                f"PBC hard invariant violated: internal bond {d.max():.3f} A > {limit} A")
    if len(half_starts):
        d = np.linalg.norm(half_ends - half_starts, axis=1)
        if float(d.max()) > limit + 1e-6:
            raise RuntimeError(
                f"PBC hard invariant violated: half-bond {d.max():.3f} A > {limit} A")


def _component_arrays(n, components):
    chain_ids = np.full(n, -1, dtype=np.int32)
    ranks = np.zeros(n, dtype=np.int8)
    for cid, comp in enumerate(components):
        for u in comp.nodes:
            chain_ids[u] = cid
            ranks[u] = comp.rank
    return chain_ids, ranks


def _unwrap_by_sigma(frac, components):
    """Lift every atom onto the BFS spanning tree of its component.

    ``Component.sigma`` is the accumulated minimum-image lattice shift from the
    component root, so ``frac[u] + sigma[u]`` puts each atom on the same
    continuous branch as the seed.  For a rank-1 chain this walks the strand
    out of the cell; for a rank-0 molecule it restores the whole molecule.
    """
    unwrapped = frac.copy()
    for comp in components:
        for u in comp.nodes:
            shift = comp.sigma.get(u)
            if shift is not None:
                unwrapped[u] = frac[u] + np.asarray(shift, dtype=int)
    return unwrapped


def _cluster_report(cid, comp, nodes, shift_vec, cell, geom_positions,
                    kept_edges, n_cut):
    """Per-cluster diagnostics: size, cross-period flag, unwrap offset, degrees."""
    idx = np.asarray(nodes, dtype=int)
    degree = {int(u): 0 for u in nodes}
    for (a, b) in kept_edges:
        if a in degree and b in degree:
            degree[a] += 1
            degree[b] += 1
    hist = {}
    for d in degree.values():
        hist[str(int(d))] = hist.get(str(int(d)), 0) + 1
    endpoints = sorted(
        ((int(u), int(d)) for u, d in degree.items() if d <= 1),
        key=lambda item: item[0])
    extent = geom_positions[idx].max(axis=0) - geom_positions[idx].min(axis=0)
    moved = np.linalg.norm(shift_vec[idx] @ cell, axis=1)

    # Where the unwrapped strand actually stops: chain termini (degree <= 1
    # leaves at the two extremes of the chain axis) expressed as a fractional
    # coordinate of the cell, so "inside / outside the box" is testable.
    termini = []
    if comp.axis is not None and len(idx):
        axis = np.asarray(comp.axis, dtype=float)
        m = int(np.argmax(np.abs(axis)))
        along = geom_positions[idx, m] / float(np.linalg.norm(cell[m]))
        leaves = [u for u in nodes if degree.get(int(u), 0) <= 1]
        if leaves:
            pos_of = {int(u): i for i, u in enumerate(idx)}
            for u in (min(leaves, key=lambda a: along[pos_of[a]]),
                      max(leaves, key=lambda a: along[pos_of[a]])):
                f = float(along[pos_of[u]])
                termini.append([int(u), round(f, 4), bool(0.0 <= f <= 1.0)])
    return {
        "id": int(cid),
        "type": comp.component_type,
        "rank": int(comp.rank),
        "axis": [int(x) for x in comp.axis] if comp.axis is not None else None,
        "n_atoms": int(len(nodes)),
        "n_bonds": int(len(kept_edges)),
        "n_cut_edges": int(n_cut),
        "cross_period": bool(comp.rank >= 1 or np.any(shift_vec[idx] != 0)),
        "max_unwrap_shift_angstrom": round(float(moved.max()) if len(idx) else 0.0, 4),
        "extent_angstrom": [round(float(x), 3) for x in extent],
        "degree_histogram": dict(sorted(hist.items())),
        "n_endpoints": int(sum(1 for d in degree.values() if d == 1)),
        "n_isolated": int(sum(1 for d in degree.values() if d == 0)),
        "endpoint_atoms": [[u, d] for u, d in endpoints[:32]],
        "chain_termini": termini,
    }


def realize_pbc_cluster(structure, graph, components, boundary_pad=None):
    """pbc_cluster: unwrap each periodic component into one coherent entity.

    Algorithm (all steps purely geometric, no rendering padding):

    1. Bond shifts come from the periodic neighbour search, i.e. every edge
       already carries the *minimum image* lattice translation (``edge_shift``).
    2. ``analyze_components`` walks a BFS spanning tree per connected component
       and accumulates those shifts (``Component.sigma``); adding sigma to the
       fractional coordinate lifts each atom onto the same continuous branch as
       the seed atom, always choosing the mirror nearest to its parent.
    3. The lifted complex is checked edge by edge: any edge that is still longer
       than the render invariant can only be a *periodic closure* edge (the bond
       that closes the ring through the cell), so it is dropped and reported -
       never replaced by a half-bond stub or a ghost copy.
    4. Components are then placed with rigid lattice translations: molecules
       (rank 0) are centred in the cell as a whole, chains/sheets/networks keep
       their lateral registry (integer shift only) and a pure-chain model is
       additionally centred along the chain axis so the strand hangs
       symmetrically out of the cell.

    ``is_ghost``, ``is_boundary_pad`` and the half-bond arrays are all empty by
    construction: the output consists only of real atoms and real bonds.
    """
    frac = structure.frac_coords
    cell = structure.cell
    z = structure.atomic_numbers
    n = len(z)
    if n == 0:
        raise UnwrapError("empty structure")
    chain_ids, ranks = _component_arrays(n, components)

    unwrapped = _unwrap_by_sigma(frac, components)
    shift_vec = np.zeros((n, 3), dtype=int)
    for comp in components:
        for u in comp.nodes:
            shift_vec[u] = comp.sigma.get(u, np.zeros(3, int))

    eu = np.asarray(graph.edge_u, dtype=int)
    ev = np.asarray(graph.edge_v, dtype=int)
    if len(eu):
        dist = np.linalg.norm((unwrapped[ev] - unwrapped[eu]) @ cell, axis=1)
    else:
        dist = np.zeros(0, dtype=float)
    keep = dist <= MAX_RENDER_BOND + 1e-6

    tree_edges = {int(k) for comp in components for k in comp.tree_edges}
    bad_tree = [k for k in sorted(tree_edges)
                if k < len(keep) and not bool(keep[k])]
    if bad_tree:
        raise UnwrapError(
            "spanning-tree bond {} is {:.2f} A after unwrapping; the periodic "
            "graph is not self-consistent".format(
                bad_tree[0], float(dist[bad_tree[0]])))

    # --- rigid placement --------------------------------------------------- #
    # The dominant chain axis decides which direction keeps its registry: the
    # model is centred along it at the end so a 1D strand hangs symmetrically
    # out of the cell instead of poking out of one face only.
    axes = [tuple(int(x) for x in comp.axis) for comp in components
            if comp.axis is not None and comp.rank == 1]
    along_axis = None
    if axes:
        dominant = Counter(axes).most_common(1)[0][0]
        along_axis = int(np.argmax(np.abs(np.asarray(dominant, dtype=float))))

    for comp in components:
        nodes = np.asarray(comp.nodes, dtype=int)
        if len(nodes) == 0:
            continue
        if comp.rank == 0:
            centre = 0.5 * (unwrapped[nodes].min(axis=0) + unwrapped[nodes].max(axis=0))
            unwrapped[nodes] += np.rint(0.5 - centre).astype(int)
        else:
            com = unwrapped[nodes].mean(axis=0)
            shift = np.zeros(3)
            for m in range(3):
                if m == along_axis:
                    continue
                shift[m] = np.floor(0.5 - com[m] + 0.5)  # integer lattice move
            unwrapped[nodes] += shift
    # Only a pure-chain model is centred along the axis: moving the whole model
    # past a lattice period would push co-existing discrete molecules out of the
    # cell, and their position relative to the chains is not arbitrary.
    if along_axis is not None and not any(comp.rank == 0 for comp in components):
        span = unwrapped[:, along_axis]
        unwrapped[:, along_axis] += 0.5 - 0.5 * (span.min() + span.max())

    positions = unwrapped @ cell

    # --- per-cluster report ------------------------------------------------ #
    clusters = []
    cursor_edges = np.nonzero(keep)[0]
    kept_pairs = [(int(u), int(v)) for u, v in zip(eu[cursor_edges], ev[cursor_edges])]
    for cid, comp in enumerate(components):
        nodes = [int(u) for u in comp.nodes]
        nodeset = set(nodes)
        kept = [(u, v) for (u, v) in kept_pairs if u in nodeset and v in nodeset]
        n_cut = sum(1 for k in range(len(eu))
                    if not keep[k] and (int(eu[k]) in nodeset or int(ev[k]) in nodeset))
        clusters.append(_cluster_report(
            cid, comp, nodes, shift_vec, cell, positions, kept, n_cut))

    bonds_idx = np.asarray(kept_pairs, dtype=int).reshape(-1, 2)
    half_starts = np.zeros((0, 3))
    half_ends = np.zeros((0, 3))
    half_z = np.zeros(0, int)
    _assert_render_invariant(positions, bonds_idx, half_starts, half_ends)

    return RealizedGeometry(
        cell=cell, positions=positions, frac_positions=unwrapped,
        atomic_numbers=z.copy(), occupancies=structure.occupancies.copy(),
        disorder_groups=structure.disorder_groups.copy(),
        chain_ids=chain_ids, component_ranks=ranks,
        is_ghost=np.zeros(n, bool), is_boundary_pad=np.zeros(n, bool),
        base_index=np.arange(n), bonds_idx=bonds_idx,
        half_bond_starts=half_starts, half_bond_ends=half_ends,
        half_bond_z=half_z, cut_edges=[int(k) for k in np.nonzero(~keep)[0]],
        mode="pbc_cluster", clusters=clusters, pbc_unwrapped=True)


def realize_clipped(structure, graph, components, boundary_pad=(0.05, 0.05, 0.05)):
    """periodic_clipped: pad images, internal bonds, half-bond stubs + ghosts."""
    frac = structure.frac_coords
    cell = structure.cell
    z = structure.atomic_numbers
    n = len(z)
    pad = np.asarray(boundary_pad, dtype=float).reshape(3)
    chain_ids, ranks = _component_arrays(n, components)

    pos_list = list(frac @ cell)
    frac_list = [frac[i].copy() for i in range(n)]
    base_list = list(range(n))
    ghost_list = [False] * n
    pad_list = [False] * n
    map_pad = {(i, (0, 0, 0)): i for i in range(n)}

    # Boundary padding images (atoms within pad of a face).
    for i in range(n):
        opts = []
        for m in range(3):
            o = [0]
            if frac[i, m] < pad[m]:
                o.append(1)
            if frac[i, m] > 1.0 - pad[m]:
                o.append(-1)
            opts.append(o)
        for sh in itertools.product(*opts):
            if sh == (0, 0, 0):
                continue
            sf = frac[i] + np.array(sh)
            idx = len(pos_list)
            pos_list.append(sf @ cell)
            frac_list.append(sf)
            base_list.append(i)
            ghost_list.append(False)
            pad_list.append(True)
            map_pad[(i, sh)] = idx

    bonds = []
    half_starts, half_ends, half_z = [], [], []
    map_ghost = {}
    for (u, v, ell) in graph.edges():
        ell_t = tuple(ell)
        neg = tuple(-x for x in ell_t)
        if (v, ell_t) in map_pad:
            bonds.append((u, map_pad[(v, ell_t)]))
        elif (u, neg) in map_pad:
            bonds.append((map_pad[(u, neg)], v))
        else:
            r_u = pos_list[u]
            r_v = (frac[v] + np.array(ell)) @ cell
            half_starts.append(r_u)
            half_ends.append(0.5 * (r_u + r_v))
            half_z.append(int(z[v]))
            if (v, ell_t) not in map_ghost:
                sf = frac[v] + np.array(ell)
                gidx = len(pos_list)
                pos_list.append(sf @ cell)
                frac_list.append(sf)
                base_list.append(v)
                ghost_list.append(True)
                pad_list.append(False)
                map_ghost[(v, ell_t)] = gidx

    positions = np.array(pos_list, dtype=float).reshape(-1, 3)
    frac_positions = np.array(frac_list, dtype=float).reshape(-1, 3)
    base_index = np.array(base_list, dtype=int)
    is_ghost = np.array(ghost_list, dtype=bool)
    is_pad = np.array(pad_list, dtype=bool)

    bonds_idx = np.array(bonds, dtype=int).reshape(-1, 2)
    half_starts = np.array(half_starts, float).reshape(-1, 3)
    half_ends = np.array(half_ends, float).reshape(-1, 3)
    half_z = np.array(half_z, int).reshape(-1)

    _assert_render_invariant(positions, bonds_idx, half_starts, half_ends)

    all_z = z[base_index]
    all_occ = structure.occupancies[base_index]
    all_grp = structure.disorder_groups[base_index]
    all_chain = chain_ids[base_index]
    all_rank = ranks[base_index]
    return RealizedGeometry(
        cell=cell, positions=positions, frac_positions=frac_positions,
        atomic_numbers=all_z, occupancies=all_occ, disorder_groups=all_grp,
        chain_ids=all_chain, component_ranks=all_rank,
        is_ghost=is_ghost, is_boundary_pad=is_pad, base_index=base_index,
        bonds_idx=bonds_idx, half_bond_starts=half_starts,
        half_bond_ends=half_ends, half_bond_z=half_z, mode="periodic_clipped")


def realize_chain_continuous(structure, graph, components, chain_repeat_count=None):
    """chain_continuous: cut rank-1 cycle edges, unwrap continuous strands."""
    frac = structure.frac_coords
    cell = structure.cell
    z = structure.atomic_numbers
    n = len(z)
    chain_ids, ranks = _component_arrays(n, components)

    cut = set()
    for comp in components:
        if comp.rank == 1 and comp.cycle_edges:
            # Order cuts by proximity to a reference plane orthogonal to p.
            p = np.asarray(comp.axis, float)
            pnorm = p / (np.linalg.norm(p) or 1.0)
            cart = frac @ cell
            def key(k):
                u, v, t = graph.edge_u[k], graph.edge_v[k], graph.edge_shift[k]
                mid = 0.5 * (cart[u] + (frac[v] + t) @ cell)
                return abs(float(mid @ pnorm))
            for k in sorted(comp.cycle_edges, key=key):
                cut.add(int(k))

    sigma = {}
    for comp in components:
        for u, s in comp.sigma.items():
            sigma[u] = np.array(s, int)

    unwrapped_frac = frac.copy()
    for u in range(n):
        unwrapped_frac[u] = frac[u] + sigma.get(u, np.zeros(3, int))

    # Recentre: rank-0 whole molecule into [0,1)^3; rank-1 lateral centroid only.
    for comp in components:
        nodes = comp.nodes
        if not nodes:
            continue
        com = unwrapped_frac[nodes].mean(axis=0)
        shift = np.zeros(3)
        if comp.rank == 1 and comp.axis is not None:
            p = np.asarray(comp.axis)
            along = int(np.argmax(np.abs(p)))
            for m in range(3):
                if m != along:
                    shift[m] = -np.floor(com[m])
        else:
            shift = -np.floor(com)
        unwrapped_frac[nodes] += shift

    positions = unwrapped_frac @ cell
    bonds = [(int(graph.edge_u[k]), int(graph.edge_v[k]))
             for k in range(len(graph)) if k not in cut]
    bonds_idx = np.array(bonds, dtype=int).reshape(-1, 2)
    half_starts = np.zeros((0, 3))
    half_ends = np.zeros((0, 3))
    half_z = np.zeros(0, int)
    _assert_render_invariant(positions, bonds_idx, half_starts, half_ends)

    return RealizedGeometry(
        cell=cell, positions=positions, frac_positions=unwrapped_frac,
        atomic_numbers=z.copy(), occupancies=structure.occupancies.copy(),
        disorder_groups=structure.disorder_groups.copy(),
        chain_ids=chain_ids, component_ranks=ranks,
        is_ghost=np.zeros(n, bool), is_boundary_pad=np.zeros(n, bool),
        base_index=np.arange(n), bonds_idx=bonds_idx,
        half_bond_starts=half_starts, half_bond_ends=half_ends,
        half_bond_z=half_z, cut_edges=sorted(cut), mode="chain_continuous")


def realize(structure, graph, components, mode="pbc_cluster",
            boundary_pad=(0.05, 0.05, 0.05), chain_repeat_count=None,
            hkl=(0, 0, 1), slab_thickness=15.0):
    """Dispatch to the requested PBC realization mode."""
    mode = (mode or "pbc_cluster").strip().lower()
    if mode == "pbc_cluster":
        return realize_pbc_cluster(structure, graph, components, boundary_pad)
    if mode == "periodic_clipped":
        return realize_clipped(structure, graph, components, boundary_pad)
    if mode == "chain_continuous":
        return realize_chain_continuous(structure, graph, components, chain_repeat_count)
    if mode == "hkl_lamella":
        from .hkl_slab import generate_slab
        slab = generate_slab(structure, hkl, slab_thickness)
        g2 = build_bond_graph(slab)
        c2 = analyze_components(slab.n_atoms, g2)
        geom = realize_clipped(slab, g2, c2, boundary_pad)
        geom.mode = "hkl_lamella"
        return geom
    raise ValueError(f"Unknown pbc_mode: {mode!r}")