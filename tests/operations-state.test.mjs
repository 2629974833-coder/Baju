import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { setImmediate } from 'node:timers/promises';

// Exercise the app's maintenance state transitions without mounting browser UI.
const app = readFileSync(new URL('../dist/app.js', import.meta.url), 'utf8');
const maintenance = app.slice(app.indexOf('async function refreshOperations('), app.indexOf('async function refreshRealState('));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
function controller(request) {
  const runtime = { data: { ready: true }, backups: [], loading: false, error: '', busy: false, updatedAt: 0 };
  const context = vm.createContext({ ui: { page: 'settings', operations: runtime }, api: { request }, renderPage() {}, workspaceInteractionActive: () => false, toast() {} });
  vm.runInContext(maintenance, context);
  return { runtime, refresh: context.refreshOperations, maintain: context.maintainBackup };
}

test('a successful background status read preserves the failed backup action reason', async () => {
  const status = deferred();
  const { runtime, maintain } = controller((path, options) => options?.method === 'POST'
    ? Promise.reject(new Error('备份空间不足')) : status.promise);
  await assert.rejects(maintain(), /备份空间不足/);
  status.resolve({ ready: true });
  await setImmediate();
  assert.match(runtime.error, /备份空间不足/);
  assert.equal(runtime.loading, false);
});

test('an older backup list cannot remove a newly created and verified backup', async () => {
  const oldStatus = deferred(), oldList = deferred();
  const backup = { id: 'new-snapshot', verified: true, verification: 'verified' };
  let statusReads = 0;
  const { runtime, refresh, maintain } = controller((path, options) => {
    if (options?.method === 'POST') return Promise.resolve(backup);
    if (path === '/api/maintenance/backups') return oldList.promise;
    return ++statusReads === 1 ? oldStatus.promise : Promise.resolve({ ready: true });
  });
  const staleRead = refresh();
  await maintain();
  assert.equal(runtime.backups[0]?.id, 'new-snapshot');
  oldStatus.resolve({ ready: true });
  oldList.resolve({ backups: [] });
  await staleRead;
  await setImmediate();
  assert.equal(runtime.backups.length, 1);
  assert.equal(runtime.backups[0]?.id, 'new-snapshot');
  assert.equal(runtime.backups[0]?.verified, true);
});

test('a stale read cannot clear a newer loading state or publish its stale error', async () => {
  const oldStatus = deferred(), oldList = deferred(), currentStatus = deferred();
  let statusReads = 0;
  const { runtime, refresh, maintain } = controller((path, options) => {
    if (options?.method === 'POST') return Promise.resolve({ id: 'created' });
    if (path === '/api/maintenance/backups') return oldList.promise;
    return ++statusReads === 1 ? oldStatus.promise : currentStatus.promise;
  });
  const staleRead = refresh();
  await maintain();
  oldStatus.reject(new Error('过期维护响应'));
  oldList.resolve({ backups: [] });
  await staleRead;
  assert.equal(runtime.loading, true);
  assert.doesNotMatch(runtime.error, /过期维护响应/);
  currentStatus.resolve({ ready: true });
  await setImmediate();
  assert.equal(runtime.loading, false);
});
