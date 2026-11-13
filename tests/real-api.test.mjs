import test from 'node:test';
import assert from 'node:assert/strict';
import { createApiClient, normalizeRealState } from '../dist/real-api.js';

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
test('only a missing capability endpoint selects the static demo', async () => {
  const api = createApiClient(async () => new Response('Not found', { status: 404 }));
  assert.equal(await api.mode(), 'demo');
});
test('server and network failure never select demo data', async () => {
  await assert.rejects(createApiClient(async () => json({ detail: 'Database unavailable' }, 500)).mode(), /Database unavailable/);
  await assert.rejects(createApiClient(async () => { throw new TypeError('Failed to fetch'); }).mode(), /连接/);
});
test('real capabilities are required, unexpected HTML or protocol is an error', async () => {
  assert.equal(await createApiClient(async () => json({ mode: 'real', version: 1 })).mode(), 'real');
  await assert.rejects(createApiClient(async () => new Response('<html>fallback</html>')).mode(), /响应/);
  await assert.rejects(createApiClient(async () => json({ mode: 'demo' })).mode(), /协议/);
});
test('mutation encodes JSON and reports server validation errors', async () => {
  let actual;
  const api = createApiClient(async (path, options) => {
    actual = { path, options };
    return json({ detail: '请先保存会话' }, 409);
  });
  await assert.rejects(api.request('/api/downloads', { method: 'POST', body: { ids: ['a'] } }), /请先保存会话/);
  assert.equal(actual.options.body, '{"ids":["a"]}');
  assert.equal(actual.options.headers['Content-Type'], 'application/json');
});
test('real state uses safe defaults without fabricated assets or permission verification', () => {
  const state = normalizeRealState({ mode: 'real', version: 1, assets: [{ id: 'a', thumbnail: '', title: '<unsafe>' }], sites: [] });
  assert.equal(state.assets[0].thumbnail, 'assets/favicon.svg');
  assert.deepEqual(state.assets[0].tags, []);
  assert.equal(state.assets[0].title, '<unsafe>');
  assert.deepEqual(state.jobs, []);
  assert.deepEqual(state.sites, []);
  assert.throws(() => normalizeRealState({ mode: 'demo', assets: [] }), /状态/);
});

import { renderProcess, renderAutomation, renderSettings, renderQueue } from '../dist/workflow-view.js';
import { renderAuthModal } from '../dist/sites-view.js';
const realState = () => normalizeRealState({ mode: 'real', version: 1, assets: [], sites: [{ id: 'site', name: '测试站点', domain: 'example.com', sessionSaved: true, verified: false }], settings: { concurrency: 2 } });
test('real processing accepts local originals and automation exposes persisted rules', () => {
  const state = realState();
  assert.match(renderProcess(state), /先下载一些素材/);
  assert.match(renderProcess(state), /从原件重新处理/);
  assert.doesNotMatch(renderProcess(state), /开始模拟处理/);
  assert.match(renderAutomation(state), /还没有自动化规则/);
  assert.match(renderAutomation(state), /data-action="new-rule"/);
});
test('real settings exposes operations without workspace reset or automatic processing rules', () => {
  const html = renderSettings(realState());
  assert.doesNotMatch(html, /data-action="reset-demo"/);
  assert.match(html, /id="setting-auto-process"[^>]*disabled/);
  assert.match(html, /运行维护/);
  assert.match(html, /data-action="refresh-operations"/);
  assert.doesNotMatch(html, /处理服务尚未接入/);
});
test('saved real session never claims verified membership or fake authorization', () => {
  const html = renderAuthModal(realState(), 'site');
  assert.match(html, /会话已保存，权限待验证/);
  assert.match(html, /打开登录窗口/);
  assert.doesNotMatch(html, /模拟授权连接|30 天（演示）|演示账号/);
});
test('real completed queue exposes a real file and escapes untrusted names', () => {
  const state = realState();
  state.assets.push({ id: 'asset', title: '<img onerror=attack()>', thumbnail: '', sizeMB: 1, siteId: 'site' });
  state.jobs.push({ id: 'job', assetId: 'asset', type: 'download', status: 'completed', progress: 100 });
  const html = renderQueue(state);
  assert.match(html, /\/api\/assets\/asset\/file/);
  assert.match(html, /&lt;img onerror=attack\(\)&gt;/);
  assert.doesNotMatch(html, /模拟进度|演示任务|去处理/);
});

import { formatSize } from '../dist/ui.js';
test('small real files retain byte accuracy and display useful units', () => {
  const state = normalizeRealState({ mode: 'real', version: 1, sites: [], assets: [{ id: 'small', sizeMB: 0, sizeBytes: 3575 }] });
  assert.ok(state.assets[0].sizeMB > 0);
  assert.equal(formatSize(state.assets[0].sizeMB), '3 KB');
  assert.equal(formatSize(1 / 1024 / 1024), '1 B');
  assert.equal(formatSize(1), '1 MB');
  assert.equal(formatSize(1024), '1.0 GB');
});

test('processing jobs normalize separately from download jobs and tolerate absent arrays', () => {
  const base = {mode:'real',version:1,assets:[],sites:[]};
  assert.deepEqual(normalizeRealState(base).processingJobs, []);
  const jobs = [{id:'download',type:'download'}], processingJobs=[{id:'process',type:'process'}];
  const state = normalizeRealState({...base,jobs,processingJobs});
  assert.deepEqual(state.jobs,jobs);
  assert.deepEqual(state.processingJobs,processingJobs);
  assert.deepEqual(normalizeRealState({...base,processingJobs:{}}).processingJobs,[]);
});

test('archive downloads retain ZIP bytes and maintenance errors retain actionable detail', async () => {
  const bytes = new Uint8Array([80,75,3,4,0,255]);
  let request;
  const api=createApiClient(async(path,options)=>{request={path,options};return new Response(bytes,{headers:{'content-type':'application/zip'}});});
  const blob=await api.request('/api/library/archive',{method:'POST',body:{ids:['one'],variant:'latest'},blob:true});
  assert.deepEqual(new Uint8Array(await blob.arrayBuffer()),bytes);
  assert.equal(request.options.body,'{"ids":["one"],"variant":"latest"}');
  await assert.rejects(createApiClient(async()=>json({detail:'维护中，请稍后重试'},503)).request('/api/maintenance/backups',{method:'POST',body:{}}),/维护中，请稍后重试/);
});

test('local browser authorization shows only supported actions and preserves an actionable save error', () => {
  const state = realState();
  const html = (dialog) => renderAuthModal(state, 'site', dialog);
  assert.doesNotMatch(html({}), /使用本机浏览器授权|保存本机浏览器会话/);
  assert.match(html({localInfo:{available:true,active:false}}), /使用本机浏览器授权/);
  const system = {mode:'system-browser', available:true};
  assert.match(html({localInfo:system}), /btn-primary" data-action="start-local-auth"/);
  assert.match(html({localInfo:system}), /面板内登录/);
  const linux = html({localInfo:{...system,active:true,message:'使用专用窗口菜单退出浏览器后，再保存会话。'}});
  assert.match(linux, /使用专用窗口菜单退出浏览器后，再保存会话。/);
  assert.match(linux, /保存本机浏览器会话/);
  const pending = html({localInfo:{available:true,active:true},localBusy:'save'});
  assert.match(pending, /data-action="save-local-auth"[^>]+disabled/);
  assert.match(pending, /data-action="cancel-local-auth"[^>]+disabled/);
  const failed = html({localInfo:{available:true,active:true},localError:'<需要关闭活动任务>'});
  assert.match(failed, /role="alert"[^>]*>&lt;需要关闭活动任务&gt;/);
  assert.match(failed, /保存本机浏览器会话/);
});

test('Chunlou troubleshooting remains available after closing the dedicated window', () => {
  const modal = info => renderAuthModal(realState(), 'site', {localInfo:info});
  for (const active of [false, true]) {
    const html = modal({mode:'system-browser', available:true, active});
    assert.match(html, /<summary>Cloudflare 仍然验证失败？<\/summary>/);
    for (const url of [
      'https://chunlou.tv/auth?r=/feed',
      'https://debug.challenges.cloudflare.com/',
      'https://developers.cloudflare.com/turnstile/troubleshooting/client-side-errors/error-codes/',
    ]) assert.ok(html.includes(`href="${url}"`), url);
  }
  assert.doesNotMatch(modal({mode:'guarded-browser', available:true, active:true}), /debug\.challenges/);
  assert.doesNotMatch(modal({mode:'system-browser', available:false, active:false}), /debug\.challenges/);
});

test('existing-browser authorization follows the import capability and pending window', () => {
  const modal = (info, extra = {}) => renderAuthModal(realState(), 'site', {localInfo:info, ...extra});
  const info = {mode:'system-browser', canImportAccount:true, available:false};
  const href = 'href="/chunlou-connect.html?site=site"';
  for (const available of [true, false]) {
    const html = modal({...info, available});
    assert.ok(html.includes(href));
    assert.match(html, /使用已登录的浏览器/);
  }
  for (const html of [modal({...info, available:true, active:true}), modal(info, {localBusy:'start'})]) {
    assert.ok(!html.includes(href));
    assert.match(html, /disabled>使用已登录的浏览器/);
  }
  assert.match(modal({...info, active:true}), /先保存或取消/);
  assert.ok(!modal({available:true, mode:'system-browser'}).includes(href));
  assert.ok(!modal({available:false, canImportAccount:false}).includes(href));
});

test('Lata Google authorization offers native browser with site-specific guidance', () => {
  const state = realState();
  state.sites[0] = {...state.sites[0], url:'https://lata.casa/series', domain:'lata.casa', name:'Lata Casa'};
  const html = renderAuthModal(state, 'site', {localInfo:{mode:'system-browser', available:true, canImportAccount:false}});
  assert.match(html, /Google/);
  assert.match(html, /btn-primary" data-action="start-local-auth"/);
  assert.doesNotMatch(html, /春楼|chunlou\.tv|Cloudflare|chunlou-connect/);
});
