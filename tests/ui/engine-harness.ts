// Test-only native transport. Requests normally use the actual Python engine.
// Explicit boundary overrides simulate failures without executing OS mutations.
// Production has no browser bridge or alternate API server.
import { spawn } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import type { Page } from '@playwright/test';

export async function connectEngine(page: Page, options: {boundaryOverride?: (method:string,params:Record<string,unknown>) => unknown | Promise<unknown>} = {}) {
  const data = await mkdtemp(join(tmpdir(), 'q22-ui-evidence-'));
  const python = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';
  const child = spawn(resolve(python), ['-m', 'q22_engine', '--data-dir', data]);
  let sequence = 0;
  let closing = false;
  const pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void }>();
  const calls: { method: string; params: Record<string, unknown> }[] = [];
  const files: string[] = [];
  createInterface({ input: child.stdout }).on('line', line => {
    const result = JSON.parse(line);
    const request = pending.get(result.id);
    pending.delete(result.id);
    if (result.error) request?.reject(new Error(result.error.message));
    else request?.resolve(result.result);
  });
  child.stderr.on('data', value => process.stderr.write(value));
  function rejectPending(error: Error) {
    for (const request of pending.values()) request.reject(error);
    pending.clear();
  }
  // Persistent UI polling may race test teardown. Match the native supervisor's
  // closed-transport behavior instead of writing to an ended Node stream.
  child.stdin.on('error', rejectPending);
  child.on('error', error => { closing = true; rejectPending(error); });
  child.on('exit', code => {
    closing = true;
    rejectPending(new Error(`Test engine exited (${code})`));
  });
  async function rpc(method: string, params: Record<string, unknown> = {}) {
    if (closing || child.stdin.destroyed || child.stdin.writableEnded || child.exitCode !== null)
      throw new Error('Test engine transport is closed');
    calls.push({ method, params });
    return new Promise<unknown>((resolveRequest, reject) => {
      const id = ++sequence;
      pending.set(id, { resolve: resolveRequest, reject });
      child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n', error => {
        if (error) { pending.delete(id); reject(error); }
      });
    });
  }
  await page.exposeFunction('__q22TestInvoke', async (command: string, args: Record<string, unknown>) => {
    if (command === 'engine_request') {
      const method = String(args.method), params = args.params as Record<string,unknown>;
      const override = await options.boundaryOverride?.(method,params);
      if (override !== undefined) {calls.push({method,params});return override;}
      return rpc(method, params);
    }
    if (command === 'plugin:dialog|open') return files.shift() ?? null;
    throw new Error(`Unexpected test command ${command}`);
  });
  await page.addInitScript(() => {
    (window as any).__TAURI_INTERNALS__ = {
      invoke: async (command: string, args: unknown) => {
        const result = await (window as any).__q22TestInvoke(command, args);
        if (result?.__q22TransportError) throw result.__q22TransportError;
        return result;
      },
    };
  });
  return {
    rpc, calls, files, data,
    async close() {
      closing = true;
      if (!child.stdin.destroyed && !child.stdin.writableEnded) child.stdin.end();
      await new Promise<void>(resolveExit => {
        if (child.exitCode !== null) return resolveExit();
        child.once('exit', () => resolveExit());
      });
      await rm(data, { recursive: true, force: true });
    },
  };
}
