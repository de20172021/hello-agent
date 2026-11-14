---
name: excel-analysis
description: 分析既有 Excel/CSV 表格：读取确认字段、清洗、透视统计、结论先行，可出图
version: "1.0.0"
tags: [excel, csv, 数据分析]
author: 迁移自 workbuddy-sheetagent / sheet-generation
---

# 表格分析流程

对工作区里已有的 Excel（.xlsx/.xls）或 CSV 文件做查询、清洗、统计、透视。
不做主观发挥：数字必须有出处，结论必须能被数据支撑。

## 第一步：确认数据长什么样

不要假设表头。先读前几行确认字段、编码、数据从哪一行开始（Excel 常有标题行、
合并单元格、备注行）。用 `run_python`（脚本放 `.sandbox/` 下）：

```python
import pandas as pd

# CSV：编码不对就换 gbk / gb18030；分隔符不对就换 sep="\t"
df = pd.read_csv("数据.csv", nrows=5)
# Excel：sheet_name=None 会读所有工作表，返回字典
# df = pd.read_excel("数据.xlsx", sheet_name=None)

print(df.shape)
print(df.dtypes)
print(df.head())
```

多个工作表时，先把每个表的名字、行数、列名列出来，确认用户指的是哪一个。

## 第二步：清洗与统计

- 缺失值、重复行、异常值：先报告发现了什么（多少行、哪些列），再决定怎么处理；
  用户没说怎么处理时，默认**保留原数据**，用派生列或过滤视图分析。
- 日期列用 `pd.to_datetime` 转换后再做时间统计。
- 输出只打印关键数字（行数、汇总值、分布），不要把整个 DataFrame 刷出来。

## 第三步：结论先行

1. 先用 3 条以内的要点给出发现，再给支撑数据。
2. 涉及金额、比例统一保留两位小数，说明口径与样本量（基于多少行、哪个时间范围）。
3. 用户要新表时（筛选结果、透视表），用 `df.to_excel()` / `to_csv()` 落盘到工作区，
   告诉用户文件路径；保留原文件不动。

## 出图（可选）

```python
import matplotlib.pyplot as plt
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei"]  # Windows 中文字体
plt.rcParams["axes.unicode_minus"] = False

df["列"].value_counts().head(10).plot(kind="bar")
plt.tight_layout()
plt.savefig("chart.png", dpi=150)
```

图片存到工作区并在回答里给出路径。柱状/折线/饼图按数据类型选，不堆砌。

## 依赖

pandas 不可用时用 `run_command` 安装 `python -m pip install pandas openpyxl`；
仍失败则退回标准库 `csv` 模块，并说明功能受限。
