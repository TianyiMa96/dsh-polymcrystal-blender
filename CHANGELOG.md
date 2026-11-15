# Changelog

All notable changes to `dsh-plugin-polycrystal-bridge`.
Version numbers follow SemVer on the triple (host plugin / extension / sidecar), kept in lockstep.

## 0.3.1 — 2026-11-15

Ball-and-stick polish: the surface is a clear-coated PBR shader, atoms are four
times denser, and the unit cell is a separate shadowless object.

### Changed

- `geonodes_builder` (`GROUP_VERSION 16 → 17`): the surface shader now reads two
  extra geometry attributes — `rough` (base roughness) and `coat` (clear coat
  weight) — stored per island: atoms `0.30 / 0.26`, bonds `0.40 / 0.06`,
  backbone `0.34 / 0.16`. `SURFACE_MATERIAL` `3 → 4` (+`Coat Roughness 0.06`,
  `Specular IOR Level 0.5`, `IOR 1.46`, `Diffuse Roughness 0.15`).
- The cell branch is shaded with a dedicated `DSH_PolyCrystal_UnitCell`
  material (amber, emission 0.20) *before* the join, so the body material cannot
  overwrite it.
- `Subdivision Quality` default `2 → 3`: 80 → 320 ico faces per atom. pbc_111
  total 2932 → 10612 faces, EEVEE 1200×1500 render time unchanged (0.25 s).
- `loader_npz`: the unit cell is written to its own mesh/object
  `PolyCrystal_UnitCell` (8 verts / 12 edges), sharing the master GN modifier,
  with `visible_shadow = False`; the crystal mesh no longer contains the cell
  block. `load_bundle` reports `cell_edges`, `cell_object`,
  `cell_visible_shadow`.
- `loader_npz.ensure_lighting`: added the small `PolyCrystal_Spec` area light
  (clear-coat highlight), Fill 120 → 150, world gradient top 0.055 → 0.085
  (`WORLD_VERSION 2 → 3`); an existing rig is topped up instead of left alone.
- `bridge_client.frame_camera` honours `azimuth` (yaw around world Z, degrees) —
  the GUI launcher had been passing `-35` since 0.3.0 with no effect.
- `bridge_client.build_state` exposes `unit_cell` (object, `hide_render`,
  `visible_shadow`, modifier).

### Defaults (confirmed for 0.3.1)

- PBC realization: `pbc_cluster` (`ghost_atoms = 0`, `half_bonds = 0`,
  `boundary_pad_atoms = 0`); `periodic_clipped` / `chain_continuous` /
  `hkl_lamella` remain explicit fallbacks.
- `supercell = [1,1,1]` — input CIFs are usually already expanded; `"auto"`
  stretches along the chain axis and must be requested explicitly.
- Camera: `ORTHO` (perspective opt-in via `perspective=true`), `fill 0.92`,
  `iso` view, optional `azimuth` / crystal-axis alignment.
- Ghost atoms hidden by default (`Show Ghost Boundary Atoms = False`).

### Measured (pbc_111, 1200×1500, iso / azimuth −35, fill 0.92)

- sphere faces 80 → 320 (+240), crystal faces 2932 → 10612.
- cell visible vs hidden: 54 291 px (3.02 % of the frame) change, bbox spans the
  whole cage; cell `visible_shadow = False` vs `True`: 11 750 px (0.65 %)
  change, **100 % of them on the crystal body** — the ring-shaped shadow
  stripes of the old default.
- regression: `ghost_atoms = 0`, `half_bonds = 0`, `supercell = [1, 1, 1]`,
  ortho, `fill = 0.92`; smoke test 49 checks pass.

## 0.3.0 — 2026-11-15

Coherent molecules/chains: PBC clustering now happens in the sidecar *before* the bundle is
written, so the Blender side never has to patch fragments back together. `pbc_mode` defaults to
`pbc_cluster` (sidecar / extension / host tools).

### Added — `pbc_cluster` realization

- `polycrystal_core/polymer_unwrap.py::realize_pbc_cluster`: periodic bond graph → connected
  components → BFS spanning tree unwrap (`frac + sigma`, always the mirror nearest to the parent) →
  edges re-checked after unwrapping, only the periodic closure edge is dropped and reported.
- Rigid placement: rank-0 molecules are moved as a whole (integer lattice translation, centred),
  chains/sheets/networks keep their lateral registry (integer shift) and the model is centred along
  the chain axis so the strand hangs symmetrically out of the cell.
- `RealizedGeometry.clusters` + `pbc_unwrapped`: per-cluster atom/bond counts, cross-period flag,
  max unwrap displacement, bbox, degree histogram, endpoint atoms, cut-edge count.
- CLI: `--pbc-mode pbc_cluster` (new default) and `--pbc-cluster auto|off`; `UnwrapError` falls back
  to `periodic_clipped` with `report.pbc_cluster.warning`, an explicit legacy `pbc_mode` is left
  untouched. Manifest gains `pbc_unwrapped`, `pbc_cluster`, and a `validation` block with
  `max_rendered_bond_length` so the host tool can verify the 3.5 Å invariant for real.

### Changed

- `is_ghost`, `is_boundary_pad` and `half_bond_*` are all zero for `pbc_cluster` bundles; the
  loader/GN ghost and half-bond branches stay in place but are now no-ops.
- `loader_npz.load_bundle` returns `pbc_mode`, `pbc_unwrapped`, `pbc_clusters` and adds an
  "unwrapped" note; the ghost note only appears when ghost atoms actually exist.
- `polycrystal_load` / `polycrystal_modify` default `pbc_mode` to `pbc_cluster`; the load tool's
  bond-length validation reads the sidecar report instead of giving up with "not reported".

### Measured (PCLtrue.cif, P2₁2₁21, auto supercell 1×1×2)

- bundle: 64 atoms, 62 bonds, 2 clusters (32 atoms / 31 bonds each), 2 cut periodic edges,
  `half_bonds=0`, `ghost_atoms=0`, `boundary_pad_atoms=0`, max rendered bond 1.5367 Å.
- evaluated mesh: 5988 faces with ghosts hidden and 5988 with ghosts shown (delta 0), 0 half-bond
  edges, 0 ghost / 0 pad points, 64 atom points; each chain connected with degree histogram
  `{1:5, 2:24, 3:3}` and a 32.89 Å z-span (2 periods of c = 34.1 Å).
- sidecar suite: 20 passed.

## 0.2.0 — 2026-11-15

First release that renders a polymer crystal correctly in Blender 5.x. The 0.1.0 render path was
visually broken on 5.x even though it produced geometry: bonds were 1 Å-thick tubes, every surface
was painted with the unit-cell material, and CPK colours collapsed to grey.

### Fixed — Blender 5.x compatibility

- **Modifier inputs**: Blender 5.x removed IDProperties from modifiers (`mod["Socket_3"] = v` raises
  `id properties not supported for this type`). `props.set_modifier_input()` now drives
  `mod.properties.inputs.<Socket>.value` first and only falls back to the 4.x IDProperty path.
  `props.get_modifier_input()` mirrors that for state read-back.
- **Tube radius**: `Curve to Mesh` on 5.x ignores the per-point `Set Curve Radius` and uses the
  profile circle's own radius. Every bond, backbone and unit-cell edge therefore rendered at 1.0 Å.
  `_Builder.tube()` now feeds the radius into the profile's `Radius` input.
- **Material index remap**: `Join Geometry` re-sorts branch material indices on 5.x, so the atom
  branch silently inherited the unit-cell material (measured: 3280 atom faces on slot 3). Replaced
  per-island materials with one `DSH_PolyCrystal_Surface` material driven by geometry attributes.
- **Realize Instances**: instanced point attributes are discarded by `Realize Instances`; `col`,
  `alpha`, `glow` and `is_ghost_f` are now stored on the **INSTANCE** domain before realizing.
- **Named Attribute dtype**: a `FLOAT`-typed Named Attribute node reading a `FLOAT_COLOR` attribute
  returns luminance only, which is why oxygen turned grey. The ghost re-colour branch now requests
  `FLOAT_COLOR` explicitly.
- **EEVEE engine id**: 5.x exposes `BLENDER_EEVEE` (no `BLENDER_EEVEE_NEXT`); the render handler
  already falls back through the candidate list.
- **Registration crash**: `snapshot_meta()` read `bpy.data.filepath` while `_RestrictData` was in
  effect during add-on registration; it now tolerates `AttributeError`.

### Fixed — protocol

- The extension now implements `polycrystal.inspect` and `polycrystal.modify`, which the host tools
  `polycrystal-inspect.js` / `polycrystal-modify.js` were already calling (they failed with
  `unknown_method`).
- `build_state()` no longer crashes on `modifier.get(...)` (`this type doesn't support IDProperties`).
- `polycrystal_core/cli.py` accepts `--backend`, matching what the host tools pass.

### Changed — default look

- Ball-and-stick is the default (`Style Mode 0`, `Atom Scale 0.36`, `Bond Radius 0.15`); CPK carbon
  raised to `(0.58, 0.59, 0.63)` and bonds darkened to `(0.33, 0.34, 0.39)` so spheres and sticks
  read as separate parts on a dark backdrop.
- Unit-cell frame reduced to a 0.04 Å tube with weak emission; it used to be a 1 Å glowing cage that
  tinted the whole frame amber.
- World is a vertical dark gradient (`0.012 → 0.055` blue-grey) instead of a flat `0.135` grey that
  was brighter than the model.
- Lighting is a size-adaptive three-point rig (`Key`/`Fill`/`Rim`) aimed at the structure centre,
  with inverse-square gain so a 20 Å crystal is lit like a 9 Å one. Previously fixed coordinates put
  the key light inside tall crystals.
- Colour management defaults to AgX "Medium High Contrast", exposure +0.2, EEVEE raytracing and
  shadows on, 64 render samples.
- Smooth shading on all generated surfaces (ico spheres, tubes, tubes along backbones).

### Changed — camera

- `frame_camera()` now fits the projected **evaluated** geometry: a bbox-corner perspective solve
  followed by an iterative refinement against the modifiers' output, because `obj.bound_box` is the
  base mesh of a Geometry Nodes object and mis-measured the subject badly.
- `fill` parameter (default `0.92`) is the fraction of the frame the subject reaches toward the
  nearest border; `distance` is reported back for inspection.
- Fixed sensor-fit handling: Blender's `sensor_fit = AUTO` maps the 36 mm sensor to the *longer*
  image side, so portrait renders previously used the wrong vertical FOV and cropped the crystal.
- `polycrystal_render` refits automatically when the requested resolution changes the aspect ratio by
  more than 8 %, unless `frame_camera` is passed explicitly (new `refit` argument, default `true`).

### Known limits

- Blender 4.2/4.3 are still declared as the minimum, but only 5.3 has been exercised end to end; the
  4.x fallbacks exist but are untested in this release.
- The crystal is a single mesh object; per-component materials are intentionally gone, so a style
  change is a node-input change, not a material swap.
