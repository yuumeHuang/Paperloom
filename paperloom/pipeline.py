from __future__ import annotations

import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pydantic import ValidationError
from .extract import crop_asset, extract, rect_valid, refresh_text_blocks
from .llm import LLMClient, LLMError, LLMResponseError, check_cancel
from .models import (Asset, Document, Issue, LLMConfig, LayoutPlan, Metadata, RawBlock, TranslationBatch)
from .package import pack
from .reading import stable_ids,verify_structure,verify_translations,chunks,assemble_reading,check_source_coverage
from .structure import (associate_assets, coverage_check, finalize, heuristic_plan, materialize,
                        normalize_caption_notes, remove_repeated_margins, promote_source_title, split_source_headings, normalize_reference_sections, merge_prose_continuations, graphic_evidence, translation_numbers_valid, translation_language_valid)


def batches(items, budget=11000, text=lambda v: v.text):
    current, length = [], 0
    for item in items:
        cost = len(text(item)) + 200
        if current and length + cost > budget:
            yield current
            current, length = [], 0
        current.append(item)
        length += cost
    if current:
        yield current


def layout_evidence(block):
    # Compact coordinates bound prompt size without discarding the source evidence.
    return {'id': block.id, 'page': block.page, 'bbox': [round(v, 2) for v in block.bbox],
            'text': block.text, 'font_size': round(block.font_size, 2), 'bold': block.bold, 'ocr': block.ocr}


def layout_batches_by_page(raw, budget=60000):
    """Keep ordinary pages whole so columns, panels and captions share context."""
    by_page = {}
    for block in raw:
        by_page.setdefault(block.page, []).append(block)
    for page_blocks in by_page.values():
        current, length = [], 0
        for block in page_blocks:
            cost = len(json.dumps(layout_evidence(block), ensure_ascii=False))
            if current and length + cost > budget:
                yield current
                current, length = [], 0
            current.append(block)
            length += cost
        if current:
            yield current


def recover_layout_responses(directory, raw, model):
    """Recover immutable source decisions; every candidate is validated again below."""
    aliases, parts_by_parent = {}, {}
    for b in raw:
        if b.parent_id:
            aliases.setdefault(b.parent_id, []).append(b.id)
            parts_by_parent.setdefault(b.parent_id, []).append(b)
    index = {}
    audit_file = directory / 'audit.jsonl'
    if not audit_file.exists():
        return index
    for line in audit_file.read_text(encoding='utf-8').splitlines():
        try:
            entry = json.loads(line)
            key = entry.get('input_hash', '')
            if entry.get('stage') != 'layout' or entry.get('model') != model or not re.fullmatch(r'[a-f0-9]{64}', key):
                continue
            result = json.loads((directory / 'cache' / f'{key}.json').read_text(encoding='utf-8'))
            candidate = LayoutPlan.model_validate(result)
            roles = {}
            for node in candidate.nodes:
                for s in node.source_ids:
                    roles.setdefault(s,set()).add(node.kind)
            for node in candidate.nodes:
                expanded=[]
                for s in node.source_ids:
                    children=aliases.get(s,[s]); parts=parts_by_parent.get(s,[])
                    if parts and len(parts)>1 and parts[0].styles and all(t.bold for t in parts[0].styles if t.text.strip()) and not all(t.bold for t in parts[1].styles if t.text.strip()) and 'heading' in roles[s] and roles[s].intersection({'paragraph','reference'}):
                        children = children[:1] if node.kind=='heading' else children[1:] if node.kind in ('paragraph','reference') else children
                    expanded.extend(children)
                node.source_ids=expanded
            for region in candidate.regions:
                region.caption_source_ids = [child for s in region.caption_source_ids for child in aliases.get(s, [s])]
            expanded_ignored = []
            used = [s for n in candidate.nodes for s in n.source_ids]
            for ignored in candidate.ignored:
                for s in aliases.get(ignored.source_id, [ignored.source_id]):
                    # Preserve source text if a model both retained and excluded the same ID.
                    if s not in used:
                        expanded_ignored.append(ignored.model_copy(update={'source_id': s}))
            candidate.ignored = expanded_ignored
            ids = frozenset(used + [v.source_id for v in candidate.ignored])
            index.setdefault(ids, []).append((candidate, key, entry.get('prompt_hash')))
        except (OSError, ValueError, KeyError, ValidationError):
            continue
    return index


def recovered_page_batches(raw, pages, assets, recovered):
    """Keep a validated old partition when a segmentation upgrade moves the budget boundary."""
    by_page = {}
    for b in raw:
        by_page.setdefault(b.page,[]).append(b)
    result=[]
    for number, page_raw in by_page.items():
        remaining={b.id for b in page_raw}; parts=[]
        page=[p for p in pages if p.number==number]; candidates=[a for a in assets if a.page==number]
        for ids, plans in sorted(recovered.items(),key=lambda item:len(item[0]),reverse=True):
            if not ids or not ids.issubset(remaining):
                continue
            batch=[b for b in page_raw if b.id in ids]
            try:
                valid=next((p for p,h,q in reversed(plans) if _valid_coverage(p,batch,page,candidates)),None)
            except ValueError:
                valid=None
            if valid:
                parts.append(batch);remaining-=ids
        if remaining:
            result.extend(layout_batches_by_page(page_raw))
        else:
            order={b.id:i for i,b in enumerate(page_raw)}
            result.extend(sorted(parts,key=lambda part:min(order[b.id] for b in part)))
    return result


def _valid_coverage(plan,raw,pages,assets):
    try:
        coverage_check(plan,raw,pages,assets);return True
    except ValueError:
        return False


def translate_batch(client, batch, config, title):
    accepted={};review={};pending={b.id:b for b in batch};feedback=''
    for target in client.targets('translate'):
        for attempt in range(config.retry_attempts):
            if not pending:break
            check_cancel(client.cancel)
            payload={'target_language':config.target_language,'glossary':config.glossary,'paper_title_context':title,
                     'items':[{'id':b.id,'kind':b.kind,'original':b.original,'superscript_annotations':[s.text for s in b.styles if s.superscript]} for b in pending.values()]}
            try:result=client.complete('translate',payload,repair=feedback,target=target)
            except LLMError:
                for b in pending.values():b.translation_error='服务请求在重试后仍失败，已尝试下一层备用模型'
                break
            items=result.get('translations',[])
            if not isinstance(items,list):items=[]
            ids=[x.get('id') for x in items if isinstance(x,dict) and isinstance(x.get('id'),str)]
            count={id:ids.count(id) for id in ids};values={x['id']:x.get('translated') for x in items if isinstance(x,dict) and isinstance(x.get('id'),str) and x.get('id') in pending and count.get(x.get('id'))==1}
            rejected={}
            for id,b in list(pending.items()):
                value=values.get(id)
                if not isinstance(value,str) or not value.strip():reason='missing_or_empty'
                elif not translation_language_valid(b,value,config.target_language):reason='untranslated'
                elif not translation_numbers_valid(b.original,value,b.styles):review[id]=(value,target.model);reason='numeric_mismatch'
                else:
                    accepted[id]=value;del pending[id];b.translation_error='';b.translated_by=target.model;continue
                rejected[id]=reason;b.translation_error={'missing_or_empty':'模型返回了缺失、空或重复的段落译文','untranslated':'模型保留了未翻译的正文','numeric_mismatch':'译文数值或符号仍与原文不同'}[reason]
            client.audit({'stage':'translation_validation','model':target.model,'accepted_ids':list(accepted),'rejected':rejected,'attempt':attempt+1,'tier':target.tier})
            feedback='Translate every remaining exact ID independently; preserve all numbers and signs. Failed IDs and reasons: '+json.dumps(rejected)
        if not pending:break
    for id,(value,model) in review.items():
        if id not in accepted:accepted[id]=value;next(b for b in batch if b.id==id).translated_by=model
    for b in batch:
        b.translated=accepted.get(b.id,'');b.translation_status='unavailable' if not b.translated else 'translated' if translation_numbers_valid(b.original,b.translated,b.styles) else 'review'
    try:verify_translations(client,batch,config,title)
    except LLMError:
        for b in batch:
            if b.translation_status!='unavailable':b.translation_status='review';b.translation_error='独立校验服务在重试及备用模型后仍不可用'
        client.audit({'stage':'translation_verification_unavailable','block_ids':[b.id for b in batch]})
    return {b.id:b.translated for b in batch if b.translated}


def translation_settings(config):
    return {'target_language':config.target_language,'glossary':config.glossary}


def prepare(pdf: Path, directory: Path, job_id: str, filename: str, config: LLMConfig,
            cancel: threading.Event, update, audit) -> Document:
    config.ensure_preparation_ready()
    update("extract", 5, "解析 PDF 文字、版面坐标、图片和表格")
    cached_extraction = directory / 'extraction.json'
    extraction_data = None
    if cached_extraction.exists():
        try:
            extraction_data = json.loads(cached_extraction.read_text(encoding='utf-8'))
            if extraction_data.get('scanned'):
                extraction_data = None
        except (OSError,ValueError):
            pass
    if extraction_data:
        from .models import Page
        pages = [Page.model_validate(p) for p in extraction_data['pages']]
        assets = [Asset.model_validate(a) for a in extraction_data['assets']]
        raw = refresh_text_blocks(pdf, [RawBlock.model_validate(b) for b in extraction_data['blocks']])
        scanned, pdfmeta = [], extraction_data['pdf_metadata']
        extraction_data['blocks'] = [b.model_dump() for b in raw]
        cached_extraction.write_text(json.dumps(extraction_data,ensure_ascii=False,indent=2),encoding='utf-8')
        update('extract',25,'复用来源页与图表，更新文字分段')
    else:
        raw, pages, assets, scanned, pdfmeta = extract(pdf, directory, cancel,
            lambda n, total: update("extract", 5 + int(n / total * 20), f"解析第 {n}/{total} 页"))
    client = LLMClient(config, directory / "cache", cancel, audit)
    previous=None
    if (directory/'document.json').exists():
        try:previous=Document.model_validate_json((directory/'document.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
    issues = []
    if scanned:
        if not config.enabled or not config.vision:
            raise ValueError(f"第 {', '.join(map(str, scanned))} 页缺少文本层。请启用支持图像的布局模型和视觉 OCR 后重试")
        for n in scanned:
            update("ocr", 26, f"视觉转录第 {n} 页；随后由独立 agent 校验")
            page = pages[n - 1]
            response = client.complete("ocr", {"page": n, "width": page.width, "height": page.height}, [directory / page.preview])
            transcribed = []
            for i, item in enumerate(response.get("blocks", [])):
                if not item.get("text") or not rect_valid(item.get("bbox", []), page):
                    raise ValueError("视觉 OCR 返回了无效文字或坐标")
                transcribed.append(RawBlock(id=f"p{n:04d}-o{i + 1:04d}", page=n, bbox=item["bbox"],
                    text=item["text"], font_size=item.get("font_size", 10), bold=item.get("bold", False), ocr=True))
            if not transcribed:
                raise ValueError("视觉 OCR 未识别出文字")
            raw = [b for b in raw if b.page != n] + transcribed
        raw.sort(key=lambda b: b.page)
        issues.append(Issue(code="ocr_review", message="部分页使用视觉 OCR，文字、公式与表格保留来源页用于追溯"))
        extracted = json.loads((directory / "extraction.json").read_text(encoding="utf-8"))
        extracted["blocks"] = [b.model_dump() for b in raw]
        (directory / "extraction.json").write_text(json.dumps(extracted, ensure_ascii=False, indent=2), encoding="utf-8")
    if not raw:
        raise ValueError("PDF 没有可解析的文字")
    update("layout", 30, "重建章节、段落与自然阅读顺序")
    if config.enabled:
        nodes, ignored = [], []
        recovered = recover_layout_responses(directory, raw, config.layout_model)
        layout_batches = recovered_page_batches(raw,pages,assets,recovered)
        previous_context = [];outline_context=[]
        for i, batch in enumerate(layout_batches):
            check_cancel(cancel)
            update('layout',30+int(i/len(layout_batches)*20),f'布局分析 {i+1}/{len(layout_batches)} 批')
            page_numbers = {b.page for b in batch}
            page_subset = [p for p in pages if p.number in page_numbers]
            asset_subset = [a for a in assets if a.page in page_numbers]
            payload = {"pages": [p.model_dump(exclude={"preview"}) for p in page_subset],
                       "blocks": [layout_evidence(b) for b in batch],
                       "assets": [a.model_dump(exclude={"path", "table", "caption_translated"}) for a in asset_subset],
                       "previous_context_readonly": previous_context,"outline_context_readonly":outline_context[-60:]}
            images = [directory / p.preview for p in page_subset] if config.vision else None
            error = ""
            plan = None
            for candidate, cached_hash, prompt_hash in reversed(recovered.get(frozenset(b.id for b in batch), [])):
                try:
                    coverage_check(candidate, batch, page_subset, asset_subset)
                    plan = candidate.model_copy(deep=True)
                    audit({'stage': 'layout_reuse', 'model': config.layout_model, 'input_hash': cached_hash,
                           'prompt_hash': prompt_hash, 'source_ids': len(batch), 'revalidated': True})
                    break
                except ValueError:
                    continue
            for attempt in range(3 if plan is None else 0):
                try:
                    result = client.complete("layout", payload, images, error,
                        validator=lambda value:coverage_check(LayoutPlan.model_validate(value),batch,page_subset,asset_subset))
                    candidate = LayoutPlan.model_validate(result)
                    coverage_check(candidate, batch, page_subset, asset_subset)
                    plan = candidate
                    break
                except (ValueError, ValidationError, LLMResponseError) as exc:
                    error = str(exc)
                except LLMError:
                    error='所有配置的模型均未通过当前页的来源覆盖校验'
                    break
            if plan is None:
                plan = heuristic_plan(batch, page_subset, asset_subset)
                issues.append(Issue(code="layout_fallback", message=f"第 {i + 1} 个布局批次未通过来源校验，保留全部文字，交由结构 agent 与独立 agent 校验"))
            for region in plan.regions:
                if not graphic_evidence(region,plan,batch,asset_subset):
                    issues.append(Issue(code='caption_crop_rejected',severity='info',message=f'第 {region.page} 页的 {region.label or "图"} 候选区域缺少图形证据，已保留文字并忽略该裁剪'))
                    continue
                aid = f"a{len(assets) + 1:04d}"
                asset = Asset(id=aid, kind=region.kind, label=region.label, page=region.page,
                              bbox=region.bbox, path=f"assets/{aid}.png", confidence=.8)
                # Avoid two assets with the same caption label; keep the refined crop.
                old = next((a for a in assets if a.label == region.label and region.label), None)
                if old:
                    if old.page!=region.page:
                        continue
                    asset.id, asset.path = old.id, old.path
                    asset.bbox=[min(old.bbox[0],asset.bbox[0]),min(old.bbox[1],asset.bbox[1]),
                                max(old.bbox[2],asset.bbox[2]),max(old.bbox[3],asset.bbox[3])]
                    assets.remove(old)
                crop_asset(pdf, directory, asset)
                assets.append(asset)
                for node in plan.nodes:
                    if set(node.source_ids) & set(region.caption_source_ids):
                        node.asset_id = asset.id
            nodes.extend(plan.nodes)
            ignored.extend(plan.ignored)
            outline_context.extend({'level':n.heading_level,'text':' '.join(next(b.text for b in batch if b.id==s) for s in n.source_ids)} for n in plan.nodes if n.kind=='heading')
            previous_context = [{"kind": n.kind, "text": " ".join(next(b.text for b in batch if b.id == s) for s in n.source_ids)} for n in plan.nodes[-3:]]
            update("layout", 30 + int((i + 1) / len(layout_batches) * 20), f"布局分析 {i + 1}/{len(layout_batches)} 批")
        plan = LayoutPlan(nodes=nodes, ignored=ignored)
    else:
        plan = heuristic_plan(raw, pages, assets)
        issues.append(Issue(code="no_llm", message="此任务使用几何解析，尚未进行 LLM 结构校正与翻译"))
    promote_source_title(plan, raw, pdfmeta.get('title', ''))
    split_source_headings(plan, raw)
    coverage_check(plan, raw, pages, assets)
    blocks = materialize(plan, raw)
    normalize_reference_sections(blocks)
    blocks = merge_prose_continuations(blocks)
    metadata = Metadata(filename=filename, target_language=config.target_language)
    text_all = " ".join(b.original for b in blocks)
    doi = re.search(r"10\.\d{4,9}/[^\s<>]+", text_all, re.I)
    if doi:
        metadata.doi = doi.group().rstrip(".,;)")
    metadata.title = next((b.original for b in blocks if b.kind == "title"), pdfmeta.get("title") or filename)
    metadata.authors = [b.original for b in blocks if b.kind == "author"]
    document = Document(id=job_id, metadata=metadata, blocks=blocks, assets=assets, pages=pages, issues=issues,
        excluded={x.source_id: x.reason for x in plan.ignored},
        provenance={"pipeline": "paperloom-0.3", "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                    "layout_model": config.layout_model if config.enabled else "geometry",
                    "translation_model": config.translation_model if config.enabled else None,
                    "source_blocks": len(raw), "accounted_blocks": sum(len(b.source) for b in blocks) + len(plan.ignored),
                    "translation_settings":translation_settings(config),
                    "ocr_pages": scanned, "prompt_hashes": {p.stem: hashlib.sha256(p.read_bytes()).hexdigest() for p in (Path(__file__).parent / "prompts").glob("*.md")}})
    stable_ids(document,previous)
    verify_structure(document,raw,client,config,update)
    update("translate", 52, "按段落翻译并核对数字与引用")
    normalize_caption_notes(document)
    remove_repeated_margins(document, raw)
    blocks = document.blocks
    for b in blocks:
        if b.kind in ("equation", "table", "figure_text", "reference", "author", "metadata"):
            b.translation_status = "not_required"
    previous_file=directory/'document.json'
    if config.enabled and previous_file.exists():
        try:
            previous=Document.model_validate_json(previous_file.read_text(encoding='utf-8'))
            old_settings=previous.provenance.get('translation_settings',{})
            if previous.provenance.get('pdf_sha256')==document.provenance['pdf_sha256'] and {k:old_settings.get(k,{} if k=='glossary' else '简体中文') for k in ('target_language','glossary')}==translation_settings(config):
                saved={(b.kind,b.original):(b.translated,b.translated_by or previous.provenance.get('translation_model') or config.translation_model) for b in previous.blocks if b.translated and b.translation_status in ('translated','review')}
                reused=0
                for b in blocks:
                    cached,producer=saved.get((b.kind,b.original),('', ''))
                    if b.translation_status=='pending' and cached and translation_language_valid(b,cached,config.target_language):
                        b.translated=cached;b.translated_by=producer;b.translation_status='translated' if translation_numbers_valid(b.original,cached,b.styles) else 'review';reused+=1
                audit({'stage':'translation_reuse','model':config.translation_model,'blocks':reused,'source_pdf_hash':document.provenance['pdf_sha256']})
        except (OSError,ValueError,ValidationError):
            pass
    targets = [b for b in blocks if b.translation_status == "pending"]
    reused_targets=[b for b in blocks if b.translation_status in ('translated','review')]
    if config.enabled and reused_targets:
        for i,group in enumerate(chunks(reused_targets,26000,lambda b:b.original+b.translated)):
            update('translation_verify',52,f'独立 agent 校验复用译文 {i+1} 批')
            try:verify_translations(client,group,config,metadata.title)
            except LLMError:
                for b in group:b.translation_status='review';b.translation_error='独立校验服务在重试及备用模型后仍不可用'
    # A checker may reject a previously saved translation. Send it through the
    # same bounded repair/fallback chain as a newly extracted paragraph.
    targets=[b for b in blocks if b.translation_status in ('pending','unavailable')]
    translation_batches = list(batches(targets, 6000, lambda b: b.original))
    if config.enabled:
        with ThreadPoolExecutor(max_workers=config.translation_workers,thread_name_prefix='paperloom-translate') as pool:
            pending={pool.submit(translate_batch,client,batch,config,metadata.title):batch for batch in translation_batches}
            try:
                for completed, future in enumerate(as_completed(pending),1):
                    check_cancel(cancel)
                    batch,translated=pending[future],future.result()
                    for b in batch:
                        if translated and translated.get(b.id) and b.translation_status!='unavailable':
                            b.translated=translated[b.id]
                        else:
                            b.translation_status='unavailable'
                    update('translate',52+int(completed/max(1,len(translation_batches))*32),f'翻译完成 {completed}/{len(translation_batches)} 批')
            except BaseException:
                cancel.set()
                for future in pending:
                    future.cancel()
                raise
    else:
        for b in targets:
            b.translation_status = "unavailable"
    for b in blocks:
        if b.translation_status in ('review','unavailable'):
            document.issues.append(Issue(code='translation_invalid' if b.translation_status=='unavailable' else 'translation_check',block_id=b.id,severity='error',message=b.translation_error or '该段译文在自动重试与校验后仍未通过，已保留原文'))
    checked=[b for b in blocks if b.translation_status not in ('not_required',)]
    document.provenance['translation_verification']={'blocks':len(checked),'status':'complete' if config.enabled and all(b.translation_status=='translated' for b in checked) else 'partial','independent_model':bool(config.enabled),'unavailable':sum(b.translation_status=='unavailable' for b in checked),'differences':sum(b.translation_status=='review' for b in checked)}
    update("verify", 88, "检查来源覆盖、章节目录与图表链接")
    associate_assets(document)
    # Formula crops preserve layout and notation even if extracted Unicode is imperfect.
    for block in document.blocks:
        if block.kind == "equation" and not block.asset_id:
            source = block.source[0]
            aid = f"a{len(document.assets) + 1:04d}"
            asset = Asset(id=aid, kind="equation", label=block.label, path=f"assets/{aid}.png", page=source.page, bbox=source.bbox)
            crop_asset(pdf, directory, asset)
            document.assets.append(asset)
            block.asset_id = aid
    from .structure import project_translation_styles
    for b in document.blocks:project_translation_styles(b)
    finalize(document)
    assemble_reading(document)
    check_source_coverage(document,raw)
    check_cancel(cancel)
    update("package", 96, "生成带完整性校验的 .folio 文献包")
    pack(document, directory)
    return document
