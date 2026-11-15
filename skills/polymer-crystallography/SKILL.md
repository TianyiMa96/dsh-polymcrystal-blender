---
# SPDX-License-Identifier: GPL-3.0-or-later
name: polymer-crystallography
description: "Standard operating procedure for the DSH x Blender polymer-crystal bridge (dsh-plugin-polycrystal-bridge). Use when loading CIF/POSCAR/XYZ/PDB/LAMMPS crystal structures, reasoning about periodic polymer PBC topology, chain conformation/helices, (hkl) lamellar faces, or co-editing a live Blender 4.2+ GUI with a human. Covers the five polycrystal_env/load/modify/inspect/render tools, the isolated Python sidecar, and the 127.0.0.1:39888 gateway."
license: GPL-3.0-or-later
---

# Polymer Crystallography — DSH × Blender Bridge SOP

This skill drives `dsh-plugin-polycrystal-bridge`: a non-intrusive bridge between
DeepSeek Harness, an **isolated Python crystallography sidecar** (`gemmi`,
`pymatgen`, `spglib`, `ase`, `vesin`) and a **user-installed interactive Blender
4.2+ GUI**. Never pollute Blender's bundled Python and never listen on port 9876.

## 0. Hard rules

- **Never** `pip install` into Blender's bundled Python. All heavy packages live
  in `$DSH_HOME/plugins/dsh-polycrystal/.venv`.
- The gateway is `127.0.0.1:39888` (`DSH_POLYCRYSTAL_PORT` overrides). Port
  `39877` belongs to `sixtysevenlf/dsh-blender-plugin`; `9876` is forbidden.
- The Blender extension `dsh_polycrystal_ext` reverse-dials the gateway, sends a
  heartbeat every 2 s, and executes RPC on Blender's main thread. An instance is
  live only while `age_ms < 6000`.
- Rendered bonds must never span the simulation box: the hard invariant is
  `max_rendered_bond_length <= 3.5 A`.

## 1. First turn of every session — always doctor first

Call `polycrystal_env(action="doctor")` before anything else. Read its report:

- `blender.executable` / `version_ok` — need Blender >= 4.2.0.
- `blender.extension_installed` — if false, call
  `polycrystal_env(action="install_blender_ext")` (non-destructive Blender CLI
  `install-file`), then restart an open GUI.
- `sidecar.venv_ready` — if false, call
  `polycrystal_env(action="setup_sidecar")` to provision the isolated `.venv`
  and verify `python -m polycrystal_core.cli doctor` returns `{"ok":true}`.
- `live_instance` / `gateway.sessions` — if **no GUI is connected**, call
  `polycrystal_env(action="launch_gui")` to start a detached interactive Blender
  that auto-connects via `--python-expr`. If several sessions are live, call
  `polycrystal_env(action="claim_session", session_id="...")` to pick one.

Do **not** load a structure until at least one live, heartbeating GUI exists.

## 2. Before applying edits — always inspect first

On any subsequent turn, call `polycrystal_inspect(include_viewport_capture=true)`
**before** `polycrystal_modify` or a second `polycrystal_load`. Respect what the
human changed in the Blender `PolyCrystal` N-panel:

- `allow_ai_edits: false` → AI edits are **paused**; do not mutate the scene.
- `lock_camera_and_lights` / `lock_custom_materials` → never reset the human's
  camera angle or custom look-dev materials (loading already passes
  `respect_human_locks: true`).
- `selected_chain_ids` / `selected_objects` → these are what the human clicked;
  frame answers and highlights around them.
- `hide_hydrogens`, `style_mode`, `color_scheme`, `show_backbone_tube` → keep the
  human's slider values unless they explicitly ask to change them.

Use the returned `viewport_capture_path` to see exactly what the human sees.

## 3. Choose `pbc_mode` intelligently

| User asks about | Use |
| --- | --- |
| Anything about a polymer crystal (default) | `pbc_mode="pbc_cluster"` — the sidecar clusters every periodic component and unwraps it into one coherent molecule/chain before export; no ghost atoms, no boundary pad, no half-bond stubs |
| Unit-cell packing, symmetry, space group, lattice parameters (a,b,c,α,β,γ) | `pbc_mode="periodic_clipped"` with `show_half_bonds=true`, `show_unit_cell=true`, default `boundary_pad=[0.05,0.05,0.05]` (legacy: pads the boundary, so expect ghost copies) |
| A single chain: conformation, helical pitch (3₁ / 7₂), planar zigzag, tacticity, backbone morphology | `pbc_mode="chain_continuous"` with `supercell=[1,1,3]` (repeats along the chain axis, usually `c`) and `show_backbone_tube=true` |
| Crystal growth faces, lamellar fold surfaces, (hkl) d-spacing | `pbc_mode="hkl_lamella"`, or add an `hkl_plane=[h,k,l,shift]` overlay via `polycrystal_modify` |

`polycrystal_load` runs the sidecar:

```
python -m polycrystal_core.cli build --input <file> --supercell Na,Nb,Nc \
  --pbc-mode <mode> --boundary-pad da,db,dc --out <staging_dir>
```

It writes `bundle.npz` + `manifest.json` into
`$DSH_HOME/plugins/dsh-polycrystal/staging/<structure_hash>/`, validates the
zero-cross-box-long-bond invariant, and pushes the bundle to the claimed Blender
GUI via RPC `polycrystal.load_bundle`. Under WSL2 the staging paths are
translated with `wslpath -w` so Windows Blender can read them.

## 4. Tool reference (5 tools, compact surface)

1. `polycrystal_env` — `doctor | setup_sidecar | install_blender_ext | launch_gui | list_sessions | claim_session`. Discover/provision/attach; verify live round-trip health.
2. `polycrystal_load` — `file_path`, `supercell`, `pbc_mode`, `boundary_pad`, `disorder_filter`, `backend`, `structure_id`, `session_id`. Build + validate + push; returns crystallographic & polymer chain topology report (space group, lattice, chain axes, chain rank, disorder summary).
3. `polycrystal_modify` — `style_mode`, `color_scheme`, `hide_hydrogens`, `show_backbone_tube`, `show_ghost_atoms`, `show_half_bonds`, `highlight_chain_ids`, `hkl_plane`, or re-run the sidecar for a new `supercell` / `pbc_mode` while preserving human-locked materials & camera.
4. `polycrystal_inspect` — `include_viewport_capture`. Read back every N-panel slider, `allow_ai_edits` / locks, selection, camera transform, and an optional viewport PNG path.
5. `polycrystal_render` — `engine` (`BLENDER_EEVEE_NEXT` | `BLENDER_EEVEE` | `CYCLES`), `samples`, `resolution`, `orthographic_crystal_axis` (`a|b|c|chain_axis|current_user_view`), `transparent`, `refit`, `fill`, `output_path`. Returns the image path for DSH visual inspection. Blender 5.x only exposes `BLENDER_EEVEE`; the `_NEXT` alias is translated by the extension. `refit` (default true) re-fits the camera whenever the requested resolution changes the aspect ratio by more than 8% — without it a portrait render of a tall c-axis crystal becomes a sliver. `fill` (default 0.92) is how far the structure reaches toward the nearest border.

## 5. Typical flows

**Packing question**
```
polycrystal_env(doctor) -> [launch_gui if needed] ->
polycrystal_load(file_path=..., pbc_mode="pbc_cluster", supercell=[2,2,2]) ->
polycrystal_render(engine="CYCLES", samples=256, orthographic_crystal_axis="c")
```

**Single-chain / helix question**
```
polycrystal_inspect() ->
polycrystal_load(file_path=..., pbc_mode="chain_continuous", supercell=[1,1,3]) ->
polycrystal_modify(show_backbone_tube=true, highlight_chain_ids=[0]) ->
polycrystal_inspect(include_viewport_capture=true)
```

**Human-in-the-loop editing**: after the human toggles `Hide Hydrogens` or rotates
the camera in Blender, always `polycrystal_inspect` again and honor `allow_ai_edits`
and the lock flags before issuing another `polycrystal_modify`.

## 6. Failure hygiene

- `SIDECAR_NOT_READY` / `VENV_MISSING` → `polycrystal_env(action="setup_sidecar")`.
- `BLENDER_NOT_FOUND` / `BLENDER_TOO_OLD` → set `BLENDER_EXECUTABLE` or install 4.2+.
- `EXTENSION_ARTIFACT_NOT_FOUND` → build `dsh_polycrystal_ext.zip` under `extension/dist/`.
- `NO_ACTIVE_SESSIONS` / `RPC_TIMEOUT` → the GUI stopped heartbeating; re-launch or
  re-claim via `polycrystal_env`, then retry.
- `BUNDLE_VALIDATION_FAILED` → a bond exceeded 3.5 A; the sidecar reports
  `report.pbc_cluster.status == "fallback"` plus a `warning` when it could not
  unwrap and fell back to `periodic_clipped`. Retry with `pbc_mode="pbc_cluster"`
  on a periodic structure, or a larger `boundary_pad` for the legacy path.

## 中文速查

首轮必跑 `polycrystal_env(doctor)`；改场景前必跑 `polycrystal_inspect` 尊重人工
滑块与 `allow_ai_edits` / 相机材质锁。默认用 `pbc_cluster`(sidecar 内按周期边界把
每条分子/链解开成连贯实体,无 ghost/pad/半键)；晶胞堆积对照用 `periodic_clipped`；
单链构象/螺旋用 `chain_continuous` + `supercell=[1,1,3]` + 骨架管；生长面/(hkl) 用
`hkl_lamella` 或 `hkl_plane` 叠加。禁止污染 Blender 内置 Python，禁止监听 9876。
