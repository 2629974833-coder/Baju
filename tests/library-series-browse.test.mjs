import test from 'node:test';
import {groupLocalAssets} from '../dist/asset-series.js';
import {librarySearchIds, libraryVisibleGroups, renderLibraryGroups} from '../dist/library-groups-view.js';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {escapeHtml as esc} from '../dist/ui.js';
import {hasOriginal, matchesFileCollection} from '../dist/version-view.js';
import {matchesContent} from '../dist/content-view.js';

const app = readFileSync(new URL('../dist/app.js', import.meta.url), 'utf8');
function fixture() {
  const file = (id, title, siteId='A', kind='video') => ({id,title,siteId,kind,tags:[],category:'剧作',status:'downloaded'});
  const assets = [file('a10','第十章'), file('other','散片'), file('b1','首章','B'), file('cover','海报','A','image'), file('a2','第二章')];
  const makeSeries = (id,siteId,entries) => ({id,siteId,customTitle:'岸与海',title:'岸与海',catalog:{seriesKey:id,entries:entries.map(([ordinal,assetId])=>({ordinal,downloadAssetId:assetId,assetIds:[assetId]}))}});
  const state = {assets,sites:[{id:'A',name:'甲站'},{id:'B',name:'乙站'}], series:[makeSeries('A1','A',[[2,'a2'],[10,'a10']]),makeSeries('B2','B',[[1,'b1']])]};
  const ui = {page:'library',selected:new Set(),q:'',kind:'all',category:'all',site:'all',content:'all',status:'all',collection:'all',sort:'recent',view:'grid',librarySections:new Map()};
  const scope = vm.createContext({groupLocalAssets,librarySearchIds,libraryVisibleGroups,renderLibraryGroups,state,ui,esc,pages:{library:'资料库'},icon:()=>'',pageHeading:()=>'',isReal:()=>true,hasOriginal,matchesFileCollection,matchesContent,contentKinds:[],findSite:id=>state.sites.find(s=>s.id===id),renderAsset:a=>`<i data-fixture="${a.id}"></i>`,renderFacets:()=>'',renderCatalogEmpty:()=>'<p>没有匹配</p>',renderAssetInspector:()=>''});
  vm.runInContext(app.slice(app.indexOf('function catalogScope('),app.indexOf('function renderAsset(')),scope);
  return {state,ui,scope};
}

test('library groups same-title shows separately, keeps other videos and photos reachable',()=>{
  const {scope}=fixture();
  const html=scope.renderCatalog();
  assert.match(html,/data-library-group="A1"/);
  assert.match(html,/data-library-group="B2"/);
  assert.match(html,/甲站/);
  assert.match(html,/乙站/);
  assert.match(html,/data-library-group="unassigned-video"/);
  assert.match(html,/data-library-group="other-media"/);
});

test('library title search finds complete shows and collection filters still limit selection and exports',()=>{
  const {scope,ui,state}=fixture();
  ui.q='岸与海';
  assert.equal(scope.catalogAssets().length,3);
  assert.deepEqual(Array.from(scope.catalogAssets(), a=>a.id).filter(id=>id.startsWith('a')),['a2','a10']);
  ui.site='A';
  scope.toggleCatalogPageSelection();
  assert.deepEqual([...ui.selected],['a2','a10']);
  state.assets.find(a=>a.id==='a10').favorite=true;
  ui.collection='favorites';
  assert.deepEqual(Array.from(scope.catalogAssets(), a=>a.id),['a10']);
});
