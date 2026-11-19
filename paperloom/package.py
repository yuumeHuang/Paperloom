from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path, PurePosixPath

from .models import Document


def safe_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value and ":" not in value


def validate_document(document: Document):
    block_ids = [b.id for b in document.blocks]
    asset_ids = [a.id for a in document.assets]
    pages = {p.number: p for p in document.pages}
    if len(set(block_ids)) != len(block_ids) or len(set(asset_ids)) != len(asset_ids):
        raise ValueError("文字块或图表 ID 重复")
    if not document.blocks:
        raise ValueError("文献没有可阅读的内容")
    seen=set();groups=set()
    if len(document.reading_sections)>40:raise ValueError('阅读分组过多')
    for section in document.reading_sections:
        if not isinstance(section,dict) or not isinstance(section.get('id'),str) or not re.fullmatch(r'[a-z_]{1,80}',section['id']) or section['id'] in groups or not isinstance(section.get('block_ids'),list) or not section['block_ids'] or section.get('collapsed') is not True or not isinstance(section.get('title'),str) or not isinstance(section.get('translated'),str):
            raise ValueError('阅读分组格式无效')
        groups.add(section['id'])
        for id in section['block_ids']:
            if id not in block_ids or id in seen:raise ValueError('阅读分组包含未知或重复段落')
            seen.add(id)
    if any(v not in block_ids for v in document.block_aliases.values()):raise ValueError('段落别名目标不存在')
    for block in document.blocks:
        if block.asset_id and block.asset_id not in asset_ids:
            raise ValueError("文字块绑定的图表不存在")
        if not block.source or any(s.page not in pages for s in block.source):
            raise ValueError("文字块缺少有效来源页")
        for lang, refs in block.references.items():
            text = block.original if lang == "original" else block.translated
            if any(r.asset_id not in asset_ids or r.start < 0 or r.end > len(text) or r.end <= r.start for r in refs):
                raise ValueError("图表引用区间无效")
    for entry in document.toc:
        if entry.block_id not in block_ids:
            raise ValueError("目录目标不存在")
    for asset in document.assets:
        if not safe_path(asset.path) or not asset.path.startswith("assets/"):
            raise ValueError("图表文件路径无效")
    for page in document.pages:
        if not safe_path(page.preview) or not page.preview.startswith("pages/"):
            raise ValueError("来源预览路径无效")


def write_document(document: Document, directory: Path):
    validate_document(document)
    target = directory / "document.json"
    temp = target.with_suffix(".tmp")
    temp.write_text(document.model_dump_json(indent=2), encoding="utf-8")
    temp.replace(target)


def pack(document: Document, directory: Path) -> Path:
    write_document(document, directory)
    paths = {"document.json", "source.pdf", "extraction.json"}
    paths.update(a.path for a in document.assets)
    paths.update(p.preview for p in document.pages)
    files = []
    for name in sorted(paths):
        if not safe_path(name):
            raise ValueError("打包文件路径无效")
        path = directory / name
        if not path.is_file():
            raise ValueError(f"文献包缺少文件：{name}")
        files.append({"path": name, "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = {"format": "paperloom", "version": "1.0", "document": "document.json", "id": document.id,
                "metadata": document.metadata.model_dump(), "files": files, "reviewed": document.reviewed}
    target = directory / "paper.folio"
    temporary = target.with_suffix(".tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for item in files:
            archive.write(directory / item["path"], item["path"])
    temporary.replace(target)
    return target


def verify_package(path: Path) -> Document:
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 3000 or sum(i.file_size for i in entries) > 400 * 1024 * 1024:
            raise ValueError("文献包解压大小超过限制")
        names = [i.filename for i in entries]
        if len(set(names)) != len(names) or any(not safe_path(n) for n in names):
            raise ValueError("文献包包含重复或不安全的文件路径")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != "paperloom" or manifest.get("version") != "1.0":
            raise ValueError("不支持的文献包版本")
        listed = manifest["files"]
        if len({f["path"] for f in listed}) != len(listed):
            raise ValueError("清单中存在重复文件")
        if set(names) != {"manifest.json", *(f["path"] for f in listed)}:
            raise ValueError("文件列表与清单不一致")
        for item in listed:
            data = archive.read(item["path"])
            if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError("文献包校验失败")
        document = Document.model_validate_json(archive.read("document.json"))
        validate_document(document)
        if document.id != manifest["id"]:
            raise ValueError("文献包 ID 不一致")
        return document
