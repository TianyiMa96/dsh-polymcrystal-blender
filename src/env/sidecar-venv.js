// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * Isolated Python sidecar venv provisioning + health check (agent.md 2.3).
 *
 * uv is preferred; falls back to system python3 / `py -3` (>=3.11).
 * The venv always lives at $DSH_HOME/plugins/dsh-polycrystal/.venv and is
 * provisioned with `pip install -e <repo>/sidecar`. Blender's bundled Python
 * is NEVER touched.
 */
import fs from 'node:fs';
import { findUv, findSystemPython, runProcess, parseJsonLoose, ensureDir } from '../runtime.js';

export function venvExists(runtime) { return fs.existsSync(runtime.paths.venvPython); }

/** Provision the .venv. Returns structured steps/errors, never throws. */
export async function provisionVenv(runtime, { timeoutMs = 600000, signal } = {}) {
  const steps = [];
  const uv = await findUv(runtime);
  const sysPython = await findSystemPython(runtime);
  await ensureDir(runtime.paths.polyHome);

  if (uv) {
    const venvArgs = ['venv', runtime.paths.venvDir, '--python', '>=3.11'];
    steps.push({ command: uv.path, args: venvArgs, phase: 'uv venv' });
    let r = await runProcess(uv.path, venvArgs, { timeoutMs, signal, cwd: runtime.paths.repoRoot });
    if (!r.ok) {
      return { ok: false, error: 'UV_VENV_FAILED', phase: 'uv venv', ...summarize(r), steps, uv, sysPython };
    }
    const installArgs = ['pip', 'install', '--python', runtime.paths.venvDir, '-e', runtime.paths.sidecarDir];
    steps.push({ command: uv.path, args: installArgs, phase: 'uv pip install -e sidecar' });
    r = await runProcess(uv.path, installArgs, { timeoutMs, signal, cwd: runtime.paths.repoRoot });
    if (!r.ok) {
      return { ok: false, error: 'UV_INSTALL_FAILED', phase: 'uv pip install', ...summarize(r), steps, uv, sysPython };
    }
  } else if (sysPython) {
    const venvArgs = [...sysPython.args, '-m', 'venv', runtime.paths.venvDir];
    steps.push({ command: sysPython.command, args: venvArgs, phase: 'python -m venv' });
    let r = await runProcess(sysPython.command, venvArgs, { timeoutMs, signal });
    if (!r.ok) {
      return { ok: false, error: 'VENV_FAILED', phase: 'python -m venv', ...summarize(r), steps, uv, sysPython };
    }
    const pipArgs = ['-m', 'pip', 'install', '-e', runtime.paths.sidecarDir];
    steps.push({ command: runtime.paths.venvPython, args: pipArgs, phase: 'pip install -e sidecar' });
    r = await runProcess(runtime.paths.venvPython, pipArgs, { timeoutMs, signal, cwd: runtime.paths.repoRoot });
    if (!r.ok) {
      return { ok: false, error: 'PIP_INSTALL_FAILED', phase: 'pip install', ...summarize(r), steps, uv, sysPython };
    }
  } else {
    return {
      ok: false, error: 'NO_PYTHON_OR_UV', steps, uv, sysPython,
      message: 'Neither uv nor a system python >=3.11 was found; cannot provision the isolated sidecar venv.',
    };
  }

  return {
    ok: true, python: runtime.paths.venvPython, venvDir: runtime.paths.venvDir,
    uv, sysPython, steps,
  };
}

/** Run `<venv>/bin/python -m polycrystal_core.cli doctor`; expect {"ok":true,...}. */
export async function sidecarDoctor(runtime, { timeoutMs = 30000, signal } = {}) {
  if (!venvExists(runtime)) {
    return {
      ok: false, error: 'VENV_MISSING',
      message: 'The sidecar .venv is not provisioned; call polycrystal_env(action="setup_sidecar") first.',
    };
  }
  const r = await runProcess(
    runtime.paths.venvPython,
    ['-m', 'polycrystal_core.cli', 'doctor'],
    { timeoutMs, signal, cwd: runtime.paths.repoRoot },
  );
  const data = parseJsonLoose(r.stdout);
  if (!r.ok) {
    return {
      ok: false, error: r.timedOut ? 'DOCTOR_TIMEOUT' : 'DOCTOR_FAILED',
      code: r.code, timedOut: r.timedOut,
      stdout: r.stdout.slice(0, 4000), stderr: r.stderr.slice(0, 4000),
    };
  }
  if (!data || data.ok !== true) {
    return {
      ok: false, error: 'DOCTOR_NOT_OK', data,
      stdout: r.stdout.slice(0, 4000), stderr: r.stderr.slice(0, 2000),
    };
  }
  return { ok: true, versions: data.versions || {}, data };
}

/** Doctor first; if unhealthy, (re)provision then doctor again. */
export async function ensureVenv(runtime, { skipProvision = false, timeoutMs = 600000, signal } = {}) {
  let doctor = await sidecarDoctor(runtime, { timeoutMs: 30000, signal });
  if (doctor.ok) return { ok: true, provisioned: false, doctor };
  if (skipProvision) return { ok: false, provisioned: false, doctor };

  const provision = await provisionVenv(runtime, { timeoutMs, signal });
  if (!provision.ok) return { ok: false, provisioned: false, provision, doctor };
  doctor = await sidecarDoctor(runtime, { timeoutMs: 30000, signal });
  return { ok: !!doctor.ok, provisioned: true, provision, doctor };
}

/** Generic sidecar CLI invocation: `python -m polycrystal_core.cli <args>`. */
export async function runSidecar(runtime, args, { timeoutMs = 300000, signal } = {}) {
  const r = await runProcess(
    runtime.paths.venvPython,
    ['-m', 'polycrystal_core.cli', ...args],
    { timeoutMs, signal, cwd: runtime.paths.repoRoot },
  );
  const data = parseJsonLoose(r.stdout);
  return {
    ok: r.ok && !!data,
    code: r.code,
    timedOut: r.timedOut,
    data,
    stdout: r.stdout,
    stderr: r.stderr,
    args,
  };
}

function summarize(r) {
  return {
    code: r.code, timedOut: r.timedOut,
    stdout: (r.stdout || '').slice(0, 4000), stderr: (r.stderr || '').slice(0, 4000),
  };
}
