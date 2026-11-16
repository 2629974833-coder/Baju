const {createVideoShareFlow} = require('../../utils/video-share');
Page({
  data:{busy:false,ready:false,path:'',note:'',error:''},
  onLoad() { this.flow = createVideoShareFlow(wx, state => this.setData(state)); },
  onSharePress() { this.flow.press(); },
  onUnload() { this.flow.dispose(); },
});
