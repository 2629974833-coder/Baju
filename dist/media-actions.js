import {escapeHtml as esc} from './ui.js';
import {versionFileUrl, processedVersions} from './version-view.js';
import {shareEnvironment} from './share-environment.js';
import {getNativeVideoBridge} from './native-video-bridge.js';

const SHARE_LIMIT = 128 * 1024 ** 2;
const saveErrors = {
  401: '��¼��ʧЧ����ˢ��ҳ�沢���µ�¼�󱣴档',
  403: 'û�б�����ļ���Ȩ�ޣ�����ԭ���������ԡ�',
  404: '�������ϵ�ԭ�ļ������ڻ��ѱ����������������ظ���Ƶ��',
  410: '�ļ��ѹ��ڣ����������ء�',
};

export async function checkMediaFile(url, fetcher = fetch, {signal, timeoutMs = 20000} = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener('abort', abort, {once:true});
  if (signal?.aborted) abort();
  const timer = setTimeout(abort, timeoutMs);
  try {
  let response = await fetcher(url, {method: 'HEAD', credentials: 'same-origin', cache: 'no-store', signal:controller.signal});
  // Older Baju services only expose GET on media routes.
  if ([404, 405, 501].includes(response.status)) {
    response = await fetcher(url, {headers:{Range:'bytes=0-0'},credentials:'same-origin',cache:'no-store',signal:controller.signal});
    const headers = new Headers(response.headers);
    const total = /\/(\d+)$/.exec(headers.get('content-range') || '')?.[1];
    if (total) headers.set('content-length',total);
    await response.body?.cancel();
    response = new Response(null,{status:response.status,headers});
  }
  if (!response.ok) throw new Error(saveErrors[response.status] || `��ʱ�޷������ļ���״̬�� ${response.status}�������Ժ����ԡ�`);
  const type = response.headers.get('content-type') || '';
  if (!/^(video\/|audio\/|image\/|application\/octet-stream)/i.test(type)) {
    throw new Error('������û�з���ý���ļ�����ˢ��ҳ������ԡ�');
  }
  if (response.headers.get('content-length') === '0') throw new Error('�������ϵ��ļ�Ϊ�գ����������ء�');
  return response;
  } catch (error) {
    if (controller.signal.aborted && !signal?.aborted) throw new Error('�����Ƶ��ʱ��������������ԡ�');
    throw error;
  } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort); }
}

export function saveMediaFile(url, name = '') {
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.dataset.verifiedSave = 'true';
  document.body.append(link);
  link.click();
  link.remove();
}

export function bindMediaActions({getAsset, canProcess, onProcess, toast}) {
  let currentDialog = null;
  const close = () => { currentDialog?.close(); currentDialog?.remove(); currentDialog = null; };
  function open(asset, intent, preferred = 'original') {
    close();
    const environment = shareEnvironment();
    let requestController = null, file = null, busy = false;
    const versions = processedVersions(asset).filter(v => ['remove', 'both'].includes(v.options?.mode || v.mode));
    const clean = asset.shortMedia?.watermark === 'source-clean';
    const dialog = document.createElement('dialog');
    currentDialog = dialog;
    dialog.className = 'media-action-dialog';
    const title = intent === 'share' ? '������΢�ź���' : intent === 'save' ? '���浽�ֻ����' : '��ˮӡѡ��';
    dialog.innerHTML = `<header><div><small>��Ƶ�ļ�</small><h2>${title}</h2></div><button type="button" class="icon-button" data-close aria-label="�ر���Ƶ����">��</button></header>
      <div class="media-action-body"><p class="media-action-title">${esc(asset.title)}</p>
      <label for="media-file-choice">ѡ��Ҫ���ͻ򱣴����Ƶ</label><select id="media-file-choice" class="select"><option value="original">${clean ? 'Դվ��ˮӡԭ��Ƶ' : 'ԭʼ��Ƶ �� ˮӡ״̬��ȷ��'}</option>${clean ? '' : '<option disabled>Դվ��ˮӡԭ��Ƶ �� ��δ��ȡ</option>'}${versions.map((v, i) => `<option value="${esc(v.id)}">ȥˮӡ�汾 ${i + 1} �� ���鴦��Ч��</option>`).join('')}</select>
      <div class="media-watermark-note">${clean ? '��Դ���Ϊ��ˮӡ�汾������ǰ�Խ��鲥�ż�顣' : 'ԭ��Ƶ���ܴ�ˮӡ��������������°汾�����ӱ����������ۡ�'}</div>
      <button type="button" class="btn btn-secondary" data-process ${canProcess() ? '' : 'disabled'}>��������ˮӡ</button>${canProcess() ? '' : '<p class="hint">��ǰ����ģʽδ����ˮӡ���������ڸ��˹�����ʹ�á�</p>'}
      <p class="media-action-status" role="status" aria-live="polite"></p>
      <div class="media-help">${intent === 'share' ? '֧��ʱ���ϵͳ�������ڣ�������ѡ��΢�ź��ѡ������Զ����ͣ�Ҳ�����˽�����ص�ַ������ȥ��' : 'ƻ���ֻ�������ϵͳ��������ѡ�񡰴洢��Ƶ������׿�ֻ������غ�ɴ������ļ������д򿪡�΢���ڲ���ֱ�ӱ���ʱ�������ֻ��������ͬһ�����������ء�'}</div></div>
      <footer><p class="media-action-feedback" role="alert"></p><button class="btn btn-secondary" data-download ${intent === 'share' ? 'hidden' : ''}>������Ƶ�ļ�</button><button class="btn btn-primary" data-share>${intent === 'share' ? '������΢�ź���' : '׼����Ƶ�ļ�'}</button></footer>`;
    const choice = dialog.querySelector('select');
    if ([...choice.options].some(o => o.value === preferred)) choice.value = preferred;
    const status = dialog.querySelector('[role=status]');
    const share = dialog.querySelector('[data-share]');
    const download = dialog.querySelector('[data-download]');
    const say = text => { status.textContent = text; dialog.querySelector('[role=alert]').textContent = text; };
    const reset = () => { requestController?.abort(); requestController = null; busy = false; file = null; share.disabled = false; share.textContent = intent === 'share' ? '������΢�ź���' : '׼����Ƶ�ļ�'; say(''); };
    choice.addEventListener('change', () => { reset(); if (intent === 'share') void prepare(); });
    dialog.addEventListener('close', () => { requestController?.abort(); file = null; dialog.remove(); if (currentDialog === dialog) currentDialog = null; });
    dialog.querySelector('[data-close]').onclick = () => dialog.close();
    dialog.querySelector('[data-process]').onclick = () => { dialog.close(); onProcess(asset.id); };
    download.onclick = async () => {
      if (busy) return false;
      if (getNativeVideoBridge()) {
        if (!file) await prepare();
        if (!file || !dialog.open) return false;
        await transferNative('save');
        return true;
      }
      const url = versionFileUrl(asset, choice.value);
      download.disabled = true; say('���ڼ����Ƶ�ļ���');
      try { await checkMediaFile(url + '?download=true'); if (dialog.open) { saveMediaFile(url + '?download=true'); say('�ѽ�����������أ���鿴���ؼ�¼����ɺ����΢��ѡ�����Ƶ���͡�'); return true; } }
      catch (error) { if (dialog.open) say(error.message); return false; }
      finally { download.disabled = false; }
    };
    async function transferNative(action) {
      const native = getNativeVideoBridge();
      busy = true; share.disabled = true; download.disabled = true;
      const controller = requestController = new AbortController();
      try {
        const result = await native[action === 'save' ? 'saveVideo' : 'shareVideo'](file, {
          signal: controller.signal,
          onProgress: (done, total) => say(`����${action === 'save' ? '����' : '����'}��ǰ��ҳ��Ƶ��${Math.round(done / total * 100)}%`),
        });
        if (dialog.open) say(result.message || (action === 'save' ? '��Ƶ�ѱ��浽�ֻ���ᡣ' : '������΢�Ž��յ�ǰ��ҳ��Ƶ��������ѡ����Ѳ�ȷ�ϡ�'));
      } catch (error) { if (dialog.open) say(error.name === 'AbortError' ? '��ȡ����' : error.message); }
      finally { if (requestController === controller) { busy = false; share.disabled = false; download.disabled = false; } }
    }
    // File preparation happens before the click; only a real click invokes system sharing.
    share.onclick = async () => {
      if (busy) return;
      if (file) {
        const native = getNativeVideoBridge();
        if (native && intent === 'share') {
          await transferNative('share');
          return;
        }
        if (native && intent === 'save') {
          await transferNative('save');
          return;
        }
        try { await navigator.share({files: [file], title: asset.title}); say('�ѽ���ϵͳ�������ڣ����������ѡ����ҳ�޷�ȷ���Ƿ��ѷ��ͻ������ᡣ'); }
        catch (error) { say(error.name === 'AbortError' ? '��ȡ��������������ѡ��' : 'ϵͳδ�ܷ������ļ��������غ��΢�Ż�����д򿪡�'); }
        return;
      }
      if (intent === 'share' && !getNativeVideoBridge() && !environment.systemShare) { const requested = await download.onclick(); if (requested && dialog.open) say(`�����������������Ƶ����δ��΢�š�${environment.fallback}`); return; }
      await prepare();
      if (file && getNativeVideoBridge() && dialog.open) await transferNative(intent === 'save' ? 'save' : 'share');
    };
    async function prepare() {
      const url = versionFileUrl(asset, choice.value);
      if (!environment.systemShare && !getNativeVideoBridge()) { say(`��ʶ��${environment.label}���޷�ֱ�ӵ����ļ�������${environment.fallback}`); return; }
      const controller = requestController = new AbortController();
      let timedOut = false;
      const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 120000);
      busy = true; share.disabled = true; download.disabled = true; say('����׼����Ƶ�ļ���');
      try {
        const head = await checkMediaFile(url, fetch, {signal:controller.signal});
        const size = Number(head.headers.get('content-length'));
        if (size > SHARE_LIMIT) throw new Error('����Ƶ�����ݲ�֧��ֱ�ӷ����򱣴浽��ᡣ');
        const response = await fetch(url, {credentials: 'same-origin', signal: controller.signal, cache: 'no-store'});
        if (!response.ok || !/^video\//i.test(response.headers.get('content-type') || '')) throw new Error('δ�ܶ�ȡ��Ƶ�ļ����볢�����ء�');
        const reader = response.body.getReader(), chunks = [];
        let total = 0;
        while (true) {
          const {done, value} = await reader.read();
          if (done) break;
          total += value.byteLength;
          if (total > SHARE_LIMIT) { await reader.cancel(); throw new Error('��Ƶ���������غ��΢�ŷ��͡�'); }
          chunks.push(value);
          say(size > 0 ? `���ڶ�ȡ��Ƶ��${Math.min(100, Math.round(total / size * 100))}%` : `���ڶ�ȡ��Ƶ��${(total / 1024 ** 2).toFixed(1)} MB`);
        }
        if ((size > 0 && total !== size) || total === 0) throw new Error('��Ƶ���䲻������������׼����');
        if (controller.signal.aborted || !dialog.open) return;
        const type = response.headers.get('content-type').split(';')[0];
        const extension = type === 'video/mp4' ? 'mp4' : type === 'video/webm' ? 'webm' : 'mov';
        const candidate = new File(chunks, `${asset.title.replace(/[\\/:*?"<>|]/g, '_').slice(0, 80)}.${extension}`, {type});
        if (!getNativeVideoBridge() && !navigator.canShare({files: [candidate]})) throw new Error('����������ܷ�������Ƶ��ʽ�������غ��΢�ŷ��͡�');
        file = candidate;
        share.textContent = intent === 'share' ? '������΢�ź���' : intent === 'save' ? '��ϵͳ����ѡ��' : '��ϵͳ����';
        say(getNativeVideoBridge() ? '�����Ӱ�׿Ӧ�á���ǰ��ҳ��Ƶ��׼���ã����������ֱ�ӽ���΢�ţ�������������ѡ��' : `��ʶ��${environment.label}����Ƶ��׼���ã������ť����ϵͳ����������ѡ��΢�š��ܷ��г�΢���ɵ�ǰ�豸������`);
      } catch (error) { if (dialog.open && (timedOut || !controller.signal.aborted)) say(timedOut ? '��ȡ��Ƶ��ʱ��������������ԡ�' : `${error.message} ${intent === 'share' && !getNativeVideoBridge() ? environment.fallback : ''}`); }
      finally { clearTimeout(timer); if (requestController === controller) { busy = false; share.disabled = false; download.disabled = false; } }
    };
    document.body.append(dialog);
    dialog.showModal();
    if (intent === 'share') void prepare();
    const nativeReady = () => {if(dialog.open && intent === 'share' && !busy && !file)void prepare();};
    window.addEventListener('framekeep-native-ready',nativeReady);
    dialog.addEventListener('close',()=>window.removeEventListener('framekeep-native-ready',nativeReady));
  }
  document.addEventListener('click', async event => {
    const action = event.target.closest('[data-action^="media-"]');
    if (action) {
      event.preventDefault();
      action.closest('details')?.removeAttribute('open');
      const asset = getAsset(action.dataset.id);
      if (asset) open(asset, action.dataset.action.replace('media-', ''), action.dataset.version);
      return;
    }
    const link = event.target.closest('a[download]:not([data-verified-save])');
    if (!link || new URL(link.href).origin !== location.origin || !/\/api\/assets\/[^/]+\/(?:versions\/[^/]+\/)?file$/.test(new URL(link.href).pathname)) return;
    event.preventDefault();
    if (link.dataset.saving) return;
    link.dataset.saving = 'true';
    try {
      const native = getNativeVideoBridge();
      const match = /\/api\/assets\/([^/]+)\/(?:versions\/([^/]+)\/)?file$/.exec(new URL(link.href).pathname);
      const asset = match && getAsset(decodeURIComponent(match[1]));
      if (native && asset?.kind === 'video') { open(asset, 'save', match[2] ? decodeURIComponent(match[2]) : 'original'); return; }
      await checkMediaFile(link.href); saveMediaFile(link.href, link.download);
    }
    catch (error) { toast(error.message, true); }
    finally { delete link.dataset.saving; }
  }, true);
  return {open, close};
}
