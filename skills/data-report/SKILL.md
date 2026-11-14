---
name: 数据分析报告
description: 读取工作区里的 CSV/Excel/JSON 数据，做统计并输出结论与图表建议
tags: [data, report]
---

# 数据分析流程

1. 用 `list_files` 找到数据文件，`read_file` 读取前 100 行确认字段与编码。
2. 用 `run_python` 执行 pandas 分析（若环境没有 pandas，改用 csv 模块）。
   脚本写到工作区 `.sandbox/` 下，输出只保留关键数字，避免刷屏。
3. 结论先行：先用 3 条以内的要点给出发现，再给支撑数据。
4. 涉及金额、比例时统一保留两位小数，并说明口径与样本量。
5. 需要可视化时，用 `run_python` 生成图片到工作区并告诉用户文件路径。
