# -*- coding: utf-8 -*-
"""文本提取责任链包：每个梯队一个 TextExtractor 子类，ExtractorChain 串接。

现有梯队（= extract_text._CHAIN 的注册顺序）：
    pypdf_ext.PypdfExt        Tier 1   vendor pypdf（pdf）
    jxa_ext.JxaExt            Tier 1   macOS JXA/PDFKit（pdf，仅 darwin）
    vision_ext.VisionOcrExt   Tier 1.5 macOS Vision OCR（扫描件/图片，仅 darwin）
    docx_zip_ext.DocxZipExt   Tier 1   stdlib zipfile（docx）
    doc_textutil_ext.DocTextutilExt Tier 1  macOS textutil（doc）
    image_ext.ImageExt        终态     图片无文本层，如实 no_text_layer
    agent_patch_ext.AgentPatchExt  Tier 2  agent 多模态补丁；
                                      必须排链尾——can_handle 靠 doc.prior 判定本机
                                      梯队全失败，补丁表为空时恒不受理

新增梯队 = 新增一个类 + 在门面注册一行。
import 风格与调用方一致：shared/ 在 sys.path 上，包内模块用顶层绝对 import
（documents / extraction.*），与 extract_text、extract_fields 的既有惯例相同。
"""
