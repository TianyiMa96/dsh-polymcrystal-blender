# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 1 & 2: symmetry expansion, disorder filtering and supercell building.

Pure numpy. Space-group operation *strings* (``-x+1/2, y, -z``) are parsed with
a restricted evaluator so that asymmetric-unit expansion works even when no
crystallographic backend is installed. Looking up operations from a bare
Hermann-Mauguin symbol optionally uses gemmi / spglib / pymatgen.
"""
from __future__ import annotations

import ast
import re
import itertools

import numpy as np

from .structure import Structure

_SYMOP_RE = re.compile(r"^[xyz0-9+\-*/().,\s]+$", re.IGNORECASE)

# Allowlisted AST interpreter for symmetry-operation expressions. No eval /
# compile / exec is used: only arithmetic on x, y, z and numeric literals.
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd, ast.Load,
)


def _eval_symop_expr(expr: str, variables: dict) -> float:
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"Disallowed syntax in symmetry operation: {expr!r}")
        if isinstance(node, ast.Name) and node.id not in variables:
            raise ValueError(f"Unknown variable in symmetry operation: {node.id}")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise ValueError(f"Non-numeric constant in symmetry operation: {expr!r}")

    def rec(n):
        if isinstance(n, ast.Expression):
            return rec(n.body)
        if isinstance(n, ast.Constant):
            return float(n.value)
        if isinstance(n, ast.Name):
            return float(variables[n.id])
        if isinstance(n, ast.UnaryOp):
            v = rec(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp):
            a, b = rec(n.left), rec(n.right)
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            if isinstance(n.op, ast.Div):
                return a / b
        raise ValueError(f"Unsupported expression: {expr!r}")

    return rec(tree)


def cell_matrix(a, b, c, alpha, beta, gamma) -> np.ndarray:
    """Standard crystallographic matrix (rows = a, b, c), angles in degrees.

    a along x, b in the xy-plane, c general triclinic. ``cart = frac @ M``.
    """
    alpha, beta, gamma = np.radians([alpha, beta, gamma])
    ca, cb, cg = np.cos([alpha, beta, gamma])
    sg = np.sin(gamma)
    va = np.array([a, 0.0, 0.0])
    vb = np.array([b * cg, b * sg, 0.0])
    cx = c * cb
    cy = c * (ca - cb * cg) / sg
    cz = np.sqrt(max(c * c - cx * cx - cy * cy, 0.0))
    vc = np.array([cx, cy, cz])
    return np.array([va, vb, vc])


def parse_symop(op_str: str):
    """Parse ``x,y,z``-style operation into (rotation 3x3 int, translation 3)."""
    s = op_str.strip().strip("'\"")
    if not _SYMOP_RE.match(s):
        raise ValueError(f"Unsafe symmetry operation string: {op_str!r}")
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 3:
        raise ValueError(f"Symmetry operation must have 3 components: {op_str!r}")

    def eval_comp(expr, x, y, z):
        return _eval_symop_expr(expr, {"x": x, "y": y, "z": z})

    origin = np.array([eval_comp(p, 0.0, 0.0, 0.0) for p in parts])
    rot = np.zeros((3, 3))
    for j, e in enumerate(np.eye(3)):
        col = np.array([eval_comp(p, *e) for p in parts])
        rot[:, j] = col - origin
    rot = np.rint(rot).astype(int)
    return rot, origin


def ops_from_symbol(symbol: str):
    """Resolve a space-group symbol to a list of (rot, tran) via a backend."""
    try:
        import gemmi
        sg = gemmi.SpaceGroup(symbol)
        ops = []
        for op in sg.operations():
            rot = np.array(op.rot, dtype=float).reshape(3, 3) / op.DEN
            tran = np.array(op.tran, dtype=float) / op.DEN
            ops.append((np.rint(rot).astype(int), tran))
        return ops, sg.hall_number()
    except Exception:
        pass
    try:
        from pymatgen.symmetry.groups import SpaceGroup
        sg = SpaceGroup(symbol)
        ops = [(np.rint(o.rotation_matrix).astype(int), o.translation_vector)
               for o in sg.symmetry_ops]
        return ops, None
    except Exception:
        pass
    raise ImportError(
        f"Cannot resolve space-group symbol {symbol!r}: install gemmi or "
        "pymatgen, or provide explicit _symmetry_equiv_pos_as_xyz operations.")


def _mic_min_distance(s1, s2, cell):
    """Minimum-image distance (Angstrom) between two fractional points."""
    d = s2 - s1
    d -= np.round(d)
    best = np.linalg.norm(d @ cell)
    # Triclinic safety: scan the 27 neighbouring images.
    for sh in itertools.product((-1, 0, 1), repeat=3):
        v = (d + np.array(sh)) @ cell
        n = np.linalg.norm(v)
        if n < best:
            best = n
    return best


def expand_symmetry(frac, atomic_numbers, occupancies, disorder_groups,
                    cell, ops, eps_sym=1e-3):
    """Expand an asymmetric unit and deduplicate overlaps within eps_sym (A).

    ``ops`` is a list of (rot, tran). Returns (frac, atomic_numbers,
    occupancies, disorder_groups) as deduplicated arrays.
    """
    frac = np.asarray(frac, dtype=float).reshape(-1, 3)
    atomic_numbers = np.asarray(atomic_numbers, dtype=int).reshape(-1)
    occupancies = np.asarray(occupancies, dtype=float).reshape(-1)
    disorder_groups = np.asarray(disorder_groups, dtype=int).reshape(-1)
    cell = np.asarray(cell, dtype=float).reshape(3, 3)

    out_f, out_z, out_o, out_g = [], [], [], []
    for i in range(len(frac)):
        for rot, tran in ops:
            s = (rot @ frac[i] + tran) % 1.0
            dup = False
            for k, sf in enumerate(out_f):
                if out_z[k] != atomic_numbers[i]:
                    continue
                if _mic_min_distance(s, sf, cell) < eps_sym:
                    dup = True
                    break
            if not dup:
                out_f.append(s)
                out_z.append(atomic_numbers[i])
                out_o.append(occupancies[i])
                out_g.append(disorder_groups[i])
    if not out_f:
        return (np.zeros((0, 3)), np.zeros(0, int),
                np.zeros(0), np.zeros(0, int))
    return (np.array(out_f), np.array(out_z, int),
            np.array(out_o), np.array(out_g, int))


def filter_disorder(disorder_groups, mode="default"):
    """Return a boolean mask of sites kept under the disorder filter.

    default / group_1 -> keep {0, 1}; group_2 -> {0, 2}; all -> everything;
    ordered / group_0 -> {0}; group_N -> {0, N}.
    """
    g = np.asarray(disorder_groups, dtype=int)
    m = (mode or "default").strip().lower()
    if m in ("all", "both", "superimposed"):
        return np.ones(len(g), dtype=bool)
    if m in ("ordered", "0", "group_0", "none"):
        return g == 0
    if m in ("default", "01", "group_1", "1"):
        keep = {0, 1}
    elif m in ("group_2", "2"):
        keep = {0, 2}
    elif m.startswith("group_"):
        keep = {0, int(m.split("_", 1)[1])}
    else:
        keep = {0, 1}
    return np.isin(g, list(keep))


def make_supercell(structure: Structure, na, nb, nc) -> Structure:
    """Stage 2: replicate into an Na x Nb x Nc supercell, frac in [0,1)."""
    na, nb, nc = int(na), int(nb), int(nc)
    if min(na, nb, nc) < 1:
        raise ValueError("Supercell multipliers must be positive integers")
    n = structure.n_atoms
    cell_sc = np.diag([na, nb, nc]).astype(float) @ structure.cell
    offsets = np.array(list(itertools.product(range(na), range(nb), range(nc))),
                       dtype=float)
    n_rep = len(offsets)
    frac = structure.frac_coords
    new_frac = ((frac[None, :, :] + offsets[:, None, :])
                / np.array([na, nb, nc])).reshape(-1, 3) % 1.0
    tile = lambda arr: np.tile(arr, n_rep)
    sc = Structure(
        cell=cell_sc,
        atomic_numbers=tile(structure.atomic_numbers),
        frac_coords=new_frac,
        occupancies=tile(structure.occupancies),
        disorder_groups=tile(structure.disorder_groups),
        spacegroup_symbol=structure.spacegroup_symbol,
        spacegroup_number=structure.spacegroup_number,
        hall_number=structure.hall_number,
        source=structure.source,
        labels=list(structure.labels) * n_rep,
    )
    if structure.explicit_bonds:
        idx = lambda atom, off: off * n + atom
        bonds = []
        for (u, v) in structure.explicit_bonds:
            ell = -np.round(frac[v] - frac[u]).astype(int)
            for oi, o in enumerate(offsets.astype(int)):
                ov = o + ell
                if (ov >= 0).all() and (ov < [na, nb, nc]).all():
                    oj = int(ov[0] * nb * nc + ov[1] * nc + ov[2])
                    bonds.append((idx(u, oi), idx(v, oj)))
        sc.explicit_bonds = bonds
    return sc