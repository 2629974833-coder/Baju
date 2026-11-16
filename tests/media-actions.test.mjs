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
    await assert.rejects(checkMediaFile('/file', async () => new Response(null, {status})), /��¼|Ȩ��|������|����|��ʱ/);
  }
  await assert.rejects(checkMediaFile('/file', async () => new Response('{}', {headers:{'content-type':'application/json'}})), /û�з���ý��/);
  await assert.rejects(checkMediaFile('/file', async () => new Response(null, {headers:{'content-type':'video/mp4','content-length':'0'}})), /Ϊ��/);
});

test('video menu carries the selected version and escapes user content', () => {
  const html = renderMediaMenu({id:'a"<',kind:'video',status:'downloaded'}, 'v"<');
  assert.match(html, /������΢�ź���/);
  assert.match(html, /���浽�ֻ����/);
  assert.match(html, /��ˮӡѡ��/);
  assert.doesNotMatch(html, /data-id="a"</);
  assert.match(html, /data-version="v&amp;|data-version="v&quot;/);
  assert.equal(renderMediaMenu({kind:'image',status:'downloaded'}), '');
});
test('older services without HEAD use a bounded range probe and the total original size', async()=>{
  const calls=[];let cancelled=false;
  const response=await checkMediaFile('/file',async(url,options)=>{
    calls.push(options);
    if(calls.length===1)return new Response(null,{status:405});
    return new Response(new ReadableStream({cancel(){cancelled=true;}}),{status:206,headers:{'content-type':'video/mp4','content-length':'1','content-range':'bytes 0-0/12345'}});
  });
  assert.equal(calls[1].headers.Range,'bytes=0-0');
  assert.equal(response.headers.get('content-length'),'12345');
  assert.equal(cancelled,true);
});

test('HEAD not-found is confirmed with GET before reporting a missing file', async()=>{
  for (const headStatus of [404,501]) {
    const calls=[];
    const response=await checkMediaFile('/file',async(url,options)=>{
      calls.push(options);
      return calls.length===1 ? new Response(null,{status:headStatus}) : new Response('v',{status:206,headers:{'content-type':'video/mp4','content-range':'bytes 0-0/100'}});
    });
    assert.equal(calls.length,2);
    assert.equal(response.headers.get('content-length'),'100');
  }
});

test('a stalled media check fails visibly instead of waiting forever',async()=>{
  await assert.rejects(checkMediaFile('/file',(_url,{signal})=>new Promise((resolve,reject)=>{
    signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')));
  }),{timeoutMs:10}),/�����Ƶ��ʱ/);
});
