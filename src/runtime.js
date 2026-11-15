// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * Shared runtime primitives for dsh-plugin-polycrystal-bridge.
 *
 * Pure ESM, Node 22+, zero npm dependencies. Everything here is a built-in
 * module: path resolution, persisted config.json, the localhost gateway HTTP
 * client, cooperative process spawning, and small JSON/WSL helpers.
 */
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const IS_WIN = process.platform === 'win32';

export const DEFAULT_PORT = 39888;
export const ACTIVE_AGE_MS = 6000;

const DEFAULT_CONFIG = Object.freeze({
  blenderExecutable: null,
  uvExecutable: null,
  preferredBackend: 'native_gn',
  gatewayPort: DEFAULT_PORT,
  claimedInstanceId: null,
  lastStructure: null,
});

export function repoRoot() { return REPO_ROOT; }
export function isWindows() { return IS_WIN; }

/** Resolve the canonical $DSH_HOME / $POLYCRYSTAL_HOME runtime hierarchy. */
export function resolvePaths(overrides = {}) {
  const dshHome = overrides.dshHome || process.env.DSH_HOME || path.join(os.homedir(), '.dsh');
  const polyHome = path.join(dshHome, 'plugins', 'dsh-polycrystal');
  const port = Number(overrides.port || process.env.DSH_POLYCRYSTAL_PORT) || DEFAULT_PORT;
  return {
    dshHome,
    polyHome,
    configPath: path.join(polyHome, 'config.json'),
    venvDir: path.join(polyHome, '.venv'),
    venvPython: path.join(polyHome, '.venv', IS_WIN ? 'Scripts' : 'bin', IS_WIN ? 'python.exe' : 'python'),
    stagingDir: path.join(polyHome, 'staging'),
    capturesDir: path.join(polyHome, 'staging', 'captures'),
    rendersDir: path.join(polyHome, 'staging', 'renders'),
    sessionsDir: path.join(polyHome, 'sessions'),
    repoRoot: REPO_ROOT,
    sidecarDir: path.join(REPO_ROOT, 'sidecar'),
    extensionDir: path.join(REPO_ROOT, 'extension'),
    port,
  };
}

export function createRuntime(overrides = {}) {
  return { paths: resolvePaths(overrides), isWin: IS_WIN, platform: process.platform, arch: process.arch };
}

/* ------------------------------------------------------------------ dirs */

export async function ensureDir(dir) { await fsp.mkdir(dir, { recursive: true }); }

export async function ensureRuntimeDirs(runtime) {
  for (const d of [
    runtime.paths.polyHome,
    runtime.paths.stagingDir,
    runtime.paths.capturesDir,
    runtime.paths.rendersDir,
    runtime.paths.sessionsDir,
  ]) await ensureDir(d);
}

/* ---------------------------------------------------------------- config */

export async function readConfig(runtime) {
  try {
    const parsed = JSON.parse(await fsp.readFile(runtime.paths.configPath, 'utf8'));
    return { ...DEFAULT_CONFIG, ...parsed };
  } catch { return { ...DEFAULT_CONFIG }; }
}

export async function writeConfig(runtime, patch) {
  await ensureDir(path.dirname(runtime.paths.configPath));
  const next = { ...(await readConfig(runtime)), ...patch };
  await fsp.writeFile(runtime.paths.configPath, JSON.stringify(next, null, 2) + '\n', 'utf8');
  return next;
}

/* --------------------------------------------------------------- gateway */

export function gatewayUrl(runtime, pathname) {
  return `http://127.0.0.1:${runtime.paths.port}${pathname}`;
}

export async function gatewayFetch(runtime, pathname, { method = 'GET', body, timeoutMs = 15000, signal } = {}) {
  const controller = new AbortController();
  const onAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener('abort', onAbort, { once: true });
  }
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(gatewayUrl(runtime, pathname), {
      method,
      headers: body === undefined ? {} : { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    const text = await res.text();
    let data;
    try { data = text ? JSON.parse(text) : {}; }
    catch { data = { ok: false, error: 'INVALID_GATEWAY_JSON', raw: text }; }
    return { status: res.status, okHttp: res.ok, data };
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener('abort', onAbort);
  }
}

export async function gatewayRequest(runtime, pathname, body, { timeoutMs = 15000, signal, method } = {}) {
  const hasBody = body !== undefined;
  const { status, okHttp, data } = await gatewayFetch(runtime, pathname, {
    method: method || (hasBody ? 'POST' : 'GET'), body, timeoutMs, signal,
  });
  if (!okHttp) {
    throw new Error(`gateway ${pathname} failed (HTTP ${status}): ${data?.error || data?.message || 'unknown error'}`);
  }
  return data;
}

export async function gatewayHealth(runtime, { timeoutMs = 3000, signal } = {}) {
  try {
    const d = await gatewayRequest(runtime, '/health', undefined, { timeoutMs, signal });
    return { running: true, connected_instances: d.connected_instances ?? 0 };
  } catch { return { running: false, connected_instances: 0 }; }
}

export async function listSessions(runtime, { timeoutMs = 6000, signal } = {}) {
  const d = await gatewayRequest(runtime, '/sessions', undefined, { timeoutMs, signal });
  return Array.isArray(d.sessions) ? d.sessions : (Array.isArray(d) ? d : []);
}

/** Resolve which live Blender GUI instance an RPC should target. */
export async function resolveTargetInstance(runtime, sessionId, { signal } = {}) {
  const sessions = await listSessions(runtime, { signal });
  const cfg = await readConfig(runtime);
  const wanted = sessionId || cfg.claimedInstanceId;
  if (wanted) {
    const found = sessions.find(s => s.instance_id === wanted);
    if (found) return found;
    if (sessionId) {
      throw new Error(`Blender session "${wanted}" is not active. Call polycrystal_env(action="list_sessions") to see active instances.`);
    }
  }
  if (sessions.length === 1) {
    await writeConfig(runtime, { claimedInstanceId: sessions[0].instance_id }).catch(() => {});
    return sessions[0];
  }
  if (sessions.length === 0) {
    throw new Error('No active Blender GUI instance connected to the gateway. Call polycrystal_env(action="launch_gui") first.');
  }
  throw new Error(`${sessions.length} Blender sessions are active; call polycrystal_env(action="claim_session", session_id="...") to choose one.`);
}

/**
 * POST /rpc and suspend until Blender POSTs /rpc-response (or the 15s default
 * gateway timeout). `opts.timeoutMs` is forwarded as `timeout_ms` so long
 * operations (Cycles renders) can request a larger budget.
 */
export async function rpcCall(runtime, instanceId, methodName, params = {}, opts = {}) {
  const requestId = `rpc-${crypto.randomUUID()}`;
  const budget = opts.timeoutMs || 15000;
  const payload = {
    instance_id: instanceId,
    method: methodName,
    params,
    request_id: requestId,
    timeout_ms: budget,
  };
  const data = await gatewayRequest(runtime, '/rpc', payload, { timeoutMs: budget + 5000, signal: opts.signal });
  return { ok: data.ok !== false, request_id: requestId, result: data.result ?? data, error: data.error || null, raw: data };
}

/* --------------------------------------------------------------- process */

/** Spawn a child process with a hard timeout and cooperative abort support. */
export function runProcess(cmd, args = [], opts = {}) {
  const { timeoutMs = 30000, cwd, env, maxBuffer = 32 * 1024 * 1024, signal } = opts;
  return new Promise((resolve) => {
    let child, timer;
    let stdout = '', stderr = '', timedOut = false, settled = false;
    function finish(result) {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', onAbort);
      resolve(result);
    }
    function onAbort() {
      timedOut = true;
      try { if (child) child.kill('SIGKILL'); } catch {}
      finish({ ok: false, code: null, stdout, stderr: stderr + '\n[aborted by caller]', timedOut: true, aborted: true });
    }
    try {
      child = spawn(cmd, args, {
        cwd, env: { ...process.env, ...(env || {}) },
        windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (e) {
      resolve({ ok: false, code: null, stdout: '', stderr: String(e?.message || e), timedOut: false, spawnError: true });
      return;
    }
    timer = setTimeout(() => {
      timedOut = true;
      try { child.kill('SIGKILL'); } catch {}
    }, timeoutMs);
    if (signal) {
      if (signal.aborted) onAbort();
      else signal.addEventListener('abort', onAbort, { once: true });
    }
    child.stdout.on('data', d => { if (stdout.length < maxBuffer) stdout += d.toString(); });
    child.stderr.on('data', d => { if (stderr.length < maxBuffer) stderr += d.toString(); });
    child.on('error', e => finish({ ok: false, code: null, stdout, stderr: stderr + '\n' + String(e?.message || e), timedOut, spawnError: true }));
    child.on('close', code => finish({ ok: code === 0 && !timedOut, code, stdout, stderr, timedOut }));
  });
}

/** Fire-and-forget detached GUI launch (never blocks the tool turn). */
export function launchDetached(cmd, args = [], { cwd, env } = {}) {
  const child = spawn(cmd, args, {
    cwd, env: { ...process.env, ...(env || {}) },
    detached: true, stdio: 'ignore', windowsHide: false,
  });
  child.on('error', () => {});
  child.unref();
  return { pid: child.pid ?? null };
}

/* --------------------------------------------------------------- helpers */

export function sha256(buf) { return crypto.createHash('sha256').update(buf).digest('hex'); }

/** Stable short hash over file content + build parameters -> staging dir name. */
export async function computeStructureHash(runtime, filePath, params = {}) {
  let digest;
  try {
    const data = await fsp.readFile(filePath);
    digest = sha256(Buffer.concat([data, Buffer.from(JSON.stringify(params))]));
  } catch {
    digest = sha256(Buffer.from(`${path.resolve(filePath)}|${JSON.stringify(params)}`));
  }
  return digest.slice(0, 20);
}

export function sleep(ms, signal) {
  return new Promise((resolve) => {
    if (signal?.aborted) return resolve();
    const t = setTimeout(done, ms);
    function done() { clearTimeout(t); if (signal) signal.removeEventListener('abort', done); resolve(); }
    if (signal) signal.addEventListener('abort', done, { once: true });
  });
}

export async function pollFor(fn, { intervalMs = 500, timeoutMs = 10000, signal } = {}) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (signal?.aborted) return null;
    const v = await fn().catch(() => null);
    if (v) return v;
    await sleep(intervalMs, signal);
  }
  return null;
}

export function isWsl2() {
  if (process.platform !== 'linux') return false;
  if (process.env.WSL_DISTRO_NAME || process.env.WSL_INTEROP) return true;
  try { return /microsoft/i.test(fs.readFileSync('/proc/version', 'utf8')); } catch { return false; }
}

/** Translate a staging path for a Windows Blender GUI driven from WSL2. */
export async function toBlenderPath(runtime, p) {
  if (!isWsl2()) return p;
  const r = await runProcess('wslpath', ['-w', p], { timeoutMs: 5000 });
  return r.ok && r.stdout.trim() ? r.stdout.trim() : p;
}

export async function findUv(runtime) {
  const cfg = await readConfig(runtime);
  const candidates = [];
  if (process.env.UV) candidates.push({ p: process.env.UV, s: 'env:UV' });
  if (cfg.uvExecutable) candidates.push({ p: cfg.uvExecutable, s: 'config.json' });
  candidates.push({ p: path.join(os.homedir(), '.local', 'bin', IS_WIN ? 'uv.exe' : 'uv'), s: '~/.local/bin' });
  candidates.push({ p: path.join(os.homedir(), '.cargo', 'bin', IS_WIN ? 'uv.exe' : 'uv'), s: '~/.cargo/bin' });
  for (const c of candidates) {
    try { await fsp.access(c.p, fs.constants.X_OK); return { path: c.p, source: c.s }; } catch {}
  }
  const cmd = IS_WIN ? 'where.exe' : 'which';
  const r = await runProcess(cmd, ['uv'], { timeoutMs: 4000 });
  if (r.ok) {
    const line = r.stdout.split(/\r?\n/).map(s => s.trim()).find(Boolean);
    if (line) return { path: line, source: cmd };
  }
  return null;
}

export async function findSystemPython(runtime) {
  if (IS_WIN) {
    const r = await runProcess('py', ['-3', '--version'], { timeoutMs: 5000 });
    if (r.ok) return { command: 'py', args: ['-3'], version: r.stdout.trim(), source: 'py -3' };
    const r2 = await runProcess('python', ['--version'], { timeoutMs: 5000 });
    if (r2.ok) return { command: 'python', args: [], version: r2.stdout.trim(), source: 'python' };
    return null;
  }
  for (const c of ['python3', 'python']) {
    const r = await runProcess(c, ['--version'], { timeoutMs: 5000 });
    if (r.ok) return { command: c, args: [], version: r.stdout.trim(), source: c };
  }
  return null;
}

export function normalizeTriple(value, fallback, { integers = false } = {}) {
  const arr = (Array.isArray(value) ? value.slice(0, 3) : fallback.slice());
  while (arr.length < 3) arr.push(fallback[arr.length]);
  return arr.map((v, i) => {
    let n = Number(v);
    if (!Number.isFinite(n)) n = fallback[i];
    if (integers) n = Math.max(1, Math.round(n));
    return n;
  });
}

/** Extract the last JSON value printed by the sidecar CLI on stdout. */
export function parseJsonLoose(text) {
  if (!text) return null;
  try { return JSON.parse(text.trim()); } catch {}
  for (let i = text.length - 1; i >= 0; i--) {
    const ch = text[i];
    if (ch !== '{' && ch !== '[') continue;
    try { return JSON.parse(text.slice(i).trim()); } catch {}
  }
  const lines = text.split(/\r?\n/);
  for (let i = lines.length - 1; i >= 0; i--) {
    const l = lines[i].trim();
    if (!l) continue;
    try { return JSON.parse(l); } catch {}
  }
  return null;
}

/** Coerce a sidecar output field (string path or nested object) to a path. */
export function asPath(value) {
  if (typeof value === 'string' && value) return value;
  if (value && typeof value === 'object') {
    return value.npz || value.bundle || value.path || value.file || value.manifest || null;
  }
  return null;
}

/** Canonical value -> model-facing text content block. */
export function textContent(value) {
  let s;
  try { s = typeof value === 'string' ? value : JSON.stringify(value, null, 2); }
  catch { s = String(value); }
  return [{ type: 'text', text: s }];
}
