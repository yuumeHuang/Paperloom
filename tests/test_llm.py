from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest
from paperloom.llm import Cancelled, LLMClient, LLMError, parse_json
from paperloom.models import LLMConfig
from paperloom.pipeline import prepare, layout_batches_by_page, layout_evidence
from paperloom.models import RawBlock, LayoutPlan, Page
from paperloom.pipeline import recover_layout_responses, recovered_page_batches
from paperloom.structure import coverage_check, promote_source_title, split_source_headings, graphic_evidence
from paperloom.extract import split_large_font_lines


def install_transport(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr('paperloom.llm.httpx.Client', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


def agent_fixture(payload):
    if 'allowed_sections' in payload:
        entries=[{'id':b['id'],'kind':b['kind'],'section':b['section_hint'],'heading_level':b['heading_level']} for b in payload['blocks']]
        result={'classifications':entries}
        if 'proposal' in payload:result.update(reviewed_ids=[b['id'] for b in payload['blocks']],joins=[])
        return result
    if payload.get('items') and 'translated' in payload['items'][0]:
        return {'reviewed_ids':[b['id'] for b in payload['items']],'corrections':[],'unresolved_ids':[]}
    return None


def test_compatible_protocol_cache_and_auth_not_persisted(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        body = json.loads(request.content); calls.append(body)
        assert request.url.path == '/v1/chat/completions'
        assert request.headers['authorization'] == 'Bearer test-key'
        assert body['response_format'] == {'type': 'json_object'}
        assert body['max_completion_tokens'] == 8192
        assert 'untrusted DATA' in body['messages'][0]['content']
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"translations":[{"id":"b1","translated":"数据 2"}]}'}, 'finish_reason': 'stop'}], 'usage': {'total_tokens': 20}})
    install_transport(monkeypatch, handler)
    cfg = LLMConfig(base_url='https://example.test/v1', api_key='test-key', layout_model='layout', translation_model='translator', token_parameter='max_completion_tokens')
    audit = []; client = LLMClient(cfg, tmp_path, threading.Event(), audit.append)
    payload = {'items': [{'id': 'b1', 'original': 'data 2'}]}
    assert client.complete('translate', payload) == client.complete('translate', payload)
    assert len(calls) == 1 and len(audit) == 1
    assert calls[0]['model'] == 'translator'
    assert all('test-key' not in p.read_text() for p in tmp_path.glob('*.json'))


def test_llm_guided_pipeline_repairs_invalid_layout(tmp_path, monkeypatch):
    # Use an actual PDF and an HTTP-compatible response double, never a prebuilt document.
    import shutil
    root = Path(__file__).resolve().parent.parent
    shutil.copyfile(root / 'web/dist/demo/source.pdf', tmp_path / 'source.pdf')
    layout_calls = 0
    translate_calls = 0
    repaired_translation = False
    def handler(request):
        nonlocal layout_calls, translate_calls, repaired_translation
        body = json.loads(request.content); payload = json.loads(body['messages'][1]['content'])
        extra=agent_fixture(payload)
        if extra is not None:return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(extra)},'finish_reason':'stop'}]})
        if 'blocks' in payload:
            layout_calls += 1
            ids = [b['id'] for b in payload['blocks']]
            if layout_calls == 1:
                result = {'nodes': [{'kind': 'paragraph', 'source_ids': ['invented']}], 'ignored': [], 'regions': []}
            else:
                if layout_calls == 2:
                    assert 'Previous output failed validation' in body['messages'][0]['content']
                result = {'nodes': [{'kind': 'paragraph', 'source_ids': [id], 'confidence': .9} for id in ids], 'ignored': [], 'regions': []}
        else:
            translate_calls += 1
            if translate_calls == 1:
                return httpx.Response(200, json={'choices': [{'message': {'content': '{"translations":['}, 'finish_reason': 'stop'}]})
            if 'Previous output failed validation' in body['messages'][0]['content']:
                repaired_translation = True
            result = {'translations': [{'id': b['id'], 'translated': '译文：' + b['original']} for b in payload['items']]}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result)}, 'finish_reason': 'stop'}]})
    install_transport(monkeypatch, handler)
    cfg = LLMConfig(base_url='https://example.test/v1', layout_model='layout', translation_model='translation')
    doc = prepare(tmp_path / 'source.pdf', tmp_path, 'llm-test', 'test.pdf', cfg, threading.Event(), lambda *a: None, lambda e: None)
    assert layout_calls == 3  # One repaired first page, then one complete second page.
    assert doc.provenance['accounted_blocks'] == doc.provenance['source_blocks']
    assert all(b.translation_status in ('translated','not_required') for b in doc.blocks)
    assert not any(i.code == 'layout_fallback' for i in doc.issues)
    assert repaired_translation
    calls_before=translate_calls
    repeated=prepare(tmp_path/'source.pdf',tmp_path,'llm-test','test.pdf',cfg,threading.Event(),lambda *a:None,lambda e:None)
    assert translate_calls==calls_before and all(b.translation_status in ('translated','not_required') for b in repeated.blocks)
    changed=cfg.model_copy(update={'glossary':{'data':'资料'}})
    prepare(tmp_path/'source.pdf',tmp_path,'llm-test','test.pdf',changed,threading.Event(),lambda *a:None,lambda e:None)
    assert translate_calls>calls_before  # A changed glossary cannot reuse the previous translations.


def test_optional_layout_values_do_not_reject_complete_page():
    plan = LayoutPlan.model_validate({'nodes': [{'kind': 'paragraph', 'source_ids': ['x'], 'heading_level': None, 'label': None}]})
    assert plan.nodes[0].heading_level == 1 and plan.nodes[0].label == ''
    with pytest.raises(ValueError):
        LayoutPlan.model_validate({'nodes': [{'kind': 'heading', 'source_ids': ['x'], 'heading_level': 7}]})


def test_title_mixed_font_split_preserves_text_and_source_coverage():
    native={'lines': [{'bbox': [10, 10, 90, 20], 'spans': [{'text': 'doi:10.1/test', 'size': 8}]},
                      {'bbox': [10, 24, 150, 52], 'spans': [{'text': 'Research title', 'size': 26}]}]}
    parent=RawBlock(id='x', page=1, bbox=[10,10,150,52], text='doi:10.1/test Research title', font_size=26)
    raw=split_large_font_lines(parent,native)
    assert [b.text for b in raw] == ['doi:10.1/test','Research title']
    assert all(b.parent_id == 'x' for b in raw)
    plan=LayoutPlan.model_validate({'nodes': [{'kind': 'metadata', 'source_ids': [b.id for b in raw]}]})
    promote_source_title(plan,raw,'Research title')
    assert next(n for n in plan.nodes if n.kind == 'title').source_ids == [raw[1].id]
    coverage_check(plan,raw,[Page(number=1,width=200,height=300,preview='p.png')],[])


def test_recovered_layout_is_revalidated_and_never_masks_missing_sources(tmp_path):
    key='a'*64; (tmp_path/'cache').mkdir()
    (tmp_path/'audit.jsonl').write_text(json.dumps({'stage':'layout','model':'m','input_hash':key})+'\n')
    (tmp_path/'cache'/f'{key}.json').write_text(json.dumps({'nodes':[{'kind':'metadata','source_ids':['x']}],
        'ignored':[{'source_id':'x','reason':'header'}]}))
    raw=[RawBlock(id='x-s01',parent_id='x',page=1,bbox=[1,1,90,10],text='doi'),
         RawBlock(id='x-s02',parent_id='x',page=1,bbox=[1,20,90,40],text='title')]
    candidate=recover_layout_responses(tmp_path,raw,'m')[frozenset(b.id for b in raw)][0][0]
    pages=[Page(number=1,width=100,height=100,preview='p.png')]
    coverage_check(candidate,raw,pages,[])
    assert not candidate.ignored  # Retained text takes priority over a contradictory exclusion.
    with pytest.raises(ValueError,match='missing'):
        coverage_check(candidate,raw+[RawBlock(id='y',page=1,bbox=[1,50,90,60],text='missing')],pages,[])


def test_mixed_method_heading_and_body_have_disjoint_sources_after_recovery(tmp_path):
    native={'lines':[{'bbox':[10,10,90,20],'spans':[{'text':'Data availability','size':10.75,'flags':16}]},
                     {'bbox':[10,23,90,33],'spans':[{'text':'The data are available.','size':8.25,'flags':0}]}]}
    parent=RawBlock(id='x',page=1,bbox=[10,10,90,33],text='Data availability The data are available.')
    raw=split_large_font_lines(parent,native)
    assert len(raw)==2
    key='b'*64;(tmp_path/'cache').mkdir()
    (tmp_path/'audit.jsonl').write_text(json.dumps({'stage':'layout','model':'m','input_hash':key})+'\n')
    (tmp_path/'cache'/f'{key}.json').write_text(json.dumps({'nodes':[{'kind':'heading','source_ids':['x']},
                                                                 {'kind':'paragraph','source_ids':['x']}]}))
    plan=recover_layout_responses(tmp_path,raw,'m')[frozenset(b.id for b in raw)][0][0]
    coverage_check(plan,raw,[Page(number=1,width=100,height=100,preview='p.png')],[])
    assert [n.source_ids for n in plan.nodes]==[[raw[0].id],[raw[1].id]]
    whole=LayoutPlan.model_validate({'nodes':[{'kind':'paragraph','source_ids':[b.id for b in raw]}]})
    split_source_headings(whole,raw)
    assert [(n.kind,n.source_ids) for n in whole.nodes]==[('heading',[raw[0].id]),('paragraph',[raw[1].id])]


def test_recovered_partition_preserves_coverage_when_segmentation_budget_moves():
    raw=[RawBlock(id=str(i),page=1,bbox=[1,20+i*10,90,29+i*10],text='body') for i in range(3)]
    pages=[Page(number=1,width=100,height=100,preview='p.png')]
    first=LayoutPlan.model_validate({'nodes':[{'kind':'paragraph','source_ids':['0','1']}]})
    second=LayoutPlan.model_validate({'nodes':[{'kind':'paragraph','source_ids':['2']}]})
    restored=recovered_page_batches(raw,pages,[],{frozenset(['0','1']):[(first,'a',None)],frozenset(['2']):[(second,'b',None)]})
    assert [[b.id for b in batch] for batch in restored]==[['0','1'],['2']]
    # An uncovered source forces a fresh complete page, never silently loses the tail.
    fresh=recovered_page_batches(raw,pages,[],{frozenset(['0','1']):[(first,'a',None)]})
    assert [[b.id for b in batch] for batch in fresh]==[['0','1','2']]


def test_numeric_checks_allow_scale_translation_and_prime_abbreviation_expansion():
    from paperloom.structure import translation_numbers_valid
    assert translation_numbers_valid('2 million nuclei and 3′ UTR','200 万个细胞核和 3′ 非翻译区（3′ UTR）')
    assert translation_numbers_valid('3\u2009′ UTR','3′ 非翻译区（3′ UTR）')
    assert translation_numbers_valid('P < 5 × 10−8','P < 5 × 10⁻⁸')
    assert not translation_numbers_valid('2 million nuclei','20 万个细胞核')
    assert not translation_numbers_valid('3′ UTR','5′ UTR')
    assert not translation_numbers_valid('3′ UTR','3′ UTR 和 5′ UTR')


def test_appendix_title_cannot_replace_first_page_paper_title():
    raw=[RawBlock(id='main',page=1,bbox=[10,10,90,40],text='Research title',font_size=26),
         RawBlock(id='appendix',page=2,bbox=[10,10,90,40],text='Reporting Summary',font_size=26)]
    plan=LayoutPlan.model_validate({'nodes':[{'kind':'metadata','source_ids':['main']},{'kind':'title','source_ids':['appendix']}]})
    promote_source_title(plan,raw,'Research title')
    assert [(n.kind,n.source_ids) for n in plan.nodes]==[('title',['main']),('heading',['appendix'])]


def test_figure_region_requires_graphic_evidence_instead_of_caption_text():
    from paperloom.models import Region,Asset
    raw=[RawBlock(id='caption',page=2,bbox=[10,10,90,80],text='Fig. 1. Full legend')]
    plan=LayoutPlan.model_validate({'nodes':[{'kind':'caption','source_ids':['caption']}]})
    region=Region(page=2,bbox=[10,10,90,80],kind='figure',label='Fig. 1')
    assert not graphic_evidence(region,plan,raw,[])
    image=Asset(id='image',kind='figure',page=2,bbox=[20,20,80,70],path='assets/image.png')
    assert graphic_evidence(region,plan,raw,[image])


def test_cross_page_prose_rejoins_around_floating_caption_without_losing_source():
    from paperloom.models import Block,Source
    from paperloom.structure import merge_prose_continuations,translation_language_valid
    first=Block(id='a',kind='paragraph',original='We link noncoding',source=[Source(page=1,bbox=[1,1,90,20],block_id='a')])
    caption=Block(id='c',kind='caption',original='Fig. 1. Caption',source=[Source(page=2,bbox=[1,1,90,20],block_id='c')])
    last=Block(id='b',kind='paragraph',original='risk variants to regulation.',source=[Source(page=2,bbox=[1,30,90,50],block_id='b')])
    joined=merge_prose_continuations([first,caption,last])
    assert [b.id for b in joined]==['a','c'] and joined[0].original=='We link noncoding risk variants to regulation.'
    assert [s.block_id for b in joined for s in b.source]==['a','b','c']
    assert not translation_language_valid(joined[0],joined[0].original,'简体中文')
    assert translation_language_valid(joined[0],'非编码风险变异影响调控。','简体中文')


def test_cancel_before_call_and_actionable_error(tmp_path, monkeypatch):
    event = threading.Event(); event.set()
    client = LLMClient(LLMConfig(layout_model='model'), tmp_path, event, lambda e: None)
    with pytest.raises(Cancelled): client.complete('layout', {})
    event.clear()
    install_transport(monkeypatch, lambda request: httpx.Response(401, json={'error': 'may contain sensitive provider details'}))
    with pytest.raises(LLMError, match='API Key') as exc: client.complete('layout', {})
    assert 'sensitive' not in str(exc.value)


def test_one_untranslated_item_does_not_discard_other_valid_translations(tmp_path,monkeypatch):
    import shutil
    root=Path(__file__).resolve().parent.parent
    shutil.copyfile(root/'web/dist/demo/source.pdf',tmp_path/'source.pdf')
    bad_id=None
    def handler(request):
        nonlocal bad_id
        body=json.loads(request.content);payload=json.loads(body['messages'][1]['content'])
        extra=agent_fixture(payload)
        if extra is not None:return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(extra)},'finish_reason':'stop'}]})
        if 'blocks' in payload:
            result={'nodes':[{'kind':'paragraph','source_ids':[b['id']]} for b in payload['blocks']]}
        else:
            if bad_id is None:
                bad_id=next(b['id'] for b in payload['items'] if b['kind']=='paragraph' and len(b['original'])>100)
            result={'translations':[{'id':b['id'],'translated':b['original'] if b['id']==bad_id else '译文：'+b['original']} for b in payload['items']]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)},'finish_reason':'stop'}]})
    install_transport(monkeypatch,handler)
    cfg=LLMConfig(base_url='https://example.test/v1',layout_model='layout',translation_model='translation')
    doc=prepare(tmp_path/'source.pdf',tmp_path,'partial','test.pdf',cfg,threading.Event(),lambda *a:None,lambda e:None)
    assert [b.id for b in doc.blocks if b.translation_status=='unavailable']==[bad_id]
    assert all(b.translation_status=='translated' for b in doc.blocks if b.id!=bad_id)


def test_numbered_reference_correction_respects_section_boundaries():
    from paperloom.models import Block
    from paperloom.structure import normalize_reference_sections
    blocks=[Block(id='h',kind='heading',original='References',source=[]),
            Block(id='r',kind='paragraph',original='1. Smith, J. Scientific title. Journal 1, 2–3 (2022).',source=[]),
            Block(id='m',kind='heading',original='Discussion',source=[]),
            Block(id='p',kind='paragraph',original='1. A numbered argument about results (2022).',source=[])]
    originals=[b.original for b in blocks]
    normalize_reference_sections(blocks)
    assert [b.kind for b in blocks]==['heading','reference','heading','paragraph']
    assert [b.original for b in blocks]==originals


def test_json_rejects_partial_and_accepts_fences():
    assert parse_json('```json\n{"ok":true}\n```') == {'ok': True}
    with pytest.raises(LLMError): parse_json('{"ok":')


def test_layout_batches_preserve_whole_pages_and_exclusive_coverage():
    raw = [RawBlock(id=f'p{p}-b{n}', page=p, bbox=[10.12345, 20.12345, 60, 40], text='label')
           for p in [1, 2] for n in range(80)]
    batches = list(layout_batches_by_page(raw))
    assert len(batches) == 2
    assert all(len({b.page for b in batch}) == 1 for batch in batches)
    assert [b.id for batch in batches for b in batch] == [b.id for b in raw]
    tiny = list(layout_batches_by_page(raw, budget=500))
    assert len(tiny) > 2 and [b.id for batch in tiny for b in batch] == [b.id for b in raw]
    assert layout_evidence(raw[0])['bbox'][:2] == [10.12, 20.12]
