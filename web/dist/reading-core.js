export const sectionTitles={references:['References','参考文献'],online_content:['Online content','在线内容'],acknowledgements:['Acknowledgements','致谢'],author_contributions:['Author contributions','作者贡献'],funding:['Funding','经费与资助'],data_availability:['Data availability','数据可用性'],code_availability:['Code availability','代码可用性'],declarations:['Declarations','声明与伦理'],additional_information:['Additional information','附加信息'],supplementary:['Supplementary material','补充材料'],reporting_summary:['Reporting summary','研究报告摘要'],publication_information:['Publication information','出版信息']};
const normalized=t=>String(t).normalize('NFKC').replaceAll('’',"'").replaceAll('‑','-').replace(/\s+/g,' ').trim();
const notice=t=>/^(check for updates|a list of authors and their affiliations appears at the end of the paper)[.!\s]*$/i.test(normalized(t));
export function readingModel(doc){
  if(doc.reading_sections?.length){const grouped=new Set(doc.reading_sections.flatMap(s=>s.block_ids));return {main:doc.blocks.filter(b=>!grouped.has(b.id)),sections:doc.reading_sections.map(s=>({...s,blocks:s.block_ids.map(id=>doc.blocks.find(b=>b.id===id))})),toc:doc.toc};}
  // Older packages retain their original source payload; display common appendix
  // headings in folds until the PC agents prepare the upgraded edition.
  const groups=new Map(),main=[];let active='body';
  const patterns=[['references',/^references$|^bibliography$/i],['online_content',/^online content$/i],['acknowledgements',/^acknowledg\w*$/i],['author_contributions',/^authors?'? contributions?$/i],['funding',/^funding$/i],['data_availability',/^data availability|^availability of data/i],['code_availability',/^code availability$/i],['declarations',/^declarations$|^competing interests$|^conflicts? of interest/i],['additional_information',/^additional information|^peer review information/i],['supplementary',/^supplementary|^supporting information/i],['reporting_summary',/^reporting summary|^reporting for specific materials/i],['publication_information',/^publisher'?s note$/i]];
  for(const b of doc.blocks){
    if(notice(b.original))continue;
    if(b.kind==='heading'){const text=normalized(b.original),found=patterns.find(([,r])=>r.test(text));if(found)active=found[0];else if(/^(?:\d+\.?\s+)?(abstract|introduction|background|results|discussion|conclusions?|methods|materials and methods)(?:\s|$)/i.test(text))active='body';}
    const key=b.kind==='reference'?'references':['metadata','author','footnote'].includes(b.kind)&&active==='body'?'publication_information':active;
    if(key==='body')main.push(b);else{if(!groups.has(key))groups.set(key,[]);groups.get(key).push(b);}
  }
  const sections=[...groups].map(([id,blocks])=>({id,title:sectionTitles[id][0],translated:sectionTitles[id][1],collapsed:true,blocks,block_ids:blocks.map(b=>b.id)}));
  const ids=new Set(main.map(b=>b.id));const toc=doc.toc.filter(t=>ids.has(t.block_id));
  for(const s of sections){const first=s.blocks.find(b=>b.kind==='heading')||s.blocks[0];toc.push({block_id:first.id,title:s.title,translated:s.translated,level:1});}
  return {main,sections,toc};
}
export function aliasBlock(doc,id){const seen=new Set();while(doc.block_aliases?.[id]&&!seen.has(id)){seen.add(id);id=doc.block_aliases[id];}return id;}
export function isSectionHeading(block,section){return block.kind==='heading'&&(normalized(block.original).toLowerCase()===normalized(section.title).toLowerCase()||section.id==='references'&&/^references$/i.test(normalized(block.original)));}
