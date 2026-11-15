# SPDX-License-Identifier: GPL-3.0-or-later
"""Procedural Geometry Nodes tree for polymer crystal bundles.

Builds a single master node group ``PolyCrystal_Master_GN`` with the exact socket
surface required by the bridge specification.  The group is applied to TWO mesh
objects that split the islands by what they must do at render time:

* ``PolyCrystal_Crystal``  atom points, covalent/half bonds, backbone segments
* ``PolyCrystal_UnitCell``  unit cell edges only, ``visible_shadow = False``

Keeping the cell on its own object is what stops the amber cage from striping
shadow bands across the crystal: object-level shadow visibility exists for both
EEVEE and Cycles, while a per-material switch cannot express it.

Inside the tree the geometry is split by those attributes.  Atoms are rendered
as procedurally generated icospheres, bonds / backbone / cell as curve tubes.
Every filter (hydrogens, ghosts, disorder, half bonds, cell, tube) is a real
Geometry Nodes socket, so the N-panel and the AI RPC drive the same graph.
"""

from __future__ import annotations

import bpy

from . import props as props_mod

GROUP_NAME = props_mod.GROUP_NAME
#: v17 = polished PBR surface (coat + per-island roughness), icosphere default
#: subdivision 3, unit cell rendered as its own shadowless object.
GROUP_VERSION = 17

#: CPK colours keyed by atomic number
CPK_COLORS = {
    1: (0.92, 0.92, 0.92, 1.0),
    6: (0.58, 0.59, 0.63, 1.0),
    7: (0.19, 0.31, 0.97, 1.0),
    8: (0.90, 0.13, 0.13, 1.0),
    9: (0.56, 0.88, 0.31, 1.0),
    15: (1.00, 0.50, 0.00, 1.0),
    16: (0.90, 0.78, 0.15, 1.0),
    17: (0.12, 0.94, 0.12, 1.0),
    35: (0.65, 0.16, 0.16, 1.0),
    53: (0.40, 0.00, 0.73, 1.0),
}

#: eight-colour chain palette (hue wheel, constant interpolation ramp)
CHAIN_PALETTE = (
    (0.20, 0.55, 0.95, 1.0),
    (0.95, 0.45, 0.15, 1.0),
    (0.30, 0.80, 0.40, 1.0),
    (0.85, 0.30, 0.75, 1.0),
    (0.95, 0.80, 0.20, 1.0),
    (0.25, 0.80, 0.80, 1.0),
    (0.60, 0.40, 0.90, 1.0),
    (0.90, 0.55, 0.55, 1.0),
)

DISORDER_PALETTE = (
    (0.80, 0.80, 0.82, 1.0),
    (0.20, 0.60, 0.95, 1.0),
    (0.95, 0.50, 0.15, 1.0),
    (0.65, 0.35, 0.90, 1.0),
)

CELL_MATERIAL = "DSH_PolyCrystal_UnitCell"

#: Single surface material. Blender 5.x ``Join Geometry`` remaps material
#: indices across branches, so every island is coloured through geometry
#: attributes (``col`` / ``alpha`` / ``glow``) instead of per-part materials.
SURFACE_MATERIAL = "DSH_PolyCrystal_Surface"
#: v4 = polished PBR (clear coat + per-island roughness/coat attributes).
SURFACE_MATERIAL_VERSION = "4"
#: The unit cell lives on its own object (``PolyCrystal_UnitCell``) which has
#: ``visible_shadow = False``; this dedicated material keeps the wireframe
#: readable without any shadow contribution of its own.
CELL_MATERIAL_VERSION = "2"


# --------------------------------------------------------------------------- #
# Materials
# --------------------------------------------------------------------------- #

def _set_input(node, names, value):
    for name in names:
        sock = node.inputs.get(name)
        if sock is None:
            continue
        try:
            sock.default_value = value
            return True
        except Exception:
            continue
    return False


def _new_principled(mat):
    mat.use_nodes = True
    tree = mat.node_tree
    tree.nodes.clear()
    out = tree.nodes.new("ShaderNodeOutputMaterial")
    out.location = (400, 0)
    bsdf = tree.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (60, 0)
    tree.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    return tree, bsdf


def _ensure_surface_material():
    """One material for every island; colour, glow and alpha come from geometry.

    Look-dev (v4): a thin clear coat over a soft diffuse base.  Molecular
    viewers read as "material" rather than plastic when the highlight is a
    small, tight coat reflection instead of one broad roughness lobe, so the
    coat is driven by a per-island ``coat`` attribute (strong on atoms, faint
    on bonds) and the base roughness comes from ``rough``.
    """
    mat = bpy.data.materials.get(SURFACE_MATERIAL)
    if mat is None:
        mat = bpy.data.materials.new(SURFACE_MATERIAL)
    elif mat.get("dsh_polycrystal_built") == SURFACE_MATERIAL_VERSION:
        return mat
    tree, bsdf = _new_principled(mat)

    def attribute(name, x, y):
        node = tree.nodes.new("ShaderNodeAttribute")
        node.location = (x, y)
        node.attribute_type = "GEOMETRY"
        node.attribute_name = name
        return node

    col_attr = attribute("col", -720, 160)
    alpha_attr = attribute("alpha", -720, -120)
    glow_attr = attribute("glow", -720, -320)
    hi_attr = attribute("is_highlighted", -720, -520)
    rough_attr = attribute("rough", -720, -700)
    coat_attr = attribute("coat", -720, -860)

    hi_mix = tree.nodes.new("ShaderNodeMixRGB")
    hi_mix.location = (-420, 120)
    hi_mix.blend_type = "MIX"
    hi_mix.inputs["Color2"].default_value = (1.0, 0.55, 0.08, 1.0)
    tree.links.new(hi_attr.outputs["Fac"], hi_mix.inputs["Fac"])
    tree.links.new(col_attr.outputs["Color"], hi_mix.inputs["Color1"])
    tree.links.new(hi_mix.outputs["Color"], bsdf.inputs["Base Color"])

    # Roughness floor: a missing attribute (e.g. a hand-built mesh) must never
    # collapse to a mirror; the GN always stores >= 0.28 anyway.
    rough = tree.nodes.new("ShaderNodeMath")
    rough.location = (-420, -700)
    rough.operation = "MAXIMUM"
    rough.inputs[1].default_value = 0.26
    tree.links.new(rough_attr.outputs["Fac"], rough.inputs[0])
    tree.links.new(rough.outputs[0], bsdf.inputs["Roughness"])

    coat = tree.nodes.new("ShaderNodeMath")
    coat.location = (-420, -860)
    coat.operation = "MAXIMUM"
    coat.inputs[1].default_value = 0.0
    tree.links.new(coat_attr.outputs["Fac"], coat.inputs[0])
    tree.links.new(coat.outputs[0], bsdf.inputs["Coat Weight"])

    emission_col = bsdf.inputs.get("Emission Color") or bsdf.inputs.get("Emission")
    if emission_col is not None:
        tree.links.new(hi_mix.outputs["Color"], emission_col)
    strength = bsdf.inputs.get("Emission Strength")
    if strength is not None:
        tree.links.new(glow_attr.outputs["Fac"], strength)
    alpha_sock = bsdf.inputs.get("Alpha")
    if alpha_sock is not None:
        tree.links.new(alpha_attr.outputs["Fac"], alpha_sock)

    _set_input(bsdf, ("Roughness",), 0.30)
    _set_input(bsdf, ("Metallic",), 0.0)
    _set_input(bsdf, ("IOR",), 1.46)
    _set_input(bsdf, ("Specular IOR Level", "Specular"), 0.5)
    _set_input(bsdf, ("Diffuse Roughness",), 0.15)
    _set_input(bsdf, ("Coat Weight",), 0.26)
    _set_input(bsdf, ("Coat Roughness",), 0.06)
    _set_input(bsdf, ("Coat IOR",), 1.50)
    _set_input(bsdf, ("Emission Strength",), 0.0)
    # Opaque solid look.  Blender 5.x removed OPAQUE from
    # ``surface_render_method`` (only DITHERED / BLENDED are offered), and
    # DITHERED with alpha == 1 *is* the opaque path: the fragment writes depth
    # and therefore sorts correctly.  The legacy ``blend_method`` is still set
    # for 4.x builds, where it drives the pipeline instead.  The previous
    # BLENDED + use_transparency_overlap combination is what made bonds and
    # atoms look like translucent jelly with white blooming rims.
    for attr, value in (
        ("blend_method", "OPAQUE"),
        ("surface_render_method", "DITHERED"),
        ("use_transparency_overlap", False),
        ("use_transparent_shadow", False),
    ):
        if hasattr(mat, attr):
            try:
                setattr(mat, attr, value)
            except Exception:
                pass
    mat["dsh_polycrystal_built"] = SURFACE_MATERIAL_VERSION
    mat["matcap_note"] = "attribute driven surface with clear coat"
    return mat


def _ensure_cell_material():
    """Amber wireframe material for the standalone unit-cell object.

    The object itself is flagged ``visible_shadow = False``; this material
    additionally keeps the cage faintly self-lit so the 12 thin tubes stay
    readable in the dark studio world without spilling amber onto the crystal.
    """
    mat = bpy.data.materials.get(CELL_MATERIAL)
    if mat is None:
        mat = bpy.data.materials.new(CELL_MATERIAL)
    elif mat.get("dsh_polycrystal_built") == CELL_MATERIAL_VERSION:
        return mat
    _tree, bsdf = _new_principled(mat)
    amber = (0.94, 0.62, 0.14, 1.0)
    _set_input(bsdf, ("Base Color",), amber)
    _set_input(bsdf, ("Roughness",), 0.38)
    _set_input(bsdf, ("Metallic",), 0.0)
    _set_input(bsdf, ("Specular IOR Level", "Specular"), 0.3)
    _set_input(bsdf, ("Emission Color", "Emission"), amber)
    _set_input(bsdf, ("Emission Strength",), 0.20)
    for attr, value in (
        ("blend_method", "OPAQUE"),
        ("surface_render_method", "DITHERED"),
    ):
        if hasattr(mat, attr):
            try:
                setattr(mat, attr, value)
            except Exception:
                pass
    mat["dsh_polycrystal_built"] = CELL_MATERIAL_VERSION
    return mat


def ensure_materials():
    """Create / refresh the attribute-driven surface and the cell material."""
    return {"surface": _ensure_surface_material(), "cell": _ensure_cell_material()}


# --------------------------------------------------------------------------- #
# Node building helpers
# --------------------------------------------------------------------------- #

def _socket_out(node, name=None, index=0):
    if name is not None:
        sock = node.outputs.get(name)
        if sock is not None:
            return sock
    return node.outputs[index]


class _Builder:
    def __init__(self, tree):
        self.tree = tree
        self.nodes = tree.nodes
        self.links = tree.links

    # -- primitives ----------------------------------------------------- #
    def add(self, idname, x=0.0, y=0.0, **kwargs):
        node = self.nodes.new(idname)
        node.location = (x, y)
        for key, value in kwargs.items():
            try:
                setattr(node, key, value)
            except Exception:
                pass
        return node

    def link(self, out_socket, in_socket):
        if out_socket is None or in_socket is None:
            return
        try:
            self.links.new(out_socket, in_socket)
        except Exception:
            pass

    def feed(self, sock, value):
        if sock is None or value is None:
            return
        if isinstance(value, bpy.types.NodeSocket):
            self.link(value, sock)
            return
        try:
            sock.default_value = value
        except Exception:
            pass

    # -- maths / fields ------------------------------------------------- #
    def math(self, operation, a=None, b=None, c=None, x=0.0, y=0.0):
        node = self.add("ShaderNodeMath", x, y, operation=operation)
        for index, value in enumerate((a, b, c)):
            if value is not None:
                self.feed(node.inputs[index], value)
        return node.outputs[0]

    def attr(self, name, x=0.0, y=0.0, data_type="FLOAT"):
        """Read a named attribute.

        ``data_type`` matters: reading a FLOAT_COLOR attribute through a FLOAT
        lookup returns its luminance, which silently flattens every colour to
        a grey shade.
        """
        node = self.add("GeometryNodeInputNamedAttribute", x, y)
        try:
            node.data_type = data_type
        except Exception:
            pass
        name_sock = node.inputs.get("Name")
        if name_sock is not None:
            name_sock.default_value = name
        return _socket_out(node, "Attribute")

    def gt(self, value, threshold, x=0.0, y=0.0):
        return self.math("GREATER_THAN", value, threshold, x=x, y=y)

    def lt(self, value, threshold, x=0.0, y=0.0):
        return self.math("LESS_THAN", value, threshold, x=x, y=y)

    def not_(self, value, x=0.0, y=0.0):
        return self.math("SUBTRACT", 1.0, value, x=x, y=y)

    def eq_int(self, value, target, x=0.0, y=0.0):
        diff = self.math("SUBTRACT", value, float(target), x=x, y=y)
        abs_diff = self.math("ABSOLUTE", diff, x=x, y=y - 160)
        return self.math("LESS_THAN", abs_diff, 0.5, x=x + 160, y=y - 160)

    def bool_float(self, condition, x=0.0, y=0.0):
        """Explicit boolean -> float conversion via a Switch node."""
        return self.switch("FLOAT", condition, 0.0, 1.0, x, y)

    def index_float(self, index_socket, values, x=0.0, y=0.0):
        """Explicit int -> float conversion via an Index Switch node."""
        node = self.add("GeometryNodeIndexSwitch", x, y)
        try:
            node.data_type = "FLOAT"
        except Exception:
            pass
        while len(node.index_switch_items) < len(values):
            node.index_switch_items.new()
        self.feed(node.inputs[0], index_socket)
        for offset, value in enumerate(values):
            self.feed(node.inputs[offset + 1], value)
        return node.outputs[0]

    def switch(self, kind, condition, false_value, true_value, x=0.0, y=0.0):
        node = self.add("GeometryNodeSwitch", x, y, input_type=kind)
        self.feed(node.inputs[0], condition)
        self.feed(node.inputs[1], false_value)
        self.feed(node.inputs[2], true_value)
        return node.outputs[0]

    # -- colour ramps --------------------------------------------------- #
    def ramp(self, stops, input_socket, name="Ramp", x=0.0, y=0.0):
        node = self.add("ShaderNodeValToRGB", x, y)
        node.name = name
        node.label = name
        ramp = node.color_ramp
        ramp.interpolation = "CONSTANT"
        ordered = sorted(stops, key=lambda item: item[0])
        first_pos, first_col = ordered[0]
        last_pos, last_col = ordered[-1]
        ramp.elements[0].position = first_pos
        ramp.elements[0].color = first_col
        ramp.elements[1].position = last_pos
        ramp.elements[1].color = last_col
        for pos, col in ordered[1:-1]:
            element = ramp.elements.new(pos)
            element.color = col
        self.link(input_socket, node.inputs["Fac"])
        return node.outputs["Color"]

    # -- geometry ------------------------------------------------------- #
    def separate(self, geometry, selection, domain, x=0.0, y=0.0):
        node = self.add("GeometryNodeSeparateGeometry", x, y)
        for attr, value in (("domain", domain), ("mode", "ALL")):
            try:
                setattr(node, attr, value)
            except Exception:
                pass
        self.link(geometry, node.inputs["Geometry"])
        self.link(selection, node.inputs["Selection"])
        return _socket_out(node, "Selection", 0)

    def delete(self, geometry, selection, domain, x=0.0, y=0.0):
        node = self.add("GeometryNodeDeleteGeometry", x, y)
        for attr, value in (("domain", domain), ("mode", "ALL")):
            try:
                setattr(node, attr, value)
            except Exception:
                pass
        self.link(geometry, node.inputs["Geometry"])
        self.link(selection, node.inputs["Selection"])
        return _socket_out(node, "Geometry", 0)

    def join(self, geos, x=0.0, y=0.0):
        node = self.add("GeometryNodeJoinGeometry", x, y)
        for geo in geos:
            self.link(geo, node.inputs[0])
        return node.outputs[0]

    def store(self, geometry, name, value, data_type="FLOAT", x=0.0, y=0.0, domain="POINT"):
        """Store a named attribute; accepts constants or field sockets."""
        node = self.add(
            "GeometryNodeStoreNamedAttribute", x, y, data_type=data_type
        )
        try:
            node.domain = domain
        except Exception:
            pass
        self.link(geometry, node.inputs["Geometry"])
        self.feed(node.inputs.get("Name"), name)
        self.feed(node.inputs.get("Value"), value)
        return _socket_out(node, "Geometry", 0)

    def tube(self, geometry, radius, resolution, x=0.0, y=0.0):
        """Turn line segments into round tubes.

        The thickness has to live on the profile circle: in Blender 5.x
        ``Curve to Mesh`` ignores the per-point curve radius, so a
        ``Set Curve Radius`` of 0.04 still produced a 1.0 Å tube. Every tube
        built that way came out one angstrom thick, which is what turned the
        unit cell into a fat cage.
        """
        to_curve = self.add("GeometryNodeMeshToCurve", x, y)
        self.link(geometry, to_curve.inputs["Mesh"])
        profile = self.add("GeometryNodeCurvePrimitiveCircle", x + 180, y - 220)
        self.feed(profile.inputs["Resolution"], resolution)
        self.feed(profile.inputs["Radius"], radius)
        to_mesh = self.add("GeometryNodeCurveToMesh", x + 400, y)
        self.link(_socket_out(to_curve, "Curve", 0), to_mesh.inputs["Curve"])
        self.link(_socket_out(profile, "Curve", 0), to_mesh.inputs["Profile Curve"])
        self.feed(to_mesh.inputs.get("Fill Caps"), True)
        return _socket_out(to_mesh, "Mesh", 0)


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #

_INPUTS = (
    ("Style Mode", "NodeSocketInt", 0, 0, 3),
    ("Atom Scale", "NodeSocketFloat", 0.36, 0.001, 3.0),
    ("Bond Radius", "NodeSocketFloat", 0.15, 0.001, 1.0),
    ("Hide Hydrogens", "NodeSocketBool", False, None, None),
    # Ghost pad atoms are a PBC bookkeeping artifact, not part of the model:
    # they start hidden (matches props.show_ghost_atoms) and the N-panel can
    # still reveal them.
    ("Show Ghost Boundary Atoms", "NodeSocketBool", False, None, None),
    ("Show Half-Bond Caps", "NodeSocketBool", True, None, None),
    ("Show Backbone Tube", "NodeSocketBool", False, None, None),
    ("Color Scheme", "NodeSocketInt", 0, 0, 3),
    ("Active Disorder Group", "NodeSocketInt", 0, 0, 2),
    ("Show Unit Cell Box", "NodeSocketBool", True, None, None),
    #: 3 = icosphere with 320 faces per atom (80 before).  Two levels higher
    #: than the old default and still cheap: 43 atoms -> ~14k faces realized.
    ("Subdivision Quality", "NodeSocketInt", 3, 1, 4),
)


def _find_interface_item(tree, name, in_out):
    for item in tree.interface.items_tree:
        if getattr(item, "in_out", None) == in_out and item.name == name:
            return item
    return None


def _ensure_interface(tree):
    if _find_interface_item(tree, "Geometry", "INPUT") is None:
        tree.interface.new_socket(
            name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry"
        )
    if _find_interface_item(tree, "Geometry", "OUTPUT") is None:
        tree.interface.new_socket(
            name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry"
        )
    for name, socket_type, default, minv, maxv in _INPUTS:
        item = _find_interface_item(tree, name, "INPUT")
        if item is None:
            item = tree.interface.new_socket(
                name=name, in_out="INPUT", socket_type=socket_type
            )
        try:
            if default is not None:
                item.default_value = default
        except Exception:
            pass
        if minv is not None:
            try:
                item.min_value = minv
            except Exception:
                pass
        if maxv is not None:
            try:
                item.max_value = maxv
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Master tree
# --------------------------------------------------------------------------- #

def _build_master(tree, materials):
    b = _Builder(tree)
    gi = b.add("NodeGroupInput", -1600, 0)
    go = b.add("NodeGroupOutput", 3000, 0)

    geo_in = _socket_out(gi, "Geometry", 0)
    style = _socket_out(gi, "Style Mode", 0)
    atom_scale = _socket_out(gi, "Atom Scale", 0)
    bond_radius = _socket_out(gi, "Bond Radius", 0)
    hide_h = _socket_out(gi, "Hide Hydrogens", 0)
    show_ghost = _socket_out(gi, "Show Ghost Boundary Atoms", 0)
    show_half = _socket_out(gi, "Show Half-Bond Caps", 0)
    show_bb = _socket_out(gi, "Show Backbone Tube", 0)
    scheme = _socket_out(gi, "Color Scheme", 0)
    disorder_mode = _socket_out(gi, "Active Disorder Group", 0)
    show_cell = _socket_out(gi, "Show Unit Cell Box", 0)
    subdivision = _socket_out(gi, "Subdivision Quality", 0)

    # Blender does not implicitly convert BOOLEAN / INT group-input sockets into
    # float maths fields, so convert them explicitly (verified on Blender 5.3).
    style_f = b.index_float(style, [0.0, 1.0, 2.0, 3.0], -1450, 380)
    scheme_f = b.index_float(scheme, [0.0, 1.0, 2.0, 3.0], -1450, 160)
    disorder_f = b.index_float(disorder_mode, [0.0, 1.0, 2.0, 2.0], -1450, -60)
    hide_h_f = b.bool_float(hide_h, -1450, -280)
    show_ghost_f = b.bool_float(show_ghost, -1450, -400)
    show_half_f = b.bool_float(show_half, -1450, -520)
    show_bb_f = b.bool_float(show_bb, -1450, -640)
    show_cell_f = b.bool_float(show_cell, -1450, -760)

    # ------------------------------------------------------------------ #
    # Atom island: point separation + all point filters
    # ------------------------------------------------------------------ #
    atoms = b.separate(
        geo_in, b.gt(b.attr("is_atom_pt_f"), 0.5), "POINT", -1350, 900
    )
    atoms = b.delete(
        atoms,
        b.math("MULTIPLY", hide_h_f, b.attr("is_hydrogen_f")),
        "POINT",
        -1150,
        900,
    )
    atoms = b.delete(
        atoms,
        b.math("MULTIPLY", b.not_(show_ghost_f), b.attr("is_ghost_f")),
        "POINT",
        -950,
        900,
    )

    # disorder filter: 0 -> keep groups 0/1, 1 -> keep 0/2, 2 -> keep all
    dg = b.attr("disorder_group_f")
    keep_group1 = b.lt(dg, 1.5)
    is_group0 = b.lt(dg, 0.5)
    is_group2 = b.math(
        "MULTIPLY", b.gt(dg, 1.5), b.lt(dg, 2.5)
    )
    keep_0_or_2 = b.math("MAXIMUM", is_group0, is_group2)
    active_is_0 = b.lt(disorder_f, 0.5)
    active_is_1 = b.math(
        "MULTIPLY", b.gt(disorder_f, 0.5), b.lt(disorder_f, 1.5)
    )
    active_is_2 = b.gt(disorder_f, 1.5)
    keep = b.math(
        "ADD",
        b.math("MULTIPLY", keep_group1, active_is_0),
        b.math("MULTIPLY", keep_0_or_2, active_is_1),
        x=-750,
        y=700,
    )
    keep = b.math("ADD", keep, active_is_2, x=-600, y=700)
    delete_disorder = b.math("SUBTRACT", 1.0, keep, x=-450, y=700)
    atoms = b.delete(atoms, delete_disorder, "POINT", -750, 1000)

    # ------------------------------------------------------------------ #
    # Atom radius by style mode
    # ------------------------------------------------------------------ #
    vdw = b.attr("vdw_radius")
    cov = b.attr("cov_radius")
    radius_ball = b.math("MULTIPLY", vdw, atom_scale, x=-800, y=420)
    radius_space = vdw
    radius_lico = b.math("MULTIPLY", bond_radius, 1.15, x=-800, y=250)
    radius_tube = b.math("MULTIPLY", cov, atom_scale, x=-800, y=140)
    radius = b.switch(
        "FLOAT", b.eq_int(style_f, 1, x=-1100, y=520), radius_ball, radius_space,
        -560, 420,
    )
    radius = b.switch(
        "FLOAT", b.eq_int(style_f, 2, x=-1100, y=340), radius, radius_lico, -380, 420
    )
    radius = b.switch(
        "FLOAT", b.eq_int(style_f, 3, x=-1100, y=160), radius, radius_tube, -200, 420
    )

    # ------------------------------------------------------------------ #
    # Colour generation (CPK / chain / backbone / disorder)
    # ------------------------------------------------------------------ #
    cpk_stops = [(0.0, (0.72, 0.72, 0.76, 1.0))]
    for z_value, colour in sorted(CPK_COLORS.items()):
        cpk_stops.append((min(z_value / 20.0, 0.95), colour))
    cpk_stops.append((1.0, (0.72, 0.72, 0.76, 1.0)))
    cpk_t = b.math(
        "DIVIDE", b.attr("atomic_number_f"), 20.0, x=-800, y=-60
    )
    cpk_col = b.ramp(cpk_stops, cpk_t, "CPK_Ramp", x=-560, y=-60)

    chain_stops = [
        (index / 8.0, colour) for index, colour in enumerate(CHAIN_PALETTE)
    ]
    chain_index = b.math(
        "MODULO", b.attr("chain_id_f"), 8.0, x=-900, y=-320
    )
    chain_t = b.math(
        "DIVIDE",
        b.math("ADD", chain_index, 0.5, x=-740, y=-320),
        8.0,
        x=-580,
        y=-320,
    )
    chain_col = b.ramp(chain_stops, chain_t, "Chain_Ramp", x=-380, y=-320)

    backbone_mix = b.switch(
        "RGBA",
        b.gt(b.attr("is_backbone_f"), 0.5),
        (0.86, 0.78, 0.58, 1.0),
        (0.05, 0.58, 0.68, 1.0),
        -380,
        -520,
    )

    disorder_stops = [
        (index * 0.25, colour) for index, colour in enumerate(DISORDER_PALETTE)
    ]
    disorder_t = b.math(
        "ADD",
        b.math("DIVIDE", b.attr("disorder_group_f"), 4.0, x=-900, y=-660),
        0.05,
        x=-740,
        y=-660,
    )
    disorder_col = b.ramp(
        disorder_stops, disorder_t, "Disorder_Ramp", x=-560, y=-660
    )

    colour = b.switch(
        "RGBA", b.gt(scheme_f, 0.5), cpk_col, chain_col, -200, -220
    )
    colour = b.switch(
        "RGBA", b.gt(scheme_f, 1.5), colour, backbone_mix, 0, -220
    )
    colour = b.switch(
        "RGBA", b.gt(scheme_f, 2.5), colour, disorder_col, 200, -220
    )

    atoms = b.store(atoms, "col", colour, "FLOAT_COLOR", 420, 780)
    atoms = b.store(atoms, "alpha", 1.0, "FLOAT", 620, 780)
    atoms = b.store(atoms, "glow", 0.0, "FLOAT", 820, 780)

    # ------------------------------------------------------------------ #
    # Atom instancing (procedural icosphere)
    # ------------------------------------------------------------------ #
    ico = b.add("GeometryNodeMeshIcoSphere", 420, 520)
    b.feed(ico.inputs.get("Radius"), 1.0)
    b.link(subdivision, ico.inputs.get("Subdivisions"))

    instancer = b.add("GeometryNodeInstanceOnPoints", 840, 620)
    b.link(atoms, instancer.inputs["Points"])
    b.link(_socket_out(ico, "Mesh", 0), instancer.inputs["Instance"])
    scale_vec = b.add("ShaderNodeCombineXYZ", 840, 420)
    for axis in ("X", "Y", "Z"):
        b.link(radius, scale_vec.inputs[axis])
    b.link(scale_vec.outputs["Vector"], instancer.inputs.get("Scale"))
    b.feed(instancer.inputs.get("Selection"), True)

    # The per-atom fields must live on the INSTANCE domain: Realize Instances
    # keeps instance attributes but silently drops point attributes that were
    # only carried along by the placement points.  Without this every sphere
    # inherits one flat colour.
    instances = _socket_out(instancer, "Instances", 0)
    instances = b.store(
        instances, "col", colour, "FLOAT_COLOR", 900, 900, domain="INSTANCE"
    )
    instances = b.store(instances, "alpha", 1.0, "FLOAT", 1000, 900, domain="INSTANCE")
    instances = b.store(instances, "glow", 0.0, "FLOAT", 1100, 900, domain="INSTANCE")
    instances = b.store(
        instances,
        "is_ghost_f",
        b.attr("is_ghost_f"),
        "FLOAT",
        1200,
        900,
        domain="INSTANCE",
    )

    realize = b.add("GeometryNodeRealizeInstances", 1060, 620)
    b.link(instances, realize.inputs["Geometry"])
    atom_geo = _socket_out(realize, "Geometry", 0)

    # Ghost boundary atoms: translucent cyan, no glow.
    ghost_sel = b.gt(b.attr("is_ghost_f"), 0.5, x=1060, y=380)
    ghost_col = b.switch(
        "RGBA",
        ghost_sel,
        b.attr("col", 1060, 250, data_type="FLOAT_COLOR"),
        (0.38, 0.74, 1.0, 1.0),
        1240,
        700,
    )
    atom_geo = b.store(atom_geo, "col", ghost_col, "FLOAT_COLOR", 1400, 700)
    ghost_alpha = b.switch("FLOAT", ghost_sel, 1.0, 0.4, 1240, 540)
    atom_geo = b.store(atom_geo, "alpha", ghost_alpha, "FLOAT", 1600, 700)
    # Per-island look-dev: atoms get a tight clear coat over a fairly glossy
    # diffuse base; the shared shader reads these two attributes directly.
    atom_geo = b.store(atom_geo, "rough", 0.30, "FLOAT", 1800, 700)
    atom_geo = b.store(atom_geo, "coat", 0.26, "FLOAT", 1950, 700)

    # ------------------------------------------------------------------ #
    # Bonds (internal + half-bond stubs)
    # ------------------------------------------------------------------ #
    bonds = b.separate(
        geo_in, b.gt(b.attr("is_bond_edge_f"), 0.5), "EDGE", -1350, -1000
    )
    hide_stub = b.math(
        "MULTIPLY", b.not_(show_half_f), b.attr("is_half_bond_f"), x=-1350, y=-1220
    )
    hide_h_bond = b.math("MULTIPLY", hide_h_f, b.attr("has_h_f"), x=-1350, y=-1340)
    hide_spacefill = b.eq_int(style_f, 1, x=-1350, y=-1460)
    bond_delete = b.math("MAXIMUM", hide_stub, hide_h_bond, x=-1150, y=-1280)
    bond_delete = b.math("MAXIMUM", bond_delete, hide_spacefill, x=-1000, y=-1280)
    bonds = b.delete(bonds, bond_delete, "EDGE", -850, -1100)
    bond_geo = b.tube(bonds, bond_radius, 12, -620, -1100)
    bond_geo = b.store(bond_geo, "col", (0.33, 0.34, 0.39, 1.0), "FLOAT_COLOR", -380, -1100)
    bond_geo = b.store(bond_geo, "alpha", 1.0, "FLOAT", -180, -1100)
    # Keep the glow attribute (the shader framework reads it) but at a value
    # that no longer lifts the bonds off the surface: everything <= 0.1.
    bond_geo = b.store(bond_geo, "glow", 0.0, "FLOAT", 20, -1100)
    # Matte, barely coated: bonds must not compete with the atom highlights.
    bond_geo = b.store(bond_geo, "rough", 0.40, "FLOAT", 200, -1100)
    bond_geo = b.store(bond_geo, "coat", 0.06, "FLOAT", 360, -1100)

    # ------------------------------------------------------------------ #
    # Backbone tube (segments stored as edges)
    # ------------------------------------------------------------------ #
    backbone = b.separate(
        geo_in, b.gt(b.attr("is_backbone_edge_f"), 0.5), "EDGE", -1350, -1800
    )
    backbone_keep = b.gt(show_bb_f, 0.5, x=-1350, y=-2020)
    backbone = b.delete(
        backbone, b.not_(backbone_keep), "EDGE", -1150, -1800
    )
    backbone_geo = b.tube(backbone, 0.25, 16, -900, -1800)
    backbone_geo = b.store(
        backbone_geo, "col", (0.06, 0.70, 0.86, 1.0), "FLOAT_COLOR", -660, -1800
    )
    backbone_geo = b.store(backbone_geo, "alpha", 1.0, "FLOAT", -460, -1800)
    backbone_geo = b.store(backbone_geo, "glow", 0.05, "FLOAT", -260, -1800)
    backbone_geo = b.store(backbone_geo, "rough", 0.34, "FLOAT", -80, -1800)
    backbone_geo = b.store(backbone_geo, "coat", 0.16, "FLOAT", 100, -1800)

    # ------------------------------------------------------------------ #
    # Unit cell wireframe
    # ------------------------------------------------------------------ #
    cell = b.separate(
        geo_in, b.gt(b.attr("is_cell_edge_f"), 0.5), "EDGE", -1350, -2500
    )
    cell_keep = b.gt(show_cell_f, 0.5, x=-1350, y=-2720)
    cell = b.delete(cell, b.not_(cell_keep), "EDGE", -1150, -2500)
    cell_geo = b.tube(cell, 0.04, 6, -900, -2500)
    cell_geo = b.store(
        cell_geo, "col", (0.95, 0.74, 0.28, 1.0), "FLOAT_COLOR", -660, -2500
    )
    cell_geo = b.store(cell_geo, "alpha", 1.0, "FLOAT", -460, -2500)
    # A hot emissive cage bleeds amber over the whole crystal through EEVEE's
    # screen-space GI, so the box stays only faintly self-lit (glow <= 0.1).
    cell_geo = b.store(cell_geo, "glow", 0.02, "FLOAT", -260, -2500)

    # ------------------------------------------------------------------ #
    # Output
    #
    # The same tree runs on two objects: ``PolyCrystal_Crystal`` (atoms, bonds,
    # backbone) and ``PolyCrystal_UnitCell`` (only the 12 cell edges, with
    # ``visible_shadow = False``).  Each object's mesh only carries the islands
    # it owns, so the other branches simply evaluate to empty geometry and the
    # shared N-panel sockets keep driving both.  The cell branch is shaded with
    # its own material *before* the join so the final Set Material on the body
    # cannot overwrite it.
    # ------------------------------------------------------------------ #
    surface = materials.get("surface") if isinstance(materials, dict) else None
    cell_material = materials.get("cell") if isinstance(materials, dict) else None

    def smooth_and_assign(geometry, material, x, y):
        node = b.add("GeometryNodeSetShadeSmooth", x, y)
        b.feed(node.inputs.get("Shade Smooth"), True)
        b.link(geometry, node.inputs["Geometry"])
        if material is None:
            return _socket_out(node, "Geometry", 0)
        assign = b.add("GeometryNodeSetMaterial", x + 220, y)
        assign.inputs["Material"].default_value = material
        b.link(_socket_out(node, "Geometry", 0), assign.inputs["Geometry"])
        return _socket_out(assign, "Geometry", 0)

    body = b.join([atom_geo, bond_geo, backbone_geo], 2200, 300)
    body = smooth_and_assign(body, surface, 2400, 300)
    cell = smooth_and_assign(cell_geo, cell_material, 2400, -2400)
    merged = b.join([body, cell], 2900, 0)
    b.link(merged, go.inputs[0])


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def is_group_current(tree):
    if tree is None or tree.bl_idname != "GeometryNodeTree":
        return False
    try:
        return int(tree.get("dsh_polycrystal_version", -1)) == GROUP_VERSION
    except Exception:
        return False


def ensure_master_group(force=False):
    """Return the master node group, rebuilding it when stale or missing."""
    materials = ensure_materials()
    tree = bpy.data.node_groups.get(GROUP_NAME)
    if tree is not None and not force and is_group_current(tree):
        return tree
    if tree is None:
        tree = bpy.data.node_groups.new(GROUP_NAME, "GeometryNodeTree")
    else:
        tree.nodes.clear()
    _ensure_interface(tree)
    _build_master(tree, materials)
    tree["dsh_polycrystal_version"] = str(GROUP_VERSION)
    tree.use_fake_user = True
    return tree


def material_slots():
    """Ordered material list matching ``loader_npz`` object slots."""
    materials = ensure_materials()
    return [materials["surface"], materials["cell"]]


def socket_names():
    """Names of the group inputs (used by smoke tests and diagnostics)."""
    tree = bpy.data.node_groups.get(GROUP_NAME)
    if tree is None:
        return []
    return [
        item.name
        for item in tree.interface.items_tree
        if getattr(item, "in_out", None) == "INPUT"
        and getattr(item, "item_type", "SOCKET") == "SOCKET"
    ]
