import test from 'node:test';
import assert from 'node:assert/strict';
import { bookmarkFileHTML, cleanPanelLink } from '../dist/chunlou-bookmark-install.js';

test('copy contains only the local install page and its site identifier', () => {
  const base = 'http://127.0.0.1:4173/chunlou-connect.html';
  assert.equal(cleanPanelLink(base + '?site=legacy_2&token=secret&from=bookmark#private'), base + '?site=legacy_2');
  assert.equal(cleanPanelLink(base + '?noise=secret'), base);
  for (const url of [base + '?site=bad%2Fid', base + '?site=a&site=b',
    'https://example.org/chunlou-connect.html', 'http://user:password@localhost/chunlou-connect.html']) {
    assert.equal(cleanPanelLink(url), '');
  }
});

test('bookmark export is a single Netscape bookmark with its URL escaped as data', () => {
  const scriptURL = 'javascript:void(&quot;"<test>")';
  const html = bookmarkFileHTML(scriptURL);
  assert.match(html, /^<!DOCTYPE NETSCAPE-Bookmark-file-1>/);
  assert.match(html, /<META HTTP-EQUIV="Content-Type" CONTENT="text\/html; charset=UTF-8">/);
  assert.equal(html.match(/<DT><A /g)?.length, 1);
  assert.match(html, /HREF="javascript:void\(&amp;quot;&quot;&lt;test&gt;&quot;\)">关联春楼账号<\/A>/);
  assert.doesNotMatch(html, /<script|<test>|src=/i);
});

test('manual bookmark editing supplies only the enabled bookmark URL and clears it if import is unavailable', async () => {
  const {setupBookmarkInstall}=await import('../dist/chunlou-bookmark-install.js');
  const nodes=new Map();
  const document={getElementById(id) {
    if (!nodes.has(id)) nodes.set(id,{value:'',addEventListener(){},removeAttribute(){},setAttribute(){}});
    return nodes.get(id);
  }};
  const ui=setupBookmarkInstall({document,panelURL:'http://localhost:4173/chunlou-connect.html?site=chunlou'});
  const address=document.getElementById('bookmark-address');
  ui.enable('javascript:void(0)');
  assert.equal(address.value,'javascript:void(0)');
  ui.disable();
  assert.equal(address.value,'');
});
