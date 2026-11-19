"""Natural reading sections, persistent hierarchy, and bounded independent agents."""
from __future__ import annotations
import re,unicodedata,statistics,json
from collections import Counter,OrderedDict
from .models import Block,Issue,TocEntry
from .extract import join_lines
from .llm import LLMError,LLMResponseError,check_cancel

SECTIONS={
 'references':('References','参考文献'),'online_content':('Online content','在线内容'),
 'acknowledgements':('Acknowledgements','致谢'),'author_contributions':('Author contributions','作者贡献'),
 'funding':('Funding','经费与资助'),'data_availability':('Data availability','数据可用性'),
 'code_availability':('Code availability','代码可用性'),'declarations':('Declarations','声明与伦理'),
 'additional_information':('Additional information','附加信息'),'supplementary':('Supplementary material','补充材料'),
 'reporting_summary':('Reporting summary','研究报告摘要'),'publication_information':('Publication information','出版信息')}
VALID_SECTIONS=set(SECTIONS)|{'body','methods','publisher_notice'}
MAJOR=re.compile(r'^(?:\d+(?:\.\d+)*\.?\s+)?(abstract|introduction|background|results|discussion|conclusions?|methods|materials?\s*(?:and|&)?\s*methods|摘要|引言|背景|结果|讨论|结论|方法)(?:\s|$)',re.I)
NOTICE=re.compile(r'^(?:check\s+for\s+updates|a\s+list\s+of\s+authors\s+and\s+their\s+affiliations\s+appears\s+at\s+the\s+end\s+of\s+the\s+paper)[.!\s]*$',re.I)

def clean(text):
    return ' '.join(unicodedata.normalize('NFKC',text).replace('’',"'").replace('‑','-').split())

def heading_section(text):
    text=clean(text).lower();text=re.sub(r'^\d+(?:\.\d+)*\.?\s+','',text)
    rules=[('references',r'^references$|^bibliography$|^参考文献$'),
      ('online_content',r'^online content$'),('acknowledgements',r'^acknowledg\w*$|^致谢$'),
      ('author_contributions',r"^authors?'? contributions?$|^credit authorship"),
      ('funding',r'^funding\b'),('data_availability',r'^(?:availability of data|data availability|data access)'),
      ('code_availability',r'^code availability'),('declarations',r'^declarations$|^competing interests$|^conflicts? of interest|^ethics approval'),
      ('additional_information',r'^additional information|^peer review information|^review history|^authors.*handles'),
      ('supplementary',r'^supplementary|^supporting information'),('reporting_summary',r'^reporting summary|^reporting for specific materials'),
      ('publication_information',r"^publisher'?s note|^copyright|^rights and permissions")]
    return next((key for key,pattern in rules if re.search(pattern,text)),None)

def remove_notices(document,raw):
    lookup={b.id:b for b in raw};kept=[]
    for b in document.blocks:
        removed=[s for s in b.source if s.block_id in lookup and NOTICE.fullmatch(clean(lookup[s.block_id].text))]
        if not removed and NOTICE.fullmatch(clean(b.original)):removed=b.source[:]
        if removed:
            for source in removed:document.excluded[source.block_id]='publisher_notice'
            b.source=[s for s in b.source if s not in removed]
            if not b.source:continue
            b.original=join_lines([lookup[s.block_id].text for s in b.source]);b.styles=[v for s in b.source for v in lookup[s.block_id].styles]
            b.translated='';b.translation_status='pending'
        kept.append(b)
    document.blocks=kept

def normalize_hierarchy(blocks,trusted=False):
    """Carry a scholarly parent across page breaks; never promote a child just because a batch ended."""
    active=None;parent=1;last=1
    for b in blocks:
        if b.kind!='heading':continue
        major=MAJOR.match(clean(b.original));appendix=heading_section(b.original)
        if trusted and b.heading_level>1:
            major=None;appendix=None  # Preserve independently verified semantic children, including journal-specific Methods headings.
        if trusted and b.heading_level==1:
            active=b.section;parent=1
        if major:
            active='methods' if re.search(r'method|方法',major.group(1),re.I) else 'body'
            b.heading_level=1;parent=1
        elif appendix:
            active=appendix;b.heading_level=1;parent=1
        elif active=='methods' and not (trusted and b.heading_level==1):
            b.heading_level=max(parent+1,min(b.heading_level,last+1))
            # Uniform unnumbered method headings remain siblings. Numbering may add depth.
            if not trusted and not re.match(r'^\d+\.\d+',clean(b.original)):b.heading_level=2
        else:b.heading_level=min(b.heading_level,last+1)
        last=b.heading_level

def assign_sections(blocks):
    current='body'
    for b in blocks:
        if b.kind=='heading':
            found=heading_section(b.original);major=MAJOR.match(clean(b.original))
            if found:current=found
            elif major:current='methods' if re.search(r'method|方法',major.group(1),re.I) else 'body'
        b.section=current
        if b.kind=='reference':b.section='references'
        elif b.kind in ('metadata','author','footnote') and current in ('body','methods'):b.section='publication_information'
        elif re.match(r'^Extended Data\b|^Supplementary (?:Fig|Table)',b.label or b.original,re.I):b.section='supplementary'

def stable_ids(document,previous):
    if not previous:return
    by_source={tuple(s.block_id for s in b.source):b for b in previous.blocks}
    reserved={b.id for b in previous.blocks};used=set();next_id=max([int(x[1:]) for x in reserved if re.fullmatch(r'b\d+',x)]+[0])+1
    for b in document.blocks:
        before=by_source.get(tuple(s.block_id for s in b.source))
        if before and before.id not in used and clean(before.original)==clean(b.original):desired=before.id
        else:
            matches=[p for p in previous.blocks if p.id not in used and clean(p.original)==clean(b.original) and p.source[0].page==b.source[0].page]
            if len(matches)==1:desired=matches[0].id
            else:
                desired=f'b{next_id:05d}';next_id+=1
        b.id=desired;used.add(desired)
    document.block_aliases={k:v for k,v in previous.block_aliases.items() if v in used}
    for old in previous.blocks:
        if old.id in used:continue
        candidates=[b for b in document.blocks if b.source[0].page==old.source[0].page and (clean(b.original) in clean(old.original) or clean(old.original) in clean(b.original))]
        if candidates:document.block_aliases[old.id]=candidates[0].id

def evidence(block):
    return {'id':block.id,'kind':block.kind,'text':block.original,'section_hint':block.section,'heading_level':block.heading_level,
      'source':[s.model_dump() for s in block.source],
      'typography':[{'text':s.text,'size':s.size,'bold':s.bold} for s in block.styles if s.bold][:12]}

def outline(blocks):
    return [{'id':b.id,'text':b.original,'level':b.heading_level,'page':b.source[0].page} for b in blocks if b.kind=='heading']

def chunks(blocks,budget=26000,text=lambda b:b.original):
    batch=[];size=0
    for b in blocks:
        cost=len(text(b))+400
        if batch and size+cost>budget:yield batch;batch=[];size=0
        batch.append(b);size+=cost
    if batch:yield batch

def classification_valid(value,ids):
    entries=value.get('classifications',[])
    if not isinstance(entries,list) or Counter(x.get('id') for x in entries if isinstance(x,dict))!=Counter(ids):
        raise LLMResponseError('Classifications must cover every exact block ID once.')
    for x in entries:
        if x.get('section') not in VALID_SECTIONS or x.get('kind') not in ('title','author','abstract','heading','paragraph','caption','figure_text','equation','table','reference','footnote','metadata') or type(x.get('heading_level')) is not int or not 1<=x['heading_level']<=6:
            raise LLMResponseError('Invalid classification, kind, or heading depth.')
    if 'reading_order' in value and (not isinstance(value['reading_order'],list) or Counter(value['reading_order'])!=Counter(ids)):
        raise LLMResponseError('reading_order must contain every exact block ID once.')

def verify_structure(document,raw,client,config,update):
    remove_notices(document,raw);normalize_hierarchy(document.blocks);assign_sections(document.blocks)
    if not config.enabled:return
    full_outline=outline(document.blocks);lookup={b.id:b for b in document.blocks};joins=[];paragraph_joins=[];verified=0;ordered=[]
    batches=list(chunks(document.blocks))
    for i,batch in enumerate(batches):
        check_cancel(client.cancel);update('structure_verify',45+int(i/len(batches)*6),f'结构 agent 与独立校验 agent {i+1}/{len(batches)} 批')
        ids=[b.id for b in batch];payload={'paper_title':document.metadata.title,'outline':full_outline,'allowed_sections':sorted(VALID_SECTIONS),'blocks':[evidence(b) for b in batch]}
        proposed=client.complete('structure',payload,validator=lambda v:classification_valid(v,ids))
        review_payload={**payload,'proposal':proposed}
        def validate_review(v):
            if Counter(v.get('reviewed_ids',[]))!=Counter(ids):raise LLMResponseError('Review every exact block ID once.')
            classification_valid({'classifications':v.get('classifications')},ids)
            if not isinstance(v.get('joins',[]),list):raise LLMResponseError('joins must be an array')
            if 'reading_order' in v:classification_valid(v,ids)
            for group in v.get('joins',[]):
                if not isinstance(group,dict) or not isinstance(group.get('block_ids'),list) or not 2<=len(group['block_ids'])<=3 or any(id not in ids for id in group['block_ids']) or len(set(group['block_ids']))!=len(group['block_ids']) or type(group.get('heading_level')) is not int or not 1<=group['heading_level']<=6:
                    raise LLMResponseError('joins must use 2–3 distinct supplied IDs and a valid heading level.')
            if not isinstance(v.get('paragraph_joins',[]),list):raise LLMResponseError('paragraph_joins must be an array')
            for group in v.get('paragraph_joins',[]):
                if not isinstance(group,dict) or not isinstance(group.get('block_ids'),list) or not 2<=len(group['block_ids'])<=4 or any(id not in ids for id in group['block_ids']) or len(set(group['block_ids']))!=len(group['block_ids']):
                    raise LLMResponseError('paragraph_joins must use 2–4 distinct supplied IDs.')
        checked=client.complete('structure_verify',review_payload,validator=validate_review,target=client.reviewer_targets('structure'))
        before={x['id']:x for x in proposed['classifications']}
        for x in checked['classifications']:
            b=lookup[x['id']]
            if x['section']=='publisher_notice':
                if before[b.id]['section']=='publisher_notice':
                    for s in b.source:document.excluded[s.block_id]='publisher_notice_verified_by_two_models'
                    b.section='publisher_notice'
                else:b.section='publication_information'
                continue
            b.kind=x['kind'];b.section=x['section'];b.heading_level=x['heading_level']
        joins.extend(checked.get('joins',[]));verified+=len(ids)
        paragraph_joins.extend(checked.get('paragraph_joins',[]))
        ordered.extend(checked.get('reading_order',proposed.get('reading_order',ids)))
        full_outline=outline(document.blocks)
    document.blocks=[lookup[id] for id in ordered if lookup[id].section!='publisher_notice']
    apply_heading_joins(document,joins)
    apply_paragraph_joins(document,paragraph_joins)
    normalize_hierarchy(document.blocks,trusted=True)
    document.provenance['structure_verification']={'blocks':verified,'independent_model':True,'status':'complete'}

def apply_heading_joins(document,joins):
    for group in joins:
        ids=group.get('block_ids',[]) if isinstance(group,dict) else []
        if not 2<=len(ids)<=3 or len(set(ids))!=len(ids):continue
        positions=[i for i,b in enumerate(document.blocks) if b.id in ids]
        if len(positions)!=len(ids) or positions!=list(range(positions[0],positions[0]+len(ids))):continue
        blocks=[document.blocks[i] for i in positions]
        if [b.id for b in blocks]!=ids or any(b.kind not in ('heading','paragraph') or len(b.original)>240 or not b.styles or b.kind!='heading' and not all(s.bold for s in b.styles if s.text.strip()) for b in blocks):continue
        if any(MAJOR.fullmatch(clean(b.original)) or heading_section(b.original) for b in blocks):continue
        sizes=[statistics.median(s.size for s in b.styles if s.text.strip()) for b in blocks]
        if max(sizes)-min(sizes)>=1:continue
        if len({s.page for b in blocks for s in b.source})!=1:continue
        first=blocks[0];first.original=join_lines([b.original for b in blocks]);first.source=[s for b in blocks for s in b.source];first.styles=[s for b in blocks for s in b.styles]
        first.kind='heading';first.translated='';first.translation_status='pending'
        first.heading_level=min(6,max(1,group.get('heading_level',first.heading_level)))
        for b in blocks[1:]:document.block_aliases[b.id]=first.id
        document.blocks[positions[0]:positions[-1]+1]=[first]

def apply_paragraph_joins(document,joins):
    for group in joins:
        ids=group['block_ids'];positions=[i for i,b in enumerate(document.blocks) if b.id in ids]
        if len(positions)!=len(ids):continue
        intervening=[b for b in document.blocks[positions[0]:positions[-1]+1] if b.id not in ids]
        if any(b.kind not in ('caption','figure_text','metadata') for b in intervening):continue
        blocks=[document.blocks[i] for i in positions]
        if [b.id for b in blocks]!=ids or any(b.kind not in ('paragraph','abstract') for b in blocks) or len({b.section for b in blocks})!=1:continue
        if any(not 0<=right.source[0].page-left.source[-1].page<=4 for left,right in zip(blocks,blocks[1:])) or sum(len(b.original) for b in blocks)>12000:continue
        first=blocks[0];first.original=join_lines([b.original for b in blocks]);first.source=[s for b in blocks for s in b.source];first.styles=[s for b in blocks for s in b.styles]
        first.translated='';first.translation_status='pending'
        for b in blocks[1:]:document.block_aliases[b.id]=first.id
        document.blocks=[b for b in document.blocks if b.id not in ids[1:]]

def assemble_reading(document):
    groups=OrderedDict();main=[]
    for b in document.blocks:
        if b.section in SECTIONS:groups.setdefault(b.section,[]).append(b)
        else:main.append(b)
    # All bibliography fragments belong to one fold, after the scientific narrative.
    if 'references' in groups:groups.move_to_end('references',last=False)
    document.blocks=main+[b for group in groups.values() for b in group]
    document.reading_sections=[{'id':key,'title':SECTIONS[key][0],'translated':SECTIONS[key][1],'collapsed':True,'block_ids':[b.id for b in group]} for key,group in groups.items()]
    toc=[TocEntry(block_id=b.id,title=b.original,translated=b.translated,level=b.heading_level) for b in main if b.kind=='heading']
    for key,group in groups.items():
        first=next((b for b in group if b.kind=='heading'),group[0])
        if key=='references':
            for b in group:
                if b.kind=='heading' and b.id!=first.id and clean(b.original).lower()=='references':document.block_aliases[b.id]=first.id
        toc.append(TocEntry(block_id=first.id,title=SECTIONS[key][0],translated=SECTIONS[key][1],level=1))
        if key!='references':toc.extend(TocEntry(block_id=b.id,title=b.original,translated=b.translated,level=max(2,b.heading_level)) for b in group if b.kind=='heading' and b.id!=first.id)
    document.toc=toc

def check_source_coverage(document,raw):
    counts=Counter(s.block_id for b in document.blocks for s in b.source)
    counts.update(document.excluded.keys())
    expected=Counter(b.id for b in raw)
    if counts!=expected:
        raise ValueError('自动结构校验后来源覆盖不完整或重复，文献包未发布')
    document.provenance['source_blocks']=len(raw)
    document.provenance['accounted_blocks']=sum(counts.values())
    valid={b.id for b in document.blocks}
    document.block_aliases={k:v for k,v in document.block_aliases.items() if v in valid and k!=v}

def verify_translations(client,blocks,config,title):
    """Different model checks completeness and meaning; numeric invariants remain enforced."""
    if not blocks:return
    producers=OrderedDict()
    for b in blocks:producers.setdefault(b.translated_by or config.translation_model,[]).append(b)
    if len(producers)>1:
        for group in producers.values():verify_translations(client,group,config,title)
        return
    from .structure import translation_numbers_valid,translation_numeric_report,translation_language_valid
    pending={b.id:b for b in blocks};feedback=''
    for target in client.reviewer_targets('translate',next(iter(producers))):
        for attempt in range(config.retry_attempts):
            if not pending:break
            check_cancel(client.cancel);ids=list(pending)
            payload={'target_language':config.target_language,'paper_title':title,'items':[{'id':b.id,'kind':b.kind,'original':b.original,'translated':b.translated,
                'superscript_annotations':[s.text for s in b.styles if s.superscript],
                'numeric_check':translation_numeric_report(b.original,b.translated,b.styles)} for b in pending.values()]}
            def validate(value):
                if Counter(value.get('reviewed_ids',[]))!=Counter(ids):raise LLMResponseError('Review every exact translation ID once.')
                corrections=value.get('corrections',[])
                if not isinstance(corrections,list) or any(not isinstance(x,dict) or x.get('id') not in ids or not isinstance(x.get('translated'),str) or not x['translated'].strip() for x in corrections):raise LLMResponseError('Invalid translation correction.')
                if len({x['id'] for x in corrections})!=len(corrections):raise LLMResponseError('Duplicate translation corrections.')
                if not isinstance(value.get('unresolved_ids',[]),list) or any(x not in ids for x in value.get('unresolved_ids',[])):raise LLMResponseError('Invalid unresolved ID.')
            try:result=client.complete('translation_verify',payload,repair=feedback,validator=validate,target=target)
            except LLMError:
                for b in pending.values():b.translation_error='独立校验服务在重试及备用模型后仍不可用'
                break
            corrections={x['id']:x['translated'] for x in result.get('corrections',[])};failed={}
            for id,b in list(pending.items()):
                candidate=corrections.get(id,b.translated)
                if id in result.get('unresolved_ids',[]):reason='semantic_unresolved'
                elif not candidate.strip() or not translation_language_valid(b,candidate,config.target_language):reason='missing_or_untranslated'
                elif not translation_numbers_valid(b.original,candidate,b.styles):reason='numeric_mismatch'
                else:
                    b.translated=candidate;b.translation_status='translated';b.translation_error=''
                    if id in corrections:b.translated_by=target.model
                    elif not b.translated_by:b.translated_by=config.translation_model
                    del pending[id];continue
                failed[id]=reason;b.translation_error={'semantic_unresolved':'校验 agent 未能确认译文完整性','missing_or_untranslated':'独立校验未能修复缺失或未翻译内容','numeric_mismatch':'独立校验后仍有数值或符号差异'}[reason]
            client.audit({'stage':'translation_verification',**target.audit_identity(),'checked_ids':ids,'rejected':failed,'attempt':attempt+1,'independent_model':True})
            feedback='Repair only these remaining IDs; preserve all explicit Arabic values and signs, and every source clause: '+json.dumps(failed,ensure_ascii=False)
        if not pending:break
    for b in pending.values():
        b.translation_status='review' if b.translated.strip() and translation_language_valid(b,b.translated,config.target_language) else 'unavailable'

