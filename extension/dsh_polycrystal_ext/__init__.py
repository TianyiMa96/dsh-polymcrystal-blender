# SPDX-License-Identifier: GPL-3.0-or-later
"""DSH Polymer Crystal Bridge — Blender 4.2+ extension entry point.

The extension is deliberately thin: all business logic lives in the sibling
modules.  Registration wires up:

* scene properties (:mod:`props`),
* N-panel, operators and viewport badge (:mod:`ui_panel`),
* the background gateway thread (:mod:`bridge_client`),
* the main-thread RPC timer (0.05 s, persistent).
"""

from __future__ import annotations

import traceback

import bpy

from . import bridge_client
from . import geonodes_builder
from . import interop_backends
from . import loader_npz
from . import props as props_mod
from . import ui_panel

__version__ = "0.3.1"

_REGISTERED = False
_TIMER = bridge_client.process_rpc_queue


# --------------------------------------------------------------------------- #
# Timer helpers
# --------------------------------------------------------------------------- #

def _register_timer():
    try:
        if not bpy.app.timers.is_registered(_TIMER):
            bpy.app.timers.register(
                _TIMER, first_interval=bridge_client.TIMER_INTERVAL, persistent=True
            )
    except Exception:
        traceback.print_exc(limit=3)


def _unregister_timer():
    try:
        if bpy.app.timers.is_registered(_TIMER):
            bpy.app.timers.unregister(_TIMER)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

def register():
    global _REGISTERED
    if _REGISTERED or hasattr(bpy.types.Scene, "dsh_polycrystal"):
        return
    props_mod.register()
    ui_panel.register()
    _register_timer()
    # The bridge thread starts immediately and retries silently when no DSH
    # gateway is listening; it never raises into Blender startup.
    bridge_client.start_client()
    _REGISTERED = True
    print("[DSH PolyCrystal] extension registered")


def unregister():
    global _REGISTERED
    _unregister_timer()
    bridge_client.shutdown()
    try:
        ui_panel.unregister()
    except Exception:
        traceback.print_exc(limit=3)
    try:
        props_mod.unregister()
    except Exception:
        traceback.print_exc(limit=3)
    _REGISTERED = False
    print("[DSH PolyCrystal] extension unregistered")


def auto_connect():
    """Entry point used by DSH's ``--python-expr`` launch command.

    Safe to call at any time: registers the extension if needed, (re)starts the
    background bridge and returns a JSON-serialisable status dictionary.
    """
    status = {"ok": True, "registered": True, "connected": False}
    try:
        if not _REGISTERED and not hasattr(bpy.types.Scene, "dsh_polycrystal"):
            register()
        client = bridge_client.start_client()
        state = client.status() if client is not None else {}
        status.update(
            {
                "gateway_url": state.get("gateway_url", bridge_client.gateway_url()),
                "instance_id": state.get("instance_id", ""),
                "connected": bool(state.get("connected", False)),
                "version": __version__,
            }
        )
    except Exception as exc:
        status["ok"] = False
        status["error"] = f"{type(exc).__name__}: {exc}"
    return status


def get_status():
    """Convenience helper for debugging from Blender's Python console."""
    client = bridge_client.get_client(create=False)
    return {
        "registered": _REGISTERED,
        "version": __version__,
        "bridge": client.status() if client is not None else None,
        "group_present": bpy.data.node_groups.get(props_mod.GROUP_NAME) is not None,
    }


__all__ = [
    "auto_connect",
    "bridge_client",
    "geonodes_builder",
    "get_status",
    "interop_backends",
    "loader_npz",
    "props_mod",
    "register",
    "ui_panel",
    "unregister",
]
