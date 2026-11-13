import test from 'node:test';
import assert from 'node:assert/strict';
import { classificationLabel, matchesContent, renderClassification } from '../dist/content-view.js';
import { renderQueue } from '../dist/workflow-view.js';

test('content labels do not claim automatic episode completeness', () => {
  assert.equal(classificationLabel({kind:'episode', source:'automatic'}), '正片候选');
  assert.equal(classificationLabel({kind:'episode', source:'manual'}), '正片（人工）');
  assert.equal(classificationLabel(), '待确认');
  assert.equal(matchesContent({kind:'video'}, 'unknown'), true);
  assert.equal(matchesContent({kind:'image'}, 'unknown'), false);
  assert.equal(matchesContent({kind:'video', classification:{kind:'preview'}}, 'episode'), false);
});

test('classification detail exposes reasons and manual reset with escaped values', () => {
  const html = renderClassification({id:'asset_x', kind:'video', classification:{kind:'preview', confidence:'high', source:'automatic', reasons:['<img onerror=x>']}});
  assert.match(html, /classification-form/);
  assert.match(html, /value="automatic"/);
  assert.match(html, /&lt;img onerror=x&gt;/);
  assert.doesNotMatch(html, /<img onerror/);
  assert.match(html, /高/);
});

test('real queue shows retry and durable resume evidence', () => {
  const state = {mode:'real',sites:[],assets:[{id:'a',title:'Media',sizeMB:2}],jobs:[{id:'j',assetId:'a',type:'download',status:'running',phase:'retrying',progress:50,retryCount:2,retryInSeconds:3,resumedBytes:1048576}]};
  let html = renderQueue(state);
  assert.match(html, /第 2 次重试/);
  assert.match(html, /3 秒/);
  assert.match(html, /复用 1 MB/);
  state.jobs[0] = {...state.jobs[0],status:'paused',resumeSupported:false,resumeMessage:'源站无验证器，将从头下载'};
  html = renderQueue(state);
  assert.match(html, /源站无验证器/);
  assert.doesNotMatch(html, />进度已保留</);
});
