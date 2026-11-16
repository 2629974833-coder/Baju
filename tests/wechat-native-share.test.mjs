import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const module = {exports:{}};
vm.runInNewContext(readFileSync(new URL('../native/wechat-miniprogram/utils/video-share.js',import.meta.url),'utf8'), {module});
const {createVideoShareFlow} = module.exports;
function fixture() {
  const calls=[];
  const wx={canIUse:()=>true,chooseMedia:o=>calls.push(['select',o]),shareVideoMessage:o=>calls.push(['video',o])};
  const states=[];
  return {calls,wx,states,flow:createVideoShareFlow(wx,s=>states.push(s))};
}
test('one button selects and prepares without sending, then uses the actual video chat API',()=>{
  const f=fixture();f.flow.press();
  assert.equal(f.calls.length,1);assert.equal(f.calls[0][0],'select');
  f.calls[0][1].success({tempFiles:[{tempFilePath:'wxfile://local.mp4',size:100}]});
  assert.equal(f.flow.snapshot().ready,true);assert.equal(f.calls.length,1);
  f.flow.press();assert.equal(f.calls[1][0],'video');
  assert.equal(f.calls[1][1].videoPath,'wxfile://local.mp4');
  assert.equal('toUserName' in f.calls[1][1],false);
});
test('unsupported environments do not pretend a link or file attachment is a video',()=>{
  const f=fixture();f.wx.canIUse=()=>false;f.flow.press();
  assert.equal(f.calls.length,0);assert.match(f.flow.snapshot().error,/不支持/);
});
test('desktop fallback selects a video from chat and synchronous errors remain retryable',()=>{
  const f=fixture();delete f.wx.chooseMedia;
  f.wx.chooseMessageFile=o=>f.calls.push(['chat',o]);f.flow.press();
  assert.equal(f.calls[0][0],'chat');assert.equal(f.calls[0][1].type,'video');
  f.calls[0][1].success({tempFiles:[{path:'wxfile://chat.mp4',size:50}]});
  f.wx.shareVideoMessage=()=>{throw new Error('unavailable');};f.flow.press();
  assert.equal(f.flow.snapshot().busy,false);assert.equal(f.flow.snapshot().ready,false);
});
test('cancel, oversized files, double press and unload do not create unwanted sends',()=>{
  const f=fixture();f.flow.press();f.flow.press();assert.equal(f.calls.length,1);
  f.calls[0][1].fail({errMsg:'chooseMedia:fail cancel'});assert.equal(f.flow.snapshot().busy,false);
  f.flow.press();f.calls[1][1].success({tempFiles:[{tempFilePath:'wxfile://x',size:200*1024**2}]});
  assert.equal(f.flow.snapshot().ready,false);assert.match(f.flow.snapshot().error,/128/);
  f.flow.press();f.flow.dispose();f.calls[2][1].success({tempFiles:[{tempFilePath:'wxfile://x',size:100}]});
  assert.equal(f.flow.snapshot().ready,false);f.flow.press();assert.equal(f.calls.length,3);
});
