# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional interoperability with beautiful-atoms and MolecularNodes.

The extension never depends on these add-ons.  This module only *detects* them
so that the DSH host can report what is available and gracefully fall back to
the native Geometry Nodes pipeline.  When an optional backend is installed we
tag the crystal object with a couple of well-known custom properties that the
respective add-on can pick up; no private or fragile API calls are made.
"""

from __future__ import annotations

import bpy

NATIVE = "native_gn"
BATOMS = "batoms"
MOLECULARNODES = "molecularnodes"

_BATOMS_IDS = ("batoms", "beautiful_atoms", "beautiful-atoms")
_MOLECULARNODES_IDS = ("molecularnodes", "molecular_nodes", "MolecularNodes")


def _addon_ids():
    try:
        return {item.module for item in bpy.context.preferences.addons}
    except Exception:
        return set()


def _has_any(installed, candidates):
    lowered = {item.lower() for item in installed}
    return any(candidate.lower() in lowered for candidate in candidates)


def detect_backends():
    """Return a plain dict describing optional renderer availability."""
    installed = _addon_ids()
    batoms = _has_any(installed, _BATOMS_IDS)
    molecularnodes = _has_any(installed, _MOLECULARNODES_IDS)
    available = [NATIVE]
    if batoms:
        available.append(BATOMS)
    if molecularnodes:
        available.append(MOLECULARNODES)
    preferred = NATIVE
    if molecularnodes:
        preferred = MOLECULARNODES
    elif batoms:
        preferred = BATOMS
    return {
        "native_gn": True,
        "batoms": batoms,
        "molecularnodes": molecularnodes,
        "available": available,
        "preferred": preferred,
        "installed_addons": sorted(installed),
    }


def resolve_backend(requested):
    """Map a requested backend to one that is actually present."""
    requested = (requested or NATIVE).strip().lower()
    detected = detect_backends()
    if requested in {BATOMS, "beautiful-atoms", "beautiful_atoms"}:
        return BATOMS if detected["batoms"] else NATIVE
    if requested in {MOLECULARNODES, "molecular_nodes", "mn"}:
        return MOLECULARNODES if detected["molecularnodes"] else NATIVE
    return NATIVE


def apply_backend(obj, backend, style=None):
    """Tag the crystal object for the selected backend.

    This is intentionally non-destructive: the native Geometry Nodes modifier
    always stays in place and keeps rendering even when an optional add-on is
    merely present.  Returns a small report instead of raising.
    """
    backend = resolve_backend(backend)
    report = {"backend": backend, "applied": False, "notes": []}
    if obj is None:
        report["notes"].append("no crystal object")
        return report

    try:
        obj["polycrystal_backend"] = backend
    except Exception:
        report["notes"].append("could not tag object")

    if backend == NATIVE:
        report["applied"] = True
        report["notes"].append("native Geometry Nodes pipeline active")
        return report

    if backend == BATOMS:
        obj["batoms_settings"] = {
            "style": "ball_and_stick",
            "model_style": 0,
            "polyhedra": False,
        }
        report["applied"] = True
        report["notes"].append(
            "beautiful-atoms detected; native GN kept as the ground truth"
        )
    elif backend == MOLECULARNODES:
        obj["molecularnodes_universe"] = "dsh_polycrystal"
        report["applied"] = True
        report["notes"].append(
            "MolecularNodes detected; native GN kept as the ground truth"
        )
    return report


def capabilities():
    detected = detect_backends()
    return {
        "native_gn": ["atoms", "bonds", "half_bonds", "backbone_tube", "unit_cell", "ghost"],
        "batoms": ["atoms", "polyhedra", "surfaces"] if detected["batoms"] else [],
        "molecularnodes": ["atoms", "cartoon", "surfaces", "trajectories"]
        if detected["molecularnodes"]
        else [],
        "detected": detected,
    }
