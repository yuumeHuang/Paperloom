import { Capacitor, registerPlugin, SystemBars } from './vendor/capacitor.js';
export const isAndroid = Capacitor.getPlatform()==='android';
const Native=registerPlugin('PaperloomNative');
const CHUNK=512*1024;
function base64(bytes) {
  let text='';for(let i=0;i<bytes.length;i+=8192)text+=String.fromCharCode(...bytes.subarray(i,i+8192));
  return btoa(text);
}
export async function stageBlob(blob,signal) {
  const {token}=await Native.createFile();
  try {
    for(let offset=0;offset<blob.size;offset+=CHUNK) {
      signal?.throwIfAborted();
      await Native.writeChunk({token,offset,data:base64(new Uint8Array(await blob.slice(offset,offset+CHUNK).arrayBuffer()))});
    }
    return token;
  } catch(error) { await Native.releaseFile({token}).catch(()=>{});throw error; }
}
async function readFile(file,signal) {
  try {
    const parts=[];
    for(let offset=0;offset<file.size;offset+=CHUNK) {
      signal?.throwIfAborted();
      const result=await Native.readChunk({token:file.token,offset,length:CHUNK});
      const text=atob(result.data);parts.push(Uint8Array.from(text,c=>c.charCodeAt(0)));
    }
    return new Blob(parts,{type:'application/zip'});
  } finally {await Native.releaseFile({token:file.token}).catch(()=>{});}
}
export async function pickNativePackage() { return readFile(await Native.pickFile()); }
export async function exportNativePackage(blob,name) {
  const token=await stageBlob(blob);
  try {return await Native.exportFile({token,name});}finally{await Native.releaseFile({token}).catch(()=>{});}
}
export async function nativeRequest(config,method,url,headers,data,signal,binary=false) {
  signal?.throwIfAborted();
  const options={url,method,headers,username:config.username,password:config.password,allowHttp:config.allowHttp};
  if (binary && method==='GET') {
    const result=await Native.download(options);
    return {status:result.status,headers:result.headers,blob:result.token?await readFile(result,signal):null};
  }
  if (data instanceof Blob) {
    const token=await stageBlob(data,signal);
    try {return await Native.upload({...options,token});}finally{await Native.releaseFile({token}).catch(()=>{});}
  }
  return Native.request({...options,data:data||''});
}
export const cancelNativeRequests=()=>Native.cancelRequests();
export const saveNativeSecret=value=>Native.saveSecret({value:JSON.stringify(value)});
export async function loadNativeSecret() { const {value}=await Native.loadSecret();return value?JSON.parse(value):null; }
export const clearNativeSecret=()=>Native.clearSecret();
export const backgroundNativeApp=()=>Native.moveToBackground();
export async function nativeTheme(night) {
  await SystemBars.setStyle({style:night?'DARK':'LIGHT'});
  await Native.setReaderBackground({night});
}
