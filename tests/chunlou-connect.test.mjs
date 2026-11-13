import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { association, bookmarkURL, panelOrigin, siteIdFromURL } from '../dist/chunlou-connect.js';

const source = 'https://chunlou.tv';
const panel = 'http://127.0.0.1:4173';
const channel = 'framekeep.chunlou.account';
const tokenKey = 'sb-grzjuztzhrexhvmetnvd-auth-token';
const allTokenKey = 'sb-grzjuztzhrexhvmetnvd-all-auth-token';
const siteNonce = 'e45801bc-5e52-49e7-bb3e-46ce17cf0431';

test('allows the panel on exact loopback hosts and rejects spoofed site selectors', () => {
  for (const origin of [panel, 'http://localhost:8080','https://[::1]']) assert.ok(panelOrigin(origin));
  for (const origin of ['https://example.com', 'http://localhost.example.com', 'http://127.0.0.2', 'file:///tmp/test', 'http://user@127.0.0.1']) assert.equal(panelOrigin(origin), false);
  assert.equal(siteIdFromURL(panel), 'chunlou');
  assert.equal(siteIdFromURL(panel+'/?site=site_beef'), 'site_beef');
  assert.equal(siteIdFromURL(panel+'/?site=..%2Fother'), null);
  assert.throws(()=>bookmarkURL(panel, '../my-site'));
});

function harness() {
  const messages = [], states = [], saves = [];
  let now = Date.now();
  const popup = {closed:false, postMessage(data, origin){messages.push({data,origin});}};
  const ctl = association({openWindow:()=>popup, now:()=>now, report:(phase)=>states.push(phase),
    save:async data=>{saves.push(data);return {saved:true,accountVerified:true};}});
  const receive = (type, fields={}, origin=source, from=popup) => ctl.receive({
    origin,source:from,data:{channel,type,siteNonce,...fields}
  });
  return {messages,states,saves,popup,ctl,receive,advance:ms=>{now+=ms;}};
}

test('handoff binds the source window and nonce and only saves once after local confirmation', async () => {
  const {messages,states,saves,ctl,receive} = harness();
  ctl.open();
  await receive('hello',{},'https://www.chunlou.tv');
  await receive('hello',{},source,{});
  assert.equal(messages.length,0);
  await receive('hello');
  const connect = messages.at(-1);
  assert.equal(connect.origin,source);
  assert.equal(connect.data.type,'pair');
  const nonce = connect.data.nonce;
  const account={session:'{}',storageKey:tokenKey,nonce};
  await receive('ready',{nonce});
  await receive('account',account);
  assert.deepEqual(saves,[]);
  assert.equal(states.at(-1),'ready');
  ctl.confirm();ctl.confirm();
  assert.equal(messages.filter(m=>m.data.type==='read').length,1);
  await receive('account',{...account,nonce:'different'});
  await receive('account',{...account,siteNonce:'other'});
  await receive('account',account,'https://elsewhere.example');
  assert.equal(saves.length,0);
  await Promise.all([receive('account',account),receive('account',account)]);
  assert.equal(saves.length,1);
  assert.deepEqual(saves[0],{storageKey:tokenKey,session:'{}'});
  assert.equal(states.at(-1),'saved');
});

test('timeout, wrong storage key, closed popups and restarted rounds reject old replies', async () => {
  const {messages,states,saves,popup,ctl,receive,advance} = harness();
  ctl.open();
  await receive('hello');
  const nonce=messages.at(-1).data.nonce;
  await receive('ready',{nonce}); ctl.confirm();
  await receive('account',{nonce,storageKey:'other',session:'{}'});
  assert.equal(saves.length,0);
  ctl.open();
  await receive('hello');
  assert.notEqual(messages.at(-1).data.nonce,nonce);
  advance(15*60_000);
  await receive('ready',{nonce:messages.at(-1).data.nonce});
  assert.equal(states.at(-1),'error');
  ctl.open();
  popup.closed=true;
  ctl.tick();
  assert.equal(states.at(-1),'error');
});

function sourceBookmark(getItem) {
  const messages=[], reads=[], events=new Map();
  const opener={closed:false,postMessage:(data,origin)=>messages.push({data,origin})};
  let opened;
  const w={opener:null,location:{origin:source},alert(){},crypto:globalThis.crypto,
    open:(url,target)=>{opened={url,target};return opener;},
    localStorage:{getItem:key=>{reads.push(key);return getItem(key);}},
    addEventListener:(type,fn)=>events.set(type,fn),removeEventListener:(type)=>events.delete(type)};
  const code=decodeURIComponent(bookmarkURL(panel,'custom_site').slice('javascript:'.length));
  vm.runInNewContext(code,{window:w,crypto:globalThis.crypto,URL,TextEncoder,setTimeout:()=>1,clearTimeout(){}});
  const nonce='e45801bc-5e52-49e7-bb3e-46ce17cf0430';
  function deliver(type,origin=panel,from=opener) {
    const proof = messages[0]?.data.siteNonce;
    events.get('message')?.({origin,source:from,data:{channel,type,nonce,siteNonce:proof}});
  }
  return {messages,reads,opener,opened,deliver};
}

const account={access_token:'fixture.access.token',refresh_token:'fixture-refresh',expires_at:Date.now()/1000+3600,
  token_type:'bearer',provider_token:'unneeded',user:{id:'user',is_anonymous:false,user_metadata:{private:'remove'}}};

test('bookmark prefers the current Chunlou account after an explicit paired request', () => {
  const {messages,reads,opener,opened,deliver}=sourceBookmark(key=>{
    if (key === allTokenKey) return JSON.stringify(account);
    throw new Error('unrelated account');
  });
  assert.deepEqual(reads,[]);
  assert.deepEqual(opened,{url:panel+'/chunlou-connect.html?site=custom_site&from=bookmark',target:'_blank'});
  deliver('panel-ready','http://localhost:4173'); deliver('panel-ready',panel,{});
  assert.equal(messages.length,0);
  deliver('panel-ready');
  assert.equal(messages[0].data.type,'hello');
  assert.equal(messages[0].origin,panel);
  deliver('read');deliver('pair','http://127.0.0.1:12345');deliver('read');
  deliver('pair',panel,{});deliver('read');
  assert.deepEqual(reads,[]);
  deliver('pair',panel,opener); deliver('read','http://127.0.0.1:12345'); assert.deepEqual(reads,[]);
  deliver('read'); deliver('read');
  assert.deepEqual(reads,[allTokenKey]);
  assert.equal(messages.at(-1).origin,panel);
  assert.equal(messages.at(-1).data.storageKey,allTokenKey);
  const state=JSON.parse(messages.at(-1).data.session);
  assert.deepEqual(state.user,{id:'user',is_anonymous:false});
  assert.equal(state.provider_token,undefined);
});

test('bookmark checks the legacy record only when the preferred record is absent', () => {
  const expired=JSON.stringify({...account,expires_at:0});
  const legacy=JSON.stringify(account);
  for (const [current,storedLegacy,reason] of [
    [null,legacy,null], [null,null,'no-session'], [expired,legacy,'expired'],
    ['{',legacy,'invalid'], ['',legacy,'invalid'], [JSON.stringify({}),legacy,'invalid'],
    [JSON.stringify({...account,user:{id:'anonymous',is_anonymous:true}}),legacy,'invalid']
  ]) {
    const {messages,reads,deliver}=sourceBookmark(key=>key===allTokenKey?current:storedLegacy);
    deliver('panel-ready');deliver('pair');deliver('read');
    assert.deepEqual(reads,current===null?[allTokenKey,tokenKey]:[allTokenKey]);
    const sent=messages.at(-1).data;
    if (reason) {
      assert.equal(sent.reason,reason);
      assert.equal(sent.session,undefined);
    } else {
      assert.equal(sent.storageKey,tokenKey);
      assert.equal(JSON.parse(sent.session).user.id,'user');
    }
  }
});

test('blocked site storage produces a storage error', () => {
  const {messages,reads,deliver}=sourceBookmark(()=>{throw new Error('SecurityError');});
  deliver('panel-ready');deliver('pair');deliver('read');
  assert.deepEqual(reads,[allTokenKey]);
  assert.equal(messages.at(-1).data.reason,'storage');
});

test('the panel preserves the verified source storage key in the import request', async () => {
  for (const key of [allTokenKey,tokenKey]) {
    const {messages,ctl,receive,saves}=harness();
    ctl.open(); await receive('hello'); const nonce=messages.at(-1).data.nonce;
    await receive('ready',{nonce}); ctl.confirm();
    await receive('account',{nonce,storageKey:key,session:'{}'});
    assert.deepEqual(saves,[{storageKey:key,session:'{}'}]);
  }
});

test('missing errors distinguish a legacy bookmark from a signed-out tab', async () => {
  for (const [reason, guidance] of [['missing',/旧版/],['no-session',/登录/]]) {
    const reports=[], replies=[];
    const popup={closed:false,postMessage:data=>replies.push(data)};
    const ctl=association({openWindow:()=>popup,report:(...args)=>reports.push(args)});
    const reply=(type,fields)=>ctl.receive({source:popup,origin:source,data:{channel,type,siteNonce,...fields}});
    ctl.open(); await reply('hello'); const nonce=replies.at(-1).nonce;
    await reply('ready',{nonce}); ctl.confirm(); await reply('error',{nonce,reason});
    const [phase,detail,code]=reports.at(-1);
    assert.equal(phase,'error'); assert.equal(code,reason);
    assert.match(detail,guidance);
  }
});

test('accounts can fill exactly 64 KiB of UTF-8 request body; a larger one never reaches save', async () => {
  const base=JSON.stringify({storageKey:tokenKey,session:''});
  const room=65536-new TextEncoder().encode(base).length;
  const session='中'.repeat(Math.floor(room/3))+'x'.repeat(room%3);
  for (const extra of ['', 'x']) {
    const {messages,ctl,receive,saves,states}=harness();
    ctl.open(); await receive('hello');
    const nonce=messages.at(-1).data.nonce;
    await receive('ready',{nonce}); ctl.confirm();
    await receive('account',{nonce,storageKey:tokenKey,session:session+extra});
    assert.equal(saves.length,extra?0:1);
    assert.equal(states.at(-1),extra?'error':'saved');
  }
});

test('a denied import remains retryable and never replays the old session automatically', async () => {
  let writes=0;
  const states=[], messages=[];
  const popup={closed:false, postMessage:data=>messages.push(data)};
  const ctl=association({openWindow:()=>popup,report:(phase,msg)=>states.push([phase,msg]),
    save:async()=>{writes++;throw new Error('春楼没有确认这个账号');}});
  const reply=(type,more={})=>ctl.receive({source:popup,origin:source,data:{channel,type,siteNonce,...more}});
  ctl.open();await reply('hello');const nonce=messages.at(-1).nonce;
  await reply('ready',{nonce});ctl.confirm();
  await reply('account',{nonce,storageKey:tokenKey,session:'{}'});
  assert.deepEqual(states.at(-1),['error','春楼没有确认这个账号']);
  ctl.open();
  await reply('account',{nonce,storageKey:tokenKey,session:'{}'});
  assert.equal(writes,1);
  assert.equal(states.at(-1)[0],'waiting');
});

test('bookmark confirmation page waits for its own opener and rejects a hello from a previous round', async () => {
  const {ctl,popup,messages,saves,receive,states}=harness();
  ctl.fromBookmark(popup);
  assert.equal(messages[0].data.type,'panel-ready');
  const nonce=messages[0].data.nonce;
  ctl.tick();
  assert.equal(messages[1].data.type,'panel-ready');
  await receive('hello',{nonce:'old'}); await receive('hello',{nonce},source,{});
  assert.equal(messages.at(-1).data.type,'panel-ready');
  await receive('hello',{nonce});
  assert.equal(messages.at(-1).data.type,'pair');
  await receive('ready',{nonce});
  assert.equal(states.at(-1),'ready');
  await receive('account',{nonce,storageKey:tokenKey,session:'{}'});
  assert.equal(saves.length,0);
  ctl.confirm();
  await receive('account',{nonce,storageKey:tokenKey,session:'{}'});
  assert.equal(saves.length,1);
});

test('bookmark reports a blocked confirmation popup without touching site storage or contacting an existing opener', () => {
  const alerts=[];
  const w={opener:{postMessage:()=>assert.fail('unrelated opener')},location:{origin:source},
    open:()=>null,alert:v=>alerts.push(v),get localStorage(){assert.fail('early read');}};
  vm.runInNewContext(decodeURIComponent(bookmarkURL(panel).slice('javascript:'.length)),{window:w});
  assert.match(alerts[0],/弹窗|窗口/);
  const {ctl,states}=harness();
  ctl.fromBookmark(null);
  assert.equal(states.at(-1),'error');
});
