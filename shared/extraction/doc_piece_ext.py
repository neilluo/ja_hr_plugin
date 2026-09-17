# -*- coding: utf-8 -*-
""".doc 梯队：OLE2/CFB 容器 + MS-DOC piece table（backend=ole_vendored/ole_stdlib）。

.doc 的规范外风险与护栏（自旧模块 docstring 原样保留）
------------------------------------------------------
实测这批 20 份 WPS 产出的 .doc，FIB flags（偏移 0x0A）bit 0x0004 = fComplex
**全部为 True**，即按 MS-DOC 规范它们是 piece table 分段存储，「fcMin/fcMac
直读 + UTF-16-LE 切片」属规范外路径（这批文件恰好只有 1 个 piece、文本连续，
所以侥幸可用）。本模块因此：
  1. 优先走**规范内**的 piece table 路径（实测 20/20 与 macOS textutil 输出
     逐字符一致，非空白字符相似度 1.0000，0 处差异）；
  2. 直读只作为兜底，一旦启用就写 warning；
  3. 无论走哪条路，返回前都过一遍护栏（护栏在门面阶段 2）：字符数下限、
     CJK 占比、乱码字符占比。护栏不过 -> status=garbled，交给上层转人工。
     **不假装 100% 覆盖。**

容器读取：vendor olefile 优先，失败退回自带 stdlib CFB 读取器（MiniOLE）。
PermissionError（EncryptionInfo/EncryptedPackage 流存在 = 加密）原样上抛，
门面映射成 status=encrypted，与旧实现一致。
"""

from __future__ import annotations

import io
import re
import struct
from typing import Any, Dict, List, Optional, Tuple

from documents import ExtractionResult, FileKind, ResumeDocument
from documents import ensure_vendor_path
from extraction.base import TextExtractor

__all__ = ["DocPieceExt", "MiniOLE"]

_OLE_SIG = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class MiniOLE(object):
    """~120 行的 stdlib OLE2(CFB) 读取器，仅用于 vendor olefile 不可用时兜底。

    实测与前序调研结论一致：对本批 20 份 .doc，读出的 WordDocument 流与
    olefile 逐字节相同（20/20）。
    """

    ENDOFCHAIN = 0xFFFFFFFE
    FREESECT = 0xFFFFFFFF

    def __init__(self, data: bytes):
        if not data.startswith(_OLE_SIG):
            raise ValueError("不是 OLE2 复合文档（魔数不符）")
        if len(data) < 0x200:
            raise ValueError("文件过短，OLE 头不完整（%d 字节）" % len(data))
        self.d = data
        self.ss = 1 << struct.unpack("<H", data[0x1E:0x20])[0]      # sector size
        self.mss = 1 << struct.unpack("<H", data[0x20:0x22])[0]     # mini sector
        self.cutoff = struct.unpack("<I", data[0x38:0x3C])[0]       # mini cutoff
        num_fat = struct.unpack("<I", data[0x2C:0x30])[0]
        dir_start = struct.unpack("<I", data[0x30:0x34])[0]
        minifat_start = struct.unpack("<I", data[0x3C:0x40])[0]
        difat_start = struct.unpack("<I", data[0x44:0x48])[0]
        difat_count = struct.unpack("<I", data[0x48:0x4C])[0]
        if self.ss < 128 or self.ss > (1 << 16):
            raise ValueError("非法 sector size: %d" % self.ss)

        difat = list(struct.unpack("<109I", data[0x4C:0x200]))
        sec = difat_start
        for _ in range(min(difat_count, 10000)):
            if sec in (self.ENDOFCHAIN, self.FREESECT) or sec >= 1 << 24:
                break
            blk = self._sector(sec)
            entries = struct.unpack("<%dI" % (self.ss // 4), blk)
            difat.extend(entries[:-1])
            sec = entries[-1]

        self.fat: List[int] = []
        for s in difat[:num_fat]:
            if s in (self.ENDOFCHAIN, self.FREESECT):
                continue
            self.fat.extend(struct.unpack("<%dI" % (self.ss // 4), self._sector(s)))

        self.dirents: List[Tuple[str, int, int, int]] = []
        for sec in self._chain(dir_start):
            blk = self._sector(sec)
            for i in range(0, self.ss, 128):
                e = blk[i:i + 128]
                if len(e) < 128:
                    break
                nlen = struct.unpack("<H", e[0x40:0x42])[0]
                name = e[:max(0, nlen - 2)].decode("utf-16-le", "ignore")
                etype = e[0x42]
                start = struct.unpack("<I", e[0x74:0x78])[0]
                size = struct.unpack("<Q", e[0x78:0x80])[0]
                self.dirents.append((name, etype, start, size))
        if not self.dirents:
            raise ValueError("OLE 目录为空")

        root = self.dirents[0]
        self.ministream = self._read_regular(root[2], root[3]) if root[3] else b""
        self.minifat: List[int] = []
        for sec in self._chain(minifat_start):
            self.minifat.extend(
                struct.unpack("<%dI" % (self.ss // 4), self._sector(sec)))

    def _sector(self, sec: int) -> bytes:
        off = (sec + 1) * self.ss
        blk = self.d[off:off + self.ss]
        if len(blk) < self.ss:
            blk = blk + b"\x00" * (self.ss - len(blk))
        return blk

    def _chain(self, start: int):
        sec = start
        seen = set()
        limit = len(self.fat) + 16
        while (sec not in (self.ENDOFCHAIN, self.FREESECT)
               and sec < len(self.fat) and sec not in seen and len(seen) < limit):
            seen.add(sec)
            yield sec
            sec = self.fat[sec]

    def _read_regular(self, start: int, size: int) -> bytes:
        out = b"".join(self._sector(s) for s in self._chain(start))
        return out[:size]

    def stream_names(self) -> List[str]:
        return [nm for nm, et, _s, _z in self.dirents if et == 2]

    def open_stream(self, name: str) -> bytes:
        for nm, etype, start, size in self.dirents:
            if nm == name and etype == 2:
                if size < self.cutoff:
                    out = b""
                    sec = start
                    seen = set()
                    while (sec not in (self.ENDOFCHAIN, self.FREESECT)
                           and sec < len(self.minifat) and sec not in seen):
                        seen.add(sec)
                        out += self.ministream[sec * self.mss:(sec + 1) * self.mss]
                        sec = self.minifat[sec]
                    return out[:size]
                return self._read_regular(start, size)
        raise KeyError("流 %r 不存在（现有流: %s）" % (name, self.stream_names()))


def _read_ole_streams(data: bytes) -> Tuple[Dict[str, bytes], str]:
    """读出 OLE 容器里的全部顶层流。返回 (streams, backend)。"""
    ensure_vendor_path()
    try:
        import olefile  # vendor 目录优先
    except Exception as e:                                  # pragma: no cover
        olefile = None
        vendor_err = "%s: %s" % (type(e).__name__, e)
    else:
        vendor_err = ""
    if olefile is not None:
        try:
            o = olefile.OleFileIO(io.BytesIO(data))
            try:
                streams = {}
                for entry in o.listdir():
                    if len(entry) != 1:
                        continue
                    try:
                        streams[entry[0]] = o.openstream(entry).read()
                    except Exception:
                        continue
                if "WordDocument" not in streams:
                    raise KeyError("olefile 未找到 WordDocument 流")
                return streams, "ole_vendored"
            finally:
                o.close()
        except Exception as e:
            vendor_err = "olefile 失败(%s: %s)，改用 stdlib CFB 读取器" % (
                type(e).__name__, e)
    # 兜底：自带 stdlib CFB 读取器
    try:
        mini = MiniOLE(data)
        streams = {}
        for nm in mini.stream_names():
            try:
                streams[nm] = mini.open_stream(nm)
            except Exception:
                continue
        if "WordDocument" not in streams:
            raise KeyError("MiniOLE 未找到 WordDocument 流")
        if vendor_err:
            streams["__vendor_note__"] = vendor_err.encode("utf-8")
        return streams, "ole_stdlib"
    except Exception as e:
        raise ValueError("OLE 容器解析失败: %s | %s" % (
            e, vendor_err or "vendor olefile 不可用"))


# MS-DOC: 8-bit 压缩 piece 用的 CP1252 特殊映射（0x80-0x9F 中非 ISO-8859-1 的部分）
_CP1252_MAP = {
    0x82: 0x201A, 0x83: 0x0192, 0x84: 0x201E, 0x85: 0x2026, 0x86: 0x2020,
    0x87: 0x2021, 0x88: 0x02C6, 0x89: 0x2030, 0x8A: 0x0160, 0x8B: 0x2039,
    0x8C: 0x0152, 0x91: 0x2018, 0x92: 0x2019, 0x93: 0x201C, 0x94: 0x201D,
    0x95: 0x2022, 0x96: 0x2013, 0x97: 0x2014, 0x98: 0x02DC, 0x99: 0x2122,
    0x9A: 0x0161, 0x9B: 0x203A, 0x9C: 0x0153, 0x9F: 0x0178,
}


def _decode_piece(raw: bytes, compressed: bool) -> str:
    if compressed:
        out = []
        for b in bytearray(raw):
            out.append(chr(_CP1252_MAP.get(b, b)))
        return "".join(out)
    return raw.decode("utf-16-le", errors="replace")


def _clean_word_text(text: str) -> str:
    """.doc 正文清洗。

    \x07（表格单元格/行结束符）-> \n，\r 与 \x0b（软换行）-> \n，
    去掉其余控制字符（**保留** \n 和 \t —— 前序实验里
    `re.sub(r"[\\x00-\\x06\\x08-\\x1f]", "", t)` 把刚生成的 \\n 也删掉了，
    导致字符数被低估 3.6%，进而误判成「textutil 比我们多提 3.6%」）。
    """
    text = text.replace("\x07", "\n").replace("\r", "\n").replace("\x0b", "\n")
    text = re.sub(r"[\x00-\x06\x08\x0c-\x1f]", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _doc_piece_table_text(wd: bytes, streams: Dict[str, bytes]) -> Tuple[str, Dict[str, Any]]:
    """按 MS-DOC 规范走 piece table：FIB.fcClx -> Clx -> Pcdt -> PlcPcd -> PCD。

    这是规范内路径；fComplex=True 的文档必须走这里才保证不漏内容。
    """
    if len(wd) < 0x1AA + 8:
        raise ValueError("WordDocument 流过短（%d 字节），无法读 FIB" % len(wd))
    flags = struct.unpack("<H", wd[0x0A:0x0C])[0]
    f_complex = bool(flags & 0x0004)
    f_which_tbl = bool(flags & 0x0200)
    fc_clx, lcb_clx = struct.unpack("<II", wd[0x01A2:0x01AA])

    preferred = "1Table" if f_which_tbl else "0Table"
    order = [preferred] + [n for n in ("0Table", "1Table") if n != preferred]
    last_err: Optional[str] = None
    for tname in order:
        tbl = streams.get(tname)
        if not tbl:
            last_err = "缺少 %s 流" % tname
            continue
        if lcb_clx <= 0 or fc_clx + lcb_clx > len(tbl):
            last_err = "fcClx/lcbClx 越界(%d/%d, 流长 %d)" % (fc_clx, lcb_clx, len(tbl))
            continue
        clx = tbl[fc_clx:fc_clx + lcb_clx]
        # 跳过前导 Prc（clxt == 0x01）数组，定位 Pcdt（clxt == 0x02）
        i = 0
        ok = True
        while i < len(clx) and clx[i] == 0x01:
            if i + 3 > len(clx):
                ok = False
                break
            cb = struct.unpack("<H", clx[i + 1:i + 3])[0]
            i += 3 + cb
        if not ok or i >= len(clx) or clx[i] != 0x02:
            last_err = "Clx 中未找到 Pcdt（偏移 %d / 共 %d 字节）" % (i, len(clx))
            continue
        lcb = struct.unpack("<I", clx[i + 1:i + 5])[0]
        plc = clx[i + 5:i + 5 + lcb]
        if lcb < 16 or len(plc) < lcb:
            last_err = "PlcPcd 长度非法(lcb=%d, 实得 %d)" % (lcb, len(plc))
            continue
        n = (lcb - 4) // 12
        if n <= 0 or 4 * (n + 1) + 8 * n > len(plc):
            last_err = "piece 数量非法(n=%d, plc=%d)" % (n, len(plc))
            continue
        cps = struct.unpack("<%dI" % (n + 1), plc[:4 * (n + 1)])
        pieces: List[str] = []
        pcd_base = 4 * (n + 1)
        bad = False
        for k in range(n):
            pcd = plc[pcd_base + 8 * k: pcd_base + 8 * k + 8]
            fc_raw = struct.unpack("<I", pcd[2:6])[0]
            compressed = bool(fc_raw & 0x40000000)
            fc = fc_raw & 0x3FFFFFFF
            if compressed:
                fc //= 2
            cch = cps[k + 1] - cps[k]
            if cch < 0 or cch > len(wd):
                bad = True
                break
            nbytes = cch if compressed else cch * 2
            if fc + nbytes > len(wd):
                bad = True
                break
            pieces.append(_decode_piece(wd[fc:fc + nbytes], compressed))
        if bad:
            last_err = "PCD 的 fc/cch 越界（piece %d）" % k
            continue
        meta = {
            "fComplex": f_complex,
            "fWhichTblStm": f_which_tbl,
            "table_stream": tname,
            "pieces": n,
            "fcClx": fc_clx,
            "lcbClx": lcb_clx,
            "cp_max": cps[-1],
            "doc_path": "piece_table",
        }
        return "".join(pieces), meta
    raise ValueError("piece table 解析失败: %s" % (last_err or "未知原因"))


def _doc_direct_text(wd: bytes) -> Tuple[str, Dict[str, Any]]:
    """规范外兜底：fcMin(0x18)/fcMac(0x1C) 直读 + UTF-16-LE 解码。

    仅当文本恰好连续（单 piece）时才正确，属于侥幸路径，必须打 warning。
    """
    fc_min, fc_mac = struct.unpack("<II", wd[0x18:0x20])
    if fc_mac <= fc_min or fc_mac > len(wd):
        raise ValueError("fcMin/fcMac 非法(%d/%d, 流长 %d)" % (fc_min, fc_mac, len(wd)))
    flags = struct.unpack("<H", wd[0x0A:0x0C])[0]
    meta = {
        "fComplex": bool(flags & 0x0004),
        "fWhichTblStm": bool(flags & 0x0200),
        "table_stream": None,
        "pieces": None,
        "fcMin": fc_min,
        "fcMac": fc_mac,
        "doc_path": "fcMin_fcMac_direct",
    }
    return wd[fc_min:fc_mac].decode("utf-16-le", errors="replace"), meta


def _doc_text(data: bytes) -> Tuple[str, str, Dict[str, Any], List[str]]:
    """返回 (text, backend, doc_meta, warn_bits)。

    与旧实现的唯一差异：warning 不再在这里 join，warn_bits 列表经
    ExtractionResult.notes 交给门面统一 join（输出串字节一致）。
    """
    streams, backend = _read_ole_streams(data)
    warn_bits: List[str] = []
    note = streams.pop("__vendor_note__", None)
    if note:
        warn_bits.append(note.decode("utf-8", "replace"))
    if any(k in streams for k in ("EncryptionInfo", "EncryptedPackage")):
        raise PermissionError("文档已加密（存在 EncryptionInfo/EncryptedPackage 流）")
    wd = streams["WordDocument"]
    try:
        raw, meta = _doc_piece_table_text(wd, streams)
    except Exception as e:
        raw, meta = _doc_direct_text(wd)
        warn_bits.append(
            "piece table 解析失败(%s: %s)，退回 fcMin/fcMac 直读——这是 MS-DOC "
            "规范外路径，仅当正文连续时正确，可能漏内容" % (type(e).__name__, e))
    else:
        if meta.get("fComplex"):
            warn_bits.append(
                "FIB fComplex=True（piece table 分段存储），已按规范解析 piece "
                "table（pieces=%s）；该标记只说明存储方式，不代表提取失败"
                % meta.get("pieces"))
    return _clean_word_text(raw), backend, meta, warn_bits


class DocPieceExt(TextExtractor):
    def can_handle(self, doc: ResumeDocument) -> bool:
        return doc.kind == FileKind.DOC

    def extract(self, doc: ResumeDocument) -> ExtractionResult:
        text, backend, meta, warn_bits = _doc_text(doc.data())
        return ExtractionResult(text, None, "ok", backend, warn_bits,
                                meta=meta)
