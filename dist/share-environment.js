export function shareEnvironment(nav = globalThis.navigator, secure = globalThis.isSecureContext) {
  const ua = nav?.userAgent || '';
  const wechat = /MicroMessenger/i.test(ua);
  const ios = /iPhone|iPad|iPod/i.test(ua) || (/Macintosh/i.test(ua) && nav?.maxTouchPoints > 1);
  const android = /Android/i.test(ua);
  const platform = ios ? '苹果手机或平板' : android ? '安卓手机或平板' : '电脑';
  const systemShare = Boolean(secure && typeof nav?.share === 'function' && typeof nav?.canShare === 'function');
  const label = `${platform}${wechat ? ' · 微信内打开' : ' · 浏览器打开'}`;
  const fallback = wechat
    ? '当前微信窗口无法直接转发这个视频文件。请下载视频，再在微信聊天中选择该视频发送。公开临时工作区不能直接换浏览器继续，请先保存文件。'
    : ios ? '请保存视频，在手机的系统分享窗口选择微信，或从微信相册中选取视频。'
      : android ? '请下载视频，再从手机相册的分享菜单选择微信，或在微信聊天中选择该视频。'
        : '请下载视频，再把下载好的视频拖入微信聊天窗口，确认后自行发送。';
  return {platform, wechat, ios, android, label, systemShare, fallback};
}
