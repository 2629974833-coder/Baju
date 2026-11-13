import test from 'node:test';
import {groupLocalAssets} from '../dist/asset-series.js';
import {librarySearchIds, libraryVisibleGroups, renderLibraryGroups} from '../dist/library-groups-view.js';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { createInitialState } from '../dist/model.js';
import { isValidStoredState } from '../dist/view-model.js';
import { matchesFileCollection, hasOriginal } from '../dist/version-view.js';

const app = readFileSync(new URL('../dist/app.js', import.meta.url), 'utf8');
const between = (start, end) => app.slice(app.indexOf(start), app.indexOf(end));
const controllerSource = [
  between('function catalogScope(', 'function renderCatalog('),
  between('function requestDiscoveryRemoval(', 'function openModal('),
  between('async function refreshRealState(', 'function renderRealScans('),
].join('\n');

function workspace(real, api = {}, state = createInitialState()) {
  const ui = { page: 'discover', kind: 'all', category: 'all', site: 'all', status: 'all', q: '', collection: 'all', selected: new Set() };
  const feedback = [];
  let saved;
  const context = vm.createContext({groupLocalAssets,librarySearchIds,libraryVisibleGroups,renderLibraryGroups,
    state, ui, api, stateReadGeneration: 0, isReal: () => real, hasOriginal, matchesFileCollection,
    matchesContent: () => true, findSite: id => state.sites.find(site => site.id === id),
    renderModal() {}, renderPage() {}, renderServiceWarning() {}, $: () => null,
    openModal(type, data) { ui.modal = { type, ...data }; },
    closeModal() { ui.modal = null; },
    persist() { if (!real) saved = JSON.stringify(state); },
    toast: message => feedback.push(message),
  });
  vm.runInContext(controllerSource, context);
  return { context, ui, state, feedback, saved: () => JSON.parse(saved) };
}

test('confirmation freezes checked IDs in the current search, including when a new result arrives', async () => {
  const requests = [];
  const { context, ui, state } = workspace(true, {
    request: async (path, options) => { requests.push([path, options]); return { dismissed: 1 }; },
  });
  const target = state.assets.find(hasOriginal);
  const other = state.assets.find(asset => asset.id !== target.id);
  ui.selected.add(target.id).add(other.id);
  ui.q = target.title;
  context.requestDiscoveryRemoval();
  assert.equal(ui.modal.title.includes('1 项'), true);
  state.assets.unshift({ ...target, id: 'later' });
  await context.dismissDiscoverySelection();
  assert.equal(requests[0][0], '/api/assets/dismiss');
  assert.deepEqual(Array.from(requests[0][1].body.ids), [target.id]);
  assert.equal(requests[0][1].method, 'POST');
  assert.equal(ui.selected.has(target.id), false);
  assert.deepEqual(Array.from(context.catalogAssets(), a => a.id), ['later']);
  ui.page = 'library';
  assert.ok(context.catalogAssets().some(a => a.id === target.id));
});

test('searching the discovery list keeps the batch bar and removal scoped to its visible checks', () => {
  const {context,ui,state}=workspace(true);
  const bar={innerHTML:''};
  Object.assign(context, {
    icon:()=>'', esc:String, pageHeading:()=>'', renderRealScans:()=>'',
    renderCatalogEmpty:()=>'', renderAsset:()=>'', contentKinds:[],
    findAsset:id=>state.assets.find(asset=>asset.id===id), $:()=>bar,
  });
  vm.runInContext(between('function renderCatalog()', 'function renderAsset(') +
    between('function renderBatch()', 'function requestDiscoveryRemoval('),context);
  const [a,b]=state.assets;
  ui.selected.add(a.id).add(b.id);
  ui.q=a.title;
  assert.match(context.renderCatalog(),/已选择 1 项/);
  context.renderBatch();
  assert.deepEqual([...ui.selected],[a.id]);
  assert.match(bar.innerHTML,/data-action="dismiss-selection"/);
  context.requestDiscoveryRemoval();
  assert.deepEqual(Array.from(ui.modal.ids),[a.id]);
  ui.q='no-matching-assets';
  context.renderCatalog();
  context.renderBatch();
  assert.equal(bar.innerHTML,'');
});

test('a stale bulk delete click after a filter change explains the empty scope without writing', () => {
  const {ui,state,context,feedback}=workspace(true);
  ui.selected.add(state.assets[0].id);
  ui.q='this-does-not-match-a-fixture';
  context.requestDiscoveryRemoval();
  assert.equal(ui.modal,undefined);
  assert.match(feedback.at(-1),/重新勾选/);
});

test('request failure keeps the confirmation and selection available for retry', async () => {
  let attempts = 0;
  const { context, ui, state, feedback } = workspace(true, {
    request: async () => {
      if (++attempts === 1) throw new Error('服务暂不可用');
      return { dismissed: 1 };
    },
  });
  const target = state.assets[0];
  ui.selected.add(target.id);
  context.requestDiscoveryRemoval();
  await context.dismissDiscoverySelection();
  assert.equal(feedback.at(-1), '服务暂不可用');
  assert.equal(ui.modal.busy, false);
  assert.equal(ui.selected.has(target.id), true);
  assert.ok(!target.dismissedFromDiscovery);
  await context.dismissDiscoverySelection();
  assert.ok(target.dismissedFromDiscovery);
  assert.equal(ui.modal, null);
});

test('an in-flight older poll cannot reintroduce an item after a successful dismissal', async () => {
  let finishRead, finishWrite, writes = 0;
  const { context, ui, state } = workspace(true, {
    state: () => new Promise(resolve => { finishRead = resolve; }),
    request: () => { writes++; return new Promise(resolve => { finishWrite = resolve; }); },
  });
  const snapshot = structuredClone(state);
  const poll = context.refreshRealState();
  const target = state.assets[0];
  ui.selected.add(target.id);
  context.requestDiscoveryRemoval();
  const first = context.dismissDiscoverySelection();
  await context.dismissDiscoverySelection();
  assert.equal(writes, 1);
  finishWrite({ dismissed: 1 });
  await first;
  finishRead(snapshot);
  await poll;
  assert.ok(!context.catalogScope().some(a => a.id === target.id));
});

test('demo dismissal survives reloading and retains the processed file and job references', async () => {
  const { context, ui, state, saved } = workspace(false);
  const target = state.assets.find(asset => asset.status === 'processed');
  const jobs = structuredClone(state.jobs);
  ui.selected.add(target.id);
  context.requestDiscoveryRemoval();
  await context.dismissDiscoverySelection();
  const disk = saved();
  assert.ok(isValidStoredState(disk));
  assert.deepEqual(disk.jobs, jobs);
  const reload = workspace(false, {}, disk);
  assert.ok(!reload.context.catalogScope().some(a => a.id === target.id));
  reload.ui.page = 'library';
  assert.ok(reload.context.catalogAssets().some(a => a.id === target.id));
});
