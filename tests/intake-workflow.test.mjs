import test from 'node:test';
import assert from 'node:assert/strict';
import {intakeDestination, homePendingAssets, renderHomePending} from '../dist/intake-workflow.js';

test('whole-site and whole-show submissions open series; single posts retain their results', () => {
  assert.deepEqual(intakeDestination('site', 'scan_a'), {page:'series', scanId:'scan_a'});
  assert.deepEqual(intakeDestination('series', 'scan_b'), {page:'series', scanId:'scan_b'});
  assert.deepEqual(intakeDestination('single', 'scan_c'), {page:'results', scanId:'scan_c'});
});

const state = {mode:'real', jobs:[], sites:[{id:'a',name:'示例站'}], series:[{
  catalog:{entries:[{assetIds:['episode'],downloadAssetId:'episode'}]}, artwork:{assetIds:['poster']},
}], assets:[
  {id:'episode',title:'第1集',siteId:'a',kind:'video',status:'discovered'},
  {id:'poster',title:'海报',siteId:'a',kind:'image',status:'discovered'},
  {id:'saved',kind:'video',status:'downloaded'},
  {id:'hidden',dismissedFromDiscovery:true},
  ...Array.from({length:15},(_,i)=>({id:'clip'+i,title:'片段'+i,kind:'video',siteId:'a',status:'discovered'})),
]};
test('overview keeps ungrouped media manageable without duplicating series assets', () => {
  const first=homePendingAssets(state,{page:1});
  assert.equal(first.total,15);
  assert.equal(first.items.length,12);
  const last=homePendingAssets(state,{page:99});
  assert.equal(last.page,2);
  assert.equal(last.items.length,3);
  const found=homePendingAssets(state,{query:'片段14'});
  assert.deepEqual(found.items.map(a=>a.id),['clip14']);
});
test('overview legacy media has explicit controls and no lost discovery navigation', () => {
  const html=renderHomePending(state,{open:true,page:1,selected:['clip0']});
  assert.match(html,/其他待下载素材/);
  assert.match(html,/data-action="home-pending-download"/);
  assert.match(html,/data-action="home-pending-remove"/);
  assert.match(html,/data-action="home-pending-page"/);
  assert.doesNotMatch(html,/data-page="discover"/);
  assert.doesNotMatch(html,/data-id="poster"/);
});
