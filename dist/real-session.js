import { icon, escapeHtml as esc } from './ui.js';

/** An image-backed browser surface. Credentials are sent only as transient input. */
export function createSessionPanel({ api, root, onClose, onSaved, onLocalAuth }) {
  let controller, timer, imageUrl, active = false, siteId, width = 1280, height = 800;
  let inputQueue = Promise.resolve(), refreshingSignal = null;
  let saveNotice = null, reportedSaved = false;
  const select = (selector) => root.querySelector(selector);
  const endpoint = (suffix = '') => `/api/sites/${encodeURIComponent(siteId)}/session${suffix}`;
  const isCurrent = (signal) => active && controller?.signal === signal && !signal.aborted;
  const sessionMessage = () => reportedSaved ? '会话已保存，权限待验证。' : '点击浏览器画面操作；完成登录后点击“保存会话”。';
  const message = (text, error = false) => {
    const el = select('#remote-status');
    if (el) { el.textContent = text; el.classList.toggle('is-error', error); }
  };
  // Screenshot polling must not dismiss an in-flight save or its result.
  function showStatus(text = sessionMessage(), error = false) {
    message(saveNotice?.text ?? text, saveNotice?.error ?? error);
  }
  function showSaveStatus(phase, text, error = false) {
    saveNotice = { phase, text, error };
    showStatus();
  }
  function close() {
    active = false;
    clearTimeout(timer);
    controller?.abort();
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = null;
  }
  async function refresh() {
    if (!active || refreshingSignal === controller.signal) return;
    const signal = controller.signal;
    refreshingSignal = signal;
    clearTimeout(timer);
    try {
      const [status, blob] = await Promise.all([
        api.request(endpoint(), { signal }),
        api.request(endpoint('/screenshot'), { blob: true, signal }),
      ]);
      if (!isCurrent(signal)) return;
      width = Number(status.width) || 1280;
      height = Number(status.height) || 800;
      const img = select('#remote-screen');
      const previous = imageUrl;
      imageUrl = URL.createObjectURL(blob);
      img.src = imageUrl;
      img.hidden = false;
      if (previous) URL.revokeObjectURL(previous);
      const address = select('#remote-address');
      if (document.activeElement !== address) address.value = status.url || '';
      const picker = select('#remote-tabs');
      const tabs = status.tabs || [];
      const signature = JSON.stringify(tabs);
      if (picker.dataset.tabs !== signature && document.activeElement !== picker) {
        picker.innerHTML = tabs.map((tab) => `<option value="${esc(tab.id)}" ${(status.activeTabId ? tab.id === status.activeTabId : tab.url === status.url) ? 'selected' : ''}>${esc(tab.title || tab.url || '新标签页')}</option>`).join('');
        picker.dataset.tabs = signature;
      }
      select('#remote-title').textContent = status.title || '站点登录窗口';
      reportedSaved = Boolean(status.saved);
      showStatus();
    } catch (error) {
      if (error.name !== 'AbortError' && isCurrent(signal)) showStatus(error.message, true);
    } finally {
      if (refreshingSignal === signal) refreshingSignal = null;
      if (active && controller.signal === signal && !signal.aborted) timer = setTimeout(refresh, 1000);
    }
  }
  function send(input) {
    const signal = controller.signal;
    const path = endpoint('/input');
    const noticeAtInput = saveNotice;
    inputQueue = inputQueue.catch(() => {}).then(async () => {
      if (!isCurrent(signal)) return;
      await api.request(path, { method: 'POST', body: input, signal });
      if (!isCurrent(signal)) return;
      if (noticeAtInput && saveNotice === noticeAtInput && saveNotice.phase !== 'saving') {
        saveNotice = null;
        showStatus();
      }
    });
    return inputQueue.catch((error) => {
      if (error.name !== 'AbortError' && isCurrent(signal)) showStatus(error.message, true);
    });
  }
  function coordinates(event) {
    const rect = select('#remote-screen').getBoundingClientRect();
    return { x: Math.max(0, Math.min(width - 1, Math.round((event.clientX - rect.left) * width / rect.width))), y: Math.max(0, Math.min(height - 1, Math.round((event.clientY - rect.top) * height / rect.height))) };
  }
  async function open(site, {publicMode = false} = {}) {
    close();
    active = true;
    siteId = site.id;
    controller = new AbortController();
    inputQueue = Promise.resolve();
    saveNotice = null;
    reportedSaved = false;
    const signal = controller.signal;
    root.innerHTML = `<div class="modal-backdrop remote-backdrop"><section class="modal remote-modal" role="dialog" aria-modal="true" aria-label="真实站点登录"><div class="modal-header"><div><h2>${esc(site.name)} · 站点登录</h2><p id="remote-title">正在启动独立浏览器…</p></div><button class="icon-button" data-action="close-modal" aria-label="关闭登录窗口">${icon('x', 20)}</button></div><div class="remote-toolbar"><select class="select" id="remote-tabs" aria-label="浏览器标签页"></select><form id="remote-navigation"><input class="input" id="remote-address" type="url" value="${esc(site.url || `https://${site.domain}`)}" aria-label="远程浏览器地址" required autocomplete="off"><button class="btn btn-secondary" type="submit">前往</button></form><button class="icon-button" id="remote-refresh" aria-label="刷新浏览器画面" title="刷新画面">${icon('refresh', 18)}</button></div><div class="remote-screen-container"><img id="remote-screen" class="remote-screen" alt="远程站点浏览器画面，点击后可输入和滚动" tabindex="0" hidden></div><div class="remote-input-tools"><form id="remote-text-form" autocomplete="off"><input class="input" id="remote-text" type="text" placeholder="中文或粘贴文本，发送至远程浏览器当前焦点" aria-label="发送到远程浏览器的文本" autocomplete="off"><button class="btn btn-secondary" type="submit">发送文本</button></form><span>文本发送后即清空，不记录输入内容。画面内按 Alt + Esc 返回地址栏。</span></div><div class="remote-footer"><div id="remote-status" role="status" aria-live="polite">正在连接站点浏览器…</div><div class="row"><button type="button" class="btn btn-secondary" id="remote-local-auth" hidden>使用本机浏览器授权</button><button class="btn btn-secondary" data-action="close-modal">关闭窗口</button><button class="btn btn-primary" id="remote-save" disabled>${icon('check', 16)}保存会话</button></div></div><p class="remote-note">请自行完成登录、验证码及站点内操作。保存会话不代表已验证 VIP 或付费权限；本应用不会自动付款。</p></section></div>`;
    if (onLocalAuth && !publicMode) {
      const local = select('#remote-local-auth');
      local.addEventListener('click', () => onLocalAuth(site.id), { signal });
      api.request(`/api/sites/${encodeURIComponent(site.id)}/local-auth`, { allowMissing: true, signal }).then(info => {
        if (isCurrent(signal)) local.hidden = !(info?.available || info?.active);
      }).catch(() => {}); // The embedded window is also available on older services.
    }
    const screen = select('#remote-screen');
    const toolbarFocus = () => select('#remote-address')?.focus({ preventScroll: true });
    root.querySelector('button')?.focus({ preventScroll: true });
    screen.addEventListener('click', (event) => { screen.focus(); send({ type: 'click', ...coordinates(event) }); }, { signal });
    screen.addEventListener('wheel', (event) => { event.preventDefault(); send({ type: 'scroll', deltaY: event.deltaY, ...coordinates(event) }); }, { passive: false, signal });
    screen.addEventListener('keydown', (event) => {
      if (event.altKey && event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        toolbarFocus();
        return;
      }
      if (event.isComposing || ['Shift', 'Control', 'Meta', 'Alt'].includes(event.key)) return;
      event.preventDefault();
      event.stopPropagation();
      if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) send({ type: 'text', text: event.key });
      else {
        const modifiers = [event.ctrlKey && 'Control', event.metaKey && 'Meta', event.altKey && 'Alt', event.shiftKey && 'Shift'].filter(Boolean);
        send({ type: 'key', key: [...modifiers, event.key === ' ' ? 'Space' : event.key].join('+') });
      }
    }, { signal });
    select('#remote-navigation').addEventListener('submit', async (event) => { event.preventDefault(); await send({ type: 'navigate', url: select('#remote-address').value }); if (isCurrent(signal)) screen.focus(); }, { signal });
    select('#remote-text-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const field = select('#remote-text');
      const text = field.value;
      field.value = '';
      if (text) await send({ type: 'text', text });
      if (isCurrent(signal)) screen.focus();
    }, { signal });
    select('#remote-tabs').addEventListener('change', (event) => send({ type: 'tab', tabId: event.target.value }), { signal });
    select('#remote-refresh').addEventListener('click', refresh, { signal });
    select('#remote-save').addEventListener('click', async () => {
      if (!isCurrent(signal) || saveNotice?.phase === 'saving') return;
      const button = select('#remote-save');
      const hadFocus = document.activeElement === button;
      const savePath = endpoint('/save');
      const pendingInput = inputQueue;
      button.disabled = true;
      showSaveStatus('saving', '正在等待站点请求完成并保存会话…');
      let saved = false;
      try {
        await pendingInput;
        if (!isCurrent(signal)) return;
        await api.request(savePath, { method: 'POST', signal });
        if (!isCurrent(signal)) return;
        saved = true;
        showSaveStatus('saved', '会话已保存，权限待验证。');
        await onSaved();
      } catch (error) {
        if (error.name === 'AbortError' || !isCurrent(signal)) return;
        if (saved) showSaveStatus('saved', `会话已保存，权限待验证。工作区刷新失败：${error.message}`, true);
        else showSaveStatus('error', `保存失败：${error.message}。请重试保存会话。`, true);
      } finally {
        if (isCurrent(signal) && button.isConnected) {
          button.disabled = false;
          if (hadFocus && document.activeElement === document.body) button.focus({ preventScroll: true });
        }
      }
    }, { signal });
    root.addEventListener('keydown', (event) => {
      if (event.target === screen) return;
      if (event.key === 'Escape') { onClose(); return; }
      if (event.key !== 'Tab') return;
      const controls = [...root.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), img[tabindex]')].filter((element) => !element.hidden);
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }, { signal });
    try {
      await api.request(endpoint(), { method: 'POST', signal });
      if (!isCurrent(signal)) return;
      select('#remote-save').disabled = false;
      await refresh();
    } catch (error) {
      if (error.name !== 'AbortError' && isCurrent(signal)) showStatus(error.message, true);
    }
  }
  return { open, close };
}
