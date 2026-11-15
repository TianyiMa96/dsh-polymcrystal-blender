# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent Blender properties for the DSH PolyCrystal bridge.

All human-facing N-panel controls live in a single ``bpy.props.PropertyGroup``
attached to ``bpy.types.Scene``.  Every Geometry Nodes socket is mirrored by a
property with a stable snake_case name so that ``polycrystal.get_state`` can
return exactly the values the human sees in the sidebar.
"""

from __future__ import annotations

import bpy
from bpy.props import (
    BoolProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

# --------------------------------------------------------------------------- #
# Shared constants (also imported by loader_npz / geonodes_builder / ui_panel)
# --------------------------------------------------------------------------- #

GROUP_NAME = "PolyCrystal_Master_GN"
ROOT_NAME = "PolyCrystal_Root"
CRYSTAL_NAME = "PolyCrystal_Crystal"
#: The unit cell wireframe is a separate object so it can carry
#: ``visible_shadow = False``; see ``geonodes_builder`` for the rationale.
CELL_NAME = "PolyCrystal_UnitCell"
COLLECTION_NAME = "PolyCrystal"
SCENE_PROP = "dsh_polycrystal"

#: property name -> Geometry Nodes group input socket name
GN_SOCKETS = {
    "style_mode": "Style Mode",
    "atom_scale": "Atom Scale",
    "bond_radius": "Bond Radius",
    "hide_hydrogens": "Hide Hydrogens",
    "show_ghost_atoms": "Show Ghost Boundary Atoms",
    "show_half_bonds": "Show Half-Bond Caps",
    "show_backbone_tube": "Show Backbone Tube",
    "color_scheme": "Color Scheme",
    "active_disorder_group": "Active Disorder Group",
    "show_unit_cell": "Show Unit Cell Box",
    "subdivision_quality": "Subdivision Quality",
}

STYLE_MODES = (
    "Ball & Stick",
    "Space-Filling VdW",
    "Licorice",
    "Backbone Tube + Sidechain Stick",
)

COLOR_SCHEMES = (
    "Element CPK",
    "Polymer Chain ID",
    "Backbone vs Sidechain",
    "Disorder Group",
)

DISORDER_MODES = (
    "Group 1 (default)",
    "Group 2",
    "Both superimposed",
)

#: every GN-backed property, in N-panel order
GN_PROP_KEYS = tuple(GN_SOCKETS.keys())

#: read-only / governance / connection keys returned by get_state
STATE_PROP_KEYS = (
    "connected",
    "connection_status",
    "instance_id",
    "gateway_url",
    "last_error",
    "allow_ai_edits",
    "lock_camera_and_lights",
    "lock_custom_materials",
    "backend",
    "highlight_chain_ids",
    "atom_count",
    "bond_count",
    "chain_count",
    "structure_name",
    "bundle_path",
    "manifest_path",
)

_SYNC_GUARD = False


# --------------------------------------------------------------------------- #
# Modifier synchronisation helpers
# --------------------------------------------------------------------------- #

def iter_crystal_objects():
    """Yield every object that may carry the master GN modifier."""
    root = bpy.data.objects.get(ROOT_NAME)
    seen = set()
    if root is not None:
        seen.add(root.name)
        yield root
        for child in root.children_recursive:
            if child.name not in seen:
                seen.add(child.name)
                yield child
    crystal = bpy.data.objects.get(CRYSTAL_NAME)
    if crystal is not None and crystal.name not in seen:
        yield crystal


def iter_master_modifiers():
    for obj in iter_crystal_objects():
        for mod in getattr(obj, "modifiers", ()):
            if mod.type != "NODES":
                continue
            group = getattr(mod, "node_group", None)
            if group is not None and group.name == GROUP_NAME:
                yield obj, mod


def _socket_identifier(group, socket_name):
    """Map a group input name to the identifier used as modifier key."""
    try:
        for item in group.interface.items_tree:
            if (
                getattr(item, "in_out", None) == "INPUT"
                and getattr(item, "name", None) == socket_name
            ):
                return item.identifier
    except Exception:
        pass
    return None


def _modifier_interface(mod):
    """Blender >= 5.x stores modifier inputs under ``mod.properties.inputs``."""
    return getattr(getattr(mod, "properties", None), "inputs", None)


def set_modifier_input(mod, identifier, socket_name, value):
    """Write one Geometry Nodes modifier input across Blender versions.

    4.x exposed inputs as ID properties (``mod["Socket_2"]``); 5.0 removed
    ID property support from modifiers entirely, so the typed interface
    (``mod.properties.inputs.Socket_2.value``) must be preferred.
    """
    key = identifier or socket_name
    interface = _modifier_interface(mod)
    if interface is not None and key:
        sock = getattr(interface, key, None)
        if sock is not None:
            try:
                sock.value = value
                return True
            except Exception:
                pass
    try:
        mod[key] = value
        return True
    except Exception:
        return False


def get_modifier_input(mod, identifier, socket_name, default=None):
    """Read one Geometry Nodes modifier input across Blender versions."""
    key = identifier or socket_name
    interface = _modifier_interface(mod)
    if interface is not None and key:
        sock = getattr(interface, key, None)
        if sock is not None:
            try:
                return sock.value
            except Exception:
                pass
    try:
        return mod.get(key, default)
    except Exception:
        return default


def apply_prop_to_modifiers(key, value):
    """Push one property value into every master modifier input."""
    socket_name = GN_SOCKETS.get(key)
    if socket_name is None:
        return
    for obj, mod in iter_master_modifiers():
        identifier = _socket_identifier(mod.node_group, socket_name)
        try:
            if not set_modifier_input(mod, identifier, socket_name, value):
                continue
            # Modifier input changes made from scripts do not always tag the
            # dependency graph; force the owner object's data to re-evaluate.
            tagged = False
            for refresh in ({"DATA"}, {"OBJECT"}):
                try:
                    obj.update_tag(refresh=refresh)
                    tagged = True
                    break
                except Exception:
                    continue
            if not tagged:
                try:
                    obj.update_tag()
                except Exception:
                    pass
        except Exception:
            # A missing socket means the node group was rebuilt without it.
            pass


def sync_all_props_to_modifiers(props):
    """Push every mirrored property into the GN modifiers (after load)."""
    if props is None:
        return
    for key in GN_PROP_KEYS:
        apply_prop_to_modifiers(key, getattr(props, key))


def get_props(context=None):
    """Return the scene PropertyGroup, or ``None`` when not registered."""
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    if scene is None:
        return None
    return getattr(scene, SCENE_PROP, None)


def tag_redraw():
    """Request a redraw of all 3D viewports (safe outside of the UI)."""
    context = bpy.context
    screen = getattr(context, "screen", None)
    if screen is None:
        return
    for area in screen.areas:
        if area.type == "VIEW_3D":
            area.tag_redraw()


def _make_gn_update(key):
    def _update(self, context):
        if _SYNC_GUARD:
            return
        apply_prop_to_modifiers(key, getattr(self, key))
        tag_redraw()

    return _update


# --------------------------------------------------------------------------- #
# PropertyGroup
# --------------------------------------------------------------------------- #

class DSHPolyCrystalProps(bpy.types.PropertyGroup):
    """Scene level state for the PolyCrystal bridge and N-panel."""

    # -- connection ------------------------------------------------------- #
    connected: BoolProperty(
        name="Connected",
        description="A DSH gateway instance is currently registered",
        default=False,
    )
    connection_status: StringProperty(
        name="Connection Status",
        default="Disconnected",
    )
    instance_id: StringProperty(
        name="Instance ID",
        default="",
    )
    gateway_url: StringProperty(
        name="Gateway URL",
        default="http://127.0.0.1:39888",
    )
    last_error: StringProperty(
        name="Last Error",
        default="",
    )
    last_heartbeat: FloatProperty(
        name="Last Heartbeat",
        description="Unix timestamp of the last successful gateway contact",
        default=0.0,
    )

    # -- AI governance ---------------------------------------------------- #
    allow_ai_edits: BoolProperty(
        name="Allow AI Edits",
        description="When disabled, AI style / bundle mutations are rejected",
        default=True,
    )
    lock_camera_and_lights: BoolProperty(
        name="Lock Camera & Lights",
        description="Prevent DSH from moving the camera or adding lights",
        default=False,
    )
    lock_custom_materials: BoolProperty(
        name="Lock Custom Materials",
        description="Prevent DSH from replacing user look-dev materials",
        default=False,
    )

    # -- Geometry Nodes sockets ------------------------------------------ #
    style_mode: IntProperty(
        name="Style Mode",
        description="0 Ball & Stick, 1 Space-Filling, 2 Licorice, 3 Backbone Tube",
        default=0,
        min=0,
        max=3,
        update=_make_gn_update("style_mode"),
    )
    atom_scale: FloatProperty(
        name="Atom Scale",
        description="Multiplier applied to van der Waals / covalent radii",
        default=0.36,
        min=0.01,
        max=3.0,
        update=_make_gn_update("atom_scale"),
    )
    bond_radius: FloatProperty(
        name="Bond Radius",
        description="Radius of covalent bond cylinders (Angstrom)",
        default=0.15,
        min=0.005,
        max=1.0,
        update=_make_gn_update("bond_radius"),
    )
    hide_hydrogens: BoolProperty(
        name="Hide Hydrogens",
        description="Filter hydrogen atoms and every bond attached to them",
        default=False,
        update=_make_gn_update("hide_hydrogens"),
    )
    show_ghost_atoms: BoolProperty(
        name="Show Ghost Boundary Atoms",
        description=(
            "Show the translucent cyan periodic-boundary ghost copies. Off by "
            "default: they are PBC bookkeeping, not part of the structural model"
        ),
        default=False,
        update=_make_gn_update("show_ghost_atoms"),
    )
    show_half_bonds: BoolProperty(
        name="Show Half-Bond Caps",
        description="Show clipped half-bond stubs at the unit cell boundary",
        default=True,
        update=_make_gn_update("show_half_bonds"),
    )
    show_backbone_tube: BoolProperty(
        name="Show Backbone Tube",
        description="Render the polymer backbone as a smooth tube",
        default=False,
        update=_make_gn_update("show_backbone_tube"),
    )
    color_scheme: IntProperty(
        name="Color Scheme",
        description="0 CPK, 1 Chain ID, 2 Backbone, 3 Disorder group",
        default=0,
        min=0,
        max=3,
        update=_make_gn_update("color_scheme"),
    )
    active_disorder_group: IntProperty(
        name="Active Disorder Group",
        description="0 group 1, 1 group 2, 2 both superimposed",
        default=0,
        min=0,
        max=2,
        update=_make_gn_update("active_disorder_group"),
    )
    show_unit_cell: BoolProperty(
        name="Show Unit Cell Box",
        description="Render the triclinic unit cell wireframe",
        default=True,
        update=_make_gn_update("show_unit_cell"),
    )
    subdivision_quality: IntProperty(
        name="Subdivision Quality",
        description="Icosphere subdivisions per atom (1 = 20 faces, 3 = 320, 4 = 1280)",
        default=3,
        min=1,
        max=4,
        update=_make_gn_update("subdivision_quality"),
    )

    # -- extra model / state --------------------------------------------- #
    backend: StringProperty(
        name="Backend",
        description="Rendering backend requested by DSH",
        default="native_gn",
    )
    highlight_chain_ids: StringProperty(
        name="Highlighted Chain IDs",
        description="Comma separated chain ids emphasised by the AI",
        default="",
    )
    atom_count: IntProperty(name="Atom Count", default=0, min=0)
    bond_count: IntProperty(name="Bond Count", default=0, min=0)
    chain_count: IntProperty(name="Chain Count", default=0, min=0)
    structure_name: StringProperty(name="Structure", default="")
    bundle_path: StringProperty(name="Bundle", default="", subtype="FILE_PATH")
    manifest_path: StringProperty(name="Manifest", default="", subtype="FILE_PATH")
    selection_snapshot: StringProperty(
        name="Selection Snapshot",
        description="JSON snapshot of the current Blender selection",
        default="",
    )


# --------------------------------------------------------------------------- #
# Register / unregister
# --------------------------------------------------------------------------- #

_CLASSES = (DSHPolyCrystalProps,)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.dsh_polycrystal = PointerProperty(type=DSHPolyCrystalProps)


def unregister():
    if hasattr(bpy.types.Scene, "dsh_polycrystal"):
        del bpy.types.Scene.dsh_polycrystal
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
