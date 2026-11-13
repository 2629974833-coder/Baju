import test from 'node:test';
import assert from 'node:assert/strict';
import * as studio from '../dist/processing-view.js';

const media = (id, siteId = 'chunlou', kind = 'video') =>
  ({ id, title: id, kind, siteId, status: 'downloaded', versions: [], pageUrl: 'https://example.com/' + id });
const mediaItems = [media('ten'), media('foreign','elsewhere'), media('two'), media('photo','chunlou','image'), media('loose')];
const series = (id, title, siteId, episodes) => ({ id, title, siteId, catalog: {
  seriesKey:id, entries:episodes.map(([ordinal,assetId]) => ({ordinal,title:'第 '+ordinal+' 集',pageUrl:'https://example.com/'+assetId,assetIds:[assetId],downloadAssetId:assetId})),
}});
const state = { mode:'real', assets:mediaItems, sites:[{id:'chunlou',name:'春楼'},{id:'elsewhere',name:'另一站'}], series:[
  series('s1','我的剧','chunlou',[[10,'ten'],[2,'two']]),series('s2','我的剧','elsewhere',[[1,'foreign']])
]};

test('choosing a drama scopes the episode order, preview and submissions to that drama', () => {
  const draft={groupId:'s1',previewId:'foreign',selectedIds:['foreign','ten']};
  const batch=studio.studioBatch(state,draft);
  assert.deepEqual(batch.items.map(({asset})=>asset.id),['two','ten']);
  assert.deepEqual(batch.selectedIds,['ten']);
  assert.equal(batch.preview.id,'ten');
  const html=studio.renderRealProcess(state,draft);
  assert.match(html,/data-processing-series/);
  assert.match(html,/我的剧[^<]*春楼/);
  assert.match(html,/我的剧[^<]*另一站/);
  assert.match(html,/data-action="process-preview-asset" data-id="two"/);
  assert.doesNotMatch(html,/data-action="toggle-process" data-id="(?:foreign|photo|loose)"/);
  const other=studio.studioBatch(state,{groupId:'other-media'});
  assert.deepEqual(other.items.map(i=>i.asset.id),['photo']);
  assert.deepEqual(studio.studioBatch(state,{groupId:'unassigned-video'}).items.map(i=>i.asset.id),['loose']);
});

test('a mixed send-to-studio batch retains exactly the incoming videos and images until a show is chosen', () => {
  const draft={previewId:'foreign',incomingIds:['foreign','photo'],selectedIds:['foreign','photo']};
  const batch=studio.studioBatch(state,draft);
  assert.deepEqual(batch.selectedIds.sort(),['foreign','photo']);
  assert.deepEqual(batch.items.map(i=>i.asset.id).sort(),['foreign','photo']);
  const scoped=studio.studioBatch(state,{...draft,groupId:'s1'});
  assert.deepEqual(scoped.selectedIds,[]);
  assert.equal(scoped.preview.id,'two');
});

test('one click submits a region-free glyph request for the selected Chunlou videos or the current preview', () => {
  const draft={groupId:'s1',previewId:'two',selectedIds:[]};
  const build=d=>studio.glyphRequest(state,studio.studioBatch(state,d));
  assert.deepEqual(build(draft),{path:'/api/processes',body:{ids:['two'],options:{mode:'remove',method:'chunlou-glyph',revision:2}}});
  assert.deepEqual(build({...draft,selectedIds:['two','ten']}).body.ids,['two','ten']);
  assert.throws(()=>build({groupId:'other-media'}),/春楼视频/);
  assert.throws(()=>build({groupId:'s2'}),/春楼视频/);
  assert.match(studio.renderRealProcess(state,draft),/data-action="process-chunlou-glyph"/);
  assert.match(studio.renderRealProcess(state,draft),/前后帧.*另存/);
  assert.doesNotMatch(studio.renderRealProcess(state,{groupId:'s2'}),/data-action="process-chunlou-glyph"/);
});

test('completed glyph jobs report actually repaired frames', () => {
  const completed={id:'v',kind:'processed',status:'completed',fileUrl:'/foo',glyphFrames:15,totalFrames:20};
  const html=studio.renderProcessingJobs({...state,assets:[{...mediaItems[0],versions:[completed]}],
    processingJobs:[{id:'j',assetId:'ten',status:'completed',versionId:'v',progress:100,options:{mode:'remove',method:'chunlou-glyph'}}]});
  assert.match(html,/春楼飘字修复/);
  assert.match(html,/15\s*\/\s*20\s*帧/);
});

test('two saved resolutions of one episode can be identified in the preview picker and batch list', () => {
  const first={...media('lo'),height:360}, second={...media('hi'),height:720};
  const show=series('s1','有两种画质','chunlou',[[1,'hi']]);
  show.catalog.entries[0].assetIds=['lo','hi'];
  const html=studio.renderRealProcess({...state,assets:[first,second],series:[show]});
  assert.match(html,/<option value="lo"[^>]*>第 1 集 · 360p<\/option>/);
  assert.match(html,/<option value="hi"[^>]*>第 1 集 · 720p<\/option>/);
  assert.match(html,/aria-label="预览 第 1 集 · 720p"/);
});

test('temporal job opens exactly its published MP4 even with older or newer finished versions', () => {
  const versions = ['older','target','newer'].map(id=>({id,kind:'processed',status:'completed',fileUrl:'/'+id}));
  const asset = {...mediaItems[0],versions};
  const job = {id:'j2',assetId:asset.id,status:'completed',versionId:'target',options:{mode:'remove',method:'chunlou-glyph',revision:2}};
  const render = job => studio.renderProcessingJobs({assets:[asset], processingJobs:[job]});
  const html = render(job);
  assert.match(html,/春楼飘字精修/);
  assert.match(html,/data-action="detail"[^>]*data-version="target"[^>]*>查看成品/);
  assert.match(html,/versions\/target\/file\?download=true/);
  for (const versionId of [undefined,'original','missing']) {
    assert.doesNotMatch(render({...job,versionId}),/查看成品|保存版本/);
  }
});
