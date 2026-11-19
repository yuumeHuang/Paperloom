from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from collections import Counter, defaultdict

from .extract import join_lines, rect_valid
from .models import (Asset, Block, Document, Issue, LayoutNode, LayoutPlan, Page, RawBlock,
                     Reference, Source, TocEntry)

LABEL = re.compile(r"^(?:(?:Supplementary|Extended Data)\s+)?(?:Fig(?:ure)?s?\.?|Tables?|图|表)\s*[S\d一二三四五六七八九十][\w.()]*", re.I)
MENTION = re.compile(r"(?<![A-Za-z])(?:(?:Supplementary|Extended Data)\s+)?(?:Fig(?:ure)?s?\.?|Tables?|图|表)\s*(?:[S]?\d+(?:[a-z]|\([a-z]\))?|(?-i:[IVX]+))(?![A-Za-z0-9_])", re.I)
HEADING_NAMES = re.compile(r"^(abstract|introduction|background|materials?\s+and\s+methods|methods|results|discussion|conclusions?|references|acknowledg\w*|funding|data\s+availability|supporting\s+information|supplement\w*|conflict\w*|摘要|引言|方法|结果|讨论|结论|参考文献)\b", re.I)


def label_key(label: str) -> str:
    value = label.strip().lower()
    kind = "table" if re.search(r"table|表", value) else "figure"
    number = re.search(r"\b(s?\d+|[ivx]+)(?:[a-z]|\([a-z]\))?\b", value)
    if not number:
        number = re.search(r"[图表]\s*(s?\d+)", value)
    prefix = "supp:" if "supplement" in value else "extended:" if "extended data" in value else ""
    return f"{prefix}{kind}:{number.group(1) if number else value}"


def coverage_check(plan: LayoutPlan, raw: list[RawBlock], pages: list[Page], assets: list[Asset]):
    known = {b.id: b for b in raw}
    page_map = {p.number: p for p in pages}
    occurrences = Counter(s for n in plan.nodes for s in n.source_ids)
    occurrences.update(i.source_id for i in plan.ignored)
    missing = set(known) - set(occurrences)
    extra = set(occurrences) - set(known)
    duplicates = [k for k, v in occurrences.items() if v != 1]
    if missing or extra or duplicates:
        raise ValueError(f"source coverage: missing={sorted(missing)}, unknown={sorted(extra)}, duplicate={duplicates}")
    asset_ids = {a.id for a in assets}
    for node in plan.nodes:
        if node.asset_id and node.asset_id not in asset_ids:
            raise ValueError(f"unknown asset_id: {node.asset_id}")
    for item in plan.ignored:
        block = known[item.source_id]
        page = page_map[block.page]
        if not (block.bbox[1] < page.height * .13 or block.bbox[3] > page.height * .9):
            raise ValueError(f"cannot ignore non-marginal text: {block.id}")
    caption_ids = {s for n in plan.nodes if n.kind == "caption" for s in n.source_ids}
    for region in plan.regions:
        if region.page not in page_map or not rect_valid(region.bbox, page_map[region.page]):
            raise ValueError("region bbox must be finite in-bounds PDF coordinates")
        if any(s not in caption_ids for s in region.caption_source_ids):
            raise ValueError("region caption_source_ids must refer to caption nodes")
        if any(known[s].page != region.page for s in region.caption_source_ids):
            raise ValueError("region and caption must share the same page")


def ordered_page(blocks: list[RawBlock], page: Page) -> list[RawBlock]:
    mid = page.width / 2
    left = [b for b in blocks if b.bbox[2] <= mid + 18]
    right = [b for b in blocks if b.bbox[0] >= mid - 18]
    if len(left) < 2 or len(right) < 2:
        return sorted(blocks, key=lambda b: (b.bbox[1], b.bbox[0]))
    wide = sorted([b for b in blocks if b not in left and b not in right], key=lambda b: b.bbox[1])
    remaining = [b for b in blocks if b not in wide]
    result = []
    for barrier in wide:
        band = [b for b in remaining if b.bbox[1] < barrier.bbox[1]]
        result.extend(sorted(band, key=lambda b: (int(b.bbox[0] >= mid - 18), b.bbox[1], b.bbox[0])))
        result.append(barrier)
        taken = {b.id for b in band}
        remaining = [b for b in remaining if b.id not in taken]
    result.extend(sorted(remaining, key=lambda b: (int(b.bbox[0] >= mid - 18), b.bbox[1], b.bbox[0])))
    return result


def overlap(bbox: list, other: list) -> float:
    intersection = max(0, min(bbox[2], other[2]) - max(bbox[0], other[0])) * max(0, min(bbox[3], other[3]) - max(bbox[1], other[1]))
    return intersection / max(1, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))


def heuristic_plan(raw: list[RawBlock], pages: list[Page], assets: list[Asset]) -> LayoutPlan:
    nodes = []
    ignored = []
    repeat = Counter(b.text for b in raw)
    sizes = sorted(b.font_size for b in raw if len(b.text) > 60)
    body_size = sizes[len(sizes) // 2] if sizes else 10
    title_seen = False
    reference_section = False
    for page in pages:
        for b in ordered_page([b for b in raw if b.page == page.number], page):
            margin = b.bbox[1] < page.height * .08 or b.bbox[3] > page.height * .94
            if margin and ((repeat[b.text] >= 2 and len(b.text) < 140) or re.fullmatch(r"\d{1,4}", b.text)):
                ignored.append({"source_id": b.id, "reason": "page_number" if b.text.isdigit() else "header" if b.bbox[1] < 80 else "footer"})
                continue
            kind, label, aid = "paragraph", "", None
            caption = LABEL.match(b.text)
            table_asset = next((a for a in assets if a.kind == "table" and a.page == b.page and overlap(b.bbox, a.bbox) > .7), None)
            figure_asset = next((a for a in assets if a.kind == "figure" and a.page == b.page and overlap(b.bbox, a.bbox) > .85), None)
            if table_asset:
                kind, aid = "table", table_asset.id
            elif figure_asset:
                kind, aid = "figure_text", figure_asset.id
            elif caption:
                kind, label = "caption", caption.group(0).rstrip(". ")
            elif b.page == 1 and not title_seen and b.font_size >= body_size * 1.3 and b.bbox[1] < page.height * .4:
                kind, title_seen = "title", True
            elif len(b.text) < 180 and (HEADING_NAMES.match(b.text) or
                    (re.match(r"^\d+(?:\.\d+)*\.?\s+[A-Z]", b.text) and b.bold) or
                    (b.bold and b.font_size > body_size * 1.12)):
                kind = "heading"
                reference_section = bool(re.match(r"(?:\d+(?:\.\d+)*\.?\s+)?(?:references|参考文献)\b", b.text, re.I))
            elif reference_section:
                kind = "reference"
            elif re.match(r"^(?:doi:|https?://doi\.org/|keywords?:)", b.text, re.I):
                kind = "metadata"
            elif len(b.text) < 200 and re.search(r"[=∑∫√]", b.text) and not re.search(r"\b(?:is|was|were|the|we|and)\b", b.text, re.I):
                kind = "equation"
            level = 1
            heading_num = re.match(r"^(\d+(?:\.\d+)*)", b.text)
            if heading_num and kind == "heading":
                level = min(6, heading_num.group(1).count(".") + 1)
            # Only obvious continuation fragments are joined. Full paragraphs remain stable.
            if kind == "paragraph" and nodes and nodes[-1].kind == "paragraph":
                prev = next(x for x in raw if x.id == nodes[-1].source_ids[-1])
                same_column = abs(prev.bbox[0] - b.bbox[0]) < 25
                close = b.page == prev.page and same_column and 0 <= b.bbox[1] - prev.bbox[3] < body_size * 2
                continuation = b.page == prev.page + 1
                if (close or continuation) and b.text[:1].islower() and not re.search(r"[.!?:;。]$", prev.text):
                    nodes[-1].source_ids.append(b.id)
                    continue
            nodes.append(LayoutNode(kind=kind, source_ids=[b.id], label=label, asset_id=aid, heading_level=level, confidence=.62))
    return LayoutPlan(nodes=nodes, ignored=ignored)


def materialize(plan: LayoutPlan, raw: list[RawBlock]) -> list[Block]:
    lookup = {b.id: b for b in raw}
    blocks = []
    for i, node in enumerate(plan.nodes):
        source = [lookup[s] for s in node.source_ids]
        blocks.append(Block(id=f"b{i + 1:05d}", kind=node.kind, original=join_lines([b.text for b in source]),
                            source=[Source(page=b.page, bbox=b.bbox, block_id=b.id) for b in source],
                            styles=[s for b in source for s in b.styles], heading_level=node.heading_level,
                            confidence=node.confidence, label=node.label, asset_id=node.asset_id))
    return blocks


def promote_source_title(plan: LayoutPlan, raw: list[RawBlock], pdf_title: str):
    lookup={b.id:b for b in raw}
    for node in plan.nodes:
        if node.kind=='title' and all(lookup[s].page>1 for s in node.source_ids):
            node.kind='heading';node.heading_level=1
    if not pdf_title:
        return
    normalize = lambda text: re.sub(r'\W+', '', text.casefold())
    candidate = next((b for b in raw if b.page == 1
                      and normalize(b.text) == normalize(pdf_title)), None)
    if not candidate:
        return
    for index, node in enumerate(plan.nodes):
        if candidate.id not in node.source_ids:
            continue
        if node.kind=='title' and node.source_ids==[candidate.id]:
            break
        node.source_ids.remove(candidate.id)
        title = LayoutNode(kind='title', source_ids=[candidate.id], confidence=.85)
        if node.source_ids:
            if node.kind=='title':node.kind='metadata'
            plan.nodes.insert(index + 1, title)
        else:
            plan.nodes[index] = title
        break


def graphic_evidence(region,plan,raw,assets):
    if region.kind!='figure':
        return True
    known={b.id:b for b in raw}
    labels=[known[s] for n in plan.nodes if n.kind=='figure_text' for s in n.source_ids
            if known[s].page==region.page and overlap(known[s].bbox,region.bbox)>.5]
    return len(labels)>=3 or any(a.kind=='figure' and a.page==region.page and overlap(a.bbox,region.bbox)>.5 for a in assets)


def split_source_headings(plan: LayoutPlan, raw: list[RawBlock]):
    """Use isolated leading bold lines only within LLM-classified prose/references."""
    by_parent = defaultdict(list)
    for b in raw:
        if b.parent_id:
            by_parent[b.parent_id].append(b)
    headings = set()
    for parts in by_parent.values():
        if len(parts) < 2:
            continue
        # A native block can contain a main heading, a smaller bold subsection,
        # and its first paragraph. All leading headings need their own IDs.
        for part in parts:
            if (not part.styles or not all(s.bold for s in part.styles if s.text.strip())
                    or len(part.text) > 240 or LABEL.match(part.text)):
                break
            headings.add(part.id)
    result = []
    for node in plan.nodes:
        if node.kind not in ('paragraph','heading','reference') or not headings.intersection(node.source_ids):
            result.append(node); continue
        group=[]
        for s in node.source_ids:
            if s in headings:
                if group:
                    result.append(node.model_copy(update={'source_ids':group,'kind':'paragraph' if node.kind=='heading' else node.kind}))
                    group=[]
                result.append(node.model_copy(update={'source_ids':[s],'kind':'heading'}))
            else:
                group.append(s)
        if group:
            result.append(node.model_copy(update={'source_ids':group,'kind':'paragraph' if node.kind=='heading' else node.kind}))
    plan.nodes=result


def normalize_reference_sections(blocks):
    """Correct numbered bibliography prose only inside an explicit References section."""
    active=False
    for block in blocks:
        if block.kind=='heading':
            active=bool(re.fullmatch(r'(?:\d+(?:\.\d+)*\.?\s+)?(?:references|参考文献)',block.original.strip(),re.I))
        elif (active and block.kind=='paragraph' and re.match(r'^\d+\.\s+',block.original)
              and re.search(r'\((?:19|20)\d{2}[a-z]?\)',block.original)):
            block.kind='reference'


def merge_prose_continuations(blocks):
    result=[]
    for block in blocks:
        index=len(result)-1
        while index>=0 and result[index].kind in ('caption','figure_text','metadata'):
            index-=1
        previous=result[index] if index>=0 else None
        if (block.kind=='paragraph' and previous and previous.kind=='paragraph'
                and (block.original[:1].islower() or block.original.startswith(('(','（')))
                and not re.search(r'[.!?:;。](?:\s*[\d,–-]+)?$',previous.original)
                and 0<=block.source[0].page-previous.source[-1].page<=4
                and block.section==previous.section):
            previous.original=join_lines([previous.original,block.original])
            previous.source.extend(block.source);previous.styles.extend(block.styles)
            previous.confidence=min(previous.confidence,block.confidence,.7)
            previous.translated='';previous.translation_status='pending';previous.translated_by=''
        else:
            result.append(block)
    return result


def associate_assets(document: Document):
    used = set()
    for b in document.blocks:
        if b.kind != "caption" or not b.source:
            continue
        if not b.label:
            match = LABEL.match(b.original)
            b.label = match.group(0).rstrip(". ") if match else ""
        kind = "table" if re.search(r"table|表", b.label, re.I) else "figure"
        existing = next((a for a in document.assets if a.id == b.asset_id), None)
        if not existing and b.label:
            # Nature-style extended figures can put the complete legend on the next page.
            src = b.source[0]
            labelled = [a for a in document.assets if a.label and label_key(a.label) == label_key(b.label)
                        and abs(src.page - a.page) <= 2]
            if len(labelled) == 1:
                existing = labelled[0]
                b.asset_id = existing.id
        if not existing:
            src = b.source[0]
            candidates = [a for a in document.assets if a.page == src.page and a.kind == kind and a.id not in used
                          and (not a.label or not b.label or label_key(a.label) == label_key(b.label))]
            def score(a):
                gap = min(abs(a.bbox[3] - src.bbox[1]), abs(src.bbox[3] - a.bbox[1]))
                horizontal = abs((a.bbox[0] + a.bbox[2] - src.bbox[0] - src.bbox[2]) / 2)
                return gap + horizontal * .3
            candidates.sort(key=score)
            if candidates and score(candidates[0]) < 210:
                existing = candidates[0]
                b.asset_id = existing.id
        if existing:
            existing.label = b.label or existing.label
            existing.caption = b.original
            existing.caption_translated = b.translated
            existing.confidence = min(b.confidence, existing.confidence)
            used.add(existing.id)
        else:
            document.issues.append(Issue(code="missing_asset", block_id=b.id, message=f"{b.label or '图表'} 暂未匹配到可靠图像区域，可查看来源页"))


def normalize_caption_notes(document: Document):
    """A publisher's page locator is source evidence, not a scientific legend."""
    for b in document.blocks:
        if LABEL.match(b.original) and re.search(r'\bsee\s+(?:the\s+)?(?:next|following)\s+page\s+for\s+(?:the\s+)?caption\b', b.original, re.I):
            b.kind = 'figure_text'
            b.translation_status = 'not_required'


def remove_repeated_margins(document: Document, raw: list[RawBlock]):
    """Retain a first occurrence; account for repeated edge text as running furniture."""
    pages = {p.number: p for p in document.pages}
    groups = defaultdict(list)
    lookup = {b.id: b for b in raw}
    for block in raw:
        page = pages.get(block.page)
        if page and len(block.text) < 140 and (block.bbox[3] < page.height * .06 or block.bbox[1] > page.height * .96):
            groups[' '.join(block.text.casefold().split())].append(block)
    removed = {}
    for instances in groups.values():
        if len({b.page for b in instances}) >= 3:
            for b in instances[1:]:
                removed[b.id] = 'header' if b.bbox[1] < pages[b.page].height / 2 else 'footer'
    kept = []
    for block in document.blocks:
        remaining = [s for s in block.source if s.block_id not in removed]
        if not remaining:
            continue
        if len(remaining) != len(block.source):
            evidence = [lookup[s.block_id] for s in remaining]
            block.source = remaining
            block.original = join_lines([b.text for b in evidence])
            block.styles = [s for b in evidence for s in b.styles]
        kept.append(block)
    document.blocks = kept
    document.excluded.update(removed)


def finalize(document: Document):
    document.toc = [TocEntry(block_id=b.id, title=b.original, translated=b.translated, level=b.heading_level)
                    for b in document.blocks if b.kind == "heading"]
    title = next((b for b in document.blocks if b.kind == "title"), None)
    if title:
        document.metadata.title = title.original
        document.metadata.title_translated = title.translated
    asset_index = {}
    for a in document.assets:
        if a.label:
            key = label_key(a.label)
            if key in asset_index and asset_index[key] != a.id:
                document.issues.append(Issue(code="duplicate_label", message=f"图表标签 {a.label} 对应多个区域，自动绑定存在歧义"))
            else:
                asset_index[key] = a.id
    for b in document.blocks:
        b.references = {"original": [], "translated": []}
        for language, text in (("original", b.original), ("translated", b.translated)):
            for match in MENTION.finditer(text):
                key = label_key(match.group())
                aid = asset_index.get(key)
                # Some supplementary captions use S1 without spelling 'Supplementary'.
                if not aid and ":s" in key:
                    aid = asset_index.get("supp:" + key) or asset_index.get(key.removeprefix("supp:"))
                if aid:
                    b.references[language].append(Reference(start=match.start(), end=match.end(), asset_id=aid, label=match.group()))
                elif b.kind not in ("caption", "reference") and language == "original":
                    document.issues.append(Issue(code="unresolved_reference", block_id=b.id, message=f"引用 {match.group()} 尚未绑定图表"))
    low = [b for b in document.blocks if b.confidence < .75]
    if low:
        document.issues.append(Issue(code="low_confidence", message=f"{len(low)} 个文字块的初始结构置信度低于 0.75，来源页已保留"))
    unique = {}
    for issue in document.issues:
        unique[(issue.code, issue.block_id, issue.message)] = issue
    document.issues = list(unique.values())


def numeric_tokens(text: str) -> Counter:
    # Whitespace, Unicode minus and thousands separators are formatting, not data changes.
    text=re.sub(r'(?<=[A-Za-z0-9])([⁻⁺]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+)',r' \1',text)
    # Range punctuation is distinct from the mathematical minus U+2212.
    text=re.sub(r'(?<=\d)[–—-](?=\d)',' ',text)
    text = unicodedata.normalize('NFKC',text).replace("−", "-").replace("–", "-").replace('’',"'").replace('‘',"'")
    # A lexical hyphen (day-40, 2-by-2) is not a negative sign.
    text=re.sub(r'(?<=[A-Za-z])-(?=\d)',' ',text)
    text = re.sub(r"(?<=\d)\s+(?=[′'])",'',text)
    text = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)
    scales={'thousand':1000,'million':1000000,'billion':1000000000,'万':10000,'亿':100000000}
    def scaled(match):
        value=Decimal(match.group(1))*scales[match.group(2).casefold()]
        return format(value.normalize(),'f')
    text=re.sub(r'([+-]?\d+(?:\.\d+)?)\s*(thousand\b|million\b|billion\b|万|亿)',scaled,text,flags=re.I)
    # Prime-bearing names are checked separately; expanding an abbreviation can repeat them.
    text=re.sub(r"(?<![\d.])\d+[′']+",'',text)
    return Counter(re.findall(r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", text))


def numeric_source(text,styles=None):
    previous=''
    for span in styles or []:
        if span.superscript and span.text.strip():
            last=re.search(r'(\S+)$',previous.rstrip())
            if last:
                # Preserve span boundaries: DaPars2 + superscript 25 is not
                # a single quantity 225. Source text itself stays unchanged.
                before=last.group(1);marker=span.text.strip()
                replacement=before+' '+marker
                if before[-1:] in ('−','–','-','+') and marker[:1].isdigit():
                    replacement=before[:-1]+' '+before[-1]+marker
                text=text.replace(before+marker,replacement,1)
            if re.fullmatch(r'\d+(?:,\d+)+',span.text.strip()):
                marker=span.text.strip();text=text.replace(marker,marker.replace(',',' '))
        previous=span.text
    return text


def numeric_counts(original,translated,styles=None):
    original=numeric_source(original,styles);translated=numeric_source(translated,styles)
    pattern=re.compile(r'(?<![A-Za-z0-9])[A-Za-z][A-Za-z_]*\d+[A-Za-z0-9_]*')
    common=set(pattern.findall(original))&set(pattern.findall(translated))
    # Only exact identifiers preserved in BOTH texts may repeat for clarity.
    # English words with attached citation numbers remain counted when the
    # word has been translated, e.g. neurons10 -> 神经元10.
    def strip_names(text):return pattern.sub(lambda m:'' if m.group() in common else m.group(),text)
    return numeric_tokens(strip_names(original)),numeric_tokens(strip_names(translated))


def translation_numbers_valid(original: str, translated: str, styles=None) -> bool:
    def prime_markers(text):
        text=re.sub(r"(?<=\d)\s+(?=[′'])",'',unicodedata.normalize('NFKC',text).replace('’',"'").replace('‘',"'"))
        return {(n,len(primes)) for n,primes in re.findall(r"(?<![\d.])(\d+)([′']+)",text)}
    def operators(text):
        text = text.replace("≤", "<=").replace("≥", ">=").replace("％", "%")
        return Counter(re.findall(r"<=|>=|[<>±%]", text))
    source_count,target_count=numeric_counts(original,translated,styles)
    return (source_count == target_count and operators(original) == operators(translated)
            and prime_markers(original)==prime_markers(translated))


def translation_numeric_report(original,translated,styles=None):
    source,target=numeric_counts(original,translated,styles)
    return {'passed':translation_numbers_valid(original,translated,styles),'missing':list((source-target).elements()),'extra':list((target-source).elements())}


def project_translation_styles(block):
    """Carry known superscript boundaries into preserved names/notation in the translation."""
    from .models import TextStyle
    ranges=[];previous=''
    for span in block.styles:
        if span.superscript and span.text.strip():
            last=re.search(r'([A-Za-z0-9_]+(?:\.\d+)?)([−–+-])?$',previous.rstrip())
            if last:
                marker=(last.group(2) or '')+span.text.strip()
                pattern=re.compile(r'(?<![A-Za-z0-9_])'+re.escape(last.group(1))+r'\s*'+re.escape(marker))
                for match in pattern.finditer(block.translated):
                    start=match.end()-len(marker);end=match.end()
                    if not any(start<b and end>a for a,b in ranges):ranges.append((start,end));break
        previous=span.text
    block.translated_styles=[];cursor=0
    for start,end in sorted(ranges):
        if start>cursor:block.translated_styles.append(TextStyle(text=block.translated[cursor:start]))
        block.translated_styles.append(TextStyle(text=block.translated[start:end],superscript=True));cursor=end
    if ranges and cursor<len(block.translated):block.translated_styles.append(TextStyle(text=block.translated[cursor:]))


def translation_language_valid(block,translated,target_language):
    chinese=bool(re.search(r'中文|简体|繁体|chinese|^zh(?:-|$)',target_language,re.I))
    prose=block.kind in ('title','heading','paragraph','abstract','caption','footnote')
    return (not chinese or not prose or len(re.findall(r'[A-Za-z]',block.original))<20
            or bool(re.search(r'[\u3400-\u9fff]',translated)))
