# -*- coding: utf-8 -*-
"""文本提取责任链包：每个梯队一个 TextExtractor 子类，ExtractorChain 串接。

P1 只含既有 5 个梯队（pypdf / JXA-PDFKit / docx-zip / doc-piece / image）。
新增梯队（如 P3 的 Vision OCR）= 新增一个类 + 在门面注册一行。
import 风格与调用方一致：shared/ 在 sys.path 上，包内模块用顶层绝对 import
（documents / extraction.*），与 extract_text、extract_fields 的既有惯例相同。
"""
