import test from 'node:test';
import assert from 'node:assert/strict';
import * as view from '../dist/series-view.js';

const show = (n, extra = {}) => ({
  id:`show-${n}`, title:`剧集 ${n}`, siteId:'lata', scanId:`scan-${n}`,
  categories:['剧情'], artwork:{total:1,saved:0,pending:0,missing:1},
  catalog:{seriesKey:`s-${n}`,entries:[{episodeKey:`${n}:1`,ordinal:1,mediaStatus:'unresolved'}], expectedCount:1,downloadedCount:0},
  ...extra,
});
const state = {mode:'real',seriesArchiveAvailable:true,series:[],scans:[],assets:[],sites:[{id:'lata',name:'Lata Casa'},{id:'other',name:'其他来源'}]};

test('series category and source filters combine without duplicating a show with several categories', () => {
  const first=show(1,{categories:['剧情','喜剧']}); const second=show(2,{siteId:'other',categories:['喜剧']});
  assert.deepEqual(view.filterSeriesItems([first,second],{category:'喜剧',siteId:'lata'}),[first]);
  assert.deepEqual(view.filterSeriesItems([first,second],{category:'剧情',query:'2'}),[]);
  assert.deepEqual(view.filterSeriesItems([first,second],{category:'纪录'}),[]);
});

test('pagination renders twelve shows and clamps stale pages after filtering', () => {
  const series=Array.from({length:28},(_,i)=>show(i+1));
  const html=view.renderSeriesLibrary({...state,series},{filters:{page:2}});
  const ids=[...html.matchAll(/<article[^>]*data-series-id="([^"]+)"/g)].map(m=>m[1]);
  assert.deepEqual(ids,series.slice(12,24).map(s=>s.id));
  assert.match(html,/2 \/ 3/);
  assert.match(html,/data-action="series-page" data-value="3"/);
  assert.match(html,/全选本页/);
  const final=view.seriesPageItems(series,{page:100});
  assert.equal(final.page,3);assert.equal(final.items.length,4);
  const filtered=view.seriesPageItems(series,{query:'剧集 28',page:100});
  assert.equal(filtered.page,1);assert.equal(filtered.total,1);
});

test('source sections and category choices are escaped, and every unparsed show offers one-click download', () => {
  const html=view.renderSeriesLibrary({...state,series:[show(1,{categories:['<喜剧>']}),show(2,{siteId:'other'})]});
  assert.match(html,/data-series-source="lata"/);
  assert.match(html,/data-series-source="other"/);
  assert.match(html,/data-series-filter="category"/);
  assert.match(html,/&lt;喜剧&gt;/);
  assert.doesNotMatch(html,/<喜剧>/);
  const download=html.match(/<button[^>]*data-action="series-download-all"[^>]*data-id="show-1"[^>]*>/)[0];
  assert.doesNotMatch(download,/disabled/);
  assert.match(html,/扫描可访问单集并下载，含海报/);
});

test('saved artwork is part of the visible archive action and an active show cannot be queued again', () => {
  const item=show(1,{artwork:{total:1,saved:1,pending:0,missing:0},catalog:{...show(1).catalog,downloadedCount:1}});
  const html=view.renderSeriesLibrary({...state,series:[item],scans:[{id:item.scanId,status:'running',downloadAfterScan:true}]});
  assert.match(html,/海报已保存/);
  assert.match(html,/导出本剧（含海报）/);
  assert.match(html,/href="\/api\/series\/show-1\/archive"/);
  assert.match(html,/data-action="series-download-all"[^>]*disabled/);
  assert.match(html,/正在扫描并下载/);
});

test('download submission is disabled immediately, before the scan appears in state', () => {
  const html=view.renderSeriesLibrary({...state,series:[show(1)]},{downloadSubmitting:new Set(['show-1'])});
  assert.match(html,/data-action="series-download-all"[^>]*disabled/);
  assert.match(html,/正在提交/);
});

test('site scans stay visible while directories fill the library and expose resume or authorization actions', () => {
  const scans=[
    {id:'current',mode:'site',siteId:'lata',status:'running',pagesVisited:4,siteSummary:{shows:6,episodes:18}},
    {id:'old',mode:'site',siteId:'lata',status:'failed',message:'旧提示'},
    {id:'resume',mode:'site',siteId:'other',status:'completed',canContinue:true,siteSummary:{shows:2,episodes:9}},
  ];
  const html=view.renderSeriesLibrary({...state,scans});
  assert.match(html,/已列出 6 部 · 18 集/);
  assert.match(html,/data-action="cancel-scan" data-id="current"/);
  assert.match(html,/data-action="series-continue-site" data-id="resume"/);
  assert.doesNotMatch(html,/旧提示/);
  const denied=view.renderSiteScanProgress({...state,scans:[{...scans[0],status:'needs-user',message:'请登录'}]});
  assert.match(denied,/data-action="auth-site" data-id="lata"/);
  assert.match(denied,/data-action="series-notice-dismiss" data-id="current"/);
  assert.equal(view.renderSiteScanProgress({...state,scans},'missing'),'');
  assert.doesNotMatch(view.renderSiteScanProgress({...state,scans:[{...scans[0],seriesNoticeDismissed:true},scans[1]]}),/旧提示/);
});

test('a parent site scan reports its warning once rather than repeating it on every show', () => {
  const scan={id:'site-scan',siteId:'lata',mode:'site',status:'failed',error:'目录连接临时中断',siteSummary:{shows:2,episodes:2}};
  const html=view.renderSeriesLibrary({...state,scans:[scan],series:[show(1,{scanId:scan.id}),show(2,{scanId:scan.id})]});
  assert.equal((html.match(/目录连接临时中断/g)||[]).length,1);
});

test('unsaved artwork is not labelled as downloading until a real task exists', () => {
  const item=show(1,{artwork:{total:1,saved:0,pending:1,missing:0,assetIds:['poster']}});
  const html=view.renderSeriesLibrary({...state,series:[item],jobs:[]});
  assert.match(html,/海报待下载/);
  assert.doesNotMatch(html,/海报正在下载/);
  const queued=view.renderSeriesLibrary({...state,series:[item],jobs:[{assetId:'poster',status:'queued'}]});
  assert.match(queued,/海报已入队/);
});

test('archive waits for an active poster job and becomes available after failure or completion', () => {
  const item=show(1,{artwork:{total:1,saved:0,assetIds:['poster']},catalog:{...show(1).catalog,downloadedCount:1}});
  for (const status of ['queued','running']) {
    const html=view.renderSeriesLibrary({...state,series:[item],jobs:[{assetId:'poster',status}]});
    assert.match(html,/等待海报保存/);
    assert.doesNotMatch(html,/href="\/api\/series\/show-1\/archive"/);
  }
  const failed=view.renderSeriesLibrary({...state,series:[item],jobs:[{assetId:'poster',status:'failed'}]});
  assert.match(failed,/海报下载失败/);
  assert.match(failed,/href="\/api\/series\/show-1\/archive"/);
});

test('completed whole-show scans report queued files and inaccessible episodes', () => {
  const item=show(1);
  const scan={id:item.scanId,status:'completed',checkTotal:3,checkedCount:3,downloadAfterScan:true,downloadSummary:{added:2,duplicates:1,unavailable:['E3']}};
  const html=view.renderSeriesLibrary({...state,series:[item],scans:[scan]});
  assert.match(html,/已加入 2 个下载任务/);
  assert.match(html,/1 项已保存或已在队列/);
  assert.match(html,/1 集暂不可下载/);
  const failed=view.renderSeriesLibrary({...state,series:[item],scans:[{...scan,downloadSummary:{error:'存储暂时不可用'}}]});
  assert.match(failed,/存储暂时不可用/);
});

test('an indexed show with a pending directory can be downloaded without manually entering its URL again', () => {
  const item=show(1,{directoryPending:true,catalog:{entries:[],expectedCount:null}});
  const html=view.renderSeriesLibrary({...state,series:[item]});
  assert.match(html,/选集目录待读取/);
  assert.doesNotMatch(html,/目录内 0 集/);
  const button=html.match(/<button[^>]*data-action="series-download-all"[^>]*>/)[0];
  assert.doesNotMatch(button,/disabled/);
});
