// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * Tool 4: polycrystal_inspect
 * Read back the live human UI tweaks from the Blender PolyCrystal N-panel,
 * AI-edit lock status, selection, camera transform, and an optional viewport
 * PNG capture (persisted to staging for DSH visual inspection).
 */
import fsp from 'node:fs/promises';
import path from 'node:path';
import { defineTool } from '@deepseek-ai/dsh-tools';
import { textContent, rpcCall, resolveTargetInstance, ensureDir } from '../runtime.js';

export function createInspectTool(runtime) {
  return defineTool({
    name: 'polycrystal_inspect',
    description:
      `Read the current live Blender GUI state before making edits: every N-panel slider value, `
      + `allow_ai_edits / camera & material locks, selected objects and chain ids, camera transform, `
      + `and an optional viewport PNG screenshot. Always call this before applying edits on a new turn.`,
    parameters: {
      include_viewport_capture: {
        type: 'boolean', default: true,
        description: 'Request a viewport PNG screenshot so the AI sees what the human sees (default true).',
      },
      session_id: { type: 'string', description: 'Target Blender instance_id (defaults to claimed / only active).' },
    },
    output: { schema: { type: 'json' }, render: (_args, value) => textContent(value) },
    timeoutMs: 120000,
    async execute(args, exec) { return runInspect(runtime, args, exec); },
  });
}

async function runInspect(runtime, args, exec) {
  const signal = exec?.signal;
  const include = args.include_viewport_capture !== false;
  const instance = await resolveTargetInstance(runtime, args.session_id, { signal });

  const rpc = await rpcCall(runtime, instance.instance_id, 'polycrystal.inspect', {
    include_viewport_capture: include,
  }, { timeoutMs: 90000, signal });
  if (!rpc.ok) {
    return { ok: false, error: 'RPC_INSPECT_FAILED', instance_id: instance.instance_id, rpc };
  }

  const state = rpc.result || {};
  let capturePath = null;
  const b64 = state.viewport_capture_base64
    || state.viewport_png_base64
    || state.screenshot_base64
    || state.capture_base64;
  if (typeof b64 === 'string' && b64.length) {
    await ensureDir(runtime.paths.capturesDir);
    capturePath = path.join(runtime.paths.capturesDir, `viewport_${Date.now()}.png`);
    const clean = b64.replace(/^data:image\/[a-z]+;base64,/i, '');
    try { await fsp.writeFile(capturePath, Buffer.from(clean, 'base64')); }
    catch { capturePath = null; }
  }

  return {
    ok: true,
    instance_id: instance.instance_id,
    include_viewport_capture: include,
    viewport_capture_path: capturePath || state.viewport_capture_path || state.image_path || null,
    // Convenience projections of the fields the AI most often needs.
    allow_ai_edits: state.allow_ai_edits ?? null,
    lock_camera_and_lights: state.lock_camera_and_lights ?? state.locks?.lock_camera_and_lights ?? null,
    lock_custom_materials: state.lock_custom_materials ?? state.locks?.lock_custom_materials ?? null,
    hide_hydrogens: state.hide_hydrogens ?? state.sliders?.hide_hydrogens ?? null,
    selected_chain_ids: state.selected_chain_ids ?? state.selection?.chain_ids ?? null,
    selected_objects: state.selected_objects ?? state.selection?.objects ?? null,
    camera: state.camera ?? state.camera_transform ?? null,
    sliders: state.sliders ?? state.n_panel ?? null,
    state,
    message: 'Live state read back. Respect allow_ai_edits and camera/material locks before applying further edits.',
  };
}
