---
name: docx-writer
description: 用 python-docx 生成结构规范的 Word 文档：结构先行、样式统一、中文排版
version: "1.0.0"
tags: [word, docx, 文档]
author: 迁移自 workbuddy-docx
---

# Word 文档生成流程

把大纲、报告、整理好的内容生成为 .docx 文件。核心原则：**结构先行，样式统一**。

## 第一步：先定结构，再写代码

动笔前先在回答里列出文档大纲（标题层级、每节写什么），内容多时请用户确认后再生成。
不要一边生成一边现编结构。

## 第二步：检查依赖

用 `run_python` 确认 `python-docx` 可用；不可用时 `run_command` 执行
`python -m pip install python-docx`，装不上就明确告诉用户无法生成 Word，
并建议改出 Markdown 代替，不要硬凑。

## 第三步：生成文档

脚本写到 `.sandbox/` 下执行，产物落在工作区（如 `报告.docx`）：

```python
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn

doc = Document()

# 统一样式：正文宋体小四、西文 Times New Roman，一次设置全局生效
style = doc.styles["Normal"]
style.font.name = "Times New Roman"
style.font.size = Pt(12)
style.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")

doc.add_heading("文档标题", level=0)   # 标题用内置样式，不要手动加粗模拟
doc.add_heading("第一章 背景", level=1)
doc.add_paragraph("正文段落……")

# 表格用 add_table，不要用空格/制表符模拟对齐
table = doc.add_table(rows=1, cols=3)
table.style = "Table Grid"

doc.save("报告.docx")
```

## 排版守则

- 标题一律用 `add_heading`（大纲级别正确，导航窗格可用）；正文用 `add_paragraph`。
- 中文字体必须同时设置 `w:eastAsia`（宋体/黑体），只设 `font.name` 中文会不生效。
- 层级只用 1-3 级标题，不再往下套；并列内容优先用表格或列表，不用长段罗列。
- 数字口径与来源要在正文或脚注里说明；金额保留两位小数。
- 长内容分节生成后再合并，避免一个超长脚本中途报错全部重来。

## 收尾

生成后打印文件的绝对路径与大小确认成功，在回答里告诉用户路径。
用户要改内容时改脚本重新生成，不要手工描述「哪里该改」让用户自己弄。
