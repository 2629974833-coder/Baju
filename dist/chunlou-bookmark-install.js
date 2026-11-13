const bookmarkName = '关联春楼账号';

export function cleanPanelLink(value) {
  try {
    const source = new URL(value);
    const ids = source.searchParams.getAll('site');
    if (!['http:', 'https:'].includes(source.protocol) || source.username || source.password ||
        !['127.0.0.1', 'localhost', '[::1]'].includes(source.hostname) ||
        source.pathname !== '/chunlou-connect.html' || ids.length > 1 ||
        (ids.length === 1 && !/^[a-z0-9_-]{1,128}$/i.test(ids[0]))) return '';
    const link = new URL('/chunlou-connect.html', source.origin);
    if (ids.length) link.searchParams.set('site', ids[0]);
    return link.href;
  } catch {
    return '';
  }
}

function safeHref(value) {
  return value.replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;').replaceAll('>','&gt;');
}

export function bookmarkFileHTML(scriptURL) {
  if (!scriptURL.startsWith('javascript:')) throw new Error('书签地址无效');
  return `<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>拾帧书签</TITLE>
<H1>拾帧书签</H1>
<DL><p>
    <DT><A HREF="${safeHref(scriptURL)}">${bookmarkName}</A>
</DL><p>
`;
}

export function setupBookmarkInstall({document, panelURL}) {
  const elem = id => document.getElementById(id);
  const bookmark = elem('bookmark');
  const download = elem('bookmark-file');
  const help = elem('bookmark-help');
  const copy = elem('copy-panel-link');
  const field = elem('panel-link');
  const address = elem('bookmark-address');
  const cleanLink = cleanPanelLink(panelURL);
  let fileURL = '';

  function disable() {
    if (fileURL) URL.revokeObjectURL(fileURL);
    fileURL = '';
    address.value = '';
    for (const link of [bookmark, download]) {
      link.removeAttribute('href');
      link.setAttribute('aria-disabled', 'true');
    }
    help.textContent = '';
  }

  function enable(scriptURL) {
    disable();
    if (!cleanLink) return;
    const file = new Blob([bookmarkFileHTML(scriptURL)], {type:'text/html;charset=utf-8'});
    fileURL = URL.createObjectURL(file);
    bookmark.href = scriptURL;
    address.value = scriptURL;
    download.href = fileURL;
    for (const link of [bookmark, download]) link.removeAttribute('aria-disabled');
  }

  bookmark.addEventListener('click', event => {
    event.preventDefault();
    if (!fileURL) return;
    help.textContent = '请按住虚线框，把它拖到浏览器顶部的书签栏后松开。也可以点击旁边的「下载书签文件」，通过浏览器菜单导入。';
  });
  bookmark.addEventListener('dragstart', event => {
    if (!fileURL || !event.dataTransfer) { event.preventDefault(); return; }
    const url = bookmark.href;
    event.dataTransfer.setData('text/uri-list', url);
    event.dataTransfer.setData('text/html', `<a href="${safeHref(url)}">${bookmarkName}</a>`);
    event.dataTransfer.setData('text/x-moz-url', url + '\n' + bookmarkName);
    event.dataTransfer.effectAllowed = 'copyLink';
  });
  download.addEventListener('click', () => {
    if (!fileURL) return;
    elem('import-steps').open = true;
    help.textContent = '下载后，在这个浏览器的菜单中导入书签文件。';
  });

  copy.disabled = !cleanLink;
  field.value = cleanLink;
  copy.addEventListener('click', async () => {
    if (!cleanLink) return;
    const feedback = elem('copy-panel-feedback');
    try {
      await document.defaultView.navigator.clipboard.writeText(cleanLink);
      field.hidden = true;
      feedback.textContent = '已复制本页地址，请在已登录春楼的浏览器中打开。';
    } catch {
      field.hidden = false;
      field.focus();
      field.select();
      feedback.textContent = '请复制下面选中的地址，在已登录春楼的浏览器中打开。';
    }
  });
  disable();
  return {enable, disable};
}
