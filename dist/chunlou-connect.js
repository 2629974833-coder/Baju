import { createApiClient } from './real-api.js';
import { setupBookmarkInstall } from './chunlou-bookmark-install.js';

const SOURCE = 'https://chunlou.tv';
const CHANNEL = 'framekeep.chunlou.account';
const ACCOUNT_KEYS = ['sb-grzjuztzhrexhvmetnvd-all-auth-token', 'sb-grzjuztzhrexhvmetnvd-auth-token'];
const NONCE = /^[a-f0-9-]{36}$/i;
const MAX_BODY = 65536;

export function panelOrigin(value) {
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password &&
      ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname) && value === url.origin;
  } catch {
    return false;
  }
}

export function siteIdFromURL(href) {
  const raw = new URL(href).searchParams.getAll('site');
  if (!raw.length) return 'chunlou';
  return raw.length === 1 && /^[A-Za-z0-9_-]{1,128}$/.test(raw[0]) ? raw[0] : null;
}

// Runs only when the user clicks the installed bookmark in their browser.
function associateFromSource(panel, channel, keys, site) {
  const w = window;
  if (w.location.origin !== 'https://chunlou.tv') {
    w.alert('请到春楼官网已登录的页面点击这个书签。');
    return;
  }
  // The browser needs the bookmark's user gesture here. Opening after an async
  // check would be treated as a popup without user interaction.
  let confirmation;
  try {
    confirmation = w.open(panel + '/chunlou-connect.html?site=' + encodeURIComponent(site) + '&from=bookmark', '_blank');
  } catch { /* Some browsers throw when opening a popup is disabled. */ }
  if (!confirmation || confirmation.closed) {
    w.alert('本机确认窗口被浏览器拦截。请允许春楼打开弹窗，再点一次书签。');
    return;
  }
  const siteNonce = w.crypto.randomUUID();
  const expires = Date.now() + 10 * 60_000;
  let nonce = '';
  let paired = false;
  const send = (type, fields = {}) => confirmation.postMessage({channel, type, siteNonce, nonce, ...fields}, panel);
  function stop() {
    clearTimeout(timer);
    w.removeEventListener('message', receive);
    w.removeEventListener('pagehide', stop);
  }
  function receive(event) {
    if (Date.now() >= expires) { stop(); return; }
    const data = event.data;
    if (event.source !== confirmation || event.origin !== panel || data?.channel !== channel ||
        typeof data.nonce !== 'string' || !/^[a-f0-9-]{36}$/i.test(data.nonce)) return;
    if (data.type === 'panel-ready') {
      if (data.nonce !== nonce) paired = false;
      nonce = data.nonce;
      send('hello');
      return;
    }
    if (!nonce || data.nonce !== nonce || data.siteNonce !== siteNonce) return;
    if (data.type === 'pair') {
      paired = true;
      send('ready');
      return;
    }
    if (data.type === 'cancel') { stop(); return; }
    if (!paired || data.type !== 'read') return;
    stop();
    let session, storageKey;
    try {
      for (const key of keys) {
        const raw = w.localStorage.getItem(key);
        if (raw === null) continue;
        storageKey = key;
        try { session = JSON.parse(raw); }
        catch { send('error', {reason:'invalid'}); return; }
        break;
      }
      if (!storageKey) { send('error', {reason:'no-session'}); return; }
      if (typeof session?.access_token !== 'string' || !session.access_token ||
          typeof session.refresh_token !== 'string' || !session.refresh_token ||
          typeof session.user?.id !== 'string' || !session.user.id || session.user.is_anonymous === true) {
        send('error', {reason:'invalid'}); return;
      }
      if (!Number.isFinite(session.expires_at) || session.expires_at <= Date.now() / 1000) {
        send('error', {reason:'expired'}); return;
      }
      const body = {storageKey, session:JSON.stringify({
        access_token:session.access_token, refresh_token:session.refresh_token,
        expires_at:session.expires_at, token_type:'bearer', user:{id:session.user.id, is_anonymous:false}
      })};
      if (new TextEncoder().encode(JSON.stringify(body)).length > 65536) {
        send('error', {reason:'size'}); return;
      }
      send('account', body);
    } catch {
      send('error', {reason:'storage'});
    }
  }
  const timer = setTimeout(stop, 10 * 60_000);
  w.addEventListener('message', receive);
  w.addEventListener('pagehide', stop, {once:true});
}

export function bookmarkURL(origin, site = 'chunlou') {
  if (!panelOrigin(origin)) throw new Error('需要从本机拾帧面板创建书签。');
  if (typeof site !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(site)) throw new Error('来源编号无效。');
  const args = [origin, CHANNEL, ACCOUNT_KEYS, site].map(x => JSON.stringify(x)).join(',');
  return 'javascript:' + encodeURIComponent('void (' + associateFromSource.toString() + ')(' + args + ')');
}

const sourceErrors = {
  missing:'旧版书签只检查了一个登录位置。请在已登录的春楼标签页点击更新后的书签。',
  'no-session':'当前春楼标签页没有登录记录。请在这个浏览器打开春楼，登录到能看到自己账号的页面，再点书签。',
  expired:'春楼登录已过期。请刷新日常浏览器里的春楼，确认仍已登录后重新关联。',
  invalid:'当前记录缺少完整的已登录账号。请在春楼确认已登录，再重新关联。',
  storage:'无法读取春楼会话。请允许官网使用站点存储，再重新关联。',
  size:'账号记录超过本机导入上限。'
};

export function association({openWindow, save, report, now = Date.now}) {
  let popup, nonce, siteNonce, deadline, phase;
  let bookmarkOpened = false;
  function update(next, detail = '', reason) {
    phase = next;
    report(next, detail, reason);
  }
  function send(type) {
    popup.postMessage({channel:CHANNEL, type, siteNonce, nonce}, SOURCE);
  }
  function reset() {
    // A refreshed confirmation page can initiate another round with its opener.
    if (!bookmarkOpened && popup && !popup.closed && siteNonce) send('cancel');
    popup = null;
    bookmarkOpened = false;
    nonce = siteNonce = '';
    deadline = 0;
    phase = 'idle';
  }
  function fail(message, reason) {
    reset();
    update('error', message, reason);
  }
  function tick() {
    if (!popup || phase === 'saving') return;
    if (popup.closed) fail('春楼标签页已关闭或窗口关联已断开。若正在第三方登录，请先在官网完成，再重新关联。');
    else if (now() >= deadline) fail('未能在时限内完成关联。请回到已登录的春楼页，再点一次书签。');
    else if (bookmarkOpened && phase === 'waiting' && !siteNonce) send('panel-ready');
  }
  function open() {
    if (phase === 'saving') return;
    reset();
    popup = openWindow(SOURCE + '/feed', '_blank');
    if (!popup || popup.closed) { fail('浏览器拦截了新窗口。请允许本机拾帧打开弹窗后重试。'); return; }
    nonce = crypto.randomUUID();
    deadline = now() + 10 * 60_000;
    update('waiting');
  }
  function fromBookmark(opener) {
    if (phase === 'saving') return;
    reset();
    if (!opener || opener.closed) {
      fail('未找到开启本页的春楼标签页。请回到已登录的春楼官网，再点一次书签。');
      return;
    }
    bookmarkOpened = true;
    popup = opener;
    nonce = crypto.randomUUID();
    deadline = now() + 30_000;
    update('waiting', '正在连接刚才点击书签的春楼标签页…');
    send('panel-ready');
  }
  function confirm() {
    tick();
    if (phase !== 'ready') return;
    deadline = now() + 15_000;
    update('reading');
    send('read');
  }
  async function receive(event) {
    const data = event.data;
    if (!popup || event.origin !== SOURCE || event.source !== popup || data?.channel !== CHANNEL ||
        typeof data.siteNonce !== 'string' || !NONCE.test(data.siteNonce)) return;
    if (popup.closed || now() >= deadline) { tick(); return; }
    // A fresh bookmark click after a source page refresh starts a new pairing.
    if (data.type === 'hello' && (phase === 'waiting' || phase === 'ready')) {
      if (bookmarkOpened && (data.nonce !== nonce || (siteNonce && siteNonce !== data.siteNonce))) return;
      if (siteNonce && siteNonce !== data.siteNonce) {
        send('cancel');
        update('waiting');
      }
      siteNonce = data.siteNonce;
      send('pair');
      return;
    }
    if (data.siteNonce !== siteNonce || data.nonce !== nonce) return;
    if (data.type === 'ready' && phase === 'waiting') {
      deadline = now() + 10 * 60_000;
      update('ready');
      return;
    }
    if (phase !== 'reading') return;
    if (data.type === 'error') {
      if (typeof data.reason === 'string' && Object.hasOwn(sourceErrors, data.reason)) {
        fail(sourceErrors[data.reason], data.reason);
      } else {
        fail('春楼返回了无法读取的账号记录。');
      }
      return;
    }
    if (data.type !== 'account' || !ACCOUNT_KEYS.includes(data.storageKey) || typeof data.session !== 'string') return;
    const body = {storageKey:data.storageKey, session:data.session};
    if (data.session.length > MAX_BODY || new TextEncoder().encode(JSON.stringify(body)).length > MAX_BODY) {
      fail(sourceErrors.size); return;
    }
    reset();
    update('saving');
    try {
      const result = await save(body);
      if (result?.saved !== true || result.accountVerified !== true) throw new Error('没有收到账号核对及保存成功的结果。');
      update('saved');
    } catch (error) {
      fail(error.message || '保存失败。请检查本机服务后重试。');
    }
  }
  return {open, fromBookmark, confirm, receive, tick, reset};
}

async function mount() {
  const elem = id => document.getElementById(id);
  const opener = elem('open-site'), confirm = elem('confirm-save');
  const restart = elem('restart'), status = elem('status');
  const returnSource = elem('return-source'), openSource = elem('open-source'), updateBookmark = elem('update-bookmark');
  const fromSite = new URL(location.href).searchParams.get('from') === 'bookmark';
  const messages = {
    loading:'正在连接本机拾帧…', idle:'安装书签后，在已登录的春楼标签页点击它。',
    waiting:'请在春楼页面点击书签栏里的「关联春楼账号」。新版书签将打开本机确认页。',
    ready:'春楼标签页已关联。确认后，拾帧会向春楼核对账号，并在本机加密保存会话。',
    reading:'正在接收该站点的账号会话…', saving:'正在核对春楼账号并保存…',
    saved:'账号已确认，会话已保存。可以回到拾帧重新拾取。若之后提示会话过期，请刷新日常浏览器里的春楼，再重新关联。'
  };
  let importing = false;
  let contactedSource = false;
  function render(phase, message, reason) {
    importing = phase === 'saving';
    const finishedAttempt = fromSite && contactedSource && phase === 'error';
    if (fromSite || phase === 'ready') document.body.dataset.flow = 'confirm';
    else if (phase === 'idle') document.body.dataset.flow = 'install';
    status.textContent = message || messages[phase];
    status.dataset.state = phase;
    opener.disabled = phase !== 'idle';
    confirm.disabled = phase !== 'ready';
    restart.hidden = phase !== 'error' || finishedAttempt;
    returnSource.hidden = !finishedAttempt || !window.opener || window.opener.closed;
    openSource.hidden = !finishedAttempt || !returnSource.hidden;
    updateBookmark.hidden = !(phase === 'error' && reason === 'missing');
    elem('return-panel').hidden = phase !== 'saved';
    if (phase === 'saved') confirm.hidden = true;
  }
  const id = siteIdFromURL(location.href);
  if (!id || !panelOrigin(location.origin) || window.top !== window.self) {
    render('unavailable','请在运行拾帧的电脑上，从「来源 → 春楼 → 站点授权」打开此页。');
    return;
  }
  updateBookmark.href = '/chunlou-connect.html?site=' + encodeURIComponent(id) + '#manual-update';
  if (!fromSite && location.hash === '#manual-update') elem('manual-update').open = true;
  elem('bookmark-address').addEventListener('focus', event => event.target.select());
  returnSource.addEventListener('click', () => {
    if (!window.opener || window.opener.closed) {
      returnSource.hidden = true;
      openSource.hidden = false;
      return;
    }
    window.opener.focus();
    window.close();
  });
  // This preference belongs to the local panel. The bookmark only reads the Chunlou account key.
  try {
    const theme = localStorage.getItem('framekeep-theme');
    if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
  } catch { /* The panel still works when local preferences are disabled. */ }
  const api = createApiClient((path, options) => fetch(path, {
    // A same-origin POST with no-referrer can send "Origin: null", which the panel rejects.
    ...options, mode:'same-origin', credentials:'same-origin', redirect:'error', referrerPolicy:'same-origin'
  }));
  const endpoint = '/api/sites/' + encodeURIComponent(id) + '/local-auth';
  const installation = fromSite ? null : setupBookmarkInstall({document, panelURL:location.href});
  let enabled = false;
  const handoff = association({
    openWindow:(url,name)=>window.open(url,name),
    save:body=>api.request(endpoint+'/import',{method:'POST',body}),
    report:render
  });
  async function check() {
    enabled = false;
    contactedSource = false;
    handoff.reset();
    installation?.disable();
    render('loading');
    try {
      const info = await api.request(endpoint,{signal:AbortSignal.timeout(15_000)});
      if (info?.canImportAccount !== true || info.active !== false) {
        render('error', info?.active ? '专用授权窗口正在使用。请先在拾帧保存或取消该窗口，再重新检查。' : '本机服务未启用这个站点的账号关联。请检查来源及服务版本。');
        return;
      }
      enabled = true;
      if (fromSite) {
        contactedSource = true;
        handoff.fromBookmark(window.opener);
      }
      else {
        installation.enable(bookmarkURL(location.origin, id));
        render('idle');
      }
    } catch (error) {
      render('error', error.message);
    }
  }
  opener.addEventListener('click',()=>{if (enabled) handoff.open();});
  confirm.addEventListener('click',()=>{if (enabled) handoff.confirm();});
  restart.addEventListener('click',()=>{if (!importing) void check();});
  window.addEventListener('message',handoff.receive);
  const timer = setInterval(handoff.tick, 1000);
  window.addEventListener('pagehide',()=>{
    clearInterval(timer);
    handoff.reset();
    installation?.disable();
    window.removeEventListener('message',handoff.receive);
  },{once:true});
  window.addEventListener('pageshow',event=>{if (event.persisted) location.reload();});
  await check();
}

if (typeof document !== 'undefined') void mount();
