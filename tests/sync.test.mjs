import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {DOMParser} from '../mobile/node_modules/@xmldom/xmldom/lib/index.js';
import {unzipSync,zipSync} from '../web/dist/vendor/fflate.js';
import {decodePackage} from '../web/dist/package-core.js';
import {emptyState,mergeStates,validateState,deviceFiles,deviceFile,davConfig,blobHash,readingFor,syncMessage} from '../web/dist/sync-core.js';
import {SyncEngine,SyncController,DAVTransport} from '../web/dist/sync.js';
import {ReadingState} from '../web/dist/reader-state.js';
globalThis.DOMParser=DOMParser;
const A='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',B='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const config={url:'https://example.test/dav/',folder:'Paperloom',username:'reader',password:'test-only-password'};
const directoryXML='<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>';
function folderTransport(existing,settings=config) {
  const transport=new DAVTransport(settings),calls=[];
  transport.request=async(method,path)=>{
    calls.push([method,path]);
    if(method==='MKCOL') {
      const parent=path.split('/').slice(0,-1).join('/');
      if(!existing.has(parent))return {status:409,request:{method,path}};
      existing.add(path);return {status:201,request:{method,path}};
    }
    return {status:existing.has(path)?207:404,body:directoryXML,request:{method,path}};
  };
  return {transport,calls};
}

test('existing DAV directories are read first and never receive a redundant MKCOL',async()=>{
  const {transport,calls}=folderTransport(new Set(['','Paperloom','Paperloom/v1','Paperloom/v1/packages','Paperloom/v1/devices']));
  await transport.ensureFolders();assert.equal(calls.filter(([method])=>method==='MKCOL').length,0);
  assert.deepEqual(await transport.inspectFolders(),[]);
});

test('a fresh nested DAV folder is created in parent order and then verified',async()=>{
  const {transport,calls}=folderTransport(new Set(['']),{...config,folder:'Research/Paperloom'});
  assert.equal((await transport.inspectFolders()).length,5);assert.ok(calls.every(([method])=>method==='PROPFIND'));
  await transport.ensureFolders();
  assert.deepEqual(calls.filter(([method])=>method==='MKCOL').map(([,path])=>path),['Research','Research/Paperloom','Research/Paperloom/v1','Research/Paperloom/v1/packages','Research/Paperloom/v1/devices']);
  assert.deepEqual(await transport.inspectFolders(),[]);
});

test('DAV 207 for a file or missing properties cannot pass a connection check',async()=>{
  const {transport}=folderTransport(new Set(['']));
  transport.request=async()=>({status:207,body:directoryXML.replace('<d:collection/>','')});
  await assert.rejects(transport.ensureFolders(),/文件或无效目录/);
  transport.request=async()=>({status:207,body:directoryXML.replace('200 OK','404 Not Found')});
  await assert.rejects(transport.inspectFolders(),/文件或无效目录/);
});

test('DAV creation errors show the failing method and relative directory',async()=>{
  const {transport}=folderTransport(new Set([''])),request=transport.request;
  transport.request=(method,path)=>method==='MKCOL'?Promise.resolve({status:404,request:{method,path}}):request(method,path);
  await assert.rejects(transport.ensureFolders(),/404；MKCOL Paperloom/);
});
async function fixture(id) {
  const files=unzipSync(new Uint8Array(await readFile(new URL('../web/dist/demo/demo.folio',import.meta.url))));
  const document=JSON.parse(new TextDecoder().decode(files['document.json']));document.metadata.demo=false;
  if(id)document.id=id;
  files['document.json']=new TextEncoder().encode(JSON.stringify(document));
  const manifest=JSON.parse(new TextDecoder().decode(files['manifest.json']));
  manifest.id=document.id;
  const entry=manifest.files.find(e=>e.path==='document.json');entry.size=files['document.json'].length;entry.sha256=await blobHash(new Blob([files['document.json']]));
  files['manifest.json']=new TextEncoder().encode(JSON.stringify(manifest));
  return decodePackage(zipSync(files));
}
class Remote {
  constructor(){this.config=config;this.files=new Map();this.revision=0;this.corrupt=false;}
  async ensureFolders(){}
  async list(){return [...this.files.keys()].filter(p=>p.startsWith('Paperloom/v1/devices/')).map(p=>p.split('/').at(-1));}
  async request(method,path,headers={},data='',signal,binary=false) {
    signal?.throwIfAborted();
    if(method==='PUT') {
      const previous=this.files.get(path);
      if(headers['If-None-Match']==='*'&&previous || headers['If-Match']&&headers['If-Match']!==previous?.etag)return {status:412,headers:{}};
      const value={data,etag:'"'+(++this.revision)+'"'};this.files.set(path,value);return {status:previous?204:201,headers:{etag:value.etag}};
    }
    const value=this.files.get(path);if(!value)return {status:404,headers:{}};
    if(binary)return {status:200,headers:{etag:value.etag},blob:this.corrupt?new Blob(['corrupt']):value.data};
    return {status:200,headers:{etag:value.etag},body:value.data};
  }
}
function client(device,papers=[],options={}) {
  const reading={};let stored=0;
  const engine=new SyncEngine({device,...options,getPapers:()=>papers,getReading:()=>reading,putPaper:async(blob,id,hash)=>{
    const paper=await decodePackage(await blob.arrayBuffer());assert.equal(paper.id,id);paper.syncHash=hash;
    const index=papers.findIndex(p=>p.id===id);if(index>=0)papers.splice(index,1);papers.push(paper);stored++;
  },applyReading:values=>Object.assign(reading,values),persist:()=>{}});
  return {engine,papers,reading,get stored(){return stored;}};
}

test('a migrated test-only cache record cannot request or publish its vanished package',async()=>{
  const paper=await fixture(),remote=new Remote(),owner=client(A,[paper]);await owner.engine.run(remote);
  const cached=emptyState(B),ghost=structuredClone(owner.engine.catalog.books[paper.id]);
  ghost.bundle.hash='3'.repeat(64);ghost.bookmarks.saved={value:true,stamp:[Date.now(),0,B]};cached.books['old-test-only']=ghost;
  const receiver=client(B,[],{catalog:cached}),calls=[],request=remote.request.bind(remote);
  remote.request=(method,path,...rest)=>{calls.push([method,path]);return request(method,path,...rest);};
  const result=await receiver.engine.run(remote);
  assert.equal(result.downloaded,1);assert.equal(result.missing.length,0);
  assert.ok(!receiver.engine.catalog.books['old-test-only']);assert.equal(receiver.engine.suspended.books['old-test-only'].bookmarks.saved.value,true);
  assert.ok(!calls.some(([,path])=>path.includes(ghost.bundle.hash)));
  for(const [path,value] of remote.files)if(path.includes('/devices/'))assert.ok(!JSON.parse(value.data).books['old-test-only']);
  await receiver.engine.run(remote);assert.ok(!calls.some(([,path])=>path.includes(ghost.bundle.hash)));
});

test('an unbacked cached version cannot override the live bundle, but preserves reading changes',async()=>{
  const paper=await fixture(),remote=new Remote(),owner=client(A,[paper]);await owner.engine.run(remote);
  const cached=structuredClone(owner.engine.catalog);cached.device=B;
  const book=cached.books[paper.id],block=paper.document.blocks[4].id;
  book.bundle.hash='9'.repeat(64);book.bundle.stamp=[Date.now()+1000,0,B];
  book.progress={percent:83,block,mode:'bilingual',stamp:[Date.now()+1000,1,B]};book.bookmarks[block]={value:true,stamp:[Date.now()+1000,2,B]};
  const receiver=client(B,[],{catalog:cached});await receiver.engine.run(remote);
  assert.equal(receiver.engine.catalog.books[paper.id].bundle.hash,paper.syncHash);
  assert.equal(receiver.reading[paper.id].percent,83);assert.deepEqual(receiver.reading[paper.id].bookmarks,[block]);
  assert.equal(receiver.engine.suspended.books[paper.id].bundle.hash,'9'.repeat(64));
});

test('detached reading changes survive restart and reattach when the original copy returns',async()=>{
  const paper=await fixture(),remote=new Remote(),first=client(A,[paper]);await first.engine.seed();
  const block=paper.document.blocks[4].id;first.engine.bookmark(paper.id,block,true);first.engine.progress(paper.id,{percent:67,block,mode:'original'});
  first.papers.length=0;await first.engine.run(remote);
  const restarted=client(B,[],{suspended:structuredClone(first.engine.suspended)});
  await restarted.engine.imported(paper);restarted.papers.push(paper);await restarted.engine.run(remote);
  assert.equal(restarted.reading[paper.id].percent,67);assert.deepEqual(restarted.reading[paper.id].bookmarks,[block]);
  assert.ok(!restarted.engine.suspended.books[paper.id]);
});

test('a local verified copy repairs a package missing behind a still-existing cloud snapshot',async()=>{
  const paper=await fixture(),remote=new Remote(),owner=client(A,[paper]);await owner.engine.run(remote);
  const path='Paperloom/v1/packages/'+paper.syncHash+'.folio';remote.files.delete(path);
  assert.equal((await owner.engine.run(remote)).uploaded,1);
  assert.equal(await blobHash(remote.files.get(path).data),paper.syncHash);
  const receiver=client(B);assert.equal((await receiver.engine.run(remote)).downloaded,1);
});

test('an unavailable cloud book does not block valid books or enter the published snapshot',async()=>{
  const paper=await fixture(),remote=new Remote(),owner=client(A,[paper]);await owner.engine.run(remote);
  const bad=emptyState(B);bad.books['missing-cloud-book']=structuredClone(owner.engine.catalog.books[paper.id]);bad.books['missing-cloud-book'].bundle.hash='2'.repeat(64);
  const legacy=JSON.stringify(bad);remote.files.set('Paperloom/v1/devices/'+B+'.json',{data:legacy,etag:null});
  const receiver=client('cccccccc-cccc-4ccc-8ccc-cccccccccccc'),result=await receiver.engine.run(remote);
  assert.equal(result.downloaded,1);assert.equal(result.missing.length,1);assert.match(syncMessage(result),/^已同步可用文献/);
  const own=[...remote.files].find(([path])=>path.includes('/devices/cccccccc-cccc-4ccc-8ccc-cccccccccccc.'));
  assert.ok(!JSON.parse(own[1].data).books['missing-cloud-book']);
  assert.equal(remote.files.get('Paperloom/v1/devices/'+B+'.json').data,legacy);
});

test('servers without HEAD fall back to verified GET without repeatedly uploading',async()=>{
  class NoHead extends Remote {async request(method,...args){return method==='HEAD'?{status:405,headers:{}}:super.request(method,...args);}}
  const paper=await fixture(),remote=new NoHead(),owner=client(A,[paper]);await owner.engine.run(remote);
  assert.equal((await owner.engine.run(remote)).uploaded,0);
});

test('a missing newer bundle does not apply its reading positions to the older local document',async()=>{
  const paper=await fixture(),remote=new Remote(),owner=client(A,[paper]);await owner.engine.run(remote);
  owner.reading[paper.id]={percent:12,block:paper.document.blocks[4].id,mode:'original',bookmarks:[]};
  const newer=emptyState(B);newer.books[paper.id]=structuredClone(owner.engine.catalog.books[paper.id]);
  const book=newer.books[paper.id];book.bundle.hash='7'.repeat(64);book.bundle.stamp=[Date.now()+1000,0,B];
  book.progress={percent:90,block:'new-revision-block',mode:'bilingual',stamp:[Date.now()+1000,1,B]};
  remote.files.set('Paperloom/v1/devices/'+B+'.json',{data:JSON.stringify(newer),etag:null});
  const result=await owner.engine.run(remote);assert.equal(result.missing.length,1);
  assert.equal(owner.reading[paper.id].percent,12);assert.equal(owner.reading[paper.id].mode,'original');
  assert.equal(owner.papers[0].syncHash,paper.syncHash);
});

test('HEAD Content-Length describes the package and is not treated as a metadata response body',async()=>{
  const original=globalThis.fetch;
  globalThis.fetch=async()=>new Response(null,{status:200,headers:{'Content-Length':String(64*1024*1024)}});
  try{assert.equal((await new DAVTransport(config).request('HEAD','Paperloom/v1/packages/'+'1'.repeat(64)+'.folio')).status,200);}
  finally{globalThis.fetch=original;}
});

test('legacy cache migration persists detached records separately and restores them after restart',async()=>{
  const original=globalThis.localStorage,values=new Map();
  globalThis.localStorage={getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value)};
  try {
    const paper=await fixture(),remote=new Remote(),owner=client(A,[paper]);await owner.engine.run(remote);
    const ghost=emptyState(B);ghost.books['old-qa']=structuredClone(owner.engine.catalog.books[paper.id]);
    ghost.books['old-qa'].bundle.hash='4'.repeat(64);ghost.books['old-qa'].bookmarks.saved={value:true,stamp:[Date.now(),0,B]};
    const {password,...safe}=config;values.set('paperloom-webdav-config-v1',JSON.stringify(safe));values.set('paperloom-sync-catalog-v1',JSON.stringify(ghost));
    const papers=[],reading={};
    const hooks={getPapers:()=>papers,getReading:()=>reading,server:()=>true,applyReading:value=>Object.assign(reading,value),putPaper:async(blob,id,hash)=>{const received=await decodePackage(await blob.arrayBuffer());received.syncHash=hash;papers.push(received);}};
    const controller=new SyncController(hooks);await controller.configure(config);controller.transport=()=>remote;
    assert.equal((await controller.synchronize()).downloaded,1);
    const restarted=new SyncController(hooks);
    assert.ok(!restarted.engine.catalog.books['old-qa']);assert.equal(restarted.engine.suspended.books['old-qa'].bookmarks.saved.value,true);
    assert.ok([...values].filter(([key])=>key.endsWith(':suspended')).some(([,value])=>JSON.parse(value).books['old-qa']));
    controller.cancel();restarted.cancel();
  }finally{globalThis.localStorage=original;}
});
test('two independent libraries exchange a real package, bookmark removals and reading progress',async()=>{
  const paper=await fixture(),remote=new Remote(),first=client(A,[paper]),second=client(B);
  await first.engine.imported(paper);const [one,two]=paper.document.blocks.slice(4,6).map(b=>b.id);
  first.engine.bookmark(paper.id,one,true);first.engine.progress(paper.id,{percent:35,block:one,mode:'translated'});
  assert.equal((await first.engine.run(remote)).uploaded,1);
  assert.equal((await second.engine.run(remote)).downloaded,1);
  assert.equal(second.papers[0].document.metadata.title,paper.document.metadata.title);
  second.engine.bookmark(paper.id,one,false);second.engine.progress(paper.id,{percent:75,block:two,mode:'bilingual'});
  first.engine.bookmark(paper.id,two,true);
  await second.engine.run(remote);await first.engine.run(remote);await second.engine.run(remote);
  assert.deepEqual(first.engine.catalog.books,second.engine.catalog.books);
  assert.deepEqual(first.reading[paper.id].bookmarks,[two]);assert.equal(first.reading[paper.id].percent,75);
  assert.equal(first.reading[paper.id].mode,'bilingual');
  assert.equal([...remote.files.keys()].filter(p=>p.endsWith('.folio')).length,1);
  assert.ok([...remote.files].filter(([path])=>path.startsWith('Paperloom/v1/devices/')).every(([,value])=>!value.data.includes('test-only-password')));
});

test('incoming cloud progress does not move an active reader while new bookmarks still arrive',async()=>{
  const paper=await fixture(),remote=new Remote(),owner=client(A,[paper]),receiver=client(B);
  await owner.engine.run(remote);await receiver.engine.run(remote);
  const [here,there]=paper.document.blocks.slice(4,6).map(block=>block.id),readingState=new ReadingState(receiver.reading);
  readingState.open(receiver.papers[0]);readingState.update(receiver.papers[0],receiver.papers[0],{percent:25,block:here,mode:'bilingual'});
  receiver.engine.hooks.applyReading=values=>{for(const [id,value] of Object.entries(values))readingState.receive(receiver.papers.find(p=>p.id===id),value);};
  owner.engine.progress(paper.id,{percent:88,block:there,mode:'original'});owner.engine.bookmark(paper.id,there,true);await owner.engine.run(remote);
  const result=await receiver.engine.run(remote);assert.equal(result.downloaded,0);
  assert.equal(readingState.live(paper.id).percent,25);assert.equal(readingState.live(paper.id).block,here);assert.equal(readingState.live(paper.id).mode,'bilingual');
  assert.deepEqual(readingState.live(paper.id).bookmarks,[there]);
  assert.equal(receiver.engine.catalog.books[paper.id].progress.percent,88);
});
test('concurrent windows preserve both updates when the server ignores conditions and ETags',async()=>{
  class Unconditional extends Remote {
    async request(method,path,headers,data,...rest){const result=await super.request(method,path,{},data,...rest);result.headers={};return result;}
  }
  const paper=await fixture(),remote=new Unconditional(),first=client(A,[paper]);await first.engine.imported(paper);await first.engine.run(remote);
  const sameDevice=client(A,[paper]);sameDevice.engine.catalog=structuredClone(first.engine.catalog);sameDevice.engine.observe(sameDevice.engine.catalog);
  const [one,two]=paper.document.blocks.slice(4,6).map(b=>b.id);
  first.engine.bookmark(paper.id,one,true);sameDevice.engine.bookmark(paper.id,two,true);
  await Promise.all([first.engine.run(remote),sameDevice.engine.run(remote)]);
  const third=client(B);await third.engine.run(remote);
  assert.equal(third.engine.catalog.books[paper.id].bookmarks[one].value,true);assert.equal(third.engine.catalog.books[paper.id].bookmarks[two].value,true);
  assert.ok([...remote.files.keys()].filter(path=>path.includes('/devices/')).every(path=>deviceFile(path.split('/').at(-1)).hash));
  const count=remote.files.size;await third.engine.run(remote);assert.equal(remote.files.size,count);
});
test('legacy device files are merged without modifying them',async()=>{
  const paper=await fixture(),remote=new Remote(),first=client(A,[paper]);await first.engine.imported(paper);
  const legacy=JSON.stringify(first.engine.catalog);remote.files.set('Paperloom/v1/devices/'+A+'.json',{data:legacy,etag:null});
  remote.files.set('Paperloom/v1/packages/'+paper.syncHash+'.folio',{data:paper.package,etag:null});
  const second=client(B);await second.engine.run(remote);assert.equal(second.papers.length,1);
  assert.equal(remote.files.get('Paperloom/v1/devices/'+A+'.json').data,legacy);
});
test('snapshot payload changes cannot enter a fresh receiving library',async()=>{
  const paper=await fixture(),remote=new Remote(),first=client(A,[paper]);await first.engine.imported(paper);await first.engine.run(remote);
  const path=[...remote.files.keys()].find(path=>path.includes('/devices/'));remote.files.get(path).data+=' ';
  const second=client(B);await assert.rejects(second.engine.run(remote),/快照的哈希/);assert.equal(second.stored,0);
});
test('corrupt packages never enter the receiving library or publish success',async()=>{
  const paper=await fixture(),remote=new Remote(),first=client(A,[paper]),second=client(B);await first.engine.imported(paper);await first.engine.run(remote);
  remote.corrupt=true;await assert.rejects(second.engine.run(remote),/哈希|大小/);
  assert.equal(second.stored,0);assert.equal(second.papers.length,0);assert.ok(![...remote.files.keys()].some(path=>path.includes('/devices/'+B+'.')));
});
test('local removal keeps the cloud copy and does not immediately redownload it',async()=>{
  const paper=await fixture(),remote=new Remote(),first=client(A,[paper]),second=client(B);await first.engine.imported(paper);await first.engine.run(remote);await second.engine.run(remote);
  second.engine.hide(paper.id);second.papers.length=0;await second.engine.run(remote);assert.equal(second.papers.length,0);
  second.engine.restoreHidden();await second.engine.run(remote);assert.equal(second.papers.length,1);
});

test('three cloud books with two or three local hides report the skipped books explicitly',async()=>{
  const papers=await Promise.all(['one','two','three'].map(id=>fixture('book-'+id))),remote=new Remote(),owner=client(A,papers);
  await owner.engine.run(remote);
  for(const count of [2,3]) {
    const receiver=client(B,[],{hidden:papers.slice(0,count).map(p=>p.id)});
    const result=await receiver.engine.run(remote);
    assert.equal(result.cloud,3);assert.equal(result.local,3-count);assert.equal(result.downloaded,3-count);
    assert.deepEqual(result.hidden.map(p=>p.id).sort(),papers.slice(0,count).map(p=>p.id).sort());
    assert.match(syncMessage(result),new RegExp(`云端 3 篇，本机 ${3-count} 篇`));
    assert.match(syncMessage(result),new RegExp(`${count} 篇在本机隐藏`));assert.match(syncMessage(result),/恢复并同步云端书库/);
    assert.ok(!syncMessage(result).startsWith('同步完成'));
    const repeated=await receiver.engine.run(remote);assert.equal(repeated.downloaded,0);assert.equal(repeated.hidden.length,count);
  }
});

test('one restore action recovers all three legacy-hidden books and stays restored after restart',async()=>{
  const original=globalThis.localStorage,values=new Map();
  globalThis.localStorage={getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value)};
  try {
    const documents=await Promise.all(['one','two','three'].map(id=>fixture('book-'+id))),remote=new Remote(),owner=client(A,documents);
    await owner.engine.run(remote);
    const block=documents[0].document.blocks[4].id;owner.engine.bookmark(documents[0].id,block,true);owner.engine.progress(documents[0].id,{percent:37,block,mode:'bilingual'});await owner.engine.run(remote);
    const {password,...safe}=config,scope='paperloom-sync-catalog-v2:'+JSON.stringify([safe.url,safe.folder,safe.username]);
    values.set('paperloom-webdav-config-v1',JSON.stringify(safe));values.set('paperloom-sync-active-scope-v2',JSON.stringify(scope));
    values.set('paperloom-sync-hidden-v1',JSON.stringify(documents.map(p=>p.id)));
    const papers=[],reading={};const hooks={getPapers:()=>papers,getReading:()=>reading,server:()=>true,applyReading:value=>Object.assign(reading,value),putPaper:async(blob,id,hash)=>{const paper=await decodePackage(await blob.arrayBuffer());assert.equal(paper.id,id);paper.syncHash=hash;papers.push(paper);}};
    const controller=new SyncController(hooks);await controller.configure(config);controller.transport=()=>remote;
    assert.equal((await controller.synchronize()).downloaded,0);assert.equal(controller.engine.hidden.size,3);
    const restored=await controller.restoreAndSynchronize();assert.equal(restored.downloaded,3);assert.equal(restored.local,3);assert.equal(restored.hidden.length,0);
    assert.deepEqual(reading[documents[0].id].bookmarks,[block]);assert.equal(reading[documents[0].id].percent,37);assert.equal(reading[documents[0].id].mode,'bilingual');
    assert.deepEqual(JSON.parse(values.get(scope+':hidden')),[]);
    const restarted=new SyncController(hooks);await restarted.configure(config);restarted.transport=()=>remote;
    assert.equal(restarted.engine.hidden.size,0);assert.equal((await restarted.synchronize()).local,3);
    controller.cancel();restarted.cancel();
  }finally{globalThis.localStorage=original;}
});

test('local hides are scoped by DAV connection and resume only in their original library',async()=>{
  const original=globalThis.localStorage,values=new Map();
  globalThis.localStorage={getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value)};
  try {
    const paper=await fixture(),papers=[paper],hooks={getPapers:()=>papers,getReading:()=>({}),server:()=>true,applyReading:()=>{}};
    const controller=new SyncController(hooks);await controller.configure(config);
    controller.engine.hide(paper.id);papers.length=0;
    await controller.configure({...config,folder:'AnotherLibrary'});assert.equal(controller.engine.hidden.size,0);
    await controller.configure(config);assert.ok(controller.engine.hidden.has(paper.id));
    await controller.disconnect();const restarted=new SyncController(hooks);
    await restarted.configure({...config,username:'another-account'});assert.equal(restarted.engine.hidden.size,0);
    await restarted.configure(config);assert.ok(restarted.engine.hidden.has(paper.id));
    restarted.engine.restoreHidden();await restarted.configure({...config,folder:'AnotherLibrary'});await restarted.configure(config);assert.equal(restarted.engine.hidden.size,0);
    controller.cancel();restarted.cancel();
  }finally{globalThis.localStorage=original;}
});

test('legacy global hides are migrated once to the last known scope, never into a different one',async()=>{
  const original=globalThis.localStorage,values=new Map();
  globalThis.localStorage={getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value)};
  try {
    const {password,...safe}=config,oldScope='paperloom-sync-catalog-v2:'+JSON.stringify([safe.url,'Previous',safe.username]);
    values.set('paperloom-webdav-config-v1',JSON.stringify(safe));values.set('paperloom-sync-active-scope-v2',JSON.stringify(oldScope));values.set('paperloom-sync-hidden-v1',JSON.stringify(['book-one']));
    const hooks={getPapers:()=>[],getReading:()=>({}),server:()=>true,applyReading:()=>{}};
    const controller=new SyncController(hooks);assert.equal(controller.engine.hidden.size,0);
    assert.deepEqual(JSON.parse(values.get(oldScope+':hidden')),['book-one']);
    await controller.configure({...config,folder:'Previous'});assert.ok(controller.engine.hidden.has('book-one'));
    controller.engine.restoreHidden();await controller.configure(config);controller.cancel();
    const restarted=new SyncController(hooks);await restarted.configure({...config,folder:'Previous'});assert.equal(restarted.engine.hidden.size,0);restarted.cancel();
  }finally{globalThis.localStorage=original;}
});

test('restoring while sync is busy cannot discard hides or initiate a second transfer',async()=>{
  const original=globalThis.localStorage,values=new Map();
  globalThis.localStorage={getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value)};
  try {
    const controller=new SyncController({getPapers:()=>[],getReading:()=>({}),server:()=>true,applyReading:()=>{}});
    controller.engine.hide('book-one');await assert.rejects(controller.restoreAndSynchronize(),/先配置/);assert.ok(controller.engine.hidden.has('book-one'));
    await controller.configure(config);controller.engine.hide('book-one');controller.engine.running=true;
    await assert.rejects(controller.restoreAndSynchronize(),/同步正在进行/);assert.ok(controller.engine.hidden.has('book-one'));
    controller.engine.running=false;controller.cancel();
  }finally{globalThis.localStorage=original;}
});

test('sync inventory excludes teaching demos and distinguishes an empty cloud from completed downloads',async()=>{
  const teaching=await decodePackage(await readFile(new URL('../web/dist/demo/demo.folio',import.meta.url))),empty=client(B,[teaching]);
  const result=await empty.engine.run(new Remote());assert.equal(result.cloud,0);assert.equal(result.local,0);assert.equal(result.downloaded,0);
  assert.match(syncMessage(result),/云端书库暂无文献/);assert.match(syncMessage(result),/PC 书库/);
});
test('WebDAV hrefs cannot escape the configured directory or send credentials to another host',()=>{
  const root='https://example.test/dav/Paperloom/v1/devices/';
  const xml=`<d:multistatus xmlns:d="DAV:"><d:response><d:href>${root}${A}.json</d:href></d:response><d:response><d:href>https://evil.test/${B}.json</d:href></d:response><d:response><d:href>../${B}.json</d:href></d:response><d:response><d:href>%2e%2e%2f${B}.json</d:href></d:response></d:multistatus>`;
  assert.deepEqual(deviceFiles(xml,root),[A+'.json']);assert.throws(()=>deviceFiles('<!DOCTYPE x><d:multistatus/>',root));
  const hash='1'.repeat(64),name=A+'.'+hash+'.json';assert.deepEqual(deviceFile(name),{device:A,hash});
  assert.deepEqual(deviceFiles(`<d:multistatus xmlns:d="DAV:"><d:response><d:href>${root}${name}</d:href></d:response></d:multistatus>`,root),[name]);
  assert.throws(()=>davConfig({...config,url:'https://reader:password@example.test/'}));assert.throws(()=>davConfig({...config,url:'http://example.test/'}));
  assert.throws(()=>davConfig({...config,folder:'../outside'}));
});
test('state validation rejects prototype keys, invalid clocks and unsupported formats',()=>{
  assert.throws(()=>validateState({version:2,device:A,books:{}}));assert.throws(()=>validateState({version:1,device:A,books:123}));
  assert.throws(()=>validateState(JSON.parse(`{"version":1,"device":"${A}","books":{"__proto__":{}}}`)));
});
test('cancelled synchronization never publishes a device snapshot',async()=>{
  const paper=await fixture(),remote=new Remote(),first=client(A,[paper]);await first.engine.imported(paper);
  const abort=new AbortController();abort.abort();await assert.rejects(first.engine.run(remote,abort.signal));
  assert.ok(![...remote.files.keys()].some(path=>path.includes('/devices/'+A+'.')));
});

test('automatic scheduling survives a manual sync and changing the configuration',async()=>{
  const old={storage:globalThis.localStorage,document:globalThis.document,set:globalThis.setTimeout,clear:globalThis.clearTimeout};
  const values=new Map(),timers=new Map();let next=0;
  globalThis.localStorage={getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value)};
  globalThis.document={visibilityState:'visible'};
  globalThis.setTimeout=(fn,delay)=>{const id=++next;timers.set(id,{fn,delay});return id;};
  globalThis.clearTimeout=id=>timers.delete(id);
  try {
    const controller=new SyncController({getPapers:()=>[],getReading:()=>({}),server:()=>false,applyReading:()=>{}});
    await controller.configure({...config,auto:true});assert.equal([...timers.values()][0].delay,2500);
    controller.engine.run=async()=>({uploaded:0,downloaded:0});await controller.synchronize();
    assert.equal(timers.size,1);assert.equal([...timers.values()][0].delay,60000);
    await controller.configure({...config,auto:true});assert.equal(timers.size,1);assert.equal([...timers.values()][0].delay,2500);
    await controller.disconnect();assert.equal(timers.size,0);
  }finally{globalThis.localStorage=old.storage;globalThis.document=old.document;globalThis.setTimeout=old.set;globalThis.clearTimeout=old.clear;}
});

test('changing WebDAV scope isolates remote records and restores each scope when reconnected',async()=>{
  const old=globalThis.localStorage,values=new Map();
  globalThis.localStorage={getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value)};
  try {
    const paper=await fixture(),papers=[paper],reading={};
    const controller=new SyncController({getPapers:()=>papers,getReading:()=>reading,server:()=>true,applyReading:()=>{}});
    await controller.configure(config);
    const block=paper.document.blocks[4].id;controller.engine.bookmark(paper.id,block,true);
    controller.engine.catalog.books['other-cloud-only']=structuredClone(controller.engine.catalog.books[paper.id]);controller.engine.persist();
    await controller.configure({...config,folder:'Other'});
    assert.ok(!controller.engine.catalog.books['other-cloud-only']);assert.ok(controller.engine.catalog.books[paper.id]);
    assert.deepEqual(controller.engine.catalog.books[paper.id].bookmarks,{});
    await controller.configure(config);assert.ok(controller.engine.catalog.books['other-cloud-only']);
    assert.equal(controller.engine.catalog.books[paper.id].bookmarks[block].value,true);
    await controller.disconnect();
    const reloaded=new SyncController({getPapers:()=>papers,getReading:()=>reading,server:()=>true,applyReading:()=>{}});
    await reloaded.configure({...config,folder:'Fresh'});assert.ok(!reloaded.engine.catalog.books['other-cloud-only']);
    assert.ok([...values.entries()].every(([key,value])=>!key.includes('test-only-password')&&!value.includes('test-only-password')));
  }finally{globalThis.localStorage=old;}
});
