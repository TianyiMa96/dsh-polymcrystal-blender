# SPDX-License-Identifier: GPL-3.0-or-later
"""Command-line entry point invoked by the DSH host plugin.

  python -m polycrystal_core.cli doctor
  python -m polycrystal_core.cli build --input F --supercell Na,Nb,Nc \
      --pbc-mode M --boundary-pad da,db,dc --disorder-filter D --out DIR

Both commands emit a single JSON object on stdout. ``doctor`` never crashes on
missing packages; ``build`` returns exit code 1 with {"ok": false, "error": ...}
on any failure.
"""
from __future__ import annotations

import os
import re
import sys
import json
import argparse
import importlib
from collections import Counter

import numpy as np

REQUIRED_PACKAGES = ["numpy", "scipy", "networkx"]
OPTIONAL_PACKAGES = ["gemmi", "pymatgen", "ase", "spglib", "vesin"]


def _package_version(name):
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        pass
    try:
        mod = importlib.import_module(name)
        return getattr(mod, "__version__", "unknown")
    except Exception:
        return None


def cmd_doctor(_args=None):
    versions, missing, optional_missing = {}, [], []
    for name in REQUIRED_PACKAGES + OPTIONAL_PACKAGES:
        v = _package_version(name)
        if v is None:
            (missing if name in REQUIRED_PACKAGES else optional_missing).append(name)
        else:
            versions[name] = v
    result = {"ok": not missing, "versions": versions}
    if missing:
        result["missing"] = missing
    if optional_missing:
        result["optional_missing"] = optional_missing
    print(json.dumps(result))
    return 0


def _parse_triplet(s, cast, default):
    if not s:
        return default
    parts = [p for p in re.split(r"[,\s]+", str(s).strip()) if p]
    if len(parts) != 3:
        raise ValueError(f"Expected 3 comma-separated values, got {s!r}")
    return tuple(cast(p) for p in parts)


def _apply_mask(structure, mask):
    from .structure import Structure
    idx = np.where(mask)[0]
    remap = {int(old): new for new, old in enumerate(idx)}
    bonds = None
    if structure.explicit_bonds:
        bonds = [(remap[int(u)], remap[int(v)]) for (u, v) in structure.explicit_bonds
                 if int(u) in remap and int(v) in remap]
    return Structure(
        cell=structure.cell.copy(),
        atomic_numbers=structure.atomic_numbers[idx],
        frac_coords=structure.frac_coords[idx],
        occupancies=structure.occupancies[idx],
        disorder_groups=structure.disorder_groups[idx],
        spacegroup_symbol=structure.spacegroup_symbol,
        spacegroup_number=structure.spacegroup_number,
        hall_number=structure.hall_number,
        explicit_bonds=bonds, source=structure.source,
        labels=[structure.labels[i] for i in idx] if structure.labels else [])


def _max_render_bond(geom):
    dmax = 0.0
    if len(geom.bonds_idx):
        d = np.linalg.norm(geom.positions[geom.bonds_idx[:, 0]]
                           - geom.positions[geom.bonds_idx[:, 1]], axis=1)
        dmax = max(dmax, float(d.max()))
    if len(geom.half_bond_starts):
        d = np.linalg.norm(geom.half_bond_ends - geom.half_bond_starts, axis=1)
        dmax = max(dmax, float(d.max()))
    return dmax


def _chain_records(components):
    records = []
    for cid, comp in enumerate(components):
        records.append({
            "id": cid,
            "type": comp.component_type,
            "rank": int(comp.rank),
            "n_atoms": len(comp.nodes),
            "axis": [int(x) for x in comp.axis] if comp.axis is not None else None,
            "n_cycle_vectors": int(len(comp.cycle_vectors)),
        })
    return records


def _disorder_summary(structure, disorder_filter):
    counts = Counter(int(g) for g in structure.disorder_groups)
    return {
        "filter": disorder_filter,
        "groups_present": sorted(counts),
        "counts": {str(k): int(v) for k, v in sorted(counts.items())},
    }


#: How many repeat periods of the polymer chain the auto supercell shows.
AUTO_SUPERCELL_REPEATS = 2


def _auto_supercell(structure, k_tol, repeats=AUTO_SUPERCELL_REPEATS):
    """Expand along the detected polymer chain axis so >= ``repeats`` periods fit.

    A 1x1x1 cell of a 1D polymer clips every chain at the cell boundary, which
    reads as a broken fragment in the render.  Detect the chain axis on the
    unit cell (cheap: the graph is built again on the real supercell anyway)
    and stretch that lattice direction.
    """
    from .pbc_topology import build_bond_graph, analyze_components

    graph = build_bond_graph(structure, k_tol=k_tol)
    components = analyze_components(structure.n_atoms, graph)
    axes = []
    for comp in components:
        axis = getattr(comp, "axis", None)
        if getattr(comp, "rank", 0) == 1 and axis is not None:
            axes.append(tuple(int(x) for x in axis))

    info = {
        "enabled": True,
        "requested": "auto",
        "repeats": int(max(1, repeats)),
        "n_chain_components": len(axes),
    }
    if not axes:
        info.update(status="no_polymer_chain_detected", chain_axis=None)
        return (1, 1, 1), info

    chain_axis = Counter(axes).most_common(1)[0][0]
    direction = int(np.argmax(np.abs(np.asarray(chain_axis, dtype=float))))
    multiplier = [1, 1, 1]
    multiplier[direction] = int(max(1, repeats))
    info.update(
        status="expanded_along_chain_axis",
        chain_axis=[int(x) for x in chain_axis],
        lattice_direction="abc"[direction],
    )
    return tuple(multiplier), info


def cmd_build(args):
    from .io_parsers import parse_structure
    from .symmetry import filter_disorder, make_supercell
    from .pbc_topology import build_bond_graph, analyze_components
    from .polymer_unwrap import realize, MAX_RENDER_BOND, UnwrapError
    from .backbone_spline import extract_backbones
    from .bundle_writer import write_bundle

    supercell_spec = str(args.supercell if args.supercell is not None else "auto").strip()
    auto_supercell = supercell_spec.lower() in ("", "auto")
    supercell = (1, 1, 1) if auto_supercell else _parse_triplet(supercell_spec, int, (1, 1, 1))
    pad = _parse_triplet(args.boundary_pad, float, (0.05, 0.05, 0.05))
    hkl = _parse_triplet(args.hkl, int, (0, 0, 1))

    structure = parse_structure(args.input)
    mask = filter_disorder(structure.disorder_groups, args.disorder_filter)
    structure = _apply_mask(structure, mask)
    unit_cell_atoms = structure.n_atoms

    supercell_info = {"enabled": False, "requested": supercell_spec}
    if auto_supercell:
        supercell, supercell_info = _auto_supercell(structure, args.k_tol)

    sc = make_supercell(structure, *supercell)
    graph = build_bond_graph(sc, k_tol=args.k_tol)
    components = analyze_components(sc.n_atoms, graph)

    # PBC clustering: unwrap every periodic component into one coherent
    # molecule/chain inside the sidecar, so the .npz never carries padding
    # images, ghost copies or half-bond stubs.
    cluster_opt = str(getattr(args, "pbc_cluster", "auto") or "auto").strip().lower()
    requested_mode = str(args.pbc_mode or "pbc_cluster").strip().lower()
    pbc_cluster_info = {
        "requested": cluster_opt,
        "enabled": False,
        "pbc_unwrapped": False,
        "status": "off",
        "fallback_mode": None,
        "warning": None,
    }
    fallback_mode = "periodic_clipped" if requested_mode == "pbc_cluster" else requested_mode
    if requested_mode != "pbc_cluster":
        # An explicit legacy mode wins: the user asked for that realization.
        pbc_cluster_info.update(status="off", reason="explicit_pbc_mode")
        geom = realize(sc, graph, components, mode=requested_mode, boundary_pad=pad,
                       chain_repeat_count=args.chain_repeat_count, hkl=hkl,
                       slab_thickness=args.slab_thickness)
    elif cluster_opt != "auto":
        pbc_cluster_info.update(status="off", reason="pbc_cluster_disabled")
        geom = realize(sc, graph, components, mode=fallback_mode, boundary_pad=pad,
                       chain_repeat_count=args.chain_repeat_count, hkl=hkl,
                       slab_thickness=args.slab_thickness)
    else:
        try:
            geom = realize(sc, graph, components, mode="pbc_cluster", boundary_pad=pad,
                           chain_repeat_count=args.chain_repeat_count, hkl=hkl,
                           slab_thickness=args.slab_thickness)
            pbc_cluster_info.update(enabled=True, pbc_unwrapped=True, status="unwrapped")
        except (UnwrapError, RuntimeError) as exc:
            pbc_cluster_info.update(
                status="fallback", fallback_mode=fallback_mode,
                warning=f"pbc cluster unwrap failed ({type(exc).__name__}: {exc}); "
                        f"fell back to {fallback_mode}")
            geom = realize(sc, graph, components, mode=fallback_mode, boundary_pad=pad,
                           chain_repeat_count=args.chain_repeat_count, hkl=hkl,
                           slab_thickness=args.slab_thickness)

    bb_points, bb_offsets, is_bb = extract_backbones(sc, graph, components, geom)
    geom.is_backbone = is_bb

    a, b, c, al, be, ga = structure.lattice_parameters
    rank_counts = Counter(int(comp.rank) for comp in components)
    chains = _chain_records(components)
    disorder = _disorder_summary(structure, args.disorder_filter)
    clusters = list(getattr(geom, "clusters", []) or [])
    pbc_cluster_info["clusters"] = clusters
    pbc_cluster_info["n_clusters"] = len(clusters)
    pbc_cluster_info["cut_periodic_edges"] = [int(k) for k in getattr(geom, "cut_edges", [])]
    dmax = _max_render_bond(geom)

    manifest_base = {
        "formula": structure.formula,
        "source": os.path.abspath(args.input),
        "space_group": {
            "symbol": structure.spacegroup_symbol,
            "number": int(structure.spacegroup_number),
            "hall_number": int(structure.hall_number),
        },
        "lattice": {"a": a, "b": b, "c": c, "alpha": al, "beta": be, "gamma": ga},
        "supercell": list(supercell),
        "supercell_auto": supercell_info,
        "pbc_mode": geom.mode,
        "pbc_mode_requested": requested_mode,
        "pbc_unwrapped": bool(getattr(geom, "pbc_unwrapped", False)),
        "pbc_cluster": pbc_cluster_info,
        "boundary_pad": list(pad),
        "hkl": list(hkl),
        "polymer": {
            "n_components": len(components),
            "n_chains": int(rank_counts.get(1, 0)),
            "rank_counts": {str(k): int(v) for k, v in sorted(rank_counts.items())},
            "chains": chains,
            "chain_axes": [ch["axis"] for ch in chains if ch["axis"] is not None],
        },
        "disorder": disorder,
        "validation": {
            "max_rendered_bond_length": round(dmax, 4),
            "max_allowed_bond_length": MAX_RENDER_BOND,
            "zero_cross_box_long_bonds": bool(dmax <= MAX_RENDER_BOND + 1e-6),
        },
    }

    out_dir = os.path.abspath(args.out)
    bundle_path, manifest_path, manifest = write_bundle(
        out_dir, geom, bb_points, bb_offsets, manifest_base)

    report = {
        "supercell": list(supercell),
        "supercell_auto": supercell_info,
        "crystal": {
            "formula": structure.formula,
            "space_group": manifest_base["space_group"],
            "lattice": manifest_base["lattice"],
            "n_atoms_unit_cell": unit_cell_atoms,
            "n_atoms_supercell": sc.n_atoms,
        },
        "topology": manifest_base["polymer"],
        "pbc_cluster": pbc_cluster_info,
        "geometry": {
            "mode": geom.mode,
            "pbc_unwrapped": bool(getattr(geom, "pbc_unwrapped", False)),
            "n_atoms": int(len(geom.atomic_numbers)),
            "n_internal_bonds": int(len(geom.bonds_idx)),
            "n_half_bonds": int(len(geom.half_bond_z)),
            "n_ghost_atoms": int(geom.is_ghost.sum()),
            "n_boundary_pad_atoms": int(geom.is_boundary_pad.sum()),
            "n_cut_periodic_edges": int(len(getattr(geom, "cut_edges", []) or [])),
            "n_clusters": int(len(clusters)),
            "n_backbone_curves": int(len(bb_offsets) - 1),
            "max_render_bond_angstrom": round(dmax, 4),
            "invariant_limit_angstrom": MAX_RENDER_BOND,
            "invariant_ok": bool(dmax <= MAX_RENDER_BOND + 1e-6),
        },
        "disorder": disorder,
    }
    if pbc_cluster_info.get("warning"):
        report["warning"] = pbc_cluster_info["warning"]
    print(json.dumps({"ok": True, "bundle": bundle_path,
                      "manifest": manifest_path, "report": report}))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="polycrystal_core.cli",
                                description="Polymer crystallography PBC sidecar engine")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Probe installed packages and print JSON status")

    b = sub.add_parser("build", help="Parse a structure and emit bundle.npz + manifest.json")
    b.add_argument("--input", required=True, help="CIF/POSCAR/XYZ/PDB/LAMMPS data file")
    b.add_argument("--supercell", default="1,1,1",
                   help="Na,Nb,Nc (default 1,1,1 — input CIFs are often already expanded), "
                        "or 'auto': stretch the polymer chain axis until it shows %d repeat periods"
                        % AUTO_SUPERCELL_REPEATS)
    b.add_argument("--pbc-mode", default="pbc_cluster",
                   choices=["pbc_cluster", "periodic_clipped", "chain_continuous",
                            "hkl_lamella"],
                   help="pbc_cluster (default): unwrap each periodic component into "
                        "one coherent molecule/chain; no pad/ghost/half bonds. "
                        "periodic_clipped: legacy padding + half-bond stubs. "
                        "chain_continuous: cut + unwrap strands. "
                        "hkl_lamella: Miller slab.")
    b.add_argument("--pbc-cluster", default="auto", choices=["auto", "off"],
                   help="PBC molecular clustering before export. 'auto' (default) "
                        "unwraps and falls back to --pbc-mode with a warning when "
                        "the unwrap is inconsistent; 'off' keeps the legacy path.")
    b.add_argument("--boundary-pad", default="0.05,0.05,0.05", help="da,db,dc fractional pad")
    b.add_argument("--disorder-filter", default="default",
                   help="default|all|group_2|ordered|group_N")
    b.add_argument("--out", required=True, help="Output directory for bundle/manifest")
    b.add_argument("--k-tol", type=float, default=1.20, help="Covalent radius tolerance")
    b.add_argument("--hkl", default="0,0,1", help="Miller indices for hkl_lamella")
    b.add_argument("--slab-thickness", type=float, default=15.0, help="Slab thickness (A)")
    b.add_argument("--chain-repeat-count", type=int, default=None)
    b.add_argument("--backend", default="native_gn",
                   choices=["native_gn", "batoms", "molecularnodes"],
                   help="Rendering backend hint recorded in the manifest (consumed by Blender)")
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "build":
        try:
            return cmd_build(args)
        except Exception as e:  # never leak a traceback to the host
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
            return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())