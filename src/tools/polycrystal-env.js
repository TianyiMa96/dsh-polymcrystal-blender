// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * Tool 1: polycrystal_env
 * Environment doctor, sidecar auto-setup, non-destructive Blender extension
 * installation, detached GUI launch/attach, and gateway session claiming.
 */
import { defineTool } from '@deepseek-ai/dsh-tools';
import {
  textContent, readConfig, writeConfig, gatewayHealth,
  launchDetached, pollFor, isWsl2, gatewayRequest,
} from '../runtime.js';
import {
  detectEnvironment, queryLiveSessions, findBlenderExecutable,
  findExtensionArtifact, installExtensionCommand, buildExtensionZip,
} from '../env/detector.js';
import { ensureVenv } from '../env/sidecar-venv.js';

export function createEnvTool(runtime) {
  return defineTool({
    name: 'polycrystal_env',
    description:
      'Prove and prepare the DSH x Blender polymer-crystal bridge. Actions: doctor (full environment probe), '
      + 'setup_sidecar (build isolated .venv), install_blender_ext (non-destructive CLI install), '
      + 'launch_gui (detached interactive Blender GUI that auto-connects), list_sessions (active GUI instances), '
      + 'claim_session (choose which live instance subsequent tools drive).',
    parameters: {
      action: {
        type: 'string', required: true,
        enum: ['doctor', 'setup_sidecar', 'install_blender_ext', 'launch_gui', 'list_sessions', 'claim_session'],
        description: 'Which environment operation to perform.',
      },
      session_id: {
        type: 'string',
        description: 'For claim_session: the instance_id to claim. Omit to auto-claim the first active session.',
      },
    },
    output: { schema: { type: 'json' }, render: (_args, value) => textContent(value) },
    timeoutMs: 600000,
    async execute(args, exec) {
      const signal = exec?.signal;
      switch (args.action) {
        case 'doctor': return actionDoctor(runtime, { signal });
        case 'setup_sidecar': return actionSetupSidecar(runtime, { signal });
        case 'install_blender_ext': return actionInstallExt(runtime, { signal });
        case 'launch_gui': return actionLaunchGui(runtime, { signal });
        case 'list_sessions': return actionListSessions(runtime, { signal });
        case 'claim_session': return actionClaimSession(runtime, args.session_id, { signal });
        default: throw new Error(`polycrystal_env: unknown action "${args.action}"`);
      }
    },
  });
}

async function actionDoctor(runtime, { signal }) {
  return detectEnvironment(runtime, { signal, includeSidecarDoctor: true });
}

async function actionSetupSidecar(runtime, { signal }) {
  const res = await ensureVenv(runtime, { timeoutMs: 600000, signal });
  return {
    ok: res.ok,
    action: 'setup_sidecar',
    provisioned: res.provisioned,
    python: runtime.paths.venvPython,
    venv_dir: runtime.paths.venvDir,
    doctor: res.doctor,
    provision: res.provision
      ? { ok: res.provision.ok, error: res.provision.error || null, phases: (res.provision.steps || []).map(s => s.phase) }
      : null,
    message: res.ok
      ? 'Sidecar .venv is ready and `python -m polycrystal_core.cli doctor` returned {"ok":true}.'
      : 'Sidecar provisioning/health check failed; inspect doctor and provision fields for the cause.',
  };
}

async function actionInstallExt(runtime, { signal }) {
  const found = await findBlenderExecutable(runtime, { signal });
  if (!found.executable) {
    return { ok: false, action: 'install_blender_ext', error: 'BLENDER_NOT_FOUND', message: 'No Blender executable discovered; run doctor first.' };
  }
  if (!found.version_ok) {
    return { ok: false, action: 'install_blender_ext', error: 'BLENDER_TOO_OLD', blender: found.executable, version: found.version };
  }
  const artifact = await findExtensionArtifact(runtime);
  let zip = artifact.zip;
  let build = null;
  if (!zip && artifact.sourceDir) {
    build = await buildExtensionZip(runtime, found.executable, artifact.sourceDir, { timeoutMs: 120000, signal });
    zip = build.zip;
  }
  if (!zip) {
    return {
      ok: false, action: 'install_blender_ext', error: 'EXTENSION_ARTIFACT_NOT_FOUND',
      searched: [
        `${runtime.paths.extensionDir}/dist/dsh_polycrystal_ext.zip`,
        `${runtime.paths.extensionDir}/dsh_polycrystal_ext.zip`,
        `${runtime.paths.extensionDir}/dsh_polycrystal_ext/blender_manifest.toml`,
      ],
      build,
      message: 'Package the extension zip (blender --command extension build) or place it under extension/dist/.',
    };
  }
  const install = await installExtensionCommand(runtime, found.executable, zip, { timeoutMs: 180000, signal });
  return {
    ok: install.ok,
    action: 'install_blender_ext',
    blender: found.executable,
    version: found.version,
    zip,
    built_from_source: !!build,
    build: build ? { ok: build.ok, command: build.command, stderr: build.stderr.slice(0, 2000) } : null,
    command: install.command,
    stdout: install.stdout.slice(0, 4000),
    stderr: install.stderr.slice(0, 4000),
    extension_dir: found.extension_dir,
    message: install.ok
      ? 'Extension installed into Blender user_default; restart an open GUI to load it.'
      : 'Extension install command failed; see stdout/stderr.',
  };
}

async function actionLaunchGui(runtime, { signal }) {
  const found = await findBlenderExecutable(runtime, { signal });
  if (!found.executable) {
    return { ok: false, action: 'launch_gui', error: 'BLENDER_NOT_FOUND', message: 'No Blender executable discovered; run doctor first.' };
  }
  const pythonExpr = 'import dsh_polycrystal_ext; dsh_polycrystal_ext.auto_connect()';
  const args = ['--python-expr', pythonExpr];
  const env = { DSH_POLYCRYSTAL_PORT: String(runtime.paths.port) };
  let launched;
  try {
    launched = launchDetached(found.executable, args, { env });
  } catch (e) {
    return { ok: false, action: 'launch_gui', error: 'SPAWN_FAILED', message: String(e?.message || e) };
  }

  const before = new Set((await queryLiveSessions(runtime, { signal })).map(s => s.instance_id));
  const verified = await pollFor(async () => {
    const now = await queryLiveSessions(runtime, { signal });
    return now.find(s => !before.has(s.instance_id)) || null;
  }, { intervalMs: 750, timeoutMs: 12000, signal });

  return {
    ok: true,
    action: 'launch_gui',
    launched: true,
    pid: launched.pid,
    binary_path: found.executable,
    port: runtime.paths.port,
    python_expr: pythonExpr,
    wsl2: isWsl2(),
    session_registered: !!verified,
    session: verified || null,
    hint: verified
      ? 'GUI registered and heartbeating. Call claim_session (or just load a structure).'
      : 'GUI spawned but no heartbeat seen yet; re-check with polycrystal_env(action="list_sessions").',
  };
}

async function actionListSessions(runtime, { signal }) {
  const health = await gatewayHealth(runtime, { signal });
  const sessions = await queryLiveSessions(runtime, { signal });
  const cfg = await readConfig(runtime);
  return {
    ok: true,
    action: 'list_sessions',
    gateway: health,
    count: sessions.length,
    sessions,
    claimed_instance_id: cfg.claimedInstanceId,
  };
}

async function actionClaimSession(runtime, sessionId, { signal }) {
  const sessions = await queryLiveSessions(runtime, { signal });
  if (!sessions.length) {
    return {
      ok: false, action: 'claim_session', error: 'NO_ACTIVE_SESSIONS',
      message: 'No live Blender GUI instance; call launch_gui first.',
    };
  }
  const target = sessionId ? sessions.find(s => s.instance_id === sessionId) : sessions[0];
  if (!target) {
    return {
      ok: false, action: 'claim_session', error: 'SESSION_NOT_ACTIVE', session_id: sessionId,
      available: sessions.map(s => ({ instance_id: s.instance_id, age_ms: s.age_ms, pid: s.pid })),
    };
  }
  await writeConfig(runtime, { claimedInstanceId: target.instance_id });
  await gatewayRequest(runtime, '/claim', { instance_id: target.instance_id }, { timeoutMs: 4000, signal }).catch(() => null);
  return {
    ok: true, action: 'claim_session', claimed: target,
    message: `Session ${target.instance_id} is now the AI-claimed target for load/modify/inspect/render.`,
  };
}
