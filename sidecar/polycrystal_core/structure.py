# SPDX-License-Identifier: GPL-3.0-or-later
"""Core data structures shared by the sidecar engine.

The :class:`Structure` container is deliberately dependency-free (numpy only)
so that the periodic-topology and unwrapping algorithms never require
pymatgen/ASE/gemmi at runtime. Heavy crystallography backends are confined to
:mod:`polycrystal_core.io_parsers` and :mod:`polycrystal_core.hkl_slab`.

Lattice convention: ``cell`` is a (3, 3) matrix whose *rows* are the lattice
vectors a, b, c. Cartesian coordinates (Angstrom) are ``frac @ cell`` which is
equivalent to the crystallographic ``r = M s`` with ``M = cell.T``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from collections import Counter

import numpy as np

# Covalent radii (Cordero et al. 2008, Angstrom) indexed by atomic number.
COVALENT_RADII = {
    1: 0.31, 2: 0.28, 3: 1.28, 4: 0.96, 5: 0.84, 6: 0.76, 7: 0.71, 8: 0.66,
    9: 0.57, 10: 0.58, 11: 1.66, 12: 1.41, 13: 1.21, 14: 1.11, 15: 1.07,
    16: 1.05, 17: 1.02, 18: 1.06, 19: 2.03, 20: 1.76, 21: 1.70, 22: 1.60,
    23: 1.53, 24: 1.39, 25: 1.39, 26: 1.32, 27: 1.26, 28: 1.24, 29: 1.32,
    30: 1.22, 31: 1.22, 32: 1.20, 33: 1.19, 34: 1.20, 35: 1.20, 36: 1.16,
    37: 2.20, 38: 1.95, 39: 1.90, 40: 1.75, 41: 1.64, 42: 1.54, 43: 1.47,
    44: 1.46, 45: 1.42, 46: 1.39, 47: 1.45, 48: 1.44, 49: 1.42, 50: 1.39,
    51: 1.39, 52: 1.38, 53: 1.39, 54: 1.40, 55: 2.44, 56: 2.15, 57: 2.07,
    58: 2.04, 59: 2.03, 60: 2.01, 61: 1.99, 62: 1.98, 63: 1.98, 64: 1.96,
    65: 1.94, 66: 1.92, 67: 1.92, 68: 1.89, 69: 1.90, 70: 1.87, 71: 1.87,
    72: 1.75, 73: 1.70, 74: 1.62, 75: 1.51, 76: 1.44, 77: 1.41, 78: 1.36,
    79: 1.36, 80: 1.32, 81: 1.45, 82: 1.46, 83: 1.48, 84: 1.40, 85: 1.50,
    86: 1.50, 87: 2.60, 88: 2.21, 89: 2.15, 90: 2.06, 91: 2.00, 92: 1.96,
    93: 1.90, 94: 1.87, 95: 1.80, 96: 1.69,
}

# Van der Waals radii (Bondi / Alvarez, Angstrom).
VDW_RADII = {
    1: 1.20, 2: 1.40, 3: 1.82, 4: 1.53, 5: 1.92, 6: 1.70, 7: 1.55, 8: 1.52,
    9: 1.47, 10: 1.54, 11: 2.27, 12: 1.73, 13: 1.84, 14: 2.10, 15: 1.80,
    16: 1.80, 17: 1.75, 18: 1.88, 19: 2.75, 20: 2.31, 26: 2.04, 29: 1.40,
    30: 1.39, 35: 1.85, 53: 1.98,
}

ELEMENT_SYMBOLS = [
    "n", "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne", "Na", "Mg",
    "Al", "Si", "P", "S", "Cl", "Ar", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn",
    "Fe", "Co", "Ni", "Cu", "Zn", "Ga", "Ge", "As", "Se", "Br", "Kr", "Rb",
    "Sr", "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd", "In",
    "Sn", "Sb", "Te", "I", "Xe", "Cs", "Ba", "La", "Ce", "Pr", "Nd", "Pm",
    "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "Lu", "Hf", "Ta",
    "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi", "Po", "At",
    "Rn", "Fr", "Ra", "Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm",
]
_SYMBOL_TO_Z = {s.lower(): i for i, s in enumerate(ELEMENT_SYMBOLS) if i > 0}


def symbol_to_z(symbol: str) -> int:
    """Map an element symbol (possibly with a trailing label, e.g. 'C1') to Z."""
    s = symbol.strip()
    if s.isdigit():
        return int(s)
    # Strip numeric / underscore suffixes used as atom labels.
    core = ""
    for ch in s:
        if ch.isalpha():
            core += ch
        else:
            break
    z = _SYMBOL_TO_Z.get(core.lower())
    if z is None and len(core) >= 1:
        z = _SYMBOL_TO_Z.get(core[0].lower())
    if z is None:
        raise ValueError(f"Unknown element symbol: {symbol!r}")
    return z


def z_to_symbol(z: int) -> str:
    if 0 < z < len(ELEMENT_SYMBOLS):
        return ELEMENT_SYMBOLS[z]
    return "X"


def cov_radius(z: int) -> float:
    return COVALENT_RADII.get(int(z), 1.5)


def vdw_radius(z: int) -> float:
    return VDW_RADII.get(int(z), 1.70)


def reduced_formula(atomic_numbers) -> str:
    counts = Counter(int(z) for z in atomic_numbers)
    if not counts:
        return ""
    from math import gcd
    from functools import reduce
    g = reduce(gcd, counts.values())
    parts = []
    # Carbon first, hydrogen second, then alphabetical (Hill system).
    order = sorted(counts, key=lambda z: (z != 6, z != 1, z_to_symbol(z)))
    for z in order:
        n = counts[z] // g
        sym = z_to_symbol(z)
        parts.append(sym if n == 1 else f"{sym}{n}")
    return "".join(parts)


@dataclass
class Structure:
    """A periodic crystal structure in fractional coordinates."""

    cell: np.ndarray                       # (3,3) rows = a,b,c ; cart = frac @ cell
    atomic_numbers: np.ndarray             # (N,) int
    frac_coords: np.ndarray                # (N,3) in [0,1)
    occupancies: np.ndarray                # (N,) float
    disorder_groups: np.ndarray            # (N,) int, 0 = ordered
    spacegroup_symbol: str = "P1"
    spacegroup_number: int = 1
    hall_number: int = 1
    explicit_bonds: list | None = None     # optional [(i,j), ...] (LAMMPS)
    source: str = ""
    labels: list = field(default_factory=list)

    def __post_init__(self):
        self.cell = np.asarray(self.cell, dtype=float).reshape(3, 3)
        self.atomic_numbers = np.asarray(self.atomic_numbers, dtype=int).reshape(-1)
        self.frac_coords = np.asarray(self.frac_coords, dtype=float).reshape(-1, 3)
        n = len(self.atomic_numbers)
        if self.occupancies is None:
            self.occupancies = np.ones(n)
        self.occupancies = np.asarray(self.occupancies, dtype=float).reshape(-1)
        if self.disorder_groups is None:
            self.disorder_groups = np.zeros(n, dtype=int)
        self.disorder_groups = np.asarray(self.disorder_groups, dtype=int).reshape(-1)

    @property
    def n_atoms(self) -> int:
        return len(self.atomic_numbers)

    @property
    def cart_coords(self) -> np.ndarray:
        return self.frac_coords @ self.cell

    @property
    def formula(self) -> str:
        return reduced_formula(self.atomic_numbers)

    @property
    def lattice_parameters(self) -> tuple:
        a, b, c = (np.linalg.norm(self.cell[i]) for i in range(3))
        alpha = np.degrees(np.arccos(np.clip(
            np.dot(self.cell[1], self.cell[2]) / (b * c), -1, 1)))
        beta = np.degrees(np.arccos(np.clip(
            np.dot(self.cell[0], self.cell[2]) / (a * c), -1, 1)))
        gamma = np.degrees(np.arccos(np.clip(
            np.dot(self.cell[0], self.cell[1]) / (a * b), -1, 1)))
        return (float(a), float(b), float(c),
                float(alpha), float(beta), float(gamma))

    def copy(self) -> "Structure":
        return replace(
            self,
            cell=self.cell.copy(),
            atomic_numbers=self.atomic_numbers.copy(),
            frac_coords=self.frac_coords.copy(),
            occupancies=self.occupancies.copy(),
            disorder_groups=self.disorder_groups.copy(),
            explicit_bonds=list(self.explicit_bonds) if self.explicit_bonds else None,
            labels=list(self.labels),
        )