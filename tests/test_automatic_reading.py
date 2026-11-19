"""Regression cases for source preservation, independent agents and fallback."""
import json
import threading
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from paperloom.extract import split_large_font_lines
from paperloom.llm import LLMClient, LLMResponseError
from paperloom.models import Block, Document, LayoutPlan, LLMConfig, Metadata, Page, RawBlock, Source, TextStyle
from paperloom.package import validate_document
from paperloom.pipeline import translate_batch
from paperloom.reading import (apply_heading_joins, apply_paragraph_joins, assemble_reading,
    assign_sections, check_source_coverage, normalize_hierarchy, remove_notices, stable_ids, verify_structure,verify_translations)
from paperloom.server import create_app
from paperloom.structure import coverage_check, split_source_headings, translation_numbers_valid


def block(id, text, kind='paragraph', page=1, level=1, bold=False, size=9):
    return Block(id=id,original=text,kind=kind,heading_level=level,
        styles=[TextStyle(text=text,bold=bold,size=size)],
        source=[Source(page=page,bbox=[10,40,200,60],block_id='s-'+id)])


def document(blocks):
    return Document(id='regression',metadata=Metadata(),blocks=blocks,assets=[],
        pages=[Page(number=i,width=600,height=800,preview=f'pages/{i}.png') for i in (1,2,3)])


def transport(monkeypatch,handler):
    original=httpx.Client
    monkeypatch.setattr('paperloom.llm.httpx.Client',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))


def response(value):
    return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}]})


def test_main_methods_subsection_and_body_split_without_loss():
    native={'lines':[{'bbox':[10,10+i*12,200,22+i*12],'spans':[{'text':text,'size':size,'flags':flags}]} for i,(text,size,flags) in enumerate([
        ('Methods',10.75,16),('snRNA-seq data collection',8.75,16),('Scientific methods and data.',8.3,0)])]}
    raw=split_large_font_lines(RawBlock(id='x',page=1,bbox=[10,10,200,50],text='Methods snRNA-seq data collection Scientific methods and data.'),native)
    plan=LayoutPlan(nodes=[{'kind':'heading','source_ids':[r.id for r in raw]}])
    split_source_headings(plan,raw)
    assert [n.kind for n in plan.nodes]==['heading','heading','paragraph']
    assert [n.source_ids for n in plan.nodes]==[[r.id] for r in raw]
    coverage_check(plan,raw,[Page(number=1,width=600,height=800,preview='pages/1.png')],[])


def test_method_parent_survives_page_break_and_reference_groups_unify():
    blocks=[block('res','Results','heading'),block('r1','References','heading'),block('ref1','1. Main citation.','reference'),
        block('m','Methods','heading',2),block('rna','RNA-seq alignment','heading',2,2),
        block('hits','HITS-CLIP data analysis','heading',3),block('egfr','EGFR pathway expression','heading',3),
        block('r2','References','heading',3),block('ref2','94. Methods citation.','reference',3)]
    normalize_hierarchy(blocks);assign_sections(blocks)
    assert [b.heading_level for b in blocks if b.id in ('hits','egfr')]==[2,2]
    doc=document(blocks);assemble_reading(doc)
    assert [b.id for b in doc.blocks[:5]]==['res','m','rna','hits','egfr']
    refs=doc.reading_sections[0]
    assert refs['id']=='references' and refs['collapsed'] is True
    assert refs['block_ids']==['r1','ref1','r2','ref2']
    assert sum(t.title=='References' for t in doc.toc)==1
    assert doc.block_aliases['r2']=='r1'

def test_semantic_hierarchy_for_uncommon_methods_heading_is_respected():
    blocks=[block('parent','Experimental procedures','heading'),block('child','Results visualization','heading',2,2)]
    for b in blocks:b.section='methods'
    normalize_hierarchy(blocks,trusted=True)
    assert [b.heading_level for b in blocks]==[1,2]


def test_mixed_metadata_drops_only_notice_and_source_ledger_stays_exact():
    raw=[RawBlock(id='s-meta',page=1,bbox=[10,40,200,60],text='Published 2026'),
        RawBlock(id='s-notice',page=1,bbox=[10,60,200,80],text='Check for updates'),
        RawBlock(id='s-science',page=1,bbox=[10,80,200,100],text='Scientific results and conclusions.')]
    meta=block('meta','Published 2026 Check for updates','metadata');meta.source=[Source(page=1,bbox=r.bbox,block_id=r.id) for r in raw[:2]]
    science=block('science',raw[2].text);doc=document([meta,science])
    remove_notices(doc,raw);check_source_coverage(doc,raw)
    assert doc.blocks[0].original=='Published 2026'
    assert doc.excluded=={'s-notice':'publisher_notice'}
    assert doc.provenance['source_blocks']==doc.provenance['accounted_blocks']==3
    doc.blocks[1].source=[]
    with pytest.raises(ValueError,match='覆盖'):check_source_coverage(doc,raw)


def test_resegmented_ids_match_content_instead_of_shifted_source_suffix():
    old=document([block('b00001','Methods snRNA-seq collection','heading'),block('b00002','Study details.')])
    new=document([block('temp1','Methods','heading'),block('temp2','snRNA-seq collection','heading'),block('temp3','Study details.')])
    new.blocks[0].source=old.blocks[0].source
    new.blocks[1].source=old.blocks[1].source  # Reused suffix now means another piece.
    stable_ids(new,old)
    assert new.blocks[2].id=='b00002' and new.blocks[0].id!='b00001'
    assert new.block_aliases['b00001']==new.blocks[0].id


def test_heading_join_keeps_main_parent_and_merges_only_wrapped_heading():
    doc=document([block('m','Methods','heading',bold=True,size=11),block('s','Data collection','heading',bold=True,size=9),
        block('h1','APC nonsense and frameshift mutations','heading',bold=True),block('h2','enhance distal poly(A) site selection','heading',bold=True)])
    apply_heading_joins(doc,[{'block_ids':['m','s'],'heading_level':1},{'block_ids':['h1','h2'],'heading_level':2}])
    assert [b.id for b in doc.blocks]==['m','s','h1']
    assert doc.blocks[2].original.endswith('poly(A) site selection') and len(doc.blocks[2].source)==2
    assert doc.block_aliases['h2']=='h1'


def test_agent_can_restore_uppercase_continuation_across_columns():
    doc=document([block('p1','The locus acts through an'),block('p2','APA-dependent mechanism.'),block('p3','An independent paragraph.')])
    apply_paragraph_joins(doc,[{'block_ids':['p1','p2']}])
    assert doc.blocks[0].original=='The locus acts through an APA-dependent mechanism.'
    assert doc.block_aliases['p2']=='p1' and len(doc.blocks[0].source)==2
    assert doc.blocks[1].id=='p3'

def test_full_page_floats_do_not_break_an_unfinished_paragraph():
    from paperloom.structure import merge_prose_continuations
    before=block('a','We found a correlation with previously',page=1)
    figure=block('fig','Figure legend.','caption',2)
    after=block('b','identified genes.',page=3)
    result=merge_prose_continuations([before,figure,after])
    assert [b.id for b in result]==['a','fig'] and result[0].original.endswith('previously identified genes.')
    assert len(result[0].source)==2

def test_verified_sans_serif_wrapped_heading_can_join_without_bold_flag():
    doc=document([block('a','APC expression correlates with genes involved','heading',level=2,size=9.2),block('b','in growth factor signaling','heading',level=2,size=9.2)])
    apply_heading_joins(doc,[{'block_ids':['a','b'],'heading_level':2}])
    assert len(doc.blocks)==1 and doc.block_aliases['b']=='a'


def test_numeric_check_ignores_prime_glyph_and_identifier_repetition_but_keeps_data():
    assert translation_numbers_valid('3’aQTL with PAK6',"3′aQTL 与 PAK6，PAK6 编码一种蛋白")
    assert translation_numbers_valid('2-by-2 matrix; day-40 organoids','2×2 矩阵；第 40 天类器官')
    assert not translation_numbers_valid('p < 0.05; n = 120; -5','p > 0.05；n = 120；-5')
    assert not translation_numbers_valid('p < 0.05; n = 120; -5','p < 0.05；n = 120；5')
    assert not translation_numbers_valid('p < 0.05; n = 120','p < 0.05；n = 12')
    assert translation_numbers_valid('neurons10,77 and PAK6','神经元10,77 与 PAK6，PAK6 编码一种蛋白')
    assert not translation_numbers_valid('neurons10,77 and PAK6','神经元10,77 与 PAK7')
    styles=[TextStyle(text='DaPars2'),TextStyle(text='25',superscript=True)]
    assert translation_numbers_valid('DaPars225','DaPars2 25',styles)
    assert not translation_numbers_valid('DaPars225','DaPars2 26',styles)
    assert translation_numbers_valid('103','10³',[TextStyle(text='10'),TextStyle(text='3',superscript=True)])
    refs=[TextStyle(text='SRSF3'),TextStyle(text='38,39',superscript=True)]
    assert translation_numbers_valid('SRSF338,39','SRSF338,39',refs)
    assert translation_numbers_valid('tests103,104','检测103,104',[TextStyle(text='tests'),TextStyle(text='103,104',superscript=True)])
    assert translation_numbers_valid('results1–4','结果1–4',[TextStyle(text='results'),TextStyle(text='1–4',superscript=True)])
    assert translation_numbers_valid('n = 2–5','n = 2 至 5')
    assert not translation_numbers_valid('10−5','10⁵')
    exponent=[TextStyle(text='10−'),TextStyle(text='75',superscript=True)]
    assert translation_numbers_valid('10−75','10−75',exponent)
    assert not translation_numbers_valid('10−75','10⁷⁵',exponent)

def test_translation_superscripts_do_not_merge_citation_into_gene_name():
    from paperloom.structure import project_translation_styles
    b=block('gene','SRSF338,39');b.styles=[TextStyle(text='SRSF3'),TextStyle(text='38,39',superscript=True)];b.translated='调控因子 SRSF338,39。'
    project_translation_styles(b)
    assert ''.join(s.text for s in b.translated_styles)==b.translated
    assert [s.text for s in b.translated_styles if s.superscript]==['38,39']


def test_retry_auth_fallback_independent_next_model_and_secret_isolation(tmp_path,monkeypatch):
    calls=[];audit=[]
    cfg=LLMConfig(base_url='https://primary.test/v1',api_key='PRIMARY_TEST_SECRET',layout_model='main',translation_model='main',retry_attempts=2,
        fallbacks=[{'base_url':'https://backup.test/v1','api_key':'BACKUP_TEST_SECRET','models':['backup','judge']}])
    def handler(request):
        body=json.loads(request.content);calls.append((request.url.host,body['model'],request.headers.get('authorization')))
        if request.url.host=='primary.test':return httpx.Response(429,json={'error':'PRIMARY_TEST_SECRET'})
        return response({'ok':True})
    transport(monkeypatch,handler);client=LLMClient(cfg,tmp_path,threading.Event(),audit.append)
    monkeypatch.setattr(client.cancel,'wait',lambda _:False)
    assert client.complete('structure',{})=={'ok':True}
    assert [m for _,m,_ in calls]==['main','main','backup']
    assert all(key=='Bearer BACKUP_TEST_SECRET' for host,_,key in calls if host=='backup.test')
    assert client.reviewer_targets('structure')[0].model=='judge'
    assert all(t.identity!=('https://backup.test/v1','backup') for t in client.reviewer_targets('structure'))
    persisted=json.dumps(audit)+json.dumps(cfg.safe_dump())+''.join(p.read_text() for p in tmp_path.glob('*.json'))
    assert 'PRIMARY_TEST_SECRET' not in persisted and 'BACKUP_TEST_SECRET' not in persisted
    with pytest.raises(ValueError):LLMConfig(base_url='https://primary.test/v1',fallbacks=[{'base_url':'https://foreign.test/v1','models':['b'],'inherit_primary_key':True}])


def test_partial_translation_acceptance_retries_only_bad_ids_then_falls_back(tmp_path,monkeypatch):
    calls=[];blocks=[block('good','Data 2'),block('bad','Results 3')]
    cfg=LLMConfig(base_url='https://provider.test/v1',layout_model='judge',translation_model='main',retry_attempts=2,
        fallbacks=[{'base_url':'https://provider.test/v1','models':['backup','other-judge']}])
    def handler(request):
        body=json.loads(request.content);payload=json.loads(body['messages'][1]['content']);calls.append((body['model'],payload))
        if 'translated' in payload['items'][0]:return response({'reviewed_ids':[b['id'] for b in payload['items']],'corrections':[],'unresolved_ids':[]})
        if body['model']=='main':return response({'translations':[{'id':b['id'],'translated':'数据 2' if b['id']=='good' else '', 'id_note':'extraneous model annotation'} for b in payload['items']]})
        return response({'translations':[{'id':b['id'],'translated':'结果 3'} for b in payload['items']]})
    transport(monkeypatch,handler);client=LLMClient(cfg,tmp_path,threading.Event(),lambda _:None)
    result=translate_batch(client,blocks,cfg,'Title')
    assert result=={'good':'数据 2','bad':'结果 3'} and all(b.translation_status=='translated' for b in blocks)
    assert [b['id'] for b in calls[1][1]['items']]==['bad']
    assert calls[2][0]=='backup' and calls[3][0]=='backup' and calls[4][0]=='other-judge'
    assert [b['id'] for b in calls[3][1]['items']]==['good']
    assert [b['id'] for b in calls[4][1]['items']]==['bad']

def test_independent_checker_retries_only_failed_correction(tmp_path,monkeypatch):
    blocks=[block('good','Data 2'),block('bad','Result 3')]
    blocks[0].translated='数据 2';blocks[1].translated='结果 4';calls=[]
    cfg=LLMConfig(base_url='https://provider.test/v1',layout_model='judge',translation_model='main',retry_attempts=2)
    def handler(request):
        payload=json.loads(json.loads(request.content)['messages'][1]['content']);calls.append(payload)
        return response({'reviewed_ids':[b['id'] for b in payload['items']],'corrections':[{'id':'bad','translated':'结果 4' if len(calls)==1 else '结果 3'}],'unresolved_ids':[]})
    transport(monkeypatch,handler);verify_translations(LLMClient(cfg,tmp_path,threading.Event(),lambda _:None),blocks,cfg,'Title')
    assert [b['id'] for b in calls[1]['items']]==['bad']
    assert all(b.translation_status=='translated' for b in blocks)


def test_uncommon_publisher_notice_requires_two_models_to_agree(tmp_path,monkeypatch):
    doc=document([block('real','Scientific result.'),block('notice','Visit our article metrics panel.'),block('disputed','Experimental design details.')])
    raw=[RawBlock(id=b.source[0].block_id,page=1,bbox=b.source[0].bbox,text=b.original) for b in doc.blocks]
    cfg=LLMConfig(base_url='https://provider.test/v1',layout_model='main',translation_model='judge',retry_attempts=1)
    seen=[]
    def handler(request):
        body=json.loads(request.content);seen.append(body['model']);payload=json.loads(body['messages'][1]['content'])
        entries=[{'id':b['id'],'kind':b['kind'],'heading_level':1,'section':'publisher_notice' if b['id']=='notice' or body['model']=='judge' and b['id']=='disputed' else 'body'} for b in payload['blocks']]
        value={'classifications':entries,'reading_order':['real','notice','disputed']}
        if 'proposal' in payload:value.update(reviewed_ids=[b['id'] for b in payload['blocks']],joins=[])
        return response(value)
    transport(monkeypatch,handler);verify_structure(doc,raw,LLMClient(cfg,tmp_path,threading.Event(),lambda _:None),cfg,lambda *a:None)
    assert seen==['main','judge'] and [b.id for b in doc.blocks]==['real','disputed']
    assert doc.blocks[1].section=='publication_information'
    check_source_coverage(doc,raw)


def test_stop_record_delete_removes_full_task_only_and_rejects_active(tmp_path):
    root=tmp_path/'jobs';jid=str(uuid.uuid4());task=root/jid;task.mkdir(parents=True)
    (task/'status.json').write_text(json.dumps({'id':jid,'status':'cancelled','created':'2026-01-01'}))
    (task/'source.pdf').write_bytes(b'%PDF test');(task/'cache').mkdir();(task/'cache'/'response.json').write_text('{}')
    outside=tmp_path/'keep.txt';outside.write_text('user file')
    with TestClient(create_app(root)) as client:
        manager=client.app.state.jobs;manager.events[jid]=threading.Event()
        assert client.delete('/api/jobs/'+jid).status_code==409 and task.exists()
        manager.events.pop(jid)
        assert client.delete('/api/jobs/'+jid).status_code==200 and not task.exists()
        assert client.get('/api/jobs/'+jid).status_code==404 and outside.read_text()=='user file'


def test_single_model_is_rejected_before_pdf_processing(tmp_path):
    with TestClient(create_app(tmp_path/'jobs')) as client:
        r=client.post('/api/jobs',files={'file':('paper.pdf',b'%PDF test')},data={'config':json.dumps({'layout_model':'same','translation_model':'same'})})
        assert r.status_code==422 and '另一个模型' in r.json()['detail']
        assert not list((tmp_path/'jobs').glob('*/source.pdf'))

def test_same_model_on_another_provider_is_not_an_independent_checker(tmp_path):
    cfg=LLMConfig(layout_model='main',translation_model='main',fallbacks=[{'base_url':'https://second.test/v1','models':['main','judge']}])
    client=LLMClient(cfg,tmp_path,threading.Event(),lambda _:None)
    assert [t.model for t in client.reviewer_targets('translate')]==['judge']


def test_bad_fold_members_and_alias_targets_cannot_enter_package():
    doc=document([block('p','Body.')]);doc.reading_sections=[{'id':'references','title':'References','translated':'参考文献','collapsed':True,'block_ids':['missing']}]
    with pytest.raises(ValueError,match='分组'):validate_document(doc)
    doc.reading_sections=[];doc.block_aliases={'old':'missing'}
    with pytest.raises(ValueError,match='别名'):validate_document(doc)
