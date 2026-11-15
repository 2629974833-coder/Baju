import {escapeHtml as esc} from './ui.js';
import {versionFileUrl, processedVersions} from './version-view.js';

const SHARE_LIMIT = 128 * 1024 ** 2;
const saveErrors = {
  401: '登录已失效，请刷新页面并重新登录后保存。',
  403: '没有保存此文件的权限，请在原工作区重试。',
  404: '服务器上的原文件不存在或已被清理，请重新下载该视频。',
  410: '文件已过期，请重新下载。',
};

export async function checkMediaFile(url, fetcher = fetch) {
  const response = await fetcher(url, {method: 'HEAD', credentials: 'same-origin', cache: 'no-store'});
  if (!response.ok) throw new Error(saveErrors[response.status] || `暂时无法保存文件（状态码 ${response.status}），请稍后重试。`);
  const type = response.headers.get('content-type') || '';
  if (!/^(video\/|audio\/|image\/|application\/octet-stream)/i.test(type)) {
    throw new Error('服务器没有返回媒体文件，请刷新页面后重试。');
  }
  if (response.headers.get('content-length') === '0') throw new Error('服务器上的文件为空，请重新下载。');
  return response;
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
    let requestController = null, file = null, busy = false;
    const versions = processedVersions(asset).filter(v => ['remove', 'both'].includes(v.options?.mode || v.mode));
    const clean = asset.shortMedia?.watermark === 'source-clean';
    const dialog = document.createElement('dialog');
    currentDialog = dialog;
    dialog.className = 'media-action-dialog';
    const title = intent === 'share' ? '分享给微信好友' : intent === 'save' ? '保存到手机相册' : '无水印选项';
    dialog.innerHTML = `<header><div><small>视频文件</small><h2>${title}</h2></div><button type="button" class="icon-button" data-close aria-label="关闭视频操作">×</button></header>
      <div class="media-action-body"><p class="media-action-title">${esc(asset.title)}</p>
      <label for="media-file-choice">选择要发送或保存的视频</label><select id="media-file-choice" class="select"><option value="original">${clean ? '源站无水印原视频' : '原始视频 · 水印状态待确认'}</option>${clean ? '' : '<option disabled>源站无水印原视频 · 暂未获取</option>'}${versions.map((v, i) => `<option value="${esc(v.id)}">去水印版本 ${i + 1} · 请检查处理效果</option>`).join('')}</select>
      <div class="media-watermark-note">${clean ? '来源标记为无水印版本，发送前仍建议播放检查。' : '原视频可能带水印。处理后会生成新版本，复杂背景可能留痕。'}</div>
      <button type="button" class="btn btn-secondary" data-process ${canProcess() ? '' : 'disabled'}>处理已有水印</button>${canProcess() ? '' : '<p class="hint">当前公开模式未开放水印处理，请在个人工作区使用。</p>'}
      <p class="media-action-status" role="status" aria-live="polite"></p>
      <div class="media-help">${intent === 'share' ? '支持时会打开系统分享窗口，请自行选择微信好友。不会自动发送，也不会把私人下载地址分享出去。' : '苹果手机：可在系统分享窗口选择“存储视频”。安卓手机：下载后可从相册或文件管理中打开。微信内不能直接保存时，请用手机浏览器打开同一工作区后下载。'}</div></div>
      <footer><button class="btn btn-secondary" data-download>下载视频文件</button><button class="btn btn-primary" data-share>准备视频文件</button></footer>`;
    const choice = dialog.querySelector('select');
    if ([...choice.options].some(o => o.value === preferred)) choice.value = preferred;
    const status = dialog.querySelector('[role=status]');
    const share = dialog.querySelector('[data-share]');
    const download = dialog.querySelector('[data-download]');
    const say = text => { status.textContent = text; };
    const reset = () => { requestController?.abort(); requestController = null; busy = false; file = null; share.disabled = false; share.textContent = '准备视频文件'; say(''); };
    choice.addEventListener('change', reset);
    dialog.addEventListener('close', () => { requestController?.abort(); file = null; dialog.remove(); if (currentDialog === dialog) currentDialog = null; });
    dialog.querySelector('[data-close]').onclick = () => dialog.close();
    dialog.querySelector('[data-process]').onclick = () => { dialog.close(); onProcess(asset.id); };
    download.onclick = async () => {
      const url = versionFileUrl(asset, choice.value);
      download.disabled = true;
      try { await checkMediaFile(url + '?download=true'); if (dialog.open) { saveMediaFile(url + '?download=true'); say('已交给浏览器下载，请查看下载记录；完成后可在微信选择该视频发送。'); } }
      catch (error) { if (dialog.open) say(error.message); }
      finally { download.disabled = false; }
    };
    // Preparing and sharing use separate clicks so the share call retains a user gesture.
    share.onclick = async () => {
      if (busy) return;
      if (file) {
        try { await navigator.share({files: [file], title: asset.title}); say('已交给系统分享窗口，请自行完成选择；网页无法确认是否已发送或存入相册。'); }
        catch (error) { say(error.name === 'AbortError' ? '已取消分享，可重新选择。' : '系统未能分享此文件，请下载后从微信或相册中打开。'); }
        return;
      }
      const url = versionFileUrl(asset, choice.value);
      if (!globalThis.isSecureContext || !navigator.share || !navigator.canShare) { say('此浏览器不支持分享视频文件。请点“下载视频文件”，保存后从微信发送或移入相册。'); return; }
      const controller = requestController = new AbortController();
      busy = true; share.disabled = true; say('正在准备视频文件…');
      try {
        const head = await checkMediaFile(url);
        const size = Number(head.headers.get('content-length'));
        if (!Number.isFinite(size) || size <= 0 || size > SHARE_LIMIT) throw new Error('此视频不适合直接分享，请下载后从微信发送。');
        const response = await fetch(url, {credentials: 'same-origin', signal: controller.signal, cache: 'no-store'});
        if (!response.ok || !/^video\//i.test(response.headers.get('content-type') || '')) throw new Error('未能读取视频文件，请尝试下载。');
        const reader = response.body.getReader(), chunks = [];
        let total = 0;
        while (true) {
          const {done, value} = await reader.read();
          if (done) break;
          total += value.byteLength;
          if (total > SHARE_LIMIT) { await reader.cancel(); throw new Error('视频过大，请下载后从微信发送。'); }
          chunks.push(value);
        }
        if (total !== size || total === 0) throw new Error('视频传输不完整，请重新准备。');
        if (controller.signal.aborted || !dialog.open) return;
        const type = response.headers.get('content-type').split(';')[0];
        const extension = type === 'video/mp4' ? 'mp4' : type === 'video/webm' ? 'webm' : 'mov';
        const candidate = new File(chunks, `${asset.title.replace(/[\\/:*?"<>|]/g, '_').slice(0, 80)}.${extension}`, {type});
        if (!navigator.canShare({files: [candidate]})) throw new Error('此浏览器不能分享该视频格式，请下载后从微信发送。');
        file = candidate;
        share.textContent = intent === 'save' ? '打开系统保存选项' : '选择微信好友';
        say('视频已准备好。点击下方按钮，在系统窗口中完成操作。');
      } catch (error) { if (!controller.signal.aborted && dialog.open) say(error.message); }
      finally { if (requestController === controller) { busy = false; share.disabled = false; } }
    };
    document.body.append(dialog);
    dialog.showModal();
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
    try { await checkMediaFile(link.href); saveMediaFile(link.href, link.download); }
    catch (error) { toast(error.message, true); }
    finally { delete link.dataset.saving; }
  }, true);
  return {open, close};
}
