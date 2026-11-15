# SPDX-License-Identifier: GPL-3.0-or-later
"""Zero-dependency ``bundle.npz`` importer.

Reads the canonical array keys produced by the DSH sidecar
(:mod:`polycrystal_core.bundle_writer`), builds the crystal mesh from:

* atom vertices + point attributes,
* covalent bond edges + half-bond stub edges,
* backbone polyline segments,

and a second ``PolyCrystal_UnitCell`` object holding only the triclinic cell
wireframe.  Both objects share the master Geometry Nodes modifier; the cell
object has ``visible_shadow = False`` so its tubes cannot stripe shadows across
the crystal.  Only ``bpy`` and Blender's bundled ``numpy`` are used.
"""

from __future__ import annotations

import json
import math
import os
import time

import bpy
import numpy as np
from mathutils import Vector

from . import geonodes_builder
from . import props as props_mod

# Canonical bundle keys (agent.md section 3.3)
KEY_POSITIONS = "positions"
KEY_FRAC = "frac_positions"
KEY_Z = "atomic_numbers"
KEY_VDW = "vdw_radii"
KEY_COV = "cov_radii"
KEY_CHAINS = "chain_ids"
KEY_RANKS = "component_ranks"
KEY_DISORDER = "disorder_groups"
KEY_OCCUPANCY = "occupancies"
KEY_IS_BACKBONE = "is_backbone"
KEY_IS_GHOST = "is_ghost"
KEY_IS_PAD = "is_boundary_pad"
KEY_BONDS = "bonds_idx"
KEY_HB_START = "half_bond_starts"
KEY_HB_END = "half_bond_ends"
KEY_HB_Z = "half_bond_z"
KEY_CELL_V = "cell_vertices"
KEY_CELL_E = "cell_edges"
KEY_BB_POINTS = "backbone_points"
KEY_BB_OFFSETS = "backbone_curve_offsets"

DEFAULT_VDW = 1.70
DEFAULT_COV = 0.70

SCENE_MANIFEST = "dsh_polycrystal_manifest"
SCENE_MANIFEST_PATH = "dsh_polycrystal_manifest_path"
SCENE_NPZ_PATH = "dsh_polycrystal_npz_path"
SCENE_LOADED_AT = "dsh_polycrystal_loaded_at"
SCENE_SELECTION = "dsh_polycrystal_selection_snapshot"
WORLD_VERSION = "3"


# --------------------------------------------------------------------------- #
# npz helpers
# --------------------------------------------------------------------------- #

def _array(npz, key, shape, dtype):
    """Read an optional array.  A zero in ``shape`` means "any length"."""
    if key not in npz:
        return np.zeros(shape, dtype=dtype)
    value = np.asarray(npz[key])
    if value.size == 0:
        return np.zeros(shape, dtype=dtype)
    if value.ndim == 0:
        value = value.reshape(1)
    if value.ndim != len(shape):
        raise ValueError(
            f"bundle array '{key}' has {value.ndim} dimensions, expected {len(shape)}"
        )
    for got, want in zip(value.shape, shape):
        if want and got != want:
            raise ValueError(
                f"bundle array '{key}' has shape {value.shape}, expected {shape}"
            )
    return value.astype(dtype, copy=False)


# --------------------------------------------------------------------------- #
# Mesh attribute helpers
# --------------------------------------------------------------------------- #

def _put(mesh, name, data_type, domain, array):
    attr = mesh.attributes.get(name)
    if attr is None:
        attr = mesh.attributes.new(name=name, type=data_type, domain=domain)
    elif attr.data_type != data_type or attr.domain != domain:
        mesh.attributes.remove(attr)
        attr = mesh.attributes.new(name=name, type=data_type, domain=domain)
    if data_type == "FLOAT_VECTOR":
        attr.data.foreach_set("vector", np.ascontiguousarray(array, np.float32).ravel())
    elif data_type == "FLOAT_COLOR":
        attr.data.foreach_set("color", np.ascontiguousarray(array, np.float32).ravel())
    elif data_type == "BOOLEAN":
        attr.data.foreach_set("value", np.ascontiguousarray(array, bool))
    elif data_type == "INT":
        attr.data.foreach_set("value", np.ascontiguousarray(array, np.int32))
    else:
        attr.data.foreach_set("value", np.ascontiguousarray(array, np.float32))
    return attr


def _put_bool_mirror(mesh, name, domain, values):
    """Store a boolean attribute and its float mirror used by GN maths."""
    values = np.asarray(values, bool)
    _put(mesh, name, "BOOLEAN", domain, values)
    _put(mesh, name + "_f", "FLOAT", domain, values.astype(np.float32))


# --------------------------------------------------------------------------- #
# Scene helpers
# --------------------------------------------------------------------------- #

def _ensure_collection(scene):
    collection = bpy.data.collections.get(props_mod.COLLECTION_NAME)
    if collection is None:
        collection = bpy.data.collections.new(props_mod.COLLECTION_NAME)
    if collection.name not in {child.name for child in scene.collection.children}:
        scene.collection.children.link(collection)
    return collection


def _remove_existing():
    root = bpy.data.objects.get(props_mod.ROOT_NAME)
    if root is not None:
        for child in list(root.children_recursive):
            bpy.data.objects.remove(child, do_unlink=True)
        bpy.data.objects.remove(root, do_unlink=True)
    for name in (props_mod.CRYSTAL_NAME, props_mod.CELL_NAME):
        obj = bpy.data.objects.get(name)
        if obj is not None:
            bpy.data.objects.remove(obj, do_unlink=True)
    # The factory startup cube sits inside the crystal bbox and ruins both the
    # framing and the picture; drop it only when it is still pristine.
    cube = bpy.data.objects.get("Cube")
    if (
        cube is not None
        and cube.type == "MESH"
        and len(cube.data.vertices) == 8
        and cube.users_collection
        and all(
            child.name in {"Collection", "Scene Collection"}
            for child in cube.users_collection
        )
    ):
        try:
            bpy.data.objects.remove(cube, do_unlink=True)
        except Exception:
            pass


def _safe_name(text):
    cleaned = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in text)
    return cleaned.strip("_")[:48] or "crystal"


# --------------------------------------------------------------------------- #
# Public loader
# --------------------------------------------------------------------------- #

def load_bundle(npz_path, manifest_path=None, name=None, backend="native_gn"):
    """Load a canonical geometry bundle into the current scene.

    Returns a JSON-serialisable report.  Raises ``ValueError`` for malformed
    bundles and ``OSError`` when the file is missing.
    """
    npz_path = os.fspath(npz_path)
    if not os.path.isfile(npz_path):
        raise OSError(f"bundle not found: {npz_path}")

    with np.load(npz_path, allow_pickle=False) as npz:
        if KEY_POSITIONS not in npz:
            raise ValueError("bundle is missing the required 'positions' array")
        positions = np.asarray(npz[KEY_POSITIONS], np.float32)
        if positions.ndim != 2 or positions.shape[1] != 3:
            raise ValueError(
                f"positions has shape {positions.shape}, expected (N, 3)"
            )
        atom_count = int(positions.shape[0])
        if atom_count == 0:
            raise ValueError("bundle contains zero atoms")

        frac = _array(npz, KEY_FRAC, (atom_count, 3), np.float32)
        atomic_numbers = _array(npz, KEY_Z, (atom_count,), np.int16)
        vdw = _array(npz, KEY_VDW, (atom_count,), np.float32)
        cov = _array(npz, KEY_COV, (atom_count,), np.float32)
        chain_ids = _array(npz, KEY_CHAINS, (atom_count,), np.int32)
        ranks = _array(npz, KEY_RANKS, (atom_count,), np.int8)
        disorder = _array(npz, KEY_DISORDER, (atom_count,), np.int16)
        occupancy = _array(npz, KEY_OCCUPANCY, (atom_count,), np.float32)
        is_backbone = _array(npz, KEY_IS_BACKBONE, (atom_count,), bool)
        is_ghost = _array(npz, KEY_IS_GHOST, (atom_count,), bool)
        is_pad = _array(npz, KEY_IS_PAD, (atom_count,), bool)

        bonds = _array(npz, KEY_BONDS, (0, 2), np.int32)
        if bonds.size == 0:
            bonds = np.zeros((0, 2), np.int32)
        hb_start = _array(npz, KEY_HB_START, (0, 3), np.float32)
        if hb_start.size == 0:
            hb_start = np.zeros((0, 3), np.float32)
        hb_end = _array(npz, KEY_HB_END, (0, 3), np.float32)
        if hb_end.size == 0:
            hb_end = np.zeros((0, 3), np.float32)
        hb_z = _array(npz, KEY_HB_Z, (0,), np.int16)
        if hb_z.size == 0:
            hb_z = np.zeros((0,), np.int16)

        cell_vertices = _array(npz, KEY_CELL_V, (0, 3), np.float32)
        if cell_vertices.size == 0:
            cell_vertices = np.zeros((0, 3), np.float32)
        cell_edges = _array(npz, KEY_CELL_E, (0, 2), np.int32)
        if cell_edges.size == 0:
            cell_edges = np.zeros((0, 2), np.int32)

        bb_points = _array(npz, KEY_BB_POINTS, (0, 3), np.float32)
        if bb_points.size == 0:
            bb_points = np.zeros((0, 3), np.float32)
        bb_offsets = _array(npz, KEY_BB_OFFSETS, (0,), np.int32)
        if bb_offsets.size == 0:
            bb_offsets = np.zeros((0,), np.int32)

    # ---------------------------------------------------------------- #
    # Sanitise indices and fill derived values
    # ---------------------------------------------------------------- #
    if bonds.size:
        valid = (
            (bonds[:, 0] >= 0)
            & (bonds[:, 1] >= 0)
            & (bonds[:, 0] < atom_count)
            & (bonds[:, 1] < atom_count)
            & (bonds[:, 0] != bonds[:, 1])
        )
        bonds = bonds[valid]
    if hb_z.shape[0] != hb_start.shape[0]:
        hb_z = np.zeros((hb_start.shape[0],), np.int16)

    if not np.any(vdw > 0):
        vdw = np.full((atom_count,), DEFAULT_VDW, np.float32)
    if not np.any(cov > 0):
        cov = np.full((atom_count,), DEFAULT_COV, np.float32)
    vdw = np.where(vdw > 0, vdw, DEFAULT_VDW).astype(np.float32)
    cov = np.where(cov > 0, cov, DEFAULT_COV).astype(np.float32)

    hb_count = int(hb_start.shape[0])
    bb_count = int(bb_points.shape[0])
    cell_count = int(cell_vertices.shape[0])

    base_hb = atom_count
    base_bb = base_hb + 2 * hb_count
    base_cell = base_bb + bb_count
    # The unit cell block lives on its own object now, so the crystal mesh
    # stops right before it; ``cell_vertices`` is consumed by ``_build_cell_mesh``.
    vertex_count = base_cell

    # ---------------------------------------------------------------- #
    # Combined vertex array
    # ---------------------------------------------------------------- #
    verts = np.zeros((vertex_count, 3), np.float32)
    verts[:atom_count] = positions
    if hb_count:
        pairs = np.empty((hb_count * 2, 3), np.float32)
        pairs[0::2] = hb_start
        pairs[1::2] = hb_end
        verts[base_hb:base_bb] = pairs
    if bb_count:
        verts[base_bb:base_cell] = bb_points

    # ---------------------------------------------------------------- #
    # Edges
    # ---------------------------------------------------------------- #
    edge_blocks = []
    edge_kinds = []  # 'bond', 'half', 'backbone', 'cell'

    if bonds.size:
        edge_blocks.append(bonds.astype(np.int32))
        edge_kinds.append(("bond", bonds.shape[0]))
    if hb_count:
        hb_edges = np.empty((hb_count, 2), np.int32)
        hb_edges[:, 0] = base_hb + np.arange(hb_count) * 2
        hb_edges[:, 1] = hb_edges[:, 0] + 1
        edge_blocks.append(hb_edges)
        edge_kinds.append(("half", hb_count))
    if bb_count and bb_offsets.shape[0] >= 2:
        segments = []
        chain_count = int(bb_offsets.shape[0]) - 1
        for chain in range(chain_count):
            start = int(bb_offsets[chain])
            stop = int(bb_offsets[chain + 1])
            start = max(0, min(start, bb_count))
            stop = max(start, min(stop, bb_count))
            if stop - start >= 2:
                idx = np.arange(start, stop - 1, dtype=np.int32) + base_bb
                segments.append(np.stack([idx, idx + 1], axis=1))
        if segments:
            bb_edges = np.concatenate(segments, axis=0).astype(np.int32)
            edge_blocks.append(bb_edges)
            edge_kinds.append(("backbone", bb_edges.shape[0]))
    cell_edge_array = np.zeros((0, 2), np.int32)
    if cell_count and cell_edges.size:
        valid_cell = (
            (cell_edges[:, 0] >= 0)
            & (cell_edges[:, 1] >= 0)
            & (cell_edges[:, 0] < cell_count)
            & (cell_edges[:, 1] < cell_count)
        )
        cell_edges = cell_edges[valid_cell]
        if cell_edges.size:
            cell_edge_array = cell_edges.astype(np.int32)

    if edge_blocks:
        edges = np.concatenate(edge_blocks, axis=0)
    else:
        edges = np.zeros((0, 2), np.int32)
    edge_count = int(edges.shape[0])

    flags = {
        "bond": np.zeros(edge_count, bool),
        "half": np.zeros(edge_count, bool),
        "backbone": np.zeros(edge_count, bool),
        "cell": np.zeros(edge_count, bool),
    }
    cursor = 0
    for kind, count in edge_kinds:
        flags[kind][cursor:cursor + count] = True
        cursor += count

    has_h = np.zeros(edge_count, bool)
    if bonds.size:
        has_h[: bonds.shape[0]] = (
            (atomic_numbers[bonds[:, 0]] == 1) | (atomic_numbers[bonds[:, 1]] == 1)
        )
    if hb_count:
        offset = bonds.shape[0]
        has_h[offset:offset + hb_count] = hb_z == 1

    # ---------------------------------------------------------------- #
    # Build mesh
    # ---------------------------------------------------------------- #
    _remove_existing()
    scene = bpy.context.scene
    collection = _ensure_collection(scene)

    label = _safe_name(name or os.path.splitext(os.path.basename(npz_path))[0])
    mesh = bpy.data.meshes.new(label + "_mesh")
    mesh.vertices.add(vertex_count)
    mesh.vertices.foreach_set("co", verts.ravel())
    if edge_count:
        mesh.edges.add(edge_count)
        mesh.edges.foreach_set("vertices", edges.ravel())
    mesh.update()
    try:
        mesh.validate(verbose=False, clean_customdata=False)
    except TypeError:
        mesh.validate(verbose=False)

    # ---- point attributes -------------------------------------------- #
    is_atom = np.zeros(vertex_count, bool)
    is_atom[:atom_count] = True
    hydro = np.zeros(vertex_count, bool)
    hydro[:atom_count] = atomic_numbers == 1

    zeros_f = np.zeros(vertex_count, np.float32)
    zeros_i = np.zeros(vertex_count, np.int32)

    point_z = zeros_i.copy()
    point_z[:atom_count] = atomic_numbers.astype(np.int32)
    point_chain = zeros_i.copy()
    point_chain[:atom_count] = chain_ids
    point_disorder = zeros_i.copy()
    point_disorder[:atom_count] = disorder
    point_occ = zeros_f.copy()
    point_occ[:atom_count] = occupancy

    point_vdw = np.full(vertex_count, DEFAULT_VDW, np.float32)
    point_vdw[:atom_count] = vdw
    point_cov = np.full(vertex_count, DEFAULT_COV, np.float32)
    point_cov[:atom_count] = cov

    bb_flag = np.zeros(vertex_count, bool)
    bb_flag[:atom_count] = is_backbone
    ghost_flag = np.zeros(vertex_count, bool)
    ghost_flag[:atom_count] = is_ghost
    highlight = np.zeros(vertex_count, bool)

    frac_vec = np.zeros((vertex_count, 3), np.float32)
    frac_vec[:atom_count] = frac

    _put_bool_mirror(mesh, "is_atom_pt", "POINT", is_atom)
    _put_bool_mirror(mesh, "is_hydrogen", "POINT", hydro)
    _put_bool_mirror(mesh, "is_backbone", "POINT", bb_flag)
    _put_bool_mirror(mesh, "is_ghost", "POINT", ghost_flag)
    _put_bool_mirror(mesh, "is_highlighted", "POINT", highlight)
    _put(mesh, "atomic_number", "INT", "POINT", point_z)
    _put(mesh, "atomic_number_f", "FLOAT", "POINT", point_z.astype(np.float32))
    _put(mesh, "chain_id", "INT", "POINT", point_chain)
    _put(mesh, "chain_id_f", "FLOAT", "POINT", point_chain.astype(np.float32))
    _put(mesh, "disorder_group", "INT", "POINT", point_disorder)
    _put(mesh, "disorder_group_f", "FLOAT", "POINT", point_disorder.astype(np.float32))
    _put(mesh, "component_rank", "INT", "POINT", _pad_int(ranks, vertex_count))
    _put(mesh, "occupancy", "FLOAT", "POINT", point_occ)
    _put(mesh, "vdw_radius", "FLOAT", "POINT", point_vdw)
    _put(mesh, "cov_radius", "FLOAT", "POINT", point_cov)
    _put(mesh, "is_boundary_pad", "BOOLEAN", "POINT", _pad_bool(is_pad, vertex_count))
    _put(mesh, "frac_position", "FLOAT_VECTOR", "POINT", frac_vec)

    # ---- edge attributes --------------------------------------------- #
    _put_bool_mirror(mesh, "is_bond_edge", "EDGE", flags["bond"] | flags["half"])
    _put_bool_mirror(mesh, "is_half_bond", "EDGE", flags["half"])
    _put_bool_mirror(mesh, "has_h", "EDGE", has_h)
    _put_bool_mirror(mesh, "is_backbone_edge", "EDGE", flags["backbone"])
    _put_bool_mirror(mesh, "is_cell_edge", "EDGE", flags["cell"])

    mesh.update()

    # ---------------------------------------------------------------- #
    # Object / collection / modifier
    # ---------------------------------------------------------------- #
    root = bpy.data.objects.new(props_mod.ROOT_NAME, None)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = 1.0
    collection.objects.link(root)

    obj = bpy.data.objects.new(props_mod.CRYSTAL_NAME, mesh)
    collection.objects.link(obj)
    obj.parent = root

    materials = geonodes_builder.material_slots()
    for material in materials:
        mesh.materials.append(material)

    group = geonodes_builder.ensure_master_group()
    modifier = obj.modifiers.new("PolyCrystal Master GN", "NODES")
    modifier.node_group = group

    # ---------------------------------------------------------------- #
    # Unit cell: separate object with visible_shadow = False
    # ---------------------------------------------------------------- #
    cell_obj = None
    cell_edge_count = int(cell_edge_array.shape[0])
    if cell_count:
        cell_mesh = bpy.data.meshes.new(label + "_cell_mesh")
        cell_mesh.vertices.add(cell_count)
        cell_mesh.vertices.foreach_set(
            "co", np.ascontiguousarray(cell_vertices, np.float32).ravel()
        )
        if cell_edge_count:
            cell_mesh.edges.add(cell_edge_count)
            cell_mesh.edges.foreach_set(
                "vertices", np.ascontiguousarray(cell_edge_array, np.int32).ravel()
            )
        cell_mesh.update()
        try:
            cell_mesh.validate(verbose=False, clean_customdata=False)
        except TypeError:
            cell_mesh.validate(verbose=False)
        _put_bool_mirror(
            cell_mesh,
            "is_cell_edge",
            "EDGE",
            np.ones(cell_edge_count, bool),
        )
        cell_mesh.update()
        for material in materials:
            cell_mesh.materials.append(material)

        cell_obj = bpy.data.objects.new(props_mod.CELL_NAME, cell_mesh)
        collection.objects.link(cell_obj)
        cell_obj.parent = root
        # The amber cage must be visible but must not paint stripe shadows
        # across the model.  Object-level visibility is honoured by EEVEE and
        # Cycles alike; a shader trick would still leave the shadow terminator.
        for attr, value in (
            ("visible_shadow", False),
            ("hide_render", False),
            ("hide_viewport", False),
        ):
            if hasattr(cell_obj, attr):
                try:
                    setattr(cell_obj, attr, value)
                except Exception:
                    pass
        cell_modifier = cell_obj.modifiers.new("PolyCrystal Master GN", "NODES")
        cell_modifier.node_group = group
        cell_obj["polycrystal_role"] = "unit_cell"
        cell_obj["polycrystal_visible_shadow"] = False

    # ---------------------------------------------------------------- #
    # Manifest
    # ---------------------------------------------------------------- #
    manifest = {}
    manifest_path = os.fspath(manifest_path) if manifest_path else ""
    if manifest_path and os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except Exception as exc:  # keep the geometry even with a broken manifest
            manifest = {"_error": f"manifest parse failed: {exc}"}

    manifest_json = json.dumps(manifest, ensure_ascii=False, default=str)
    scene[SCENE_MANIFEST] = manifest_json
    scene[SCENE_MANIFEST_PATH] = manifest_path
    scene[SCENE_NPZ_PATH] = npz_path
    scene[SCENE_LOADED_AT] = time.time()

    obj["polycrystal_role"] = "crystal"
    obj["polycrystal_manifest"] = manifest_json
    obj["polycrystal_atom_count"] = atom_count
    obj["polycrystal_bond_count"] = int(bonds.shape[0])
    obj["polycrystal_backend"] = backend

    stats = props_mod.get_props()
    if stats is not None:
        stats.atom_count = atom_count
        stats.bond_count = int(bonds.shape[0])
        stats.chain_count = int(len(np.unique(chain_ids))) if atom_count else 0
        stats.structure_name = str(manifest.get("formula") or label)
        stats.bundle_path = npz_path
        stats.manifest_path = manifest_path
        stats.backend = backend or "native_gn"
        props_mod.sync_all_props_to_modifiers(stats)
        apply_highlight(_parse_chain_ids(stats.highlight_chain_ids))

    bbox_min = positions.min(axis=0)
    bbox_max = positions.max(axis=0)

    # Ghost pad atoms are PBC bookkeeping copies, not part of the model.  They
    # stay hidden by default; when the user re-enables them the report has to
    # say what those translucent cyan spheres are.
    ghost_total = int(np.count_nonzero(is_ghost))
    ghost_visible = bool(getattr(stats, "show_ghost_atoms", False)) if stats else False
    polymer = manifest.get("polymer") if isinstance(manifest.get("polymer"), dict) else {}
    chain_axes = polymer.get("chain_axes") or manifest.get("chain_axes") or []
    notes = []
    ghost_note = ""
    if ghost_total:
        if ghost_visible:
            ghost_note = (
                f"蓝色半透明球为周期边界 ghost 原子：{ghost_total} 个 PBC 补位副本"
                "（可在 N 面板关闭 Show Ghost Boundary Atoms）"
            )
            notes.append(ghost_note)
        else:
            notes.append(
                f"bundle 内含 {ghost_total} 个周期边界 ghost 原子，默认已隐藏"
            )
    supercell = manifest.get("supercell") or []
    if manifest.get("supercell_auto", {}).get("enabled"):
        auto = manifest["supercell_auto"]
        axis = auto.get("chain_axis")
        notes.append(
            f"supercell 自动扩展为 {list(supercell)}：沿链轴 {axis} 展开 "
            f"{auto.get('repeats', 2)} 个重复周期"
        )

    # PBC 聚类展开（sidecar 端）：分子/链已是完整连贯实体，bundle 里不应再出现
    # ghost 补位、边界 pad 或半键残端。
    pbc_unwrapped = bool(manifest.get("pbc_unwrapped"))
    pbc_clusters = []
    if isinstance(manifest.get("pbc_cluster"), dict):
        pbc_clusters = manifest["pbc_cluster"].get("clusters") or []
    if pbc_unwrapped:
        notes.append(
            f"PBC 聚类展开：{len(pbc_clusters)} 个分子/链在 sidecar 内按周期边界"
            f"解开为连贯实体（ghost {ghost_total}、pad {int(np.count_nonzero(is_pad))}、"
            f"半键 {hb_count}）"
        )

    obj["polycrystal_ghost_atoms"] = ghost_total
    obj["polycrystal_supercell"] = json.dumps(list(supercell))
    obj["polycrystal_pbc_unwrapped"] = pbc_unwrapped

    return {
        "ok": True,
        "object": obj.name,
        "collection": collection.name,
        "atoms": atom_count,
        "bonds": int(bonds.shape[0]),
        "half_bonds": hb_count,
        "backbone_points": bb_count,
        "cell_vertices": cell_count,
        "cell_edges": cell_edge_count,
        "cell_object": cell_obj.name if cell_obj is not None else "",
        "cell_visible_shadow": False if cell_obj is not None else None,
        "chains": int(len(np.unique(chain_ids))),
        "chain_axes": chain_axes,
        "supercell": list(supercell),
        "supercell_auto": manifest.get("supercell_auto", {"enabled": False}),
        "pbc_mode": manifest.get("pbc_mode", ""),
        "pbc_unwrapped": pbc_unwrapped,
        "pbc_clusters": pbc_clusters,
        "ghost_atoms": ghost_total,
        "ghost_visible": ghost_visible,
        "ghost_note": ghost_note,
        "notes": notes,
        "highlighted_vertices": int(highlight.sum()),
        "bbox_min": [round(float(v), 4) for v in bbox_min],
        "bbox_max": [round(float(v), 4) for v in bbox_max],
        "manifest_loaded": bool(manifest),
        "backend": backend or "native_gn",
    }


def _pad_int(values, length):
    result = np.zeros(length, np.int32)
    count = min(length, values.shape[0])
    result[:count] = values[:count].astype(np.int32)
    return result


def _pad_bool(values, length):
    result = np.zeros(length, bool)
    count = min(length, values.shape[0])
    result[:count] = values[:count].astype(bool)
    return result


# --------------------------------------------------------------------------- #
# Highlight / scene utilities
# --------------------------------------------------------------------------- #

def _parse_chain_ids(value):
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = str(value or "").replace(";", ",").split(",")
    result = []
    for item in items:
        try:
            result.append(int(str(item).strip()))
        except (TypeError, ValueError):
            continue
    return result


def apply_highlight(chain_ids):
    """Flag atoms belonging to the requested chain ids for emissive colouring."""
    chain_ids = set(_parse_chain_ids(chain_ids))
    obj = bpy.data.objects.get(props_mod.CRYSTAL_NAME)
    if obj is None or obj.type != "MESH" or obj.data is None:
        return 0
    mesh = obj.data
    count = len(mesh.vertices)
    attr = mesh.attributes.get("is_highlighted")
    if attr is None or len(attr.data) != count:
        _put_bool_mirror(mesh, "is_highlighted", "POINT", np.zeros(count, bool))
        attr = mesh.attributes.get("is_highlighted")
    mask = np.zeros(count, bool)
    if chain_ids and attr is not None:
        chain_attr = mesh.attributes.get("chain_id")
        if chain_attr is not None and len(chain_attr.data) == count:
            chain_values = np.zeros(count, np.int32)
            chain_attr.data.foreach_get("value", chain_values)
            atom_mask = np.zeros(count, bool)
            atom_attr = mesh.attributes.get("is_atom_pt")
            if atom_attr is not None:
                atom_attr.data.foreach_get("value", atom_mask)
            wanted = np.isin(chain_values, list(chain_ids))
            mask = atom_mask & wanted
    _put_bool_mirror(mesh, "is_highlighted", "POINT", mask)
    mesh.update()
    return int(mask.sum())


def read_manifest(scene=None):
    scene = scene or bpy.context.scene
    raw = scene.get(SCENE_MANIFEST, "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {"_error": "stored manifest is not valid JSON"}


def manifest_summary(manifest=None):
    """Compact crystallography card data used by the N-panel and get_state."""
    manifest = manifest if manifest is not None else read_manifest()
    lattice = manifest.get("lattice") or {}
    if not isinstance(lattice, dict):
        lattice = {}
    chain_axes = manifest.get("chain_axes") or manifest.get("polymer_chain_axes") or []
    return {
        "formula": manifest.get("formula", ""),
        "space_group": manifest.get("space_group")
        or manifest.get("space_group_symbol", ""),
        "hall_number": manifest.get("hall_number", ""),
        "lattice": {
            "a": lattice.get("a", manifest.get("a", "")),
            "b": lattice.get("b", manifest.get("b", "")),
            "c": lattice.get("c", manifest.get("c", "")),
            "alpha": lattice.get("alpha", manifest.get("alpha", "")),
            "beta": lattice.get("beta", manifest.get("beta", "")),
            "gamma": lattice.get("gamma", manifest.get("gamma", "")),
        },
        "chain_axes": chain_axes,
        "chains": manifest.get("chains", manifest.get("chain_count", "")),
        "disorder": manifest.get("disorder", manifest.get("disorder_summary", "")),
        "sha256": manifest.get("sha256", manifest.get("hash", "")),
        "pbc_mode": manifest.get("pbc_mode", ""),
    }


def _aim(obj, target=(0.0, 0.0, 0.0)):
    """Point an object's local -Z axis at ``target`` (lights, cameras)."""
    try:
        from mathutils import Vector

        direction = Vector(target) - obj.location
        if direction.length > 1e-6:
            obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    except Exception:
        pass


def _ensure_world(scene):
    """Dark graduated studio backdrop so CPK colours read correctly.

    A flat mid-grey backdrop used to be brighter than the model itself, which
    made every render look washed out; the gradient keeps the lower half nearly
    black so the crystal always sits on a darker field than its own shading.
    """
    world = scene.world
    if world is None:
        world = bpy.data.worlds.new("PolyCrystal_World")
        scene.world = world
    if world.get("dsh_polycrystal_built") == WORLD_VERSION:
        return False
    world.use_nodes = True
    tree = world.node_tree
    tree.nodes.clear()
    out = tree.nodes.new("ShaderNodeOutputWorld")
    out.location = (600, 0)
    bg = tree.nodes.new("ShaderNodeBackground")
    bg.location = (380, 0)
    bg.inputs["Strength"].default_value = 1.0
    coord = tree.nodes.new("ShaderNodeTexCoord")
    coord.location = (-340, 0)
    sep = tree.nodes.new("ShaderNodeSeparateXYZ")
    sep.location = (-140, 0)
    ramp = tree.nodes.new("ShaderNodeValToRGB")
    ramp.location = (100, 0)
    ramp.color_ramp.elements[0].position = 0.0
    ramp.color_ramp.elements[0].color = (0.012, 0.014, 0.020, 1.0)
    ramp.color_ramp.elements[1].position = 1.0
    ramp.color_ramp.elements[1].color = (0.085, 0.092, 0.120, 1.0)
    tree.links.new(coord.outputs["Generated"], sep.inputs["Vector"])
    tree.links.new(sep.outputs["Z"], ramp.inputs["Fac"])
    tree.links.new(ramp.outputs["Color"], bg.inputs["Color"])
    tree.links.new(bg.outputs["Background"], out.inputs["Surface"])
    world["dsh_polycrystal_built"] = WORLD_VERSION
    return True


def _ensure_color_management(scene):
    """AgX + a filmic look; Standard clips the specular highlights flat."""
    view = scene.view_settings
    for value in ("AgX", "Filmic", "Standard"):
        try:
            view.view_transform = value
            break
        except Exception:
            continue
    for look in ("AgX - Medium High Contrast", "Medium High Contrast", "None"):
        try:
            view.look = look
            break
        except Exception:
            continue
    try:
        view.exposure = 0.2
    except Exception:
        pass
    eevee = getattr(scene, "eevee", None)
    if eevee is not None:
        for attr, value in (
            ("use_raytracing", True),
            ("use_shadows", True),
            ("use_volumetric_shadows", True),
            ("taa_render_samples", 64),
        ):
            if hasattr(eevee, attr):
                try:
                    setattr(eevee, attr, value)
                except Exception:
                    pass
    try:
        scene.render.film_transparent = False
    except Exception:
        pass


def ensure_lighting():
    """Match a deterministic studio look; returns True when a rig was added.

    The world, colour management and EEVEE quality settings are refreshed on
    every call (they are cheap and scene-independent); the adaptive
    Key/Fill/Rim rig plus the small ``PolyCrystal_Spec`` clear-coat light is
    only created when the scene has no PolyCrystal light of its own.
    """
    scene = bpy.context.scene
    _ensure_world(scene)
    _ensure_color_management(scene)

    lights = [obj for obj in scene.objects if obj.type == "LIGHT"]
    existing = {obj.name for obj in lights}
    rig_present = any(name.startswith("PolyCrystal_") for name in existing)
    # Our own rig already owns the scene lighting: leave it alone (but make
    # sure a rig created by an older version also gets the specular light).
    if not rig_present:
        # A pristine startup scene only holds the factory lamp; it is far too
        # dim and misses the subject entirely, so swap it for the studio rig.
        for obj in lights:
            if obj.name.startswith("Light") and obj.name in {"Light", "Light.001"}:
                try:
                    bpy.data.objects.remove(obj, do_unlink=True)
                except Exception:
                    pass
    collection = _ensure_collection(scene)

    # Place the rig around the actual subject: fixed world coordinates sat
    # inside tall crystals and lit only a corner of them.  Power follows the
    # inverse square law so a 20 A crystal is as bright as a 9 A one.
    center, extent = _mesh_bounds(scene)
    span = max(extent.x, extent.y, extent.z, 2.0)
    radius = span * 1.05
    gain = (radius / 9.0) ** 2

    # The graduated world is intentionally almost black, so it contributes no
    # ambient light: the rig has to carry the subject on its own.  Key/Fill/Rim
    # shape the volume, the small Spec light exists only to put a tight clear
    # coat highlight on the atoms (the classic molecular-viewer look).
    rig = (
        ("PolyCrystal_Key", (0.62, -0.55, 0.56), 420.0 * gain, max(3.0, span * 0.35)),
        ("PolyCrystal_Fill", (-0.68, -0.42, 0.30), 150.0 * gain, max(4.0, span * 0.6)),
        ("PolyCrystal_Rim", (-0.25, 0.78, 0.42), 260.0 * gain, max(2.0, span * 0.28)),
        ("PolyCrystal_Spec", (0.30, -0.72, 0.90), 220.0 * gain, max(1.0, span * 0.13)),
    )
    created = 0
    for name, direction, energy, size in rig:
        if name in existing:
            continue
        data = bpy.data.lights.new(name, "AREA")
        data.energy = energy
        data.size = size
        obj = bpy.data.objects.new(name, data)
        vector = Vector(direction).normalized() * radius
        obj.location = center + vector
        _aim(obj, center)
        collection.objects.link(obj)
        created += 1
    return bool(created)


def _mesh_bounds(scene):
    """Return (center, extent) of every mesh object in the scene."""
    points = []
    for obj in scene.objects:
        if obj.type != "MESH":
            continue
        for corner in obj.bound_box:
            points.append(obj.matrix_world @ Vector(corner))
    if not points:
        return Vector((0.0, 0.0, 0.0)), Vector((4.0, 4.0, 4.0))
    lo = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
    hi = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
    return (lo + hi) * 0.5, hi - lo
