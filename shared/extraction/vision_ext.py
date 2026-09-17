# -*- coding: utf-8 -*-
"""OCR 梯队（Tier 1.5）：macOS 自带 Vision framework（backend=vision_ocr，仅 darwin）。

产品化自实测原型 /tmp/jahr-perf-audit/vision_ocr.js（B1 结论：3 份失败件全救回、
手机号 3/3、单份 1.16~1.58s、3 份并行 1.764s）。零 pip 依赖、零联网、零 API key：
只走系统自带 `osascript -l JavaScript`（JXA）调 VNRecognizeTextRequest
（Accurate 档，zh-Hans/zh-Hant/en-US，语言纠错开）。PDF 逐页 2x 缩放渲染提升
中文小字号识别率，页间用 \\f 分隔；图片直接 NSImage -> CGImage。

自包含纪律（P1 评审裁决②）：
  - osascript 走 subprocess argv（脚本按行拆成多个 -e 传入），不依赖调用方 cwd、
    不落任何临时文件、不新增 sys.path 操作；
  - 对 detect_scanned 的引用是**函数内延迟 import**（extract_text 门面在本模块
    之后才完成加载，顶层互相 import 会成环）。

护栏（B1 教训：markitdown 的水印噪声曾骗过护栏造成静默假成功）：
  OCR 文本在本梯队内先过两道——①数字字符数为 0 直接判不可信（简历几乎必含
  手机号/年份）；②detect_scanned 同款水印/重复串判定。过不了返回
  no_text_layer 而**不是**假 ok；chain 的 gate 还会对 ok 结果复核一遍。

TCC：osascript 首次读 ~/Desktop、~/Documents、iCloud 下的文件可能触发 macOS
授权弹窗，需用户点一次允许（部署文档已写明）。超时上限因此设为 60s：弹窗挂起
时不至于吃满旧 JXA 梯队的 120s。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

from documents import ExtractionResult, FileKind, ResumeDocument, nws
from extraction.base import TextExtractor, normalize_pdf_text

__all__ = ["VisionOcrExt"]

#: 单份 OCR 超时（秒）。实测单份 1.16~1.58s；上限主要防 TCC 弹窗挂起。
VISION_OCR_TIMEOUT_S = 60
#: 建议的 OCR 并发上限（实测 3 份并行 1.764s vs 串行 3.810s）。
#: 本梯队自身是同步单文件的；并发由调用方（intake 提取线程池）按此值封顶。
OCR_CONCURRENCY = 4
#: 低于此置信度的行计入 lowConfLines（notes 里给人工复核提示用）
LOW_CONF_THRESHOLD = 0.6

# JXA 脚本：逐行以 -e 传给 osascript（与 vision_ocr.js 同逻辑，输出改为 JSON，
# 附带置信度统计供 notes 记录）。
_VISION_SCRIPT = r'''
ObjC.import('Vision');
ObjC.import('AppKit');
ObjC.import('Quartz');
ObjC.import('Foundation');
function ocrCG(cg) {
  var req = $.VNRecognizeTextRequest.alloc.init;
  req.setRecognitionLevel(0);
  req.setRecognitionLanguages($.NSArray.arrayWithArray($(['zh-Hans', 'zh-Hant', 'en-US'])));
  req.setUsesLanguageCorrection(true);
  var h = $.VNImageRequestHandler.alloc.initWithCGImageOptions(cg, $.NSDictionary.dictionary);
  var err = Ref();
  var ok = h.performRequestsError($([req]), err);
  var out = {lines: [], confs: []};
  if (!ok) { return out; }
  var res = req.results;
  for (var i = 0; i < res.count; i++) {
    var c = res.objectAtIndex(i).topCandidates(1);
    if (c.count > 0) {
      out.lines.push(ObjC.unwrap(c.objectAtIndex(0).string));
      out.confs.push(c.objectAtIndex(0).confidence);
    }
  }
  return out;
}
function cgFromNSImage(img) {
  if (img.isNil()) { return null; }
  var tiff = img.TIFFRepresentation;
  if (tiff.isNil()) { return null; }
  var rep = $.NSBitmapImageRep.imageRepWithData(tiff);
  if (rep.isNil()) { return null; }
  return rep.CGImage;
}
function run(argv) {
  try {
    var path = argv[0];
    var lower = path.toLowerCase();
    var parts = [], confs = [], npages = 0;
    if (lower.slice(-4) === '.pdf') {
      var doc = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(path));
      if (doc.isNil()) { return JSON.stringify({ok: false, error: '无法打开 PDF'}); }
      var n = doc.pageCount;
      npages = n;
      for (var i = 0; i < n; i++) {
        var page = doc.pageAtIndex(i);
        var b = page.boundsForBox($.kPDFDisplayBoxMediaBox);
        var sz = $.NSMakeSize(b.size.width * 2.0, b.size.height * 2.0);
        var thumb = page.thumbnailOfSizeForBox(sz, $.kPDFDisplayBoxMediaBox);
        var cg = cgFromNSImage(thumb);
        var r = (cg === null) ? {lines: [], confs: []} : ocrCG(cg);
        parts.push(r.lines.join('\n'));
        confs = confs.concat(r.confs);
      }
    } else {
      var img = $.NSImage.alloc.initWithContentsOfFile(path);
      var cg2 = cgFromNSImage(img);
      if (cg2 === null) { return JSON.stringify({ok: false, error: '无法载入图片'}); }
      var r2 = ocrCG(cg2);
      parts.push(r2.lines.join('\n'));
      confs = r2.confs;
      npages = 1;
    }
    var sum = 0.0, min = 1.0, low = 0;
    for (var j = 0; j < confs.length; j++) {
      sum += confs[j];
      if (confs[j] < min) { min = confs[j]; }
      if (confs[j] < %LOW_CONF%) { low++; }
    }
    return JSON.stringify({
      ok: true, text: parts.join('\f'), npages: npages,
      stats: {lines: confs.length,
              avgConf: confs.length ? sum / confs.length : 0.0,
              minConf: confs.length ? min : 0.0,
              lowConfLines: low}
    });
  } catch (e) {
    return JSON.stringify({ok: false, error: String(e)});
  }
}
'''.replace("%LOW_CONF%", repr(LOW_CONF_THRESHOLD))


def _run_vision_ocr(path: Path) -> Dict[str, Any]:
    """跑一次 Vision OCR，返回脚本输出的 JSON dict。异常原样上抛由梯队捕获。"""
    if sys.platform != "darwin":
        raise RuntimeError("非 macOS，无 Vision framework 可用")
    if shutil.which("osascript") is None:
        raise RuntimeError("找不到 osascript")
    argv = ["osascript", "-l", "JavaScript"]
    for line in _VISION_SCRIPT.splitlines():
        if line.strip():
            argv += ["-e", line]
    argv.append(str(path))
    proc = subprocess.run(argv, capture_output=True, timeout=VISION_OCR_TIMEOUT_S)
    out = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        raise RuntimeError("osascript 退出码 %d: %s" % (
            proc.returncode,
            (proc.stderr.decode("utf-8", "replace").strip() or out)[:200]))
    try:
        payload = json.loads(out)
    except ValueError:
        raise RuntimeError("OCR 输出不是合法 JSON：%s" % out[:200])
    if not isinstance(payload, dict):
        raise RuntimeError("OCR 输出结构异常：%s" % out[:200])
    return payload


class VisionOcrExt(TextExtractor):
    """扫描件/图片的 OCR 救回梯队：在文本层梯队之后、放弃之前。

    can_handle 只在 darwin 上为 True：
      - 图片：直接受理（图片没有文本层梯队可言）；
      - PDF：仅当前序梯队全部没拿到可用文本（chain 已把 pypdf/JXA 的失败与
        「提取到文本但被护栏判为扫描件/水印」的降级结果记进 doc.prior）。
    跨平台兜底（agent 多模态）是 P4 的梯队，不在本模块范围。
    """

    def can_handle(self, doc: ResumeDocument) -> bool:
        # RECRUIT_NO_VISION（P4，仅测试用）：置位时本梯队恒不受理，用来在 darwin
        # 上模拟「非 macOS / Vision 失败或不可信」，验证 agent 多模态兜底通道
        # （needs_agent_vision → VISION_NEEDED → --apply-vision-patch）。
        # 默认不置位，行为与 P3 完全一致。
        if os.environ.get("RECRUIT_NO_VISION"):
            return False
        if sys.platform != "darwin":
            return False
        if doc.kind == FileKind.IMAGE:
            return True
        if doc.kind == FileKind.PDF:
            return bool(getattr(doc, "prior", None))
        return False

    def extract(self, doc: ResumeDocument) -> ExtractionResult:
        try:
            payload = _run_vision_ocr(doc.path)
        except subprocess.TimeoutExpired:
            return ExtractionResult(
                "", 0, "error", "none",
                ["Vision OCR 超时(%ds)——若在等待 macOS 授权弹窗，请点允许后重跑"
                 % VISION_OCR_TIMEOUT_S])
        except Exception as e:
            return ExtractionResult(
                "", 0, "error", "none",
                ["Vision OCR 失败(%s: %s)" % (type(e).__name__, e)])
        if not payload.get("ok"):
            return ExtractionResult(
                "", 0, "error", "none",
                ["Vision OCR 未能读取文件：%s" % str(payload.get("error") or "未知错误")[:200]])

        text = normalize_pdf_text(str(payload.get("text") or ""))
        npages = int(payload.get("npages") or 0) or (1 if nws(text) else 0)
        stats = payload.get("stats") or {}
        conf_note = ("OCR %s 行，置信度 mean=%.2f min=%.2f，低置信(<%.1f) %s 行"
                     % (stats.get("lines", "?"), float(stats.get("avgConf") or 0.0),
                        float(stats.get("minConf") or 0.0), LOW_CONF_THRESHOLD,
                        stats.get("lowConfLines", "?")))

        # ---- 护栏：OCR 文本不可信时判 no_text_layer，绝不假 ok ----
        body = nws(text)
        if not body:
            return ExtractionResult(
                "", npages, "no_text_layer", "none",
                ["Vision OCR 输出 0 个非空白字符（图片可能无文字内容）"])
        digits = sum(1 for c in body if c.isdigit())
        if digits == 0:
            return ExtractionResult(
                "", npages, "no_text_layer", "none",
                ["Vision OCR 文本 0 个数字字符——简历几乎必含手机号/年份，判 OCR 不可信"
                 "（%d 字符；%s）" % (len(body), conf_note)])
        if self._looks_scanned(text, doc.kind.value):
            return ExtractionResult(
                "", npages, "no_text_layer", "none",
                ["Vision OCR 文本仍是水印/重复串（%d 字符、数字 %d 个），判 OCR 不可信"
                 % (len(body), digits)])

        return ExtractionResult(text, npages, "ok", "vision_ocr", [
            "Vision OCR 救回：%d 字符 / %d 页；%s。"
            "OCR 文本可能有小误读（如 qq→q9、版面顺序导致的姓名误抓），"
            "手机号/邮箱/姓名等关键字段请留意报告里的人工确认警告"
            % (len(body), npages or 1, conf_note)])

    @staticmethod
    def _looks_scanned(text: str, kind: str) -> bool:
        # 延迟 import 避免与门面成环（见模块 docstring）
        from extract_text import detect_scanned
        return bool(detect_scanned(text, kind))
