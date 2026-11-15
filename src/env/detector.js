// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * Cross-platform Blender / Python / uv discovery engine (agent.md 2.2).
 *
 * Probe waterfall, in strict order and without failing silently:
 *   1. Live connected GUI instance (gateway /sessions, age_ms < 6000)
 *   2. BLENDER_EXECUTABLE env var
 *   3. $POLYCRYSTAL_HOME/config.json -> blenderExecutable
 *   4. OS standard locations (Windows registry / Program Files / Scoop /
 *      where.exe; macOS /Applications; Linux which/snap; WSL2 /mnt/c probe)
 *   5. Version probe `blender --version` (>= 4.2.0) + extension readiness
 */
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {
  runProcess, listSessions, readConfig, gatewayHealth, isWsl2, findUv, findSystemPython,
  ACTIVE_AGE_MS,
} from '../runtime.js';
import { sidecarDoctor } from './sidecar-venv.js';

export const MIN_BLENDER = [4, 2, 0];

/* -------------------------------------------------------------- versions */

export function parseBlenderVersion(text) {
  const m = String(text || '').match(/Blender\s+(\d+)\.(\d+)(?:\.(\d+))?/i);
  if (!m) return null;
  const major = Number(m[1]);
  const minor = Number(m[2]);
  const patch = Number(m[3] || 0);
  const ok = major > MIN_BLENDER[0]
    || (major === MIN_BLENDER[0] && (minor > MIN_BLENDER[1]
      || (minor === MIN_BLENDER[1] && patch >= MIN_BLENDER[2])));
  return { version: `${major}.${minor}.${patch}`, major, minor, patch, ok };
}

export async function probeVersion(exe, { timeoutMs = 5000, signal } = {}) {
  const r = await runProcess(exe, ['--version'], { timeoutMs, signal });
  if (!r.ok) {
    return {
      ok: false, stdout: r.stdout, stderr: r.stderr,
      error: r.timedOut ? 'VERSION_PROBE_TIMEOUT' : 'VERSION_PROBE_FAILED',
      version: null, version_ok: false,
    };
  }
  const v = parseBlenderVersion(r.stdout);
  return {
    ok: true, stdout: r.stdout, stderr: r.stderr,
    version: v, version_ok: v ? v.ok : false,
    first_line: r.stdout.split(/\r?\n/)[0] || null,
  };
}

/* --------------------------------------------------------- OS candidates */

function compareVersionDir(a, b) {
  const nums = s => (String(s).match(/\d+/g) || []).map(Number);
  const na = nums(a), nb = nums(b);
  for (let i = 0; i < Math.max(na.length, nb.length); i++) {
    const d = (nb[i] || 0) - (na[i] || 0);
    if (d) return d;
  }
  return 0;
}

function windowsBlenderCandidates() {
  const out = [];
  const roots = [
    process.env.ProgramFiles,
    process.env['ProgramFiles(x86)'],
    'C:\\Program Files',
    'C:\\Program Files (x86)',
  ].filter(Boolean);
  for (const root of roots) {
    const dir = path.join(root, 'Blender Foundation');
    try {
      const names = fs.readdirSync(dir).filter(n => /^Blender [45]\./i.test(n));
      names.sort(compareVersionDir);
      for (const n of names) out.push(path.join(dir, n, 'blender.exe'));
    } catch {}
  }
  // Scoop / Winget / Steam common paths.
  out.push(path.join(os.homedir(), 'scoop', 'apps', 'blender', 'current', 'blender.exe'));
  out.push(path.join(os.homedir(), 'AppData', 'Local', 'Programs', 'Blender Foundation', 'Blender 4.2', 'blender.exe'));
  out.push('C:\\Program Files (x86)\\Steam\\steamapps\\common\\Blender\\blender.exe');
  return out;
}

function macBlenderCandidates() {
  return [
    '/Applications/Blender.app/Contents/MacOS/Blender',
    path.join(os.homedir(), 'Applications', 'Blender.app', 'Contents', 'MacOS', 'Blender'),
    '/opt/homebrew/bin/blender',
    '/usr/local/bin/blender',
  ];
}

function linuxBlenderCandidates() {
  const out = ['/usr/bin/blender', '/snap/bin/blender', '/usr/local/bin/blender'];
  const homeApps = path.join(os.homedir(), 'apps');
  try {
    for (const n of fs.readdirSync(homeApps).filter(n => /^blender-/i.test(n))) {
      out.push(path.join(homeApps, n, 'blender'));
    }
  } catch {}
  return out;
}

/** WSL2 best-effort: probe the Windows-side Blender install via /mnt/c. */
function wslWindowsBlenderCandidates() {
  const out = [];
  const base = '/mnt/c/Program Files/Blender Foundation';
  try {
    const names = fs.readdirSync(base).filter(n => /^Blender [45]\./i.test(n));
    names.sort(compareVersionDir);
    for (const n of names) out.push(path.join(base, n, 'blender.exe'));
  } catch {}
  return out;
}

async function whereBlender() {
  const cmd = process.platform === 'win32' ? 'where.exe' : 'which';
  const r = await runProcess(cmd, ['blender'], { timeoutMs: 4000 });
  if (!r.ok) return null;
  return r.stdout.split(/\r?\n/).map(s => s.trim()).find(Boolean) || null;
}

async function registryBlender() {
  if (process.platform !== 'win32') return null;
  const r = await runProcess('reg.exe', ['query', 'HKLM\\SOFTWARE\\BlenderFoundation\\blender', '/s'], { timeoutMs: 4000 });
  if (!r.ok) return null;
  const matches = r.stdout.match(/[A-Za-z]:[^\r\n]*blender\.exe/gi);
  return matches && matches.length ? matches[matches.length - 1] : null;
}

/* ------------------------------------------------------ extension paths */

export function extensionDirFor(major, minor) {
  const ver = `${major}.${minor}`;
  if (process.platform === 'win32') {
    const appdata = process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming');
    return path.join(appdata, 'Blender Foundation', 'Blender', ver, 'extensions', 'user_default', 'dsh_polycrystal_ext');
  }
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', 'Blender', ver, 'extensions', 'user_default', 'dsh_polycrystal_ext');
  }
  return path.join(os.homedir(), '.config', 'blender', ver, 'extensions', 'user_default', 'dsh_polycrystal_ext');
}

export function isExtensionInstalled(major, minor) {
  if (!major) return false;
  try { return fs.existsSync(extensionDirFor(major, minor)); } catch { return false; }
}

/** Locate the packaged extension zip, or the source dir to build one from. */
export async function findExtensionArtifact(runtime) {
  const dist = path.join(runtime.paths.extensionDir, 'dist');
  const candidates = [
    path.join(dist, 'dsh_polycrystal_ext.zip'),
    path.join(runtime.paths.extensionDir, 'dsh_polycrystal_ext.zip'),
  ];
  for (const p of candidates) {
    try { await fsp.access(p); return { zip: p, sourceDir: null }; } catch {}
  }
  const sourceDir = path.join(runtime.paths.extensionDir, 'dsh_polycrystal_ext');
  try {
    await fsp.access(path.join(sourceDir, 'blender_manifest.toml'));
    return { zip: null, sourceDir };
  } catch { return { zip: null, sourceDir: null }; }
}

/** Blender 4.2+ official non-destructive extension install command. */
export async function installExtensionCommand(runtime, exe, zip, { timeoutMs = 180000, signal } = {}) {
  const args = ['--background', '--command', 'extension', 'install-file', '-r', 'user_default', '-e', zip];
  const r = await runProcess(exe, args, { timeoutMs, signal, cwd: runtime.paths.extensionDir });
  return {
    ok: r.ok, command: `${exe} ${args.join(' ')}`, code: r.code, timedOut: r.timedOut,
    stdout: r.stdout, stderr: r.stderr,
  };
}

/** Fallback: build the zip from extension source via headless Blender. */
export async function buildExtensionZip(runtime, exe, sourceDir, { timeoutMs = 120000, signal } = {}) {
  const outDir = path.join(runtime.paths.extensionDir, 'dist');
  await fsp.mkdir(outDir, { recursive: true });
  const args = ['--background', '--command', 'extension', 'build', '--source-dir', sourceDir, '--output-dir', outDir];
  const r = await runProcess(exe, args, { timeoutMs, signal, cwd: runtime.paths.extensionDir });
  const zip = path.join(outDir, 'dsh_polycrystal_ext.zip');
  let zipExists = false;
  try { await fsp.access(zip); zipExists = true; } catch {}
  return { ok: r.ok && zipExists, zip: zipExists ? zip : null, command: `${exe} ${args.join(' ')}`, stdout: r.stdout, stderr: r.stderr };
}

/* --------------------------------------------------------- live sessions */

export async function queryLiveSessions(runtime, { signal } = {}) {
  try { return await listSessions(runtime, { signal, timeoutMs: 6000 }); }
  catch { return []; }
}

function checkAddon(live, name) {
  const addons = live?.installed_addons || live?.addons || {};
  return !!(addons[name] || (Array.isArray(live?.addon_names) && live.addon_names.includes(name)));
}

/* ---------------------------------------------------- discovery waterfall */

export async function findBlenderExecutable(runtime, { signal, timeoutMs = 5000 } = {}) {
  const tried = [];
  const candidates = [];

  // (2) explicit env var
  if (process.env.BLENDER_EXECUTABLE) {
    candidates.push({ path: process.env.BLENDER_EXECUTABLE, source: 'env:BLENDER_EXECUTABLE' });
  }
  // (3) persisted config.json
  const cfg = await readConfig(runtime);
  if (cfg.blenderExecutable) {
    candidates.push({ path: cfg.blenderExecutable, source: 'config.json' });
  }
  // (4) OS-specific standard locations
  if (process.platform === 'win32') {
    for (const p of windowsBlenderCandidates()) candidates.push({ path: p, source: 'windows:standard' });
  } else if (process.platform === 'darwin') {
    for (const p of macBlenderCandidates()) candidates.push({ path: p, source: 'macos:standard' });
  } else {
    for (const p of linuxBlenderCandidates()) candidates.push({ path: p, source: 'linux:standard' });
  }
  if (isWsl2()) {
    for (const p of wslWindowsBlenderCandidates()) candidates.push({ path: p, source: 'wsl2:windows-blender' });
  }

  let found = null;
  for (const c of candidates) {
    if (!c.path) continue;
    try { await fsp.access(c.path, fs.constants.X_OK); found = c; break; }
    catch { tried.push({ path: c.path, source: c.source, reason: 'missing-or-not-executable' }); }
  }

  // (4b) where.exe / which, then Windows registry
  if (!found) {
    const w = await whereBlender();
    if (w) {
      try { await fsp.access(w, fs.constants.X_OK); found = { path: w, source: 'where.exe/which' }; }
      catch { tried.push({ path: w, source: 'where.exe/which', reason: 'not-executable' }); }
    }
  }
  if (!found && process.platform === 'win32') {
    const rp = await registryBlender();
    if (rp) {
      try { await fsp.access(rp, fs.constants.X_OK); found = { path: rp, source: 'windows:registry' }; }
      catch { tried.push({ path: rp, source: 'windows:registry', reason: 'not-executable' }); }
    }
  }

  if (!found) {
    return {
      executable: null, source: null, tried,
      version: null, version_string: null, version_ok: false,
      extension_installed: false, extension_dir: null, probe_error: 'BLENDER_NOT_FOUND',
    };
  }

  const probe = await probeVersion(found.path, { timeoutMs, signal });
  const v = probe.version;
  const extDir = v ? extensionDirFor(v.major, v.minor) : null;
  return {
    executable: found.path,
    source: found.source,
    tried,
    version: v ? v.version : null,
    version_string: probe.first_line || null,
    version_ok: probe.version_ok,
    probe_error: probe.error || null,
    extension_installed: extDir ? isExtensionInstalled(v.major, v.minor) : false,
    extension_dir: extDir,
  };
}

/** Full environment doctor report (never throws; surfaces issues explicitly). */
export async function detectEnvironment(runtime, { signal, includeSidecarDoctor = true } = {}) {
  const report = {
    ok: false,
    action: 'doctor',
    timestamp: new Date().toISOString(),
    platform: process.platform,
    arch: process.arch,
    wsl2: isWsl2(),
    paths: {
      dshHome: runtime.paths.dshHome,
      polyHome: runtime.paths.polyHome,
      configPath: runtime.paths.configPath,
      venvDir: runtime.paths.venvDir,
      stagingDir: runtime.paths.stagingDir,
    },
    gateway: { port: runtime.paths.port, running: false, connected_instances: 0, sessions: [] },
    live_instance: null,
    blender: null,
    sidecar: { venv_ready: false, python: runtime.paths.venvPython, doctor: null },
    uv: null,
    python: null,
    issues: [],
    recommendations: [],
  };

  // (1) Live connected GUI first.
  const health = await gatewayHealth(runtime, { signal });
  report.gateway.running = health.running;
  report.gateway.connected_instances = health.connected_instances;
  const sessions = await queryLiveSessions(runtime, { signal });
  report.gateway.sessions = sessions;
  const live = sessions.find(s => s.age_ms < ACTIVE_AGE_MS) || sessions[0] || null;

  // (2)-(5) full discovery waterfall.
  const discovered = await findBlenderExecutable(runtime, { signal });
  let blender = { ...discovered, installed_addons: {}, pid: null, open_blend_file: null };
  if (live) {
    report.live_instance = live;
    blender = {
      ...discovered,
      source: 'live_instance',
      executable: live.binary_path || discovered.executable,
      version: live.version_string || live.version || discovered.version,
      version_ok: discovered.version_ok,
      pid: live.pid,
      open_blend_file: live.open_blend_file || live.filepath || null,
      installed_addons: live.installed_addons || live.addons || {},
      extension_installed: checkAddon(live, 'dsh_polycrystal_ext') || discovered.extension_installed,
    };
    if (live.binary_path && live.binary_path !== discovered.executable) {
      const p = await probeVersion(live.binary_path, { timeoutMs: 5000, signal });
      if (p.version) { blender.version = p.version.version; blender.version_ok = p.version_ok; }
    }
  }
  report.blender = blender;

  // Python / uv / sidecar venv.
  report.uv = await findUv(runtime);
  report.python = await findSystemPython(runtime);
  const venvPresent = fs.existsSync(runtime.paths.venvPython);
  report.sidecar.venv_ready = venvPresent;
  if (venvPresent && includeSidecarDoctor) {
    report.sidecar.doctor = await sidecarDoctor(runtime, { timeoutMs: 30000, signal });
    report.sidecar.venv_ready = !!report.sidecar.doctor.ok;
  }

  // Issues + non-silent recommendations.
  if (!health.running) {
    report.issues.push(`Gateway 127.0.0.1:${runtime.paths.port} is not reachable; the plugin host may not have started it yet.`);
  }
  if (!live) {
    report.issues.push('No live Blender GUI instance is heartbeating on the gateway.');
    report.recommendations.push('Call polycrystal_env(action="launch_gui") to start a detached interactive Blender GUI.');
  }
  if (!blender.executable) {
    report.issues.push('Blender executable was not found by env var, config.json, standard locations, or where.exe.');
    report.recommendations.push('Set BLENDER_EXECUTABLE, or install Blender 4.2+ and re-run doctor.');
  } else if (!blender.version_ok) {
    report.issues.push(`Blender at ${blender.executable} reports version ${blender.version || 'unknown'}, need >= 4.2.0.`);
  }
  if (blender.executable && !blender.extension_installed) {
    report.issues.push('The dsh_polycrystal_ext extension is not installed in Blender user extensions.');
    report.recommendations.push('Call polycrystal_env(action="install_blender_ext") for a non-destructive CLI install.');
  }
  if (!report.uv && !report.python) {
    report.issues.push('Neither uv nor a system python (>=3.11) was found for the isolated sidecar venv.');
  }
  if (!report.sidecar.venv_ready) {
    report.issues.push('Isolated sidecar .venv is not ready (doctor did not return {"ok":true}).');
    report.recommendations.push('Call polycrystal_env(action="setup_sidecar") to provision uv/python venv and pip install -e sidecar.');
  }
  report.ok = !!blender.executable && !!blender.version_ok && !!live && report.gateway.running && report.sidecar.venv_ready;
  return report;
}
