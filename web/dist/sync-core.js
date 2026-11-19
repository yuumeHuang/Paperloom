export const SYNC_VERSION = 1;
export const MAX_PACKAGE = 180 * 1024 * 1024;
const idValid = id => typeof id === 'string' && id.length > 0 && id.length <= 120 && !/[\x00-\x1f]/.test(id) && !Object.hasOwn(Object.prototype,id) && id!=='prototype';
export const deviceValid = id => /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(id || '');
export const hashValid = hash => /^[a-f0-9]{64}$/.test(hash || '');
export function emptyState(device) { return { version: SYNC_VERSION, device, books: {} }; }
export function compareStamp(a,b) {
  if (!a) return b ? -1 : 0;
  if (!b) return 1;
  return a[0]-b[0] || a[1]-b[1] || a[2].localeCompare(b[2]);
}
function validStamp(value) {
  return Array.isArray(value) && value.length===3 && Number.isSafeInteger(value[0]) && value[0]>0 && value[0]<=Date.now()+86400000 && Number.isSafeInteger(value[1]) && value[1]>=0 && value[1]<1000000000 && deviceValid(value[2]);
}
export function validateState(value) {
  if (!value || value.version!==SYNC_VERSION || !deviceValid(value.device) || !value.books || typeof value.books!=='object' || Array.isArray(value.books) || Object.keys(value.books).length>2000) throw new Error('云端同步状态格式无效或超限');
  const clean=emptyState(value.device);
  for (const [id,book] of Object.entries(value.books)) {
    if (!idValid(id) || !book || !book.bundle || !validStamp(book.bundle.stamp) || !hashValid(book.bundle.hash) || !Number.isSafeInteger(book.bundle.size) || book.bundle.size<=0 || book.bundle.size>MAX_PACKAGE || typeof book.bundle.title!=='string' || book.bundle.title.length>800) throw new Error('云端文献记录无效');
    const out={bundle:{hash:book.bundle.hash,size:book.bundle.size,title:book.bundle.title,stamp:book.bundle.stamp},bookmarks:{}};
    if (book.progress) {
      const p=book.progress;
      if (!validStamp(p.stamp) || !Number.isFinite(p.percent) || p.percent<0 || p.percent>100 || !(p.block===null || idValid(p.block)) || !['translated','original','bilingual'].includes(p.mode)) throw new Error('云端阅读进度无效');
      out.progress={percent:p.percent,block:p.block,mode:p.mode,stamp:p.stamp};
    }
    if (!book.bookmarks || typeof book.bookmarks!=='object' || Array.isArray(book.bookmarks) || Object.keys(book.bookmarks).length>10000) throw new Error('云端书签记录超限');
    for (const [block,mark] of Object.entries(book.bookmarks)) {
      if (!idValid(block) || !mark || typeof mark.value!=='boolean' || !validStamp(mark.stamp)) throw new Error('云端书签记录无效');
      out.bookmarks[block]={value:mark.value,stamp:mark.stamp};
    }
    clean.books[id]=out;
  }
  return clean;
}
function later(a,b) {
  const difference=compareStamp(a?.stamp,b?.stamp);
  if (difference) return difference>0?a:b;
  if(typeof a?.value==='boolean'&&typeof b?.value==='boolean')return a.value===false?a:b;
  // Deterministic tie breaking also makes corrupted/duplicated snapshots converge.
  return JSON.stringify(a||null)>=JSON.stringify(b||null)?a:b;
}
export function mergeStates(local,remote) {
  const result=structuredClone(validateState(local));
  const other=validateState(remote);
  for (const [id,book] of Object.entries(other.books)) {
    if (!result.books[id]) { result.books[id]=structuredClone(book);continue; }
    const current=result.books[id];
    current.bundle=structuredClone(later(current.bundle,book.bundle));
    const progress=later(current.progress,book.progress);
    if (progress) current.progress=structuredClone(progress);
    for (const [block,mark] of Object.entries(book.bookmarks)) current.bookmarks[block]=structuredClone(later(current.bookmarks[block],mark));
  }
  return validateState(result);
}
export function readingFor(book) {
  return { ...(book.progress?{percent:book.progress.percent,block:book.progress.block,mode:book.progress.mode}:{}), bookmarks:Object.entries(book.bookmarks||{}).filter(([,value])=>value.value).map(([id])=>id) };
}
export function syncMessage(summary) {
  const counts=`上传 ${summary.uploaded} 篇，下载 ${summary.downloaded} 篇`;
  const inventory=Number.isInteger(summary.cloud)&&Number.isInteger(summary.local)?`云端 ${summary.cloud} 篇，本机 ${summary.local} 篇；`:'';
  const hidden=summary.hidden?.length?`；${summary.hidden.length} 篇在本机隐藏，可点“恢复并同步云端书库”取回。`:'';
  if(summary.missing?.length)return `已同步可用文献：${inventory}${counts}；${summary.missing.length} 篇云端文献包暂不可用，可从保存文献的设备再次同步补回。${hidden}`;
  if(hidden)return `同步结果：${inventory}${counts}${hidden}`;
  if(summary.cloud===0&&summary.local===0)return `云端书库暂无文献：${counts}。请先在 PC 书库同步准备好的文献。`;
  return `同步完成：${inventory}${counts}`;
}
export function davConfig(value) {
  const raw=String(value.url||'').trim();
  let url;
  try { url=new URL(raw); } catch { throw new Error('请填写完整 WebDAV 地址'); }
  if (!['https:','http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || /[\x00-\x20\\]/.test(raw)) throw new Error('WebDAV 地址不能包含登录信息、查询参数或片段');
  if (url.protocol==='http:' && !value.allowHttp) throw new Error('请使用 HTTPS；局域网 HTTP 需要主动允许明文连接');
  url.pathname=url.pathname.replace(/\/*$/,'/');
  const folder=String(value.folder||'Paperloom').trim();
  if (folder.length>160 || folder.split('/').some(p=>!p || ['.','..'].includes(p) || /[\\:%\x00-\x1f]/.test(p))) throw new Error('同步目录必须是相对目录名，不能包含 .. 或特殊路径字符');
  if (String(value.username||'').length>500 || String(value.password||'').length>2000) throw new Error('登录信息过长');
  return {url:url.href,folder,username:String(value.username||''),password:String(value.password||''),allowHttp:!!value.allowHttp,auto:!!value.auto,remember:!!value.remember};
}
export function resourceUrl(config,path='') {
  if (path && path.split('/').some(p=>!p || ['.','..'].includes(p) || /[\\:%\x00-\x1f]/.test(p))) throw new Error('同步资源路径无效');
  return new URL(path.split('/').map(encodeURIComponent).join('/'),config.url).href;
}
export function syncRoot(config) { return config.folder+'/v1'; }
export function syncFolders(config) {
  const result=[];let path='';
  for(const part of [...config.folder.split('/'),'v1']){path+=(path?'/':'')+part;result.push(path);}
  return [...result,syncRoot(config)+'/packages',syncRoot(config)+'/devices'];
}
export function collectionResponse(xml) {
  if(typeof xml!=='string' || xml.length>2*1024*1024 || /<!DOCTYPE|<!ENTITY/i.test(xml))throw new Error('WebDAV 目录响应无效或超限');
  const document=new DOMParser().parseFromString(xml,'application/xml');
  if(document.getElementsByTagName('parsererror').length || document.documentElement?.localName!=='multistatus')throw new Error('服务未返回有效的 WebDAV 目录');
  return [...document.getElementsByTagNameNS('DAV:','propstat')].some(prop=>/\s200(?:\s|$)/.test(prop.getElementsByTagNameNS('DAV:','status')[0]?.textContent||'') && prop.getElementsByTagNameNS('DAV:','collection').length>0);
}
export function deviceFile(name) {
  if(typeof name!=='string')return null;
  const match=/^([a-f0-9-]{36})(?:\.([a-f0-9]{64}))?\.json$/.exec(name);
  return match&&deviceValid(match[1])?{device:match[1],hash:match[2]||null}:null;
}
export function deviceFiles(xml,collectionUrl) {
  if (typeof xml!=='string' || xml.length>2*1024*1024 || /<!DOCTYPE|<!ENTITY/i.test(xml)) throw new Error('WebDAV 目录响应无效或超限');
  const document=new DOMParser().parseFromString(xml,'application/xml');
  if (document.getElementsByTagName('parsererror').length || document.documentElement?.localName!=='multistatus') throw new Error('服务未返回有效的 WebDAV 目录');
  const root=new URL(collectionUrl.endsWith('/')?collectionUrl:collectionUrl+'/'),files=new Set();
  for (const response of document.getElementsByTagNameNS('DAV:','response')) {
    const href=response.getElementsByTagNameNS('DAV:','href')[0]?.textContent;
    if (!href) continue;
    let url;try {url=new URL(href,root);} catch {continue;}
    if (url.origin!==root.origin || url.search || url.hash || !url.pathname.startsWith(root.pathname)) continue;
    let name;try {name=decodeURIComponent(url.pathname.slice(root.pathname.length));}catch{continue;}
    if (deviceFile(name)) files.add(name);
  }
  if (files.size>2000) throw new Error('同步快照超过 2,000 份，请整理同步目录');
  if (new Set([...files].map(name=>deviceFile(name).device)).size>100) throw new Error('同步设备超过 100 个，请整理同步目录');
  return [...files].sort();
}
export async function blobHash(blob) {
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await blob.arrayBuffer())),b=>b.toString(16).padStart(2,'0')).join('');
}
export function authHeader(username,password) {
  const bytes=new TextEncoder().encode(username+':'+password);
  return 'Basic '+btoa(Array.from(bytes,b=>String.fromCharCode(b)).join(''));
}
