# SPDX-License-Identifier: GPL-3.0-or-later
"""polycrystal_core: isolated polymer crystallography & PBC topology sidecar.

The core periodic-bond-graph / homology / unwrapping algorithms depend only on
numpy, scipy and networkx. Crystallographic I/O (gemmi / pymatgen / ASE) and
fast neighbor lists (vesin) are optional accelerators with graceful fallbacks.
"""
from __future__ import annotations

__version__ = "0.3.1"

from .structure import Structure  # noqa: F401