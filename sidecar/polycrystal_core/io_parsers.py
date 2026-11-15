# SPDX-License-Identifier: GPL-3.0-or-later
"""Input parsing: CIF (gemmi / pymatgen / pure-python fallback), POSCAR / XYZ /
PDB (ASE) and LAMMPS ``.data`` (self-contained parser with explicit bonds).

CIF parsing preserves ``_atom_site_occupancy`` and ``_atom_site_disorder_group``
which is essential for the disorder-aware bond graph. The pure-python fallback
means the test fixtures parse even when no heavy backend is installed.
"""
from __future__ import annotations

import os
import re

import numpy as np

from .structure import Structure, symbol_to_z, z_to_symbol
from . import symmetry


# --------------------------------------------------------------------------
# Minimal CIF tokenizer (fallback path, also used to read disorder columns).
# --------------------------------------------------------------------------
def _tokenize_cif(text):
    tokens = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(";"):
            buf = [line[1:]]
            i += 1
            while i < len(lines) and not lines[i].startswith(";"):
                buf.append(lines[i])
                i += 1
            tokens.append("\n".join(buf))
            i += 1
            continue
        j, n = 0, len(line)
        while j < n:
            ch = line[j]
            if ch == "#":
                break
            if ch.isspace():
                j += 1
                continue
            if ch in "'\"":
                q = ch
                j += 1
                start = j
                while j < n and line[j] != q:
                    j += 1
                tokens.append(line[start:j])
                j += 1
            else:
                start = j
                while j < n and not line[j].isspace() and line[j] != "#":
                    j += 1
                tokens.append(line[start:j])
        i += 1
    return tokens


def _parse_cif(text):
    """Return (pairs dict, loops list of (tags, rows)) for the first block."""
    toks = _tokenize_cif(text)
    pairs, loops = {}, []
    i = 0
    while i < len(toks):
        t = toks[i]
        low = t.lower()
        if low == "loop_":
            i += 1
            tags = []
            while i < len(toks) and toks[i].startswith("_"):
                tags.append(toks[i].lower())
                i += 1
            vals = []
            while i < len(toks) and not toks[i].startswith("_") \
                    and toks[i].lower() not in ("loop_",) \
                    and not toks[i].lower().startswith("data_"):
                vals.append(toks[i])
                i += 1
            if tags:
                rows = [vals[k:k + len(tags)] for k in range(0, len(vals), len(tags))]
                loops.append((tags, rows))
        elif t.startswith("_"):
            if i + 1 < len(toks):
                pairs[low] = toks[i + 1]
                i += 2
            else:
                i += 1
        else:
            i += 1
    return pairs, loops


def _cif_float(s, default=0.0):
    if s is None:
        return default
    s = str(s).strip()
    if s in ("?", ".", ""):
        return default
    s = re.sub(r"\(.*?\)", "", s)  # strip standard uncertainty
    try:
        return float(s)
    except ValueError:
        return default


def _cif_int(s, default=0):
    if s is None:
        return default
    s = str(s).strip()
    if s in ("?", ".", ""):
        return default
    try:
        return int(float(s))
    except ValueError:
        return default


def _element_labels(atomic_numbers):
    return [f"{z_to_symbol(int(z))}{i + 1}" for i, z in enumerate(atomic_numbers)]


def _atom_columns_from_loop(tags, rows):
    idx = {t: k for k, t in enumerate(tags)}

    def col(row, *names, default=None):
        for nm in names:
            if nm in idx:
                return row[idx[nm]]
        return default

    z_list, frac, occ, grp, labels = [], [], [], [], []
    for row in rows:
        if len(row) < len(tags):
            continue
        sym = col(row, "_atom_site_type_symbol", "_atom_site_label", default="C")
        z_list.append(symbol_to_z(sym))
        frac.append([
            _cif_float(col(row, "_atom_site_fract_x")),
            _cif_float(col(row, "_atom_site_fract_y")),
            _cif_float(col(row, "_atom_site_fract_z")),
        ])
        occ.append(_cif_float(col(row, "_atom_site_occupancy"), 1.0))
        grp.append(_cif_int(col(row, "_atom_site_disorder_group"), 0))
        labels.append(col(row, "_atom_site_label", default=sym))
    return (np.array(z_list, int), np.array(frac, float).reshape(-1, 3),
            np.array(occ, float), np.array(grp, int), labels)


def _read_cif_raw(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    pairs, loops = _parse_cif(text)
    a = _cif_float(pairs.get("_cell_length_a"))
    b = _cif_float(pairs.get("_cell_length_b"))
    c = _cif_float(pairs.get("_cell_length_c"))
    al = _cif_float(pairs.get("_cell_angle_alpha"), 90.0)
    be = _cif_float(pairs.get("_cell_angle_beta"), 90.0)
    ga = _cif_float(pairs.get("_cell_angle_gamma"), 90.0)
    cell = symmetry.cell_matrix(a, b, c, al, be, ga)
    symbol = (pairs.get("_symmetry_space_group_name_h-m")
              or pairs.get("_space_group_name_h-m_alt")
              or pairs.get("_symmetry_space_group_name_hall") or "P1")
    symbol = symbol.strip().strip("'\"")

    op_strings = None
    for tags, rows in loops:
        for tag in ("_symmetry_equiv_pos_as_xyz", "_space_group_symop_operation_xyz"):
            if tag in tags:
                ci = tags.index(tag)
                op_strings = [r[ci] for r in rows if len(r) > ci]
                break
        if op_strings:
            break

    atom_data = None
    for tags, rows in loops:
        if "_atom_site_fract_x" in tags:
            atom_data = _atom_columns_from_loop(tags, rows)
            break
    if atom_data is None:
        raise ValueError(f"No _atom_site_fract_x loop found in {path}")
    it_number = _cif_int(pairs.get("_symmetry_int_tables_number"), 1)
    return cell, symbol, op_strings, it_number, atom_data


def _ops_from_strings_or_symbol(op_strings, symbol):
    if op_strings:
        return [symmetry.parse_symop(s) for s in op_strings], None
    ops, hall = symmetry.ops_from_symbol(symbol)
    return ops, hall


def _read_cif_gemmi(path):
    import gemmi
    doc = gemmi.cif.read_file(str(path))
    block = doc.sole_block()

    def val(tag):
        v = block.find_value(tag)
        return v if v else None

    a = _cif_float(val("_cell_length_a"))
    b = _cif_float(val("_cell_length_b"))
    c = _cif_float(val("_cell_length_c"))
    al = _cif_float(val("_cell_angle_alpha"), 90.0)
    be = _cif_float(val("_cell_angle_beta"), 90.0)
    ga = _cif_float(val("_cell_angle_gamma"), 90.0)
    cell = symmetry.cell_matrix(a, b, c, al, be, ga)
    symbol = (val("_symmetry_space_group_name_H-M")
              or val("_space_group_name_H-M_alt") or "P1")
    symbol = str(symbol).strip().strip("'\"")

    ops, hall = None, None
    for tag in ("_symmetry_equiv_pos_as_xyz", "_space_group_symop_operation_xyz"):
        col = block.find_loop(tag)
        if col is not None:
            loop = col.get_loop()
            tags = [t.lower() for t in loop.tags]
            ci = tags.index(tag.lower())
            w, rows = loop.width(), loop.length()
            op_strings = [loop.values[r * w + ci] for r in range(rows)]
            ops = [symmetry.parse_symop(s) for s in op_strings]
            break
    if ops is None:
        try:
            sg = gemmi.SpaceGroup(symbol)
            hall = sg.hall_number()
            ops = []
            for op in sg.operations():
                rot = np.array(op.rot, dtype=float).reshape(3, 3) / op.DEN
                tran = np.array(op.tran, dtype=float) / op.DEN
                ops.append((np.rint(rot).astype(int), tran))
        except Exception:
            ops = [(np.eye(3, dtype=int), np.zeros(3))]

    col = block.find_loop("_atom_site_fract_x")
    if col is None:
        raise ValueError(f"No _atom_site_fract_x loop in {path}")
    loop = col.get_loop()
    tags = [t.lower() for t in loop.tags]
    w, rows = loop.width(), loop.length()
    flat = loop.values
    rows_list = [flat[r * w:(r + 1) * w] for r in range(rows)]
    atom_data = _atom_columns_from_loop(tags, rows_list)
    number = _cif_int(val("_symmetry_Int_Tables_number"), 0)
    if not number:
        try:
            number = gemmi.SpaceGroup(symbol).number()
        except Exception:
            number = 1
    if hall is None:
        try:
            hall = gemmi.SpaceGroup(symbol).hall_number()
        except Exception:
            hall = 1
    return cell, symbol, ops, hall, number, atom_data


def _read_cif_pymatgen(path):
    from pymatgen.io.cif import CifParser
    parser = CifParser(str(path), occupancy_tolerance=1.0, site_tolerance=1e-4)
    structs = parser.parse_structures(primitive=False)
    s = structs[0]
    cell = np.array(s.lattice.matrix)
    z, occ, grp = [], [], []
    for site in s:
        comp = site.species                      # Composition (handles disorder)
        dominant = max(comp, key=lambda el: comp[el])
        z.append(int(dominant.Z))
        occ.append(float(site.properties.get("occupancy", sum(comp.values()))))
        grp.append(int(site.properties.get("disorder_group", 0)))
    frac = np.array([site.frac_coords for site in s], float)
    # Prefer the declared CIF symbol over an re-analyzed one.
    symbol = "P1"
    try:
        pairs, _ = _parse_cif(open(str(path), encoding="utf-8", errors="replace").read())
        symbol = (pairs.get("_symmetry_space_group_name_h-m")
                  or pairs.get("_space_group_name_h-m_alt") or "P1").strip().strip("'\"")
    except Exception:
        pass
    return Structure(cell=cell, atomic_numbers=np.array(z, int),
                     frac_coords=frac % 1.0, occupancies=np.array(occ, float),
                     disorder_groups=np.array(grp, int),
                     spacegroup_symbol=symbol, source=str(path))


def parse_cif(path):
    """Parse a CIF into a symmetry-expanded Structure.

    Backend order: gemmi -> pure-python tokenizer -> pymatgen. The gemmi and
    pure-python paths preserve disorder groups and occupancies exactly.
    """
    errors = []
    try:
        cell, symbol, ops, hall, number, atom_data = _read_cif_gemmi(path)
        z, frac, occ, grp, labels = atom_data
        ff, fz, fo, fg = symmetry.expand_symmetry(frac, z, occ, grp, cell, ops)
        return Structure(cell=cell, atomic_numbers=fz, frac_coords=ff,
                         occupancies=fo, disorder_groups=fg,
                         spacegroup_symbol=symbol, spacegroup_number=number,
                         hall_number=hall or 1, source=str(path),
                         labels=_element_labels(fz))
    except ImportError as e:
        errors.append(f"gemmi: {e}")
    except Exception as e:
        errors.append(f"gemmi: {e}")

    # Pure-python fallback: faithful disorder/occupancy preservation, no deps.
    try:
        cell, symbol, op_strings, it_number, atom_data = _read_cif_raw(path)
        z, frac, occ, grp, labels = atom_data
        ops, hall = _ops_from_strings_or_symbol(op_strings, symbol)
        ff, fz, fo, fg = symmetry.expand_symmetry(frac, z, occ, grp, cell, ops)
        return Structure(cell=cell, atomic_numbers=fz, frac_coords=ff,
                         occupancies=fo, disorder_groups=fg,
                         spacegroup_symbol=symbol, spacegroup_number=it_number,
                         hall_number=hall or 1, source=str(path),
                         labels=_element_labels(fz))
    except Exception as e:
        errors.append(f"raw: {e}")

    try:
        return _read_cif_pymatgen(path)
    except Exception as e:
        errors.append(f"pymatgen: {e}")
        raise ImportError("No CIF backend available / parse failed: "
                          + "; ".join(errors))


# --------------------------------------------------------------------------
# ASE-backed formats: POSCAR / CONTCAR / .vasp / .xyz / .extxyz / .pdb
# --------------------------------------------------------------------------
def parse_ase(path):
    from ase.io import read
    atoms = read(str(path))
    cell = np.array(atoms.cell.array, dtype=float)
    if np.allclose(cell, 0):
        raise ValueError(f"{path}: no lattice (non-periodic file); "
                         "polymer PBC engine requires a periodic cell")
    frac = atoms.get_scaled_positions(wrap=True)
    z = np.array(atoms.numbers, dtype=int)
    return Structure(cell=cell, atomic_numbers=z, frac_coords=frac,
                     occupancies=np.ones(len(z)), disorder_groups=np.zeros(len(z), int),
                     source=str(path))


# --------------------------------------------------------------------------
# LAMMPS data (atom_style full / molecular) with explicit bond table.
# --------------------------------------------------------------------------
def _mass_to_z(mass):
    from .structure import ELEMENT_SYMBOLS
    # Approximate atomic masses for common elements.
    masses = {1: 1.008, 6: 12.011, 7: 14.007, 8: 15.999, 9: 18.998,
              14: 28.085, 16: 32.06, 17: 35.45, 11: 22.99, 12: 24.305,
              13: 26.982, 20: 40.078, 26: 55.845, 29: 63.546, 30: 65.38}
    best, bd = 6, 1e9
    for z, m in masses.items():
        d = abs(m - mass)
        if d < bd:
            bd, best = d, z
    return best


def parse_lammps_data(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    n_atoms = n_bonds = n_types = 0
    xlo = xhi = ylo = yhi = zlo = zhi = 0.0
    xy = xz = yz = 0.0
    masses = {}
    atoms_rows, bonds_rows = [], []
    section = None
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        low = s.lower()
        if low in ("atoms", "bonds", "masses", "angles", "dihedrals",
                   "impropers", "velocities", "bond coeffs", "pair coeffs"):
            section = low
            continue
        if "atoms" in low and "atom" in low and section is None:
            m = re.match(r"(\d+)\s+atoms", low)
            if m:
                n_atoms = int(m.group(1))
                continue
        m = re.match(r"(\d+)\s+bonds", low)
        if m:
            n_bonds = int(m.group(1))
            continue
        m = re.match(r"(\d+)\s+atom types", low)
        if m:
            n_types = int(m.group(1))
            continue
        m = re.match(r"([-\d.eE+]+)\s+([-\d.eE+]+)\s+xlo\s+xhi", low)
        if m:
            xlo, xhi = float(m.group(1)), float(m.group(2))
            continue
        m = re.match(r"([-\d.eE+]+)\s+([-\d.eE+]+)\s+ylo\s+yhi", low)
        if m:
            ylo, yhi = float(m.group(1)), float(m.group(2))
            continue
        m = re.match(r"([-\d.eE+]+)\s+([-\d.eE+]+)\s+zlo\s+zhi", low)
        if m:
            zlo, zhi = float(m.group(1)), float(m.group(2))
            continue
        m = re.match(r"([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+xy\s+xz\s+yz", low)
        if m:
            xy, xz, yz = (float(m.group(k)) for k in (1, 2, 3))
            continue
        if section == "masses":
            parts = s.split()
            if len(parts) >= 2:
                masses[int(parts[0])] = float(parts[1])
        elif section == "atoms":
            atoms_rows.append(s.split())
        elif section == "bonds":
            bonds_rows.append(s.split())

    a = np.array([xhi - xlo, 0.0, 0.0])
    b = np.array([xy, yhi - ylo, 0.0])
    c = np.array([xz, yz, zhi - zlo])
    cell = np.array([a, b, c])
    origin = np.array([xlo, ylo, zlo])
    inv = np.linalg.inv(cell)

    z_list, cart = [], []
    for parts in atoms_rows:
        # atom_style full: id mol type q x y z ; molecular: id mol type x y z
        if len(parts) >= 7:
            atype = int(parts[2])
            xyz = [float(parts[4]), float(parts[5]), float(parts[6])]
        elif len(parts) >= 6:
            atype = int(parts[2])
            xyz = [float(parts[3]), float(parts[4]), float(parts[5])]
        else:
            continue
        z_list.append(_mass_to_z(masses.get(atype, 12.011)))
        cart.append(xyz)
    cart = np.array(cart, float).reshape(-1, 3) - origin
    frac = (cart @ inv) % 1.0
    bonds = [(int(p[2]) - 1, int(p[3]) - 1) for p in bonds_rows if len(p) >= 4]
    return Structure(cell=cell, atomic_numbers=np.array(z_list, int),
                     frac_coords=frac, occupancies=np.ones(len(z_list)),
                     disorder_groups=np.zeros(len(z_list), int),
                     explicit_bonds=bonds, source=str(path))


def parse_structure(path):
    """Dispatch on filename / extension to the appropriate parser."""
    path = str(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Input structure not found: {path}")
    base = os.path.basename(path).lower()
    ext = os.path.splitext(base)[1]
    if ext == ".cif":
        return parse_cif(path)
    if base.startswith(("poscar", "contcar")) or ext in (".vasp",):
        return parse_ase(path)
    if ext in (".xyz", ".extxyz", ".pdb"):
        return parse_ase(path)
    if ext in (".data", ".lammps", ".lmp"):
        return parse_lammps_data(path)
    # Unknown extension: try ASE generically.
    return parse_ase(path)