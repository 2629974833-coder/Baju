import test from 'node:test';
import assert from 'node:assert/strict';
import {shareEnvironment} from '../dist/share-environment.js';

test('detects mobile, desktop, iPad desktop mode and WeChat without assuming share support', () => {
  for (const [ua, touch, platform, wechat] of [
    ['iPhone Safari', 5, '苹果手机或平板', false],
    ['Android MicroMessenger', 5, '安卓手机或平板', true],
    ['Macintosh Safari', 5, '苹果手机或平板', false],
    ['Windows NT MicroMessenger', 0, '电脑', true],
    ['Windows NT Chrome', 0, '电脑', false],
  ]) {
    const result = shareEnvironment({userAgent:ua,maxTouchPoints:touch}, true);
    assert.equal(result.platform, platform);
    assert.equal(result.wechat, wechat);
    assert.equal(result.systemShare, false);
  }
});

test('sharing requires actual browser capabilities and secure context on every platform', () => {
  const nav = {userAgent:'Android MicroMessenger',share(){},canShare(){return true;}};
  assert.equal(shareEnvironment(nav,true).systemShare,true);
  assert.equal(shareEnvironment(nav,false).systemShare,false);
  assert.equal(shareEnvironment({...nav,canShare:undefined},true).systemShare,false);
});
