from __future__ import annotations

import json
import threading
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from paperloom.extract import extract
from paperloom.models import LLMConfig, LayoutPlan, Page, RawBlock
from paperloom.package import verify_package
from paperloom.pipeline import prepare
from paperloom.server import create_app
from paperloom.structure import coverage_check, heuristic_plan, label_key, numeric_tokens, translation_numbers_valid
from paperloom.structure import associate_assets, normalize_caption_notes, remove_repeated_margins
from paperloom.models import Asset, Block, Document, Metadata, Source

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / 'web/dist/demo/source.pdf'


@pytest.fixture
def sample(tmp_path):
    import shutil
    shutil.copyfile(PDF, tmp_path / 'source.pdf')
    return tmp_path


def test_real_pdf_extract_layout_and_package(sample):
    doc = prepare(sample / 'source.pdf', sample, 'test-doc', 'example.pdf', LLMConfig(enabled=False), threading.Event(), lambda *args: None, lambda e: None)
    assert len(doc.pages) == 2
    assert any(a.kind == 'table' and a.table for a in doc.assets)
    assert doc.provenance['source_blocks'] == doc.provenance['accounted_blocks']
    assert len(doc.toc) >= 5
    assert all(b.translation_status != 'translated' for b in doc.blocks)
    roundtrip = verify_package(sample / 'paper.folio')
    assert roundtrip.model_dump() == doc.model_dump()
    assert any(b.references['original'] for b in doc.blocks)


@pytest.mark.parametrize('source_ids', [['missing'], ['x', 'x'], []])
def test_layout_rejects_loss_duplicate_and_unknown_ids(source_ids):
    raw = [RawBlock(id='x', page=1, bbox=[10, 40, 90, 70], text='evidence')]
    page = [Page(number=1, width=100, height=100, preview='pages/1.png')]
    if not source_ids:
        plan = LayoutPlan(nodes=[])
    else:
        plan = LayoutPlan(nodes=[{'kind': 'paragraph', 'source_ids': source_ids}])
    with pytest.raises(ValueError): coverage_check(plan, raw, page, [])


def test_layout_cannot_discard_body_or_use_invalid_region():
    raw = [RawBlock(id='x', page=1, bbox=[10, 40, 90, 70], text='evidence')]
    page = [Page(number=1, width=100, height=100, preview='pages/1.png')]
    with pytest.raises(ValueError):
        coverage_check(LayoutPlan(nodes=[], ignored=[{'source_id': 'x', 'reason': 'header'}]), raw, page, [])
    plan = LayoutPlan(nodes=[{'kind': 'paragraph', 'source_ids': ['x']}], regions=[{'kind': 'figure', 'page': 1, 'bbox': [0, 0, 200, 20]}])
    with pytest.raises(ValueError): coverage_check(plan, raw, page, [])


def test_two_column_order_is_not_row_interleaving():
    raw = [RawBlock(id=id, page=1, bbox=bbox, text=text) for id, bbox, text in [
        ('l1', [10, 20, 45, 35], 'Left first.'), ('r1', [55, 20, 95, 35], 'Right first.'),
        ('l2', [10, 40, 45, 55], 'Left second.'), ('r2', [55, 40, 95, 55], 'Right second.')]]
    # Real PDF widths make a column gutter unambiguous.
    for b in raw: b.bbox = [v * 6 for v in b.bbox]
    plan = heuristic_plan(raw, [Page(number=1, width=600, height=700, preview='pages/1.png')], [])
    assert [n.source_ids[0] for n in plan.nodes] == ['l1', 'l2', 'r1', 'r2']


def test_numeric_and_reference_normalization():
    from paperloom.structure import MENTION
    assert [m.group() for m in MENTION.finditer('table in the text; Table IV; Fig. 2a; Fig. 3abc')]==['Table IV','Fig. 2a']
    assert translation_numbers_valid('p = 0.05; n = 120; Fig. 2a', 'p = 0.05；n = 120；Fig. 2a')
    assert not translation_numbers_valid('p < 0.05; n = 120', 'p < 0.5；n = 120')
    assert not translation_numbers_valid('-5 to 2', '5 至 2')
    assert not translation_numbers_valid('p < 0.05', 'p > 0.05')
    assert translation_numbers_valid('p <= 0.05', 'p ≤ 0.05')
    assert label_key('Fig. 2a') == label_key('Figure 2')
    assert label_key('Supplementary Fig. S1') != label_key('Fig. 1')


@pytest.mark.parametrize('figure_page', [1, 3])
def test_extended_figure_binds_legend_on_adjacent_page_without_wrong_label(figure_page):
    blocks = [Block(id='note', kind='caption', original='Extended Data Fig. 1 | See next page for caption.',
                    source=[Source(page=1, bbox=[10, 80, 90, 90], block_id='s1')]),
              Block(id='legend', kind='caption', original='Extended Data Fig. 1 | Complete scientific legend.',
                    label='Extended Data Fig. 1', source=[Source(page=2, bbox=[10, 10, 90, 30], block_id='s2')])]
    assets = [Asset(id='figure', kind='figure', label='Extended Data Fig. 1', page=figure_page, bbox=[10, 10, 90, 75], path='assets/f.png'),
              Asset(id='other', kind='figure', label='Fig. 1', page=2, bbox=[10, 40, 90, 75], path='assets/o.png')]
    doc = Document(id='crosspage', metadata=Metadata(), blocks=blocks, assets=assets, pages=[])
    normalize_caption_notes(doc); associate_assets(doc)
    assert blocks[0].kind == 'figure_text' and blocks[0].source[0].block_id == 's1'
    assert blocks[1].asset_id == 'figure' and assets[0].caption == blocks[1].original
    assert not any(i.code == 'missing_asset' for i in doc.issues)


def test_repeated_extreme_margins_keep_coverage_and_plot_labels():
    raw, blocks = [], []
    for p in range(1, 4):
        for name, y in [('running header', 2), ('plot axis', 20)]:
            bid = f'p{p}-{y}'
            r = RawBlock(id=bid, page=p, bbox=[10, y, 90, y+2], text=name)
            raw.append(r)
            blocks.append(Block(id=bid, kind='metadata', original=name, source=[Source(page=p, bbox=r.bbox, block_id=bid)]))
    doc = Document(id='margins', metadata=Metadata(), blocks=blocks, assets=[],
        pages=[Page(number=p, width=100, height=100, preview=f'pages/{p}.png') for p in range(1,4)])
    remove_repeated_margins(doc, raw)
    assert len(doc.excluded) == 2
    assert sum(len(b.source) for b in doc.blocks) + len(doc.excluded) == len(raw)
    assert sum(b.original == 'plot axis' for b in doc.blocks) == 3


def test_integrity_and_zip_path_rejection(tmp_path):
    source = ROOT / 'web/dist/demo/demo.folio'
    with zipfile.ZipFile(source) as archive:
        files = {n: archive.read(n) for n in archive.namelist()}
    files['document.json'] += b' '
    corrupt = tmp_path / 'bad.folio'
    with zipfile.ZipFile(corrupt, 'w') as archive:
        for name, data in files.items(): archive.writestr(name, data)
    with pytest.raises(ValueError, match='校验'): verify_package(corrupt)
    with zipfile.ZipFile(corrupt, 'a') as archive: archive.writestr('../outside.txt', 'bad')
    with pytest.raises(ValueError, match='路径'): verify_package(corrupt)


def wait_job(client, jid):
    import time
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = client.get('/api/jobs/' + jid).json()
        if status['status'] in ('ready', 'failed', 'cancelled'): return status
        time.sleep(.05)
    raise AssertionError('Job did not finish')


def test_api_upload_automatic_processing_export_and_history(tmp_path):
    with TestClient(create_app(tmp_path / 'jobs')) as client:
        response = client.post('/api/jobs', files={'file': ('sample.pdf', PDF.read_bytes(), 'application/pdf')}, data={'config': json.dumps({'enabled': False, 'api_key': 'SECRET_NOT_TO_PERSIST'})})
        assert response.status_code == 202
        jid = response.json()['id']; assert wait_job(client, jid)['status'] == 'ready'
        status = client.get('/api/jobs/' + jid).json(); assert 'api_key' not in status['config']
        assert 'SECRET_NOT_TO_PERSIST' not in (tmp_path / 'jobs' / jid / 'status.json').read_text(encoding='utf-8')
        doc = client.get(f'/api/jobs/{jid}/document').json()
        assert client.get(f'/api/jobs/{jid}/files/status.json').status_code == 404
        assert client.get(f'/api/jobs/{jid}/package').status_code == 200
        order = [b['id'] for b in doc['blocks']]
        b = doc['blocks'][0]
        review = {'revision': status['revision'], 'order': order, 'edits': [{'id': b['id'], 'translated': '校对译文'}], 'reviewed': True}
        updated = client.patch(f'/api/jobs/{jid}/review', json=review)
        assert updated.status_code == 410
        assert client.post(f'/api/jobs/{jid}/crop',json={}).status_code==410
        assert client.post('/api/jobs', files={'file': ('evil.pdf', b'not a PDF')}, data={'config': '{"enabled":false}'}).status_code == 415
        assert client.post(f'/api/jobs/{jid}/retry', json={'enabled': False}).status_code == 409
        reprocessed=client.post(f'/api/jobs/{jid}/reprepare',json={'enabled':False})
        assert reprocessed.status_code==202
        history=tmp_path/'jobs'/jid/'history'/reprocessed.json()['previous_version']
        previous=verify_package(history)
        assert previous.id==jid and previous.reading_sections
        assert wait_job(client,jid)['status']=='ready'


def test_token_and_origin_guard(tmp_path):
    with TestClient(create_app(tmp_path / 'jobs', 'a' * 32)) as client:
        assert client.get('/api/health').status_code == 200
        assert client.get('/api/jobs').status_code == 401
        assert client.get('/api/jobs', headers={'Authorization': 'Bearer ' + 'a' * 32}).status_code == 200
        assert client.post('/api/jobs/x/cancel', headers={'Authorization': 'Bearer ' + 'a' * 32, 'Origin': 'https://unrelated.example'}).status_code == 403


def test_scanned_pdf_requires_explicit_vision(tmp_path):
    import pymupdf as fitz
    file = tmp_path / 'source.pdf'
    with fitz.open() as pdf:
        pdf.new_page(); pdf.save(file)
    with pytest.raises(ValueError, match='视觉'):
        prepare(file, tmp_path, 'scan', 'scan.pdf', LLMConfig(enabled=False), threading.Event(), lambda *a: None, lambda e: None)
