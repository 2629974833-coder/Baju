import test from 'node:test';
import assert from 'node:assert/strict';
import { createSessionPanel } from '../dist/real-session.js';

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

class Element {
  constructor() {
    this.listeners = new Map();
    this.dataset = {};
    this.value = '';
    this.textContent = '';
    this.hidden = false;
    this.disabled = false;
    this.isConnected = true;
    const classes = new Set();
    this.classList = {
      toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); },
      contains(name) { return classes.has(name); },
    };
  }
  addEventListener(type, listener, { signal } = {}) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push({ listener, signal });
  }
  dispatch(type, values = {}) {
    const event = { target: this, preventDefault() {}, stopPropagation() {}, ...values };
    return Promise.all((this.listeners.get(type) || [])
      .filter(({ signal }) => !signal?.aborted)
      .map(({ listener }) => listener(event)));
  }
  focus() { document.activeElement = this; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 1280, height: 800 }; }
}

class Root extends Element {
  set innerHTML(value) {
    for (const element of this.elements?.values() || []) element.isConnected = false;
    this.elements = new Map([...value.matchAll(/id="([^"]+)"/g)].map((match) => [`#${match[1]}`, new Element()]));
    this.elements.set('button', new Element());
  }
  querySelector(selector) { return this.elements.get(selector); }
  querySelectorAll() { return [...this.elements.values()]; }
}

function setup(t) {
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');
  const body = new Element();
  Object.defineProperty(globalThis, 'document', { configurable: true, value: { body, activeElement: body } });
  const timers = new Map();
  let nextTimer = 0;
  t.mock.method(globalThis, 'setTimeout', (callback) => { timers.set(++nextTimer, callback); return nextTimer; });
  t.mock.method(globalThis, 'clearTimeout', (id) => timers.delete(id));
  const root = new Root();
  const calls = [];
  const saves = [];
  let input = () => Promise.resolve({ ok: true });
  let reportedSaved = false;
  let savedCalls = 0;
  const api = {
    async request(path, options = {}) {
      calls.push({ path, options });
      if (path.endsWith('/save')) {
        const operation = deferred();
        saves.push(operation);
        return operation.promise;
      }
      if (path.endsWith('/input')) return input();
      if (path.endsWith('/screenshot')) return new Blob(['image']);
      return { saved: reportedSaved, title: '受控测试页', url: 'https://example.test/', tabs: [], width: 1280, height: 800 };
    },
  };
  const panel = createSessionPanel({ api, root, onClose() {}, onSaved: async () => { savedCalls++; } });
  t.after(() => {
    panel.close();
    if (originalDocument) Object.defineProperty(globalThis, 'document', originalDocument);
    else delete globalThis.document;
  });
  return {
    panel, root, calls, saves,
    get savedCalls() { return savedCalls; },
    get status() { return root.querySelector('#remote-status').textContent; },
    get error() { return root.querySelector('#remote-status').classList.contains('is-error'); },
    set input(value) { input = value; },
    set reportedSaved(value) { reportedSaved = value; },
    async open(id = 'first') { await panel.open({ id, name: '测试站点', url: 'https://example.test/' }); },
    save() { return root.querySelector('#remote-save').dispatch('click'); },
    refresh() { return root.querySelector('#remote-refresh').dispatch('click'); },
    async poll() {
      const entry = timers.entries().next().value;
      assert.ok(entry, 'screenshot polling should be scheduled');
      timers.delete(entry[0]);
      await entry[1]();
    },
    inputText(text = '测试文本') {
      root.querySelector('#remote-text').value = text;
      return root.querySelector('#remote-text-form').dispatch('submit');
    },
  };
}

const settle = async () => { for (let index = 0; index < 5; index++) await Promise.resolve(); };

test('saving remains visible while input and save requests are pending despite screenshot polls', async (t) => {
  const app = setup(t);
  await app.open();
  const pendingInput = deferred();
  app.input = () => pendingInput.promise;
  const input = app.inputText();
  const saving = app.save();
  assert.match(app.status, /等待站点请求.*保存/);
  assert.equal(app.root.querySelector('#remote-save').disabled, true);
  await app.poll();
  assert.match(app.status, /等待站点请求.*保存/);
  assert.equal(app.saves.length, 0);
  pendingInput.resolve({ ok: true });
  await input;
  await settle();
  assert.equal(app.saves.length, 1);
  await app.poll();
  assert.match(app.status, /等待站点请求.*保存/);
  app.saves[0].resolve({ saved: true });
  await saving;
  assert.match(app.status, /会话已保存，权限待验证/);
  assert.equal(app.savedCalls, 1);
});

test('save errors survive polls and manual screenshot refresh until retry succeeds', async (t) => {
  const app = setup(t);
  await app.open();
  const saving = app.save();
  await settle();
  app.saves[0].reject(new Error('会话保存失败：站点请求未结束'));
  await saving;
  assert.match(app.status, /站点请求未结束/);
  assert.equal(app.error, true);
  app.reportedSaved = true;
  await app.poll();
  await app.refresh();
  assert.match(app.status, /站点请求未结束/);
  assert.equal(app.error, true);
  const retry = app.save();
  assert.match(app.status, /等待站点请求.*保存/);
  assert.equal(app.error, false);
  await settle();
  app.saves[1].resolve({ saved: true });
  await retry;
  app.reportedSaved = false;
  await app.poll();
  assert.match(app.status, /会话已保存，权限待验证/);
  assert.equal(app.root.querySelector('#remote-save').disabled, false);
});

test('only a successful new browser operation clears a sticky save error', async (t) => {
  const app = setup(t);
  await app.open();
  const saving = app.save();
  await settle();
  app.saves[0].reject(new Error('保存失败，请重试'));
  await saving;
  const pendingInput = deferred();
  app.input = () => pendingInput.promise;
  const editing = app.inputText();
  await app.poll();
  assert.match(app.status, /保存失败/);
  pendingInput.resolve({ ok: true });
  await editing;
  await app.poll();
  assert.doesNotMatch(app.status, /保存失败/);
  assert.equal(app.error, false);
});

for (const outcome of ['resolve', 'reject']) {
  test(`a ${outcome === 'resolve' ? 'resolved' : 'rejected'} save from a closed session cannot affect the reopened panel`, async (t) => {
    const app = setup(t);
    await app.open('old-site');
    const saving = app.save();
    await settle();
    await app.open('new-site');
    const newStatus = app.status;
    app.saves[0][outcome](outcome === 'resolve' ? { saved: true } : new Error('旧窗口保存失败'));
    await saving;
    assert.equal(app.status, newStatus);
    assert.equal(app.savedCalls, 0);
    assert.equal(app.error, false);
    assert.equal(app.root.querySelector('#remote-save').disabled, false);
  });
}

test('a save waiting for old input never sends a request to a newly opened site', async (t) => {
  const app = setup(t);
  await app.open('old-site');
  const pendingInput = deferred();
  app.input = () => pendingInput.promise;
  const editing = app.inputText();
  await settle();
  const saving = app.save();
  await app.open('new-site');
  pendingInput.resolve({ ok: true });
  await editing;
  await settle();
  const unexpected = app.calls.filter(({ path }) => path.endsWith('/save'));
  for (const operation of app.saves) operation.resolve({ saved: true });
  await saving;
  assert.deepEqual(unexpected, []);
  assert.equal(app.savedCalls, 0);
});
