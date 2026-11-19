# Paperloom 内部文献格式 1.0

扩展名 `.folio`，内容是标准 ZIP。MIME 可用 `application/zip`。文本 JSON 均为 UTF-8；本版本不支持加密 ZIP、ZIP64、跨卷 ZIP，也不执行包内 HTML/JS。

```text
manifest.json
document.json
source.pdf
extraction.json
pages/0001.png
assets/a0001.png
...
```

## manifest.json

```json
{
  "format": "paperloom",
  "version": "1.0",
  "document": "document.json",
  "id": "stable-document-id",
  "metadata": {"title": "Original title", "title_translated": "译文标题"},
  "reviewed": false,
  "files": [
    {"path": "document.json", "size": 1234, "sha256": "64-character lowercase hex"}
  ]
}
```

清单覆盖除 `manifest.json` 外的每个文件。路径使用相对 POSIX 路径，不得含 `..`、绝对路径、Windows 盘符或反斜杠。ZIP 与清单文件集合必须一致；长度和 SHA-256 必须逐一匹配。读取端要在解压前检查 ZIP central directory 的总长度和文件数，防止解压炸弹。

## document.json

完整类型由 `document.schema.json` 定义。

| 属性 | 作用 |
| --- | --- |
| `format`, `version`, `id` | 协议与稳定文献 ID |
| `metadata` | 原/译标题、作者、期刊、年份、DOI、语言、源文件名 |
| `blocks` | 已排好阅读顺序的稳定文字块；数组次序就是阅读次序 |
| `assets` | 图片/表格/公式区域，图注、结构化表格、来源坐标与置信度 |
| `pages` | 来源页尺寸及预览图片 |
| `toc` | 指向 heading 文字块的层级目录 |
| `issues` | 严重程度、质量提示与可选文字块目标 |
| `excluded` | 明确排除的来源 ID → 页眉/页脚/页码或出版提示原因 |
| `provenance` | 原始 PDF 哈希、模型、提示词哈希、来源覆盖与 OCR 页码 |
| `reading_sections` | 默认折叠的附属内容分组，含标题和稳定块 ID 列表 |
| `block_aliases` | 分段修复前 ID → 当前有效 ID，恢复书签和阅读进度 |
| `reviewed` | 旧版本兼容字段；当前自动校验结果记录在 provenance |

文字块种类：`title`、`author`、`abstract`、`heading`、`paragraph`、`caption`、`figure_text`、`equation`、`table`、`reference`、`footnote`、`metadata`。`figure_text` 是图内部标签，保留来源覆盖，但阅读器不把它混入正文。

每个 Block 包含：

- `id`：文档内唯一稳定 ID；翻译、目录、书签和自动校验均使用它。
- `original` / `translated`：来源组合原文与对应译文。译文为空时显示原文，不伪装为翻译完成。
- `translation_status`：`pending` / `translated` / `review` / `unavailable` / `not_required`。
- `translation_error`：重试和自动校验后仍未通过的原因；`review` 表示自动检查存在差异。
- `translated_by`：生成该段当前译文的模型 ID，供重新准备时选择另一个模型独立校验。
- `section`：`body`、`methods` 或附属内容类别。Methods 默认展开，参考文献、致谢等默认折叠。
- `source`：可能跨页的来源列表，包含 `page`、`bbox`、`block_id`。
- `styles`：来源字体名称/尺寸、加粗、斜体、上标和 span 文字。来源样式不要求译文采用相同字体。
- `translated_styles`：从来源投影的已知上标边界，辅助区分保留的基因名、数学指数和引用号。
- `heading_level`、`label`、`asset_id`：标题层级和图表关联。
- `confidence`：布局结构置信度，0..1；不是学术内容准确率。
- `references.original` / `.translated`：对应文字中的图表链接区间，含 `start`、`end`、`asset_id`、`label`。

## 坐标与文字区间

**PDF 坐标**：每页左上角为原点，x 向右、y 向下。单位是 point（1/72 inch），`bbox = [x0, y0, x1, y1]`。始终结合来源页 `width` / `height` 使用，不使用预览图的像素坐标。

**链接区间**：`start` 包含、`end` 不包含，单位是 Unicode code point；这是 Python 字符串索引语义。JavaScript 要使用 `Array.from(text)` 再切片，不能直接使用 UTF-16 `String.slice` 来解释含非 BMP 字符的区间。

图片资产只引用包内 `assets/` 文件，不携带外部 URL。表格数据是二维字符串/空值数组；来源 PNG 是最终核对依据。原始 PDF 随包保留，来源页预览供移动端快速查阅。

## 兼容策略

读取端只接受明确支持的版本；未来 schema 破坏性修改应提高协议版本。0.3.0 增加可选的阅读分组、别名、段落分类与校验原因，保留协议 1.0；缺少这些字段的旧包仍可阅读，并按常见标题临时折叠。重新准备后的文献 ID 保持不变。

`reading_sections` 示例：`{"id":"references","title":"References","translated":"参考文献","collapsed":true,"block_ids":["b00080","b00081"]}`。分组成员必须存在、全局唯一、至少一个；最多 40 组。所有 References 片段合为一个分组，目录只保留一个参考文献入口。`blocks` 的正文/方法在前，分组在后，来源页码不随重排改变。
