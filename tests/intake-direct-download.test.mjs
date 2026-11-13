import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import {freshResultView, intakeScopes, renderIntakeResult, suggestScope} from '../dist/intake-results.js';
import {normalizeScanUrl} from '../dist/model.js';

const app = readFileSync(new URL('../dist/app.js', import.meta.url), 'utf8');
const submitSource = app.slice(app.indexOf('async function submitIntake('), app.indexOf('function openPalette('));

test('pasting a single-work URL updates the home action without replacing the focused input', () => {
  let onInput;
  const submit = {innerHTML:'开始拾取<kbd>↵</kbd>'}, hint = {textContent:''};
  const buttons = ['site','series','single'].map(value => ({dataset:{value},classList:{toggle(){}},setAttribute(){}}));
  const ui = {homeScope:'site',homeDraft:'',explicitHomeScope:false};
  const context = vm.createContext({
    ui,isReal:()=>true,suggestScope,intakeScopes,normalizeScanUrl,
    document:{addEventListener:(_type,handler) => {onInput=handler;},querySelectorAll:() => buttons},
    $:selector => selector === '.home-scope-hint' ? hint : submit,
  });
  vm.runInContext(app.slice(app.indexOf('document.addEventListener("input",'),app.indexOf('document.addEventListener("compositionend",')),context);
  for (const [url,scope,label] of [
    ['https://v.douyin.com/lFT4mm8AhKk/','single','直接下载'],
    ['https://example.test/','site','开始拾取'],
    ['复制打开抖音，看看这个作品 https://v.douyin.com/lFT4mm8AhKk/ 10/25','single','直接下载'],
    ['https://lustflix.tv/#/s/C13','series','开始拾取'],
    ['https://www.youtube.com/watch?v=jNQXAC9IVRw','single','直接下载'],
  ]) {
    onInput({target:{id:'home-url',value:url,dataset:{}}});
    assert.equal(ui.homeScope,scope);
    assert.equal(submit.innerHTML,label+'<kbd>↵</kbd>');
    assert.equal(ui.homeDraft,url);
  }
});

test('a copied share message uses its single HTTP link and never guesses between several links', () => {
  assert.equal(normalizeScanUrl('复制打开抖音 https://v.douyin.com/lFT4mm8AhKk/ 10/25'), 'https://v.douyin.com/lFT4mm8AhKk/');
  assert.equal(normalizeScanUrl('看这个：https://v.douyin.com/lFT4mm8AhKk/。'), 'https://v.douyin.com/lFT4mm8AhKk/');
  assert.equal(normalizeScanUrl('第一个 https://v.douyin.com/one/ 第二个 https://v.douyin.com/two/'), null);
});

test('single-work entry points clearly offer direct download while broader scopes still scan', () => {
  for (const scope of ['single','site','series']) {
    const context = vm.createContext({
      state:{mode:'real',publicMode:true,assets:[],scans:[]},ui:{homeScope:scope,homeDraft:'',seriesDraft:{}},
      isReal:()=>true,isLocalAsset:()=>false,activeWork:()=>({total:0}),homeTasks:()=>[],homeAgenda:()=>[],
      intakeScopes,renderWorkspaceNotice:()=>'',renderHomePending:()=>'',publicWorkspace:()=>true,
      icon:()=>'',esc:String,formatSize:()=>'',
    });
    vm.runInContext(app.slice(app.indexOf('function renderHome()'),app.indexOf('async function submitHomeIntake(')),context);
    vm.runInContext(app.slice(app.indexOf('function renderIntakeModal('),app.indexOf('async function submitIntake(')),context);
    const label = scope === 'single' ? '直接下载' : '开始拾取';
    for (const html of [context.renderHome(),context.renderIntakeModal({scope,opts:{}})]) {
      assert.match(html,new RegExp('type="submit"[^>]*>'+label));
    }
  }
});

function submission(publicMode, request) {
  const calls = [], destinations = [];
  const button = {disabled:false,classList:{add(){},remove(){}},isConnected:true};
  const form = {querySelector:selector => selector === 'button[type="submit"]' ? button : null};
  const context = vm.createContext({
    URL, FormData, state:{mode:'real',publicMode,sites:[]}, ui:{homeDraft:'https://video.example/watch/1',page:'home',modal:null},
    normalizeScanUrl: value => String(value || ''), isReal:() => true, toast(){},
    api:{request:async (path, options) => { calls.push({path,...options}); return request ? request() : {id:'scan_new'}; }},
    openIntakeDestination:(scan,scope) => destinations.push({id:scan.id,scope}),
    refreshRealState:async () => {}, document:{querySelectorAll:() => [button]},
  });
  vm.runInContext(submitSource, context);
  const values = new FormData(); values.set('url', 'https://video.example/watch/1');
  return {calls,destinations,button,submit:scope => context.submitIntake(form,values,scope)};
}

test('one single-work submission requests server-side automatic download without an authorization step', async () => {
  for (const publicMode of [true,false]) {
    const flow = submission(publicMode);
    await flow.submit('single');
    assert.equal(flow.calls.length,1);
    assert.equal(flow.calls[0].path,'/api/intake');
    assert.equal(flow.calls[0].body.autoDownload,true);
    assert.equal(flow.calls[0].body.limit,1);
    assert.deepEqual(flow.destinations,[{id:'scan_new',scope:'single'}]);
    assert.equal(flow.button.disabled,false);
  }
});

test('site and series submissions keep their existing scan-only behavior', async () => {
  for (const scope of ['site','series']) {
    const flow = submission(true);
    await flow.submit(scope);
    assert.notEqual(flow.calls[0].body.autoDownload,true);
  }
});

test('a repeated click while intake is submitting cannot create a second automatic download', async () => {
  let finish;
  const flow = submission(true, () => new Promise(resolve => {finish=resolve;}));
  const first = flow.submit('single');
  await flow.submit('single');
  assert.equal(flow.calls.length,1);
  finish({id:'scan_new'});
  await first;
});

const result = {scanId:'scan_new',scope:'single',status:'completed',url:'https://video.example/watch/1',
  downloadAfterScan:true,downloadSummary:{added:1,blocked:0,duplicates:0},groups:[{key:'post',assetIds:['video']}]};
const asset = {id:'video',title:'公开视频',kind:'video',status:'discovered'};
const base = {publicMode:true,scans:[{id:'scan_new',siteId:'source'}],assets:[asset],jobs:[]};
const render = (sample=result,state=base) => renderIntakeResult(sample,state,freshResultView());

test('automatic intake shows parsing before any media is available', () => {
  const html = render({...result,status:'running',groups:[]});
  assert.match(html,/>解析中</);
  assert.match(html,/解析完成后自动下载/);
});

test('resolved automatic intake shows the actual download task and ignores another work', () => {
  const html = render(result,{...base,jobs:[{assetId:'video',status:'running',progress:37},{assetId:'other',status:'failed'}]});
  assert.match(html,/>下载中</);
  assert.match(html,/37%/);
  assert.match(html,/data-page="queue"/);
  assert.doesNotMatch(html,/>下载失败</);
});

test('a completed parse can acknowledge its queue transaction before the next state poll', () => {
  const html = render(result,{...base,assets:[],jobs:[]});
  assert.match(html,/已加入下载队列/);
  assert.doesNotMatch(html,/未找到可下载素材|下载未开始/);
});

test('a multi-attachment work waits for every original and uses the latest task attempt', () => {
  const html = render({...result,groups:[{key:'post',assetIds:['image','video']}]},{...base,
    assets:[{id:'image',kind:'image',status:'downloaded'},asset],
    jobs:[{assetId:'video',status:'failed'},{assetId:'video',status:'running',progress:12}]});
  assert.match(html,/已保存 1 \/ 2 项/);
  assert.match(html,/12%/);
  assert.doesNotMatch(html,/>下载完成</);
});

test('retrying an earlier task takes precedence over a newer cancelled history record', () => {
  const html = render(result,{...base,jobs:[
    {id:'earlier',assetId:'video',status:'running',progress:65},
    {id:'newer',assetId:'video',status:'cancelled'},
  ]});
  assert.match(html,/>下载中</);
  assert.match(html,/65%/);
  assert.doesNotMatch(html,/下载已取消|已在队列/);
});

test('completed automatic download offers saving the real original', () => {
  const html = render(result,{...base,assets:[{...asset,status:'downloaded'}],jobs:[{assetId:'video',status:'completed'}]});
  assert.match(html,/>下载完成</);
  assert.match(html,/保存到本机/);
  assert.match(html,/\/api\/assets\/video\/file\?download=true/);
});

test('failed or paused download never claims that the automatically requested work is saved', () => {
  const failed = render(result,{...base,jobs:[{assetId:'video',status:'failed',error:'源站暂时拒绝访问'}]});
  assert.match(failed,/>下载失败</);
  assert.match(failed,/源站暂时拒绝访问/);
  assert.doesNotMatch(failed,/>下载完成</);
  assert.match(render(result,{...base,jobs:[{assetId:'video',status:'paused'}]}),/>下载已暂停</);
});

test('an enqueue error remains visible and safely escaped', () => {
  const html = render({...result,downloadSummary:{added:0,blocked:0,duplicates:0,error:'容量不足 <retry>'}});
  assert.match(html,/>下载未开始</);
  assert.match(html,/容量不足 &lt;retry&gt;/);
});

test('public source restrictions do not direct visitors to account authorization', () => {
  const blocked = {...result,status:'needs-user',message:'来源站点要求验证',groups:[]};
  const html = render(blocked);
  assert.match(html,/源站/);
  assert.match(html,/拾帧无需登录/);
  assert.doesNotMatch(html,/data-action="auth-site"|管理站点授权/);
  assert.match(render(blocked,{...base,publicMode:false}),/data-action="auth-site"/);
});

test('old scan-only results keep explicit download controls', () => {
  const {downloadAfterScan,downloadSummary,...legacy} = result;
  const html = render(legacy);
  assert.match(html,/>已完成</);
  assert.match(html,/data-action="intake-download" data-asset="video"/);
  assert.doesNotMatch(html,/>下载中</);
});
