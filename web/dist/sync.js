import {emptyState,validateState,mergeStates,readingFor,syncMessage,blobHash,davConfig,resourceUrl,syncRoot,syncFolders,collectionResponse,deviceFiles,deviceFile,deviceValid,MAX_PACKAGE} from './sync-core.js';
import {isAndroid,nativeRequest,cancelNativeRequests,saveNativeSecret,loadNativeSecret,clearNativeSecret} from './native.js';

const XML='<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getetag/></d:prop></d:propfind>';
function encodedHeader(value){return btoa(Array.from(new TextEncoder().encode(JSON.stringify(value)),b=>String.fromCharCode(b)).join(''));}
async function boundedBody(response,limit){
  if(!response.body)return new Blob();
  const reader=response.body.getReader(),parts=[];let size=0;
  try{for(;;){const result=await reader.read();if(result.done)break;size+=result.value.byteLength;if(size>limit){await reader.cancel();throw new Error('WebDAV 响应大小超限');}parts.push(result.value);}}finally{reader.releaseLock();}
  return new Blob(parts);
}
function read(key,fallback) {try{return JSON.parse(localStorage.getItem(key))??fallback;}catch{return fallback;}}
function save(key,value) {localStorage.setItem(key,JSON.stringify(value));}
const CATALOG_PREFIX='paperloom-sync-catalog-v2:',ACTIVE_SCOPE='paperloom-sync-active-scope-v2';
const LEGACY_HIDDEN='paperloom-sync-hidden-v1',HIDDEN_MIGRATED='paperloom-sync-hidden-migrated-v2';
function hiddenIds(value){return Array.isArray(value)?value.filter(id=>typeof id==='string'&&id.length>0&&id.length<=120).slice(0,2000):[];}
function catalogKey(config){const value=davConfig(config);return CATALOG_PREFIX+JSON.stringify([value.url,value.folder,value.username]);}
function failure(status,request) {
  if(status===401)return new Error('WebDAV 登录未通过，请检查账号及应用专用密码');
  if(status===403)return new Error('WebDAV 拒绝访问，请检查目录读写权限');
  if(status>=300&&status<400)return new Error('WebDAV 地址发生重定向，请填写服务的最终地址');
  if(status===507)return new Error('WebDAV 存储空间不足');
  const where=request?`；${request.method} ${request.path||'基础目录'}`:'';
  return new Error(`WebDAV ${status===404?'找不到同步目录或文件':'请求失败'} (${status}${where})`);
}
function check(response,statuses=[200,201,204,207]) {if(!statuses.includes(response.status))throw failure(response.status,response.request);return response;}
export class DAVTransport {
  constructor(config,options={}) {this.config=davConfig(config);this.options=options;}
  async request(method,path='',headers={},data='',signal,binary=false) {
    signal?.throwIfAborted();
    let url=resourceUrl(this.config,path);
    if(['PROPFIND','MKCOL'].includes(method)&&!url.endsWith('/'))url+='/';
    const result=value=>({...value,request:{method,path}});
    if(isAndroid)return result(await nativeRequest(this.config,method,url,headers,data,signal,binary));
    if(this.options.server) {
      const payload={url:this.config.url,username:this.config.username,password:this.config.password,allowHttp:this.config.allowHttp,method,path,headers};
      const relayHeaders=new Headers();if(this.options.token)relayHeaders.set('Authorization','Bearer '+this.options.token());
      let endpoint='request',body;
      if(data instanceof Blob || binary) {
        endpoint=binary?'download':'upload';relayHeaders.set('X-Paperloom-DAV',encodedHeader(payload));
        if(!binary){body=data;relayHeaders.set('Content-Type','application/zip');}
      } else {relayHeaders.set('Content-Type','application/json');body=JSON.stringify({...payload,data});}
      const response=await fetch('/api/webdav/'+endpoint,{method:'POST',headers:relayHeaders,body,signal,cache:'no-store'});
      if(!response.ok&& !binary) {
        const result=await response.json().catch(()=>({}));throw new Error(result.detail||'PC 同步代理请求失败');
      }
      if(binary)return result({status:response.status,headers:Object.fromEntries(response.headers),blob:response.ok?await response.blob():null});
      return result(await response.json());
    }
    if(this.config.url.startsWith('http:'))throw new Error('独立网页阅读器需要 HTTPS WebDAV；局域网 HTTP 请使用安卓 App 或 PC 准备端');
    const directHeaders=new Headers(headers);
    if(this.config.username||this.config.password) {
      const {authHeader}=await import('./sync-core.js');directHeaders.set('Authorization',authHeader(this.config.username,this.config.password));
    }
    let response;
    try {response=await fetch(url,{method,headers:directHeaders,body:['GET','HEAD'].includes(method)?undefined:data||undefined,signal,redirect:'error',cache:'no-store',credentials:'omit',referrerPolicy:'no-referrer'});}
    catch(error){if(signal?.aborted)throw error;throw new Error('无法连接 WebDAV。网页端需服务允许跨域；也可改用安卓 App 或 PC 准备端。');}
    if(method!=='HEAD'&&Number(response.headers.get('content-length'))>(binary?MAX_PACKAGE:2*1024*1024))throw new Error('WebDAV 响应大小超限');
    if(binary){const blob=response.ok?await boundedBody(response,MAX_PACKAGE):null;return result({status:response.status,headers:Object.fromEntries(response.headers),blob});}
    const body=await (await boundedBody(response,2*1024*1024)).text();
    return result({status:response.status,headers:Object.fromEntries(response.headers),body});
  }
  async folder(path,signal) {
    const response=await this.request('PROPFIND',path,{'Depth':'0','Content-Type':'application/xml'},XML,signal);
    if(response.status===404 && path)return false;
    check(response,[207]);
    if(!collectionResponse(response.body))throw new Error(`WebDAV 地址指向文件或无效目录：${path||'基础目录'}`);
    return true;
  }
  async inspectFolders(signal) {
    await this.folder('',signal);const missing=[];
    for(const path of syncFolders(this.config))if(!await this.folder(path,signal))missing.push(path);
    return missing;
  }
  async ensureFolders(signal) {
    await this.folder('',signal);
    for(const path of syncFolders(this.config)) {
      if(await this.folder(path,signal))continue;
      check(await this.request('MKCOL',path,{},'',signal),[201,200,204,405]);
      if(!await this.folder(path,signal))throw new Error(`WebDAV 同步目录创建后仍不可读取：${path}`);
    }
  }
  async list(signal) {
    const path=syncRoot(this.config)+'/devices';
    const response=check(await this.request('PROPFIND',path,{'Depth':'1','Content-Type':'application/xml'},XML,signal),[207]);
    return deviceFiles(response.body,resourceUrl(this.config,path)+'/');
  }
}

export class SyncEngine {
  constructor({device,catalog,suspended,getPapers,getReading,putPaper,applyReading,status,persist,hidden=[]}) {
    if(!deviceValid(device))throw new Error('同步设备 ID 无效');
    this.device=device;this.catalog=catalog?validateState(catalog):emptyState(device);this.catalog.device=device;
    this.suspended=suspended?validateState(suspended):emptyState(device);this.suspended.device=device;
    this.hooks={getPapers,getReading,putPaper,applyReading,status,persist};this.hidden=new Set(hidden);
    this.time=0;this.counter=0;this.observe(this.catalog);this.observe(this.suspended);this.running=false;
    this.snapshotCache=new Map();this.snapshotBytes=0;
  }
  observe(catalog) {
    for(const book of Object.values(catalog.books))for(const value of [book.bundle,book.progress,...Object.values(book.bookmarks)])if(value?.stamp&&value.stamp[0]>=this.time){this.time=value.stamp[0];this.counter=Math.max(this.counter,value.stamp[1]);}
  }
  stamp() {const now=Date.now();if(now>this.time){this.time=now;this.counter=0;}else this.counter++;return [this.time,this.counter,this.device];}
  persist() {this.hooks.persist?.(this.catalog,[...this.hidden],this.suspended);}
  reconcile(remote) {
    // Cached observations cannot prove that packages belong to this DAV scope.
    // Retain detached reading changes privately until a copy or live cloud record returns.
    const remembered=mergeStates(this.suspended,this.catalog),attached=emptyState(this.device),suspended=emptyState(this.device);
    const local=new Map(this.hooks.getPapers().filter(p=>!p.document.metadata.demo).map(p=>[p.id,p]));
    for(const [id,book] of Object.entries(remembered.books)) {
      const cloud=remote.books[id],paper=local.get(id);
      if(!cloud&&!paper){suspended.books[id]=book;continue;}
      attached.books[id]=structuredClone(book);
      if(cloud&&paper?.syncHash!==book.bundle.hash) {
        attached.books[id].bundle=structuredClone(cloud.bundle);
        if(book.bundle.hash!==cloud.bundle.hash)suspended.books[id]=book;
      }
    }
    this.catalog=mergeStates(attached,remote);this.suspended=suspended;
    this.observe(this.catalog);this.observe(this.suspended);this.persist();
  }
  async imported(paper,legacy=false) {
    const hash=paper.syncHash||await blobHash(paper.package);paper.syncHash=hash;
    let book=this.catalog.books[paper.id];
    if(!book&&this.suspended.books[paper.id]){book=this.suspended.books[paper.id];this.catalog.books[paper.id]=book;delete this.suspended.books[paper.id];}
    if(!book || book.bundle.hash!==hash) {
      const stamp=legacy?[Math.min(paper.importedAt||Date.now(),Date.now()),0,this.device]:this.stamp();
      this.catalog.books[paper.id]={...book,bundle:{hash,size:paper.package.size,title:(paper.document.metadata.title_translated||paper.document.metadata.title).slice(0,800),stamp},bookmarks:book?.bookmarks||{}};
    }
    this.hidden.delete(paper.id);this.observe(this.catalog);this.persist();return paper;
  }
  bookmark(id,block,value) {const book=this.catalog.books[id];if(!book)return;book.bookmarks[block]={value,stamp:this.stamp()};this.persist();}
  progress(id,value) {
    const book=this.catalog.books[id];if(!book)return;
    const previous=book.progress;
    if(previous&&previous.percent===value.percent&&previous.block===value.block&&previous.mode===value.mode)return;
    book.progress={percent:value.percent||0,block:value.block||null,mode:value.mode||'translated',stamp:this.stamp()};this.persist();
  }
  hide(id){this.hidden.add(id);this.persist();}
  restoreHidden(){this.hidden.clear();this.persist();}
  cacheSnapshot(name,state,size) {
    if(this.snapshotCache.has(name))return;
    while(this.snapshotBytes+size>16*1024*1024&&this.snapshotCache.size) {
      const oldest=this.snapshotCache.keys().next().value;
      this.snapshotBytes-=this.snapshotCache.get(oldest).size;this.snapshotCache.delete(oldest);
    }
    this.snapshotCache.set(name,{state,size});this.snapshotBytes+=size;
  }
  async seed() {
    for(const paper of this.hooks.getPapers()) {
      if(paper.document.metadata.demo || this.hidden.has(paper.id))continue;
      const isNew=!this.catalog.books[paper.id]&&!this.suspended.books[paper.id];await this.imported(paper,true);
      if(isNew) {
        const reading=this.hooks.getReading()[paper.id];
        if(reading){const stamp=this.catalog.books[paper.id].bundle.stamp;this.catalog.books[paper.id].progress={percent:reading.percent||0,block:reading.block||null,mode:reading.mode||'translated',stamp};for(const block of reading.bookmarks||[])this.catalog.books[paper.id].bookmarks[block]={value:true,stamp};}
      }
    }
    this.persist();
  }
  async run(transport,signal) {
    if(this.running)throw new Error('同步正在进行');this.running=true;
    const summary={uploaded:0,downloaded:0,devices:0,missing:[]};
    const note=text=>this.hooks.status?.(text);
    const root=syncRoot(transport.config);
    let remote=emptyState(this.device);const remoteHashes=new Set();
    const available=new Set(),absent=new Set();
    const packageExists=async(hash,size)=>{
      if(available.has(hash))return true;if(absent.has(hash))return false;
      const path=root+'/packages/'+hash+'.folio';
      let response=await transport.request('HEAD',path,{},'',signal);
      if([405,501].includes(response.status)) {
        response=await transport.request('GET',path,{},'',signal,true);
        if(response.status!==404){check(response,[200]);if(!response.blob||response.blob.size!==size||await blobHash(response.blob)!==hash)throw new Error('云端已有文献包的大小或哈希不一致，没有覆盖它');}
      } else if(response.status!==404) {
        check(response,[200]);const length=Number(response.headers?.['content-length']);
        if(length>0&&length!==size)throw new Error('云端已有文献包大小不一致，没有覆盖它');
      }
      if(response.status===404){absent.add(hash);return false;}
      available.add(hash);return true;
    };
    const mergeRemote=(state)=>{remote=mergeStates(remote,state);for(const book of Object.values(state.books))remoteHashes.add(book.bundle.hash);};
    const load=async (name,force=false)=>{
      const identity=deviceFile(name);if(!identity)throw new Error('云端设备文件名无效');
      const cached=identity.hash&&!force&&this.snapshotCache.get(name);
      if(cached)return cached.state;
      const response=await transport.request('GET',root+'/devices/'+name,{},'',signal);
      check(response,[200]);let state;
      const body=new Blob([response.body]);
      if(identity.hash&&await blobHash(body)!==identity.hash)throw new Error('云端同步快照的哈希不匹配，没有覆盖本机或云端状态');
      try{state=validateState(JSON.parse(response.body));}catch{throw new Error('云端设备状态损坏；没有覆盖云端文件');}
      if(state.device!==identity.device)throw new Error('云端设备文件名与内容不一致');
      if(identity.hash)this.cacheSnapshot(name,state,body.size);
      return state;
    };
    const preparePackages=async()=>{
      for(const paper of this.hooks.getPapers()) {
        if(paper.document.metadata.demo || this.hidden.has(paper.id))continue;
        const book=this.catalog.books[paper.id];if(!book || book.bundle.hash!==paper.syncHash || available.has(paper.syncHash))continue;
        if(remoteHashes.has(paper.syncHash)&&await packageExists(paper.syncHash,book.bundle.size))continue;
        signal?.throwIfAborted();note('上传文献：'+book.bundle.title);
        const path=root+'/packages/'+paper.syncHash+'.folio';
        const response=await transport.request('PUT',path,{'If-None-Match':'*','Content-Type':'application/zip'},paper.package,signal);
        if(response.status===412) {
          const existing=check(await transport.request('GET',path,{},'',signal,true),[200]);
          if(!existing.blob || await blobHash(existing.blob)!==paper.syncHash)throw new Error('云端已有文献包的哈希不一致，没有覆盖它');
        }else {check(response,[200,201,204]);summary.uploaded++;}
        remoteHashes.add(paper.syncHash);
        absent.delete(paper.syncHash);available.add(paper.syncHash);
      }
    };
    try {
      await this.seed();note('连接同步目录');await transport.ensureFolders(signal);
      const names=await transport.list(signal);summary.devices=new Set(names.map(name=>deviceFile(name)?.device)).size;note('合并设备状态');
      for(const name of names){signal?.throwIfAborted();mergeRemote(await load(name));}
      this.reconcile(remote);
      await preparePackages();
      const downloadPackages=async()=>{for(const [id,book] of Object.entries(this.catalog.books)) {
        if(this.hidden.has(id))continue;
        const local=this.hooks.getPapers().find(p=>p.id===id);
        if(local?.syncHash===book.bundle.hash)continue;
        signal?.throwIfAborted();note('下载文献：'+book.bundle.title);
        const response=await transport.request('GET',root+'/packages/'+book.bundle.hash+'.folio',{},'',signal,true);
        if(response.status===404){absent.add(book.bundle.hash);summary.missing.push({id,title:book.bundle.title});continue;}
        check(response,[200]);
        if(!response.blob || response.blob.size!==book.bundle.size || await blobHash(response.blob)!==book.bundle.hash)throw new Error('下载文献包的大小或哈希不匹配，本机文献保持原样');
        if(this.catalog.books[id].bundle.hash!==book.bundle.hash)continue;
        await this.hooks.putPaper(response.blob,id,book.bundle.hash);available.add(book.bundle.hash);summary.downloaded++;
      }};
      signal?.throwIfAborted();await downloadPackages();await preparePackages();
      const published=emptyState(this.device);
      for(const [id,book] of Object.entries(this.catalog.books))if(await packageExists(book.bundle.hash,book.bundle.size))published.books[id]=book;
      const data=JSON.stringify(validateState(published)),blob=new Blob([data]);
      if(blob.size>2*1024*1024)throw new Error('同步状态超过 2 MB');
      const hash=await blobHash(blob),ownName=this.device+'.'+hash+'.json';
      // Each state has a unique content address. Ignored conditional headers cannot
      // overwrite another window's update; concurrent branches are merged next run.
      if(!names.includes(ownName)) {
        note('保存本机同步快照');
        check(await transport.request('PUT',root+'/devices/'+ownName,{'Content-Type':'application/json; charset=utf-8','If-None-Match':'*'},data,signal),[200,201,204,412]);
        await load(ownName,true);
      }
      const local=new Map(this.hooks.getPapers().map(p=>[p.id,p.syncHash]));
      this.hooks.applyReading(Object.fromEntries(Object.entries(this.catalog.books).filter(([id,book])=>local.get(id)===book.bundle.hash).map(([id,book])=>[id,readingFor(book)])));
      summary.cloud=Object.keys(this.catalog.books).length;
      summary.local=this.hooks.getPapers().filter(p=>!p.document.metadata.demo).length;
      summary.hidden=Object.entries(this.catalog.books).filter(([id])=>this.hidden.has(id)).map(([id,book])=>({id,title:book.bundle.title}));
      this.persist();note(syncMessage(summary));return summary;
    }finally{this.running=false;}
  }
}

export class SyncController {
  constructor(hooks) {
    this.hooks=hooks;this.config=read('paperloom-webdav-config-v1',{});this.password='';this.timer=null;this.abort=null;this.onStatus=hooks.status;
    let device=read('paperloom-sync-device-v1',null);if(!deviceValid(device)){device=crypto.randomUUID();save('paperloom-sync-device-v1',device);}
    const previous=read(ACTIVE_SCOPE,null);
    this.catalogKey=typeof previous==='string'&&previous.startsWith(CATALOG_PREFIX)?previous:'paperloom-sync-catalog-v1';
    if(this.config.url){try{this.catalogKey=catalogKey(this.config);}catch{this.config={};}}
    let catalog=read(this.catalogKey,null);
    if(this.config.url&&!catalog&&!read('paperloom-sync-migrated-v1',false)) {
      catalog=read('paperloom-sync-catalog-v1',null);save('paperloom-sync-migrated-v1',true);
    }
    try{if(catalog)validateState(catalog);}catch{catalog=null;}
    let suspended=read(this.catalogKey+':suspended',null);try{if(suspended)validateState(suspended);}catch{suspended=null;}
    // The old list was global: a removal in one DAV library also hid that ID in
    // every other library. Bind it once to the last known connection, then load
    // and persist each connection's removals independently.
    const legacyScope=typeof previous==='string'&&previous.startsWith(CATALOG_PREFIX)?previous:this.config.url?this.catalogKey:null;
    if(legacyScope&&!read(HIDDEN_MIGRATED,false)) {
      if(read(legacyScope+':hidden',null)===null)save(legacyScope+':hidden',hiddenIds(read(LEGACY_HIDDEN,[])));
      save(HIDDEN_MIGRATED,true);
    }
    const hidden=hiddenIds(read(this.catalogKey+':hidden',[]));
    this.engine=new SyncEngine({...hooks,device,catalog,suspended,hidden,persist:(state,hidden,suspended)=>{save(this.catalogKey,state);save(this.catalogKey+':suspended',suspended);save(this.catalogKey+':hidden',hidden);if(this.catalogKey.startsWith(CATALOG_PREFIX))save(ACTIVE_SCOPE,this.catalogKey);}});
  }
  async initialize(){if(isAndroid&&this.config.remember){try{const secret=await loadNativeSecret();if(secret?.url===this.config.url&&secret.username===this.config.username)this.password=secret.password||'';}catch{this.onStatus?.('需要重新填写 WebDAV 密码');}}
    await this.engine.seed();this.schedule();}
  value(){return {...this.config,password:this.password};}
  async configure(value){
    const next=davConfig(value);
    if(this.engine.running)throw new Error('正在同步，请等待完成或取消后再保存配置');
    this.cancel();const key=catalogKey(next);
    if(key!==this.catalogKey) {
      this.engine.persist();let saved=read(key,null);try{if(saved)validateState(saved);}catch{saved=null;}
      let suspended=read(key+':suspended',null);try{if(suspended)validateState(suspended);}catch{suspended=null;}
      this.catalogKey=key;this.engine.catalog=saved?validateState(saved):emptyState(this.engine.device);this.engine.catalog.device=this.engine.device;
      this.engine.suspended=suspended?validateState(suspended):emptyState(this.engine.device);this.engine.suspended.device=this.engine.device;
      this.engine.hidden=new Set(hiddenIds(read(key+':hidden',[])));
      this.engine.snapshotCache.clear();this.engine.snapshotBytes=0;this.engine.observe(this.engine.catalog);this.engine.observe(this.engine.suspended);await this.engine.seed();
      save('paperloom-sync-migrated-v1',true);
    }
    this.password=next.password;const {password,...safe}=next;this.config=safe;save('paperloom-webdav-config-v1',safe);
    if(isAndroid){if(next.remember)await saveNativeSecret({url:next.url,username:next.username,password});else await clearNativeSecret();}
    this.schedule();
  }
  async disconnect(){this.cancel();this.config={};this.password='';save('paperloom-webdav-config-v1',{});if(isAndroid)await clearNativeSecret();}
  transport(value=this.value()){return new DAVTransport(value,{server:this.hooks.server(),token:this.hooks.token});}
  async test(value){const missing=await this.transport(value).inspectFolders();return missing.length?`连接成功；同步时将创建 ${missing.length} 个子目录`:'连接成功，同步目录均可读取';}
  async synchronize(){if(!this.config.url)throw new Error('请先配置 WebDAV');if(this.engine.running)throw new Error('同步正在进行');clearTimeout(this.timer);this.timer=null;this.abort=new AbortController();
    try {
      const run=()=>this.engine.run(this.transport(),this.abort.signal);
      const result=navigator.locks?await navigator.locks.request('paperloom-webdav-sync',{ifAvailable:true},lock=>{if(!lock)throw new Error('其他窗口正在同步');return run();}):await run();
      save('paperloom-webdav-last-sync',Date.now());return result;
    } finally {this.abort=null;this.schedule(60000);}
  }
  async restoreAndSynchronize(){
    if(!this.config.url)throw new Error('请先配置 WebDAV');
    if(this.engine.running)throw new Error('同步正在进行，请等待完成后恢复文献');
    this.engine.restoreHidden();return this.synchronize();
  }
  schedule(delay=2500){if(!this.config.auto||!this.config.url||document.visibilityState==='hidden'||this.engine.running){clearTimeout(this.timer);this.timer=null;return;}const due=Date.now()+delay;if(this.timer&&this.dueAt<=due)return;clearTimeout(this.timer);this.dueAt=due;this.timer=setTimeout(()=>{this.timer=null;this.synchronize().catch(error=>{this.onStatus?.(error.message);this.schedule(60000);});},delay);}
  cancel(){clearTimeout(this.timer);this.timer=null;this.abort?.abort(new DOMException('同步已取消','AbortError'));if(isAndroid&&this.engine.running)cancelNativeRequests().catch(()=>{});}
  async imported(paper){if(!paper.document.metadata.demo)await this.engine.imported(paper);this.schedule();}
  bookmark(id,block,value){this.engine.bookmark(id,block,value);this.schedule();}
  progress(id,value){this.engine.progress(id,value);this.schedule(12000);}
}
