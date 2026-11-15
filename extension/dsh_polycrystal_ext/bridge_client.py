# SPDX-License-Identifier: GPL-3.0-or-later
"""Thread-safe outbound bridge between Blender and the DSH gateway.

Architecture (strictly respected):

* A background ``threading.Thread`` only performs HTTP I/O with the standard
  library.  It **never** touches ``bpy``.
* Requests received from the gateway are pushed into a ``queue.Queue``.
* ``bpy.app.timers`` calls :func:`process_rpc_queue` on Blender's main thread
  every 0.05 s; queued requests are dispatched there and their results are
  POSTed back to ``/rpc-response``.

Gateway contract (localhost only, port ``DSH_POLYCRYSTAL_PORT`` / 39888):

* ``POST /register``       -> ``{"instance_id": "..."}``
* ``POST /heartbeat``      -> keep-alive, every 2 s
* ``POST /rpc``            -> long-ish poll, returns one pending request
* ``POST /rpc-response``   -> ``{"request_id": ..., "result": {...}}``
"""

from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
import traceback
import urllib.error
import urllib.request

import bpy
from mathutils import Vector

from . import interop_backends
from . import loader_npz
from . import props as props_mod

DEFAULT_PORT = 39888
DEFAULT_HOST = "127.0.0.1"
EXTENSION_VERSION = "0.3.1"
HEARTBEAT_INTERVAL = 2.0
POLL_TIMEOUT = 2.0
TIMER_INTERVAL = 0.05
MAX_REQUESTS_PER_TICK = 4
MUTATING_METHODS = {
    "polycrystal.load_bundle",
    "polycrystal.set_style",
    "polycrystal.frame_camera",
}

_CLIENT = None
_CLIENT_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Low level HTTP helpers (background-thread safe)
# --------------------------------------------------------------------------- #

def gateway_port(port=None):
    if port is not None:
        return int(port)
    raw = os.environ.get("DSH_POLYCRYSTAL_PORT", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_PORT


def gateway_url(port=None):
    override = os.environ.get("DSH_POLYCRYSTAL_GATEWAY", "").strip()
    if override:
        return override.rstrip("/")
    return f"http://{DEFAULT_HOST}:{gateway_port(port)}"


def _decode(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return {"_raw": raw.decode("utf-8", errors="replace")[:2000]}


def post_json(url, payload, timeout=4.0):
    """POST JSON and return ``(status_code, decoded_body)``.

    Network failures raise ``urllib.error.URLError`` / ``OSError`` so callers
    can decide whether to retry silently.
    """
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": f"dsh-polycrystal-ext/{EXTENSION_VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200)), _decode(response.read())
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        return int(exc.code), _decode(raw)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class BridgeClient:
    """Background gateway client.  All public methods except ``pump`` may be
    called from Blender's main thread."""

    def __init__(self, port=None, token=None):
        self.port = gateway_port(port)
        self.base_url = gateway_url(port)
        self.token = token or os.environ.get("DSH_POLYCRYSTAL_TOKEN", "")
        self._state_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._rpc_queue = queue.Queue()
        self._outbox = queue.Queue()
        self._meta = {
            "pid": os.getpid(),
            "blender_version": "",
            "filepath": "",
            "extension": "dsh_polycrystal_ext",
            "extension_version": EXTENSION_VERSION,
            "capabilities": [
                "npz_bundle",
                "geometry_nodes",
                "render",
                "camera_framing",
            ],
        }
        self._instance_id = ""
        self._connected = False
        self._last_error = ""
        self._last_heartbeat = 0.0
        self._next_heartbeat = 0.0
        self._next_register = 0.0
        self._busy = False
        self._requests_done = 0
        self._requests_failed = 0

    # -- lifecycle (main thread) --------------------------------------- #
    def snapshot_meta(self):
        """Refresh plain-data metadata from Blender.  MAIN THREAD ONLY."""
        try:
            blend_filepath = bpy.data.filepath or ""
        except AttributeError:
            # During extension registration Blender restricts bpy.data
            # (_RestrictData); no blend file info is available yet.
            blend_filepath = ""
        meta = {
            "pid": os.getpid(),
            "blender_version": bpy.app.version_string,
            "filepath": blend_filepath,
            "extension": "dsh_polycrystal_ext",
            "extension_version": EXTENSION_VERSION,
            "capabilities": [
                "npz_bundle",
                "geometry_nodes",
                "render",
                "camera_framing",
                "selection_snapshot",
            ],
            "backends": interop_backends.detect_backends(),
        }
        with self._state_lock:
            previous = self._meta
            self._meta = meta
            if previous.get("filepath") != meta["filepath"]:
                # advertise the new file on the next heartbeat / registration
                self._next_heartbeat = 0.0

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self.snapshot_meta()
        self._next_register = 0.0
        self._thread = threading.Thread(
            target=self._run,
            name="DSHPolyCrystalBridge",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self, timeout=1.5):
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._state_lock:
            self._connected = False
            self._instance_id = ""

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def status(self):
        with self._state_lock:
            return {
                "connected": self._connected,
                "instance_id": self._instance_id,
                "last_error": self._last_error,
                "last_heartbeat": self._last_heartbeat,
                "gateway_url": self.base_url,
                "requests_done": self._requests_done,
                "requests_failed": self._requests_failed,
            }

    # -- background loop (NEVER touches bpy) ---------------------------- #
    def _run(self):
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                with self._state_lock:
                    connected = self._connected
                    register_due = now >= self._next_register
                    heartbeat_due = now >= self._next_heartbeat

                if not connected:
                    if register_due:
                        if not self._register():
                            with self._state_lock:
                                self._next_register = time.monotonic() + 2.0
                            self._stop.wait(2.0)
                    else:
                        self._stop.wait(0.25)
                    continue

                if heartbeat_due:
                    self._heartbeat()
                self._drain_outbox()
                self._poll()
            except Exception as exc:  # network hiccup -> silent retry
                with self._state_lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    self._connected = False
                    self._instance_id = ""
                    self._next_register = time.monotonic() + 2.0
                self._stop.wait(2.0)

    def _register(self):
        with self._state_lock:
            payload = dict(self._meta)
            payload.update(
                {
                    "instance_id": self._instance_id,
                    "gateway_port": self.port,
                    "token": self.token,
                    "status": "connecting",
                }
            )
        status, body = post_json(self.base_url + "/register", payload, timeout=5.0)
        instance_id = str(
            body.get("instance_id")
            or body.get("id")
            or body.get("session_id")
            or ""
        )
        if status >= 400 or not instance_id:
            with self._state_lock:
                self._last_error = (
                    f"register failed (HTTP {status})"
                    if status >= 400
                    else "register response missing instance_id"
                )
                self._connected = False
            return False
        with self._state_lock:
            self._instance_id = instance_id
            self._connected = True
            self._last_error = ""
            self._last_heartbeat = time.time()
            self._next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL
            self._next_register = time.monotonic() + 2.0
        return True

    def _heartbeat(self):
        with self._state_lock:
            payload = dict(self._meta)
            payload.update(
                {
                    "instance_id": self._instance_id,
                    "status": "busy" if self._busy else "idle",
                    "pending_requests": self._rpc_queue.qsize(),
                    "requests_done": self._requests_done,
                }
            )
        status, body = post_json(self.base_url + "/heartbeat", payload, timeout=4.0)
        if status >= 400 or body.get("ok") is False:
            with self._state_lock:
                self._last_error = f"heartbeat failed (HTTP {status})"
                if status in (404, 410):
                    self._connected = False
                    self._instance_id = ""
                    self._next_register = time.monotonic() + 0.5
                self._next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL
            return
        with self._state_lock:
            self._last_heartbeat = time.time()
            self._last_error = ""
            self._next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL

    def _poll(self):
        with self._state_lock:
            instance_id = self._instance_id
        if not instance_id:
            return
        payload = {
            "instance_id": instance_id,
            "wait_ms": int(POLL_TIMEOUT * 1000),
        }
        status, body = post_json(
            self.base_url + "/rpc-pending", payload, timeout=POLL_TIMEOUT + 4.0
        )
        if status == 204 or status >= 400 or body.get("pending") is not True:
            if status in (404, 410):
                with self._state_lock:
                    self._connected = False
                    self._instance_id = ""
                    self._next_register = time.monotonic() + 0.5
            return
        if not isinstance(body, dict):
            return

        request = body.get("request")
        if isinstance(request, dict):
            body = request
        elif isinstance(body.get("requests"), list) and body["requests"]:
            body = body["requests"][0]

        request_id = body.get("request_id") or body.get("id")
        method = body.get("method")
        if not request_id or not method:
            return
        params = body.get("params")
        if not isinstance(params, dict):
            params = {}
        self._rpc_queue.put(
            {
                "request_id": request_id,
                "method": method,
                "params": params,
            }
        )

    def _drain_outbox(self, budget=6):
        for _ in range(budget):
            try:
                path, payload = self._outbox.get_nowait()
            except queue.Empty:
                return
            try:
                post_json(self.base_url + path, payload, timeout=3.0)
            except Exception:
                return

    # -- main thread ---------------------------------------------------- #
    def pump(self):
        """Execute queued RPCs on the main thread and POST their results."""
        handled = 0
        for _ in range(MAX_REQUESTS_PER_TICK):
            try:
                request = self._rpc_queue.get_nowait()
            except queue.Empty:
                break
            self._execute(request)
            handled += 1
        return handled

    def _execute(self, request):
        request_id = request.get("request_id")
        method = request.get("method", "")
        params = request.get("params") or {}
        self._busy = True
        try:
            if method in MUTATING_METHODS:
                try:
                    bpy.ops.ed.undo_push(message=f"DSH {method}")
                except Exception:
                    pass
            result = dispatch_rpc(method, params)
            if not isinstance(result, dict):
                result = {"ok": True, "result": result}
            result.setdefault("ok", True)
            with self._state_lock:
                self._requests_done += 1
        except Exception:
            result = {
                "ok": False,
                "error": "dispatch_exception",
                "traceback": traceback.format_exc(limit=6),
            }
            with self._state_lock:
                self._requests_failed += 1
        finally:
            self._busy = False

        response = {
            "request_id": request_id,
            "instance_id": self._instance_id,
            "result": result,
        }
        try:
            status, _body = post_json(
                self.base_url + "/rpc-response", response, timeout=2.0
            )
            if status >= 400:
                self._outbox.put(("/rpc-response", response))
        except Exception:
            self._outbox.put(("/rpc-response", response))

    def send_event(self, event, payload):
        """Fire-and-forget event from the main thread (e.g. selection)."""
        body = {"instance_id": self._instance_id, "event": event, "payload": payload}
        self._outbox.put(("/event", body))
        self.snapshot_meta()
        self.start()
        return True


# --------------------------------------------------------------------------- #
# Singleton accessors
# --------------------------------------------------------------------------- #

def get_client(create=True, restart=False):
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None and create:
            _CLIENT = BridgeClient()
        client = _CLIENT
    if client is not None and restart and client.is_running():
        client.stop()
    return client


def start_client():
    client = get_client(create=True)
    if client is not None:
        client.start()
    return client


def shutdown():
    global _CLIENT
    with _CLIENT_LOCK:
        client = _CLIENT
        _CLIENT = None
    if client is not None:
        client.stop()


# --------------------------------------------------------------------------- #
# RPC dispatch table
# --------------------------------------------------------------------------- #

_HANDLERS = {}


def rpc_handler(method):
    def decorator(func):
        _HANDLERS[method] = func
        return func

    return decorator


def dispatch_rpc(method, params=None):
    """Execute one RPC on the main thread.  Never call from a worker thread."""
    params = params or {}
    handler = _HANDLERS.get(method)
    if handler is None and not method.startswith("polycrystal."):
        handler = _HANDLERS.get("polycrystal." + method)
    if handler is None:
        return {
            "ok": False,
            "error": "unknown_method",
            "method": method,
            "available": sorted(_HANDLERS),
        }
    try:
        result = handler(params)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=6),
        }
    if not isinstance(result, dict):
        result = {"ok": True, "value": result}
    result.setdefault("ok", True)
    return result


# --------------------------------------------------------------------------- #
# State readers
# --------------------------------------------------------------------------- #

def _camera_state(scene):
    camera = scene.camera
    if camera is None:
        return {"present": False}
    matrix = [list(row) for row in camera.matrix_world]
    return {
        "present": True,
        "name": camera.name,
        "location": [round(v, 6) for v in camera.location],
        "rotation_euler": [round(v, 6) for v in camera.rotation_euler],
        "lens": round(float(camera.data.lens), 4),
        "type": camera.data.type,
        "ortho_scale": round(float(camera.data.ortho_scale), 4),
        "matrix_world": [[round(float(v), 6) for v in row] for row in matrix],
    }


def _selection_state():
    context = bpy.context
    selected_objects = getattr(context, "selected_objects", []) or []
    objects = []
    chains = set()
    selected_atoms = 0
    for obj in selected_objects:
        entry = {"name": obj.name, "type": obj.type}
        if obj.type == "MESH" and obj.data is not None:
            mesh = obj.data
            count = len(mesh.vertices)
            entry["vertices"] = count
            chain_attr = mesh.attributes.get("chain_id")
            atom_attr = mesh.attributes.get("is_atom_pt")
            select_values = None
            if count:
                import numpy as np

                select_values = np.zeros(count, bool)
                try:
                    mesh.vertices.foreach_get("select", select_values)
                except Exception:
                    select_values = None
            atom_mask = None
            if atom_attr is not None:
                import numpy as np

                atom_mask = np.zeros(count, bool)
                try:
                    atom_attr.data.foreach_get("value", atom_mask)
                except Exception:
                    atom_mask = None
            if chain_attr is not None:
                import numpy as np

                chain_values = np.zeros(count, np.int32)
                try:
                    chain_attr.data.foreach_get("value", chain_values)
                except Exception:
                    chain_values = None
                if chain_values is not None:
                    if select_values is not None and bool(select_values.any()):
                        mask = select_values
                        if atom_mask is not None:
                            mask = mask & atom_mask
                        selected_atoms += int(mask.sum())
                        chains.update(int(v) for v in sorted(set(chain_values[mask])))
                    elif atom_mask is not None and bool(atom_mask.any()):
                        chains.update(
                            int(v) for v in sorted(set(chain_values[atom_mask]))
                        )
        objects.append(entry)

    active = context.view_layer.objects.active
    return {
        "mode": getattr(context, "mode", "OBJECT"),
        "active_object": active.name if active is not None else "",
        "selected_objects": objects,
        "selected_chains": sorted(chains),
        "selected_atoms": selected_atoms,
    }


def build_state():
    """Full human-visible state returned by ``polycrystal.get_state``."""
    p = props_mod.get_props()
    scene = bpy.context.scene
    client = get_client(create=False)
    status = client.status() if client is not None else {}

    state = {}
    if p is not None:
        for key in props_mod.STATE_PROP_KEYS + props_mod.GN_PROP_KEYS:
            try:
                state[key] = getattr(p, key)
            except Exception:
                continue
        state["highlight_chain_ids"] = [
            int(item)
            for item in str(p.highlight_chain_ids or "").replace(";", ",").split(",")
            if item.strip().lstrip("-").isdigit()
        ]
    else:
        state["allow_ai_edits"] = True
        state["lock_camera_and_lights"] = False
        state["lock_custom_materials"] = False

    state["connected"] = bool(status.get("connected", False))
    state["instance_id"] = status.get("instance_id", "")
    state["gateway_url"] = status.get("gateway_url", gateway_url())
    state["last_error"] = status.get("last_error", state.get("last_error", ""))
    state["bridge_running"] = bool(client.is_running()) if client else False
    state["selection"] = _selection_state()
    state["camera"] = _camera_state(scene)
    state["scene"] = {
        "filepath": bpy.data.filepath or "",
        "name": scene.name,
        "engine": scene.render.engine,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "frame": scene.frame_current,
    }
    state["crystallography"] = loader_npz.manifest_summary()
    state["backends"] = interop_backends.detect_backends()
    state["blender_version"] = bpy.app.version_string
    state["extension_version"] = EXTENSION_VERSION

    crystal = bpy.data.objects.get(props_mod.CRYSTAL_NAME)
    if crystal is not None and crystal.type == "MESH":
        mesh = crystal.data
        state["object"] = crystal.name
        state["mesh"] = {
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "attributes": sorted(attr.name for attr in mesh.attributes),
        }
        modifier = None
        for mod in crystal.modifiers:
            if mod.type == "NODES" and mod.node_group is not None:
                modifier = mod
                break
        if modifier is not None:
            inputs = {}
            try:
                items = [
                    item for item in modifier.node_group.interface.items_tree
                    if getattr(item, "in_out", None) == "INPUT"
                    and getattr(item, "item_type", "SOCKET") == "SOCKET"
                ]
            except Exception:
                items = []
            for item in items:
                # 4.x modifiers exposed IDProperties; 5.x moved inputs to
                # ``modifier.properties.inputs``. Both paths are tried so a
                # missing API never takes the whole state read down.
                inputs[item.name] = props_mod.get_modifier_input(
                    modifier, item.identifier, item.name, None
                )
            state["modifier_inputs"] = inputs

    # The unit cell is a separate object (visible, but shadowless); the show /
    # hide switch is still the shared ``Show Unit Cell Box`` GN socket.
    cell = bpy.data.objects.get(props_mod.CELL_NAME)
    if cell is not None:
        state["unit_cell"] = {
            "object": cell.name,
            "hide_render": bool(cell.hide_render),
            "visible_shadow": bool(getattr(cell, "visible_shadow", True)),
            "modifier": next(
                (mod.name for mod in cell.modifiers if mod.type == "NODES"), ""
            ),
        }
    return state


# --------------------------------------------------------------------------- #
# Style / conversion helpers
# --------------------------------------------------------------------------- #

def _as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "none"}
    return bool(value)


def _as_index(value, names):
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return max(0, min(len(names) - 1, int(text)))
        lowered = text.lower()
        for index, name in enumerate(names):
            if name.lower() == lowered or name.lower().startswith(lowered):
                return index
        aliases = {
            "ball": 0,
            "ball_and_stick": 0,
            "vdw": 1,
            "spacefill": 1,
            "space_filling": 1,
            "licorice": 2,
            "tube": 3,
            "backbone": 3,
            "cpk": 0,
            "chain": 1,
            "chain_id": 1,
            "backbone_sidechain": 2,
            "disorder": 3,
        }
        return aliases.get(lowered, 0)
    return max(0, min(len(names) - 1, int(value)))


def _coerce_prop(key, value):
    if key in {"style_mode"}:
        return _as_index(value, props_mod.STYLE_MODES)
    if key == "color_scheme":
        return _as_index(value, props_mod.COLOR_SCHEMES)
    if key == "active_disorder_group":
        return max(0, min(2, int(value)))
    if key == "subdivision_quality":
        return max(1, min(4, int(value)))
    if key in {"atom_scale", "bond_radius"}:
        return float(value)
    if key in {
        "hide_hydrogens",
        "show_ghost_atoms",
        "show_half_bonds",
        "show_backbone_tube",
        "show_unit_cell",
    }:
        return _as_bool(value)
    return value


def _parse_chain_ids(value):
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = str(value or "").replace(";", ",").split(",")
    result = []
    for item in raw:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


# --------------------------------------------------------------------------- #
# Camera / render
# --------------------------------------------------------------------------- #

def _scene_bounds():
    objects = []
    for obj in bpy.context.scene.objects:
        if obj.type == "MESH" and obj.name.startswith("PolyCrystal"):
            objects.append(obj)
    if not objects:
        objects = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not objects:
        return Vector((0, 0, 0)), Vector((2, 2, 2))

    mins = Vector((float("inf"),) * 3)
    maxs = Vector((float("-inf"),) * 3)
    for obj in objects:
        matrix = obj.matrix_world
        for corner in obj.bound_box:
            world = matrix @ Vector(corner)
            for axis in range(3):
                mins[axis] = min(mins[axis], world[axis])
                maxs[axis] = max(maxs[axis], world[axis])
    if mins.x == float("inf"):
        return Vector((0, 0, 0)), Vector((2, 2, 2))
    center = (mins + maxs) * 0.5
    size = maxs - mins
    return center, size


def ensure_camera(scene):
    """Return the crystal camera, creating an ORTHOGRAPHIC one when missing.

    A crystal is an orthographic subject: parallel bonds must stay parallel on
    screen, otherwise the perspective foreshortening makes the lattice look
    bent.  Every entry point that could create a camera goes through here so
    the default never drifts back to PERSP.
    """
    camera = scene.camera
    if camera is not None:
        return camera
    data = bpy.data.cameras.new("PolyCrystal_Camera")
    data.type = "ORTHO"
    data.ortho_scale = 24.0
    data.lens = 50.0
    data.clip_start = 0.01
    data.clip_end = 10000.0
    camera = bpy.data.objects.new("PolyCrystal_Camera", data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    return camera


def _ortho_axis_factors(camera, aspect):
    """World units along image width / height per unit of ``ortho_scale``.

    Measured with ``world_to_camera_view`` on Blender 5.3: with the default
    AUTO sensor fit ``ortho_scale`` spans the LONGER image side, so a portrait
    render maps it to the vertical extent (1600x1200 -> width, 1200x1500 ->
    height).  Getting this backwards crops the top and bottom of a tall
    crystal.
    """
    sensor_fit = getattr(camera.data, "sensor_fit", "AUTO")
    if sensor_fit == "HORIZONTAL":
        return 1.0, 1.0 / aspect
    if sensor_fit == "VERTICAL":
        return aspect, 1.0
    if aspect >= 1.0:
        return 1.0, 1.0 / aspect
    return aspect, 1.0


def _evaluated_camera_points(camera, scene):
    """Camera-space vertices of the *evaluated* geometry.

    ``obj.bound_box`` only describes the base mesh of a Geometry Nodes object,
    so atoms scaled by the modifier are invisible to it.  Projecting the real
    mesh is the only way to frame what is actually rendered.
    """
    depsgraph = bpy.context.evaluated_depsgraph_get()
    inverse = camera.matrix_world.inverted()
    points = []
    for obj in scene.objects:
        if obj.type != "MESH":
            continue
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            matrix = evaluated.matrix_world
            for vertex in mesh.vertices:
                points.append(inverse @ (matrix @ vertex.co))
        finally:
            evaluated.to_mesh_clear()
    return points


def _chain_axis_view():
    """Camera view name that looks *across* the polymer chain axis.

    Looking straight down a helix hides everything that makes it a chain, so
    "chain_axis" resolves to a lattice direction perpendicular to the axis
    (chain along c -> view along a).
    """
    axes = loader_npz.manifest_summary().get("chain_axes") or []
    index = 2
    for axis in axes:
        try:
            vec = [abs(float(item)) for item in axis]
        except (TypeError, ValueError):
            continue
        if len(vec) != 3 or max(vec) <= 0.0:
            continue
        index = max(range(3), key=lambda i: vec[i])
        break
    candidates = [i for i in range(3) if i != index]
    return "abc"[candidates[0]]


def frame_camera(params=None):
    """Frame the crystal with the scene camera.  Returns a report dict.

    Defaults to an ORTHO camera whose ``ortho_scale`` is solved against the
    evaluated geometry (``fill`` = fraction of the frame the subject spans).
    ``perspective=true`` switches back to a PERSP camera with an exact FOV fit.
    """
    params = params or {}
    scene = bpy.context.scene
    center, size = _scene_bounds()
    radius = max(float(size.x), float(size.y), float(size.z), 0.5)
    camera = ensure_camera(scene)

    direction_name = str(params.get("view", params.get("direction", "iso"))).lower()
    directions = {
        "iso": (1.0, -1.0, 0.75),
        "front": (0.0, -1.0, 0.0),
        "top": (0.0, 0.0, 1.0),
        "side": (1.0, 0.0, 0.0),
        "a": (1.0, 0.0, 0.0),
        "b": (0.0, 1.0, 0.0),
        "c": (0.0, 0.0, 1.0),
    }
    direction = Vector(directions.get(direction_name, directions["iso"])).normalized()
    # Optional yaw around world Z.  The GUI launcher already passed
    # ``azimuth=-35`` (to separate the two PCL chains); it used to be ignored.
    azimuth = float(params.get("azimuth", 0.0) or 0.0)
    if abs(azimuth) > 1e-6:
        angle = math.radians(azimuth)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        direction = Vector(
            (
                direction.x * cos_a - direction.y * sin_a,
                direction.x * sin_a + direction.y * cos_a,
                direction.z,
            )
        ).normalized()
    perspective = _as_bool(params.get("perspective", False))
    fill = min(max(float(params.get("fill", 0.92)), 0.3), 0.99)
    lens = float(params.get("lens", 50.0))
    sensor = float(getattr(camera.data, "sensor_width", 36.0) or 36.0)
    res_x = max(1.0, float(scene.render.resolution_x or 16))
    res_y = max(1.0, float(scene.render.resolution_y or 16))
    aspect = res_x / res_y

    # Blender's default sensor fit is AUTO: the 36 mm sensor spans the LONGER
    # image side, so on a portrait render the vertical FOV is the wide one.
    sensor_fit = getattr(camera.data, "sensor_fit", "AUTO")
    if sensor_fit == "HORIZONTAL" or (sensor_fit == "AUTO" and aspect >= 1.0):
        half_w = math.atan(sensor * 0.5 / lens)
        half_h = math.atan((sensor / aspect) * 0.5 / lens)
    else:
        half_h = math.atan(sensor * 0.5 / lens)
        half_w = math.atan((sensor * aspect) * 0.5 / lens)

    right = direction.cross(Vector((0.0, 0.0, 1.0)))
    if right.length < 1e-4:
        right = direction.cross(Vector((0.0, 1.0, 0.0)))
    right.normalize()
    up = right.cross(direction).normalized()

    # Orthographic framing does not depend on the camera distance, so it only
    # has to be far enough to keep the subject inside the clip range.
    distance = max(radius * 3.0, 1.0)

    def place(dist=None):
        nonlocal distance
        if dist is not None:
            distance = dist
        camera.location = center + direction * distance
        camera.rotation_euler = (
            center - camera.location
        ).to_track_quat("-Z", "Y").to_euler()
        camera.data.type = "PERSP" if perspective else "ORTHO"
        camera.data.lens = lens
        camera.data.clip_start = max(0.001, min(distance * 0.01, 1.0))
        camera.data.clip_end = max(1000.0, distance * 20.0)
        bpy.context.view_layer.update()

    measured = 0.0
    if perspective:
        # Solve the perspective fit exactly: for every bbox corner the
        # projected offset must stay inside ``fill`` of the frame, which gives
        #   D >= c·dir + |c·right| / (fill * tan(half_fov))
        tan_w = math.tan(half_w)
        tan_h = math.tan(half_h)
        half = size * 0.5
        distance = 0.0
        for sx in (-1.0, 1.0):
            for sy in (-1.0, 1.0):
                for sz in (-1.0, 1.0):
                    corner = Vector((sx * half.x, sy * half.y, sz * half.z))
                    depth = corner.dot(direction)
                    distance = max(
                        distance,
                        depth + abs(corner.dot(right)) / (fill * tan_w),
                        depth + abs(corner.dot(up)) / (fill * tan_h),
                    )
        distance = max(distance, 1.0)

        def perspective_fill():
            worst = 0.0
            for point in _evaluated_camera_points(camera, scene):
                depth = -point.z
                if depth <= 1e-4:
                    continue
                worst = max(
                    worst,
                    abs(point.x) / (depth * tan_w),
                    abs(point.y) / (depth * tan_h),
                )
            return worst

        place(distance)
        # bbox corners are only a starting guess for a GN object: correct the
        # distance against the real (modifier-evaluated) geometry.
        for _ in range(6):
            measured = perspective_fill()
            if measured <= 1e-4:
                break
            ratio = measured / fill
            if abs(ratio - 1.0) < 0.01:
                break
            place(max(distance * (1.0 + (ratio - 1.0) * 0.9), 0.5))
        measured = perspective_fill()
    else:
        kw, kh = _ortho_axis_factors(camera, aspect)
        place(distance)
        for _ in range(6):
            points = _evaluated_camera_points(camera, scene)
            if not points:
                break
            min_x = min(p.x for p in points)
            max_x = max(p.x for p in points)
            min_y = min(p.y for p in points)
            max_y = max(p.y for p in points)
            # Recentre on the *rendered* extent: the base-mesh bbox centre is
            # offset from the evaluated geometry by the atom radii.
            offset_x = 0.5 * (min_x + max_x)
            offset_y = 0.5 * (min_y + max_y)
            if abs(offset_x) > 1e-4 or abs(offset_y) > 1e-4:
                center = center + right * offset_x + up * offset_y
                place(distance)
                points = _evaluated_camera_points(camera, scene)
                if not points:
                    break
                min_x = min(p.x for p in points)
                max_x = max(p.x for p in points)
                min_y = min(p.y for p in points)
                max_y = max(p.y for p in points)
            half_x = max(abs(min_x), abs(max_x))
            half_y = max(abs(min_y), abs(max_y))
            # ortho_scale is linear in the frame size, so one solve is exact.
            scale = max(
                2.0 * half_x / (kw * fill),
                2.0 * half_y / (kh * fill),
                1e-3,
            )
            camera.data.ortho_scale = scale
            bpy.context.view_layer.update()
            measured = max(
                half_x / (0.5 * kw * scale), half_y / (0.5 * kh * scale)
            )
            if abs(measured - fill) <= 0.01 * fill:
                break

    scene.camera = camera
    report = {
        "ok": True,
        "camera": camera.name,
        "type": camera.data.type,
        "location": [round(v, 4) for v in camera.location],
        "distance": round(distance, 4),
        "center": [round(v, 4) for v in center],
        "view": direction_name,
        "azimuth": round(azimuth, 3),
        "fill": round(measured, 4),
        "aspect": round(aspect, 4),
    }
    if camera.data.type == "ORTHO":
        report["ortho_scale"] = round(float(camera.data.ortho_scale), 4)
    else:
        report["lens"] = round(lens, 4)
    return report


def _valid_engines():
    try:
        items = bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items
        return {item.identifier for item in items}
    except Exception:
        return set()


def _set_engine(scene, requested):
    if not requested:
        return scene.render.engine
    candidates = [requested]
    if requested == "BLENDER_EEVEE_NEXT":
        candidates.append("BLENDER_EEVEE")
    if requested == "BLENDER_EEVEE":
        candidates.append("BLENDER_EEVEE_NEXT")
    valid = _valid_engines()
    for candidate in candidates:
        if valid and candidate not in valid:
            continue
        try:
            scene.render.engine = candidate
            return candidate
        except Exception:
            continue
    return scene.render.engine


# --------------------------------------------------------------------------- #
# RPC handlers
# --------------------------------------------------------------------------- #

@rpc_handler("polycrystal.ping")
def _rpc_ping(params):
    client = get_client(create=False)
    return {
        "ok": True,
        "pong": True,
        "instance_id": client.status()["instance_id"] if client is not None else "",
        "blender_version": bpy.app.version_string,
    }


@rpc_handler("polycrystal.get_state")
def _rpc_get_state(params):
    return {"ok": True, **build_state()}


@rpc_handler("polycrystal.set_style")
def _rpc_set_style(params):
    p = props_mod.get_props()
    if p is None:
        return {"ok": False, "error": "properties_unavailable"}
    if not p.allow_ai_edits:
        return {
            "ok": False,
            "reason": "ai_edits_disabled",
            "hint": "Enable 'Allow AI Edits' in the PolyCrystal N-panel.",
        }
    return _apply_style_params(p, params)


@rpc_handler("polycrystal.modify")
def _rpc_modify(params):
    p = props_mod.get_props()
    if p is None:
        return {"ok": False, "error": "properties_unavailable"}
    if not p.allow_ai_edits:
        return {"ok": False, "reason": "ai_edits_disabled"}
    updates = params.get("updates") or {}
    if not isinstance(updates, dict):
        return {"ok": False, "error": "updates_must_be_object"}
    flat = dict(params)
    flat.pop("updates", None)
    flat.update(updates)
    return _apply_style_params(p, flat)


@rpc_handler("polycrystal.inspect")
def _rpc_inspect(params):
    return {"ok": True, **build_state()}


def _apply_style_params(p, params):
    applied = {}
    rejected = {}
    for key in props_mod.GN_PROP_KEYS:
        if key not in params or params[key] is None:
            continue
        try:
            value = _coerce_prop(key, params[key])
            setattr(p, key, value)
            applied[key] = getattr(p, key)
        except Exception as exc:
            rejected[key] = f"{type(exc).__name__}: {exc}"

    # friendlier aliases used by the DSH tools
    alias_map = {
        "show_unit_cell_box": "show_unit_cell",
        "show_unit_cell": "show_unit_cell",
    }
    for alias, key in alias_map.items():
        if alias in params and key not in applied:
            setattr(p, key, _as_bool(params[alias]))
            applied[key] = getattr(p, key)

    if "highlight_chain_ids" in params:
        chain_ids = _parse_chain_ids(params["highlight_chain_ids"])
        p.highlight_chain_ids = ",".join(str(item) for item in chain_ids)
        applied["highlight_chain_ids"] = chain_ids
        loader_npz.apply_highlight(chain_ids)

    if "backend" in params and params["backend"]:
        p.backend = str(params["backend"])
        applied["backend"] = p.backend

    props_mod.sync_all_props_to_modifiers(p)
    props_mod.tag_redraw()
    result = {
        "ok": True,
        "applied": applied,
        "state": {
            key: getattr(p, key)
            for key in props_mod.GN_PROP_KEYS
            if hasattr(p, key)
        },
    }
    if rejected:
        result["rejected"] = rejected
    return result


@rpc_handler("polycrystal.load_bundle")
def _rpc_load_bundle(params):
    p = props_mod.get_props()
    if p is None:
        return {"ok": False, "error": "properties_unavailable"}
    if not p.allow_ai_edits:
        return {
            "ok": False,
            "reason": "ai_edits_disabled",
            "hint": "Enable 'Allow AI Edits' in the PolyCrystal N-panel.",
        }

    npz_path = params.get("npz_path") or params.get("path") or params.get("bundle_path")
    manifest_path = params.get("manifest_path") or params.get("manifest") or ""
    if not npz_path:
        return {"ok": False, "error": "missing_npz_path"}
    if manifest_path and not os.path.isabs(manifest_path) and os.path.isabs(str(npz_path)):
        candidate = os.path.join(os.path.dirname(str(npz_path)), os.fspath(manifest_path))
        if os.path.isfile(candidate):
            manifest_path = candidate

    report = loader_npz.load_bundle(
        npz_path,
        manifest_path or None,
        name=params.get("name"),
        backend=str(params.get("backend") or "native_gn"),
    )

    lights_added = False
    camera_result = {"ok": True, "skipped": True, "reason": "camera_locked"}
    if not p.lock_camera_and_lights:
        lights_added = loader_npz.ensure_lighting()
        camera_result = frame_camera(params.get("camera") if isinstance(params.get("camera"), dict) else {})

    return {
        "ok": True,
        "report": report,
        "camera": camera_result,
        "lights_added": lights_added,
        "lock_camera_and_lights": p.lock_camera_and_lights,
    }


@rpc_handler("polycrystal.frame_camera")
def _rpc_frame_camera(params):
    p = props_mod.get_props()
    if p is not None and p.lock_camera_and_lights:
        return {
            "ok": True,
            "skipped": True,
            "reason": "camera_locked",
            "hint": "Uncheck 'Lock Camera & Lights' to let DSH reframe.",
        }
    return frame_camera(params)


@rpc_handler("polycrystal.get_selection")
def _rpc_get_selection(params):
    return {"ok": True, **build_selection_snapshot()}


@rpc_handler("polycrystal.set_highlight")
def _rpc_set_highlight(params):
    p = props_mod.get_props()
    if p is None:
        return {"ok": False, "error": "properties_unavailable"}
    if not p.allow_ai_edits:
        return {"ok": False, "reason": "ai_edits_disabled"}
    chain_ids = _parse_chain_ids(params.get("highlight_chain_ids", params.get("chain_ids", [])))
    p.highlight_chain_ids = ",".join(str(item) for item in chain_ids)
    count = loader_npz.apply_highlight(chain_ids)
    props_mod.tag_redraw()
    return {"ok": True, "highlight_chain_ids": chain_ids, "highlighted_atoms": count}


@rpc_handler("polycrystal.render")
def _rpc_render(params):
    scene = bpy.context.scene
    render = scene.render
    engine = _set_engine(scene, params.get("engine"))
    samples = params.get("samples")
    if samples is not None:
        samples = max(1, int(samples))
        if engine == "CYCLES" and hasattr(scene, "cycles"):
            scene.cycles.samples = samples
            try:
                scene.cycles.use_denoising = bool(params.get("denoise", True))
            except Exception:
                pass
        elif hasattr(scene, "eevee"):
            for attr in ("taa_render_samples", "taa_samples"):
                if hasattr(scene.eevee, attr):
                    try:
                        setattr(scene.eevee, attr, samples)
                    except Exception:
                        pass

    resolution = params.get("resolution") or params.get("resolution_xy")
    previous_aspect = (
        float(render.resolution_x or 16) / float(render.resolution_y or 16)
    )
    if isinstance(resolution, (list, tuple)) and len(resolution) >= 2:
        render.resolution_x = max(16, int(resolution[0]))
        render.resolution_y = max(16, int(resolution[1]))
        render.resolution_percentage = 100
    new_aspect = (
        float(render.resolution_x or 16) / float(render.resolution_y or 16)
    )
    render.film_transparent = bool(params.get("transparent", render.film_transparent))

    output_path = params.get("output_path") or params.get("filepath")
    if output_path:
        output_path = os.path.abspath(os.fspath(output_path))
        folder = os.path.dirname(output_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        render.filepath = output_path
        render.image_settings.file_format = "PNG"
    else:
        output_path = os.path.abspath(render.filepath)

    camera_locked = False
    want_refit = params.get("frame_camera")
    if want_refit is None and abs(new_aspect - previous_aspect) > 0.08 * previous_aspect:
        # The framing was solved for a different frame shape, so a tall crystal
        # would land as a sliver in the middle of a portrait render.
        want_refit = True
    if want_refit:
        p = props_mod.get_props()
        if p is not None and p.lock_camera_and_lights:
            camera_locked = True
        else:
            # ``frame_camera`` may be a dict of frame_camera() params (fill,
            # lens, view, perspective); a bare ``True`` keeps the defaults.
            frame_params = dict(want_refit) if isinstance(want_refit, dict) else {}
            axis = str(params.get("orthographic_crystal_axis") or "").strip().lower()
            if "view" not in frame_params:
                if axis in {"a", "b", "c"}:
                    frame_params["view"] = axis
                elif axis == "chain_axis":
                    frame_params["view"] = _chain_axis_view()
            if "perspective" not in frame_params and params.get("perspective") is not None:
                frame_params["perspective"] = params["perspective"]
            frame_camera(frame_params)

    bpy.ops.render.render(write_still=True)
    exists = os.path.isfile(output_path)
    return {
        "ok": exists,
        "engine": render.engine,
        "samples": samples,
        "resolution": [render.resolution_x, render.resolution_y],
        "output_path": output_path,
        "exists": exists,
        "bytes": os.path.getsize(output_path) if exists else 0,
        "camera_locked": camera_locked,
    }


# --------------------------------------------------------------------------- #
# Selection snapshot (also used by the N-panel button)
# --------------------------------------------------------------------------- #

def build_selection_snapshot():
    scene = bpy.context.scene
    snapshot = _selection_state()
    snapshot["timestamp"] = time.time()
    snapshot["filepath"] = bpy.data.filepath or ""
    snapshot["camera"] = _camera_state(scene)
    snapshot["style"] = {
        key: getattr(props_mod.get_props(), key, None)
        for key in props_mod.GN_PROP_KEYS
    }
    snapshot["crystallography"] = loader_npz.manifest_summary()
    encoded = json.dumps(snapshot, ensure_ascii=False, default=str)
    scene[loader_npz.SCENE_SELECTION] = encoded
    p = props_mod.get_props()
    if p is not None:
        p.selection_snapshot = encoded
    return snapshot


def publish_selection_snapshot():
    snapshot = build_selection_snapshot()
    client = get_client(create=True)
    client.send_event("selection_snapshot", snapshot)
    return snapshot


# --------------------------------------------------------------------------- #
# Timer entry point (main thread)
# --------------------------------------------------------------------------- #

_TICK = 0


def _sync_props():
    client = get_client(create=False)
    if client is None:
        return
    p = props_mod.get_props()
    if p is None:
        return
    status = client.status()
    connected = bool(status["connected"])
    state = "Connected" if connected else (
        "Connecting..." if client.is_running() else "Disconnected"
    )
    if p.connected != connected:
        p.connected = connected
    if p.connection_status != state:
        p.connection_status = state
    if p.instance_id != status["instance_id"]:
        p.instance_id = status["instance_id"]
    if p.last_error != status["last_error"]:
        p.last_error = status["last_error"]
    try:
        p.last_heartbeat = float(status["last_heartbeat"])
    except Exception:
        pass
    if p.gateway_url != status["gateway_url"]:
        p.gateway_url = status["gateway_url"]


def process_rpc_queue():
    """``bpy.app.timers`` callback: pump queue, sync UI, reschedule."""
    global _TICK
    try:
        client = get_client(create=False)
        if client is not None:
            _TICK += 1
            if _TICK % 20 == 0:
                client.snapshot_meta()
            client.pump()
            _sync_props()
    except Exception:
        traceback.print_exc(limit=5)
    return TIMER_INTERVAL
