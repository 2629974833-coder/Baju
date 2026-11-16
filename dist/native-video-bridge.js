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
    reply.ok ? request.resolve(reply) : request.reject(new Error(reply.error || 'Ӧ��δ�ܽ�����Ƶ��'));
  };
  port.start?.();
  function call(op, values = {}) {
    if (closed) return Promise.reject(new Error('Ӧ�������ѹرգ���ˢ��ҳ�档'));
    const id = String(++serial);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error('Ӧ����Ӧ��ʱ�������ԡ�')); }, 30000);
      pending.set(id, {resolve,reject,timer});
      try { port.postMessage(JSON.stringify({id,op,...values})); }
      catch (error) { clearTimeout(timer); pending.delete(id); reject(error); }
    });
  }
  const bridge = {
    shareVideo(file, options) { return bridge.transferVideo(file, 'share', options); },
    saveVideo(file, options) { return bridge.transferVideo(file, 'save', options); },
    async transferVideo(file, action, {signal, onProgress = () => {}} = {}) {
      if (file.type !== 'video/mp4' || file.size <= 0 || file.size > 128 * 1024**2) throw new Error('Ӧ��Ŀǰ���ղ�����Լ 134 MB �ĳ�����Ƶ��ʽ����ѡ����ݵ���Ƶ�汾��');
      const check = () => { if (signal?.aborted) throw new DOMException('��ȡ��','AbortError'); };
      check();
      await call('begin', {size:file.size,mime:file.type,action});
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
    close() { closed = true; port.close(); for (const request of pending.values()) {clearTimeout(request.timer);request.reject(new Error('��ҳ���뿪��������ȡ����'));} pending.clear(); },
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
