// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * Tool 5: polycrystal_render
 * Publication-quality EEVEE-Next / Cycles render driven through the live
 * Blender GUI via gateway RPC, without freezing human interaction. Returns
 * an image path for DSH visual inspection.
 */
import path from 'node:path';
import { defineTool } from '@deepseek-ai/dsh-tools';
import {
  textContent, rpcCall, resolveTargetInstance, ensureDir, toBlenderPath, normalizeTriple,
} from '../runtime.js';

export function createRenderTool(runtime) {
  return defineTool({
    name: 'polycrystal_render',
    description:
      'Render a publication-quality figure from the live Blender GUI (BLENDER_EEVEE_NEXT or CYCLES). '
      + 'Supports samples, resolution, orthographic crystal-axis alignment and transparent film. '
      + 'Returns the image path for DSH visual inspection.',
    parameters: {
      engine: {
        type: 'string', default: 'BLENDER_EEVEE_NEXT',
        enum: ['BLENDER_EEVEE_NEXT', 'BLENDER_EEVEE', 'CYCLES'],
        description:
          'Render engine. Blender 5.x exposes EEVEE as BLENDER_EEVEE (there is no *_NEXT id); '
          + 'the _NEXT alias is kept for 4.2+ and translated by the extension.',
      },
      samples: { type: 'integer', description: 'Sample count (defaults: 64 EEVEE, 128 Cycles).' },
      resolution: { type: 'array', items: { type: 'integer' }, default: [1600, 1200], description: '[width, height].' },
      orthographic_crystal_axis: {
        type: 'string', default: 'current_user_view',
        enum: ['a', 'b', 'c', 'chain_axis', 'current_user_view'],
        description: 'Align an orthographic camera to a crystal axis, or keep the human view.',
      },
      transparent: { type: 'boolean', default: false, description: 'Render with transparent film (default false).' },
      refit: {
        type: 'boolean', default: true,
        description: 'Re-fit the camera to the requested resolution before rendering (skipped when the camera is locked).',
      },
      fill: {
        type: 'number', default: 0.92,
        description: 'How much of the frame the structure should reach toward the nearest border (0.5-0.98).',
      },
      output_path: { type: 'string', description: 'Where to save the PNG. Defaults under $POLYCRYSTAL_HOME/staging/renders/.' },
      session_id: { type: 'string', description: 'Target Blender instance_id (defaults to claimed / only active).' },
    },
    output: { schema: { type: 'json' }, render: (_args, value) => textContent(value) },
    timeoutMs: 600000,
    async execute(args, exec) { return runRender(runtime, args, exec); },
  });
}

async function runRender(runtime, args, exec) {
  const signal = exec?.signal;
  const instance = await resolveTargetInstance(runtime, args.session_id, { signal });
  const engine = args.engine || 'BLENDER_EEVEE_NEXT';
  const samples = Number.isFinite(args.samples) && args.samples > 0
    ? Math.round(args.samples)
    : (engine === 'CYCLES' ? 128 : 64);
  const resolution = normalizePair(args.resolution, [1600, 1200]);
  const axis = args.orthographic_crystal_axis || 'current_user_view';
  const transparent = args.transparent === true;
  const fill = Number.isFinite(args.fill) ? Math.min(Math.max(args.fill, 0.4), 0.98) : 0.92;
  const outputPath = args.output_path
    ? path.resolve(args.output_path)
    : path.join(runtime.paths.rendersDir, `polycrystal_${Date.now()}.png`);
  await ensureDir(path.dirname(outputPath));

  const rpc = await rpcCall(runtime, instance.instance_id, 'polycrystal.render', {
    engine,
    samples,
    resolution,
    orthographic_crystal_axis: axis,
    transparent,
    // Reframe for the requested resolution: a camera fitted for a 1920x1080
    // viewport leaves a tall crystal as a thin sliver in a portrait render.
    // The extension skips this when the user locked the camera.
    frame_camera: args.refit === false ? false : { fill: fill },
    output_path: await toBlenderPath(runtime, outputPath),
    non_blocking: true,
  }, { timeoutMs: 600000, signal });

  if (!rpc.ok) {
    return { ok: false, error: 'RPC_RENDER_FAILED', instance_id: instance.instance_id, rpc };
  }
  const result = rpc.result || {};
  return {
    ok: true,
    image_path: result.image_path || result.output_path || outputPath,
    engine,
    samples,
    resolution,
    orthographic_crystal_axis: axis,
    transparent,
    instance_id: instance.instance_id,
    rpc_result: result,
    message: 'Render complete; pass image_path to DSH visual inspection.',
  };
}

function normalizePair(value, fallback) {
  const arr = Array.isArray(value) ? value.slice(0, 2) : fallback.slice();
  while (arr.length < 2) arr.push(fallback[arr.length]);
  return arr.map((v, i) => {
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? Math.round(n) : fallback[i];
  });
}
