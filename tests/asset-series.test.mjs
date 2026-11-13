import test from 'node:test';
import assert from 'node:assert/strict';
import {groupLocalAssets} from '../dist/asset-series.js';

const asset = (id, siteId='s', kind='video') => ({id, siteId, kind, title:'相同的素材名'});
const entry = (ordinal, downloadAssetId, assetIds=[]) => ({ordinal, title:'第'+ordinal+'集', downloadAssetId, assetIds});
const show = (id,siteId,entries) => ({id,siteId,title:'原名',customTitle:'同名',catalog:{seriesKey:id,entries}});
const state = {sites:[{id:'s',name:'来源S'},{id:'t',name:'来源T'}],series:[
  show('a','s',[entry(10,'v10',['trailer','v10']),entry(2,'v2',['v1'])]),
  show('b','t',[entry(1,'v1',['v2'])]),
  show('c','s',[entry(1,'confirmed',['trailer','v2'])]),
]};
test('两页按真实目录、站点和数字集序归类，保留同名剧与未归集文件', () => {
  const groups=groupLocalAssets(state,[asset('v10'),asset('v1','t'),asset('v2'),asset('unknown'),asset('cover','s','image'),asset('v10')]);
  assert.deepEqual(groups.map(g=>[g.id,g.title,g.items.map(i=>i.asset.id)]),[
    ['a','同名',['v2','v10']],['b','同名',['v1']],['unassigned-video','未归集视频',['unknown']],['other-media','其他素材',['cover']]]);
  assert.deepEqual(groups[0].items.map(i=>[i.ordinal,i.episodeTitle]),[[2,'第2集'],[10,'第10集']]);
  assert.equal(groups[0].siteName,'来源S'); assert.equal(groups[1].seriesKey,'b');
});
test('共用宣传片无法唯一归集时单列，明确的主媒体归属优先',()=>{
  const g=groupLocalAssets(state,[asset('trailer'),asset('v2')]);
  assert.deepEqual(g.map(x=>[x.id,x.items.map(i=>i.asset.id)]),[['a',['v2']],['unassigned-video',['trailer']]]);
});
