import test from 'node:test';
import assert from 'node:assert/strict';
import * as operations from '../dist/operations-view.js';

const status={status:'ready',ready:true,uptimeSeconds:3661,database:{available:true},disk:{totalBytes:100*1024**3,usedBytes:20*1024**3,freeBytes:80*1024**3,minFreeBytes:1024**3,availableBytes:79*1024**3,healthy:true},dependencies:{ffmpeg:{available:true},ffprobe:{available:true},chromium:{available:true},pillow:{available:true}},workers:{download:{running:true,heartbeatAgeSeconds:1,healthy:true},processing:{running:true,heartbeatAgeSeconds:2,healthy:true},automation:{running:true,heartbeatAgeSeconds:1,healthy:true}},queues:{download:{running:1,queued:2,paused:3,failed:4,completed:5,needsUser:1},processing:{running:0,queued:0,failed:0},scans:{running:0,queued:0,failed:1}},recentFailures:[{id:'job<1>',type:'download',status:'failed',code:'storage-low',at:'2026-11-04'}]};

test('operations renders observable runtime and storage without claiming deployment acceptance',()=>{
 const html=operations.renderOperations(status,[]);
 assert.match(html,/运行维护/);
 assert.match(html,/运行正常/);
 assert.match(html,/自动化调度/);
 assert.match(html,/80\.0 GB/);
 assert.match(html,/1\.0 GB/);
 assert.match(html,/FFmpeg/);
 assert.match(html,/Chromium/);
 assert.match(html,/job&lt;1&gt;/);
 assert.match(html,/data-action="refresh-operations"/);
 assert.doesNotMatch(html,/生产环境已验收|服务器验收通过|undefined|NaN/);
});

test('unknown or failed operations lookup does not display healthy defaults',()=>{
 assert.match(operations.renderOperations(null,[],{loading:true}),/正在读取/);
 assert.doesNotMatch(operations.renderOperations(null),/运行正常|可用空间 0/);
 const html=operations.renderOperations(null,[],{error:'<连接失败>'});
 assert.match(html,/&lt;连接失败&gt;/);
 assert.match(html,/data-action="refresh-operations"/);
});

test('backup action is blocked by active or queued work and status remains evidence bound',()=>{
 let html=operations.renderOperations(status,[{id:'backup/1',createdAt:'2026-11-04',fileCount:6,totalBytes:2048,verified:null,verification:'not-checked',path:'backups/example'}]);
 assert.match(html,/data-action="backup-create"[^>]*disabled/);
 assert.match(html,/先暂停/);
 assert.match(html,/未复查/);
 assert.match(html,/data-action="backup-verify" data-id="backup\/1"/);
 assert.doesNotMatch(html,/恢复到当前工作区|立即覆盖/);
 html=operations.renderOperations({...status,queues:{}},[{id:'safe',verified:true,verification:'verified',fileCount:3,totalBytes:1024}]);
 assert.match(html,/data-action="backup-create"(?![^>]*disabled)/);
 assert.match(html,/校验通过/);
 assert.match(html,/新空目录/);
});

test('degraded worker and disk conditions are actionable and displayed separately',()=>{
 const html=operations.renderOperations({...status,ready:false,status:'degraded',disk:{...status.disk,healthy:false},workers:{...status.workers,processing:{running:true,healthy:false,heartbeatAgeSeconds:180}}},[]);
 assert.match(html,/需要关注/);
 assert.match(html,/容量不足|低于预留/);
 assert.match(html,/处理调度.*心跳异常/s);
});

test('missing heartbeat age stays unknown instead of looking freshly updated',()=>{
 const html=operations.renderOperations({...status,workers:{download:{healthy:false,heartbeatAgeSeconds:null},processing:{healthy:false,heartbeatAgeSeconds:null}}},[]);
 assert.match(html,/尚未收到/);
 assert.doesNotMatch(html,/0 秒前/);
});
