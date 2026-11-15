# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase 2 acceptance tests for the polymer PBC engine (agent.md 6, Gate 2).

Test 1: PE orthorhombic  -- rank 1, chain axis p=[0,0,1], no rendered bond
        > 2.0 A in periodic_clipped, continuous 3c chain in chain_continuous.
Test 2: iPP monoclinic   -- triclinic MIC C-C bond lengths in [1.52, 1.56].
Test 3: PVDF disorder    -- disorder groups 1 and 2 never cross-bond.

Plus auxiliary coverage: doctor JSON, bundle schema, hard invariant, supercell
normalization, rank-0 molecules and the scipy neighbor-list fallback.
"""
import os
import sys
import json
import subprocess
import itertools

import numpy as np
import pytest
import networkx as nx

from conftest import fixture_path, load_cif, run_pipeline, rendered_bond_lengths

from polycrystal_core.structure import Structure
from polycrystal_core.symmetry import make_supercell, filter_disorder, cell_matrix
from polycrystal_core.pbc_topology import (
    build_bond_graph, analyze_components, periodic_neighbors, _neighbors_scipy)
from polycrystal_core.polymer_unwrap import realize, MAX_RENDER_BOND


# --------------------------------------------------------------------------
# Test 1 -- PE orthorhombic
# --------------------------------------------------------------------------
def test_pe_rank1_chain_axis():
    s, sc, graph, comps, geom = run_pipeline("pe_orthorhombic.cif")
    assert sc.n_atoms == 8                       # 1 C asymmetric unit x 8 ops
    assert len(comps) == 4                       # 4 zigzag chains
    assert all(c.rank == 1 for c in comps)
    assert all(c.component_type == "1d_polymer_chain" for c in comps)
    for c in comps:
        assert list(c.axis) == [0, 0, 1]


def test_pe_periodic_clipped_no_long_bonds():
    s, sc, graph, comps, geom = run_pipeline(
        "pe_orthorhombic.cif", mode="periodic_clipped")
    lengths = rendered_bond_lengths(geom)
    assert lengths, "expected rendered bonds"
    assert max(lengths) <= 2.0                   # acceptance: zero bonds > 2.0 A
    assert max(lengths) <= MAX_RENDER_BOND       # hard invariant
    # C-C covalent bonds are ~1.53 A; half stubs (if any) are shorter still.
    assert all(0.4 <= d <= 2.0 for d in lengths)


def test_pe_chain_continuous_3c():
    c = 2.534
    s, sc, graph, comps, geom = run_pipeline(
        "pe_orthorhombic.cif", supercell=(1, 1, 3), mode="chain_continuous")
    assert abs(sc.cell[2, 2] - 3 * c) < 1e-6     # supercell c = 3c
    chain = comps[0]
    assert chain.rank == 1 and list(chain.axis) == [0, 0, 1]
    nodes = chain.nodes
    assert len(nodes) == 6                       # 2 C/cell x 3 cells

    node_set = set(nodes)
    sub = [(int(u), int(v)) for u, v in geom.bonds_idx
           if int(u) in node_set and int(v) in node_set]
    assert len(sub) == len(nodes) - 1            # one unbroken path (cycle cut once)
    g = nx.Graph()
    g.add_nodes_from(nodes)
    g.add_edges_from(sub)
    assert nx.is_connected(g)
    # path: exactly two endpoints of degree 1, rest degree 2
    degs = sorted(d for _, d in g.degree())
    assert degs == [1, 1, 2, 2, 2, 2]

    pos = geom.positions[nodes]
    bond_len = np.linalg.norm(geom.positions[sub[0][0]] - geom.positions[sub[0][1]])
    assert 1.4 <= bond_len <= 1.7
    zs = np.sort(pos[:, 2])
    rise = np.diff(zs)
    assert np.allclose(rise, c / 2.0, atol=0.05)  # constant helical rise
    span = zs[-1] - zs[0]
    # closed chain length = span + one rise = 3c (the periodic repeat)
    assert abs(span + c / 2.0 - 3 * c) < 0.15
    assert list(chain.cycle_vectors[0]) == [0, 0, 1]


# --------------------------------------------------------------------------
# Test 2 -- iPP monoclinic (triclinic MIC)
# --------------------------------------------------------------------------
def test_ipp_monoclinic_mic_bond_lengths():
    s = load_cif("ipp_alpha_monoclinic.cif")
    a, b, c, al, be, ga = s.lattice_parameters
    assert abs(be - 99.3) < 0.2                  # monoclinic beta != 90
    assert abs(al - 90) < 1e-6 and abs(ga - 90) < 1e-6

    sc = make_supercell(s, 1, 1, 1)
    graph = build_bond_graph(sc)
    cc = [(u, v, t) for (u, v, t) in graph.edges()
          if sc.atomic_numbers[u] == 6 and sc.atomic_numbers[v] == 6]
    assert len(cc) >= 9                          # 6 backbone + 3 methyl C-C
    for (u, v, t) in cc:
        d = float(np.linalg.norm(
            (sc.frac_coords[v] + np.array(t) - sc.frac_coords[u]) @ sc.cell))
        assert 1.52 <= d <= 1.56, f"C-C bond {d:.4f} A outside [1.52,1.56]"

    # Cross-check MIC against a brute-force 27-image minimum for every bond.
    frac, cell = sc.frac_coords, sc.cell
    images = np.array(list(itertools.product((-1, 0, 1), repeat=3)))
    for (u, v, t) in graph.edges():
        d_mic = np.linalg.norm((frac[v] + np.array(t) - frac[u]) @ cell)
        cand = (frac[v] + images - frac[u]) @ cell
        d_brute = np.linalg.norm(cand, axis=1).min()
        assert abs(d_mic - d_brute) < 1e-6

    comps = analyze_components(sc.n_atoms, graph)
    assert any(cp.rank == 1 and list(cp.axis) == [0, 0, 1] for cp in comps)


# --------------------------------------------------------------------------
# Test 3 -- PVDF disorder
# --------------------------------------------------------------------------
def test_pvdf_disorder_no_cross_group_bonds():
    s = load_cif("pvdf_disordered.cif")
    assert set(int(g) for g in s.disorder_groups) == {0, 1, 2}
    assert (s.occupancies < 1.0).any()

    # Default filter keeps ordered backbone + group 1 only.
    mask = filter_disorder(s.disorder_groups, "default")
    kept = set(int(g) for g in s.disorder_groups[mask])
    assert kept == {0, 1}

    # With ALL groups present, groups 1 and 2 must never cross-bond.
    sc = make_supercell(s, 1, 1, 1)
    graph = build_bond_graph(sc)
    grp = sc.disorder_groups
    for (u, v, t) in graph.edges():
        gu, gv = int(grp[u]), int(grp[v])
        assert not (gu > 0 and gv > 0 and gu != gv), "cross-disorder-group bond!"

    # Both alternative F sites bond to the ordered C backbone (group 0).
    f_idx = np.where(sc.atomic_numbers == 9)[0]
    assert len(f_idx) == 2
    for f in f_idx:
        nbrs = [v for (u, v, t) in graph.edges() if u == f] + \
               [u for (u, v, t) in graph.edges() if v == f]
        assert any(sc.atomic_numbers[nb] == 6 and grp[nb] == 0 for nb in nbrs)


# --------------------------------------------------------------------------
# Auxiliary coverage
# --------------------------------------------------------------------------
def test_doctor_json_never_crashes():
    out = subprocess.run([sys.executable, "-m", "polycrystal_core.cli", "doctor"],
                         capture_output=True, text=True)
    assert out.returncode == 0
    data = json.loads(out.stdout)
    assert isinstance(data["ok"], bool)
    assert "numpy" in data["versions"]
    if not data["ok"]:
        assert "missing" in data


def test_supercell_normalization():
    s = load_cif("pe_orthorhombic.cif")
    sc = make_supercell(s, 2, 2, 2)
    assert sc.n_atoms == 8 * 8
    assert (sc.frac_coords >= 0).all() and (sc.frac_coords < 1).all()
    assert abs(sc.cell[0, 0] - 2 * 7.40) < 1e-6


def test_rank0_discrete_molecule():
    # Two carbons 1.54 A apart in a large box: a finite molecule, rank 0.
    cell = cell_matrix(20, 20, 20, 90, 90, 90)
    frac = np.array([[0.5, 0.5, 0.5], [0.5, 0.5, 0.5 + 1.54 / 20]])
    s = Structure(cell=cell, atomic_numbers=np.array([6, 6]), frac_coords=frac,
                  occupancies=np.ones(2), disorder_groups=np.zeros(2, int))
    graph = build_bond_graph(s)
    comps = analyze_components(s.n_atoms, graph)
    assert len(comps) == 1
    assert comps[0].rank == 0
    assert comps[0].component_type == "discrete_molecule"
    assert comps[0].axis is None


def test_hard_invariant_all_modes():
    for mode in ("pbc_cluster", "periodic_clipped", "chain_continuous"):
        _, _, _, _, geom = run_pipeline("pe_orthorhombic.cif", supercell=(1, 1, 2),
                                        mode=mode)
        lengths = rendered_bond_lengths(geom)
        assert lengths
        assert max(lengths) <= MAX_RENDER_BOND + 1e-6


# --------------------------------------------------------------------------
# PBC clustering (unwrap before export)
# --------------------------------------------------------------------------
def test_pbc_cluster_pe_unwrapped_chains():
    s, sc, graph, comps, geom = run_pipeline(
        "pe_orthorhombic.cif", supercell=(1, 1, 3), mode="pbc_cluster")
    assert geom.mode == "pbc_cluster" and geom.pbc_unwrapped is True
    # No padding artefacts at all.
    assert int(geom.is_ghost.sum()) == 0
    assert int(geom.is_boundary_pad.sum()) == 0
    assert len(geom.half_bond_z) == 0
    # One rendered atom per periodic atom: clusters are copies, not padding.
    assert len(geom.atomic_numbers) == sc.n_atoms == 24
    assert len(geom.bonds_idx) == 20             # 4 chains x 5 bonds
    assert len(geom.clusters) == 4

    for cluster in geom.clusters:
        assert cluster["n_atoms"] == 6
        assert cluster["n_bonds"] == 5
        assert cluster["cross_period"] is True
        assert cluster["n_cut_edges"] == 1       # the periodic ring closure
        assert cluster["degree_histogram"] == {"1": 2, "2": 4}
        assert cluster["n_isolated"] == 0
        # 6 carbons at a c/2 helical rise -> 5 rises of zigzag advance.
        assert abs(cluster["extent_angstrom"][2] - 5 * (2.534 / 2.0)) < 0.2
        assert cluster["max_unwrap_shift_angstrom"] > 0.0

    # Every chain is a connected path in the exported bond list.
    for comp in comps:
        nodes = set(int(u) for u in comp.nodes)
        sub = [(int(u), int(v)) for u, v in geom.bonds_idx
               if int(u) in nodes and int(v) in nodes]
        g = nx.Graph()
        g.add_nodes_from(nodes)
        g.add_edges_from(sub)
        assert nx.is_connected(g)
        assert len(sub) == len(nodes) - 1


def test_pbc_cluster_small_molecule_stays_whole():
    cell = cell_matrix(20, 20, 20, 90, 90, 90)
    # Three-carbon molecule straddling the c face (0.97 -> 0.03 -> 0.09).
    frac = np.array([[0.5, 0.5, 0.97], [0.5, 0.5, 0.03], [0.5, 0.5, 0.09]])
    s = Structure(cell=cell, atomic_numbers=np.array([6, 6, 6]), frac_coords=frac,
                  occupancies=np.ones(3), disorder_groups=np.zeros(3, int))
    graph = build_bond_graph(s)
    comps = analyze_components(s.n_atoms, graph)
    assert len(comps) == 1 and comps[0].rank == 0
    geom = realize(s, graph, comps, mode="pbc_cluster")
    assert len(geom.bonds_idx) == 2               # chain, not the 2.4 A pair
    d = np.linalg.norm(geom.positions[geom.bonds_idx[:, 0]]
                       - geom.positions[geom.bonds_idx[:, 1]], axis=1)
    assert np.all(d < 2.0)                        # both bonds stay short
    assert geom.clusters[0]["cross_period"] is True
    # The molecule is contiguous in z (2 x 1.2 A) instead of being split.
    z = np.sort(geom.positions[:, 2])
    assert z[-1] - z[0] < 2.6


def test_pbc_cluster_fallback_warns(tmp_path):
    # --pbc-cluster off keeps the legacy padded path and reports it.
    out = subprocess.run(
        [sys.executable, "-m", "polycrystal_core.cli", "build",
         "--input", fixture_path("pe_orthorhombic.cif"), "--supercell", "1,1,2",
         "--pbc-mode", "periodic_clipped", "--pbc-cluster", "off",
         "--out", str(tmp_path)],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    rep = json.loads(out.stdout)["report"]
    assert rep["pbc_cluster"]["enabled"] is False
    assert rep["geometry"]["pbc_unwrapped"] is False
    assert rep["geometry"]["n_boundary_pad_atoms"] > 0   # legacy path still pads


def test_cli_pbc_cluster_is_default(tmp_path):
    out = subprocess.run(
        [sys.executable, "-m", "polycrystal_core.cli", "build",
         "--input", fixture_path("pe_orthorhombic.cif"),
         "--supercell", "auto", "--out", str(tmp_path)],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    rep = data["report"]
    assert rep["geometry"]["mode"] == "pbc_cluster"
    assert rep["geometry"]["pbc_unwrapped"] is True
    assert rep["geometry"]["n_half_bonds"] == 0
    assert rep["geometry"]["n_ghost_atoms"] == 0
    assert rep["geometry"]["n_boundary_pad_atoms"] == 0
    assert rep["pbc_cluster"]["status"] == "unwrapped"
    assert rep["pbc_cluster"]["n_clusters"] == len(rep["pbc_cluster"]["clusters"])
    with np.load(data["bundle"]) as bundle:
        assert not bundle["is_ghost"].any()
        assert not bundle["is_boundary_pad"].any()
        assert bundle["half_bond_z"].shape == (0,)


def test_neighbor_scipy_fallback_consistency():
    s = load_cif("pe_orthorhombic.cif")
    sc = make_supercell(s, 1, 1, 1)
    cart, cell, z = sc.cart_coords, sc.cell, sc.atomic_numbers
    cutoff = 1.2 * 2 * 0.76 + 1e-6
    ref = set(periodic_neighbors(z, cart, cell, cutoff))
    raw = _neighbors_scipy(cart, cell, cutoff)
    from polycrystal_core.pbc_topology import _canonical, _lex_negative
    got = set()
    for i, j, t in raw:
        if i == j and not any(t):
            continue
        key = _canonical(i, j, t)
        if key[0] == key[1] and _lex_negative(key[2]):
            continue
        got.add(key)
    assert ref == got


def test_bundle_schema_and_manifest(tmp_path):
    from polycrystal_core.backbone_spline import extract_backbones
    from polycrystal_core.bundle_writer import write_bundle
    s, sc, graph, comps, geom = run_pipeline("pe_orthorhombic.cif")
    pts, offs, is_bb = extract_backbones(sc, graph, comps, geom)
    geom.is_backbone = is_bb
    bundle, manifest_path, manifest = write_bundle(
        str(tmp_path), geom, pts, offs, {"formula": s.formula})
    assert os.path.isfile(bundle) and os.path.isfile(manifest_path)

    expected = {
        "positions": (np.float32, 2), "frac_positions": (np.float32, 2),
        "atomic_numbers": (np.int16, 1), "vdw_radii": (np.float32, 1),
        "cov_radii": (np.float32, 1), "chain_ids": (np.int32, 1),
        "component_ranks": (np.int8, 1), "disorder_groups": (np.int16, 1),
        "occupancies": (np.float32, 1), "is_backbone": (np.bool_, 1),
        "is_ghost": (np.bool_, 1), "is_boundary_pad": (np.bool_, 1),
        "bonds_idx": (np.int32, 2), "half_bond_starts": (np.float32, 2),
        "half_bond_ends": (np.float32, 2), "half_bond_z": (np.int16, 1),
        "cell_vertices": (np.float32, 2), "cell_edges": (np.int32, 2),
        "backbone_points": (np.float32, 2), "backbone_curve_offsets": (np.int32, 1),
    }
    with np.load(bundle) as data:
        assert set(data.files) == set(expected)
        for key, (dtype, ndim) in expected.items():
            assert data[key].dtype == dtype, key
            assert data[key].ndim == ndim, key
        assert data["cell_vertices"].shape == (8, 3)
        assert data["cell_edges"].shape == (12, 2)
        assert len(data["positions"]) == len(data["atomic_numbers"])

    assert len(manifest["sha256"]) == 64
    assert manifest["counts"]["atoms"] == len(geom.atomic_numbers)


def test_cli_build_smoke(tmp_path):
    out = subprocess.run(
        [sys.executable, "-m", "polycrystal_core.cli", "build",
         "--input", fixture_path("pe_orthorhombic.cif"),
         "--supercell", "1,1,2", "--pbc-mode", "periodic_clipped",
         "--boundary-pad", "0.05,0.05,0.05", "--disorder-filter", "default",
         "--out", str(tmp_path)],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["ok"] is True
    assert os.path.isfile(data["bundle"])
    assert os.path.isfile(data["manifest"])
    rep = data["report"]
    assert rep["geometry"]["invariant_ok"] is True
    assert rep["geometry"]["max_render_bond_angstrom"] <= MAX_RENDER_BOND
    assert rep["topology"]["n_chains"] >= 1
    assert rep["crystal"]["space_group"]["symbol"] == "Pnam"
    assert rep["crystal"]["space_group"]["number"] == 62


def test_cli_build_failure_exit_code(tmp_path):
    out = subprocess.run(
        [sys.executable, "-m", "polycrystal_core.cli", "build",
         "--input", str(tmp_path / "does_not_exist.cif"), "--out", str(tmp_path)],
        capture_output=True, text=True)
    assert out.returncode == 1
    data = json.loads(out.stdout)
    assert data["ok"] is False
    assert "error" in data


def test_hkl_lamella_invariant():
    s, sc, graph, comps, geom = run_pipeline(
        "ipp_alpha_monoclinic.cif", supercell=(1, 1, 2), mode="periodic_clipped")
    # Re-realize as a (001) lamella via the hkl path.
    geom = realize(sc, graph, comps, mode="hkl_lamella", hkl=(0, 0, 1),
                   slab_thickness=8.0)
    assert geom.mode == "hkl_lamella"
    lengths = rendered_bond_lengths(geom)
    assert lengths
    assert max(lengths) <= MAX_RENDER_BOND + 1e-6


def test_ipp_backbone_helix_extraction():
    from polycrystal_core.backbone_spline import extract_backbones
    s, sc, graph, comps, geom = run_pipeline(
        "ipp_alpha_monoclinic.cif", supercell=(1, 1, 1), mode="chain_continuous")
    pts, offs, is_bb = extract_backbones(sc, graph, comps, geom)
    assert len(offs) - 1 >= 1                    # at least one backbone curve
    assert len(pts) >= 6                         # 6 backbone C per helix repeat
    # Methyl carbons are pruned: backbone marks only the 6 helix carbons.
    assert int(is_bb.sum()) == 6
    seg = np.linalg.norm(np.diff(pts[:6], axis=0), axis=1)
    assert np.all((seg >= 1.52) & (seg <= 1.56))


def test_lammps_explicit_bonds(tmp_path):
    from polycrystal_core.io_parsers import parse_lammps_data
    # Finite 3-carbon chain in a 20 A box, atom_style molecular + bond table.
    data = """LAMMPS test chain

3 atoms
2 bonds
1 atom types
1 bond types

0.0 20.0 xlo xhi
0.0 20.0 ylo yhi
0.0 20.0 zlo zhi

Masses

1 12.011

Atoms

1 1 1 10.0 10.0 10.0
2 1 1 10.0 10.0 11.54
3 1 1 10.0 10.0 13.08

Bonds

1 1 1 2
2 1 2 3
"""
    p = tmp_path / "chain.data"
    p.write_text(data)
    s = parse_lammps_data(str(p))
    assert s.n_atoms == 3
    assert s.explicit_bonds and len(s.explicit_bonds) == 2
    graph = build_bond_graph(s)
    assert len(graph) == 2
    comps = analyze_components(s.n_atoms, graph)
    assert len(comps) == 1 and comps[0].rank == 0   # finite oligomer
    for (u, v, t) in graph.edges():
        d = np.linalg.norm((s.frac_coords[v] + np.array(t) - s.frac_coords[u]) @ s.cell)
        assert 1.5 <= d <= 1.6