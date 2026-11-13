import test from 'node:test';
import assert from 'node:assert/strict';
import * as processing from '../dist/processing-view.js';
import * as versions from '../dist/version-view.js';

const asset = {id:'media/a',title:'课程 <一>',status:'downloaded',kind:'video',width:320,height:180,sizeMB:1,versions:[
  {id:'original',label:'原件'},
  {id:'v/one',kind:'processed',status:'completed',fileUrl:'/api/assets/media%2Fa/versions/v%2Fone/file',label:'加水印',width:320,height:180,sizeBytes:2048,createdAt:'2026-11-04T00:00:00Z'},
  {id:'broken',kind:'processed',status:'failed'},
]};
const state = {mode:'real',assets:[asset,{id:'remote',title:'未下载',status:'discovered',kind:'image'}],sites:[],processingJobs:[]};

test('processing request freezes known local selections and percentage options', () => {
  const options={mode:'both',text:'拾帧',position:'top-left',opacity:65,fontSize:4,region:{x:10,y:20,width:25,height:15}};
  const result=processing.processingRequest(state,['media/a','media/a'],options);
  assert.deepEqual(result,{path:'/api/processes',body:{ids:['media/a'],options}});
  options.region.x=88;
  assert.equal(result.body.options.region.x,10);
  assert.throws(()=>processing.processingRequest(state,[],{}),/选择/);
  assert.throws(()=>processing.processingRequest(state,['remote'],{}),/已下载/);
  assert.throws(()=>processing.processingRequest(state,['missing'],{}),/已下载/);
});

test('processing rejects invalid modes, numbers, empty adding text and out of frame regions', () => {
  for(const options of [{mode:'unknown'},{mode:'add',text:' '},{opacity:-1},{fontSize:21},{mode:'remove',region:{x:90,y:5,width:20,height:10}},{mode:'remove',region:{x:1,y:1,width:0,height:10}},{opacity:Infinity}]) {
    assert.throws(()=>processing.processingRequest(state,['media/a'],options));
  }
});

test('real studio shows only local files, escaped names, percentage region and honest preview', () => {
  const html=processing.renderRealProcess(state,{selectedIds:['media/a'],previewId:'media/a',options:{mode:'both',text:'<script>x</script>',region:{x:10,y:20,width:30,height:15}}});
  assert.match(html,/课程 &lt;一&gt;/);
  assert.doesNotMatch(html,/>未下载<|开始模拟处理|尚未接入|<script>x/);
  assert.match(html,/data-action="start-process"/);
  assert.match(html,/data-processing-region="x"[^>]*value="10"/);
  assert.match(html,/left:10%;top:20%;width:30%;height:15%/);
  assert.match(html,/仅显示位置/);
  assert.match(html,/移动水印|复杂背景/);
  assert.match(html,/\/api\/assets\/media%2Fa\/file/);
});

test('processing jobs expose cancellation, retry and restart semantics without download resume claims', () => {
  const html=processing.renderRealProcess({...state,processingJobs:[
    {id:'running',assetId:'media/a',status:'running',progress:35,phase:'processing'},
    {id:'paused',assetId:'media/a',status:'paused',progress:12},
    {id:'failed',assetId:'media/a',status:'failed',error:'<无法编码>'},
    {id:'done',assetId:'media/a',status:'completed',versionId:'v/one',progress:100},
  ]});
  assert.match(html,/data-action="processing-job-action" data-id="running" data-value="pause"/);
  assert.match(html,/data-id="running" data-value="cancel"/);
  assert.match(html,/data-id="paused" data-value="resume"/);
  assert.match(html,/data-id="failed" data-value="retry"/);
  assert.match(html,/从原件重新处理/);
  assert.match(html,/&lt;无法编码&gt;/);
  assert.doesNotMatch(html,/断点续传|已复用|正在传输/);
});

test('versions include only completed processed files and never infer a processed URL', () => {
  assert.deepEqual(versions.processedVersions(asset).map(v=>v.id),['v/one']);
  assert.equal(versions.versionFileUrl(asset,'original'),'/api/assets/media%2Fa/file');
  assert.equal(versions.versionFileUrl(asset,'v/one'),'/api/assets/media%2Fa/versions/v%2Fone/file');
  assert.equal(versions.versionFileUrl(asset,'broken'),null);
  assert.equal(versions.versionFileUrl(asset,'invented'),null);
  assert.equal(versions.hasOriginal(asset),true);
  assert.equal(versions.hasOriginal({...asset,status:'discovered'}),false);
});

test('version workspace compares actual files and exposes per-version download without overwriting', () => {
  const html=versions.renderVersionWorkspace(asset,{versionId:'v/one',compare:true});
  assert.match(html,/data-real-version/);
  assert.match(html,/value="v\/one" selected/);
  assert.match(html,/原始文件/);
  assert.match(html,/保存此版本/);
  assert.match(html,/\/api\/assets\/media%2Fa\/versions\/v%2Fone\/file\?download=true/);
  assert.match(html,/class="version-comparison"/);
  assert.equal((html.match(/<video /g)||[]).length,2);
  assert.doesNotMatch(html,/broken|水印处理示意|模拟/);
  const original=versions.renderVersionWorkspace(asset,{versionId:'invented',compare:true});
  assert.equal((original.match(/<video /g)||[]).length,1);
  assert.match(original,/value="original" selected/);
});

test('drag region coordinates clamp to the visible frame and reject accidental zero area', () => {
  assert.deepEqual(processing.regionFromPoints({x:80,y:60},{x:10,y:15}),{x:10,y:15,width:70,height:45});
  assert.deepEqual(processing.regionFromPoints({x:-4,y:0},{x:120,y:100}),{x:0,y:0,width:100,height:100});
  assert.equal(processing.regionFromPoints({x:5,y:5},{x:5,y:15}),null);
});

test('preview uses decoded image orientation and keeps wide frames exact', () => {
  const html = processing.renderRealProcess({...state,assets:[{...asset,width:1000,height:100}]});
  assert.match(html,/aspect-ratio:10;/);
  const events={};
  const media={tagName:'IMG',naturalWidth:0,naturalHeight:0,addEventListener:(name,fn)=>{events[name]=fn;}};
  const frame={style:{},querySelector:()=>media};
  const toggle={disabled:false};
  const container={querySelector:selector=>selector==='.processing-media-frame'?frame:toggle};
  const ready=processing.bindProcessingPreview(container);
  assert.equal(toggle.disabled,true);
  assert.equal(ready(),false);
  media.naturalWidth=80; media.naturalHeight=120; events.load();
  assert.equal(frame.style.aspectRatio,String(80/120));
  assert.equal(toggle.disabled,false);
  assert.equal(ready(),true);
});

test('cancelled region drag restores the actual configured rectangle', () => {
  const element = () => {
    const events={},attributes={};
    return {events,style:{},addEventListener:(name,handler)=>{events[name]=handler;},getAttribute:name=>attributes[name],setAttribute:(name,value)=>{attributes[name]=value;}};
  };
  const surface=element(),toggle=element(),mask=element();
  const media={...element(),tagName:'IMG',naturalWidth:100,naturalHeight:100};
  const frame={style:{},querySelector:()=>media};
  let drawing=false;
  surface.classList={toggle:(_name,value)=>{drawing=value;},contains:()=>drawing};
  surface.getBoundingClientRect=()=>({left:0,top:0,width:100,height:100});
  surface.setPointerCapture=()=>{};
  const fields={x:{value:70},y:{value:80},width:{value:25},height:{value:15}};
  const container={querySelector:selector=>({
    '[data-region-surface]':surface,'[data-region-toggle]':toggle,'[data-region-mask]':mask,
    '.processing-media-frame':frame,
    ...Object.fromEntries(Object.entries(fields).map(([key,value])=>[`[data-processing-region="${key}"]`,value])),
  })[selector]};
  const changes=[];
  processing.bindProcessingRegion(container,region=>changes.push(region));
  toggle.events.click();
  surface.events.pointerdown({clientX:10,clientY:10,button:0,pointerId:1,preventDefault(){}});
  surface.events.pointermove({clientX:40,clientY:30});
  assert.equal(mask.style.left,'10%');
  surface.events.pointercancel();
  assert.equal(mask.style.left,'70%');
  assert.equal(mask.style.top,'80%');
  assert.deepEqual(changes,[]);
});

test('file collections distinguish completed versions and use latest image output without fallback guesses', () => {
  const photo={...asset,kind:'image',thumbnail:'thumb.jpg'};
  assert.equal(versions.matchesFileCollection(photo,'processed'),true);
  assert.equal(versions.matchesFileCollection(photo,'original'),false);
  assert.equal(versions.matchesFileCollection({...photo,versions:[]},'original'),true);
  assert.equal(versions.matchesFileCollection({...photo,status:'discovered'},'all'),false);
  const view=versions.libraryFilePresentation(photo,'processed');
  assert.equal(view.versionId,'v/one');
  assert.equal(view.thumbnail,'/api/assets/media%2Fa/versions/v%2Fone/file');
  assert.equal(view.versionCount,1);
  assert.equal(versions.libraryFilePresentation(asset,'processed').thumbnail,asset.thumbnail);
  assert.equal(versions.libraryFilePresentation(photo,'all').versionId,'original');
});

test('live parameter preview updates overlays without replacing or touching media', () => {
  const watermark={style:{},className:'',textContent:''},mask={style:{}};
  const container={querySelector(selector){return selector==='.processing-watermark'?watermark:selector==='[data-region-mask]'?mask:null;}};
  processing.updateProcessingPreview(container,{text:'<hello>',opacity:40,fontSize:6,position:'top-left',region:{x:10,y:20,width:30,height:15}});
  assert.equal(watermark.textContent,'<hello>');
  assert.equal(watermark.style.opacity,'0.4');
  assert.equal(watermark.style.fontSize,'6cqh');
  assert.equal(watermark.className,'processing-watermark position-top-left');
  assert.deepEqual(mask.style,{left:'10%',top:'20%',width:'30%',height:'15%'});
});

test('switching to add excludes the hidden removal region while preserving the editing draft', () => {
  const draft={mode:'add',text:'课程',region:{x:95,y:10,width:20,height:''}};
  const before=structuredClone(draft);
  const options=processing.processingRequest(state,['media/a'],draft).body.options;
  assert.equal(options.region,null);
  assert.equal(options.text,'课程');
  assert.deepEqual(draft,before);
  assert.throws(()=>processing.processingRequest(state,['media/a'],{...draft,mode:'both'}),/修复区域/);
});

test('switching to remove ignores hidden adding fields without destroying their draft values', () => {
  const draft={mode:'remove',text:'',position:'invalid',opacity:'',fontSize:'',region:{x:10,y:20,width:25,height:15}};
  const before=structuredClone(draft);
  const options=processing.processingRequest(state,['media/a'],draft).body.options;
  assert.deepEqual(options,{mode:'remove',text:'FRAMEKEEP',position:'bottom-right',opacity:65,fontSize:4,region:draft.region});
  assert.deepEqual(draft,before);
  assert.throws(()=>processing.processingRequest(state,['media/a'],{...draft,mode:'both'}),/水印/);
});

test('moving repair sends positions for a single video and omits the hidden path in add mode', () => {
  const region = {x:65,y:80,width:20,height:15};
  const motion = [{time:0,x:0,y:0,transition:'jump'},{time:3,x:80,y:85,transition:'linear'}];
  const draft = {mode:'remove',region,motion};
  const local = {...state, assets:[asset,{...asset,id:'other',kind:'image'}]};
  assert.deepEqual(processing.processingRequest(local,[asset.id],draft).body.options.motion,motion);
  assert.throws(()=>processing.processingRequest(local,['other'],draft),/单个视频/);
  assert.throws(()=>processing.processingRequest(local,[asset.id,'other'],draft),/单个视频/);
  assert.equal(processing.processingRequest(local,['other'],{...draft,mode:'add'}).body.options.motion,undefined);
});

test('trajectory interpolates between recorded times, holds for jumps, and limits inserted markers', async () => {
  const {motionPosition,setMotionPoint} = await import('../dist/motion-editor.js');
  const marks = [{time:.5,x:0,y:0,transition:'linear'},{time:2.5,x:80,y:10,transition:'linear'},{time:4,x:5,y:20,transition:'jump'}];
  assert.equal(motionPosition(marks,.25),null);
  assert.deepEqual(motionPosition(marks,1.5),{x:40,y:5});
  assert.deepEqual(motionPosition(marks,3.99),{x:80,y:10});
  assert.deepEqual(motionPosition(marks,9),{x:5,y:20});
  const full = Array.from({length:32},(_,i)=>({...marks[0],time:i}));
  assert.equal(setMotionPoint(full,1.00001,{x:5,y:20},'jump').length,32);
  assert.throws(()=>setMotionPoint(full,32,{x:5,y:20},'linear'),/32/);
});

test('saved glyph version shows how much of the video was actually repaired', () => {
  const clip = {...asset, versions:[{...asset.versions[1], removalMethod:'chunlou-glyph',glyphFrames:4,totalFrames:10}]};
  assert.match(versions.renderVersionWorkspace(clip,{versionId:'v/one'}),/识别并修复 4 \/ 10 帧/);
  assert.doesNotMatch(versions.renderVersionWorkspace(clip,{versionId:'original'}),/识别并修复/);
});

test('each processing row can delete its task while a completed result remains accessible', () => {
  const statuses = ['queued', 'running', 'paused', 'failed', 'cancelled', 'completed'];
  const jobs = statuses.map(status => ({id:status, assetId:asset.id, status, versionId:'v/one'}));
  const html = processing.renderProcessingJobs({...state, processingJobs:jobs});
  for (const status of statuses) {
    assert.ok(html.includes(`data-action="delete-processing-job" data-id="${status}"`), status);
  }
  assert.match(html, /查看成品/);
});
