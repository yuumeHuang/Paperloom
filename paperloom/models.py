from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

Kind = Literal["title", "author", "abstract", "heading", "paragraph", "caption", "figure_text", "equation", "table", "reference", "footnote", "metadata"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMEndpoint(Model):
    base_url: str = "https://api.openai.com/v1"
    api_key: SecretStr = SecretStr("")
    vision: bool = False
    json_mode: bool = True
    temperature: float = Field(default=0, ge=0, le=1)
    timeout: int = Field(default=180, ge=10, le=300)
    max_output_tokens: int = Field(default=8192, ge=1024, le=32768)
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    thinking_mode: Literal["auto", "enabled", "disabled"] = "auto"

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        url = urlsplit(value)
        if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Base URL 必须是无凭据、无查询参数的 HTTP(S) 地址")
        return value.rstrip("/")


class FallbackProvider(LLMEndpoint):
    name: str = Field(default="备用服务", min_length=1, max_length=80)
    models: list[str] = Field(min_length=1, max_length=4)
    inherit_primary_key: bool = False

    @field_validator('models')
    @classmethod
    def valid_models(cls, value):
        if any(not x.strip() or len(x)>180 for x in value):
            raise ValueError('备用模型名称无效')
        return list(dict.fromkeys(x.strip() for x in value))


class LLMConfig(LLMEndpoint):
    enabled: bool = True
    layout_model: str = Field(default="", max_length=180)
    translation_model: str = Field(default="", max_length=180)
    fallbacks: list[FallbackProvider] = Field(default_factory=list, max_length=4)
    retry_attempts: int = Field(default=3, ge=1, le=5)
    translation_workers: int = Field(default=2, ge=1, le=4)
    target_language: str = Field(default="简体中文", min_length=1, max_length=80)
    glossary: dict[str, str] = Field(default_factory=dict, max_length=200)

    @field_validator('layout_model','translation_model')
    @classmethod
    def clean_model_name(cls,value):
        if any(ord(c)<32 for c in value):raise ValueError('模型名称不能包含控制字符')
        return value.strip()

    @model_validator(mode='after')
    def credentials_stay_on_configured_provider(self):
        if any(p.inherit_primary_key and p.base_url!=self.base_url for p in self.fallbacks):
            raise ValueError('只能在相同 Base URL 的备用模型上复用主服务密钥')
        return self

    def safe_dump(self):
        return self.model_dump(exclude={'api_key':True,'fallbacks':{'__all__':{'api_key'}}})

    def ensure_preparation_ready(self):
        if not self.enabled:return
        if not self.layout_model.strip() or not self.translation_model.strip():
            raise ValueError('请填写布局模型和翻译模型')
        names={self.layout_model,self.translation_model}
        names.update(m for p in self.fallbacks for m in p.models)
        if len(names)<2:
            raise ValueError('独立校验需要另一个模型：请添加备用 provider／模型，或设置不同的布局与翻译模型')


class Source(Model):
    page: int = Field(ge=1)
    bbox: list[float] = Field(min_length=4, max_length=4)
    block_id: str


class TextStyle(Model):
    text: str
    font: str = ""
    size: float = 0
    bold: bool = False
    italic: bool = False
    superscript: bool = False


class RawBlock(Model):
    id: str
    page: int
    bbox: list[float]
    text: str
    font_size: float = 10
    bold: bool = False
    styles: list[TextStyle] = Field(default_factory=list)
    ocr: bool = False
    parent_id: str | None = None


class Page(Model):
    number: int
    width: float
    height: float
    preview: str


class Asset(Model):
    id: str
    kind: Literal["figure", "table", "equation"]
    label: str = ""
    path: str
    page: int
    bbox: list[float]
    caption: str = ""
    caption_translated: str = ""
    table: list[list[str | None]] | None = None
    confidence: float = Field(default=0.6, ge=0, le=1)


class Issue(Model):
    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "warning"
    block_id: str | None = None


class Reference(Model):
    start: int
    end: int
    asset_id: str
    label: str


class Block(Model):
    id: str
    kind: Kind
    original: str
    translated: str = ""
    translation_status: Literal["pending", "translated", "review", "unavailable", "not_required"] = "pending"
    source: list[Source]
    styles: list[TextStyle] = Field(default_factory=list)
    heading_level: int = Field(default=1, ge=1, le=6)
    translated_styles: list[TextStyle] = Field(default_factory=list)
    confidence: float = Field(default=0.6, ge=0, le=1)
    label: str = ""
    asset_id: str | None = None
    references: dict[str, list[Reference]] = Field(default_factory=lambda: {"original": [], "translated": []})
    section: str = Field(default="body", max_length=80)
    translation_error: str = Field(default="", max_length=500)
    translated_by: str = Field(default="", max_length=180)


class TocEntry(Model):
    block_id: str
    title: str
    translated: str = ""
    level: int = 1


class Metadata(Model):
    title: str = "Untitled paper"
    title_translated: str = ""
    authors: list[str] = Field(default_factory=list)
    journal: str = ""
    year: str = ""
    doi: str = ""
    language: str = "en"
    target_language: str = "简体中文"
    filename: str = ""
    demo: bool = False


class Document(Model):
    format: Literal["paperloom"] = "paperloom"
    version: Literal["1.0"] = "1.0"
    id: str
    metadata: Metadata
    blocks: list[Block]
    assets: list[Asset]
    pages: list[Page]
    toc: list[TocEntry] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    excluded: dict[str, str] = Field(default_factory=dict)
    provenance: dict = Field(default_factory=dict)
    reviewed: bool = False
    reading_sections: list[dict] = Field(default_factory=list)
    block_aliases: dict[str, str] = Field(default_factory=dict)


class LayoutNode(Model):
    kind: Kind
    source_ids: list[str] = Field(min_length=1)
    heading_level: int = Field(default=1, ge=1, le=6)
    confidence: float = Field(default=0.8, ge=0, le=1)
    label: str = ""
    asset_id: str | None = None

    @field_validator('heading_level', mode='before')
    @classmethod
    def optional_heading(cls, value):
        return 1 if value is None else value

    @field_validator('label', mode='before')
    @classmethod
    def optional_label(cls, value):
        return '' if value is None else value


class Ignore(Model):
    source_id: str
    reason: Literal["header", "footer", "page_number"]


class Region(Model):
    page: int
    bbox: list[float] = Field(min_length=4, max_length=4)
    kind: Literal["figure", "table", "equation"]
    label: str = ""
    caption_source_ids: list[str] = Field(default_factory=list)


class LayoutPlan(Model):
    nodes: list[LayoutNode]
    ignored: list[Ignore] = Field(default_factory=list)
    regions: list[Region] = Field(default_factory=list)


class TranslationItem(Model):
    id: str
    translated: str = Field(min_length=1)


class TranslationBatch(Model):
    translations: list[TranslationItem]
