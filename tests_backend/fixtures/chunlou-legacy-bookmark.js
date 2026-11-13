void (function associateFromSource(panel, channel, key, site) {
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
    try {
      const raw = w.localStorage.getItem(key);
      if (!raw) { send('error', {reason:'missing'}); return; }
      const session = JSON.parse(raw);
      if (typeof session?.access_token !== 'string' || !session.access_token ||
          typeof session.refresh_token !== 'string' || !session.refresh_token ||
          typeof session.user?.id !== 'string' || !session.user.id || session.user.is_anonymous === true) {
        send('error', {reason:'invalid'}); return;
      }
      if (!Number.isFinite(session.expires_at) || session.expires_at <= Date.now() / 1000) {
        send('error', {reason:'expired'}); return;
      }
      const body = {storageKey:key, session:JSON.stringify({
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
})("http://127.0.0.1:4173","framekeep.chunlou.account","sb-grzjuztzhrexhvmetnvd-auth-token","chunlou")
