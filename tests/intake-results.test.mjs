import test from 'node:test';
import assert from 'node:assert/strict';
import {freshResultView, intakeResultIds, renderIntakeResult, resultRoute, suggestScope} from '../dist/intake-results.js';

const episode = (key, media) => ({key, title:key, url:'https://fixture/episode/'+key, assetIds:media});
const result = {scanId:'scan_sample', scope:'site', status:'completed', url:'https://fixture/', groups:[
  {key:'a', title:'海岸', url:'https://fixture/show/a', categories:['自然'], assetIds:['poster'], episodes:[episode('a1',['main','poster'])]},
  {key:'b', title:'山地', url:'https://fixture/show/b', categories:['地理'], assetIds:[], episodes:[episode('b1',['poster'])]},
]};
const state = {scans:[], jobs:[], assets:[{id:'main',kind:'video',title:'首集',status:'downloaded'},{id:'poster',kind:'image',title:'海报',status:'downloaded'}]};

test('input suggestions distinguish series and a single episode without following arbitrary hash text',()=>{
  assert.equal(suggestScope('https://example.test/'),'site');
  assert.equal(suggestScope('https://lustflix.tv/#/s/C13'),'series');
  assert.equal(suggestScope('https://lustflix.tv/#/s/C13/1'),'single');
  assert.equal(suggestScope('https://www.tiktok.com/@user/video/123'),'single');
  assert.equal(resultRoute('#results/scan_123'),'scan_123');
  assert.equal(resultRoute('#results/../admin'),'');
});

test('YouTube watch, Shorts and short links select one video', () => {
  for (const url of ['https://www.youtube.com/watch?v=jNQXAC9IVRw',
    'https://youtu.be/jNQXAC9IVRw', 'https://www.youtube.com/shorts/jNQXAC9IVRw']) {
    assert.equal(suggestScope(url), 'single', url);
  }
});

test('Lata links suggest the site index, whole show, or exact episode', () => {
  assert.equal(suggestScope('https://lata.casa/series'), 'site');
  assert.equal(suggestScope('https://www.lata.casa:443/series/'), 'site');
  assert.equal(suggestScope('https://lata.casa/originals/ns4'), 'series');
  assert.equal(suggestScope('https://lata.casa/watch/ns4-ep-2'), 'single');
  assert.equal(suggestScope('https://other.example/originals/ns4'), '');
});

test('a Chunlou feed targets a single episode only with an unambiguous episode ID', () => {
  const episode = 'ab7c97c5-5ebe-4e21-ae85-3a44cbf2a8d8';
  const feed = 'https://chunlou.tv/feed';
  assert.equal(suggestScope(feed + '?ep=' + episode), 'single');
  assert.equal(suggestScope(feed + '/?share=1&ep=' + episode.toUpperCase()), 'single');
  for (const other of [
    feed, feed + '?ep=1', feed + '?ep=' + episode + '&ep=' + episode,
    'https://chunlou.tv/feed/other?ep=' + episode,
    'https://chunlou.tv.evil.example/feed?ep=' + episode,
    'https://other.example/feed?ep=' + episode,
    'http://chunlou.tv/feed?ep=' + episode,
    'https://chunlou.tv:8443/feed?ep=' + episode,
    'https://guest@chunlou.tv/feed?ep=' + episode,
  ]) assert.equal(suggestScope(other), '', other);
});

test('shared originals stay visible in both groups but are only counted once per selection',()=>{
  assert.deepEqual(intakeResultIds(result),['poster','main']);
  assert.deepEqual(intakeResultIds(result,{group:'a',episode:'a1'}),['main','poster']);
  assert.deepEqual(intakeResultIds(result,{group:'b'}),['poster']);
  assert.deepEqual(intakeResultIds(result,{group:'b',episode:'a1'}),[]);
  const view=freshResultView(); view.category='地理'; view.selected.add('poster');
  const html=renderIntakeResult(result,state,view);
  assert.match(html,/已选 1 \/ 2 项/);
  assert.match(html,/data-group="b" data-episode="b1"/);
  assert.doesNotMatch(html,/data-group="a"/);
});

test('short media distinguishes marked clean streams and a processed independently exportable version',()=>{
  const single = {scope:'single', status:'completed', groups:[{key:'post',assetIds:['main']}]};
  const asset = {...state.assets[0],shortMedia:{platform:'instagram',watermark:'playback'},
    versions:[{id:'fixed',kind:'processed',status:'completed',fileUrl:'/api/file',options:{mode:'remove'}}]};
  const html = renderIntakeResult(single,{...state,assets:[asset]},freshResultView());
  assert.match(html,/水印待核实/);
  assert.match(html,/选区去水印/);
  assert.match(html,/\/versions\/fixed\/file\?download=true/);
  assert.doesNotMatch(html,/源站无水印版本/);
});

test('partially resolved shows and poster-only or gated episodes keep their own reread links',()=>{
  const partial = {scope:'site',status:'completed',groups:[{key:'show',title:'海岸',url:'https://fixture/show',
    status:'partial',assetIds:[],episodes:[
      episode('poster-only',['poster']),
      {...episode('needs-login',[]),access:'needs-user'},
    ]}]};
  const html=renderIntakeResult(partial,state,freshResultView());
  assert.match(html,/data-action="intake-read" data-value="https:\/\/fixture\/show" data-scope="series"/);
  for (const key of ['poster-only','needs-login']) {
    assert.ok(html.includes('data-action="intake-read" data-value="https://fixture/episode/'+key+'" data-scope="single"'));
  }
});

test('continuation is offered only for an explicit resumable result that has finished',()=>{
  const html = (scope,status,canContinue) => renderIntakeResult({...result,scope,status,canContinue},state,freshResultView());
  for (const scope of ['site','series']) {
    assert.match(html(scope,'completed',true),/data-action="intake-continue"/);
    for (const status of ['queued','running','needs-user']) {
      assert.doesNotMatch(html(scope,status,true),/data-action="intake-continue"/);
    }
    assert.doesNotMatch(html(scope,'completed',false),/data-action="intake-continue"/);
  }
  assert.doesNotMatch(html('single','completed',true),/data-action="intake-continue"/);
  const middle = renderIntakeResult({...result,previousScanId:'scan_old',nextScanId:'scan_next',canContinue:true},state,freshResultView());
  assert.match(middle, /href="#results\/scan_old">查看上一轮/);
  assert.match(middle, /href="#results\/scan_next">查看下一轮/);
  assert.doesNotMatch(middle, /data-action="intake-continue"/);
});

test('unsupported media is reported separately from DRM', () => {
  const html = renderIntakeResult({...result, scope:'single', status:'unsupported-media'}, state, freshResultView());
  assert.match(html, /暂不支持此播放格式/);
  assert.doesNotMatch(html, /DRM 保护/);
});

test('a Chunlou series detail or unambiguous feed drama selects whole show', () => {
  assert.equal(suggestScope('https://chunlou.tv/drama/d-ac2258c7'), 'series');
  assert.equal(suggestScope('https://chunlou.tv/feed?d=d-ac2258c7'), 'series');
  assert.equal(suggestScope('https://chunlou.tv/feed?d=d-ac2258c7&ep=invalid'), '');
});

test('intake episodes without a video show the actual playback scan result', () => {
  const ep = {...episode('e1',[]),mediaStatus:'unsupported-media',scanMessage:'该集使用受保护的播放格式'};
  const group = {key:'a',title:'演示',episodes:[ep],assetIds:[]};
  const sample = {...result,scope:'series',groups:[group]};
  const view = {...freshResultView(),episodeKey:'e1'};
  const html = renderIntakeResult(sample, state, view);
  assert.match(html,/暂不支持此播放格式/);
});

test('public short-media results offer original save without processing controls',()=>{
  const single = {scope:'single', status:'completed', groups:[{key:'post',assetIds:['main']}]};
  const asset = {...state.assets[0],shortMedia:{platform:'instagram',watermark:'playback'},
    versions:[{id:'fixed',kind:'processed',status:'completed',fileUrl:'/api/file',options:{mode:'remove'}}]};
  const html = renderIntakeResult(single,{...state,publicMode:true,assets:[asset]},freshResultView());
  assert.match(html,/保存到本机/);
  assert.doesNotMatch(html,/选区去水印|导出去水印版本|data-action="intake-clean"/);
});
