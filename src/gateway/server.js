// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * Localhost HTTP gateway for live Blender GUI instances.
 *
 * Listens on 127.0.0.1:39888 by default (DSH_POLYCRYSTAL_PORT overrides).
 * NEVER listens on 9876 (reserved by competing generic MCP bridges) and does
 * not touch 39877 (sixtysevenlf/dsh-blender-plugin).
 *
 * Endpoints:
 *   GET  /health        -> { ok:true, connected_instances:N }
 *   POST /register      -> { ok:true, instance_id }   (Blender dials in)
 *   POST /heartbeat     -> { ok:true }                (every 2s)
 *   POST /rpc           -> suspends until /rpc-response, 15s default timeout
 *   POST /rpc-response  -> delivers a Blender-side JSON-RPC result
 *   GET  /sessions      -> active instances (age_ms < 6000)
 *   GET|POST /rpc-pending (alias /rpc-poll) -> long-poll queue for Blender
 *   POST /claim         -> mark an instance as the AI-claimed one
 *   POST /event         -> ack-only channel for extension events
 */
import http from 'node:http';
import fsp from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { SessionRegistry, publicRecord, ACTIVE_AGE_MS } from './session-registry.js';

const MAX_BODY = 4 * 1024 * 1024;
const RPC_DEFAULT_TIMEOUT = 15000;
const RPC_MAX_TIMEOUT = 15 * 60 * 1000;
const POLL_DEFAULT_WAIT = 5000;
const POLL_MAX_WAIT = 30000;

const pendingRpcs = new Map();   // request_id -> { instance_id, finish(result) }
const rpcQueues = new Map();     // instance_id -> [{ request_id, method, params }]
const pollWaiters = new Map();   // instance_id -> Set<{ resolve, timer }>

let state = { server: null, registry: null, port: null, starting: null, reused: false, error: null };

export function getGatewayState() { return state; }
export function getRegistry() { return state.registry; }

function clamp(n, lo, hi) { return Math.min(hi, Math.max(lo, n)); }

/** Start the gateway once. EADDRINUSE means another DSH process owns it. */
export function startGateway(runtime, options = {}) {
  if (state.reused) return Promise.resolve(state);
  if (state.server && state.server.listening) return Promise.resolve(state);
  if (state.starting) return state.starting;

  state.starting = new Promise((resolve) => {
    const registry = options.registry || new SessionRegistry({ staleMs: ACTIVE_AGE_MS });
    const server = http.createServer((req, res) => {
      handleRequest(req, res, runtime, registry).catch(err => {
        sendJson(res, 500, { ok: false, error: 'INTERNAL_ERROR', message: String(err?.message || err) });
      });
    });
    server.on('error', (err) => {
      if (err.code === 'EADDRINUSE') {
        state = { server: null, registry: null, port: runtime.paths.port, starting: null, reused: true, error: null };
      } else {
        state = { server: null, registry: null, port: runtime.paths.port, starting: null, reused: false, error: err.message };
      }
      resolve(state);
    });
    server.listen(runtime.paths.port, '127.0.0.1', () => {
      state = { server, registry, port: runtime.paths.port, starting: null, reused: false, error: null };
      const prune = setInterval(() => { registry.prune(); }, 3000);
      if (typeof prune.unref === 'function') prune.unref();
      server.on('close', () => clearInterval(prune));
      resolve(state);
    });
  });
  return state.starting;
}

export function stopGateway() {
  if (state.server) { try { state.server.close(); } catch {} }
  state = { server: null, registry: null, port: null, starting: null, reused: false, error: null };
}

async function handleRequest(req, res, runtime, registry) {
  const url = new URL(req.url, `http://127.0.0.1:${runtime.paths.port}`);
  const pathname = url.pathname;
  const method = req.method || 'GET';
  res.setHeader('content-type', 'application/json; charset=utf-8');
  res.setHeader('access-control-allow-origin', '*');
  res.setHeader('access-control-allow-headers', 'content-type');

  if (method === 'OPTIONS') return sendJson(res, 204, {});

  if (pathname === '/health' && method === 'GET') {
    return sendJson(res, 200, { ok: true, connected_instances: registry.countActive() });
  }

  if (pathname === '/sessions' && method === 'GET') {
    const sessions = registry.list({ activeOnly: true });
    return sendJson(res, 200, { ok: true, count: sessions.length, sessions });
  }

  if (pathname === '/register' && method === 'POST') {
    const body = await readBody(req);
    if (!body || typeof body !== 'object') return sendJson(res, 400, { ok: false, error: 'INVALID_BODY' });
    const id = registry.register(body);
    await writeSessionFile(runtime, registry.get(id)).catch(() => {});
    return sendJson(res, 200, { ok: true, instance_id: id, heartbeat_interval_ms: 2000 });
  }

  if (pathname === '/heartbeat' && method === 'POST') {
    const body = await readBody(req);
    const id = body?.instance_id || body?.id;
    if (!id) return sendJson(res, 400, { ok: false, error: 'MISSING_INSTANCE_ID' });
    const ok = registry.heartbeat(id, body);
    if (!ok) return sendJson(res, 404, { ok: false, error: 'UNKNOWN_INSTANCE', instance_id: id });
    await writeSessionFile(runtime, registry.get(id)).catch(() => {});
    return sendJson(res, 200, { ok: true, instance_id: id });
  }

  if (pathname === '/claim' && method === 'POST') {
    const body = await readBody(req);
    const id = body?.instance_id || body?.id;
    if (!id) return sendJson(res, 400, { ok: false, error: 'MISSING_INSTANCE_ID' });
    const rec = registry.claim(id);
    if (!rec) return sendJson(res, 404, { ok: false, error: 'UNKNOWN_INSTANCE', instance_id: id });
    return sendJson(res, 200, { ok: true, claimed: rec });
  }

  if (pathname === '/rpc' && method === 'POST') {
    return handleRpc(req, res, registry);
  }

  if (pathname === '/rpc-response' && method === 'POST') {
    const body = await readBody(req);
    const delivered = deliverResponse(body);
    return sendJson(res, delivered ? 200 : 404, delivered
      ? { ok: true, request_id: body?.request_id }
      : { ok: false, error: 'UNKNOWN_REQUEST_ID', request_id: body?.request_id });
  }

  if ((pathname === '/rpc-pending' || pathname === '/rpc-poll') && (method === 'GET' || method === 'POST')) {
    return handlePoll(req, res, url, registry);
  }

  // Fire-and-forget events from the Blender extension (selection changes etc.).
  // Accepted and dropped for now: acknowledging prevents the extension's
  // outbox from retrying forever. Payload kept in the session record's meta.
  if (pathname === '/event' && method === 'POST') {
    const body = await readBody(req);
    const id = body?.instance_id || body?.id;
    if (!id) return sendJson(res, 400, { ok: false, error: 'MISSING_INSTANCE_ID' });
    const rec = registry.get(id);
    if (!rec) return sendJson(res, 404, { ok: false, error: 'UNKNOWN_INSTANCE', instance_id: id });
    if (body.event && rec.meta && typeof rec.meta === 'object') {
      try {
        rec.meta.last_event = { event: String(body.event), at: Date.now(), payload: body.payload ?? null };
      } catch { /* meta not writable */ }
    }
    return sendJson(res, 200, { ok: true, instance_id: id });
  }

  return sendJson(res, 404, { ok: false, error: 'NOT_FOUND', path: pathname });
}

async function handleRpc(req, res, registry) {
  const body = await readBody(req);
  const instanceId = body?.instance_id;
  const rpcMethod = body?.method;
  const requestId = body?.request_id || `rpc-${crypto.randomUUID()}`;
  if (!instanceId || !rpcMethod) {
    return sendJson(res, 400, { ok: false, error: 'MISSING_FIELDS', required: ['instance_id', 'method'] });
  }
  const known = !!registry.get(instanceId);
  const timeoutMs = clamp(Number(body.timeout_ms) || RPC_DEFAULT_TIMEOUT, 1000, RPC_MAX_TIMEOUT);
  const record = {
    request_id: requestId,
    instance_id: instanceId,
    method: rpcMethod,
    params: body.params ?? {},
    created_at: Date.now(),
  };
  if (!rpcQueues.has(instanceId)) rpcQueues.set(instanceId, []);
  rpcQueues.get(instanceId).push(record);

  const result = await new Promise((settle) => {
    const timer = setTimeout(() => {
      pendingRpcs.delete(requestId);
      removeFromQueue(instanceId, requestId);
      settle({
        ok: false,
        error: 'RPC_TIMEOUT',
        request_id: requestId,
        message: `No /rpc-response from Blender within ${timeoutMs}ms`,
      });
    }, timeoutMs);
    pendingRpcs.set(requestId, {
      instanceId,
      createdAt: Date.now(),
      finish(value) { clearTimeout(timer); pendingRpcs.delete(requestId); settle(value); },
    });
    wakePollers(instanceId);
  });

  const status = result.ok === false && result.error === 'RPC_TIMEOUT' ? 504 : 200;
  return sendJson(res, status, { ...result, known_instance: known });
}

function deliverResponse(body) {
  const id = body?.request_id;
  if (!id) return false;
  const pending = pendingRpcs.get(id);
  if (!pending) return false;
  const ok = body.ok !== false && !body.error;
  const result = Object.prototype.hasOwnProperty.call(body, 'result') ? body.result : null;
  pending.finish({
    ok,
    request_id: id,
    result,
    ...(body.error ? { error: body.error, message: body.message } : {}),
  });
  return true;
}

async function handlePoll(req, res, url, registry) {
  let body = {};
  if (req.method === 'POST') body = (await readBody(req)) || {};
  const instanceId = body.instance_id || url.searchParams.get('instance_id');
  if (!instanceId) return sendJson(res, 400, { ok: false, error: 'MISSING_INSTANCE_ID' });
  const waitMs = clamp(Number(body.wait_ms || url.searchParams.get('wait')) || POLL_DEFAULT_WAIT, 0, POLL_MAX_WAIT);
  registry.heartbeat(instanceId, body);

  const take = () => {
    const q = rpcQueues.get(instanceId);
    if (!q || !q.length) return null;
    const rec = q.shift();
    if (!q.length) rpcQueues.delete(instanceId);
    return rec;
  };

  let rec = take();
  if (!rec && waitMs > 0) {
    await new Promise((resolve) => {
      let set = pollWaiters.get(instanceId);
      if (!set) { set = new Set(); pollWaiters.set(instanceId, set); }
      const waiter = { resolve: null, timer: null };
      waiter.timer = setTimeout(() => { set.delete(waiter); resolve(); }, waitMs);
      waiter.resolve = () => { clearTimeout(waiter.timer); set.delete(waiter); resolve(); };
      set.add(waiter);
    });
    rec = take();
  }
  if (!rec) return sendJson(res, 200, { ok: true, pending: false });
  return sendJson(res, 200, {
    ok: true,
    pending: true,
    request_id: rec.request_id,
    method: rec.method,
    params: rec.params,
    instance_id: instanceId,
  });
}

function wakePollers(instanceId) {
  const set = pollWaiters.get(instanceId);
  if (!set) return;
  for (const w of set) w.resolve();
  set.clear();
}

function removeFromQueue(instanceId, requestId) {
  const q = rpcQueues.get(instanceId);
  if (!q) return;
  const i = q.findIndex(r => r.request_id === requestId);
  if (i >= 0) q.splice(i, 1);
  if (!q.length) rpcQueues.delete(instanceId);
}

async function writeSessionFile(runtime, rec) {
  if (!rec) return;
  await fsp.mkdir(runtime.paths.sessionsDir, { recursive: true });
  const file = path.join(runtime.paths.sessionsDir, `${rec.instance_id}.json`);
  await fsp.writeFile(file, JSON.stringify(publicRecord(rec, Date.now() - rec.last_heartbeat), null, 2) + '\n', 'utf8');
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.setEncoding('utf8');
    req.on('data', chunk => {
      data += chunk;
      if (data.length > MAX_BODY) { reject(new Error('BODY_TOO_LARGE')); req.destroy(); }
    });
    req.on('end', () => {
      if (!data.trim()) return resolve({});
      try { resolve(JSON.parse(data)); } catch { reject(new Error('INVALID_JSON')); }
    });
    req.on('error', reject);
  });
}

function sendJson(res, status, obj) {
  const text = JSON.stringify(obj);
  res.statusCode = status;
  res.setHeader('content-length', Buffer.byteLength(text));
  res.end(text);
}
