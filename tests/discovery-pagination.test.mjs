import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { escapeHtml as esc } from '../dist/ui.js';
import { matchesContent, contentKinds } from '../dist/content-view.js';

const app = readFileSync(new URL('../dist/app.js', import.meta.url), 'utf8');
function fixture() {
  const assets = Array.from({length: 51}, (_, i) => ({
    id: `a${i}`, title: `条目 ${i}`, tags: [], siteId: 'course', category: '教学',
    kind: i % 3 ? 'video' : 'image', classification: {kind: 'episode'}, status: 'discovered',
  }));
  const ui = {page:'discover', q:'', kind:'all', category:'all', site:'all', content:'all', status:'all', selected:new Set(), view:'grid', sort:'recent'};
  const state = {assets, sites:[{id:'course',name:'课程站'}]};
  const scope = vm.createContext({ui,state,esc,icon:()=>'',pageHeading:()=>'',isReal:()=>true,renderRealScans:()=>'',hasOriginal:()=>false,
    findSite:()=>state.sites[0],contentKinds,matchesContent,renderAsset:a=>`<i data-fixture="${a.id}"></i>`,renderCatalogEmpty:()=>'<p>没有匹配</p>'});
  vm.runInContext(app.slice(app.indexOf('function catalogScope('), app.indexOf('function renderAsset(')), scope);
  return {ui,state,scope,ids:()=>Array.from(scope.renderCatalog().matchAll(/data-fixture="([^"]+)"/g), m=>m[1])};
}

test('discovery pages both layouts and clamps when the last items disappear', () => {
  const {ui, state, ids} = fixture();
  assert.equal(ids().length,24);
  ui.discoveryPage=2; ui.view='list';
  assert.deepEqual(ids(),state.assets.slice(24,48).map(a=>a.id));
  ui.discoveryPage=3;
  assert.equal(ids().length,3);
  state.assets.length=48;
  assert.equal(ids()[0],'a24');
  assert.equal(ui.discoveryPage,2);
  ui.discoveryPageSize=12;
  assert.equal(ids().length,12);
});

test('page select-all toggles only the displayed IDs and reports off-page selections', () => {
  const {ui,scope,ids} = fixture();
  scope.toggleCatalogPageSelection();
  assert.deepEqual([...ui.selected],ids());
  ui.discoveryPage=2;
  scope.toggleCatalogPageSelection();
  assert.equal(ui.selected.size,48);
  assert.match(scope.renderCatalog(), /其他页 24 项/);
  scope.toggleCatalogPageSelection();
  assert.equal(ui.selected.size,24);
  scope.changeCatalogFilter('content','episode');
  assert.equal(ui.discoveryPage,1);
  assert.equal(ui.selected.size,0);
  assert.equal(ids().length,24);
  assert.ok(!ids().includes('a0'));
});

test('series category uses only known episode asset IDs and remembers custom titles', () => {
  const {ui, state, scope, ids} = fixture();
  state.assets[5].title='山海手记（同名但未关联）';
  state.series = [
    {id:'garden', siteId:'course', title:'自定义剧名', catalog:{entries:[{assetIds:['a2','a2','gone']}, {downloadAssetId:'a4'}, {}]}},
    {id:'unlinked', title:'没有素材的剧', catalog:{entries:[{assetIds:['gone']}]}},
    {id:'legacy', title:'旧记录'},
  ];
  const html = scope.renderCatalog();
  assert.match(html,/label="按剧集"/);
  assert.match(html,/自定义剧名/);
  assert.doesNotMatch(html,/没有素材的剧|旧记录/);
  scope.changeCatalogFilter('discoveryGroup','series:garden');
  assert.deepEqual(ids(),['a2','a4']);
  scope.changeCatalogFilter('discoveryGroup','unassigned');
  assert.ok(!ids().includes('a2') && ids().includes('a5'));
  scope.changeCatalogFilter('discoveryGroup','series:garden');
  ui.selected.add('a2');
  state.series = [];
  assert.equal(ids().length,24);
  assert.equal(ui.selected.size,0);
  assert.equal(ui.discoveryGroup,'all');
  scope.changeCatalogFilter('discoveryGroup','category:教学');
  assert.equal(ids().length,24);
});

test('mobile disclosure and active filter summary survive discovery rerenders', () => {
  const {ui,scope} = fixture();
  assert.match(scope.renderCatalog(),/aria-controls="discovery-detail-filters" aria-expanded="false"/);
  ui.discoveryFiltersOpen = true;
  scope.changeCatalogFilter('site','course');
  scope.changeCatalogFilter('discoveryGroup','category:教学');
  let html=scope.renderCatalog();
  assert.match(html,/aria-controls="discovery-detail-filters" aria-expanded="true"/);
  assert.match(html,/class="filter-count">2</);
  ui.discoveryFiltersOpen = false;
  html=scope.renderCatalog();
  const summary=html.match(/<div class="discovery-active-filters"[\s\S]+?<\/div>/)?.[0];
  assert.match(summary,/课程站/);
  assert.match(summary,/教学/);
  assert.match(summary,/data-action="clear-filters"/);
});

test('inbox counts track original files and reset selected IDs when the scope changes', () => {
  const {ui, scope, state, ids} = fixture();
  scope.hasOriginal = item => item?.status === 'downloaded';
  const [a, b, c, hidden] = state.assets;
  a.status = 'downloaded';
  b.locked = true;
  c.status = 'downloaded'; c.locked = true;
  hidden.dismissedFromDiscovery = true;
  scope.changeCatalogFilter('inbox', 'pending');
  const counts = scope.inboxCounts();
  assert.deepEqual([counts.all, counts.pending, counts.locked, counts.local],[50,48,1,2]);
  assert.ok(!ids().includes(a.id) && ids().includes(b.id));
  scope.toggleCatalogPageSelection();
  ui.discoveryPage=2;
  scope.changeCatalogFilter('inbox', 'locked');
  assert.deepEqual(ids(),[b.id]);
  assert.equal(ui.discoveryPage,1);
  assert.equal(ui.selected.size,0);
  scope.changeCatalogFilter('inbox', 'local');
  assert.deepEqual(ids(),[a.id,c.id]);
  b.status='downloaded';
  assert.equal(scope.inboxCounts().pending,47);
  assert.deepEqual(ids(),[a.id,b.id,c.id]);
});

test('the inspector follows the next filtered item across the page boundary', () => {
  const {ui,scope,state} = fixture();
  Object.assign(scope, {renderPage(){}, $:()=>null, CSS:{escape: String}});
  vm.runInContext(app.slice(app.indexOf('function focusCatalogItem('),app.indexOf('function renderActivitySide(')),scope);
  assert.equal(scope.focusCatalogItem(state.assets,23),true);
  assert.equal(ui.focusId,'a23');
  scope.stepInspector(1);
  assert.equal(ui.focusId,'a24');
  assert.equal(ui.discoveryPage,2);
  scope.stepInspector(-1);
  assert.equal(ui.focusId,'a23');
  assert.equal(ui.discoveryPage,1);
  scope.focusCatalogItem(state.assets,50);scope.stepInspector(1);
  assert.equal(ui.focusId,'a50');
});
