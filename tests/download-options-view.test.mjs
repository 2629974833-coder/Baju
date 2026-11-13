import test from 'node:test';
import assert from 'node:assert/strict';
import { downloadRequest, parseDownloadOptions, renderDownloadOptions, renderDownloadSelection } from '../dist/download-options-view.js';
import { renderQueue } from '../dist/workflow-view.js';

const adaptive = {protocol:'hls',selectable:true,heights:[1080,480,720,720,0],audioTracks:[{language:'en',name:'English',default:true},{language:'zh-Hans',name:'简体中文'}],message:''};

test('single-source options use discovered sizes and audio tracks only', () => {
  const html = renderDownloadOptions({count:1,title:'<episode>',capabilities:adaptive});
  assert.match(html, /&lt;episode&gt;/);
  assert.match(html, /原始最高/);
  assert.match(html, /value="480"/);
  assert.match(html, /value="1080"/);
  assert.doesNotMatch(html, /value="2160"/);
  assert.equal((html.match(/value="720"/g) || []).length, 1);
  assert.match(html, /value="zh-Hans"/);
  assert.match(html, /English/);
  assert.match(html, /不转码/);
});

test('unknown source size and audio never invent selectable alternatives', () => {
  const html = renderDownloadOptions({count:1,capabilities:{...adaptive,heights:[],audioTracks:[]}});
  assert.match(html, /未提供画质尺寸/);
  assert.match(html, /未提供可选音轨/);
  assert.doesNotMatch(html, /value="720"|value="en"/);
  assert.match(html, /value="auto"/);
});

test('direct files retain originals without editable quality and audio controls', () => {
  const html = renderDownloadOptions({count:1,capabilities:{protocol:'http',selectable:false,heights:[],audioTracks:[],message:'保留原文件'}});
  assert.match(html, /原件/);
  assert.doesNotMatch(html, /name="maxHeight"|name="audioLanguage"/);
  assert.deepEqual(parseDownloadOptions({maxHeight:'720',audioLanguage:'en'}, {capabilities:{protocol:'http',selectable:false}}), {maxHeight:null,audioLanguage:'auto'});
});

test('batch options explain per-source preference and validate language tags', () => {
  const html = renderDownloadOptions({count:3,batch:true});
  assert.match(html, /3 项/);
  assert.match(html, /value="720"/);
  assert.match(html, /name="audioLanguage"/);
  assert.match(html, /直接文件和图片/);
  assert.deepEqual(parseDownloadOptions({maxHeight:'1080',audioLanguage:'zh-Hans'}, {batch:true}), {maxHeight:1080,audioLanguage:'zh-hans'});
  assert.deepEqual(parseDownloadOptions({maxHeight:'',audioLanguage:' AUTO '}, {batch:true}), {maxHeight:null,audioLanguage:'auto'});
  assert.throws(() => parseDownloadOptions({maxHeight:'100',audioLanguage:'en'}, {batch:true}), /画质/);
  assert.throws(() => parseDownloadOptions({maxHeight:'720',audioLanguage:'English'}, {batch:true}), /语言/);
  assert.throws(() => parseDownloadOptions({maxHeight:'720',audioLanguage:'<script>'}, {batch:true}), /语言/);
});

test('single-source submission rejects unknown sizes and tracks', () => {
  assert.deepEqual(parseDownloadOptions({maxHeight:'720',audioLanguage:'EN'}, {capabilities:adaptive}), {maxHeight:720,audioLanguage:'en'});
  assert.throws(() => parseDownloadOptions({maxHeight:'2160',audioLanguage:'en'}, {capabilities:adaptive}), /画质/);
  assert.throws(() => parseDownloadOptions({maxHeight:'720',audioLanguage:'fr'}, {capabilities:adaptive}), /音轨/);
});

test('inspection failure exposes retry and explicit default instead of fictitious options', () => {
  const html = renderDownloadOptions({count:1,status:'error',error:'源不可达 <script>'});
  assert.match(html, /role="alert"/);
  assert.match(html, /源不可达 &lt;script&gt;/);
  assert.match(html, /data-action="retry-download-options"/);
  assert.match(html, /data-action="download-default"/);
  assert.match(html, /按原始默认下载/);
  assert.doesNotMatch(html, /name="maxHeight"|type="submit"/);
  const loading = renderDownloadOptions({count:1,status:'loading'});
  assert.match(loading, /正在读取/);
  assert.doesNotMatch(loading, /type="submit"|name="maxHeight"/);
});

test('selection summary distinguishes requested preferences, actual media, and unchanged originals', () => {
  const html = renderDownloadSelection({maxHeight:1080,audioLanguage:'ja'}, {protocol:'hls',height:720,audioLanguage:'en',optionsApplied:true,message:'未找到所选语言 <fallback>'});
  assert.match(html, /最高 1080p/);
  assert.match(html, /音轨 ja/);
  assert.match(html, /实际：720p · 音轨 en/);
  assert.match(html, /&lt;fallback&gt;/);
  const original = renderDownloadSelection({maxHeight:720,audioLanguage:'en'}, {protocol:'http',optionsApplied:false,message:'直接文件保留原件'});
  assert.match(original, /保留原件/);
  assert.doesNotMatch(original, /实际：720p/);
  const waiting = renderDownloadSelection({maxHeight:null,audioLanguage:'auto'});
  assert.match(waiting, /实际：下载时确认/);
});

test('queue exposes frozen preferences and actual result, and failed tasks can change options', () => {
  const state = {mode:'real',sites:[],assets:[{id:'a',title:'Media',kind:'video',sizeMB:2}],jobs:[{id:'j',assetId:'a',type:'download',status:'failed',progress:0,downloadOptions:{maxHeight:1080,audioLanguage:'ja'},selection:{protocol:'hls',height:720,audioLanguage:'en',optionsApplied:true,message:'已选源站音轨'}}]};
  const html = renderQueue(state);
  assert.match(html, /最高 1080p/);
  assert.match(html, /实际：720p · 音轨 en/);
  assert.match(html, /data-action="retry-download-selection"/);
  state.jobs[0].status = 'running';
  assert.doesNotMatch(renderQueue(state), /data-action="retry-download-selection"/);
});


test('download requests keep selection options with the correct asset, episode, or retry target', () => {
  const options = {maxHeight:720,audioLanguage:'ja'};
  assert.deepEqual(downloadRequest({ids:['asset-a']}, options), {path:'/api/downloads',body:{ids:['asset-a'],options}});
  assert.deepEqual(downloadRequest({scanId:'scan/a',episodeKeys:['C:2','C:3']}, options), {path:'/api/scans/scan%2Fa/downloads',body:{episodeKeys:['C:2','C:3'],options}});
  assert.deepEqual(downloadRequest({jobId:'job/a',ids:['asset-a']}, options), {path:'/api/jobs/job%2Fa/action',body:{action:'retry',options}});
});

test('older jobs without options retry with explicit original defaults', () => {
  const options = parseDownloadOptions({}, {capabilities:adaptive});
  assert.deepEqual(downloadRequest({jobId:'old-job'}, options), {path:'/api/jobs/old-job/action',body:{action:'retry',options:{maxHeight:null,audioLanguage:'auto'}}});
  const html = renderDownloadOptions({retry:true,capabilities:adaptive});
  assert.match(html, /应用选项并重试/);
  assert.match(html, /value="">原始最高/);
  assert.match(html, /value="auto">自动/);
});

test('known dimensions outside the preference range keep original selection without claiming missing metadata', () => {
  const html = renderDownloadOptions({capabilities:{...adaptive,heights:[48,8640]}});
  assert.doesNotMatch(html, /value="48"|value="8640"|未提供画质尺寸/);
  assert.match(html, /按原始画质下载/);
  assert.deepEqual(parseDownloadOptions({maxHeight:'',audioLanguage:'auto'}, {capabilities:{...adaptive,heights:[48,8640]}}), {maxHeight:null,audioLanguage:'auto'});
});

test('unlabelled audio keeps its selectable value and uses a readable label', () => {
  const capabilities = {...adaptive,audioTracks:[{language:'und',name:'und',default:true}]};
  const html = renderDownloadOptions({capabilities});
  assert.match(html, /value="und"[^>]*>语言未标注（源站默认）/);
  assert.doesNotMatch(html, />und · und/);
  assert.deepEqual(parseDownloadOptions({maxHeight:'720',audioLanguage:'und'}, {capabilities}), {maxHeight:720,audioLanguage:'und'});
  const summary = renderDownloadSelection({maxHeight:360,audioLanguage:'und'}, {protocol:'hls',height:360,audioLanguage:'und'});
  assert.match(summary, /偏好：最高 360p · 语言未标注/);
  assert.match(summary, /实际：360p · 语言未标注/);
  assert.doesNotMatch(summary, /音轨 und/);
});

test('missing separate audio track does not claim the downloaded video is silent', () => {
  const html = renderDownloadSelection({maxHeight:null,audioLanguage:'auto'}, {protocol:'dash',height:720,audioLanguage:'none'});
  assert.match(html, /实际：720p · 无独立音轨/);
  assert.doesNotMatch(html, /音轨 none|无声音|静音/);
});
