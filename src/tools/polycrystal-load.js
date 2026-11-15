// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * Tool 2: polycrystal_load
 * Parse CIF/POSCAR/XYZ/PDB/LAMMPS via the isolated Python sidecar, solve
 * polymer PBC topology, validate the zero-cross-box-long-bond invariant, then
 * push the .npz + manifest.json bundle into a claimed live Blender GUI through
 * gateway RPC method "polycrystal.load_bundle".
 */
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import { defineTool } from '@deepseek-ai/dsh-tools';
import {
  textContent, rpcCall, resolveTargetInstance, computeStructureHash, ensureDir,
  toBlenderPath, isWsl2, writeConfig, normalizeTriple, asPath,
} from '../runtime.js';
import { ensureVenv, runSidecar } from '../env/sidecar-venv.js';

export function createLoadTool(runtime) {
  return defineTool({
    name: 'polycrystal_load',
    description:
      'Build a polymer-crystal bundle from a structure file and load it into the live Blender GUI. '
      + 'Runs the isolated sidecar (gemmi/pymatgen/spglib/ase) to solve periodic bonds, chain rank and '
      + 'PBC modes, validates no rendered bond exceeds 3.5 A, then streams the result to Blender via RPC.',
    parameters: {
      file_path: {
        type: 'string', required: true,
        description: 'Absolute path to a .cif / POSCAR / CONTCAR / .xyz / .pdb / LAMMPS .data structure file.',
      },
      supercell: {
        oneOf: [
          { type: 'array', items: { type: 'integer' }, description: 'Explicit [Na, Nb, Nc] multipliers.' },
          { type: 'string', enum: ['auto'], description: 'Auto-expand along the polymer chain axis.' },
        ],
        default: [1, 1, 1],
        description: 'Supercell multipliers (default [1,1,1] — the raw unit cell; input CIFs are often '
          + 'already expanded), or "auto" to stretch the polymer chain axis until at least 2 repeat '
          + 'periods are visible.',
      },
      pbc_mode: {
        type: 'string', default: 'pbc_cluster',
        enum: ['pbc_cluster', 'periodic_clipped', 'chain_continuous', 'hkl_lamella'],
        description: 'pbc_cluster (default; PBC clustering unwraps every molecule/chain into one '
          + 'coherent entity, no ghost/pad/half bonds), periodic_clipped (legacy unit-cell packing '
          + 'with padding + half-bond stubs), chain_continuous (single-chain/helices), '
          + 'hkl_lamella (growth faces).',
      },
      boundary_pad: {
        type: 'array', items: { type: 'number' }, default: [0.05, 0.05, 0.05],
        description: 'Fractional boundary padding [da, db, dc] for ghost atoms / half-bond stubs.',
      },
      disorder_filter: {
        type: 'string', default: 'default', enum: ['default', 'all', 'group_2'],
        description: 'Which _atom_site_disorder_group alternatives to keep.',
      },
      backend: {
        type: 'string', default: 'native_gn', enum: ['native_gn', 'batoms', 'molecularnodes'],
        description: 'Blender rendering backend for the loaded bundle.',
      },
      structure_id: {
        type: 'string',
        description: 'Optional stable id for this structure (auto-derived from file+params when omitted).',
      },
      session_id: {
        type: 'string',
        description: 'Target Blender instance_id. Defaults to the claimed / only active session.',
      },
    },
    output: { schema: { type: 'json' }, render: (_args, value) => textContent(value) },
    timeoutMs: 600000,
    async execute(args, exec) { return runLoad(runtime, args, exec); },
  });
}

async function runLoad(runtime, args, exec) {
  const signal = exec?.signal;
  const filePath = path.resolve(args.file_path);
  let stat;
  try { stat = await fsp.stat(filePath); }
  catch { throw new Error(`polycrystal_load: file not found: ${filePath}`); }
  if (!stat.isFile()) throw new Error(`polycrystal_load: not a regular file: ${filePath}`);

  const supercellSpec = resolveSupercell(args.supercell);
  const pad = normalizeTriple(args.boundary_pad, [0.05, 0.05, 0.05]);
  const mode = args.pbc_mode || 'pbc_cluster';
  const backend = args.backend || 'native_gn';
  const disorder = args.disorder_filter || 'default';

  const hash = await computeStructureHash(runtime, filePath, {
    supercell: supercellSpec.auto ? 'auto' : supercellSpec.triple,
    mode, pad, disorder, backend,
  });
  const structureId = args.structure_id || `struct-${hash.slice(0, 12)}`;
  const staging = path.join(runtime.paths.stagingDir, hash);
  await ensureDir(staging);

  // Sidecar must be healthy before we spend time on a build.
  const venv = await ensureVenv(runtime, { timeoutMs: 600000, signal });
  if (!venv.ok) {
    return {
      ok: false, error: 'SIDECAR_NOT_READY', file_path: filePath,
      doctor: venv.doctor,
      provision: venv.provision ? { ok: venv.provision.ok, error: venv.provision.error || null } : null,
      hint: 'Call polycrystal_env(action="setup_sidecar") and ensure sidecar/ is present.',
    };
  }

  const cliArgs = [
    'build',
    '--input', filePath,
    '--supercell', supercellSpec.auto ? 'auto' : supercellSpec.triple.join(','),
    '--pbc-mode', mode,
    '--boundary-pad', pad.join(','),
    '--out', staging,
    '--disorder-filter', disorder,
    '--backend', backend,
  ];
  const build = await runSidecar(runtime, cliArgs, { timeoutMs: 300000, signal });
  if (!build.ok) {
    return {
      ok: false, error: 'SIDECAR_BUILD_FAILED', file_path: filePath,
      command: `python -m polycrystal_core.cli ${cliArgs.join(' ')}`,
      code: build.code, timedOut: build.timedOut,
      stdout: build.stdout.slice(0, 4000), stderr: build.stderr.slice(0, 4000),
    };
  }

  const out = build.data || {};
  const bundlePath = asPath(out.bundle) || asPath(out.bundle_path) || asPath(out.npz)
    || asPath(out.bundle_npz) || path.join(staging, 'bundle.npz');
  const manifestPath = asPath(out.manifest) || asPath(out.manifest_path)
    || asPath(out.manifest_json) || path.join(staging, 'manifest.json');
  let manifest = null;
  try { manifest = JSON.parse(await fsp.readFile(manifestPath, 'utf8')); } catch {}

  // The sidecar owns the auto rule, so report the supercell it actually used
  // instead of echoing the request back.
  const usedSupercell = normalizeSupercell(
    out.supercell ?? out.report?.supercell ?? manifest?.supercell,
  ) || (supercellSpec.auto ? null : supercellSpec.triple);
  const autoSupercell = out.supercell_auto ?? out.report?.supercell_auto
    ?? manifest?.supercell_auto ?? null;

  const validation = validateBundle(manifest, out, bundlePath, manifestPath);
  if (!validation.ok) {
    return {
      ok: false, error: 'BUNDLE_VALIDATION_FAILED', file_path: filePath,
      staging_dir: staging, validation, out,
    };
  }

  const instance = await resolveTargetInstance(runtime, args.session_id, { signal });

  // Respect human "lock camera and lights" toggle before framing.
  let lockCamera = false;
  try {
    const st = await rpcCall(runtime, instance.instance_id, 'polycrystal.get_state', {}, { timeoutMs: 8000, signal });
    const s = st.result || {};
    lockCamera = !!(s.lock_camera_and_lights || s.locks?.lock_camera_and_lights);
  } catch {}

  const bundleForBlender = await toBlenderPath(runtime, bundlePath);
  const manifestForBlender = await toBlenderPath(runtime, manifestPath);
  const stagingForBlender = await toBlenderPath(runtime, staging);

  const pushed = await rpcCall(runtime, instance.instance_id, 'polycrystal.load_bundle', {
    structure_id: structureId,
    bundle_path: bundleForBlender,
    manifest_path: manifestForBlender,
    staging_dir: stagingForBlender,
    backend,
    pbc_mode: mode,
    supercell: usedSupercell || [1, 1, 1],
    supercell_auto: autoSupercell,
    boundary_pad: pad,
    disorder_filter: disorder,
    frame_camera: !lockCamera,
    respect_human_locks: true,
  }, { timeoutMs: 120000, signal });

  if (!pushed.ok) {
    return {
      ok: false, error: 'RPC_LOAD_FAILED', file_path: filePath,
      instance_id: instance.instance_id, rpc: pushed, validation,
    };
  }

  await writeConfig(runtime, {
    lastStructure: {
      file_path: filePath, structure_id: structureId, staging_dir: staging,
      supercell: usedSupercell, supercell_auto: autoSupercell, pbc_mode: mode,
      boundary_pad: pad, disorder_filter: disorder, backend,
    },
  }).catch(() => {});

  const blenderReport = (pushed.result && pushed.result.report) || {};
  const notes = [
    ...(Array.isArray(blenderReport.notes) ? blenderReport.notes : []),
  ];
  const supercellNote = usedSupercell
    ? `supercell ${usedSupercell.join('x')}${autoSupercell?.enabled
      ? ` (auto: 沿链轴 ${JSON.stringify(autoSupercell.chain_axis)} 展开 ${autoSupercell.repeats} 个周期)`
      : ' (explicit)'}`
    : '';
  if (supercellNote) notes.unshift(supercellNote);

  return {
    ok: true,
    structure_id: structureId,
    file_path: filePath,
    staging_dir: staging,
    bundle: bundlePath,
    manifest: manifestPath,
    wsl2_path_translation: isWsl2(),
    pbc_mode: mode,
    supercell: usedSupercell,
    supercell_requested: supercellSpec.auto ? 'auto' : supercellSpec.triple,
    supercell_auto: autoSupercell,
    boundary_pad: pad,
    disorder_filter: disorder,
    backend,
    instance_id: instance.instance_id,
    frame_camera: !lockCamera,
    camera: pushed.result?.camera ?? null,
    validation,
    manifest_summary: summarizeManifest(manifest),
    polymer_report: out.polymer_report || out.report || manifest?.polymer_report || null,
    max_rendered_bond_length: validation.max_rendered_bond_length,
    notes,
    rpc_result: pushed.result,
    message: `Bundle built (${usedSupercell ? usedSupercell.join('x') : 'raw'} supercell), validated, and pushed to the live Blender GUI. ${notes.join(' ')}`.trim(),
  };
}

/** Parse the supercell argument: "auto" (default) or an explicit [Na, Nb, Nc]. */
function resolveSupercell(value) {
  if (value === undefined || value === null || value === '') return { auto: true, triple: null };
  if (typeof value === 'string') {
    const text = value.trim().toLowerCase();
    if (!text || text === 'auto') return { auto: true, triple: null };
    const parts = text.split(/[,\sx×]+/).filter(Boolean);
    if (parts.length === 3) {
      const triple = parts.map(Number);
      if (triple.every(n => Number.isFinite(n) && n >= 1)) {
        return { auto: false, triple: triple.map(n => Math.max(1, Math.round(n))) };
      }
    }
    throw new Error(`polycrystal_load: invalid supercell "${value}" (expected [Na,Nb,Nc] or "auto")`);
  }
  return { auto: false, triple: normalizeTriple(value, [1, 1, 1], { integers: true }) };
}

/** Normalise a triple reported by the sidecar; null when it is not a triple. */
function normalizeSupercell(value) {
  if (!Array.isArray(value) || value.length < 3) return null;
  const triple = value.slice(0, 3).map(v => Math.round(Number(v)));
  return triple.every(n => Number.isFinite(n) && n >= 1) ? triple : null;
}

function validateBundle(manifest, out, bundlePath, manifestPath) {
  const notes = [];
  const bundleFound = fs.existsSync(bundlePath);
  const manifestFound = fs.existsSync(manifestPath);
  if (!bundleFound) notes.push(`bundle.npz not found at ${bundlePath}`);
  if (!manifestFound) notes.push(`manifest.json not found at ${manifestPath}`);

  const candidates = [
    out.max_rendered_bond_length, out.max_bond_length,
    out.report?.geometry?.max_render_bond_angstrom,
    manifest?.max_rendered_bond_length, manifest?.max_bond_length,
    manifest?.validation?.max_rendered_bond_length, manifest?.validation?.max_bond_length,
  ].filter(v => typeof v === 'number' && Number.isFinite(v));
  const maxBond = candidates.length ? Math.max(...candidates) : null;
  const sidecarAssertion = out.zero_cross_box_long_bonds
    ?? out.report?.geometry?.invariant_ok
    ?? manifest?.validation?.zero_cross_box_long_bonds
    ?? null;
  let crossBoxOk = true;
  if (maxBond !== null && maxBond > 3.5) crossBoxOk = false;
  if (sidecarAssertion === false) crossBoxOk = false;
  if (maxBond === null) {
    notes.push('Sidecar did not report a max bond length; the 3.5 A invariant was not independently verified (best-effort).');
  }

  return {
    ok: bundleFound && manifestFound && crossBoxOk,
    bundle_found: bundleFound,
    manifest_found: manifestFound,
    max_rendered_bond_length: maxBond,
    max_allowed_bond_length: 3.5,
    zero_cross_box_long_bonds: crossBoxOk,
    sidecar_assertion: sidecarAssertion,
    notes,
  };
}

function summarizeManifest(m) {
  if (!m) return null;
  const polymer = m.polymer && typeof m.polymer === 'object' ? m.polymer : {};
  const spaceGroup = m.space_group && typeof m.space_group === 'object'
    ? (m.space_group.symbol || m.space_group)
    : (m.space_group ?? m.space_group_symbol ?? null);
  return {
    formula: m.formula ?? m.chemical_formula ?? null,
    space_group: spaceGroup,
    hall_number: m.hall_number ?? m.space_group?.hall_number ?? null,
    lattice: m.lattice ?? m.lattice_parameters ?? null,
    supercell: m.supercell ?? null,
    supercell_auto: m.supercell_auto ?? null,
    chain_axes: polymer.chain_axes ?? m.chain_axes ?? m.detected_chain_axes ?? null,
    num_chains: polymer.n_chains ?? m.num_chains ?? m.number_of_chains ?? null,
    chains: polymer.chains ?? null,
    disorder_summary: m.disorder ?? m.disorder_summary ?? null,
    sha256: m.sha256 ?? m.hash ?? null,
  };
}
