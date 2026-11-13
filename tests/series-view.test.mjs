import test from 'node:test';
import assert from 'node:assert/strict';
import * as seriesView from '../dist/series-view.js';
import { renderEpisodeCatalog } from '../dist/catalog-view.js';
import { downloadRequest } from '../dist/download-options-view.js';

const catalog = {
  entries: [
    {episodeKey:'E:1',ordinal:1,mediaStatus:'downloaded',downloaded:true},
    {episodeKey:'E:2',ordinal:2,mediaStatus:'ready',downloadAssetId:'a2'},
    {episodeKey:'E:3',ordinal:3,mediaStatus:'needs-user',access:'needs-user'},
    {episodeKey:'E:4',ordinal:4,mediaStatus:'unresolved'},
  ],
  enumeratedCount:4, expectedCount:5, resolvedCount:2, downloadedCount:1,
  blockedCount:1, ambiguousCount:0, missingCount:1, missingEpisodeKeys:['E:5'],
};
const series = {id:'series/a',scanId:'s-new',siteId:'site',title:'示例 <剧集>',url:'https://fixture.example/series/one',status:'completed',catalog};
const state = {mode:'real',series:[series],sites:[{id:'site',name:'测试站点'}],scans:[],seriesArchiveAvailable:true};

test('series library starts from one URL and groups the full catalog with honest coverage', () => {
  const html = seriesView.renderSeriesLibrary(state);
  assert.match(html, /id="series-scan-form"/);
  assert.match(html, /详情页或任意一集/);
  assert.match(html, /示例 &lt;剧集&gt;/);
  assert.match(html, /测试站点/);
  assert.match(html, /可下载<\/span><strong>1/);
  assert.match(html, /已保存<\/span><strong>1/);
  assert.match(html, /待解析<\/span><strong>1/);
  assert.match(html, /需权限<\/span><strong>1/);
  assert.match(html, /目录缺集 1 集/);
  assert.match(html, /data-action="series-scan-check"/);
  assert.match(html, /data-action="series-download-ready"/);
  assert.match(html, /\/api\/series\/series%2Fa\/archive/);
  assert.doesNotMatch(html, /\/api\/scans\/s-new\/archive|已完整|全部齐全/);
});

test('aggregated catalog actions and checkboxes stay on series identity, preserving collapsed state', () => {
  const html = renderEpisodeCatalog({...series, id:series.id}, ['E:2'], {seriesId:series.id, archiveAvailable:true, open:false});
  assert.match(html, /data-series-catalog="series\/a"/);
  assert.doesNotMatch(html, /<details[^>]*\sopen[ >]/);
  assert.match(html, /data-action="series-select-ready"/);
  assert.match(html, /data-action="series-scan-check"/);
  assert.match(html, /data-action="series-download-selected"/);
  assert.match(html, /data-episode-scan="series\/a" value="E:2" checked/);
  assert.doesNotMatch(html, /data-action="catalog-|本次实际/);
});

test('unfinished and failed scans without a recognized catalog remain visible', () => {
  const html = seriesView.renderSeriesLibrary({...state, series:[], scans:[
    {id:'s1',mode:'series',status:'failed',error:'目录解析 <失败>',pagesVisited:1,url:'https://fixture.example/a'},
    {id:'s2',mode:'series',status:'completed',pagesVisited:1,url:'https://fixture.example/b'},
    {id:'s3',mode:'series',status:'running',pagesVisited:2,url:'https://fixture.example/c'},
    {id:'other',mode:'page',status:'completed',message:'不相关记录'},
  ]});
  assert.match(html, /目录解析 &lt;失败&gt;/);
  assert.match(html, /尚未识别到剧集目录/);
  assert.match(html, /data-action="cancel-scan" data-id="s3"/);
  assert.doesNotMatch(html, /不相关记录|整剧扫描成功/);
});

test('active scan prevents duplicate series scans and completed download set is not reselected', () => {
  const html = seriesView.renderSeriesLibrary({...state, scans:[{id:'busy',status:'running',mode:'page'}]});
  assert.match(html, /data-action="series-scan-check"[^>]*disabled/);
  assert.match(html, /type="submit"[^>]*disabled/);
  assert.match(html, /data-action="series-download-ready"[^>]*>[^<]*下载本剧可用集（1）/);
  assert.deepEqual(downloadRequest({seriesId:'series/a',episodeKeys:['E:2']}, {maxHeight:720,audioLanguage:'auto'}), {
    path:'/api/series/series%2Fa/downloads', body:{episodeKeys:['E:2'],options:{maxHeight:720,audioLanguage:'auto'}},
  });
});

test('series form draft and unknown total survive rendering without introducing untrusted links', () => {
  const html = seriesView.renderSeriesLibrary({...state,series:[{...series,url:'javascript:alert(1)',catalog:{...catalog,expectedCount:null}}]}, {
    draft:{url:'https://fixture.example/?x="<x>',siteId:'site',limit:250},expanded:{'series/a':false},
  });
  assert.match(html, /总数未知/);
  assert.match(html, /value="https:\/\/fixture.example\/\?x=&quot;&lt;x&gt;"/);
  assert.match(html, /value="250"/);
  assert.match(html, /value="site" selected/);
  assert.doesNotMatch(html, /href="javascript:|<details[^>]*\sopen[ >]/);
});

test('conflicting directories are explained even when no safe catalog can be formed', () => {
  const html = seriesView.renderSeriesLibrary({...state, series:[], scans:[{
    id:'conflict', mode:'series', status:'completed', url:'https://fixture.example/series',
    catalog:{entries:[], reasons:['catalog-conflict']},
  }]});
  assert.match(html, /目录编号或归属存在冲突/);
  const card = seriesView.renderSeriesLibrary({...state, series:[{...series,catalog:{...catalog,reasons:['catalog-conflict']}}]});
  assert.match(card, /目录编号或归属存在冲突/);
});

test('one scan-and-check handles a show, selected locked-only episodes, and filtered results', () => {
  const render = options => seriesView.renderSeriesLibrary(state, options);
  const action = /<button[^>]*data-action="series-scan-check"[^>]*>(.*?)<\/button>/;
  const all = render({});
  assert.match(all.match(action)[1], /扫描并检查全剧（4 集）/);
  assert.match(all, /data-action="auth-site" data-id="site"/);
  assert.doesNotMatch(all, /series-recheck-access|series-recheck-selected|扫描所选集/);
  const locked = render({selected:{[series.id]:['E:3']}});
  assert.match(locked.match(action)[1], /扫描并检查所选集（1 集）/);
  assert.doesNotMatch(locked.match(action)[0], /disabled/);
  const filtered = render({episodeFilters:{[series.id]:{status:'needs-user'}}});
  assert.match(filtered.match(action)[1], /扫描并检查当前结果（1 集）/);
  const empty = render({episodeFilters:{[series.id]:{query:'no episode'}}});
  assert.match(empty.match(action)[0], /disabled/);
});

test('series shows the actual progress and terminal result of the combined check', () => {
  const render = (status, checkedCount, message) => seriesView.renderSeriesLibrary({
    ...state,scans:[{id:series.scanId,status,checkTotal:4,checkedCount,message,pagesVisited:2}]
  });
  assert.match(render('running',1,'已检查部分剧集'), /已检查 1 \/ 4 集/);
  const finished = render('completed',4,'3 集可下载，1 集需要权限');
  assert.match(finished,/已检查 4 \/ 4 集/);
  assert.match(finished,/3 集可下载，1 集需要权限/);
});

test('catalog discovery sources use readable labels and omit unrecognized internals', () => {
  const html = renderEpisodeCatalog({...series,catalog:{...catalog,discoverySources:['dom','json-ld','private-field','dom']}});
  assert.match(html, /目录依据：页面选集、结构化数据/);
  assert.doesNotMatch(html, /discoverySources|json-ld|private-field/);
  const unknown = renderEpisodeCatalog({...series,catalog:{...catalog,discoverySources:['<script>']}});
  assert.doesNotMatch(unknown, /目录依据|<script>|&lt;script&gt;/);
});

test('structured directory failures have plain labels even without a usable catalog', () => {
  for (const [reason, label] of [
    ['structured-limit', '页面目录数据超过读取上限，结果可能不完整'],
    ['structured-invalid', '部分页面目录数据无法读取'],
    ['structured-conflict', '页面目录数据相互冲突，请检查来源页面'],
  ]) {
    const card = renderEpisodeCatalog({...series,catalog:{...catalog,reasons:[reason]}});
    assert.ok(card.includes(label));
    const empty = seriesView.renderSeriesLibrary({...state,series:[],scans:[{
      id:'structured-failure',mode:'series',status:'completed',url:'https://fixture.example/series',
      catalog:{entries:[],reasons:[reason]},
    }]});
    assert.ok(empty.includes(label));
    assert.doesNotMatch(empty, /structured-(limit|invalid|conflict)|整剧扫描成功/);
  }
});

test('series manager scopes bulk removal to series visible through both filters', () => {
  const other = {...series, id:'series/b', title:'静物教程', siteId:'another', catalog:{...catalog,seriesKey:'V02'}};
  const first = {...series, catalog:{...catalog,seriesKey:'V01'}};
  const items = [first, other];
  const filters = {query:' v01 ',siteId:'site'};
  assert.deepEqual(seriesView.filterSeriesItems(items, filters), [first]);
  assert.deepEqual(seriesView.filterSeriesItems(items, {...filters,siteId:'another'}), []);
  const view = seriesView.renderSeriesLibrary({...state,series:items}, {filters,checkedSeries:['series/b']});
  assert.match(view, /示例 &lt;剧集&gt;/);
  assert.doesNotMatch(view, /静物教程|data-action="series-dismiss-checked"/);
  const checked = seriesView.renderSeriesLibrary({...state,series:items}, {filters,checkedSeries:['series/a','series/b']});
  assert.match(checked, /已选 1 部/);
  assert.match(checked, /data-action="series-dismiss-checked"/);
});

test('filter empty states and dismissed scans have distinct messages and original scan stays out of recent', () => {
  const filtered = seriesView.renderSeriesLibrary(state, {filters:{query:'no-match'}});
  assert.match(filtered, /没有符合筛选条件的剧集/);
  assert.doesNotMatch(filtered, /还没有归纳的剧集|尚未识别到剧集目录/);
  const emptied = seriesView.renderSeriesLibrary({...state,series:[],dismissedSeriesCount:1,scans:[
    {id:'old',mode:'series',status:'completed',url:'https://fixture.example/hidden-title',seriesDismissed:true},
  ]});
  assert.match(emptied, /整剧已全部移除/);
  assert.match(emptied, /重新扫描.*加入剧集库/);
  assert.doesNotMatch(emptied, /hidden-title|尚未识别到剧集目录/);
});

test('multiple shows start compact, and an explicit open choice survives rendering', () => {
  const data = {...state,series:[series,{...series,id:'series/b',title:'静物教程'}]};
  const compact = seriesView.renderSeriesLibrary(data);
  assert.doesNotMatch(compact, /<details[^>]*\sopen[ >]/);
  const single = seriesView.renderSeriesLibrary(data,{filters:{query:'静物'},scanOptionsOpen:true});
  assert.match(single, /data-series-catalog="series\/b" open/);
  assert.match(single, /data-series-scan-settings open/);
  const retained = seriesView.renderSeriesLibrary(data,{expanded:{'series/a':true}});
  assert.match(retained, /data-series-catalog="series\/a" open/);
});

test('custom titles are editable, safely displayed and searchable by source name or series code', () => {
  const item = {...series, title: '<晨光>', customTitle:'<晨光>', sourceTitle:'原剧名', catalog:{...catalog,seriesKey:'C13'}};
  const html = seriesView.renderSeriesLibrary({...state, series:[item]});
  assert.match(html, /name="customTitle"/);
  assert.match(html, /data-action="series-rename"/);
  assert.match(html, /&lt;晨光&gt;/);
  assert.match(html, /C13/);
  for (const query of ['晨光','原剧名','C13']) assert.deepEqual(seriesView.filterSeriesItems([item],{query}),[item]);
});

test('an explicit re-add attempt shows progress or failure while older dismissed scans stay hidden', () => {
  for (const [status, detail] of [['running','读取最新目录'], ['failed','站点目录暂不可用'], ['completed','尚未识别到剧集目录']]) {
    const hidden = {mode:'series', siteId:'site', seriesDismissed:true};
    const html = seriesView.renderSeriesLibrary({...state, series:[], dismissedSeriesCount:1, scans:[
      {...hidden,id:'current',status,url:'https://fixture.example/book/one',message:detail},
      {...hidden,id:'older',status:'completed',url:'https://fixture.example/book/two',message:'历史扫描'},
    ]}, {submittedScanId:'current'});
    assert.ok(html.includes(detail));
    assert.doesNotMatch(html,/历史扫描|book\/two/);
  }
});

test('dismissed notices suppress older attempts at the same URL while a new attempt remains visible', () => {
  const base = {mode:'series', status:'needs-user', siteId:'fixture', url:'https://fixture.example/show'};
  const notices = [
    {...base, id:'a2', seriesNoticeDismissed:true, message:'removed'},
    {...base, id:'a1', message:'stale duplicate'},
    {...base, id:'b1', url:'https://fixture.example/second', message:'still visible'},
  ];
  const render = scans => seriesView.renderSeriesLibrary({...state, series:[],scans});
  assert.doesNotMatch(render(notices), /removed|stale duplicate/);
  const html = render([{...base, id:'a3',message:'fresh attempt'}, ...notices]);
  assert.match(html,/fresh attempt/);
  assert.match(html,/still visible/);
  assert.match(html,/清除当前提示（2）/);
  assert.match(html,/data-action="series-notice-retry" data-id="a3"/);
  assert.doesNotMatch(html,/stale duplicate/);
});

test('grouped scan errors can be cleared without hiding the group or its actions', () => {
  const scan = {id:series.scanId, mode:'series', status:'failed', error:'临时断连'};
  const render = flags => seriesView.renderSeriesLibrary({...state, scans:[{...scan,...flags}]});
  assert.match(render({}), /清除提示/);
  assert.doesNotMatch(render({seriesNoticeDismissed:true}), /临时断连/);
  assert.match(render({seriesNoticeDismissed:true}), /重新读取目录/);
});

test('single episode export targets exactly one visible saved original, including a saved episode with an expired session', () => {
  const render = (selected, overrides = {}) => renderEpisodeCatalog(series, selected, {seriesId:series.id, archiveAvailable:true, ...overrides});
  assert.match(render(['E:1']), /href="\/api\/series\/series%2Fa\/episode-file\?episodeKey=E%3A1" download>导出第 1 集原件/);
  assert.match(render(['E:1']), /href="\/api\/series\/series%2Fa\/archive" download/);
  for (const selection of [[], ['E:2'], ['E:1','E:2']]) assert.doesNotMatch(render(selection), /episode-file/);
  assert.doesNotMatch(render(['E:1'], {filter:{status:'ready'}}), /episode-file/);
  const locked = {...series, catalog:{...catalog, entries:[{...catalog.entries[0],mediaStatus:'needs-user',access:'needs-user'}]}};
  assert.match(renderEpisodeCatalog(locked, ['E:1'], {seriesId:series.id,archiveAvailable:true}), /episode-file/);
});

test('timeouts and protected media are clearable in both grouped and ungrouped reports', () => {
  const statuses = [['timed-out','扫描超时'], ['unsupported-drm','媒体受 DRM 保护']];
  const scans = statuses.map(([status], i) => ({id:'terminal'+i, mode:'series', status, url:'https://fixture.example/'+i}));
  const html = seriesView.renderSeriesLibrary({...state, series:[], scans});
  assert.match(html,/清除当前提示（2）/);
  for (const [i, [, label]] of statuses.entries()) {
    assert.ok(html.includes(label));
    const grouped = seriesView.renderSeriesLibrary({...state, scans:[{...scans[i],id:series.scanId}]});
    assert.ok(grouped.includes(label) && grouped.includes('清除提示'));
  }
});

test('series card and scan summary distinguish published episodes from the eventual series length', () => {
  const published = {...catalog, publishedCount:4, expectedCount:4, ongoing:true, missingCount:0, missingEpisodeKeys:[]};
  const item = {...series, catalog:published};
  const library = seriesView.renderSeriesLibrary({...state,series:[item]});
  assert.match(library, /站点已发布 4 集/);
  assert.doesNotMatch(library, /页面标示 4 集/);
  assert.match(renderEpisodeCatalog(item), /站点已发布 4 集/);
  const partial = {...item, catalog:{...published, truncated:true, expectedCount:null}};
  assert.doesNotMatch(seriesView.renderSeriesLibrary({...state,series:[partial]}), /站点已发布 4 集/);
});

test('series poster uses local directory artwork before a video thumbnail and keeps a visual fallback', () => {
  const html = seriesView.seriesPoster({...series,posterUrl:'/api/series/series%2Fa/poster'},state,{'s:1':'/api/assets/a/thumbnail'});
  assert.match(html,/src="\/api\/series\/series%2Fa\/poster"/);
  assert.doesNotMatch(html,/\/api\/assets\/a\/thumbnail/);
  assert.match(html,/is-type/);
});

test('only supported shows offer opt-in coin spending, and the visible scope and budget appear before submission', () => {
  const capable = {...state, coinUnlockSiteIds:['site']};
  const options = {selected:{[series.id]:['E:2','E:3']}, coinDrafts:{[series.id]:{enabled:true,budget:'7'}}};
  const unsupported = seriesView.renderSeriesLibrary(state, options);
  assert.doesNotMatch(unsupported, /data-coin-unlock|自动解锁所选集/);
  const inactive = seriesView.renderSeriesLibrary(capable);
  assert.match(inactive, /data-coin-unlock=/);
  assert.doesNotMatch(inactive, /data-coin-unlock=[^>]* checked|data-coin-budget=/);
  const html = seriesView.renderSeriesLibrary(capable, options);
  assert.match(html, /data-coin-unlock=[^>]* checked/);
  assert.match(html, /data-coin-budget=[^>]*value="7"/);
  assert.match(html, /扫描并自动解锁所选集（2 集）/);
  assert.match(html, /站点报价核对账号余额和剩余预算/);
  assert.doesNotMatch(html, /权限检查只读取现有访问权限/);
  const withSite = siteId => seriesView.renderSeriesLibrary({
    ...state, series:[{...series,siteId}], coinUnlockSiteIds:[siteId],
  }, options);
  assert.match(withSite('lustflix'), /若站点临时改价，以站点结算为准/);
  assert.doesNotMatch(withSite('chunlou'), /若站点临时改价/);
  const bad = seriesView.renderSeriesLibrary(capable,{coinDrafts:{[series.id]:{enabled:true,budget:'bad'}}});
  assert.match(bad, /data-action="series-scan-check"[^>]*disabled/);
  assert.match(bad, /正整数/);
});

test('paid check result retains confirmed spend, uncertain purchases and skip reasons', () => {
  const paidScan = {id:'old', checkTotal:3, checkedCount:3, status:'completed',
    unlockSummary:{unlocked:1,spent:5,unknown:1,skipped:1,limit:8,reasons:{'余额不足':1},lastMessage:'需核对交易记录',stopped:true}};
  const html = seriesView.renderSeriesLibrary({...state,
    series:[{...series,sourceScanIds:['old',series.scanId]}],
    scans:[{id:series.scanId,status:'completed'},paidScan]});
  assert.match(html, /已解锁 1 集/);
  assert.match(html, /已确认支出 5 金币/);
  assert.match(html, /待核实 1 集/);
  assert.match(html, /余额不足/);
  assert.match(html, /需核对交易记录/);
});

test('each episode shows its durable receipt after interruption even without a playback result', () => {
  const source = {...catalog, entries:catalog.entries.map(e => ({...e, pageUrl:'https://fixture.example/'+e.ordinal}))};
  const item = {...series, catalog:source, sourceScanIds:['interrupted','later',series.scanId]};
  const scans = [
    {id:'other-show', catalog:source, unlockResults:{'E:1':{status:'purchased',spent:80}}},
    {id:series.scanId, catalog:source},
    {id:'later', catalog:source, unlockResults:{'E:1':{status:'skipped',spent:0}, 'E:4':{status:'owned',spent:0,reconciled:true}}},
    {id:'interrupted', catalog:source, status:'failed', unlockResults:{
      'E:1':{status:'purchased',spent:4,message:'单集交易已确认'},
      'E:2':{status:'uncertain',spent:null,message:'服务中断，结果待核对'},
      'E:3':{status:'pending',spent:null,message:'等待站点确认'},
      'E:4':{status:'uncertain',spent:null,message:'前次未核实'},
    }},
  ];
  const options = {selected:{[item.id]:['E:2']}};
  const html = seriesView.renderSeriesLibrary({...state, series:[item], scans},options);
  const tile = n => html.match(new RegExp('value="E:'+n+'"[^>]*>[\\s\\S]*?</label>'))[0];
  assert.match(tile(1), /已付 4 金币/);
  assert.match(tile(2), /扣款待核实/);
  assert.match(tile(3), /付款确认中/);
  assert.match(tile(4), /权益已确认，扣款待核实/);
  assert.match(html, /第 2 集 · 服务中断，结果待核对/);
  assert.doesNotMatch(html, /已付 (80|0) 金币/);
  const moved = {...source, entries:[{...source.entries[0],pageUrl:'https://fixture.example/replaced'}]};
  const changed = seriesView.renderSeriesLibrary({...state, series:[{...item,catalog:moved}], scans},options);
  assert.doesNotMatch(changed, /已付 4 金币/);
});
