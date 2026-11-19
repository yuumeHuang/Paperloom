import { decodePackage, escapeHtml as e, linkedText } from './package-core.js';
import { allPapers, putPaper, removePaper } from './store.js';
import { SyncController } from './sync.js';
import {syncMessage} from './sync-core.js';
import { isAndroid, pickNativePackage, exportNativePackage, backgroundNativeApp, nativeTheme } from './native.js';
import {readingModel,aliasBlock,isSectionHeading} from './reading-core.js';
import {ProviderEditor} from './providers.js';
import {backAction} from './navigation-core.js';
import {ReadingState} from './reader-state.js';

const $ = id => document.getElementById(id);
const defaults = { base_url: 'https://api.openai.com/v1', layout_model: '', translation_model: '', target_language: '简体中文', max_output_tokens: 8192, vision: false, json_mode: true, token_parameter: 'max_tokens', thinking_mode: 'auto', glossary: {}, fallbacks: [], retry_attempts: 3 };
const state = { papers: [], current: null, mode: 'translated', overrides: {}, server: false, tokenRequired: false, jobs: [], view: 'library', filterStar: false, selectedPdf: null, urls: new Map(), jobId: null, revision: 0, openSections: new Set(), figures: false, find: [], findIndex: 0 };
let apiKey = '', apiKeyUrl = '', accessToken = '', toastTimer, pollTimer;
let discoveryTimer, discoveryController, discoveryIdentity = '', discoveredModels = [];
function readLocal(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } }
let settings = { ...defaults, ...readLocal('paperloom-settings', {}) };
let reading = readLocal('paperloom-reading', {});
const readingState=new ReadingState(reading);
const providerEditor=new ProviderEditor($('providerList'),{api,server:()=>state.server,primary:()=>$('settingsDialog').open?({...connectionConfig(),vision:$('settingsForm').elements.vision.checked,max_output_tokens:Number($('settingsForm').elements.max_output_tokens.value)}):({...settings,api_key:apiKey})});
let pauseProgressUntil = 0;
const sync = new SyncController({
  getPapers:()=>state.papers,getReading:()=>reading,server:()=>state.server,token:()=>accessToken,
  putPaper:async (blob,id,hash)=>{
    const paper=await decodePackage(await blob.arrayBuffer());
    if(paper.id!==id)throw new Error('云端文献 ID 与包内 ID 不一致');
    paper.syncHash=hash;const previous=state.papers.find(p=>p.id===id);if(previous?.jobId)paper.jobId=previous.jobId;
    await putPaper(paper);state.papers=state.papers.filter(p=>p.id!==id);state.papers.push(paper);
  },
  applyReading:updates=>{
    for(const [id,value] of Object.entries(updates)){
      const paper=state.papers.find(p=>p.id===id);if(!paper)continue;
      readingState.receive(paper,value);
    }
    saveReading();renderLibrary();
    if(state.view==='reader'&&readingState.isCurrent(state.papers.find(p=>p.id===state.current.id)))refreshReaderBookmarks();
  },
  status:message=>{$('syncSummary').textContent=message;$('syncActivity').textContent=message;renderSyncRecovery();}
});
function saveReading() { try { localStorage.setItem('paperloom-reading', JSON.stringify(reading)); } catch { /* Reading still works if preferences cannot be stored. */ } }
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 4500); }
function report(error) { toast(error?.message || String(error)); }
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (accessToken) headers.set('Authorization', 'Bearer ' + accessToken);
  if (options.json !== undefined) { headers.set('Content-Type', 'application/json'); options.body = JSON.stringify(options.json); delete options.json; }
  const response = await fetch('/api/' + path, { ...options, headers, cache: 'no-store' });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(typeof data.detail === 'string' ? data.detail : `准备端请求失败 (${response.status})`);
  }
  return response;
}
function getConfig() { return { ...settings, fallbacks:providerEditor.runtime(settings.fallbacks||[]), api_key:apiKeyUrl===settings.base_url?apiKey:'', enabled: $('useLLM').checked }; }
function bookmarksFor(id,live=false) { return (live?readingState.live(id):reading[id])?.bookmarks || []; }
function activeDocument() { return state.current?.document; }
function readingAssets(doc) { const assets = doc.assets.filter(a => a.kind !== 'equation'), named = assets.filter(a => a.label || a.caption); return named.length ? named : assets; }
function fileUrl(path) {
  if (state.urls.has(path)) return state.urls.get(path);
  const blob = state.current?.files?.[path];
  if (!blob) return '';
  const url = URL.createObjectURL(blob); state.urls.set(path, url); return url;
}
function revokeUrls() { for (const url of state.urls.values()) URL.revokeObjectURL(url); state.urls.clear(); }
function setView(view) {
  if (view === 'reader' && !state.current) return;
  state.view = view;
  if(view!=='reader')readingState.close();
  for (const v of ['library', 'reader', 'prepare']) $(v + 'View').hidden = v !== view;
  document.querySelectorAll('.main-nav [data-view]').forEach(btn => btn.classList.toggle('active', btn.dataset.view === (view === 'reader' ? 'library' : view)));
  $('readerNavigation').hidden = view !== 'reader';
  $('pageName').textContent = ({ library: '我的书库', reader: '文献阅读', prepare: '文献准备' })[view];
  $('sidebar').classList.remove('open');
  window.scrollTo({ top: 0, behavior: 'instant' });
  if (view === 'library') renderLibrary();
  if (view === 'prepare') { renderJobs(); refreshJobs().catch(report); }
}

function renderLibrary() {
  const query = $('librarySearch').value.trim().toLowerCase();
  const papers = state.papers.filter(p => (!state.filterStar || bookmarksFor(p.id).length) && [p.document.metadata.title, p.document.metadata.title_translated, ...(p.document.metadata.authors || []), ...p.document.blocks.map(b => b.original)].join(' ').toLowerCase().includes(query));
  $('libraryCount').textContent = state.papers.length;
  $('paperCount').textContent = String(state.papers.length).padStart(2, '0');
  $('bookmarkCount').textContent = String(Object.values(reading).reduce((n, r) => n + (r.bookmarks?.length || 0), 0)).padStart(2, '0');
  $('paperGrid').innerHTML = papers.map(p => {
    const d = p.document, m = d.metadata, percent = reading[p.id]?.percent || 0;
    return `<article class="paper-card"><div class="card-visual"><span class="card-monogram">P.</span><div class="card-page" aria-hidden="true"><div class="stub-heading"></div><div class="stub-columns"><div></div><div></div></div><div class="stub-chart"><i style="height:8px"></i><i style="height:17px"></i><i style="height:23px"></i><i style="height:14px"></i></div></div></div><div class="paper-card-content"><div class="paper-card-meta"><span>${e(m.journal || 'RESEARCH PAPER')}</span><span class="pill ${m.demo ? 'warm' : ''}">${m.demo ? '教学示例' : d.provenance?.structure_verification?.status==='complete' ? '已自动校验' : '可阅读'}</span></div><h2>${e(m.title_translated || m.title)}</h2>${m.title_translated ? `<p class="english-title">${e(m.title)}</p>` : ''}<p class="authors">${e((m.authors || []).join(' · ') || '原文与来源完整保存')} · ${d.pages.length} 页 · ${readingAssets(d).length} 个图表</p><div class="card-bottom"><small>${percent ? `${percent}% 已读` : '双语阅读 · 离线可用'}</small><div><button class="card-delete" data-delete="${e(p.id)}" aria-label="从本机移除此文献">×</button><button class="button" data-open="${e(p.id)}">${percent ? '继续阅读' : '开始研读'} <span>↗</span></button></div></div></div></article>`;
  }).join('') + `<div class="import-card"><span class="large-plus">＋</span><strong>带上你的下一篇论文</strong><p>导入 PC 准备端生成的 .folio<br>在这个设备上继续思考</p><button class="button subtle small" data-import>导入文献包 ↗</button></div>`;
  if (!papers.length && state.papers.length) $('paperGrid').insertAdjacentHTML('afterbegin', '<p class="empty">没有符合条件的文献。</p>');
}

async function importBuffer(buffer, serverJobId = null) {
  const paper = await decodePackage(buffer);
  if (serverJobId) paper.jobId = serverJobId;
  await sync.imported(paper);
  await putPaper(paper);
  state.papers = state.papers.filter(p => p.id !== paper.id); state.papers.unshift(paper);
  renderLibrary();
  return paper;
}
async function openPaper(id, resume = true) {
  const paper = state.papers.find(p => p.id === id); if (!paper) return;
  revokeUrls(); state.current = paper; state.overrides = {}; state.openSections=new Set(); state.jobId = state.server ? paper.jobId || null : null;
  if (state.jobId) {
    try { const job = await (await api('jobs/' + state.jobId)).json(); state.revision = job.revision; }
    catch { state.jobId = null; }
  }
  const position=readingState.open(paper);state.mode=position.mode;saveReading();
  $('readingProgress').textContent=position.percent+'% 已读';$('readingProgressBar').style.width=position.percent+'%';
  pauseProgressUntil=Date.now()+1000;
  renderReader(); setView('reader');
  const target = resume && position.block;
  if (target) requestAnimationFrame(() => revealBlock(target)?.scrollIntoView({ behavior: 'instant', block: 'start' }));
}
function blockTools(b) {
  const saved = bookmarksFor(activeDocument().id,true).includes(b.id);
  return `<div class="block-tools"><button data-toggle="${e(b.id)}" aria-label="切换本段原文或译文">原 / 译</button><button data-bookmark="${e(b.id)}" class="${saved ? 'saved' : ''}" aria-label="${saved ? '取消' : '保存'}本段书签">${saved ? '◆' : '◇'}</button><button data-source="${e(b.id)}" aria-label="查看本段来源页">源</button></div>`;
}
function blockHtml(b, allowAuthor = false) {
  const doc = activeDocument(), mode = state.overrides[b.id] || state.mode;
  const translated = b.translated.trim(), text = mode === 'original' || !translated ? b.original : translated;
  const language = mode === 'original' || !translated ? 'original' : 'translated';
  const original = language === 'original';
  const content = linkedText(text, b.references?.[language] || [], original ? b.styles : b.translated_styles||[]);
  const note = !translated && !['not_required'].includes(b.translation_status) && mode !== 'original' ? `<span class="translation-note" title="${e(b.translation_error||'自动重试及校验后尚未获得有效译文')}">译文暂不可用 · 显示原文</span>` : b.translation_status === 'review' ? `<span class="translation-note">${e(b.translation_error||'自动校验仍有差异')}</span>` : '';
  const extra = mode === 'bilingual' && translated ? `<div class="paragraph-content original-text bilingual-secondary">${linkedText(b.original, b.references?.original || [], b.styles)}</div>` : '';
  if (['title', 'figure_text'].includes(b.kind)||b.kind==='author'&&!allowAuthor) return '';
  let body;
  if (b.kind === 'heading') {
    const tag = b.heading_level > 1 ? 'h3' : 'h2';
    body = `<${tag}>${content}</${tag}>${mode === 'bilingual' && translated ? `<div class="heading-english">${e(b.original)}</div>` : ''}`;
  } else if (b.kind === 'equation' && b.asset_id) {
    const asset = doc.assets.find(a => a.id === b.asset_id);
    body = asset ? `<button class="equation-render" data-asset="${e(asset.id)}"><img src="${e(fileUrl(asset.path))}" alt="${e(b.original)}" loading="lazy"></button>` : `<p>${content}</p>`;
  } else if (b.kind === 'table' && b.asset_id) {
    // One link per table asset, even when many PDF cells have separate source blocks.
    const index = doc.blocks.indexOf(b);
    if (doc.blocks.slice(0, index).some(prev => prev.kind === 'table' && prev.asset_id === b.asset_id)) return '';
    const asset = doc.assets.find(a => a.id === b.asset_id);
    body = `<div class="table-inline"><button data-asset="${e(b.asset_id)}">▦ ${e(asset?.label || '表格')} · 点击查看原表 ↗</button></div>`;
  } else if (b.kind === 'caption') {
    const chars = Array.from(text), excerpt = chars.length > 190 ? chars.slice(0,190).join('') + '…' : text;
    body = b.asset_id ? `${note}<div>${linkedText(excerpt,b.references?.[language] || [],original ? b.styles : [])}</div><button class="caption-asset" data-asset="${e(b.asset_id)}">${e(b.label || '图表')} · 完整图表与双语图注 ↗</button>` : `${note}<div>${content}</div>${extra}`;
  } else {
    body = `${note}<div class="paragraph-content ${original ? 'original-text' : ''}">${content}</div>${extra}`;
  }
  return `<section class="body-block ${e(b.kind)}" id="block-${e(b.id)}" data-block="${e(b.id)}">${body}${blockTools(b)}</section>`;
}
function revealBlock(id){
  id=aliasBlock(activeDocument(),id);let target=document.getElementById('block-'+id);
  if(!target){const section=readingModel(activeDocument()).sections.find(s=>s.block_ids.includes(id));if(section)target=document.querySelector(`[data-reading-section="${CSS.escape(section.id)}"] summary`);}
  for(let fold=target?.closest('details.reading-fold');fold;fold=fold.parentElement?.closest('details.reading-fold'))fold.open=true;
  return target;
}
function sectionHtml(section){
  const first=section.blocks.find(b=>b.kind==='heading')||section.blocks[0];
  const label=state.mode==='original'?section.title:section.translated||section.title;
  const canonical=isSectionHeading(first,section);
  const content=section.blocks.filter(b=>!isSectionHeading(b,section)).map(b=>blockHtml(b,true)).join('');
  const summary=canonical?`id="block-${e(first.id)}" data-block="${e(first.id)}"`:`id="fold-${e(section.id)}"`;
  return `<details class="reading-fold" data-reading-section="${e(section.id)}" ${state.openSections.has(section.id)?'open':''}><summary ${summary}><span>${e(label)}</span><small>展开阅读</small></summary><div class="fold-content">${content}</div></details>`;
}
function renderReader() {
  const doc=activeDocument();if(!doc)return;const m=doc.metadata,model=readingModel(doc),useTranslated=state.mode!=='original'&&m.title_translated;
  const information=`<details class="paper-info"><summary>文献信息与自动检查记录</summary><p>${e(m.authors.join(' · '))}</p><p>${e(m.journal)} ${e(m.year)}${m.doi?' · DOI '+e(m.doi):''}</p>${m.demo?'<p>自编教学示例，图表为示意数据。</p>':''}${doc.issues.length?`<ul>${doc.issues.map(i=>`<li>${e(i.message)}</li>`).join('')}</ul>`:'<p>没有未解决的自动检查提示。</p>'}</details>`;
  $('paperArticle').innerHTML=`<div class="paper-header"><h1>${e(useTranslated?m.title_translated:m.title)}</h1>${useTranslated?`<p class="original-title">${e(m.title)}</p>`:''}${information}</div>${model.main.map(b=>blockHtml(b)).join('')}<div class="appendix-folds">${model.sections.map(sectionHtml).join('')}</div>`;
  document.querySelectorAll('[data-mode]').forEach(btn=>btn.classList.toggle('selected',btn.dataset.mode===state.mode));
  $('toc').innerHTML=model.toc.map(t=>`<button data-jump="${e(t.block_id)}" data-level="${t.level}" style="--toc-depth:${Math.min(6,Math.max(1,t.level))}">${e(state.mode==='original'?t.title:t.translated||t.title)}</button>`).join('')||'<p class="muted">此文献没有章节目录</p>';
  $('assetRail').innerHTML=readingAssets(doc).map(a=>`<button class="asset-card" data-asset="${e(a.id)}"><img class="asset-thumb" src="${e(fileUrl(a.path))}" alt="${e(a.label||'图表')}" loading="lazy"><span>${e(a.label||'图表')} ↗</span></button>`).join('');
  document.querySelector('.figures-rail').hidden=!state.figures;document.querySelector('.reading-layout').classList.toggle('clean-reading',!state.figures);
  renderBookmarks();updateProgress();
}
document.addEventListener('toggle',event=>{const section=event.target.dataset?.readingSection;if(section&&event.target.isConnected){if(event.target.open)state.openSections.add(section);else state.openSections.delete(section);}},true);
function renderBookmarks() {
  const d = activeDocument();
  $('bookmarks').innerHTML = bookmarksFor(d.id,true).map(id => {
    const b = d.blocks.find(b => b.id === id);
    return b ? `<button class="bookmark-item" data-jump="${e(id)}">${e((b.translated || b.original).slice(0, 47))}…</button>` : '';
  }).join('') || '<p class="muted">收藏值得回看的段落</p>';
}
function refreshReaderBookmarks() {
  const saved=new Set(bookmarksFor(activeDocument().id,true));
  for(const button of $('paperArticle').querySelectorAll('[data-bookmark]')) {
    const value=saved.has(button.dataset.bookmark);button.classList.toggle('saved',value);button.textContent=value?'◆':'◇';button.setAttribute('aria-label',`${value?'取消':'保存'}本段书签`);
  }
  renderBookmarks();
}
function toggleBlock(id) {
  const b = activeDocument().blocks.find(b => b.id === id); if (!b) return;
  const mode = state.overrides[id] || state.mode;
  state.overrides[id] = mode === 'original' ? 'translated' : 'original';
  const old = $('block-' + id); if (!old) return;
  old.outerHTML = blockHtml(b,!!old.closest('details.reading-fold'));
}
function saveBookmark(id) {
  const doc=activeDocument(),latest=state.papers.find(p=>p.id===doc.id)||state.current;
  const change=readingState.bookmark(state.current,latest,id);
  if(change)sync.bookmark(doc.id,change.block,change.value);
  saveReading(); renderBookmarks();
  const btn = document.querySelector(`[data-bookmark="${CSS.escape(id)}"]`);
  if (btn) { const saved = bookmarksFor(doc.id,true).includes(id); btn.classList.toggle('saved', saved); btn.textContent = saved ? '◆' : '◇'; btn.setAttribute('aria-label', `${saved ? '取消' : '保存'}本段书签`); }
}
function updateProgress() {
  if (state.view !== 'reader' || !activeDocument() || Date.now()<pauseProgressUntil) return;
  const article = $('paperArticle'), start = article.offsetTop, range = Math.max(1, article.scrollHeight - window.innerHeight + 120);
  const percent = Math.max(0, Math.min(100, Math.round((window.scrollY - start + 130) / range * 100)));
  $('readingProgress').textContent = percent + '% 已读'; $('readingProgressBar').style.width = percent + '%';
  let current = null, heading = null;
  for (const block of article.querySelectorAll('[data-block]')) {
    const rect=block.getBoundingClientRect();
    if (rect.height>0&&rect.top < 160) { current = block.dataset.block; if (block.classList.contains('heading')) heading = current; }
  }
  $('toc').querySelectorAll('[data-jump]').forEach(btn => btn.classList.toggle('active', btn.dataset.jump === heading));
  const id=activeDocument().id,value=readingState.update(state.current,state.papers.find(p=>p.id===id),{percent,block:current,mode:state.mode});saveReading();
  if(value)sync.progress(id,value);
}
function showAsset(id) {
  const asset = activeDocument().assets.find(a => a.id === id); if (!asset) return;
  $('assetKind').textContent = asset.kind === 'figure' ? 'FIGURE / 图' : asset.kind === 'table' ? 'TABLE / 表' : 'EQUATION / 公式';
  $('assetTitle').textContent = asset.label || (asset.kind === 'table' ? '表格' : asset.kind === 'figure' ? '图像' : '公式');
  $('assetCanvas').classList.remove('zoomed'); $('assetZoom').textContent = '放大 ＋';
  $('assetCanvas').innerHTML = `<img src="${e(fileUrl(asset.path))}" alt="${e(asset.label || '论文图表')}">`;
  const caption = activeDocument().blocks.find(b => b.kind === 'caption' && b.asset_id === id);
  $('assetCaption').innerHTML = `${caption?.translation_status === 'review' ? '<small>图注自动检查仍有数值差异</small>' : ''}<p>${linkedText(asset.caption_translated || asset.caption,caption?.references?.[asset.caption_translated ? 'translated' : 'original'] || [],asset.caption_translated?caption?.translated_styles||[]:caption?.styles||[])}</p>${asset.caption_translated ? `<p>${linkedText(asset.caption,caption?.references?.original || [],caption?.styles || [])}</p>` : ''}<small>来源：PDF 第 ${asset.page} 页 · ${asset.confidence < .75 ? '区域置信度较低' : '保留原页图表'}</small>`;
  $('assetData').innerHTML = asset.table?.length ? `<details><summary class="muted">查看提取的表格数据（以原图为准）</summary><table>${asset.table.map(row => `<tr>${row.map(cell => `<td>${e(cell || '')}</td>`).join('')}</tr>`).join('')}</table></details>` : '';
  if (!$('assetDialog').open) $('assetDialog').showModal();
}
function showSource(id) {
  const doc = activeDocument(), b = doc.blocks.find(b => b.id === id); if (!b?.source.length) return;
  const number = b.source[0].page, page = doc.pages.find(p => p.number === number);
  $('sourceTitle').textContent = `来源第 ${number} 页 · ${page.width.toFixed(0)} × ${page.height.toFixed(0)} pt`;
  $('sourceCanvas').innerHTML = `<img src="${e(fileUrl(page.preview))}" alt="PDF 来源第 ${number} 页">` + b.source.filter(s => s.page === number).map(s => `<div class="source-box" style="left:${s.bbox[0] / page.width * 100}%;top:${s.bbox[1] / page.height * 100}%;width:${(s.bbox[2] - s.bbox[0]) / page.width * 100}%;height:${(s.bbox[3] - s.bbox[1]) / page.height * 100}%"></div>`).join('');
  $('sourceDialog').showModal();
}

function openSettings() {
  const form = $('settingsForm');
  for (const [key, value] of Object.entries(settings)) {
    const input = form.elements.namedItem(key); if (!input) continue;
    if (key === 'glossary') input.value = Object.entries(value).map(([k, v]) => `${k} = ${v}`).join('\n');
    else if (input.type === 'checkbox') input.checked = value;
    else input.value = value;
  }
  form.elements.api_key.value = apiKey; form.elements.access_token.value = accessToken;
  $('detectModels').disabled = !state.server;
  $('testModels').disabled = !state.server;
  providerEditor.open(settings.fallbacks||[]);
  $('settingsDialog').showModal();
}
function connectionConfig() {
  const form = $('settingsForm');
  return { base_url: form.elements.base_url.value.trim().replace(/\/+$/, ''), api_key: form.elements.api_key.value.trim(),
    layout_model: form.elements.layout_model.value.trim(), translation_model: form.elements.translation_model.value.trim(),
    json_mode: form.elements.json_mode.checked, token_parameter: form.elements.token_parameter.value,
    thinking_mode: form.elements.thinking_mode.value };
}
async function detectModels(automatic = false) {
  clearTimeout(discoveryTimer);
  if (!state.server) return;
  const form = $('settingsForm');
  if (automatic ? !form.elements.base_url.checkValidity() : !form.elements.base_url.reportValidity()) return;
  const config = connectionConfig(), identity = config.base_url + '\n' + config.api_key;
  if (automatic && identity === discoveryIdentity) return;
  discoveryController?.abort(); discoveryController = new AbortController();
  const controller = discoveryController;
  $('detectModels').disabled = true; $('modelStatus').textContent = '正在从 provider 获取模型列表…';
  accessToken = form.elements.access_token.value.trim();
  try {
    const response = await api('models/discover', { method: 'POST', json: config, signal: controller.signal });
    const result = await response.json();
    if (connectionConfig().base_url + '\n' + connectionConfig().api_key !== identity) return;
    discoveryIdentity = identity; discoveredModels = result.models;
    $('modelChoices').innerHTML = discoveredModels.filter(m => m.text_candidate).map(m => `<option value="${e(m.id)}">${e(m.owned_by)}</option>`).join('');
    $('modelList').innerHTML = discoveredModels.map(m => `<span class="model-chip${m.text_candidate ? '' : ' other'}">${e(m.id)}${m.text_candidate ? '' : ' · 其他任务'}</span>`).join('');
    $('modelList').hidden = false;
    for (const name of ['layout_model', 'translation_model']) if (!form.elements[name].value.trim()) form.elements[name].value = result.suggested_model || '';
    let applied = false;
    if (form.elements.token_parameter.value === 'max_tokens' && form.elements.thinking_mode.value === 'auto') {
      for (const [key, value] of Object.entries(result.recommended_settings || {})) { form.elements[key].value = value; applied = true; }
    }
    let checker='';
    if(form.elements.layout_model.value===form.elements.translation_model.value&&!providerEditor.values.length){
      const other=discoveredModels.find(m=>m.text_candidate&&m.id!==form.elements.layout_model.value);
      if(other){providerEditor.add();providerEditor.values.at(-1).models=[other.id];providerEditor.values.at(-1).name='独立校验与备用';providerEditor.render();checker=' 已添加 '+other.id+' 作为独立校验备用模型，可调整回退顺序。';}
    }
    $('modelStatus').textContent = result.message + (applied ? ' 已应用 MiMo 兼容参数建议。' : '')+checker;
  } catch (err) {
    if (err.name !== 'AbortError' && controller === discoveryController) { discoveryIdentity = ''; discoveredModels = []; $('modelChoices').innerHTML = ''; $('modelList').hidden = true; $('modelStatus').textContent = err.message + '；仍可手动填写。'; }
  } finally { if (controller === discoveryController) $('detectModels').disabled = false; }
}
async function testModels() {
  if (!state.server) return;
  const config = connectionConfig();
  if (!config.layout_model || !config.translation_model) { $('modelStatus').textContent = '请先检测或填写所选模型。'; return; }
  accessToken = $('settingsForm').elements.access_token.value.trim();
  $('testModels').disabled = true; $('modelStatus').textContent = '正在验证所选模型（每个模型一次小请求）…';
  try { const response = await api('models/test', { method: 'POST', json: config }); const result = await response.json(); $('modelStatus').textContent = result.message; }
  catch (err) { $('modelStatus').textContent = err.message; }
  finally { $('testModels').disabled = false; }
}
for (const field of ['base_url', 'api_key']) {
  const input = $('settingsForm').elements[field];
  input.addEventListener('input', () => {
    const form=$('settingsForm'),url=form.elements.base_url.value.trim().replace(/\/+$/,'');
    if(field==='base_url'&&apiKeyUrl&&url!==apiKeyUrl){apiKey='';apiKeyUrl='';form.elements.api_key.value='';}
    if(field==='api_key')apiKeyUrl=url;
    discoveryIdentity = ''; $('modelChoices').innerHTML = ''; $('modelList').hidden = true;
    clearTimeout(discoveryTimer); discoveryTimer = setTimeout(() => detectModels(true), 600);
  });
  input.addEventListener('change', () => detectModels(true));
}
$('addFallbackButton').addEventListener('click',()=>{if(providerEditor.values.length>=4)return toast('最多配置 4 个备用服务');providerEditor.add();});
$('detectModels').addEventListener('click', () => detectModels());
$('testModels').addEventListener('click', testModels);
$('settingsForm').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.target, data = new FormData(form);
  try {
  const glossary = {};
  for (const line of String(data.get('glossary')).split('\n').filter(v => v.trim())) {
    const split = line.indexOf('=');
    if (split < 1 || !line.slice(split + 1).trim()) return toast('术语表格式：英文 = 译法');
    glossary[line.slice(0, split).trim()] = line.slice(split + 1).trim();
  }
  const next = { ...defaults, base_url: String(data.get('base_url')).trim().replace(/\/+$/, ''), layout_model: String(data.get('layout_model')).trim(), translation_model: String(data.get('translation_model')).trim(), target_language: String(data.get('target_language')).trim(), max_output_tokens: Number(data.get('max_output_tokens')), vision: data.has('vision'), json_mode: data.has('json_mode'), token_parameter: String(data.get('token_parameter')), thinking_mode: String(data.get('thinking_mode')), glossary, fallbacks:providerEditor.safe(), retry_attempts:Number(data.get('retry_attempts')) };
  const url = new URL(next.base_url);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) return toast('Base URL 必须为无凭据、无查询参数的 HTTP(S) 地址');
  settings = next; apiKey = String(data.get('api_key')).trim();apiKeyUrl=next.base_url; accessToken = String(data.get('access_token')).trim();
  try { localStorage.setItem('paperloom-settings', JSON.stringify(settings)); }
  catch { toast('配置仅保留在本次会话'); }
  $('settingsDialog').close(); toast('模型设置已保存；密钥仅保留在本次会话'); refreshJobs().catch(report);
  }catch(error){report(error);}
});

function selectedPdf(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.pdf')) return toast('请选择 PDF 文档');
  if (file.size > 80 * 1024 * 1024) return toast('PDF 最大 80 MB');
  state.selectedPdf = file; $('selectedPdf').textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB`;
}
$('pdfInput').addEventListener('change', event => selectedPdf(event.target.files[0]));
for (const name of ['dragenter', 'dragover']) $('dropzone').addEventListener(name, event => { event.preventDefault(); $('dropzone').classList.add('dragover'); });
for (const name of ['dragleave', 'drop']) $('dropzone').addEventListener(name, event => { event.preventDefault(); $('dropzone').classList.remove('dragover'); if (name === 'drop') selectedPdf(event.dataTransfer.files[0]); });
$('prepareButton').addEventListener('click', async () => {
  if (!state.server) return toast('请先在 PC 上启动准备端，再从准备端网页上传 PDF');
  if (!state.selectedPdf) return toast('请先选择一份 PDF');
  if ($('useLLM').checked && (!settings.layout_model || !settings.translation_model)) { toast('请配置布局模型和翻译模型'); return openSettings(); }
  if (state.tokenRequired && !accessToken) { toast('请填写准备端访问令牌'); return openSettings(); }
  const form = new FormData(); form.set('file', state.selectedPdf); form.set('config', JSON.stringify(getConfig()));
  $('prepareButton').disabled = true;
  try { await api('jobs', { method: 'POST', body: form }); toast('已提交，处理进度将自动更新'); await refreshJobs(); }
  catch (err) { report(err); }
  finally { $('prepareButton').disabled = false; }
});
async function refreshJobs() {
  if (!state.server || state.tokenRequired && !accessToken) return;
  state.jobs = await (await api('jobs')).json(); renderJobs();
  const active = state.jobs.some(j => ['queued', 'running', 'cancelling'].includes(j.status));
  $('jobDot').hidden = !active;
  clearTimeout(pollTimer);
  if (active) pollTimer = setTimeout(() => refreshJobs().catch(report), 1600);
}
function renderJobs() {
  $('serverNotice').hidden = state.server;
  $('jobList').innerHTML = state.jobs.map(j => {
    let actions = '';
    if (j.status === 'ready') actions = `<button class="button subtle small" data-download-job="${e(j.id)}">导出文献包</button><button class="button subtle small" data-reprepare="${e(j.id)}">重新准备</button><button class="button primary small" data-read-job="${e(j.id)}">阅读文献 ↗</button>`;
    else if (['failed', 'cancelled', 'interrupted'].includes(j.status)) actions = `<button class="button subtle small" data-retry-job="${e(j.id)}">重试 ↻</button><button class="button danger small" data-delete-job="${e(j.id)}">彻底删除</button>`;
    else actions = `<button class="button subtle small" data-cancel-job="${e(j.id)}" ${j.status === 'cancelling' ? 'disabled' : ''}>取消</button>`;
    return `<div class="job-row"><span class="job-symbol">${j.status === 'ready' ? '✓' : j.status === 'failed' ? '!' : 'P'}</span><div class="job-info"><strong>${e(j.filename)}</strong><p>${e(j.message)}${j.status === 'ready' ? ` · ${j.issues || 0} 项自动检查提示` : ''}</p><div class="job-progress"><span style="width:${Number(j.progress) || 0}%"></span></div></div><div class="job-actions">${actions}</div></div>`;
  }).join('') || `<div class="empty">${state.server ? state.tokenRequired && !accessToken ? '在模型设置中填写访问令牌后可查看任务。' : '你的第一篇论文，从这里开始。' : '导入 .folio 后可直接阅读。PC 准备端启动方式见项目说明。'}</div>`;
}
async function readJob(id) {
  toast('正在读取并校验文献包…');
  const response = await api('jobs/' + id + '/package');
  const paper = await importBuffer(await response.arrayBuffer(), id);
  await openPaper(paper.id, false); toast('文献已保存到本机书库');
}
function downloadBlob(blob, name) {
  const url = URL.createObjectURL(blob), link = document.createElement('a'); link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 10000);
}
async function downloadJob(id) { downloadBlob(await (await api('jobs/' + id + '/package')).blob(), 'paper-' + id.slice(0, 8) + '.folio'); }

function searchReader() {
  const query = $('readerSearch').value.trim().toLowerCase();
  document.querySelectorAll('.search-hit').forEach(el => el.classList.remove('search-hit'));
  state.find = query ? activeDocument().blocks.filter(b => (b.original + '\n' + b.translated).toLowerCase().includes(query) && $('block-' + b.id)).map(b => b.id) : [];
  state.findIndex = 0; $('findCount').textContent = state.find.length ? `${state.find.length} 段` : query ? '无匹配' : '';
  if (state.find.length) findNext(false);
}
function findNext(advance = true) {
  if (!state.find.length) return;
  if (advance) state.findIndex = (state.findIndex + 1) % state.find.length;
  document.querySelectorAll('.search-hit').forEach(el => el.classList.remove('search-hit'));
  const target = revealBlock(state.find[state.findIndex]); target.classList.add('search-hit'); target.scrollIntoView({ block: 'center' });
  $('findCount').textContent = `${state.findIndex + 1}/${state.find.length} 段`;
}

function syncFormValue() {
  const form=$('syncForm');return Object.fromEntries(['url','folder','username','password'].map(name=>[name,form.elements[name].value]).concat(['auto','remember','allowHttp'].map(name=>[name,form.elements[name].checked])));
}
function openSyncSettings() {
  const value=sync.value(),form=$('syncForm');
  for(const name of ['url','folder','username','password'])form.elements[name].value=value[name]||({folder:'Paperloom'}[name]||'');
  for(const name of ['auto','remember','allowHttp'])form.elements[name].checked=!!value[name];
  $('syncRememberLabel').hidden=!isAndroid;
  $('syncTransportNote').textContent=isAndroid?'登录信息可在 Android Keystore 中加密保存。':'密码仅保留在当前会话。'+(state.server?'由 PC 准备端连接 WebDAV，无需配置浏览器跨域。':'网页连接需要 WebDAV 服务允许跨域，并暴露 ETag 响应头；安卓 App 不需要跨域配置。');
  renderSyncRecovery();
  $('syncDialog').showModal();
}
function renderSyncRecovery() {
  const hidden=Object.entries(sync.engine.catalog.books).filter(([id])=>sync.engine.hidden.has(id));
  $('syncRestoreQuick').hidden=!sync.config.url||!hidden.length;
  $('syncHiddenNotice').hidden=!sync.config.url||!hidden.length;
  $('syncHiddenCount').textContent=`本机隐藏了 ${hidden.length} 篇云端文献`;
  $('syncHiddenList').innerHTML=hidden.map(([,book])=>`<li>${e(book.bundle.title)}</li>`).join('');
}
async function runSync(restore=false) {
  if(!sync.config.url){openSyncSettings();return;}
  for(const id of ['syncNowButton','syncDialogNow','syncRestoreButton','syncRestoreQuick'])$(id).disabled=true;
  $('syncCancelButton').hidden=false;
  try {const result=await (restore?sync.restoreAndSynchronize():sync.synchronize());toast(syncMessage(result));}
  catch(error){const message=error?.name==='AbortError'?'同步已取消':error.message;$('syncActivity').textContent=message;$('syncSummary').textContent=message;toast(message);}
  finally{for(const id of ['syncNowButton','syncDialogNow','syncRestoreButton','syncRestoreQuick'])$(id).disabled=false;$('syncCancelButton').hidden=true;renderSyncRecovery();}
}
$('syncForm').addEventListener('submit',async event=>{event.preventDefault();try{await sync.configure(syncFormValue());renderSyncRecovery();$('syncActivity').textContent='配置已保存。点击立即同步，或等待前台自动同步。';$('syncSummary').textContent='WebDAV 已配置';}catch(error){report(error);}});
$('syncForm').elements.url.addEventListener('input',()=>{if(sync.config.url&&$('syncForm').elements.url.value!==sync.config.url)$('syncForm').elements.password.value='';});
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')sync.schedule();});
document.addEventListener('click', async event => {
  const button = event.target.closest('button,[data-view]'); if (!button) return;
  try {
    if (button.dataset.view) setView(button.dataset.view);
    else if (button.dataset.open) await openPaper(button.dataset.open);
    else if (button.hasAttribute('data-import') || button.id === 'importButton') {
      if(isAndroid){toast('选择文献包（.folio 或系统改名的 .bin）');const blob=await pickNativePackage();const paper=await importBuffer(await blob.arrayBuffer());await openPaper(paper.id,false);toast('文献已导入本机');}
      else $('packageInput').click();
    }
    else if (button.dataset.delete) {
      const removed=state.papers.find(p=>p.id===button.dataset.delete);
      await removePaper(button.dataset.delete);sync.engine.hide(button.dataset.delete);
      if(removed?.document.metadata.demo)localStorage.setItem('paperloom-demo-dismissed-v1','true');
      state.papers = state.papers.filter(p => p.id !== button.dataset.delete);renderLibrary();renderSyncRecovery();toast('已从本机移除，云端副本保留；可恢复并同步取回');
    } else if (button.dataset.asset) showAsset(button.dataset.asset);
    else if (button.dataset.source) showSource(button.dataset.source);
    else if (button.dataset.toggle) toggleBlock(button.dataset.toggle);
    else if (button.dataset.bookmark) saveBookmark(button.dataset.bookmark);
    else if (button.dataset.jump) { revealBlock(button.dataset.jump)?.scrollIntoView({ block: 'start' }); $('sidebar').classList.remove('open'); }
    else if (button.dataset.mode) {
      const anchor=[...$('paperArticle').querySelectorAll('[data-block]')].map(el=>({id:el.dataset.block,rect:el.getBoundingClientRect()})).filter(item=>item.rect.height>0&&item.rect.top<180).at(-1);
      state.openSections=new Set([...$('paperArticle').querySelectorAll('details.reading-fold[open]')].map(el=>el.dataset.readingSection));
      state.mode = button.dataset.mode; state.overrides = {};
      const id=activeDocument().id,value=readingState.update(state.current,state.papers.find(p=>p.id===id),{mode:state.mode});saveReading();if(value)sync.progress(id,value);
      renderReader();if(anchor){const target=$('block-'+anchor.id);if(target)window.scrollTo({top:window.scrollY+target.getBoundingClientRect().top-anchor.rect.top,behavior:'instant'});}updateProgress();
    } else if (button.hasAttribute('data-close')) button.closest('dialog').close();
    else if (button.dataset.readJob) await readJob(button.dataset.readJob);
    else if (button.dataset.downloadJob) await downloadJob(button.dataset.downloadJob);
    else if(button.dataset.deleteJob){if(confirm('彻底删除这条准备记录及其原 PDF、缓存和生成文件？此操作不可恢复。')){await api('jobs/'+button.dataset.deleteJob,{method:'DELETE'});await refreshJobs();toast('准备记录及任务文件已彻底删除');}}
    else if(button.dataset.reprepare){await api('jobs/'+button.dataset.reprepare+'/reprepare',{method:'POST',json:getConfig()});await refreshJobs();toast('已开始重新准备与自动校验');}
    else if (button.dataset.cancelJob) { await api(`jobs/${button.dataset.cancelJob}/cancel`, { method: 'POST' }); await refreshJobs(); }
    else if (button.dataset.retryJob) {
      if (state.tokenRequired && !accessToken || $('useLLM').checked && (!settings.layout_model || !settings.translation_model)) return openSettings();
      await api(`jobs/${button.dataset.retryJob}/retry`, { method: 'POST', json: getConfig() }); await refreshJobs(); toast('已重试，将复用可用缓存');
    }
    else switch (button.id) {
      case 'settingsButton': case 'prepareSettings': openSettings(); break;
      case 'syncSettingsButton': openSyncSettings(); break;
      case 'syncNowButton': await runSync(); break;
      case 'syncDialogNow': await sync.configure(syncFormValue()); await runSync(); break;
      case 'syncCancelButton': sync.cancel(); break;
      case 'syncDisconnectButton': await sync.disconnect();$('syncForm').reset();renderSyncRecovery();$('syncActivity').textContent='已断开 WebDAV，文献仍保留在本机与云端。';$('syncSummary').textContent='WebDAV 尚未配置';break;
      case 'syncRestoreButton': await sync.configure(syncFormValue());await runSync(true);break;
      case 'syncRestoreQuick': await runSync(true);break;
      case 'syncTestButton': {
        button.disabled=true;try{$('syncActivity').textContent='正在测试连接…';$('syncActivity').textContent=await sync.test(syncFormValue());}catch(error){$('syncActivity').textContent=error.message;}finally{button.disabled=false;}break;
      }
      case 'menuButton': $('sidebar').classList.toggle('open'); break;
      case 'allFilter': state.filterStar = false; $('allFilter').classList.add('selected'); $('starFilter').classList.remove('selected'); renderLibrary(); break;
      case 'starFilter': state.filterStar = true; $('starFilter').classList.add('selected'); $('allFilter').classList.remove('selected'); renderLibrary(); break;
      case 'exportButton': if (state.current.package) {const name=(activeDocument().metadata.title.slice(0,70).replace(/[\\/:*?"<>|]/g,'_')||'paper')+'.folio';if(isAndroid){const saved=await exportNativePackage(state.current.package,name);toast(saved.name?'已保存：'+saved.name:'文献包已保存');}else downloadBlob(state.current.package,name);} break;
      case 'assetZoom': $('assetCanvas').classList.toggle('zoomed'); button.textContent = $('assetCanvas').classList.contains('zoomed') ? '适应窗口 −' : '放大 ＋'; break;
      case 'topButton': window.scrollTo({ top: 0, behavior: 'smooth' }); break;
      case 'findButton': $('findPanel').hidden = !$('findPanel').hidden; if (!$('findPanel').hidden) $('readerSearch').focus(); break;
      case 'findClose': $('findPanel').hidden = true; break;
      case 'findNext': findNext(); break;
      case 'figuresButton': state.figures=!state.figures;document.querySelector('.figures-rail').hidden=!state.figures;document.querySelector('.reading-layout').classList.toggle('clean-reading',!state.figures);break;
      case 'typeButton': $('typePanel').hidden = !$('typePanel').hidden; break;
      case 'themeButton': document.body.classList.toggle('night'); if(isAndroid)nativeTheme(document.body.classList.contains('night')).catch(report); try { localStorage.setItem('paperloom-theme', document.body.classList.contains('night') ? 'night' : 'day'); } catch {} break;
    }
  } catch (err) { report(err); }
});
$('packageInput').addEventListener('change', async event => {
  const file = event.target.files[0]; if (!file) return;
  $('importButton').disabled = true;
  try { toast('正在校验并导入文献包…'); const p = await importBuffer(await file.arrayBuffer()); await openPaper(p.id, false); toast('已导入本机，可离线阅读'); }
  catch (err) { report(err); }
  finally { event.target.value = ''; $('importButton').disabled = false; }
});
$('librarySearch').addEventListener('input', renderLibrary);
$('readerSearch').addEventListener('input', searchReader);
$('readerSearch').addEventListener('keydown', event => { if (event.key === 'Enter') findNext(); });
$('fontSize').addEventListener('input', event => { document.documentElement.style.setProperty('--read-size', event.target.value + 'px'); try { localStorage.setItem('paperloom-font-size', event.target.value); } catch {} });
document.querySelector('.brand').addEventListener('click', event => { event.preventDefault(); setView('library'); });
function handleBack() {
  const dialog=document.activeElement?.closest('dialog[open]')||[...document.querySelectorAll('dialog[open]')].at(-1);
  const action=backAction({dialogOpen:!!dialog,drawerOpen:$('sidebar').classList.contains('open'),findOpen:!$('findPanel').hidden,typeOpen:!$('typePanel').hidden,view:state.view});
  switch(action) {
    case 'dialog': dialog.close();break;
    case 'drawer': $('sidebar').classList.remove('open');break;
    case 'find': $('findPanel').hidden=true;break;
    case 'type': $('typePanel').hidden=true;break;
    case 'library': updateProgress();setView('library');break;
    default:return false;
  }
  return true;
}
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&handleBack())event.preventDefault();});
document.addEventListener('paperloomBackButton',()=>{if(!handleBack()&&isAndroid)backgroundNativeApp().catch(report);});
let scrolling = false;
window.addEventListener('scroll', () => { if (!scrolling) { scrolling = true; requestAnimationFrame(() => { updateProgress(); scrolling = false; }); } }, { passive: true });

async function initialize() {
  if(isAndroid){document.body.classList.add('android-app');document.querySelectorAll('[data-view="prepare"]').forEach(el=>el.hidden=true);$('settingsButton').hidden=true;$('connectionStatus').innerHTML='安卓阅读器<small>文献与图片保存在本机</small>';}
  try { if (localStorage.getItem('paperloom-theme') === 'night') document.body.classList.add('night'); const size = Number(localStorage.getItem('paperloom-font-size')); if (size >= 16 && size <= 24) { $('fontSize').value = size; document.documentElement.style.setProperty('--read-size', size + 'px'); } } catch {}
  if(isAndroid)nativeTheme(document.body.classList.contains('night')).catch(report);
  try {
    const response = await fetch('/api/health', { cache: 'no-store' });
    if (response.ok && response.headers.get('content-type')?.includes('application/json')) {
      const health = await response.json(); state.server = health.service === 'paperloom'; state.tokenRequired = !!health.requires_token;
    }
  } catch {}
  if (state.server) { $('connectionStatus').innerHTML = 'PC 准备端已连接<small>模型调用与文献保存在本机</small>'; }
  $('detectModels').disabled = !state.server; $('testModels').disabled = !state.server;
  if (!state.server) $('modelStatus').textContent = '当前为独立阅读器。请在 PC 准备端检测与调用模型。';
  renderJobs();
  try {
    state.papers = await allPapers();
    if (!state.papers.length&&!sync.config.url&&!readLocal('paperloom-demo-dismissed-v1',false)) {
      const response = await fetch('./demo/demo.folio');
      if (!response.ok) throw new Error('示例文献尚未生成');
      await importBuffer(await response.arrayBuffer());
    }
    renderLibrary();
    await sync.initialize();
    renderSyncRecovery();
    if(sync.config.url)$('syncSummary').textContent='WebDAV 已配置';
  } catch (err) { renderLibrary(); report(err); }
  if (state.server) refreshJobs().catch(report);
  if (!isAndroid && 'serviceWorker' in navigator) navigator.serviceWorker.register('./sw.js').catch(() => { /* HTTPS is required outside localhost. */ });
}
initialize();
