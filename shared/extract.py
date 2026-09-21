# -*- coding: utf-8 -*-
"""从简历/JD 文件提取纯文本。零第三方依赖：stdlib + shared/vendor(pypdf/olefile)。

统一入口 extract(path) -> {"text": str, "needs_ocr": bool, "error": str|None}
优先级：系统工具(pdftotext/textutil) -> vendor 纯 Python 回退。
"""
import os
import re
import subprocess
import sys
import zipfile

_SHARED_DIR = os.path.dirname(os.path.abspath(__file__))
_VENDOR_DIR = os.path.join(_SHARED_DIR, "vendor")
_TIMEOUT = 30
_IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}


def _run(argv):
    """执行外部命令，成功返回 stdout 文本，否则空串。"""
    try:
        r = subprocess.run(argv, capture_output=True, timeout=_TIMEOUT)
        return r.stdout.decode("utf-8", "ignore") if r.returncode == 0 else ""
    except Exception:
        return ""


def _vendor_import(name):
    if _VENDOR_DIR not in sys.path:
        sys.path.insert(0, _VENDOR_DIR)
    return __import__(name)


def _textutil(path):
    return _run(["textutil", "-convert", "txt", "-stdout", path])


def _extract_pdf(path):
    txt = _run(["pdftotext", "-enc", "UTF-8", path, "-"])
    if txt.strip():
        return txt, None
    try:
        pypdf = _vendor_import("pypdf")
        with open(path, "rb") as fh:
            parts = [p.extract_text() or "" for p in pypdf.PdfReader(fh).pages]
        return "\n".join(parts), None
    except Exception as e:
        return txt, "pdf extract failed: %s" % e


def _extract_docx(path):
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "ignore")
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
        xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
        txt = re.sub(r"<[^>]+>", "", xml)
        if txt.strip():
            return txt, None
    except Exception:
        pass
    txt = _textutil(path)
    return txt, (None if txt.strip() else "docx extract empty")


def _extract_doc(path):
    txt = _textutil(path)
    if txt.strip():
        return txt, None
    try:  # 粗提取 WordDocument 流 utf-16le 可打印串（尽力而为）
        olefile = _vendor_import("olefile")
        ole = olefile.OleFileIO(path)
        try:
            data = ole.openstream("WordDocument").read()
        finally:
            ole.close()
        runs = re.findall(r"[\u4e00-\u9fffA-Za-z0-9\u3001\u3002\uff0c\uff1a"
                          r"\uff1b\uff08\uff09@./%\+\-\s]{8,}",
                          data.decode("utf-16-le", "ignore"))
        return "\n".join(runs), None
    except Exception as e:
        return txt, "doc extract failed: %s" % e


def extract(path):
    """统一入口：返回 {"text", "needs_ocr", "error"}。图片只标记 needs_ocr。"""
    result = {"text": "", "needs_ocr": False, "error": None}
    ext = os.path.splitext(path)[1].lower()
    if ext in _IMG_EXT:
        result["needs_ocr"] = True
        return result
    try:
        if ext == ".pdf":
            txt, err = _extract_pdf(path)
        elif ext == ".docx":
            txt, err = _extract_docx(path)
        elif ext == ".doc":
            txt, err = _extract_doc(path)
        else:  # .txt/.md 等纯文本
            with open(path, "rb") as fh:
                txt, err = fh.read().decode("utf-8", "ignore"), None
        result["text"], result["error"] = txt, err
    except Exception as e:
        result["error"] = str(e)
    return result
