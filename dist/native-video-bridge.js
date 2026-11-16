let activeBridge = null;

export function connectVideoPort(port) {
  let serial = 0, closed = false;
  const pending = new Map();
  port.onmessage = event => {
    let reply;
    try { reply = JSON.parse(event.data); } catch { return; }
    const request = pending.get(reply.id);
    if (!request) return;
    pending.delete(reply.id); clearTimeout(request.timer);
    reply.ok ? request.resolve(reply) : request.reject(new Error(reply.error || '应用未能接收视频。'));
  };
  port.start?.();
  function call(op, values = {}) {
    if (closed) return Promise.reject(new Error('应用连接已关闭，请刷新页面。'));
    const id = String(++serial);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error('应用响应超时，请重试。')); }, 30000);
      pending.set(id, {resolve,reject,timer});
      try { port.postMessage(JSON.stringify({id,op,...values})); }
      catch (error) { clearTimeout(timer); pending.delete(id); reject(error); }
    });
  }
  const bridge = {
    async shareVideo(file, {signal, onProgress = () => {}} = {}) {
      if (file.type !== 'video/mp4' || file.size <= 0 || file.size > 128 * 1024**2) throw new Error('应用目前接收不超过约 134 MB 的常见视频格式，请选择兼容的视频版本。');
      const check = () => { if (signal?.aborted) throw new DOMException('已取消','AbortError'); };
      check();
      await call('begin', {size:file.size,mime:file.type});
      try {
        let index = 0;
        for (let offset = 0; offset < file.size; offset += 32768) {
          check();
          const bytes = new Uint8Array(await file.slice(offset, offset + 32768).arrayBuffer());
          let binary = '';
          for (const byte of bytes) binary += String.fromCharCode(byte);
          await call('chunk', {index:index++,data:btoa(binary)});
          onProgress(Math.min(file.size,offset+bytes.length),file.size);
        }
        check();
        return await call('finish');
      } catch (error) { await call('cancel').catch(() => {}); throw error; }
    },
    close() { closed = true; port.close(); for (const request of pending.values()) {clearTimeout(request.timer);request.reject(new Error('网页已离开，分享已取消。'));} pending.clear(); },
  };
  return bridge;
}

export const getNativeVideoBridge = () => activeBridge;
if (typeof window !== 'undefined') {
  window.addEventListener('message', event => {
    if (window !== window.top || event.source !== null || !['','null'].includes(event.origin) || event.data !== 'framekeep-native-v1' || event.ports?.length !== 1) return;
    activeBridge?.close();
    activeBridge = connectVideoPort(event.ports[0]);
    window.dispatchEvent(new Event('framekeep-native-ready'));
  });
  window.addEventListener('pagehide',()=>{activeBridge?.close();activeBridge=null;});
}
