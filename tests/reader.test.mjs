import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { zipSync, strToU8, unzipSync } from '../web/dist/vendor/fflate.js';
import { decodePackage, inspectZip, linkedText, safePath, validateDocument, digest } from '../web/dist/package-core.js';
import {readingModel,aliasBlock} from '../web/dist/reading-core.js';
import {ProviderEditor} from '../web/dist/providers.js';
import {backAction} from '../web/dist/navigation-core.js';
import {ReadingState} from '../web/dist/reader-state.js';
const demo = new URL('../web/dist/demo/demo.folio', import.meta.url);
const basic=(id,original,kind='paragraph',heading_level=1)=>({id,original,translated:'',kind,heading_level});
const readingPaper=(hash='first',id='paper')=>({id,syncHash:hash,document:{blocks:['first','middle','last'].map(id=>({id})),block_aliases:{old:'middle'}}});

test('passive sync keeps the visible reading position and language while merging valid bookmarks',()=>{
  const paper=readingPaper(),saved={paper:{percent:25,block:'middle',mode:'bilingual',bookmarks:[]}},state=new ReadingState(saved);
  state.open(paper);
  const incoming={percent:88,block:'last',mode:'original',bookmarks:['old','missing','first','old']};
  state.receive(paper,incoming);
  assert.equal(state.live('paper').percent,25);assert.equal(state.live('paper').block,'middle');assert.equal(state.live('paper').mode,'bilingual');
  assert.deepEqual(state.live('paper').bookmarks,['middle','first']);assert.deepEqual(saved.paper.bookmarks,['middle','first']);
  assert.equal(saved.paper.percent,25);assert.equal(incoming.percent,88);assert.deepEqual(incoming.bookmarks,['old','missing','first','old']);
});

test('cloud progress received outside the reader is used when the paper is next opened',()=>{
  const paper=readingPaper(),saved={},state=new ReadingState(saved);state.open(paper);state.close();
  state.receive(paper,{percent:73,block:'old',mode:'original',bookmarks:['first']});
  assert.deepEqual(state.open(paper),{percent:73,block:'middle',mode:'original',bookmarks:['first']});
  state.receive(readingPaper('another','other'),{percent:91,block:'last',mode:'translated',bookmarks:[]});
  assert.equal(state.live('paper').percent,73);assert.equal(saved.other.percent,91);
});

test('a newer document is deferred until reopen and old-page progress cannot overwrite its state',()=>{
  const old=readingPaper(),latest=readingPaper('second'),saved={paper:{percent:25,block:'middle',mode:'bilingual',bookmarks:[]}},state=new ReadingState(saved);
  latest.document={blocks:[{id:'new-first'},{id:'new-middle'}],block_aliases:{middle:'new-middle'}};
  state.open(old);state.receive(latest,{percent:88,block:'new-first',mode:'original',bookmarks:['new-middle']});
  assert.equal(state.isCurrent(latest),false);assert.equal(state.live('paper').block,'middle');
  assert.equal(state.update(old,latest,{percent:35,block:'last',mode:'translated'}),null);
  assert.equal(state.live('paper').percent,35);assert.equal(saved.paper.percent,88);assert.equal(saved.paper.block,'new-first');
  state.close();assert.deepEqual(state.open(latest),{percent:88,block:'new-first',mode:'original',bookmarks:['new-middle']});
});

test('a bookmark made on the open older version uses the new version alias for synchronization',()=>{
  const old=readingPaper(),latest=readingPaper('second'),saved={},state=new ReadingState(saved);
  latest.document={blocks:[{id:'new-middle'}],block_aliases:{middle:'new-middle'}};
  state.open(old);state.receive(latest,{percent:80,block:'new-middle',mode:'original',bookmarks:[]});
  assert.deepEqual(state.bookmark(old,latest,'middle'),{block:'new-middle',value:true});
  assert.deepEqual(state.live('paper').bookmarks,['middle']);assert.deepEqual(saved.paper.bookmarks,['new-middle']);assert.equal(saved.paper.percent,80);
  assert.deepEqual(state.bookmark(old,latest,'middle'),{block:'new-middle',value:false});assert.deepEqual(saved.paper.bookmarks,[]);
  assert.equal(state.bookmark(old,latest,'last'),null);assert.deepEqual(saved.paper.bookmarks,[]);
});

test('user-driven progress and language changes persist normally for the open document version',()=>{
  const paper=readingPaper(),saved={},state=new ReadingState(saved);state.open(paper);
  assert.deepEqual(state.update(paper,paper,{percent:35,block:'middle',mode:'original'}),{percent:35,block:'middle',mode:'original',bookmarks:[]});
  assert.deepEqual(state.bookmark(paper,paper,'first'),{block:'first',value:true});
  state.close();assert.deepEqual(state.open(paper),{percent:35,block:'middle',mode:'original',bookmarks:['first']});
});

test('attaching a sync hash to a legacy import does not invalidate its active reading session',()=>{
  const paper=readingPaper();delete paper.syncHash;paper.manifest={files:[{path:'document.json',sha256:'stable-document-digest'}]};
  const saved={},state=new ReadingState(saved);state.open(paper);paper.syncHash='newly-computed-package-digest';
  assert.equal(state.isCurrent(paper),true);assert.equal(state.update(paper,paper,{percent:18,block:'middle',mode:'original'}).percent,18);
});

test('Back closes each reading layer before reaching the library and background',()=>{
  const state={dialogOpen:true,drawerOpen:true,findOpen:true,typeOpen:true,view:'reader'};
  assert.equal(backAction(state),'dialog');state.dialogOpen=false;
  assert.equal(backAction(state),'drawer');state.drawerOpen=false;
  assert.equal(backAction(state),'find');state.findOpen=false;
  assert.equal(backAction(state),'type');state.typeOpen=false;
  assert.equal(backAction(state),'library');state.view='library';
  assert.equal(backAction(state),'background');
  assert.equal(backAction({...state,dialogOpen:true}),'dialog');
});
test('legacy references before and after Methods become one folded bibliography',()=>{
  const blocks=[basic('body','Results','heading'),basic('r1','References','heading'),basic('ref1','1. Main citation.','reference'),basic('m','Methods','heading'),basic('child','Data analysis','heading',2),basic('r2','References','heading'),basic('ref2','94. Method citation.','reference')];
  const model=readingModel({blocks,toc:blocks.filter(b=>b.kind==='heading').map(b=>({block_id:b.id,title:b.original,level:b.heading_level}))});
  assert.deepEqual(model.main.map(b=>b.id),['body','m','child']);
  assert.equal(model.sections.length,1);assert.equal(model.sections[0].collapsed,true);
  assert.deepEqual(model.sections[0].block_ids,['r1','ref1','r2','ref2']);
  assert.equal(model.toc.filter(t=>t.title==='References').length,1);
});
test('upgraded package uses agent sections without hiding unfamiliar scientific headings',()=>{
  const doc={blocks:[basic('body','Unusual journal heading','heading'),basic('custom','Contribution details.')],reading_sections:[{id:'author_contributions',title:'Author contributions',translated:'作者贡献',collapsed:true,block_ids:['custom']}],toc:[]};
  const model=readingModel(doc);assert.deepEqual(model.main.map(b=>b.id),['body']);assert.equal(model.sections[0].blocks[0].id,'custom');
});
test('old publisher locators do not appear in the reading narrative',()=>{
  const doc={blocks:[basic('notice','Check for updates','metadata'),basic('locator','A list of authors and their affiliations appears at the end of the paper'),basic('body','Scientific results.')],toc:[]};
  assert.deepEqual(readingModel(doc).main.map(b=>b.id),['body']);
});
test('paragraph aliases restore old bookmarks and terminate on malformed cycles',()=>{
  assert.equal(aliasBlock({block_aliases:{old:'current'}},'old'),'current');
  assert.equal(aliasBlock({block_aliases:{a:'b',b:'a'}},'a'),'a');
});
test('fallback credentials remain session-only and never inherit across providers',()=>{
  const editor=new ProviderEditor({addEventListener(){}},{primary:()=>({base_url:'https://main.test/v1'}),server:()=>true,api:()=>{}});
  editor.keys.set('row','SESSION_TEST_KEY');
  const safe=[{ui_id:'row',base_url:'https://backup.test/v1',models:['m1','m2'],inherit_primary_key:false}];
  const runtime=editor.runtime(safe);assert.equal(runtime[0].api_key,'SESSION_TEST_KEY');assert.equal(runtime[0].ui_id,undefined);assert.ok(!JSON.stringify(safe).includes('SESSION_TEST_KEY'));
  assert.throws(()=>editor.runtime([{...safe[0],inherit_primary_key:true}]),/复用/);
});
test('changing a fallback address clears its previous service credential',()=>{
  const handlers={};const editor=new ProviderEditor({addEventListener(name,fn){handlers[name]=fn;}},{primary:()=>({base_url:'https://main.test/v1'}),server:()=>true,api:()=>{}});
  editor.values=[{ui_id:'row',base_url:'https://main.test/v1'}];editor.keys.set('row','OLD_SESSION_KEY');
  const key={value:'OLD_SESSION_KEY'},inherit={checked:true},card={dataset:{providerId:'row'},querySelector(selector){return selector.includes('api_key')?key:inherit;}};
  handlers.input({target:{dataset:{providerField:'base_url'},value:'https://other.test/v1',closest(){return card;}}});
  assert.equal(key.value,'');assert.equal(editor.keys.has('row'),false);assert.equal(inherit.checked,false);
});
test('rejects malformed section IDs, repeated members and missing alias targets',async()=>{
  const {document:base}=await decodePackage(new Uint8Array(await readFile(demo)));
  for(const section of [{id:'references',title:'Refs',translated:'参考文献',collapsed:true,block_ids:['unknown']},{id:'references',title:'Refs',translated:'参考文献',collapsed:true,block_ids:[base.blocks[0].id,base.blocks[0].id]},{id:'bad\"id',title:'Refs',translated:'参考文献',collapsed:true,block_ids:[base.blocks[0].id]}]){
    const doc=structuredClone(base);doc.reading_sections=[section];assert.throws(()=>validateDocument(doc),/分组/);
  }
  const doc=structuredClone(base);doc.block_aliases={old:'unknown'};assert.throws(()=>validateDocument(doc),/别名/);
  const outline=structuredClone(base);outline.toc[0].level='2" onclick="alert(1)';assert.throws(()=>validateDocument(outline),/目录/);
});
test('real package imports bilingual content and linked figures', async () => {
  const result = await decodePackage(new Uint8Array(await readFile(demo)));
  assert.equal(result.document.metadata.demo, true);
  assert.equal(result.document.assets.length, 2);
  assert.ok(result.document.toc.some(t=>t.title==='References'));
  assert.equal(result.document.reading_sections.find(s=>s.id==='references').collapsed,true);
  assert.ok(result.files['source.pdf'] instanceof Blob);
  assert.ok(result.document.blocks.some(b => b.translated && b.references.translated.length));
});
test('rejects traversal, duplicate inventory and decompression limits', () => {
  assert.equal(safePath('../secret'), false); assert.equal(safePath('C:/secret'), false); assert.equal(safePath('assets/a.png'), true);
  const zip = zipSync({ '../outside': strToU8('bad') }); assert.throws(() => inspectZip(zip), /路径/);
});
test('does not treat document text as HTML; handles Unicode codepoint offsets', () => {
  const html = linkedText('🧬 See Fig. 1 <script>', [{ start: 6, end: 12, asset_id: 'x', label: 'Fig. 1' }]);
  assert.ok(html.includes('>Fig. 1</button>')); assert.ok(html.includes('&lt;script&gt;')); assert.ok(!html.includes('<script>'));
});
test('rejects document link targets outside the asset set', async () => {
  const result = await decodePackage(new Uint8Array(await readFile(demo)));
  const doc = structuredClone(result.document);
  doc.blocks[0].asset_id = 'invented'; assert.throws(() => validateDocument(doc), /不存在/);
});
test('keeps scientific emphasis and superscript without executing markup', () => {
  assert.equal(linkedText('x2 bold', [], [{text:'x'}, {text:'2', superscript:true}, {text:' bold', bold:true}]), 'x<sup>2</sup><strong> bold</strong>');
});
test('detects a corrupted archive', async () => {
  const data = new Uint8Array(await readFile(demo)); data[60] ^= 16;
  await assert.rejects(() => decodePackage(data));
});
test('imports files above the async library worker threshold without a global Worker', async () => {
  const files=unzipSync(new Uint8Array(await readFile(demo)));
  const large=new Uint8Array(1024*1024);large.set(files['source.pdf']);files['source.pdf']=large;
  const manifest=JSON.parse(new TextDecoder().decode(files['manifest.json']));
  const item=manifest.files.find(f=>f.path==='source.pdf');item.size=large.length;item.sha256=await digest(large);
  files['manifest.json']=strToU8(JSON.stringify(manifest));
  const imported=await decodePackage(zipSync(files));
  assert.equal(imported.files['source.pdf'].size,large.length);
});
