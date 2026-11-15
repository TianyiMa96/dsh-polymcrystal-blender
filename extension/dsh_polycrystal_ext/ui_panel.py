# SPDX-License-Identifier: GPL-3.0-or-later
"""3D Viewport N-panel for the DSH PolyCrystal bridge.

Layout (``VIEW_3D -> UI -> PolyCrystal``):

1. Connection + AI governance box
2. Live polymer / PBC Geometry Nodes controls
3. Read-only crystallography card from ``manifest.json``
"""

from __future__ import annotations

import os

import bpy

from . import bridge_client
from . import loader_npz
from . import props as props_mod

ICON_OK = "CHECKMARK"
ICON_OFF = "CANCEL"
ICON_WAIT = "SORTTIME"


# --------------------------------------------------------------------------- #
# Operators
# --------------------------------------------------------------------------- #

class DSHPOLYCRYSTAL_OT_connect(bpy.types.Operator):
    """Start (or restart) the outbound connection to the DSH gateway"""

    bl_idname = "dsh_polycrystal.connect"
    bl_label = "Connect to DSH"
    bl_options = {"REGISTER"}

    def execute(self, context):
        client = bridge_client.get_client(create=True, restart=True)
        client.start()
        props = props_mod.get_props(context)
        if props is not None:
            props.gateway_url = client.base_url
        self.report({"INFO"}, f"Connecting to {client.base_url}")
        return {"FINISHED"}


class DSHPOLYCRYSTAL_OT_disconnect(bpy.types.Operator):
    """Stop the bridge thread"""

    bl_idname = "dsh_polycrystal.disconnect"
    bl_label = "Disconnect"
    bl_options = {"REGISTER"}

    def execute(self, context):
        bridge_client.shutdown()
        props = props_mod.get_props(context)
        if props is not None:
            props.connected = False
            props.connection_status = "Disconnected"
            props.instance_id = ""
        props_mod.tag_redraw()
        self.report({"INFO"}, "DSH bridge disconnected")
        return {"FINISHED"}


class DSHPOLYCRYSTAL_OT_send_snapshot(bpy.types.Operator):
    """Send the current selection / chains / camera to DSH"""

    bl_idname = "dsh_polycrystal.send_snapshot"
    bl_label = "Send Selection Snapshot to DSH"
    bl_options = {"REGISTER"}

    def execute(self, context):
        snapshot = bridge_client.publish_selection_snapshot()
        chains = snapshot.get("selected_chains", [])
        self.report(
            {"INFO"},
            f"Snapshot sent ({len(chains)} chain(s), "
            f"{snapshot.get('selected_atoms', 0)} selected atom(s))",
        )
        props_mod.tag_redraw()
        return {"FINISHED"}


class DSHPOLYCRYSTAL_OT_load_bundle(bpy.types.Operator):
    """Load a DSH bundle.npz into the scene"""

    bl_idname = "dsh_polycrystal.load_bundle"
    bl_label = "Load Bundle"
    bl_options = {"REGISTER", "UNDO"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.npz", options={"HIDDEN"})
    manifest_path: bpy.props.StringProperty(subtype="FILE_PATH", default="")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        manifest = self.manifest_path
        if not manifest:
            candidate = os.path.splitext(self.filepath)[0] + ".json"
            if os.path.isfile(candidate):
                manifest = candidate
        try:
            report = loader_npz.load_bundle(self.filepath, manifest or None)
        except Exception as exc:
            self.report({"ERROR"}, f"Bundle load failed: {exc}")
            return {"CANCELLED"}
        props = props_mod.get_props(context)
        if props is not None and not props.lock_camera_and_lights:
            loader_npz.ensure_lighting()
            bridge_client.frame_camera({})
        self.report(
            {"INFO"},
            f"Loaded {report['atoms']} atoms / {report['chains']} chains",
        )
        return {"FINISHED"}


class DSHPOLYCRYSTAL_OT_apply_highlight(bpy.types.Operator):
    """Apply highlighted chain ids to the loaded crystal"""

    bl_idname = "dsh_polycrystal.apply_highlight"
    bl_label = "Apply Highlight"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = props_mod.get_props(context)
        if props is None:
            return {"CANCELLED"}
        count = loader_npz.apply_highlight(props.highlight_chain_ids)
        props_mod.tag_redraw()
        self.report({"INFO"}, f"Highlighted {count} atom(s)")
        return {"FINISHED"}


class DSHPOLYCRYSTAL_OT_frame_camera(bpy.types.Operator):
    """Frame the crystal (respects the camera lock)"""

    bl_idname = "dsh_polycrystal.frame_camera"
    bl_label = "Frame Crystal"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = props_mod.get_props(context)
        if props is not None and props.lock_camera_and_lights:
            self.report({"WARNING"}, "Camera is locked")
            return {"CANCELLED"}
        result = bridge_client.frame_camera({})
        self.report({"INFO"}, f"Camera framed at {result['location']}")
        return {"FINISHED"}


_OPERATORS = (
    DSHPOLYCRYSTAL_OT_connect,
    DSHPOLYCRYSTAL_OT_disconnect,
    DSHPOLYCRYSTAL_OT_send_snapshot,
    DSHPOLYCRYSTAL_OT_load_bundle,
    DSHPOLYCRYSTAL_OT_apply_highlight,
    DSHPOLYCRYSTAL_OT_frame_camera,
)


# --------------------------------------------------------------------------- #
# Panel
# --------------------------------------------------------------------------- #

def _status_icon(props):
    if props is None:
        return ICON_OFF
    if props.connected:
        return ICON_OK
    if props.connection_status == "Connecting...":
        return ICON_WAIT
    return ICON_OFF


def _draw_connection(layout, context, props):
    box = layout.box()
    box.label(text="DSH Bridge", icon="URL")

    row = box.row(align=True)
    icon = _status_icon(props)
    row.label(
        text=f"Connected: {props.instance_id[:12]}" if props.connected else props.connection_status,
        icon=icon,
    )
    row.operator("dsh_polycrystal.connect", text="Connect", icon="PLUGIN")

    if props.connected:
        box.operator("dsh_polycrystal.disconnect", icon="UNLINKED")
    if props.last_error:
        sub = box.column(align=True)
        sub.scale_y = 0.8
        sub.label(text=props.last_error[:90], icon="ERROR")

    col = box.column(align=True)
    col.prop(props, "gateway_url", text="Gateway")
    col.prop(props, "allow_ai_edits", text="Allow AI Edits")
    col.prop(props, "lock_camera_and_lights", text="Lock Camera && Lights")
    col.prop(props, "lock_custom_materials", text="Lock Custom Materials")

    box.operator("dsh_polycrystal.send_snapshot", icon="EXPORT")


def _draw_style(layout, props):
    box = layout.box()
    box.label(text="Polymer Style (Geometry Nodes)", icon="NODES")

    col = box.column(align=True)
    col.prop(props, "style_mode", text="Style Mode")
    col.label(text="  " + props_mod.STYLE_MODES[props.style_mode], icon="DOT")

    col = box.column(align=True)
    col.prop(props, "atom_scale", text="Atom Scale")
    col.prop(props, "bond_radius", text="Bond Radius")
    col.prop(props, "subdivision_quality", text="Subdivision Quality")

    col = box.column(align=True)
    col.prop(props, "hide_hydrogens", text="Hide Hydrogens")
    col.prop(props, "show_ghost_atoms", text="Show Ghost Boundary Atoms")
    col.prop(props, "show_half_bonds", text="Show Half-Bond Caps")
    col.prop(props, "show_backbone_tube", text="Show Backbone Tube")
    col.prop(props, "show_unit_cell", text="Show Unit Cell Box")

    col = box.column(align=True)
    col.prop(props, "color_scheme", text="Color Scheme")
    col.label(text="  " + props_mod.COLOR_SCHEMES[props.color_scheme], icon="COLOR")
    col.prop(props, "active_disorder_group", text="Active Disorder Group")
    col.label(
        text="  " + props_mod.DISORDER_MODES[props.active_disorder_group], icon="MOD_DISPLACE"
    )

    row = box.row(align=True)
    row.prop(props, "highlight_chain_ids", text="Chains")
    row.operator("dsh_polycrystal.apply_highlight", text="", icon="SOLO_ON")


def _fmt(value, digits=4):
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    if value in (None, ""):
        return "?"
    return str(value)


def _draw_crystallography(layout, props):
    box = layout.box()
    summary = loader_npz.manifest_summary()
    box.label(text="Crystallography", icon="WORLD")

    if not summary.get("formula") and not summary.get("space_group"):
        box.label(text="No bundle loaded", icon="INFO")
        if props.bundle_path:
            box.label(text=os.path.basename(props.bundle_path))
        return

    col = box.column(align=True)
    if summary.get("formula"):
        col.label(text=f"Formula: {summary['formula']}")
    if summary.get("space_group"):
        col.label(text=f"Space group: {summary['space_group']}")
    if summary.get("hall_number") not in (None, ""):
        col.label(text=f"Hall: {summary['hall_number']}")

    lattice = summary.get("lattice") or {}
    col = box.column(align=True)
    col.label(
        text="a={}  b={}  c={}".format(
            _fmt(lattice.get("a"), 3),
            _fmt(lattice.get("b"), 3),
            _fmt(lattice.get("c"), 3),
        )
    )
    col.label(
        text="α={}  β={}  γ={}".format(
            _fmt(lattice.get("alpha"), 2),
            _fmt(lattice.get("beta"), 2),
            _fmt(lattice.get("gamma"), 2),
        )
    )

    axes = summary.get("chain_axes") or []
    if axes:
        try:
            axis_text = "  ".join(
                "[" + ", ".join(f"{float(v):g}" for v in axis) + "]"
                for axis in axes[:3]
            )
        except Exception:
            axis_text = str(axes)[:60]
        box.label(text=f"Chain axis: {axis_text}")

    col = box.column(align=True)
    col.scale_y = 0.85
    col.label(
        text=f"Atoms {props.atom_count}   Bonds {props.bond_count}   Chains {props.chain_count}"
    )
    if summary.get("disorder"):
        col.label(text=f"Disorder: {str(summary['disorder'])[:60]}")
    if props.backend:
        col.label(text=f"Backend: {props.backend}")


class VIEW3D_PT_dsh_polycrystal(bpy.types.Panel):
    bl_idname = "VIEW3D_PT_dsh_polycrystal"
    bl_label = "DSH PolyCrystal"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "PolyCrystal"
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        props = props_mod.get_props(context)
        if props is None:
            layout.label(text="Properties unavailable", icon="ERROR")
            return
        _draw_connection(layout, context, props)
        _draw_style(layout, props)
        _draw_crystallography(layout, props)

        layout.separator()
        layout.operator("dsh_polycrystal.load_bundle", icon="FILE_FOLDER")
        layout.operator("dsh_polycrystal.frame_camera", icon="CAMERA_DATA")


# --------------------------------------------------------------------------- #
# Viewport header badge
# --------------------------------------------------------------------------- #

def draw_view3d_header(self, context):
    props = props_mod.get_props(context)
    if props is None:
        return
    if not props.connected and props.connection_status != "Connecting...":
        return
    if props.connected:
        label = "DSH PolyCrystal: Connected"
        if not props.allow_ai_edits:
            label += " [AI Paused]"
        elif props.lock_camera_and_lights:
            label += " [Camera Locked]"
        icon = ICON_OK
    else:
        label = "DSH PolyCrystal: Connecting..."
        icon = ICON_WAIT
    try:
        self.layout.separator_spacer()
        self.layout.label(text=label, icon=icon)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Register / unregister
# --------------------------------------------------------------------------- #

def register():
    for operator in _OPERATORS:
        bpy.utils.register_class(operator)
    bpy.utils.register_class(VIEW3D_PT_dsh_polycrystal)
    bpy.types.VIEW3D_HT_header.append(draw_view3d_header)


def unregister():
    try:
        bpy.types.VIEW3D_HT_header.remove(draw_view3d_header)
    except Exception:
        pass
    try:
        bpy.utils.unregister_class(VIEW3D_PT_dsh_polycrystal)
    except Exception:
        pass
    for operator in reversed(_OPERATORS):
        try:
            bpy.utils.unregister_class(operator)
        except Exception:
            pass
