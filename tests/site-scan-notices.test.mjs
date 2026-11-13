import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { renderSites } from '../dist/sites-view.js';
import { escapeHtml as esc, icon } from '../dist/ui.js';
import { renderProcessingJobs } from '../dist/processing-view.js';

const site = { id: 'fixture', name: '课程站', domain: 'fixture.example', sessionSaved: true };
const cleared = {
  id: 'notice', mode: 'series', siteId: site.id, url: 'https://fixture.example/cleared',
  status: 'needs-user', seriesNoticeDismissed: true,
};

test('a dismissed scan challenge does not override a saved site session', () => {
  const html = renderSites({ mode: 'real', sites: [site], assets: [], scans: [cleared] });
  assert.match(html, /会话已保存/);
  assert.doesNotMatch(html, /需要重新登录/);
});

test('a source with no saved session offers scanning as its primary action', () => {
  const html = renderSites({ mode: 'real', sites: [{ ...site, sessionSaved: false }], assets: [] });
  assert.match(html, /class="btn btn-primary" data-action="scan-site" data-id="fixture"/);
  assert.match(html, /data-action="auth-site" data-id="fixture">站点授权/);
  assert.doesNotMatch(html, /需要重新登录|未保存会话/);
});

test('activity shows current scan challenges and suppresses dismissed or resolved ones', () => {
  const latest = { ...cleared, id: 'latest', url: 'https://fixture.example/resolved', status: 'completed', seriesNoticeDismissed: false };
  const previous = { ...latest, id: 'previous', status: 'needs-user' };
  const current = { ...previous, id: 'current', url: 'https://fixture.example/current' };
  const state = { scans: [current, latest, cleared, previous], processingJobs: [] };
  const app = readFileSync(new URL('../dist/app.js', import.meta.url), 'utf8');
  const view = app.slice(app.indexOf('function renderActivitySide('), app.indexOf('function renderRealHelp('));
  const html = vm.runInNewContext(view + '\nrenderActivitySide()', {
    state, esc, icon, isReal: () => true, scanStatusLabels: {}, renderProcessingJobs,
  });
  assert.ok(html.includes(current.url), 'current challenge must remain actionable');
  assert.ok(!html.includes(cleared.url), 'cleared challenge should stay hidden');
  assert.ok(!html.includes(previous.url), 'successful rescan should resolve the previous challenge');
});

test('public scan challenges remain visible in activity without a login-management action', () => {
  const state = {publicMode:true,scans:[{...cleared,seriesNoticeDismissed:false}],processingJobs:[]};
  const app = readFileSync(new URL('../dist/app.js', import.meta.url), 'utf8');
  const view = app.slice(app.indexOf('function renderActivitySide('), app.indexOf('function renderRealHelp('));
  const html = vm.runInNewContext(view + '\nrenderActivitySide()', {
    state,esc,icon,isReal:()=>true,scanStatusLabels:{'needs-user':'等待站点验证'},renderProcessingJobs,
  });
  assert.match(html,/源站限制访问/);
  assert.doesNotMatch(html,/data-action="auth-site"|管理站点权限/);
});
