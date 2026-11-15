# SPDX-License-Identifier: GPL-3.0-or-later
#!/usr/bin/env python3
"""Headless smoke test for the DSH PolyCrystal Blender extension.

Run with a Blender 4.2+ binary::

    "<blender>" -b --factory-startup --python-exit-code 1 -P scripts/test_blender_ext.py

The test never needs the DSH gateway.  It builds a synthetic ``bundle.npz``,
loads it through the extension, evaluates the Geometry Nodes modifier, toggles
``Hide Hydrogens`` in real time, exercises the RPC dispatcher (including the AI
lock) and finally unregisters everything.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import bpy
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
EXTENSION_PARENT = REPO_ROOT / "extension"
sys.path.insert(0, str(EXTENSION_PARENT))

import dsh_polycrystal_ext as ext  # noqa: E402

PASSED = []


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    PASSED.append(message)
    print(f"[smoke] ok: {message}")


def make_bundle(folder: Path):
    atoms = 4
    positions = np.array(
        [
            [0.00, 0.00, 0.00],
            [1.54, 0.00, 0.00],
            [-1.09, 0.00, 0.00],
            [2.63, 0.00, 0.00],
        ],
        np.float32,
    )
    cell = np.array(
        [
            [0, 0, 0],
            [10, 0, 0],
            [10, 10, 0],
            [0, 10, 0],
            [0, 0, 10],
            [10, 0, 10],
            [10, 10, 10],
            [0, 10, 10],
        ],
        np.float32,
    )
    cell_edges = np.array(
        [
            [0, 1], [1, 2], [2, 3], [3, 0],
            [4, 5], [5, 6], [6, 7], [7, 4],
            [0, 4], [1, 5], [2, 6], [3, 7],
        ],
        np.int32,
    )
    arrays = {
        "positions": positions,
        "frac_positions": positions / 10.0,
        "atomic_numbers": np.array([6, 6, 1, 1], np.int16),
        "vdw_radii": np.array([1.70, 1.70, 1.20, 1.20], np.float32),
        "cov_radii": np.array([0.76, 0.76, 0.31, 0.31], np.float32),
        "chain_ids": np.zeros(atoms, np.int32),
        "component_ranks": np.ones(atoms, np.int8),
        "disorder_groups": np.zeros(atoms, np.int16),
        "occupancies": np.ones(atoms, np.float32),
        "is_backbone": np.array([True, True, False, False]),
        "is_ghost": np.zeros(atoms, bool),
        "is_boundary_pad": np.zeros(atoms, bool),
        "bonds_idx": np.array([[0, 1], [0, 2], [1, 3]], np.int32),
        "half_bond_starts": np.array([[1.54, 0.0, 0.0]], np.float32),
        "half_bond_ends": np.array([[2.05, 0.0, 0.0]], np.float32),
        "half_bond_z": np.array([6], np.int16),
        "cell_vertices": cell,
        "cell_edges": cell_edges,
        "backbone_points": np.array([[0.0, 0.0, 0.0], [1.54, 0.0, 0.0]], np.float32),
        "backbone_curve_offsets": np.array([0, 2], np.int32),
    }
    npz_path = folder / "bundle.npz"
    np.savez(npz_path, **arrays)

    manifest = {
        "formula": "C2H2",
        "space_group": "Pnam",
        "hall_number": 123,
        "lattice": {"a": 10.0, "b": 10.0, "c": 10.0, "alpha": 90.0, "beta": 90.0, "gamma": 90.0},
        "chain_axes": [[0, 0, 1]],
        "chains": 1,
        "disorder": "none",
        "sha256": "deadbeef" * 8,
        "pbc_mode": "periodic_clipped",
    }
    manifest_path = folder / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return npz_path, manifest_path


def evaluated_vertex_count(obj):
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    return len(evaluated.data.vertices), len(evaluated.data.attributes)


def main():
    ext.register()
    check(bpy.types.Scene.dsh_polycrystal is not None, "scene PropertyGroup registered")
    check(bpy.types.VIEW3D_PT_dsh_polycrystal is not None, "N-panel registered")
    check(
        bpy.app.timers.is_registered(ext.bridge_client.process_rpc_queue),
        "RPC timer registered",
    )

    props = bpy.context.scene.dsh_polycrystal
    props.allow_ai_edits = True
    props.lock_camera_and_lights = False

    with tempfile.TemporaryDirectory(prefix="dsh_polycrystal_") as tmp:
        folder = Path(tmp)
        npz_path, manifest_path = make_bundle(folder)

        report = ext.loader_npz.load_bundle(
            npz_path, manifest_path, name="smoke_pe"
        )
        props = bpy.context.scene.dsh_polycrystal
        check(report["ok"] and report["atoms"] == 4, "bundle loaded (4 atoms)")
        check(report["chains"] == 1, "chain count reported")

        obj = bpy.data.objects.get(ext.props_mod.CRYSTAL_NAME)
        check(obj is not None and obj.type == "MESH", "crystal object created")
        modifier = next((m for m in obj.modifiers if m.type == "NODES"), None)
        check(modifier is not None and modifier.node_group is not None, "GN modifier attached")
        check(
            modifier.node_group.name == ext.props_mod.GROUP_NAME,
            "master node group name",
        )

        # v0.3.1: unit cell lives on its own shadowless object and the atom
        # spheres default to icosphere subdivisions 3 (320 faces per atom).
        cell = bpy.data.objects.get(ext.props_mod.CELL_NAME)
        check(cell is not None and len(cell.data.edges) == 12, "unit cell object (12 edges)")
        check(cell.visible_shadow is False, "unit cell does not cast shadows")
        check(
            any(m.type == "NODES" and m.node_group.name == ext.props_mod.GROUP_NAME
                for m in cell.modifiers),
            "unit cell shares the master modifier",
        )
        check(props.subdivision_quality == 3, "atom subdivision default is 3")
        check(
            "is_cell_edge" in {a.name for a in cell.data.attributes},
            "unit cell carries is_cell_edge",
        )

        expected_sockets = {
            "Style Mode",
            "Atom Scale",
            "Bond Radius",
            "Hide Hydrogens",
            "Show Ghost Boundary Atoms",
            "Show Half-Bond Caps",
            "Show Backbone Tube",
            "Color Scheme",
            "Active Disorder Group",
            "Show Unit Cell Box",
            "Subdivision Quality",
        }
        actual_sockets = set(ext.geonodes_builder.socket_names())
        check(expected_sockets <= actual_sockets, "all 11 GN sockets present")

        attribute_names = {attr.name for attr in obj.data.attributes}
        for name in (
            "is_atom_pt",
            "atomic_number",
            "vdw_radius",
            "cov_radius",
            "chain_id",
            "disorder_group",
            "is_backbone",
            "is_ghost",
            "is_half_bond",
            "is_backbone_edge",
            "is_cell_edge",
        ):
            check(name in attribute_names, f"mesh attribute '{name}' imported")

        full_vertices, _ = evaluated_vertex_count(obj)
        check(full_vertices > 0, f"GN evaluated geometry ({full_vertices} vertices)")

        props.hide_hydrogens = True
        hidden_vertices, _ = evaluated_vertex_count(obj)
        check(
            hidden_vertices < full_vertices,
            f"Hide Hydrogens socket is live ({full_vertices} -> {hidden_vertices})",
        )
        props.hide_hydrogens = False

        props.style_mode = 1
        spacefill_vertices, _ = evaluated_vertex_count(obj)
        check(spacefill_vertices > 0, "space-filling style evaluates")
        props.style_mode = 0

        props.show_unit_cell = False
        bpy.context.view_layer.update()
        props.show_unit_cell = True

        state = ext.bridge_client.dispatch_rpc("polycrystal.get_state", {})
        check(state["ok"], "RPC get_state ok")
        for key in (
            "allow_ai_edits",
            "lock_camera_and_lights",
            "lock_custom_materials",
            "style_mode",
            "atom_scale",
            "hide_hydrogens",
            "camera",
            "selection",
            "crystallography",
        ):
            check(key in state, f"get_state exposes '{key}'")
        check(
            state["crystallography"]["space_group"] == "Pnam",
            "manifest crystallography returned",
        )

        props.allow_ai_edits = False
        blocked = ext.bridge_client.dispatch_rpc(
            "polycrystal.set_style", {"hide_hydrogens": True}
        )
        check(
            not blocked["ok"] and blocked.get("reason") == "ai_edits_disabled",
            "AI lock rejects set_style",
        )
        blocked_load = ext.bridge_client.dispatch_rpc(
            "polycrystal.load_bundle", {"npz_path": str(npz_path)}
        )
        check(
            not blocked_load["ok"]
            and blocked_load.get("reason") == "ai_edits_disabled",
            "AI lock rejects load_bundle",
        )
        props.allow_ai_edits = True

        styled = ext.bridge_client.dispatch_rpc(
            "polycrystal.set_style",
            {
                "style_mode": "licorice",
                "color_scheme": "chain",
                "atom_scale": 0.42,
                "show_backbone_tube": True,
                "highlight_chain_ids": [0],
            },
        )
        check(styled["ok"] and styled["applied"]["style_mode"] == 2, "set_style applies")
        check(props.show_backbone_tube, "set_style drives GN socket")

        props.lock_camera_and_lights = True
        locked_camera = ext.bridge_client.dispatch_rpc("polycrystal.frame_camera", {})
        check(locked_camera.get("skipped") is True, "camera lock respected")
        props.lock_camera_and_lights = False

        camera = ext.bridge_client.dispatch_rpc(
            "polycrystal.frame_camera", {"view": "iso"}
        )
        check(camera["ok"] and bpy.context.scene.camera is not None, "camera framing works")

        reloaded = ext.bridge_client.dispatch_rpc(
            "polycrystal.load_bundle",
            {"npz_path": str(npz_path), "manifest_path": str(manifest_path)},
        )
        check(reloaded["ok"] and reloaded["report"]["atoms"] == 4, "RPC load_bundle works")

        auto = ext.auto_connect()
        check(auto["ok"], "auto_connect() returns ok without a gateway")

        # cleanup loaded datablocks before unregistering (the scene keeps them)
        root = bpy.data.objects.get(ext.props_mod.ROOT_NAME)
        if root is not None:
            for child in list(root.children_recursive):
                bpy.data.objects.remove(child, do_unlink=True)
            bpy.data.objects.remove(root, do_unlink=True)

    ext.unregister()
    check(
        not bpy.app.timers.is_registered(ext.bridge_client.process_rpc_queue),
        "RPC timer unregistered",
    )
    check(
        not hasattr(bpy.types.Scene, "dsh_polycrystal"),
        "scene PropertyGroup removed",
    )
    print(f"[smoke] PASS ({len(PASSED)} checks)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        traceback.print_exc()
        print("[smoke] FAIL")
        sys.exit(1)
