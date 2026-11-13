import test from 'node:test';
import assert from 'node:assert/strict';
import { renderEpisodeCatalog } from '../dist/catalog-view.js';
import * as catalogView from '../dist/catalog-view.js';
import { downloadCaption, renderQueue } from '../dist/workflow-view.js';

test('catalog separates enumerated, resolved and saved, unknown total remains unknown', () => {
  const html = renderEpisodeCatalog({id:'s1',catalog:{entries:[{episodeKey:'C:1',ordinal:1,title:'<script>',access:'unknown',assetIds:[]}], enumeratedCount:1,resolvedCount:0,downloadedCount:0,expectedCount:null,reasons:[]}}, []);
  assert.match(html, /总数未知/);
  assert.match(html, /已解析 0/);
  assert.match(html, /已保存 0/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /catalog-scan/);
  assert.match(html, /catalog-download/);
});

test('needs-user queue exposes recovery controls and sanitized reason', () => {
  assert.match(downloadCaption({status:'running',phase:'refreshing'}), /刷新/);
  const html=renderQueue({mode:'real',sites:[],assets:[{id:'a',title:'A',siteId:'site'}],jobs:[{id:'j',assetId:'a',status:'needs-user',error:'需要权限'}]});
  assert.match(html, /等待授权/);
  assert.match(html, /管理权限/);
  assert.match(html, /data-value="retry"/);
  assert.match(html, /需要权限/);
});

const entries = [
  {episodeKey:'C:1',ordinal:1,mediaStatus:'ready',downloadAssetId:'a1',assetIds:['a1','a1-alt']},
  {episodeKey:'C:2',ordinal:2,mediaStatus:'downloaded',downloadAssetId:'a2',downloaded:true},
  {episodeKey:'C:3',ordinal:3,mediaStatus:'needs-user',access:'needs-user'},
  {episodeKey:'C:4',ordinal:4,mediaStatus:'unresolved',assetIds:[]},
  {episodeKey:'C:5',ordinal:5,mediaStatus:'ambiguous',assetIds:['a5','b5']},
];

test('a site-published episode list shows its source without promising playable files', () => {
  const catalog = {entries: [{episodeKey:'C13:1', ordinal:1, mediaStatus:'unresolved'}],
    discoverySources:['site-directory'], expectedCount:1, resolvedCount:0, downloadedCount:0};
  const html = renderEpisodeCatalog({id:'scan', catalog}, [], {seriesId:'series'});
  assert.match(html, /目录依据：站点剧集目录/);
  assert.match(html, /待扫描页面/);
  assert.doesNotMatch(html, /主媒体已确认，可下载/);
});

test('catalog distinguishes the selected master, saved file, permission, unresolved and ambiguous entries', () => {
  const html = renderEpisodeCatalog({id:'s1',catalog:{entries,enumeratedCount:5,resolvedCount:2,downloadedCount:1,blockedCount:1,ambiguousCount:1,expectedCount:5}}, ['C:3']);
  assert.match(html, /主媒体已确认，可下载/);
  assert.match(html, /已保存/);
  assert.match(html, /需要站点权限/);
  assert.match(html, /待扫描页面/);
  assert.match(html, /多个正片候选，主媒体待确认/);
  assert.match(html, /需权限 1 集/);
  assert.match(html, /主媒体待确认 1 集/);
  assert.match(html, /value="C:3" checked/);
  assert.match(html, /data-action="catalog-select-pending"/);
  assert.match(html, /data-action="catalog-select-ready"/);
  assert.match(html, /需要权限的集会跳过/);
});

test('known totals show missing episode numbers but unknown totals only describe observed gaps', () => {
  const base = {entries:[entries[0]],enumeratedCount:1,resolvedCount:1,downloadedCount:0};
  const known = renderEpisodeCatalog({id:'s1',catalog:{...base,expectedCount:4,missingCount:3,missingEpisodeKeys:['C:2','C:3','C:4']}});
  assert.match(known, /目录缺集 3 集/);
  assert.match(known, /第 2、3、4 集/);
  const unknown = renderEpisodeCatalog({id:'s1',catalog:{...base,expectedCount:null,observedGapOrdinals:[2,4],missingCount:7,missingEpisodeKeys:['C:6'],reasons:['missing-entries']}});
  assert.match(unknown, /总数未知/);
  assert.match(unknown, /已观察编号之间的间隙：第 2、4 集/);
  assert.match(unknown, /无法判断全剧是否齐全/);
  assert.doesNotMatch(unknown, /目录缺集|目录仍有缺集|第 6 集|7 集/);
});

test('catalog explains partial discovery without promising completeness or charging safety', () => {
  const html = renderEpisodeCatalog({id:'s1',catalog:{entries:[entries[0]],expectedCount:null,reasons:['catalog-limit','catalog-stalled','route-changed','read-only-blocked','out-of-range-entries']}});
  assert.match(html, /目录枚举已达到上限/);
  assert.match(html, /目录未继续展开/);
  assert.match(html, /扫描中页面发生跳转/);
  assert.match(html, /部分请求被只读扫描阻止/);
  assert.match(html, /授权窗口/);
  assert.match(html, /发现超出页面总集数的编号/);
  assert.doesNotMatch(html, /已完整|全部齐全|不会扣费/);
});

test('out-of-range observations and catalog ordinal limits stay visible as contradictions', () => {
  const html = renderEpisodeCatalog({id:'s1',catalog:{entries:[entries[0]],expectedCount:2,outOfRangeOrdinals:[4,8],reasons:['out-of-range-entries','ordinal-limit']}});
  assert.match(html, /页面总集数范围外的已观察编号：第 4、8 集/);
  assert.match(html, /异常编号已达到处理上限/);
  assert.doesNotMatch(html, /href="[^\"]*C:4|href="[^\"]*C:8/);
});

test('archive requires backend capability and a saved episode file', () => {
  const scan = {id:'s/a?x=1',catalog:{entries:[entries[0]],downloadedCount:0}};
  assert.doesNotMatch(renderEpisodeCatalog(scan), /\/archive/);
  assert.doesNotMatch(renderEpisodeCatalog(scan, [], {archiveAvailable:true}), /\/archive/);
  scan.catalog.downloadedCount = 1;
  assert.doesNotMatch(renderEpisodeCatalog(scan), /\/archive|ZIP 包含/);
  assert.doesNotMatch(renderEpisodeCatalog(scan, [], {archiveAvailable:false}), /\/archive|ZIP 包含/);
  const html = renderEpisodeCatalog(scan, [], {archiveAvailable:true});
  assert.match(html, /href="\/api\/scans\/s%2Fa%3Fx%3D1\/archive" download/);
  assert.match(html, /导出已保存剧集/);
  assert.match(html, /ZIP 包含已保存原件与缺失清单/);
});

test('quick selections exclude locked episodes while manual all selection remains available', () => {
  assert.equal(typeof catalogView.selectCatalogEpisodes, 'function');
  const catalog = {entries};
  assert.deepEqual(catalogView.selectCatalogEpisodes(catalog, 'pending'), ['C:4','C:5']);
  assert.deepEqual(catalogView.selectCatalogEpisodes(catalog, 'ready'), ['C:1']);
  assert.deepEqual(catalogView.selectCatalogEpisodes(catalog, 'all'), entries.map(entry => entry.episodeKey));
  assert.deepEqual(catalogView.selectCatalogEpisodes(catalog, 'all', entries.map(entry => entry.episodeKey)), []);
  assert.deepEqual(catalogView.selectCatalogEpisodes(catalog, 'all', ['stale','C:1','C:2','C:3','C:4']), entries.map(entry => entry.episodeKey));
});

test('legacy catalogs keep single-media, saved and permission statuses usable', () => {
  assert.equal(typeof catalogView.selectCatalogEpisodes, 'function');
  const legacy = {entries:[
    {episodeKey:'C:1',ordinal:1,assetIds:['a']},
    {episodeKey:'C:2',ordinal:2,downloaded:true,assetIds:['b']},
    {episodeKey:'C:3',ordinal:3,access:'needs-user',assetIds:['c']},
    {episodeKey:'C:4',ordinal:4,assetIds:[]},
    {episodeKey:'C:5',ordinal:5,assetIds:['d','e']},
  ]};
  assert.deepEqual(catalogView.selectCatalogEpisodes(legacy, 'ready'), ['C:1']);
  assert.deepEqual(catalogView.selectCatalogEpisodes(legacy, 'pending'), ['C:4','C:5']);
});

test('permission recheck selection targets needs-user entries without changing ordinary pending scans', () => {
  assert.deepEqual(catalogView.selectCatalogEpisodes({entries}, 'needs-user'), ['C:3']);
  assert.deepEqual(catalogView.selectCatalogEpisodes({entries}, 'pending'), ['C:4','C:5']);
  assert.deepEqual(catalogView.selectCatalogEpisodes({entries:[
    {episodeKey:'old:1',access:'needs-user'},
    {episodeKey:'old:2',access:'observed',assetIds:[]},
  ]}, 'needs-user'), ['old:1']);
});

test('scan feedback reports skipped permission episodes and the actual submitted count', () => {
  assert.equal(typeof catalogView.catalogScanMessage, 'function');
  const message = catalogView.catalogScanMessage({skippedEpisodeKeys:['C:3']}, 3);
  assert.match(message, /已提交 2 集/);
  assert.match(message, /跳过 1 集需要站点权限/);
  assert.match(message, /先在授权窗口解锁/);
  assert.equal(catalogView.catalogScanMessage({}, 2), '已提交 2 集的页面扫描。');
});

test('download feedback groups unavailable reasons and preserves legacy API support', () => {
  assert.equal(typeof catalogView.catalogDownloadMessage, 'function');
  const message = catalogView.catalogDownloadMessage({added:1,duplicates:2,unavailable:['C:3','C:4','C:5','C:6'],unavailableDetails:[
    {episodeKey:'C:3',reason:'needs-user'},
    {episodeKey:'C:4',reason:'unresolved'},
    {episodeKey:'C:5',reason:'ambiguous'},
    {episodeKey:'C:6',reason:'not-found'},
  ]});
  assert.match(message, /已加入 1 集/);
  assert.match(message, /4 集暂不可下载/);
  assert.match(message, /需要站点权限 1 集/);
  assert.match(message, /尚未解析正片 1 集/);
  assert.match(message, /主媒体待确认 1 集/);
  assert.match(message, /目录中未找到 1 集/);
  assert.match(message, /2 集已下载或已在队列/);
  assert.match(catalogView.catalogDownloadMessage({added:0,unavailable:['C:1']}), /1 集尚未解析或需要权限/);
  assert.doesNotMatch(catalogView.catalogDownloadMessage({added:0,unavailable:['C:1'],unavailableDetails:[{episodeKey:'C:1',reason:'<private-url>'}]}), /private-url/);
});

test('episode search combines exact ordinal or title with status and only selects visible matches', () => {
  const episodeCatalog = {entries: [
    {episodeKey:'E:1',ordinal:1,title:'风景示例',mediaStatus:'ready'},
    {episodeKey:'E:10',ordinal:10,title:'第 10 集',mediaStatus:'downloaded'},
    {episodeKey:'E:11',ordinal:11,title:'风景示例续集',mediaStatus:'unresolved'},
  ]};
  const matched = filter => catalogView.filterEpisodeEntries(episodeCatalog, filter).map(entry=>entry.episodeKey);
  assert.deepEqual(matched({query:'第 01 集',status:'all'}), ['E:1']);
  assert.deepEqual(matched({query:'风景',status:'unresolved'}), ['E:11']);
  const scan = {id:'s',catalog:episodeCatalog};
  const options = {seriesId:'series',filter:{status:'downloaded'}};
  const view = renderEpisodeCatalog(scan, ['E:1','E:10'], options);
  assert.match(view, /共 1 集 · 已选 1 集/);
  assert.match(view, /value="E:10" checked/);
  assert.doesNotMatch(view, /value="E:1"|第 10 集 · 第 10 集/);
  const noneVisible = renderEpisodeCatalog(scan, ['E:1'], options);
  assert.match(noneVisible, /class="episode-actions" hidden/);
  assert.match(noneVisible,/data-action="series-dismiss-episodes"[^>]* disabled/);
  assert.match(renderEpisodeCatalog(scan, [], {...options,filter:{query:'水彩'}}), /没有符合筛选条件的单集/);
  assert.match(renderEpisodeCatalog({id:'s',catalog:{entries:[],dismissedEpisodeCount:3}}, [], options), /单集已全部移除/);
});

test('series selection enables only operations usable by at least one selected episode', () => {
  const scan = {id:'s',catalog:{entries}};
  const actions = selected => {
    const html = renderEpisodeCatalog(scan, selected, {seriesId:'series'});
    const enabled = action => {
      const button = html.match(new RegExp('<button[^>]+data-action="series-' + action + '"[^>]*>'));
      assert.ok(button);
      return !button[0].includes(' disabled');
    };
    return {scan:enabled('scan-check'),download:enabled('download-selected')};
  };
  assert.deepEqual(actions(['C:4']), {scan:true,download:false});
  assert.deepEqual(actions(['C:3']), {scan:true,download:false});
  assert.deepEqual(actions(['C:2']), {scan:true,download:false});
  assert.deepEqual(actions(['C:3','C:4','C:1']), {scan:true,download:true});
});

test('discovery cleanup is scoped to one scan and counts refer to the remaining entries', () => {
  const catalog = { title:'相同剧名', entries, expectedCount:5, downloadedCount:1,
    reasons:['blocked-entries','unresolved-media','ambiguous-media','truncated'] };
  const scan = {id:'scan-one',catalog,discoveryDismissedEpisodeKeys:['C:1','C:3','unrelated']};
  const result = catalogView.discoveryCatalog(scan);
  assert.deepEqual(result.entries.map(e => e.episodeKey), ['C:2','C:4','C:5']);
  assert.equal(result.enumeratedCount,3);
  assert.equal(result.resolvedCount,1);
  assert.equal(result.downloadedCount,1);
  assert.equal(result.blockedCount,0);
  assert.equal(result.ambiguousCount,1);
  assert.deepEqual(result.reasons,['unresolved-media','ambiguous-media','truncated']);
  const savedOnly = catalogView.discoveryCatalog({...scan, discoveryDismissedEpisodeKeys:['C:1','C:3','C:4','C:5']});
  assert.deepEqual(savedOnly.reasons,['truncated']);
  assert.equal(catalogView.discoveryCatalog({id:'scan-two',catalog}).entries.length,5);
  assert.equal(catalog.entries.length,5);
  assert.equal(catalogView.discoveryCatalog({...scan,discoveryCatalogDismissed:true}), null);
  assert.equal(catalogView.discoveryCatalog({...scan,discoveryDismissedEpisodeKeys:entries.map(e=>e.episodeKey)}),null);
});

test('discovery cards expose direct removal while collapsed, and filter/selection affect only visible entries', () => {
  const scan={id:'scan-a',catalog:{title:'剧名 <A>',entries,downloadedCount:1},discoveryDismissedEpisodeKeys:['C:1']};
  const options={discovery:true,filter:{status:'downloaded'},archiveAvailable:true,siteName:'测试站'};
  const view=renderEpisodeCatalog(scan,['C:1','C:2','C:3'],options);
  assert.match(view,/剧名 &lt;A&gt;/);
  assert.match(view,/测试站/);
  assert.match(view,/data-action="catalog-dismiss-all"[^>]*>[\s\S]*?<details/);
  assert.match(view,/<details [^>]*data-discovery-catalog="scan-a">/);
  assert.match(view,/data-catalog-filter="status"/);
  assert.match(view,/共 1 集 · 已选 1 集/);
  assert.match(view,/data-action="catalog-dismiss-episodes"[^>]*>移除所选集/);
  assert.match(view,/value="C:2" checked/);
  assert.doesNotMatch(view,/value="C:[1345]"/);
  const noVisible = renderEpisodeCatalog(scan,['C:1'],{...options,open:true});
  assert.match(noVisible,/data-discovery-catalog="scan-a" open/);
  assert.doesNotMatch(noVisible,/data-action="catalog-dismiss-episodes"[^>]*disabled/);
  assert.match(noVisible,/请先勾选要移除的单集/);
  assert.match(noVisible,/href="\/api\/scans\/scan-a\/archive"/);
  assert.equal(renderEpisodeCatalog({...scan,discoveryCatalogDismissed:true},[],options),'');
});

test('a scanned protected episode keeps its result, reason and status filter without returning to pending', () => {
  const protectedEp = {episodeKey:'show:1', ordinal:1, mediaStatus:'unsupported-media', scanMessage:'站点使用专用播放协议'};
  const failed = {episodeKey:'show:2', ordinal:2, mediaStatus:'failed', scanMessage:'页面超时，可重试'};
  const catalog = {entries:[protectedEp, failed], publishedCount:2, ongoing:true};
  assert.deepEqual(catalogView.selectCatalogEpisodes(catalog, 'pending'), []);
  assert.deepEqual(catalogView.filterEpisodeEntries(catalog, {status:'unsupported-media'}), [protectedEp]);
  assert.deepEqual(catalogView.filterEpisodeEntries(catalog, {status:'failed'}), [failed]);
  const html = renderEpisodeCatalog({id:'scan',catalog}, ['show:1'], {seriesId:'series'});
  assert.match(html, /已发布 2 集/);
  assert.match(html, /站点使用专用播放协议/);
  assert.match(html, /data-media-status="unsupported-media"/);
  assert.match(html, /data-action="series-download-selected"[^>]*disabled/);
  assert.doesNotMatch(html, /待扫描页面/);
});
