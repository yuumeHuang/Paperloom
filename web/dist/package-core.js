import { unzipSync } from './vendor/fflate.js';

async function unpackBytes(bytes) {
  if (typeof Worker !== 'function') return unzipSync(bytes);
  return new Promise((resolve,reject) => {
    const worker = new Worker(new URL('./package-worker.js',import.meta.url),{type:'module'});
    worker.onmessage = event => {
      worker.terminate();
      if (event.data?.error) reject(new Error('文献包解压失败'));
      else resolve(event.data.files);
    };
    worker.onerror = () => { worker.terminate(); reject(new Error('文献包解压线程不可用')); };
    const copy = bytes.slice();
    worker.postMessage(copy.buffer,[copy.buffer]);
  });
}

export function safePath(name) {
  return typeof name === 'string' && name.length > 0 && !name.startsWith('/') && !/[\\:\x00]/.test(name) && name.split('/').every(p => p && !['..', '.', '__proto__', 'prototype', 'constructor'].includes(p));
}

export function inspectZip(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let end = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65557); i--) {
    if (view.getUint32(i, true) === 0x06054b50 && i + 22 + view.getUint16(i + 20, true) === bytes.length) { end = i; break; }
  }
  if (end < 0 || view.getUint16(end + 4, true) || view.getUint16(end + 6, true)) throw new Error('无效或分卷 ZIP 文献包');
  const count = view.getUint16(end + 10, true), size = view.getUint32(end + 12, true), offset = view.getUint32(end + 16, true);
  if (count > 3000 || offset + size > end || offset + size > bytes.length) throw new Error('文献包文件数量或目录超限');
  let cursor = offset, total = 0;
  const entries = new Map(), decoder = new TextDecoder();
  for (let i = 0; i < count; i++) {
    if (cursor + 46 > end || view.getUint32(cursor, true) !== 0x02014b50) throw new Error('文献包目录损坏');
    const flags = view.getUint16(cursor + 8, true), method = view.getUint16(cursor + 10, true);
    const originalSize = view.getUint32(cursor + 24, true), nameSize = view.getUint16(cursor + 28, true);
    const extra = view.getUint16(cursor + 30, true), comment = view.getUint16(cursor + 32, true);
    const next = cursor + 46 + nameSize + extra + comment;
    if (next > end || flags & 1 || ![0, 8].includes(method) || originalSize > 100 * 1024 * 1024) throw new Error('不支持的压缩方式或文件过大');
    const name = decoder.decode(bytes.subarray(cursor + 46, cursor + 46 + nameSize));
    if (!safePath(name) || entries.has(name)) throw new Error('文献包路径无效或重复');
    total += originalSize;
    if (total > 400 * 1024 * 1024) throw new Error('文献包解压大小超过 400 MB');
    entries.set(name, originalSize);
    cursor = next;
  }
  if (cursor !== offset + size) throw new Error('ZIP 目录长度不一致');
  if (!entries.has('manifest.json') || entries.get('manifest.json') > 512 * 1024 || !entries.has('document.json') || entries.get('document.json') > 20 * 1024 * 1024) throw new Error('文献包缺少清单或清单过大');
  return entries;
}

export function validateDocument(doc) {
  if (!doc || doc.format !== 'paperloom' || doc.version !== '1.0' || typeof doc.id!=='string' || !doc.id || ['__proto__','constructor','prototype'].includes(doc.id) || !doc.metadata || typeof doc.metadata.title !== 'string') throw new Error('不支持的文献格式或版本');
  if (!Array.isArray(doc.blocks) || !doc.blocks.length || doc.blocks.length > 30000 || !Array.isArray(doc.assets) || !Array.isArray(doc.pages) || !Array.isArray(doc.toc) || !Array.isArray(doc.issues)) throw new Error('文献结构无效');
  const m = doc.metadata;
  if (!Array.isArray(m.authors) || m.authors.some(a => typeof a !== 'string') || typeof m.title_translated !== 'string' || typeof m.target_language !== 'string') throw new Error('文献元信息无效');
  const validBox = (bbox, page) => Array.isArray(bbox) && bbox.length === 4 && bbox.every(Number.isFinite) && bbox[0] >= 0 && bbox[1] >= 0 && bbox[0] < bbox[2] && bbox[1] < bbox[3] && bbox[2] <= page.width + .1 && bbox[3] <= page.height + .1;
  const pageMap = new Map(doc.pages.map(p => [p.number, p]));
  for (const p of doc.pages) if (!Number.isInteger(p.number) || p.number < 1 || !Number.isFinite(p.width) || !Number.isFinite(p.height) || p.width <= 0 || p.height <= 0) throw new Error('来源页尺寸无效');
  const bids = new Set(doc.blocks.map(b => b.id)), aids = new Set(doc.assets.map(a => a.id)), pages = new Set(doc.pages.map(p => p.number));
  if (bids.size !== doc.blocks.length || aids.size !== doc.assets.length) throw new Error('文献包含重复 ID');
  if(doc.reading_sections!==undefined){
    if(!Array.isArray(doc.reading_sections)||doc.reading_sections.length>40)throw new Error('阅读分组无效');
    const seen=new Set(),groups=new Set();
    for(const s of doc.reading_sections){if(!s||typeof s.id!=='string'||!/^[a-z_]{1,80}$/.test(s.id)||groups.has(s.id)||typeof s.title!=='string'||typeof s.translated!=='string'||s.collapsed!==true||!Array.isArray(s.block_ids)||!s.block_ids.length)throw new Error('阅读分组字段无效');groups.add(s.id);for(const id of s.block_ids){if(!bids.has(id)||seen.has(id))throw new Error('阅读分组包含未知或重复段落');seen.add(id);}}
  }
  if(doc.block_aliases!==undefined&&(!doc.block_aliases||Array.isArray(doc.block_aliases)||typeof doc.block_aliases!=='object'||Object.values(doc.block_aliases).some(id=>!bids.has(id))))throw new Error('段落别名无效');
  for (const b of doc.blocks) {
    if (!b.id || typeof b.original !== 'string' || typeof b.translated !== 'string' || !Array.isArray(b.source) || !b.source.length || b.source.some(s => !pages.has(s.page) || !validBox(s.bbox, pageMap.get(s.page)))) throw new Error('段落来源或文字无效');
    if (!Array.isArray(b.styles) || b.styles.some(s => typeof s.text !== 'string')) throw new Error('文字样式无效');
    if(b.translated_styles!==undefined&&(!Array.isArray(b.translated_styles)||b.translated_styles.some(s=>typeof s.text!=='string')))throw new Error('译文样式无效');
    if (b.asset_id && !aids.has(b.asset_id)) throw new Error('段落绑定的图表不存在');
    for (const [language, refs] of Object.entries(b.references || {})) {
      const length = Array.from(language === 'original' ? b.original : b.translated).length;
      if (!Array.isArray(refs) || refs.some(r => !aids.has(r.asset_id) || !Number.isInteger(r.start) || !Number.isInteger(r.end) || r.start < 0 || r.end <= r.start || r.end > length)) throw new Error('图表引用区间无效');
    }
  }
  for (const a of doc.assets) {
    if (!safePath(a.path) || !a.path.startsWith('assets/') || !pages.has(a.page) || !validBox(a.bbox, pageMap.get(a.page))) throw new Error('图表路径或来源无效');
    if (a.table !== null && a.table !== undefined && (!Array.isArray(a.table) || a.table.some(row => !Array.isArray(row) || row.some(cell => cell !== null && typeof cell !== 'string')))) throw new Error('表格数据无效');
  }
  for (const p of doc.pages) if (!safePath(p.preview) || !p.preview.startsWith('pages/')) throw new Error('来源页路径无效');
  for (const t of doc.toc) if (!bids.has(t.block_id)||!Number.isInteger(t.level)||t.level<1||t.level>6||typeof t.title!=='string'||typeof t.translated!=='string') throw new Error('目录目标或层级无效');
  return doc;
}

export async function digest(bytes) {
  if (!globalThis.crypto?.subtle) throw new Error('导入文献包需要 HTTPS 或 localhost，以校验文件完整性');
  const hash = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(hash), n => n.toString(16).padStart(2, '0')).join('');
}

export async function decodePackage(buffer) {
  const bytes = buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer);
  if (bytes.length > 180 * 1024 * 1024) throw new Error('文献包最大 180 MB');
  const entries = inspectZip(bytes);
  const files = await unpackBytes(bytes);
  const decoder = new TextDecoder(), manifest = JSON.parse(decoder.decode(files['manifest.json']));
  if (manifest.format !== 'paperloom' || manifest.version !== '1.0' || manifest.document !== 'document.json' || !Array.isArray(manifest.files)) throw new Error('文献包清单无效');
  const listed = new Set(manifest.files.map(f => f.path));
  if (listed.size !== manifest.files.length || listed.size + 1 !== entries.size || !listed.has('document.json')) throw new Error('文献包文件列表不一致');
  for (const item of manifest.files) {
    const data = files[item.path];
    if (!safePath(item.path) || !data || data.length !== item.size || entries.get(item.path) !== item.size || await digest(data) !== item.sha256) throw new Error('文献包完整性校验失败：' + item.path);
  }
  const document = validateDocument(JSON.parse(decoder.decode(files['document.json'])));
  if (document.id !== manifest.id) throw new Error('文献包 ID 不一致');
  const required = ['source.pdf', ...document.assets.map(a => a.path), ...document.pages.map(p => p.preview)];
  if (required.some(p => !listed.has(p))) throw new Error('文献包缺少图片或来源页');
  const blobs = {};
  for (const item of manifest.files) {
    const mime = item.path.endsWith('.png') ? 'image/png' : item.path.endsWith('.pdf') ? 'application/pdf' : 'application/json';
    blobs[item.path] = new Blob([files[item.path]], { type: mime });
  }
  return { id: document.id, document, manifest, files: blobs, package: new Blob([bytes], { type: 'application/zip' }), importedAt: Date.now() };
}

export function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export function linkedText(text, refs = [], styles = []) {
  const chars = Array.from(text), sorted = [...refs].sort((a, b) => a.start - b.start);
  const ranges = [];
  let scan = 0;
  for (const style of styles) {
    if (!style.text) continue;
    const found = text.indexOf(style.text, scan);
    if (found < 0) continue;
    scan = found + style.text.length;
    if (style.bold || style.italic || style.superscript) ranges.push({ start: Array.from(text.slice(0, found)).length, end: Array.from(text.slice(0, scan)).length, ...style });
  }
  const format = (start, end) => {
    const boundaries = new Set([start, end]);
    for (const r of ranges) { if (r.start > start && r.start < end) boundaries.add(r.start); if (r.end > start && r.end < end) boundaries.add(r.end); }
    const points = [...boundaries].sort((a, b) => a - b);
    let html = '';
    for (let i = 0; i < points.length - 1; i++) {
      const a = points[i], b = points[i + 1], style = ranges.find(r => r.start <= a && r.end >= b);
      let part = escapeHtml(chars.slice(a, b).join(''));
      if (style?.italic) part = `<em>${part}</em>`;
      if (style?.bold) part = `<strong>${part}</strong>`;
      if (style?.superscript) part = `<sup>${part}</sup>`;
      html += part;
    }
    return html;
  };
  let cursor = 0, html = '';
  for (const r of sorted) {
    if (r.start < cursor || r.end > chars.length) continue;
    html += format(cursor, r.start);
    html += `<button class="reference" data-asset="${escapeHtml(r.asset_id)}" aria-label="打开 ${escapeHtml(r.label)}">${format(r.start, r.end)}</button>`;
    cursor = r.end;
  }
  return html + format(cursor, chars.length);
}
