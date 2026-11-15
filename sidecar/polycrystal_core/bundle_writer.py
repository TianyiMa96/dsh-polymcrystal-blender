# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 6 output: canonical bundle.npz + manifest.json (agent.md 3.3).

Array key names and dtypes are fixed by the specification so the Blender
extension's zero-dependency loader can map them straight onto mesh attributes.
"""
from __future__ import annotations

import os
import json
import hashlib
import itertools

import numpy as np

from .structure import cov_radius, vdw_radius
from .polymer_unwrap import RealizedGeometry

SCHEMA_VERSION = "1.0.0"


def cell_wireframe(cell):
    """8 corners + 12 edges of the exact triclinic parallelepiped."""
    corners = np.array(list(itertools.product([0, 1], repeat=3)), dtype=float) @ cell
    edges = np.array([
        (0, 1), (0, 2), (0, 4), (1, 3), (1, 5), (2, 3),
        (2, 6), (3, 7), (4, 5), (4, 6), (5, 7), (6, 7)], dtype=int)
    return corners, edges


def _f32(a):
    return np.asarray(a, dtype=np.float32)


def build_arrays(geom: RealizedGeometry, backbone_points, backbone_offsets):
    z = geom.atomic_numbers
    corners, edges = cell_wireframe(geom.cell)
    return {
        "positions": _f32(geom.positions).reshape(-1, 3),
        "frac_positions": _f32(geom.frac_positions).reshape(-1, 3),
        "atomic_numbers": np.asarray(z, dtype=np.int16),
        "vdw_radii": _f32([vdw_radius(int(zz)) for zz in z]),
        "cov_radii": _f32([cov_radius(int(zz)) for zz in z]),
        "chain_ids": np.asarray(geom.chain_ids, dtype=np.int32),
        "component_ranks": np.asarray(geom.component_ranks, dtype=np.int8),
        "disorder_groups": np.asarray(geom.disorder_groups, dtype=np.int16),
        "occupancies": _f32(geom.occupancies),
        "is_backbone": np.asarray(geom.is_backbone, dtype=bool),
        "is_ghost": np.asarray(geom.is_ghost, dtype=bool),
        "is_boundary_pad": np.asarray(geom.is_boundary_pad, dtype=bool),
        "bonds_idx": np.asarray(geom.bonds_idx, dtype=np.int32).reshape(-1, 2),
        "half_bond_starts": _f32(geom.half_bond_starts).reshape(-1, 3),
        "half_bond_ends": _f32(geom.half_bond_ends).reshape(-1, 3),
        "half_bond_z": np.asarray(geom.half_bond_z, dtype=np.int16),
        "cell_vertices": _f32(corners).reshape(8, 3),
        "cell_edges": np.asarray(edges, dtype=np.int32).reshape(12, 2),
        "backbone_points": _f32(backbone_points).reshape(-1, 3),
        "backbone_curve_offsets": np.asarray(backbone_offsets, dtype=np.int32),
    }


def write_bundle(out_dir, geom: RealizedGeometry, backbone_points,
                 backbone_offsets, manifest_base=None):
    """Write bundle.npz + manifest.json. Returns (bundle_path, manifest_path, manifest)."""
    os.makedirs(out_dir, exist_ok=True)
    arrays = build_arrays(geom, backbone_points, backbone_offsets)
    bundle_path = os.path.join(out_dir, "bundle.npz")
    np.savez_compressed(bundle_path, **arrays)

    with open(bundle_path, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()

    manifest = dict(manifest_base or {})
    manifest["schema_version"] = SCHEMA_VERSION
    manifest["sha256"] = sha
    manifest["counts"] = {
        "atoms": int(len(arrays["atomic_numbers"])),
        "internal_bonds": int(len(arrays["bonds_idx"])),
        "half_bonds": int(len(arrays["half_bond_z"])),
        "ghost_atoms": int(arrays["is_ghost"].sum()),
        "boundary_pad_atoms": int(arrays["is_boundary_pad"].sum()),
        "clusters": int(len(getattr(geom, "clusters", []) or [])),
        "backbone_points": int(len(arrays["backbone_points"])),
        "backbone_curves": int(len(arrays["backbone_curve_offsets"]) - 1),
    }
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    return bundle_path, manifest_path, manifest