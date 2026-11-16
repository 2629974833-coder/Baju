import test from 'node:test';
import assert from 'node:assert/strict';
import {connectVideoPort} from '../dist/native-video-bridge.js';

function fixture(failChunk=false) {
  const channel = new MessageChannel();
  const calls=[], received=[];
  channel.port2.onmessage = ({data}) => {
    const request = JSON.parse(data); calls.push(request);
    if(request.op==='chunk')received.push(Buffer.from(request.data,'base64'));
    channel.port2.postMessage(JSON.stringify({id:request.id,ok:!(failChunk&&request.op==='chunk'),error:'rejected'}));
  };
  const bridge=connectVideoPort(channel.port1);
  return {calls,received,bridge,close(){bridge.close();channel.port2.close();}};
}
test('the chosen webpage file is transferred in order without a picker and only finishes after every byte',async()=>{
  const f=fixture();
  try {
    const bytes=Buffer.alloc(90000);for(let i=0;i<bytes.length;i++)bytes[i]=i%251;
    assert.equal(f.calls.length,0);
    await f.bridge.shareVideo(new File([bytes],'selected-version.mp4',{type:'video/mp4'}));
    assert.equal(f.calls[0].op,'begin');assert.equal(f.calls[0].size,bytes.length);
    assert.equal(f.calls.at(-1).op,'finish');
    assert.deepEqual(Buffer.concat(f.received),bytes);
    assert.deepEqual(f.calls.filter(c=>c.op==='chunk').map(c=>c.index),[0,1,2]);
  } finally {f.close();}
});
test('cancelled or rejected transfers never request opening WeChat',async()=>{
  for(const reject of [false,true]){
    const f=fixture(reject), controller=new AbortController();
    try {
      await assert.rejects(f.bridge.shareVideo(new File([new Uint8Array(90000)],'x.mp4',{type:'video/mp4'}),{
        signal:controller.signal,onProgress:()=>controller.abort(),
      }));
      assert.ok(f.calls.some(c=>c.op==='cancel'));
      assert.equal(f.calls.some(c=>c.op==='finish'),false);
    } finally {f.close();}
  }
});
test('non-video or empty payloads never reach the native port',async()=>{
  const f=fixture();try {
    await assert.rejects(f.bridge.shareVideo(new File(['secret'],'x.txt',{type:'text/plain'})));
    await assert.rejects(f.bridge.shareVideo(new File([],'empty.mp4',{type:'video/mp4'})));
    assert.equal(f.calls.length,0);
  } finally {f.close();}
});

test('save transfers the selected webpage bytes with a save action, not a WeChat action',async()=>{
  const f=fixture();try {
    const bytes=Buffer.from('selected webpage video bytes');
    await f.bridge.saveVideo(new File([bytes],'current.mp4',{type:'video/mp4'}));
    assert.equal(f.calls[0].action,'save');
    assert.deepEqual(Buffer.concat(f.received),bytes);
    assert.equal(f.calls.at(-1).op,'finish');
  }finally{f.close();}
});
