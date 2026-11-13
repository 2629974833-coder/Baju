import test from 'node:test';
import assert from 'node:assert/strict';
import { publicWorkspace, workspacePage, renderWorkspaceNotice, saveOriginalLink } from '../dist/public-workspace.js';
import { createApiClient } from '../dist/real-api.js';

const json = value => new Response(JSON.stringify(value), {headers:{'content-type':'application/json'}});
const base = {mode:'real',version:1,sites:[],assets:[]};

test('public capability survives state normalization and keeps retention explicit', async () => {
  for (const where of ['capabilities', 'state']) {
    const api = createApiClient(async path => json(path === '/api/capabilities'
      ? {...base, ...(where === 'capabilities' ? {publicMode:true,retentionHours:24} : {})}
      : {...base, ...(where === 'state' ? {publicMode:true,retentionHours:24} : {})}));
    await api.mode();
    const state = await api.state();
    assert.equal(publicWorkspace(state), true);
    assert.equal(state.retentionHours, 24);
    assert.equal(workspacePage(state, 'process'), 'home');
    assert.equal(workspacePage(state, 'automation'), 'home');
    assert.equal(workspacePage(state, 'settings'), 'home');
    assert.equal(workspacePage(state, 'sites'), 'sites');
  }
});

test('local workspace keeps existing pages and no temporary-space notice', () => {
  assert.equal(publicWorkspace({...base,publicMode:'true'}), false);
  assert.equal(workspacePage(base, 'process'), 'process');
  assert.equal(workspacePage(base, 'settings'), 'settings');
  assert.equal(renderWorkspaceNotice(base), '');
});

test('public notice and save links describe temporary storage without exposing raw paths', () => {
  const html = renderWorkspaceNotice({...base,publicMode:true,retentionHours:24});
  assert.match(html,/你的临时工作区/);
  assert.match(html,/免注册/);
  assert.match(html,/24 小时未使用后清理/);
  assert.match(html,/换浏览器会开启新空间/);
  assert.doesNotMatch(html,/他人|其他用户|服务器目录/);
  assert.equal(saveOriginalLink({id:'a',status:'discovered',kind:'video'}), '');
  const link = saveOriginalLink({id:'a\"<',status:'downloaded',kind:'video'});
  assert.match(link,/href="\/api\/assets\/a%22%3C\/file\?download=true" download/);
  assert.match(link,/保存到本机/);
});
