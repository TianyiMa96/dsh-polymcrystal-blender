# SPDX-License-Identifier: GPL-3.0-or-later
"""Miller (hkl) lamellar slab generation.

Primary path uses pymatgen's SlabGenerator. When pymatgen is unavailable a
self-contained oriented-cut fallback selects a slab of the requested thickness
along the reciprocal-plane normal while preserving in-plane periodicity.
"""
from __future__ import annotations

import numpy as np

from .structure import Structure, z_to_symbol


def _fallback_slab(structure, hkl, thickness):
    cell = structure.cell
    frac = structure.frac_coords
    hkl = np.asarray(hkl, dtype=float)
    recip = np.linalg.inv(cell)          # rows: a*, b*, c* (1/Angstrom)
    normal = hkl @ recip                 # Cartesian plane normal
    norm = np.linalg.norm(normal)
    if norm < 1e-9:
        raise ValueError("Invalid (hkl): zero plane normal")
    # Dimensionless plane coordinate t = frac . hkl ; distance = t / |normal|.
    t = frac @ hkl
    t = (t + 0.5) % 1.0 - 0.5            # wrap to [-0.5, 0.5)
    dist = t / norm
    keep = np.abs(dist) <= thickness / 2.0
    if not keep.any():
        keep[np.argmin(np.abs(dist))] = True
    return Structure(
        cell=cell.copy(),
        atomic_numbers=structure.atomic_numbers[keep],
        frac_coords=frac[keep] % 1.0,
        occupancies=structure.occupancies[keep],
        disorder_groups=structure.disorder_groups[keep],
        spacegroup_symbol="P1", source=structure.source)


def generate_slab(structure: Structure, hkl=(0, 0, 1), thickness=15.0,
                  vacuum=15.0) -> Structure:
    """Cleave a (hkl) slab of the given thickness (Angstrom)."""
    hkl = tuple(int(x) for x in hkl)
    try:
        from pymatgen.core import Structure as PmgStructure, Lattice
        from pymatgen.core.surface import SlabGenerator
        lattice = Lattice(structure.cell)
        species = [z_to_symbol(int(z)) for z in structure.atomic_numbers]
        pmg = PmgStructure(lattice, species, structure.frac_coords)
        gen = SlabGenerator(pmg, miller_index=hkl, min_slab_size=thickness,
                            min_vacuum_size=vacuum, center_slab=True)
        slab = gen.get_slab()
        cell = np.array(slab.lattice.matrix)
        z = np.array([site.specie.Z for site in slab], int)
        frac = np.array([site.frac_coords for site in slab], float) % 1.0
        return Structure(cell=cell, atomic_numbers=z, frac_coords=frac,
                         occupancies=np.ones(len(z)),
                         disorder_groups=np.zeros(len(z), int),
                         spacegroup_symbol="P1", source=structure.source)
    except ImportError:
        return _fallback_slab(structure, hkl, thickness)