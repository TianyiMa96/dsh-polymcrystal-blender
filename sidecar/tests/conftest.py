# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared pytest helpers for the sidecar test suite."""
import os
import sys

import pytest

# Allow running the suite without an editable install.
_SIDECAR_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SIDECAR_ROOT not in sys.path:
    sys.path.insert(0, _SIDECAR_ROOT)

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture_path(name):
    return os.path.join(FIXTURE_DIR, name)


def load_cif(name):
    """Parse a fixture CIF, skipping (with annotation) if no backend exists."""
    from polycrystal_core.io_parsers import parse_cif
    try:
        return parse_cif(fixture_path(name))
    except ImportError as e:
        pytest.skip(f"CIF backend unavailable for {name}: {e}")


def run_pipeline(name, supercell=(1, 1, 1), mode="periodic_clipped",
                 boundary_pad=(0.05, 0.05, 0.05), disorder_filter="default",
                 k_tol=1.20):
    from polycrystal_core.symmetry import filter_disorder, make_supercell
    from polycrystal_core.pbc_topology import build_bond_graph, analyze_components
    from polycrystal_core.polymer_unwrap import realize
    from polycrystal_core.cli import _apply_mask

    s = load_cif(name)
    mask = filter_disorder(s.disorder_groups, disorder_filter)
    s = _apply_mask(s, mask)
    sc = make_supercell(s, *supercell)
    graph = build_bond_graph(sc, k_tol=k_tol)
    comps = analyze_components(sc.n_atoms, graph)
    geom = realize(sc, graph, comps, mode=mode, boundary_pad=boundary_pad)
    return s, sc, graph, comps, geom


def rendered_bond_lengths(geom):
    import numpy as np
    lengths = []
    if len(geom.bonds_idx):
        d = np.linalg.norm(geom.positions[geom.bonds_idx[:, 0]]
                           - geom.positions[geom.bonds_idx[:, 1]], axis=1)
        lengths.extend(d.tolist())
    if len(geom.half_bond_starts):
        d = np.linalg.norm(geom.half_bond_ends - geom.half_bond_starts, axis=1)
        lengths.extend(d.tolist())
    return lengths