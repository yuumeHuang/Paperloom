"""A self-authored fixture, intentionally with two columns, vectors and a table."""
from __future__ import annotations

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import run  # portable dependency path
import shutil
import threading
import uuid
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import simpleSplit
from paperloom.models import Asset, Issue
from paperloom.extract import crop_asset
from paperloom.pipeline import prepare
from paperloom.models import LLMConfig
from paperloom.package import pack
from paperloom.structure import associate_assets, finalize, overlap
from paperloom.reading import assign_sections,assemble_reading,normalize_hierarchy

OUT = ROOT / "tmp" / "demo-v3-build"

TEXTS = [
    ("A PDF preserves the appearance of a page, but its visual layout does not always express the order in which a paper should be read. This self-authored example demonstrates a continuous bilingual reading experience with traceable paragraphs, figures, and tables.", "PDF 保留了页面的外观，但视觉版面并不总能表达论文应有的阅读顺序。本教学示例展示了一种连续的双语阅读体验，段落、图片与表格都能追溯到原页。"),
    ("Scientific reading moves between ideas and evidence. A reader may follow an argument, examine Fig. 1, and then return to the same paragraph. Repeatedly searching through a fixed page layout interrupts this process.", "科研阅读需要在观点与证据之间往返。读者可能先理解论证，再查看 Fig. 1，然后回到同一段落。在固定页面中反复寻找位置，会打断这一过程。"),
    ("A structured paper keeps the original text, its translation, and its source coordinates together. The translation can be changed independently, while the original evidence remains available for inspection.", "结构化文献将原文、译文和来源坐标保存在一起。译文可以独立调整，原始证据则始终可以查阅。"),
    ("The reading order is determined from both geometry and semantic structure. Full-width material interrupts column flow; captions, equations, and reference entries need their own roles rather than being mixed into body paragraphs.", "阅读顺序需要结合几何位置和语义结构判断。通栏内容会中断分栏顺序；图注、公式和参考文献条目各有独立的作用，应当与正文段落分别组织。"),
    ("This example contains illustrative values only. Its purpose is to demonstrate interaction, not to report an experiment. All translations are supplied with the example so that the reader can be explored without an API key.", "本示例只包含示意数值，用于展示交互，不用于报告实验结果。译文已随示例提供，因此无需 API Key 即可体验阅读器。"),
    ("Each extracted text block is assigned a stable source identifier. The layout planner returns groups of identifiers in reading order, and the verifier checks that every source block is either used once or explicitly recorded as marginal text.", "每个提取的文字块都分配了稳定的来源标识。布局规划器按阅读顺序返回这些标识的分组，校验器检查每个来源块是否被恰好使用一次，或被明确记录为页边文字。"),
    ("Translation is performed after the structure is reconstructed. Paragraph identifiers remain stable. A configurable glossary helps keep terminology consistent, and numeric checks flag values that differ between the original and translated text.", "翻译在结构重建之后进行，段落标识保持稳定。可配置的术语表帮助统一译法，数字检查则标记原文与译文之间不一致的数值。"),
    ("The illustrative resource inventory is shown in Table 1. Images preserve the original appearance of vector plots and tables. Structured table cells are retained when available, but the source image remains the evidence to inspect.", "Table 1 展示了示意性的资源清单。图像保留了矢量图与表格的原始外观。能够提取时也会保存结构化单元格，但仍应以来源图像为核对依据。"),
    ("A portable package contains the document structure, bilingual text, a table of contents, figures, source-page previews, and the original PDF. File hashes allow the reader to check that the package is complete before importing it.", "便携文献包包含文档结构、双语文字、章节目录、图片、来源页预览及原始 PDF。文件哈希让阅读器能够在导入之前检查文献包是否完整。"),
    ("Different publishers use different layouts. An independent verification agent checks the first model's structure and translation decisions, and the preparation service repairs failed paragraphs through an ordered fallback chain.", "不同出版商使用不同版面。独立校验 agent 检查主模型的结构和翻译决策，准备端通过有序回退链自动修复失败段落。"),
    ("The separation of preparation and reading makes automated verification practical. A PC has the tools and processing time needed to reconstruct a paper. A phone needs only the resulting package and a calm interface for reading it.", "将准备过程与阅读过程分开，便于自动校验。PC 拥有重建论文所需的工具和处理时间；手机只需要最终的文献包，以及适合专注阅读的界面。"),
    ("Fig. 1. The preparation and reading workflow. Geometry provides evidence, a layout model proposes structure, and a reader presents linked bilingual paragraphs. The diagram is illustrative.", "Fig. 1. 文献准备与阅读流程。几何信息提供依据，布局模型提出结构，阅读器呈现带有链接的双语段落。本图为示意图。"),
    ("Table 1. An illustrative inventory of resources in a portable paper package. Values describe this example only.", "Table 1. 便携文献包的示意资源清单。数值仅描述本示例。"),
]


def make_pdf(destination):
    c = canvas.Canvas(str(destination), pagesize=(612, 792))
    c.setTitle("From PDF pages to a living research paper")
    c.setAuthor("Paperloom demo")
    def text(content, x, top, width=516, size=10, font="Times-Roman", leading=14):
        c.setFillColor(HexColor("#203436")); c.setFont(font, size)
        lines = simpleSplit(content, font, size, width)
        for index, line in enumerate(lines): c.drawString(x, 792 - top - index * leading, line)
        return top + len(lines) * leading
    def margin(n):
        text("PAPERLOOM / SELF-AUTHORED READING EXAMPLE", 48, 27, size=7, font="Helvetica")
        text(str(n), 302, 770, size=8, font="Helvetica")
    margin(1)
    text("From PDF pages to a", 48, 75, size=27, leading=32)
    text("living research paper", 48, 106, size=27, leading=32)
    text("Paperloom demo | A self-authored software fixture", 48, 141, size=10, font="Helvetica")
    text("Abstract", 48, 182, size=13, font="Times-Bold")
    text(TEXTS[0][0], 48, 206, leading=14)
    text("1 Introduction", 48, 279, size=14, font="Times-Bold")
    end = text(TEXTS[1][0], 48, 307, width=242)
    text(TEXTS[2][0], 48, end + 13, width=242)
    end = text(TEXTS[3][0], 323, 307, width=241)
    text(TEXTS[4][0], 323, end + 13, width=241)
    # A vector workflow: the crop preserves strokes AND internal text.
    for i, (label, detail) in enumerate([("EVIDENCE", "Text + geometry"), ("STRUCTURE", "Order + translation"), ("READING", "Paragraphs + figures")]):
        x = 49 + i * 184
        c.setFillColor(HexColor("#ecf0e7")); c.setStrokeColor(HexColor("#b5c2ac")); c.roundRect(x, 174, 145, 120, 6, fill=1, stroke=1)
        text(label, x + 18, 526, width=112, size=10, font="Helvetica-Bold")
        text(detail, x + 18, 552, width=113, size=9, font="Helvetica")
        c.setStrokeColor(HexColor("#789067")); c.setLineWidth(2)
        c.line(x + 19, 196, x + 124, 196)
        if i < 2:
            c.line(x + 155, 232, x + 173, 232); c.line(x + 168, 237, x + 173, 232); c.line(x + 168, 227, x + 173, 232)
    text(TEXTS[11][0], 48, 651, size=9, leading=12)
    c.showPage(); margin(2)
    text("2 Methods", 48, 68, size=14, font="Times-Bold")
    end = text(TEXTS[5][0], 48, 98, width=242)
    text(TEXTS[6][0], 48, end + 13, width=242)
    text("3 Results", 323, 68, size=14, font="Times-Bold")
    end = text(TEXTS[7][0], 323, 98, width=241)
    text(TEXTS[8][0], 323, end + 13, width=241)
    text(TEXTS[12][0], 48, 292, size=9, leading=12)
    rows = [["Resource", "Count", "Purpose"], ["Source PDF", "1", "Audit original evidence"], ["Source pages", "2", "Locate text and figures"], ["Figures", "1", "Open linked diagram"], ["Tables", "1", "Preserve cells and appearance"]]
    xs = [48, 213, 292, 564]; top = 323; height = 23
    for i, row in enumerate(rows):
        y = 792 - top - (i + 1) * height
        c.setFillColor(HexColor("#e8eee2") if i == 0 else HexColor("#f9faf7")); c.rect(48, y, 516, height, fill=1, stroke=0)
        for j, cell in enumerate(row): text(cell, xs[j] + 10, top + i * height + 15, width=xs[j + 1] - xs[j] - 16, size=9, font="Helvetica-Bold" if i == 0 else "Helvetica")
    c.setStrokeColor(HexColor("#c2ccbb")); c.setLineWidth(.6)
    for i in range(6): c.line(48, 792 - top - i * height, 564, 792 - top - i * height)
    for x in xs: c.line(x, 792 - top, x, 792 - top - 5 * height)
    text("4 Discussion", 48, 489, size=14, font="Times-Bold")
    end = text(TEXTS[9][0], 48, 519, width=242)
    text(TEXTS[10][0], 48, end + 13, width=242)
    text("5 References", 323, 489, size=14, font="Times-Bold")
    text("This demonstration does not cite or reproduce a published article. Its text, diagram, table, and bilingual passages were created for testing Paperloom.", 323, 519, width=241)
    c.save()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    make_pdf(OUT / "source.pdf")
    doc = prepare(OUT / "source.pdf", OUT, "paperloom-demo-v1", "paperloom-example.pdf", LLMConfig(enabled=False), threading.Event(), lambda *args: None, lambda entry: None)
    doc.metadata.demo = True; doc.metadata.journal = "PAPERLOOM 示例"; doc.metadata.year = "2026"
    doc.metadata.authors = ["Paperloom 文献阅读示例"]
    translations = {"Abstract": "摘要", "1 Introduction": "1 引言", "2 Methods": "2 方法", "3 Results": "3 结果", "4 Discussion": "4 讨论", "5 References": "5 参考说明", "From PDF pages to a living research paper": "让论文从 PDF 页面走向自然阅读"}
    translations.update(dict(TEXTS))
    # Some PDF engines emit the two title lines as separate blocks.
    title_blocks = [b for b in doc.blocks if b.original in ["From PDF pages to a", "living research paper"]]
    if len(title_blocks) == 2:
        first, second = title_blocks; first.original += " " + second.original; first.source.extend(second.source); first.styles.extend(second.styles); doc.blocks.remove(second)
    for b in doc.blocks:
        b.confidence = 1
        if b.original.startswith("Paperloom demo"):
            b.kind = "author"; b.translation_status = "not_required"
        if b.original in translations:
            b.translated = translations[b.original]; b.translation_status = "translated"
        elif b.original.startswith("This demonstration does not cite"):
            b.kind = "reference"; b.translated = "本演示未引用或复现已发表论文。文字、流程图、表格与双语段落均为测试 Paperloom 而编写。"; b.translation_status = "translated"
    doc.assets = [Asset(id="demo-fig1", kind="figure", label="Fig. 1", path="assets/demo-fig1.png", page=1, bbox=[46, 488, 565, 621], confidence=1),
                  Asset(id="demo-table1", kind="table", label="Table 1", path="assets/demo-table1.png", page=2, bbox=[47, 322, 565, 439], confidence=1,
                        table=[["Resource", "Count", "Purpose"], ["Source PDF", "1", "Audit original evidence"], ["Source pages", "2", "Locate text and figures"], ["Figures", "1", "Open linked diagram"], ["Tables", "1", "Preserve cells and appearance"]])]
    for a in doc.assets: crop_asset(OUT / "source.pdf", OUT, a)
    for b in doc.blocks:
        if b.kind == "caption": b.asset_id = "demo-table1" if b.label.lower().startswith('table') else "demo-fig1"
        if b.kind == "table": b.asset_id = "demo-table1"
        if b.source[0].page == 1 and overlap(b.source[0].bbox, doc.assets[0].bbox) > .85:
            b.kind = "figure_text"; b.asset_id = "demo-fig1"; b.translation_status = "not_required"
        if b.source[0].page == 2 and overlap(b.source[0].bbox, doc.assets[1].bbox) > .7:
            b.kind = "table"; b.asset_id = "demo-table1"; b.translation_status = "not_required"
    doc.issues = [Issue(code="demo", severity="info", message="自编教学样例，译文预先编写，图表为示意内容")]
    associate_assets(doc); finalize(doc);doc.reviewed = False
    normalize_hierarchy(doc.blocks);assign_sections(doc.blocks);assemble_reading(doc)
    doc.provenance["translation_model"] = "self-authored fixture translations"
    pack(doc, OUT)
    shutil.copyfile(OUT / "paper.folio", OUT / "demo.folio")
    for name in ('document.json','extraction.json','source.pdf','paper.folio','demo.folio'):
        shutil.copyfile(OUT/name,ROOT/'web/dist/demo'/name)
    for name in ('pages','assets'):
        shutil.copytree(OUT/name,ROOT/'web/dist/demo'/name,dirs_exist_ok=True)
    # Emit the schema for future native readers and package tooling.
    schema_dir = ROOT / "docs"; schema_dir.mkdir(exist_ok=True)
    (schema_dir / "document.schema.json").write_text(__import__('json').dumps(type(doc).model_json_schema(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Demo: {len(doc.blocks)} blocks, {len(doc.assets)} assets, {len(doc.toc)} chapters")
    missing = [b.original[:100] for b in doc.blocks if not b.translated and b.kind not in ("table", "figure_text", "author", "reference", "equation")]
    if missing: print("Missing fixture translations:", missing)

if __name__ == '__main__': main()
