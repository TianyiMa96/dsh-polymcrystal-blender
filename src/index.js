// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * DSH Cordis plugin entry for dsh-plugin-polycrystal-bridge.
 *
 * Shape (zero dependencies, pure ESM):
 *   export function apply(ctx, config)
 *   export const inject = ['tools']
 *
 * Configuration is NOT declared as a Cordis `Config` schema (that would require
 * importing @cordisjs/core / @deepseek-ai/schemastery). Instead all persisted
 * settings live in $POLYCRYSTAL_HOME/config.json, resolved by src/runtime.js.
 */
import { createRuntime, ensureRuntimeDirs } from './runtime.js';
import { startGateway } from './gateway/server.js';
import { createEnvTool } from './tools/polycrystal-env.js';
import { createLoadTool } from './tools/polycrystal-load.js';
import { createModifyTool } from './tools/polycrystal-modify.js';
import { createInspectTool } from './tools/polycrystal-inspect.js';
import { createRenderTool } from './tools/polycrystal-render.js';

export const inject = ['tools'];

export function apply(ctx, config = {}) {
  const runtime = createRuntime(config);

  // Best-effort: create the runtime directory hierarchy on first boot.
  ensureRuntimeDirs(runtime).catch(() => {});

  // Start the dedicated localhost gateway (127.0.0.1:39888). EADDRINUSE is
  // benign: another DSH process already owns it and tools dial it over HTTP.
  startGateway(runtime).catch(err => {
    try { ctx?.logger?.warn?.(`[polycrystal-bridge] gateway: ${err?.message || err}`); } catch {}
  });

  const factories = [
    createEnvTool,
    createLoadTool,
    createModifyTool,
    createInspectTool,
    createRenderTool,
  ];
  for (const factory of factories) {
    ctx.tools.register(factory(runtime, ctx));
  }
}
