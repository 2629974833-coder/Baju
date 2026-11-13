import assert from 'node:assert/strict';
import test from 'node:test';
import { createApiClient } from '../dist/real-api.js';
import { createInitialState } from '../dist/model.js';
import { renderAutomation, renderRuleModal } from '../dist/workflow-view.js';
import { changedRuleFields, ruleRequest } from '../dist/automation-view.js';

const site = { id: 'fixture', name: '课程站', domain: 'fixture.example', url: 'https://fixture.example' };
const rule = { id: 'one', name: '日更', siteId: site.id, url: site.url + '/#/series/2', intervalMinutes: 90, enabled: true, autoDownload: false, kinds: ['image'], status: 'idle' };
const real = (rules = []) => ({ mode: 'real', sites: [site], rules });

function fields(ruleOverrides = {}) {
  const data = new FormData();
  const values = { ...rule, url: '', ...ruleOverrides };
  for (const key of ['name', 'url', 'siteId', 'intervalMinutes']) data.set(key, values[key]);
  if (values.enabled) data.set('enabled', 'on');
  if (values.autoDownload) data.set('autoDownload', 'on');
  for (const kind of values.kinds) data.append('kinds', kind);
  return data;
}

test('rule payload preserves hash routes and selected kinds, defaults empty targets to the registered site', () => {
  assert.deepEqual(ruleRequest(fields(), [site]), {
    name: '日更', siteId: site.id, url: site.url + '/', intervalMinutes: 90, enabled: true, autoDownload: false, kinds: ['image']
  });
  const updated = ruleRequest(fields({ url: rule.url, autoDownload: true, enabled: false, kinds: ['video'] }), [site]);
  assert.equal(updated.url, rule.url);
  assert.equal(updated.autoDownload, true);
  assert.equal(updated.enabled, false);
  assert.deepEqual(updated.kinds, ['video']);
});

test('incomplete rules and invalid scan targets have actionable form errors', () => {
  for (const [values, reason] of [
    [{name: '  '}, /名称/], [{siteId: 'missing'}, /来源/], [{kinds: []}, /类型/],
    [{intervalMinutes: 14}, /15/], [{intervalMinutes: 20.2}, /整数/],
    [{url: 'javascript:alert(1)'}, /网址/], [{url: 'https://elsewhere.example'}, /来源/],
  ]) assert.throws(() => ruleRequest(fields(values), [site]), reason);
});

test('real rule form exposes only supported actions with editable values and scan-only default', () => {
  const fresh = renderRuleModal(real());
  assert.match(fresh, /name="autoDownload"/);
  assert.doesNotMatch(fresh, /name="autoDownload"[^>]*checked/);
  assert.match(fresh, /name="enabled"[^>]*checked/);
  assert.match(fresh, /name="intervalMinutes"[^>]*min="15"[^>]*max="10080"/);
  assert.match(fresh, /id="rule-error"/);
  const edit = renderRuleModal(real([rule]), rule);
  assert.match(edit, /name="url"[^>]*value="https:\/\/fixture.example\/#\/series\/2"/);
  assert.match(edit, /value="image" checked/);
  assert.doesNotMatch(edit, /value="video" checked/);
  assert.match(edit, /name="intervalMinutes"[^>]*value="90"/);
});

test('running rules lock conflicting mutations and completed rules show increment and next-run time', () => {
  const html = renderAutomation(real([{ ...rule, status: 'running' }]));
  for (const action of ['run-rule', 'toggle-rule', 'edit-rule', 'delete-rule']) {
    assert.match(html, new RegExp('data-action="' + action + '"[^>]*disabled'));
  }
  const result = renderAutomation(real([{...rule, status:'completed', lastNewCount: 3, lastQueuedCount: 1, lastRunAt:'2026-11-04T10:00:00Z', nextRunAt:'2026-11-05T10:00:00Z'}]));
  assert.match(result, /新增 3 项/);
  assert.match(result, /入队 1 项/);
  assert.match(result, /datetime="2026-11-05T10:00:00Z"/);
});

test('expired sessions expose the correct recovery action, hide future execution and escape error detail', () => {
  const html = renderAutomation(real([{...rule, name:'<b>课程</b>', enabled:false, status:'needs-user', error:'失效 <script>', nextRunAt:null}]));
  assert.match(html, /需要授权|等待授权/);
  assert.match(html, /data-action="auth-site" data-id="fixture"/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<b>课程<\/b>/);
  assert.match(html, /data-action="run-rule"[^>]*disabled/);
  assert.doesNotMatch(html, /下次检查/);
});

test('static demo keeps its simulated schedule and has no service run action', () => {
  const state = createInitialState();
  assert.match(renderAutomation(state), /演示|原型/);
  assert.doesNotMatch(renderAutomation(state), /data-action="run-rule"/);
  assert.match(renderRuleModal(state), /name="schedule"/);
});

test('older services cannot create rules from a hot-updated panel', async () => {
  for (const automation of [undefined, true]) {
    const api = createApiClient(async path => Response.json(path === '/api/capabilities'
      ? {mode:'real', version:1, automation} : {...real(), version:1, assets:[]}));
    await api.mode();
    const state = await api.state();
    const html = renderAutomation(state);
    assert.equal(html.includes('data-action="new-rule"'), automation === true);
    assert.equal(html.includes('data-action="retry-service"'), automation !== true);
  }
});

test('an edit only sends intentional changes, preserving a concurrent permission pause', () => {
  const original = { ...rule, url: site.url, kinds: ['video', 'image'] };
  const draft = ruleRequest(fields({ ...original, name: '新名称' }), [site]);
  assert.deepEqual(changedRuleFields(original, draft), { name: '新名称' });
  assert.deepEqual(changedRuleFields(original, { ...draft, name: rule.name }), {});
  const target = site.url + '/#/series/3';
  assert.deepEqual(changedRuleFields(original, { ...draft, url: target }), {
    name: '新名称', url: target, expectedTarget: {url: original.url, siteId: original.siteId}
  });
});
