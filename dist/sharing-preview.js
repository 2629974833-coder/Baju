import {createApiClient} from './real-api.js';
import {createSessionPanel} from './real-session.js';
import {renderMediaMenu} from './media-menu.js';
import {bindMediaActions, checkMediaFile} from './media-actions.js';
const api = createApiClient();
const toast = text => {const el=document.querySelector('#message');el.textContent=text;el.className='preview-toast';el.hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>{el.hidden=true;},8000);};
try {
  const state = await api.request('/api/state');
  const asset = state.assets.find(a=>a.id==='preview_video');
  if (!asset) throw new Error('请先使用本地预览启动方式准备示例视频。');
  document.querySelector('#preview-video').innerHTML = `<video controls playsinline controlslist="nodownload noplaybackrate noremoteplayback" disablepictureinpicture disableremoteplayback preload="metadata" poster="/assets/mountain.jpg" src="/api/assets/preview_video/file" aria-label="本地示例视频"></video>${renderMediaMenu(asset)}`;
  document.querySelector('#load-status').textContent='已就绪 · 请点击视频右下角「⋯」';
  bindMediaActions({getAsset:id=>state.assets.find(a=>a.id===id),canProcess:()=>true,onProcess:id=>{location.href=`/?processAsset=${encodeURIComponent(id)}#process`;},toast});
  const panel = createSessionPanel({api,root:document.querySelector('#overlay-root'),onClose:()=>{panel.close();document.querySelector('#overlay-root').innerHTML='';},onSaved:()=>toast('测试会话已保存。')});
  document.querySelector('#test-drag').onclick=()=>panel.open(state.sites.find(s=>s.id==='preview'));
  document.addEventListener('click',event=>{if(event.target.closest('[data-action="close-modal"]')){panel.close();document.querySelector('#overlay-root').innerHTML='';}});
  document.querySelector('#test-missing').onclick=async()=>{try{await checkMediaFile('/api/assets/missing/file?download=true');}catch(error){toast(error.message);}};
} catch(error) {document.querySelector('#load-status').textContent=error.message;}
