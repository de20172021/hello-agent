---
name: pdf-reader
description: 读取并理解 PDF：提取文字、表格，定位引用页码，总结问答；只读，不生成/编辑 PDF
version: "1.0.0"
tags: [pdf, 文档, 提取]
author: 迁移自 workbuddy-pdf
---

# PDF 阅读理解

用 Python 提取 PDF 内容，回答用户关于文档的问题。只做**读取**：不生成、不编辑、
不转换 PDF（这类请求直接说明不支持，建议整理成 Markdown 或 DOCX 代替）。

## 第一步：确认环境与文件

1. 用 `list_files` 找到 PDF 文件的真实路径，确认它存在。
2. 用 `run_python` 检查提取库是否可用（脚本写到 `.sandbox/` 下）：

```python
try:
    import pdfplumber
    print("pdfplumber 可用")
except ImportError:
    try:
        import pypdf
        print("只有 pypdf 可用")
    except ImportError:
        print("两者都不可用，需要先安装")
```

3. 都不可用时用 `run_command` 安装：`python -m pip install pdfplumber`（优先，
   能带出表格和版面信息；装不上再退回 pypdf）。

## 第二步：先看篇幅，再决定读法

提取前先用一个小脚本输出**页数**和每页前两行（章节线索），不要一上来就把整份
文档的文字全部打印出来——长文档会刷爆上下文。

- 短文档（≤10 页）：一次性提取全文到工作区的 `pdf_extract.md`，再用 `read_file` 分段读。
- 长文档：先输出目录/章节，定位与问题相关的页码范围，只提取那些页。

## 第三步：提取与回答

```python
import pdfplumber

with pdfplumber.open("目标.pdf") as pdf:
    pages = pdf.pages[起:止]          # 长文档只取相关页
    for i, page in enumerate(pages, start=起 + 1):
        text = page.extract_text() or ""
        # 扫描页（图片型 PDF）提取结果会是空文本，不要当成空白页
        if len(text.strip()) < 10 and page.images:
            print(f"第 {i} 页疑似扫描页，无法直接提取文字")
        print(f"--- 第 {i} 页 ---\n{text[:2000]}")
```

要点：

- 提取结果可能丢字、乱序；**重要结论要能定位到页码**，引用时写明「第 N 页」。
- 提取表格用 `page.extract_tables()`，结果核对表头与单位再引用数字。
- 页眉页脚会重复出现，统计字数或引用时注意剔除。
- 回答时给结论和出处（页码），只说明影响结论的读取限制（如扫描页没 OCR）。

## 输出

- 直接回答用户问题，引用附页码；用户要原文摘录时用引用块给出。
- 提取的全文落盘到工作区（如 `pdf_extract.md`），告诉用户路径，方便后续追问。
