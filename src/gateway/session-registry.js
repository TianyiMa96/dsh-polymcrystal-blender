// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * Multi-instance lease / heartbeat registry for live Blender GUI processes.
 * An instance is "active" while its last heartbeat age is < staleMs (6000ms).
 */
import { randomUUID } from 'node:crypto';

export const ACTIVE_AGE_MS = 6000;

/** Project an internal record into the public shape consumed by the detector. */
export function publicRecord(rec, ageMs) {
  return {
    instance_id: rec.instance_id,
    pid: rec.pid,
    version: rec.version_string,
    version_string: rec.version_string,
    binary_path: rec.binary_path,
    filepath: rec.filepath,
    open_blend_file: rec.filepath,
    addons: rec.addons,
    installed_addons: rec.addons,
    addon_names: Object.keys(rec.addons || {}),
    age_ms: ageMs,
    registered_at: rec.registered_at,
    last_heartbeat: rec.last_heartbeat,
    claimed: !!rec.claimed,
  };
}

function normalizeAddons(addons) {
  if (!addons) return {};
  if (Array.isArray(addons)) return Object.fromEntries(addons.map(a => [String(a), true]));
  if (typeof addons === 'object') return { ...addons };
  return {};
}

export class SessionRegistry {
  constructor({ staleMs = ACTIVE_AGE_MS } = {}) {
    this.staleMs = staleMs;
    this.sessions = new Map();
  }

  /** POST /register -> new instance_id. */
  register(data = {}) {
    const id = randomUUID();
    const now = Date.now();
    const rec = {
      instance_id: id,
      pid: data.pid ?? null,
      version_string: data.version_string ?? null,
      binary_path: data.binary_path ?? null,
      filepath: data.filepath ?? null,
      addons: normalizeAddons(data.addons ?? data.installed_addons),
      registered_at: now,
      last_heartbeat: now,
      claimed: false,
    };
    this.sessions.set(id, rec);
    return id;
  }

  /** POST /heartbeat -> refresh lease (and optionally update reported fields). */
  heartbeat(id, extra = {}) {
    const rec = this.sessions.get(id);
    if (!rec) return false;
    rec.last_heartbeat = Date.now();
    if (extra.binary_path) rec.binary_path = extra.binary_path;
    if (extra.version_string) rec.version_string = extra.version_string;
    if (extra.filepath !== undefined) rec.filepath = extra.filepath;
    if (extra.addons || extra.installed_addons) {
      rec.addons = normalizeAddons(extra.addons || extra.installed_addons);
    }
    return true;
  }

  get(id) { return this.sessions.get(id) || null; }

  claim(id) {
    for (const rec of this.sessions.values()) rec.claimed = false;
    const rec = this.sessions.get(id);
    if (!rec) return null;
    rec.claimed = true;
    return publicRecord(rec, Date.now() - rec.last_heartbeat);
  }

  claimed() {
    for (const rec of this.sessions.values()) {
      if (rec.claimed && Date.now() - rec.last_heartbeat < this.staleMs) {
        return publicRecord(rec, Date.now() - rec.last_heartbeat);
      }
    }
    return null;
  }

  list({ activeOnly = true } = {}) {
    const now = Date.now();
    const out = [];
    for (const rec of this.sessions.values()) {
      const age = now - rec.last_heartbeat;
      if (activeOnly && age >= this.staleMs) continue;
      out.push(publicRecord(rec, age));
    }
    out.sort((a, b) => a.registered_at - b.registered_at);
    return out;
  }

  countActive() { return this.list({ activeOnly: true }).length; }

  /** Drop very stale leases (5x stale window) so the map cannot grow unbounded. */
  prune() {
    const now = Date.now();
    let removed = 0;
    for (const [id, rec] of this.sessions) {
      if (now - rec.last_heartbeat >= this.staleMs * 5) { this.sessions.delete(id); removed++; }
    }
    return removed;
  }
}
