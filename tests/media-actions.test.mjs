import test from 'node:test';
import assert from 'node:assert/strict';
import {checkMediaFile} from '../dist/media-actions.js';
import {renderMediaMenu} from '../dist/media-menu.js';

test('saving preflight preserves media types and rejects missing files and JSON error pages', async () => {
  const calls = [];
  const result = await checkMediaFile('/api/assets/a/file', async (...args) => {
    calls.push(args);
    return new Response(null, {headers:{'content-type':'video/mp4','content-length':'500'}});
  });
  assert.equal(result.status, 200);
  assert.equal(calls[0][1].method, 'HEAD');
  assert.equal(calls[0][1].credentials, 'same-origin');
  for (const status of [401,403,404,410,500]) {
    await assert.rejects(checkMediaFile('/file', async () => new Response(null, {status})), /登录|权限|不存在|过期|暂时/);
  }
  await assert.rejects(checkMediaFile('/file', async () => new Response('{}', {headers:{'content-type':'application/json'}})), /没有返回媒体/);
  await assert.rejects(checkMediaFile('/file', async () => new Response(null, {headers:{'content-type':'video/mp4','content-length':'0'}})), /为空/);
});

test('video menu carries the selected version and escapes user content', () => {
  const html = renderMediaMenu({id:'a"<',kind:'video',status:'downloaded'}, 'v"<');
  assert.match(html, /分享给微信好友/);
  assert.match(html, /保存到手机相册/);
  assert.match(html, /无水印选项/);
  assert.doesNotMatch(html, /data-id="a"</);
  assert.match(html, /data-version="v&amp;|data-version="v&quot;/);
  assert.equal(renderMediaMenu({kind:'image',status:'downloaded'}), '');
});
