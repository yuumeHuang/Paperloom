from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf as fitz
from .llm import check_cancel
from .models import Asset, Page, RawBlock, TextStyle

# MuPDF native calls run on one dedicated thread, even with concurrent LLM jobs.
PDF_LOCK = threading.RLock()
PDF_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paperloom-pdf")


def join_lines(lines: list[str]) -> str:
    text = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not text:
            text = line
        elif text.endswith("\u00ad"):
            text = text[:-1] + line
        elif text.endswith("-") and line[:1].islower():
            text += line  # Keep literal hyphens; do not change a minus or compound word.
        elif re.search(r"[\u3400-\u9fff]$", text) and re.match(r"[\u3400-\u9fff]", line):
            text += line
        else:
            text += " " + line
    return text


def rect_valid(bbox: list, page: Page) -> bool:
    return (len(bbox) == 4 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox)
            and 0 <= bbox[0] < bbox[2] <= page.width + 0.1
            and 0 <= bbox[1] < bbox[3] <= page.height + 0.1)


def split_large_font_lines(block: RawBlock, native: dict) -> list[RawBlock]:
    """Separate title/leading bold heading lines from body in a native PDF block."""
    groups, previous, leading_bold = [], None, True
    for line in native.get('lines', []):
        spans = [s for s in line.get('spans', []) if s.get('text', '').strip()]
        if not spans:
            continue
        size = statistics.median(s.get('size', 10) for s in spans)
        boundary = previous is not None and max(size, previous) >= 14 and max(size, previous) / max(1, min(size, previous)) >= 1.4
        bold = all(s.get('flags', 0) & 16 for s in spans)
        if groups and leading_bold and bold and previous is not None and abs(size-previous)>=1 and max(size,previous)/max(1,min(size,previous))>=1.15:
            boundary=True  # A main heading and its smaller bold subsection are distinct.
        if groups and leading_bold and not bold and all(s.get('flags',0) & 16 for l in groups[0] for s in l.get('spans',[]) if s.get('text','').strip()):
            boundary = True
        if not bold:
            leading_bold = False
        if not groups or boundary:
            groups.append([])
        groups[-1].append(line); previous = size
    if len(groups) <= 1:
        return [block]
    result = []
    for i, lines in enumerate(groups):
        texts, styles = [], []
        for line in lines:
            texts.append(''.join(s.get('text', '') for s in line.get('spans', [])))
            for s in line.get('spans', []):
                flags = s.get('flags', 0)
                styles.append(TextStyle(text=s.get('text', ''), font=s.get('font', ''), size=s.get('size', 10),
                    bold=bool(flags & 16), italic=bool(flags & 2), superscript=bool(flags & 1)))
        text = join_lines(texts)
        if not text:
            continue
        bbox = [min(l['bbox'][0] for l in lines), min(l['bbox'][1] for l in lines),
                max(l['bbox'][2] for l in lines), max(l['bbox'][3] for l in lines)]
        result.append(RawBlock(id=f'{block.id}-s{i+1:02d}', parent_id=block.id, page=block.page, bbox=bbox, text=text,
            styles=styles, font_size=max((s.size for s in styles), default=10), bold=any(s.bold for s in styles)))
    return result


def refresh_text_blocks(pdf: Path, existing: list[RawBlock]) -> list[RawBlock]:
    """Upgrade text segmentation using native lines; page renders/assets stay immutable."""
    with PDF_LOCK:
        return PDF_EXECUTOR.submit(_refresh_text_blocks, pdf, existing).result()


def _refresh_text_blocks(pdf, existing):
    old = {}
    for b in existing:
        old.setdefault(b.parent_id or b.id, []).append(b)
    updated = []
    with fitz.open(pdf) as doc:
        for index, page in enumerate(doc):
            number = 0
            for native in page.get_text('dict', flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)['blocks']:
                if native.get('type') != 0:
                    continue
                text = join_lines([''.join(s.get('text','') for s in l.get('spans',[])) for l in native.get('lines',[])])
                if not text:
                    continue
                number += 1
                key = f'p{index+1:04d}-b{number:04d}'
                parts = old.get(key)
                if not parts:
                    raise ValueError('PDF 来源发生变化，不能复用提取结果')
                if ' '.join(join_lines([b.text for b in parts]).split()) != ' '.join(text.split()):
                    raise ValueError('PDF 来源文字发生变化，不能复用提取结果')
                parent = RawBlock(id=key,page=index+1,bbox=list(native['bbox']),text=text,
                    styles=[s for b in parts for s in b.styles],font_size=max(b.font_size for b in parts),bold=any(b.bold for b in parts))
                updated.extend(split_large_font_lines(parent,native))
    return updated


def crop_asset(pdf: Path, output: Path, asset: Asset) -> None:
    with PDF_LOCK:
        PDF_EXECUTOR.submit(_crop_asset, pdf, output, asset).result()


def _crop_asset(pdf: Path, output: Path, asset: Asset) -> None:
    with fitz.open(pdf) as doc:
        page = doc[asset.page - 1]
        rect = fitz.Rect(asset.bbox) & page.rect
        if rect.is_empty:
            raise ValueError("图表区域为空")
        destination = output / asset.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, alpha=False).save(destination)


def extract(pdf: Path, output: Path, cancel: threading.Event, progress):
    with PDF_LOCK:
        return PDF_EXECUTOR.submit(_extract, pdf, output, cancel, progress).result()


def _extract(pdf: Path, output: Path, cancel: threading.Event, progress):
    raw: list[RawBlock] = []
    pages: list[Page] = []
    assets: list[Asset] = []
    scanned: list[int] = []
    (output / "pages").mkdir(parents=True, exist_ok=True)
    (output / "assets").mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf) as doc:
        if not doc.is_pdf or doc.needs_pass:
            raise ValueError("请上传未加密的 PDF 文档")
        if not 1 <= len(doc) <= 300:
            raise ValueError("单篇 PDF 支持 1–300 页")
        metadata = doc.metadata or {}
        for index, page in enumerate(doc):
            check_cancel(cancel)
            number = index + 1
            preview = f"pages/{number:04d}.png"
            scale = min(2, 1400 / max(page.rect.width, page.rect.height))
            page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).save(output / preview)
            page_info = Page(number=number, width=page.rect.width, height=page.rect.height, preview=preview)
            pages.append(page_info)
            # Binary image payloads are excluded from text extraction to bound memory use.
            data = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)
            page_raw = []
            block_number = 0
            for block in data["blocks"]:
                if block.get("type") != 0:
                    continue
                lines = []
                styles = []
                for line in block.get("lines", []):
                    spans = line.get("spans", [])
                    lines.append("".join(s.get("text", "") for s in spans))
                    for s in spans:
                        flags = s.get("flags", 0)
                        styles.append(TextStyle(text=s.get("text", ""), font=s.get("font", ""), size=s.get("size", 10),
                                                bold=bool(flags & 16), italic=bool(flags & 2), superscript=bool(flags & 1)))
                text = join_lines(lines)
                if not text:
                    continue
                block_number += 1
                rb = RawBlock(id=f"p{number:04d}-b{block_number:04d}", page=number,
                              bbox=list(block["bbox"]), text=text,
                              font_size=max((s.size for s in styles), default=10),
                              bold=any(s.bold for s in styles), styles=styles)
                page_raw.extend(split_large_font_lines(rb, block))
            raw.extend(page_raw)
            if sum(len(b.text.strip()) for b in page_raw) < 40:
                scanned.append(number)
            regions: list[tuple[str, list, list | None]] = []
            # find_tables also detects ruled vector tables; output remains image-backed.
            try:
                for table in page.find_tables().tables:
                    regions.append(("table", list(table.bbox), table.extract()))
            except Exception:
                pass  # A layout model may supply regions; coverage still includes all cells.
            try:
                for image in page.get_image_info():
                    bbox = list(image["bbox"])
                    rect = fitz.Rect(bbox)
                    if rect.width > 55 and rect.height > 40 and rect.get_area() < page.rect.get_area() * 0.85:
                        regions.append(("figure", bbox, None))
            except Exception:
                pass
            try:
                for rect in page.cluster_drawings():
                    if rect.width < 70 or rect.height < 45 or rect.get_area() > page.rect.get_area() * 0.8:
                        continue
                    if any((rect & fitz.Rect(bbox)).get_area() > rect.get_area() * 0.5 for _, bbox, _ in regions):
                        continue
                    regions.append(("figure", list(rect), None))
            except Exception:
                pass
            for kind, bbox, cells in regions[:100]:
                bbox = list(fitz.Rect(bbox) & page.rect)
                if not rect_valid(bbox, page_info):
                    continue
                aid = f"a{len(assets) + 1:04d}"
                asset = Asset(id=aid, kind=kind, path=f"assets/{aid}.png", page=number, bbox=bbox, table=cells)
                # Render the entire region so vector elements and internal text are preserved.
                page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=fitz.Rect(bbox), alpha=False).save(output / asset.path)
                assets.append(asset)
            progress(number, len(doc))
    extraction = {"blocks": [b.model_dump() for b in raw], "pages": [p.model_dump() for p in pages],
                  "assets": [a.model_dump() for a in assets], "scanned": scanned, "pdf_metadata": metadata}
    (output / "extraction.json").write_text(json.dumps(extraction, ensure_ascii=False, indent=2), encoding="utf-8")
    return raw, pages, assets, scanned, metadata
