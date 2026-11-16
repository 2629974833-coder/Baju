const MAX_BYTES = 128 * 1024 * 1024;

function createVideoShareFlow(wx, update) {
  let state = {busy:false, ready:false, path:'', note:'选择视频后，再点同一个按钮打开微信好友选择。', error:''};
  let alive = true;
  const publish = values => { if (alive) { state = {...state, ...values}; update({...state}); } };
  const supported = name => typeof wx[name] === 'function' && (typeof wx.canIUse !== 'function' || wx.canIUse(name));
  const cancelled = error => /cancel/i.test(error && error.errMsg || '');
  const failure = error => publish({busy:false, ...(cancelled(error) ? {} : {ready:false,path:''}), error:cancelled(error) ? '' : '微信未能完成此次操作，请重试。', note:cancelled(error) ? '已取消，没有自动发送。' : '没有确认发送成功。'});
  function chooseFromChat() {
    if (!supported('chooseMessageFile')) { publish({busy:false,error:'此环境没有可用的视频选择方式，请换到手机微信测试。'}); return; }
    wx.chooseMessageFile({count:1,type:'video',
      success:result => { const file = result.tempFiles && result.tempFiles[0]; selected(file && {...file,tempFilePath:file.path}); },fail:failure});
  }
  function selected(file) {
    if (!alive) return;
    if (!file || typeof file.tempFilePath !== 'string' || !file.tempFilePath
        || !Number.isFinite(file.size) || file.size <= 0 || file.size > MAX_BYTES) {
      publish({busy:false,error:'请选择不超过 128 MB 的有效视频文件。'});
      return;
    }
    publish({busy:false,ready:true,path:file.tempFilePath,error:'',note:'视频已准备好。点击“分享给微信好友”，再由你选择好友并确认。'});
  }
  function press() {
    if (!alive || state.busy) return;
    if (!supported('shareVideoMessage')) {
      publish({error:'此微信环境暂不支持直接转发视频。请在支持此功能的手机微信中测试；不会改为发送网页链接。'});
      return;
    }
    publish({busy:true,error:''});
    try { if (state.ready) {
      wx.shareVideoMessage({
        videoPath:state.path,
        success:() => publish({busy:false,note:'微信分享操作已返回。请以实际聊天记录为准。'}),
        fail:failure,
      });
    } else if (supported('chooseMedia')) {
      wx.chooseMedia({count:1,mediaType:['video'],sourceType:['album'],
        success:result => selected(result.tempFiles && result.tempFiles[0]),
        fail:error => {if (!alive) return; if (/not support|unsupported/i.test(error && error.errMsg || '')) { try { chooseFromChat(); } catch (e) { failure(e); } } else failure(error); }});
    } else chooseFromChat();
    } catch (error) { failure(error); }
  }
  publish({});
  return {press,dispose(){alive=false;}, snapshot(){return {...state};}};
}

module.exports = {createVideoShareFlow};
