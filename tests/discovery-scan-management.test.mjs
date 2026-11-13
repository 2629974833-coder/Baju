import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import * as catalogView from '../dist/catalog-view.js';

const app = readFileSync(new URL('../dist/app.js', import.meta.url), 'utf8');
const between = (start, end) => app.slice(app.indexOf(start), app.indexOf(end));
const controller = between('function selectedVisibleEpisodes(', 'async function submitRealForm(');
const scansView = between('function renderRealScans(', 'function renderCatalogEmpty(');

function scan(id) {
  return {
    id, status: 'completed', siteId: 'same-site', url: 'https://example.test/show', catalog: {
      title: '花园', entries: [
        { episodeKey:'1', ordinal:1, mediaStatus:'downloaded' },
        { episodeKey:'2', ordinal:2, mediaStatus:'ready' },
        { episodeKey:'3', ordinal:3, mediaStatus:'downloaded' },
      ],
    },
  };
}
function workspace({ request = async () => ({dismissed:1}), read = async () => {} } = {}) {
  const scans = ['a/1','a/2','a/3','a/4'].map(scan);
  const ui = {page:'discover', discoverySelection:{}, discoveryFilters:{}, discoveryExpanded:{}, discoveryGroupOpen:{}, discoveryRecordsOpen:true, discoveryGroupPages:{}, discoveryScanLimit:3};
  const state = {scans,series:[]};
  const downloads=[], notices=[];
  const sandbox = vm.createContext({
    ...catalogView, state, ui, stateReadGeneration:0, api:{request}, refreshRealState:read,
    openModal(type, data) { ui.modal={type,...data}; }, closeModal() { ui.modal=null; },
    toast(message){notices.push(message);}, renderPage(){}, renderModal(){},
    findSite:()=>({name:'山水'}), icon:()=>'', esc:String,scanStatusLabels:{},document:{querySelectorAll:()=>[]},
    openDownloadOptions: options=>downloads.push(options),
  });
  vm.runInContext(between('function indexSeriesByScan(', 'function renderScanHistory(')+'\n'+controller+'\n'+scansView, sandbox);
  const action = (name,id=scans[0].id) => sandbox.handleRealAction(name,{dataset:{id}});
  return {sandbox, ui, scans, action, downloads, state, notices};
}
test('unselected, filtered-out and stale removal clicks explain the next step without writing', async () => {
  const writes=[];
  const {ui,scans,action,notices}=workspace({request:async (...args)=>writes.push(args)});
  await action('catalog-dismiss-episodes');
  assert.match(notices.at(-1),/先勾选/);
  ui.discoveryFilters[scans[0].id]={query:'无匹配'};
  await action('catalog-dismiss-episodes');
  assert.match(notices.at(-1),/筛选/);
  scans[0].discoveryCatalogDismissed=true;
  await action('catalog-dismiss-all');
  assert.match(notices.at(-1),/已更新/);
  await action('catalog-dismiss-group','expired-group');
  assert.match(notices.at(-1),/整组|扫描组/);
  assert.equal(ui.modal,undefined);
  assert.deepEqual(writes,[]);
});
test('discovery card filters scope bulk selection and removal to frozen keys from a single scan', async () => {
  const writes=[];
  const {ui,scans,action,downloads}=workspace({request:async (path,{body})=>{writes.push([path,JSON.parse(JSON.stringify(body))]);return {dismissed:1};}});
  const [first,second]=scans;
  ui.discoveryFilters[first.id]={status:'downloaded'};
  ui.discoverySelection[second.id]=['1','2'];
  first.discoveryDismissedEpisodeKeys=['3'];
  await action('catalog-select');
  assert.deepEqual(Array.from(ui.discoverySelection[first.id]),['1']);
  ui.discoverySelection[first.id].push('2','3');
  await action('catalog-download');
  assert.deepEqual(Array.from(downloads[0].episodeKeys),['1']);
  await action('catalog-dismiss-episodes');
  assert.match(ui.modal.title,/1 集/);
  assert.match(ui.modal.message,/山水 · 扫描 a\/1/);
  delete ui.discoveryFilters[first.id];
  ui.discoverySelection[first.id].push('late');
  await action('confirm-scan-catalog-dismiss');
  assert.deepEqual(writes,[['/api/scans/a%2F1/discovery-dismiss',{scope:'episodes',episodeKeys:['1']}]]);
  assert.deepEqual(ui.discoverySelection[second.id],['1','2']);
});
test('whole-card confirmation submits once even when its following read fails and is retried', async () => {
  let release, writes=0,reads=0;
  const {ui,scans,action}=workspace({request:async(_,{body})=>{
    writes++;assert.deepEqual(JSON.parse(JSON.stringify(body)),{scope:'catalog'});
    return new Promise(resolve=>{release=resolve;});
  },read:async()=>{if(++reads===1)throw Error('offline');}});
  scans[0].createdAt='2026-10-01T04:00:00Z';
  scans[0].discoveryDismissedEpisodeKeys=['2'];
  await action('catalog-dismiss-all');
  assert.match(ui.modal.title,/2 集/);
  assert.match(ui.modal.message,/山水 · 2026/);
  const first=action('confirm-scan-catalog-dismiss'); await action('confirm-scan-catalog-dismiss');
  assert.equal(writes,1);
  release({dismissed:1,discoveryCatalogDismissed:true}); await first;
  assert.equal(ui.modal.busy,false);assert.match(ui.modal.error,/offline/);
  await action('confirm-scan-catalog-dismiss');
  assert.equal(writes,1);assert.equal(reads,2);assert.equal(ui.modal,null);
});
test('older per-scan cards remain reachable even if newer snapshots have the same URL or are hidden',async()=>{
  const {scans,sandbox,action}=workspace();
  assert.ok(!sandbox.renderRealScans().includes('data-discovery-catalog="a/4"'));
  assert.match(sandbox.renderRealScans(),/显示更多扫描结果/);
  await action('catalog-show-more');
  assert.ok(sandbox.renderRealScans().includes('data-discovery-catalog="a/4"'));
  scans.slice(0,3).forEach(s=>{s.discoveryCatalogDismissed=true;s.message='HIDDEN';});
  assert.ok(!sandbox.renderRealScans().includes('HIDDEN'));
  assert.ok(sandbox.renderRealScans().includes('data-discovery-catalog="a/4"'));
  scans[0].status='running';
  assert.ok(sandbox.renderRealScans().includes('data-action="cancel-scan"'));
  assert.ok(!sandbox.renderRealScans().includes('data-discovery-catalog="a/1"'));
});

test('repeated scans stay in one group after dismissing a card; matching a URL or title is insufficient', async () => {
  const {ui, scans, state, action, sandbox, notices} = workspace({
    request: async () => { scans[0].discoveryCatalogDismissed = true; return {dismissed:1, discoveryCatalogDismissed:true}; },
  });
  const group = {id:'verified-group',title:'花园',siteId:'same-site',sourceScanIds:scans.slice(0,3).map(scan=>scan.id)};
  state.series = [group];
  scans[2].status = 'running';
  let html = sandbox.renderRealScans();
  assert.match(html,/2 次扫描目录/);
  assert.doesNotMatch(html,/data-discovery-catalog="a\/1"/);
  assert.match(html,/data-discovery-catalog="a\/4"/);
  assert.match(html,/data-action="cancel-scan"/);
  await action('catalog-toggle-group',group.id);
  assert.match(sandbox.renderRealScans(),/data-discovery-catalog="a\/1"/);
  await action('catalog-dismiss-all',scans[0].id);
  await action('confirm-scan-catalog-dismiss');
  assert.match(notices.at(-1),/还剩 1 次/);
  assert.equal(ui.discoveryGroupOpen[group.id],false);
  html = sandbox.renderRealScans();
  assert.match(html,/1 次扫描目录/);
  assert.doesNotMatch(html,/data-discovery-catalog="a\/2"/);
  await action('catalog-toggle-group',group.id);
  assert.match(sandbox.renderRealScans(),/data-discovery-catalog="a\/2"/);
});

test('group confirmation freezes only visible terminal scan IDs and retries the read without repeating the write', async () => {
  let reads=0;
  const writes=[];
  const {scans, state, ui, action} = workspace({
    request:async(path,{body})=>{writes.push([path,Array.from(body.scanIds)]);return {dismissed:2};},
    read:async()=>{if(++reads===1)throw Error('offline');},
  });
  const group = {id:'verified-group',title:'花园',siteId:'same-site',sourceScanIds:scans.map(scan=>scan.id)};
  state.series=[group];
  scans[1].status='running';scans[2].discoveryCatalogDismissed=true;
  await action('catalog-dismiss-group',group.id);
  assert.match(ui.modal.title,/2 次/);
  const late=scan('after-confirmation');group.sourceScanIds.push(late.id);scans.push(late);
  await action('confirm-scan-catalog-dismiss');
  assert.match(ui.modal.error,/offline/);
  await action('confirm-scan-catalog-dismiss');
  assert.deepEqual(writes,[['/api/scans/discovery-dismiss-group',['a/1','a/4']]]);
  assert.equal(reads,2);
});

test('a removed series remains manageable by its authoritative discovery ID', async () => {
  const {scans,ui,action,sandbox} = workspace();
  for (const scan of scans.slice(0,2)) {
    scan.seriesDismissed=true;
    scan.discoverySeriesId='removed-series';
  }
  assert.match(sandbox.renderRealScans(), /2 次扫描目录/);
  await action('catalog-dismiss-group','removed-series');
  assert.deepEqual(Array.from(ui.modal.body.scanIds),['a/1','a/2']);
  assert.match(ui.modal.title,/花园/);
});

test('collapsed records expose active scans without building every directory, and reopen on demand', async () => {
  const {ui, scans, sandbox, action} = workspace();
  ui.discoveryRecordsOpen = false;
  scans[0].status = 'running';
  assert.match(sandbox.renderRealScans(), /1 项进行中/);
  assert.doesNotMatch(sandbox.renderRealScans(), /data-discovery-catalog=|data-action="cancel-scan"/);
  await action('catalog-toggle-records');
  assert.match(sandbox.renderRealScans(), /aria-expanded="true"/);
  assert.match(sandbox.renderRealScans(), /data-action="cancel-scan"/);
  assert.match(sandbox.renderRealScans(), /data-discovery-catalog="a\/2"/);
  scans[0].status = 'completed';
  assert.match(sandbox.renderRealScans(), /data-discovery-catalog="a\/2"/);
  await action('catalog-toggle-records');
  assert.doesNotMatch(sandbox.renderRealScans(), /data-discovery-catalog=/);
});

test('a repeated directory and scan history each render only their requested page', async () => {
  const {state, ui, sandbox, action} = workspace();
  state.scans = Array.from({length:31}, (_, i) => ({...scan('scan-'+i), url:'https://example.test/'+i}));
  state.series = [{id:'group', title:'花园', siteId:'same-site', sourceScanIds:state.scans.map(s=>s.id)}];
  await action('catalog-toggle-group','group');
  assert.match(sandbox.renderRealScans(), /data-discovery-catalog="scan-0"/);
  assert.doesNotMatch(sandbox.renderRealScans(), /data-discovery-catalog="scan-30"/);
  await sandbox.handleRealAction('catalog-group-page',{dataset:{id:'group',value:'2'}});
  assert.doesNotMatch(sandbox.renderRealScans(), /data-discovery-catalog="scan-0"/);
  assert.equal(ui.discoveryGroupOpen.group,true);
  vm.runInContext(between('function renderScanHistory(', 'function openScanSeries('), sandbox);
  sandbox.modalHeader = title => title;
  ui.modal = {type:'scan-history'};
  assert.equal((sandbox.renderScanHistory().match(/class="scan-history-row"/g)||[]).length,10);
  await sandbox.handleRealAction('scan-history-page',{dataset:{value:'4'}});
  assert.equal((sandbox.renderScanHistory().match(/class="scan-history-row"/g)||[]).length,1);
  state.scans = state.scans.slice(0,4);
  assert.equal((sandbox.renderScanHistory().match(/class="scan-history-row"/g)||[]).length,4);
});
