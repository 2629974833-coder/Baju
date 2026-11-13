import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { filterSeriesItems } from '../dist/series-view.js';
import { filterEpisodeEntries, selectCatalogEpisodes, seriesCheckBody } from '../dist/catalog-view.js';

const app = readFileSync(new URL('../dist/app.js', import.meta.url), 'utf8');
const source = app.slice(app.indexOf('function selectedVisibleEpisodes('), app.indexOf('async function submitRealForm('));

function workspace({ request = async () => ({ dismissed: 1 }), read = async () => {} } = {}) {
  const item = {
    id: 'series-a', title: '山野课程', siteId: 'site', catalog: { entries: [
      { episodeKey: '1', ordinal: 1, mediaStatus: 'ready' },
      { episodeKey: '2', ordinal: 2, mediaStatus: 'unresolved' },
      { episodeKey: '3', ordinal: 3, mediaStatus: 'ready' },
    ] },
  };
  const other = { ...item, id: 'series-b', title: '建筑课程' };
  const state = { series: [item, other] };
  const ui = {
    page: 'series', seriesFilters: {query: '山野'}, checkedSeries: [item.id, other.id],
    episodeFilters: {[item.id]: {status: 'unresolved'}}, episodeSelection: {[item.id]: ['1','2']},
    seriesExpanded: {}, seriesDraft: {limit: 3}, modal: null,
  };
  const downloads = [];
  const sandbox = vm.createContext({
    ui, state, stateReadGeneration: 0, filterSeriesItems, filterEpisodeEntries, selectCatalogEpisodes, seriesCheckBody,
    api: {request}, refreshRealState: read, openDownloadOptions: options => downloads.push(options),
    openModal(type, options) { ui.modal = {type, ...options}; }, closeModal() { ui.modal = null; },
    renderPage() {}, renderModal() {}, refreshProcessingJobs() {}, toast() {},
  });
  vm.runInContext(source, sandbox);
  const action = (name, id) => sandbox.handleRealAction(name, {dataset:{id}});
  return { state, ui, sandbox, action, item, downloads };
}

test('series removal freezes the visible checked series, ignoring later scans and hidden selections', async () => {
  const writes = [];
  const { ui, state, sandbox } = workspace({request: async (path, {body}) => { writes.push([path,body]); return {dismissed: 1}; }});
  sandbox.requestSeriesRemoval();
  assert.match(ui.modal.title, /1 部剧/);
  assert.match(ui.modal.message, /重新扫描.*加入剧集库/);
  state.series.push({id:'new',title:'山野课程 2'});
  ui.checkedSeries.push('new');
  await sandbox.dismissSeriesSelection();
  assert.equal(writes[0][0], '/api/series/dismiss');
  assert.deepEqual(Array.from(writes[0][1].ids), ['series-a']);
  assert.deepEqual(Array.from(ui.checkedSeries), ['series-b','new']);
});

test('episode removal retries a failed state read without posting the committed removal twice', async () => {
  const writes = [];
  let reads = 0;
  const { ui, item, sandbox } = workspace({
    request: async (path, {body}) => { writes.push(body); return {dismissed:1}; },
    read: async () => { if (++reads === 1) throw Error('测试读取失败'); },
  });
  sandbox.requestSeriesRemoval(item);
  assert.match(ui.modal.title, /山野课程.*1 集/);
  await sandbox.dismissSeriesSelection();
  assert.equal(ui.modal.busy, false);
  assert.match(ui.modal.error, /移除已保存/);
  await sandbox.dismissSeriesSelection();
  assert.equal(ui.modal, null);
  assert.equal(writes.length, 1);
  assert.equal(sandbox.stateReadGeneration, 1);
  assert.deepEqual(Array.from(writes[0].episodeKeys), ['2']);
  assert.deepEqual(Array.from(ui.episodeSelection[item.id]), ['1']);
});

test('selection and selected actions respect the episode filter while whole-series actions keep their scope', async () => {
  const { ui, item, action, downloads } = workspace();
  ui.episodeFilters[item.id] = {status:'ready'};
  ui.episodeSelection[item.id] = ['1','2'];
  await action('series-download-selected', item.id);
  assert.deepEqual(Array.from(downloads[0].episodeKeys), ['1']);
  await action('series-download-ready', item.id);
  assert.deepEqual(Array.from(downloads[1].episodeKeys), ['1','3']);
  await action('series-select', item.id);
  assert.deepEqual(Array.from(ui.episodeSelection[item.id]), ['1','3']);
  await action('series-select', item.id);
  assert.deepEqual(Array.from(ui.episodeSelection[item.id]), []);
});

test('scan and check submits one explicit set of keys, including free and locked episodes, irrespective of the add-form limit', async () => {
  const writes = [];
  const {ui,item,action} = workspace({request:async(path,{body})=>{
    writes.push([path,Array.from(body.episodeKeys),body.scanAndCheck,body.limit]); return {id:'scan',checkTotal:body.episodeKeys.length};
  }});
  item.catalog.entries[2].mediaStatus='needs-user';
  ui.seriesDraft.limit=1;
  ui.episodeFilters[item.id]={};
  ui.episodeSelection[item.id]=['2','3'];
  await action('series-scan-check',item.id);
  ui.episodeSelection[item.id]=[];
  await action('series-scan-check',item.id);
  ui.episodeFilters[item.id]={status:'needs-user'};
  await action('series-scan-check',item.id);
  assert.deepEqual(writes,[
    ['/api/series/series-a/scan',['2','3'],true,undefined],
    ['/api/series/series-a/scan',['1','2','3'],true,undefined],
    ['/api/series/series-a/scan',['3'],true,undefined],
  ]);
});

test('history links use merged scan IDs and skip dismissed, unrelated or cross-site snapshots', () => {
  const context = vm.createContext({ esc:String });
  vm.runInContext(app.slice(app.indexOf('function indexSeriesByScan('), app.indexOf('function renderScanHistory(')), context);
  const lookup = context.indexSeriesByScan([
    {id:'same-title-A',siteId:'site',scanId:'new-A',sourceScanIds:['old-A','new-A']},
    {id:'same-title-B',siteId:'site',scanId:'scan-B'},
  ]);
  const link = scan => context.scanDirectoryLink(scan, lookup);
  assert.match(link({id:'old-A',siteId:'site'}), /data-id="same-title-A"/);
  assert.match(link({id:'scan-B',siteId:'site'}), /data-id="same-title-B"/);
  assert.equal(link({id:'old-A',siteId:'other'}), '');
  assert.equal(link({id:'old-A',siteId:'site',seriesDismissed:true}), '');
  assert.equal(link({id:'unrelated',siteId:'site',catalog:{title:'same-title-A'}}), '');
});

test('adding a previously removed series clears filters only when its scan is accepted', async () => {
  let accept = false;
  const {ui, sandbox} = workspace({request: async () => {
    if (!accept) throw new Error('当前无法提交');
    return {id:'new-attempt'};
  }});
  const error = {textContent:''};
  const input = {blur() {}};
  Object.assign(sandbox, {normalizeScanUrl: value => value, $: () => error, document: {activeElement:input}});
  vm.runInContext(app.slice(app.indexOf('async function submitRealForm('), app.indexOf('setInterval(() =>', app.indexOf('async function submitRealForm('))), sandbox);
  ui.seriesFilters.siteId = 'other-site';
  const form = {id:'series-scan-form', querySelector:() => null, contains:el => el === input};
  const values = new Map([['url','https://fixture.example/series/one'],['limit','10']]);
  await sandbox.submitRealForm(form, values);
  assert.equal(ui.seriesFilters.siteId, 'other-site');
  assert.equal(error.textContent, '当前无法提交');
  accept = true;
  await sandbox.submitRealForm(form, values);
  assert.equal(ui.seriesFilters.query, '');
  assert.equal(ui.seriesFilters.siteId, '');
  assert.equal(ui.submittedSeriesScanId, 'new-attempt');
});

test('polling announces only the submitted scan after it restores the series, once', async () => {
  const notices = [];
  const scans = [{id:'older',restoredSeriesId:'other'}, {id:'submitted',status:'running'}];
  const context = vm.createContext({
    ui:{page:'series',submittedSeriesScanId:'submitted'}, state:{}, stateReadGeneration:0, serviceError:'',
    api:{state:async () => ({scans})}, toast:message => notices.push(message), renderServiceWarning(){}, $:() => null,
  });
  vm.runInContext(app.slice(app.indexOf('async function refreshRealState('),app.indexOf('const scanStatusLabels')),context);
  await context.refreshRealState();
  assert.deepEqual(notices, []);
  scans[1] = {id:'submitted', status:'completed', restoredSeriesId:'series'};
  await context.refreshRealState();
  await context.refreshRealState();
  assert.deepEqual(notices, ['已重新加入剧集库。']);
});

test('clearing a notice batch freezes its IDs and retries a failed refresh without re-posting', async () => {
  const writes = [];
  let reads = 0;
  const {ui, state, sandbox} = workspace({
    request: async (path, {body}) => { writes.push([path,Array.from(body.scanIds)]); return {dismissed:2}; },
    read: async () => { if (++reads === 1) throw Error('网络暂时不可用'); },
  });
  state.scans = [{id:'first',status:'failed'}, {id:'second',status:'needs-user'}];
  await sandbox.handleRealAction('series-notices-dismiss', {dataset:{ids:'["first","second"]'}});
  assert.match(ui.modal.title,/当前 2 条/);
  state.scans.unshift({id:'new',status:'failed'});
  await sandbox.dismissScanNotices();
  assert.equal(ui.modal.confirmLabel,'重新读取目录');
  await sandbox.dismissScanNotices();
  assert.equal(ui.modal,null);
  assert.deepEqual(writes,[['/api/series/scan-notices/dismiss',['first','second']]]);
});

test('coin spending is scoped to a capable show, an explicit opt-in and its frozen selection', async () => {
  const writes = [];
  const {state,ui,item,action} = workspace({request:async (_, {body})=>{
    writes.push(JSON.parse(JSON.stringify(body)));return {id:'scan'};
  }});
  state.mode='real';
  state.coinUnlockSiteIds=['site'];
  ui.episodeFilters[item.id] = {status:'unresolved'};
  await action('series-scan-check',item.id);
  ui.coinDrafts = {[item.id]:{enabled:true,budget:'7'}};
  await action('series-scan-check',item.id);
  ui.coinDrafts[item.id].budget = '';
  await action('series-scan-check',item.id);
  ui.coinDrafts[item.id].budget = '0.5';
  await action('series-scan-check',item.id);
  assert.equal(writes.length,3);
  state.coinUnlockSiteIds = [];
  await action('series-scan-check',item.id);
  assert.deepEqual(writes.map(x=>x.autoUnlock),[undefined,{maxCoins:7},{},undefined]);
  assert.ok(writes.every(body => body.episodeKeys.join() === '2'));
});

test('two clicks while a paid check is submitting create one scan', async () => {
  const writes = [];
  let accept;
  const {state,ui,item,action} = workspace({request: (_, {body})=>{
    writes.push(body); return new Promise(resolve => { accept = resolve; });
  }});
  state.mode = 'real';
  state.coinUnlockSiteIds = ['site'];
  ui.coinDrafts = {[item.id]:{enabled:true,budget:'12'}};
  const first = action('series-scan-check', item.id);
  await action('series-scan-check', item.id);
  assert.equal(writes.length,1);
  assert.equal(writes[0].autoUnlock.maxCoins,12);
  accept({checkTotal:1});
  await first;
});
