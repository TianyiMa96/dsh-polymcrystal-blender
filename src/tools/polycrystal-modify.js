// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * Tool 3: polycrystal_modify
 * Apply style / PBC / (hkl) updates to the live Blender scene. Re-runs the
 * sidecar for a new supercell or pbc_mode when requested, while always
 * preserving human-locked materials & camera and respecting allow_ai_edits.
 */
import path from 'node:path';
import { defineTool } from '@deepseek-ai/dsh-tools';
import {
  textContent, rpcCall, resolveTargetInstance, computeStructureHash, ensureDir,
  toBlenderPath, readConfig, normalizeTriple, asPath,
} from '../runtime.js';
import { ensureVenv, runSidecar } from '../env/sidecar-venv.js';

const STYLE_KEYS = [
  'style_mode', 'color_scheme', 'hide_hydrogens', 'show_backbone_tube',
  'show_ghost_atoms', 'show_half_bonds', 'show_unit_cell', 'show_ghost_boundary_atoms',
  'atom_scale', 'bond_radius', 'tube_radius', 'subdivision_quality',
  'active_disorder_group',
];

export function createModifyTool(runtime) {
  return defineTool({
    name: 'polycrystal_modify',
    description:
      `Update the live polymer-crystal view: Geometry Nodes style/color toggles, hide hydrogens, `
      + `backbone tube, ghost boundary atoms, half-bond caps, chain highlighting, (hkl) overlays, or `
      + `rebuild with a new supercell / pbc_mode. Human-locked camera/materials and the allow_ai_edits `
      + `pause toggle are respected.`,
    parameters: {
      structure_id: { type: 'string', description: 'Structure to modify (defaults to the last loaded structure).' },
      session_id: { type: 'string', description: 'Target Blender instance_id (defaults to claimed / only active).' },
      style_mode: { type: 'integer', description: '0 Ball&Stick, 1 Space-Filling, 2 Licorice, 3 Backbone Tube+Stick.' },
      color_scheme: { type: 'integer', description: '0 CPK, 1 Chain ID, 2 Backbone/Sidechain, 3 Disorder Group.' },
      hide_hydrogens: { type: 'boolean', description: 'Filter atomic_number==1 and H bonds in real time.' },
      show_backbone_tube: { type: 'boolean', description: 'Render smooth backbone spline tubes.' },
      show_ghost_atoms: { type: 'boolean', description: 'Show translucent ghost boundary atoms.' },
      show_half_bonds: { type: 'boolean', description: 'Show half-bond caps at clipped boundaries.' },
      show_unit_cell: { type: 'boolean', description: 'Show triclinic unit-cell wireframe.' },
      atom_scale: { type: 'number', description: 'Atom sphere scale.' },
      bond_radius: { type: 'number', description: 'Bond cylinder radius.' },
      highlight_chain_ids: { type: 'array', items: { type: 'integer' }, description: 'Chain ids to highlight.' },
      hkl_plane: { type: 'array', items: { type: 'number' }, description: 'Miller plane overlay [h, k, l, shift].' },
      supercell: { type: 'array', items: { type: 'integer' }, description: 'New [Na, Nb, Nc] -> re-run sidecar build.' },
      pbc_mode: {
        type: 'string',
        enum: ['pbc_cluster', 'periodic_clipped', 'chain_continuous', 'hkl_lamella'],
        description: 'New PBC mode -> re-run sidecar build (pbc_cluster = PBC unwrapping default).',
      },
      respect_locks: { type: 'boolean', default: true, description: 'Preserve human-locked camera/materials (default true).' },
    },
    output: { schema: { type: 'json' }, render: (_args, value) => textContent(value) },
    timeoutMs: 600000,
    async execute(args, exec) { return runModify(runtime, args, exec); },
  });
}

async function runModify(runtime, args, exec) {
  const signal = exec?.signal;
  const instance = await resolveTargetInstance(runtime, args.session_id, { signal });

  // Respect the human "pause AI edits" toggle from the N-panel.
  let state = null;
  try {
    const st = await rpcCall(runtime, instance.instance_id, 'polycrystal.get_state', {}, { timeoutMs: 8000, signal });
    state = st.result || null;
  } catch {}
  if (state && state.allow_ai_edits === false) {
    return {
      ok: false, paused: true, error: 'AI_EDITS_PAUSED', instance_id: instance.instance_id,
      message: 'Human has paused AI edits (allow_ai_edits=false) in the Blender N-panel. No changes were applied.',
      state,
    };
  }

  const cfg = await readConfig(runtime);
  const last = cfg.lastStructure || null;
  const structureId = args.structure_id || last?.structure_id || null;
  const updates = {};
  for (const key of STYLE_KEYS) if (args[key] !== undefined) updates[key] = args[key];
  if (args.highlight_chain_ids !== undefined) updates.highlight_chain_ids = args.highlight_chain_ids;
  if (args.hkl_plane !== undefined) updates.hkl_plane = args.hkl_plane;

  // Rebuild path: new supercell / pbc_mode -> sidecar -> load_bundle.
  let rebuilt = null;
  if (args.supercell !== undefined || args.pbc_mode !== undefined) {
    if (!last?.file_path) {
      return {
        ok: false, error: 'NO_STRUCTURE_HISTORY',
        message: 'No previously loaded structure to rebuild; call polycrystal_load first with a file_path.',
      };
    }
    const venv = await ensureVenv(runtime, { timeoutMs: 600000, signal });
    if (!venv.ok) return { ok: false, error: 'SIDECAR_NOT_READY', doctor: venv.doctor };

    const supercell = normalizeTriple(args.supercell || last.supercell, [1, 1, 1], { integers: true });
    const mode = args.pbc_mode || last.pbc_mode || 'pbc_cluster';
    const pad = normalizeTriple(last.boundary_pad, [0.05, 0.05, 0.05]);
    const disorder = last.disorder_filter || 'default';
    const backend = last.backend || 'native_gn';
    const hash = await computeStructureHash(runtime, last.file_path, { supercell, mode, pad, disorder, backend });
    const staging = path.join(runtime.paths.stagingDir, hash);
    await ensureDir(staging);

    const cliArgs = [
      'build',
      '--input', last.file_path,
      '--supercell', supercell.join(','),
      '--pbc-mode', mode,
      '--boundary-pad', pad.join(','),
      '--out', staging,
      '--disorder-filter', disorder,
      '--backend', backend,
    ];
    const build = await runSidecar(runtime, cliArgs, { timeoutMs: 300000, signal });
    if (!build.ok) {
      return {
        ok: false, error: 'SIDECAR_REBUILD_FAILED',
        command: `python -m polycrystal_core.cli ${cliArgs.join(' ')}`,
        stdout: build.stdout.slice(0, 4000), stderr: build.stderr.slice(0, 4000),
      };
    }
    const out = build.data || {};
    const bundlePath = asPath(out.bundle) || asPath(out.bundle_path) || path.join(staging, 'bundle.npz');
    const manifestPath = asPath(out.manifest) || asPath(out.manifest_path) || path.join(staging, 'manifest.json');
    const loadRpc = await rpcCall(runtime, instance.instance_id, 'polycrystal.load_bundle', {
      structure_id: structureId,
      bundle_path: await toBlenderPath(runtime, bundlePath),
      manifest_path: await toBlenderPath(runtime, manifestPath),
      staging_dir: await toBlenderPath(runtime, staging),
      backend, pbc_mode: mode, supercell, boundary_pad: pad,
      frame_camera: false,
      preserve_human_locks: args.respect_locks !== false,
    }, { timeoutMs: 120000, signal });
    if (!loadRpc.ok) return { ok: false, error: 'RPC_REBUILD_LOAD_FAILED', rpc: loadRpc };
    rebuilt = { staging_dir: staging, bundle: bundlePath, manifest: manifestPath, supercell, pbc_mode: mode, rpc_result: loadRpc.result };
  }

  const rpc = await rpcCall(runtime, instance.instance_id, 'polycrystal.modify', {
    structure_id: structureId,
    updates,
    respect_human_locks: args.respect_locks !== false,
  }, { timeoutMs: 60000, signal });

  return {
    ok: rpc.ok,
    instance_id: instance.instance_id,
    structure_id: structureId,
    applied_updates: updates,
    rebuilt,
    state_before: state,
    rpc_result: rpc.result,
    error: rpc.error || null,
    message: rpc.ok
      ? 'Updates sent to Blender; call polycrystal_inspect to read back the resulting live state.'
      : 'polycrystal.modify RPC failed; see rpc_result/error.',
  };
}
